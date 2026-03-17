# polymarket_stream.py — Polymarket 5m BTC market discovery + WebSocket feed
#
# Discovers the active Polymarket "Bitcoin 5-minute Up/Down" market via the Gamma
# REST API, then subscribes to both outcome tokens (UP and DOWN) on the CLOB
# WebSocket. Maintains a live Poly5mState and writes it into a shared poly_ref
# dict every time the book or a price event arrives.
#
# The poly_ref dict is read by HFTEngine / SignalFusion with no locks — safe
# because all async tasks run in the same asyncio event loop.
#
# Usage:
#   poly_ref = {"state": None}
#   stream = PolymarketStream(poly_ref)
#   asyncio.create_task(stream.run())

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import (
    POLY_WS_URL, POLY_REST_BASE, POLY_GAMMA_BASE,
    POLY_RECONNECT_DELAY, POLY_ENDGAME_SECONDS,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# State model
# ─────────────────────────────────────────────

@dataclass
class Poly5mState:
    """Live state for one Polymarket 5-minute BTC Up/Down market."""
    market_id: str
    market_question: str
    asset_id_up: str
    asset_id_down: str
    window_end_ts: float        # Unix timestamp

    # Best prices in probability space (0.0–1.0)
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None

    # Top-of-book quantities (USDC)
    up_top_qty: Optional[float] = None
    down_top_qty: Optional[float] = None

    last_trade_price_up: Optional[float] = None
    feed_ts: float = 0.0        # time.time() of last update

    @property
    def up_mid(self) -> Optional[float]:
        if self.up_bid is not None and self.up_ask is not None:
            return (self.up_bid + self.up_ask) / 2.0
        return self.up_bid or self.up_ask

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, self.window_end_ts - time.time())

    @property
    def is_endgame(self) -> bool:
        return self.seconds_to_expiry <= POLY_ENDGAME_SECONDS

    @property
    def book_age_ms(self) -> float:
        if self.feed_ts == 0:
            return float("inf")
        return (time.time() - self.feed_ts) * 1000


# ─────────────────────────────────────────────
# Stream
# ─────────────────────────────────────────────

class PolymarketStream:
    """Continuously discovers and streams the active Polymarket BTC 5m market.

    Writes current state into poly_ref["state"] on every update.
    Falls back gracefully if the market isn't found or the feed drops.
    """

    DISCOVERY_INTERVAL = 30.0   # seconds between re-discovery polls
    PING_INTERVAL = 20.0        # WebSocket keepalive ping

    def __init__(self, poly_ref: dict) -> None:
        self._poly_ref = poly_ref
        self._state: Optional[Poly5mState] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def run(self) -> None:
        """Main loop — runs forever as an asyncio task."""
        logger.info("PolymarketStream starting")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            self._session = session
            while True:
                try:
                    market = await self._discover()
                    if market is None:
                        logger.debug("Polymarket: no active BTC 5m market found, retrying in %ds", self.DISCOVERY_INTERVAL)
                        await asyncio.sleep(self.DISCOVERY_INTERVAL)
                        continue
                    logger.info(
                        "Polymarket: found market — %s | ends in %.0fs",
                        market["question"][:60], market["seconds_left"],
                    )
                    await self._stream(market)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("PolymarketStream error: %s — retrying in %ds", e, POLY_RECONNECT_DELAY)
                    await asyncio.sleep(POLY_RECONNECT_DELAY)

    # ── Discovery ─────────────────────────────────────────────────────────

    async def _discover(self) -> Optional[dict]:
        """Find the currently active BTC 5-minute prediction market on Polymarket.

        Returns a dict with: question, asset_id_up, asset_id_down, window_end_ts,
        seconds_left, market_id.
        Returns None if nothing suitable is found.
        """
        try:
            # Gamma API: search for active markets mentioning Bitcoin
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": 100,
                "_paginationParam": "id",
            }
            async with self._session.get(
                f"{POLY_GAMMA_BASE}/markets", params=params
            ) as resp:
                if resp.status != 200:
                    logger.warning("Polymarket discovery: Gamma API returned %d", resp.status)
                    return None
                markets = await resp.json()
        except Exception as e:
            logger.warning("Polymarket discovery REST failed: %s", e)
            return None

        now = time.time()
        candidates = []

        for m in (markets if isinstance(markets, list) else []):
            q = (m.get("question") or "").lower()
            # Match 5-minute BTC/Bitcoin Up/Down markets
            if not any(kw in q for kw in ("bitcoin", "btc")):
                continue
            if not any(kw in q for kw in ("5 min", "5min", "5-min", "5 minute")):
                continue

            end_str = m.get("endDate") or m.get("end_date_iso") or ""
            if not end_str:
                continue
            try:
                end_str_clean = end_str.replace("Z", "+00:00")
                end_ts = datetime.fromisoformat(end_str_clean).timestamp()
            except ValueError:
                continue

            seconds_left = end_ts - now
            # Only consider markets that are active and have time left
            if seconds_left < 5 or seconds_left > 600:
                continue

            # Extract CLOB token IDs
            token_ids_raw = m.get("clobTokenIds") or m.get("clob_token_ids") or "[]"
            if isinstance(token_ids_raw, str):
                try:
                    token_ids = json.loads(token_ids_raw)
                except (json.JSONDecodeError, ValueError):
                    continue
            else:
                token_ids = token_ids_raw

            if len(token_ids) < 2:
                continue

            # Outcomes: first token = first outcome, second = second outcome
            outcomes_raw = m.get("outcomes") or '["Up","Down"]'
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except (json.JSONDecodeError, ValueError):
                    outcomes = ["Up", "Down"]
            else:
                outcomes = outcomes_raw

            # Map UP and DOWN token IDs
            up_idx = next((i for i, o in enumerate(outcomes) if "up" in str(o).lower()), 0)
            down_idx = 1 - up_idx

            candidates.append({
                "market_id": m.get("id") or m.get("conditionId", ""),
                "question": m.get("question", ""),
                "asset_id_up": str(token_ids[up_idx]),
                "asset_id_down": str(token_ids[down_idx]),
                "window_end_ts": end_ts,
                "seconds_left": seconds_left,
            })

        if not candidates:
            return None

        # Pick the soonest-expiring active market (most information-dense)
        return min(candidates, key=lambda c: c["seconds_left"])

    # ── WebSocket stream ──────────────────────────────────────────────────

    async def _stream(self, market: dict) -> None:
        """Connect to Polymarket CLOB WebSocket and stream the given market until expiry."""
        import websockets

        state = Poly5mState(
            market_id=market["market_id"],
            market_question=market["question"],
            asset_id_up=market["asset_id_up"],
            asset_id_down=market["asset_id_down"],
            window_end_ts=market["window_end_ts"],
        )
        self._state = state
        self._poly_ref["state"] = state

        subscribe_msg = json.dumps({
            "type": "market",
            "assets_ids": [market["asset_id_up"], market["asset_id_down"]],
        })

        try:
            async with websockets.connect(
                POLY_WS_URL,
                ping_interval=self.PING_INTERVAL,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                await ws.send(subscribe_msg)
                logger.info(
                    "Polymarket WS subscribed: UP=%s DOWN=%s",
                    market["asset_id_up"][:12], market["asset_id_down"][:12],
                )

                while True:
                    # Exit loop when the window has expired
                    if state.seconds_to_expiry <= 0:
                        logger.info("Polymarket: window expired — %s", market["question"][:50])
                        self._poly_ref["state"] = None
                        return

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        events = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Polymarket sends either a single event dict or a list
                    if isinstance(events, dict):
                        events = [events]

                    for event in events:
                        self._handle_event(event, state, market)

        except Exception as e:
            logger.warning("Polymarket WS disconnected: %s", e)
            self._poly_ref["state"] = None

    # ── Event handlers ────────────────────────────────────────────────────

    def _handle_event(self, event: dict, state: Poly5mState, market: dict) -> None:
        """Dispatch a single WebSocket event to the appropriate handler."""
        etype = event.get("event_type") or event.get("type", "")
        asset_id = event.get("asset_id", "")

        if etype == "book":
            self._apply_book(event, state, asset_id, market)
        elif etype == "price_change":
            self._apply_price_change(event, state, asset_id, market)
        elif etype == "last_trade_price":
            price = float(event.get("price", 0) or 0)
            if asset_id == market["asset_id_up"] and price > 0:
                state.last_trade_price_up = price
                state.feed_ts = time.time()
                self._poly_ref["state"] = state

    def _apply_book(self, event: dict, state: Poly5mState, asset_id: str, market: dict) -> None:
        """Apply a full book snapshot for one outcome token."""
        bids = event.get("bids") or []
        asks = event.get("asks") or []

        best_bid = max((float(b["price"]) for b in bids if b.get("price")), default=None)
        best_ask = min((float(a["price"]) for a in asks if a.get("price")), default=None)
        top_bid_qty = float(bids[0]["size"]) if bids else None
        top_ask_qty = float(asks[0]["size"]) if asks else None

        if asset_id == market["asset_id_up"]:
            state.up_bid = best_bid
            state.up_ask = best_ask
            state.up_top_qty = top_bid_qty or top_ask_qty
        elif asset_id == market["asset_id_down"]:
            state.down_bid = best_bid
            state.down_ask = best_ask
            state.down_top_qty = top_bid_qty or top_ask_qty

        state.feed_ts = time.time()
        self._poly_ref["state"] = state

    def _apply_price_change(self, event: dict, state: Poly5mState, asset_id: str, market: dict) -> None:
        """Apply incremental price change events."""
        changes = event.get("changes") or []
        for change in changes:
            price = float(change.get("price", 0) or 0)
            side = (change.get("side") or "").upper()
            size = float(change.get("size", 0) or 0)

            if not price:
                continue

            if asset_id == market["asset_id_up"]:
                if side == "BUY":
                    state.up_bid = price
                    state.up_top_qty = size
                elif side == "SELL":
                    state.up_ask = price
            elif asset_id == market["asset_id_down"]:
                if side == "BUY":
                    state.down_bid = price
                    state.down_top_qty = size
                elif side == "SELL":
                    state.down_ask = price

        state.feed_ts = time.time()
        self._poly_ref["state"] = state
