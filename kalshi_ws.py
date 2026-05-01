# kalshi_ws.py — Kalshi WebSocket client for real-time orderbook data
#
# Provides tick-by-tick mid-price updates via the orderbook_delta channel.
# Uses the same RSA-PSS auth as the REST client.
#
# Usage:
#     ws = KalshiWebSocket(key_id, pem_str, on_mid_update=callback)
#     await ws.connect()
#     await ws.subscribe("KXBTC15M-26MAR312200-00")
#     # callback fires on every orderbook change with (ticker, mid_cents)
#     await ws.close()

import asyncio
import base64
import json
import logging
import time
from collections import defaultdict, deque
from typing import Callable, Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


class TradeFlowTracker:
    """Tracks real-time trade flow from Kalshi websocket.

    Measures buy vs sell pressure, trade velocity, and directional momentum
    from actual fills — not just orderbook levels.
    """

    def __init__(self, window_s: float = 60.0):
        self._window_s = window_s
        self._trades: deque = deque()  # (timestamp, side, count, price_cents)
        self.yes_volume = 0
        self.no_volume = 0
        self.yes_trades = 0
        self.no_trades = 0
        self.last_trade_side = ""
        self.last_trade_price = 0
        self.velocity_cps = 0.0  # cents per second price movement
        self._price_samples: deque = deque(maxlen=20)  # (ts, mid) for velocity

    def reset(self):
        self._trades.clear()
        self._price_samples.clear()
        self.yes_volume = self.no_volume = 0
        self.yes_trades = self.no_trades = 0
        self.last_trade_side = ""
        self.last_trade_price = 0
        self.velocity_cps = 0.0

    def on_trade(self, side: str, count: int, price_cents: int):
        """Record a trade from the websocket."""
        now = time.time()
        self._trades.append((now, side, count, price_cents))
        self.last_trade_side = side
        self.last_trade_price = price_cents
        self._prune()

    def on_mid_update(self, mid_cents: int):
        """Track mid-price for velocity calculation."""
        now = time.time()
        self._price_samples.append((now, mid_cents))
        if len(self._price_samples) >= 2:
            first_ts, first_mid = self._price_samples[0]
            elapsed = now - first_ts
            if elapsed > 0:
                self.velocity_cps = (mid_cents - first_mid) / elapsed

    def _prune(self):
        """Remove trades outside the rolling window."""
        cutoff = time.time() - self._window_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()
        # Recompute
        self.yes_volume = self.no_volume = 0
        self.yes_trades = self.no_trades = 0
        for _, side, count, _ in self._trades:
            if side == "yes":
                self.yes_volume += count
                self.yes_trades += 1
            else:
                self.no_volume += count
                self.no_trades += 1

    @property
    def buy_pressure(self) -> float:
        """YES buy pressure as fraction 0.0-1.0. >0.5 = net YES buying."""
        total = self.yes_volume + self.no_volume
        return self.yes_volume / total if total > 0 else 0.5

    @property
    def flow_direction(self) -> str:
        """'up' if YES pressure dominates, 'down' if NO, '' if balanced."""
        bp = self.buy_pressure
        if bp >= 0.6:
            return "up"
        elif bp <= 0.4:
            return "down"
        return ""

    @property
    def flow_strength(self) -> float:
        """0.0-1.0, how one-sided the flow is."""
        return abs(self.buy_pressure - 0.5) * 2

    @property
    def total_volume(self) -> int:
        return self.yes_volume + self.no_volume

    @property
    def is_active(self) -> bool:
        return len(self._trades) >= 3


class LocalOrderBook:
    """Maintains a local copy of the Kalshi orderbook from snapshot + deltas."""

    def __init__(self):
        # {price_cents: quantity} for each side
        self.yes_bids: dict[int, int] = {}
        self.no_bids: dict[int, int] = {}
        self._initialized = False

    def apply_snapshot(self, data: dict) -> None:
        """Apply a full orderbook snapshot."""
        self.yes_bids.clear()
        self.no_bids.clear()

        yes_key = "yes_dollars_fp" if "yes_dollars_fp" in data else "yes_dollars" if "yes_dollars" in data else "yes"
        no_key = "no_dollars_fp" if "no_dollars_fp" in data else "no_dollars" if "no_dollars" in data else "no"

        for level in data.get(yes_key, []):
            price_cents, qty = self._parse_level(level)
            if qty > 0:
                self.yes_bids[price_cents] = qty

        for level in data.get(no_key, []):
            price_cents, qty = self._parse_level(level)
            if qty > 0:
                self.no_bids[price_cents] = qty

        self._initialized = True

    def apply_delta(self, data: dict) -> None:
        """Apply an incremental orderbook update."""
        if not self._initialized:
            return

        price_raw = data.get("price_dollars_fp") or data.get("price_dollars") or data.get("price")
        delta_raw = data.get("delta_fp") or data.get("delta")
        side = data.get("side", "").lower()

        if price_raw is None or delta_raw is None:
            return

        # Parse price to cents
        try:
            price_f = float(price_raw)
            price_cents = round(price_f * 100) if price_f < 1.0 else int(price_f)
        except (ValueError, TypeError):
            return

        # Parse delta
        try:
            delta = int(float(delta_raw))
        except (ValueError, TypeError):
            return

        book = self.yes_bids if side == "yes" else self.no_bids
        current = book.get(price_cents, 0)
        new_qty = current + delta

        if new_qty <= 0:
            book.pop(price_cents, None)
        else:
            book[price_cents] = new_qty

    @property
    def best_yes_bid(self) -> int:
        return max(self.yes_bids.keys()) if self.yes_bids else 0

    @property
    def best_no_bid(self) -> int:
        return max(self.no_bids.keys()) if self.no_bids else 0

    @property
    def best_yes_ask(self) -> int:
        """YES ask = 100 - best NO bid."""
        return (100 - self.best_no_bid) if self.best_no_bid else 0

    @property
    def best_no_ask(self) -> int:
        """NO ask = 100 - best YES bid."""
        return (100 - self.best_yes_bid) if self.best_yes_bid else 0

    @property
    def mid_price_cents(self) -> int:
        """YES mid-price in cents."""
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid and ask:
            return (bid + ask) // 2
        return bid or ask or 50

    @property
    def is_ready(self) -> bool:
        return self._initialized

    # ── Density / depth helpers (2026-04-30) ────────────────────────────
    # Used by entry-flow gate + stop-persistence gate to evaluate how
    # well-supported a price level is. Thin density at trigger price =
    # real risk of falling-knife move. Thick density = noise filter.

    def density(self, side: str, levels: int = 5) -> dict:
        """Return depth of the bid stack for `side` (top N price levels).

        side: "yes" or "no" — which bid stack to read.
        Returns {top_volume, level_count, max_level_volume, levels: [(px, qty), ...]}.
        Levels are sorted highest-price-first (top of book first).
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        if not book:
            return {"top_volume": 0, "level_count": 0,
                    "max_level_volume": 0, "levels": []}
        sorted_levels = sorted(book.items(), key=lambda kv: -kv[0])[:max(1, levels)]
        top_volume = sum(qty for _, qty in sorted_levels)
        max_level_vol = max((qty for _, qty in sorted_levels), default=0)
        return {
            "top_volume": top_volume,
            "level_count": len(sorted_levels),
            "max_level_volume": max_level_vol,
            "levels": sorted_levels,
        }

    def volume_at_or_below(self, side: str, threshold_cents: int) -> int:
        """Sum bid quantity for `side` at prices <= threshold_cents.

        For a YES position with stop trigger at 24c, this answers:
          "How much resting YES-bid volume is there at 24c or below?"
        Thick volume below trigger = real support, defer stop. Thin = real
        risk, fire stop. Used by stop-persistence gate.
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        return sum(qty for px, qty in book.items() if px <= threshold_cents)

    def volume_at_or_above(self, side: str, threshold_cents: int) -> int:
        """Sum bid quantity for `side` at prices >= threshold_cents.

        Used to evaluate same-side bid wall at-or-above entry — strong
        wall above entry on our side = TP feasibility for protective-order
        mode.
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        return sum(qty for px, qty in book.items() if px >= threshold_cents)

    def imbalance_ratio(self, levels: int = 5) -> float:
        """Top-N depth ratio of YES bids vs NO bids. Range [0, 1].

        > 0.5 = more YES-buyers (bullish bias)
        < 0.5 = more NO-buyers (bearish bias)
        = 0.5 = balanced.
        """
        yd = self.density("yes", levels)["top_volume"]
        nd = self.density("no", levels)["top_volume"]
        total = yd + nd
        if total <= 0:
            return 0.5
        return yd / total

    @staticmethod
    def _parse_level(level) -> tuple[int, int]:
        """Parse a [price_dollars_str, qty_str] level from Kalshi orderbook."""
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return 0, 0
        try:
            price_raw = float(str(level[0]))
            qty = int(float(str(level[1])))
            # Kalshi WS sends dollar prices as strings ("0.4200") — convert to cents
            price_cents = round(price_raw * 100)
            return price_cents, qty
        except (ValueError, TypeError):
            return 0, 0


class KalshiWebSocket:
    """Async WebSocket client for Kalshi real-time orderbook data.

    Connects, authenticates via RSA-PSS, subscribes to orderbook_delta
    for specified tickers, maintains local orderbook, and fires callbacks
    on every mid-price change.
    """

    def __init__(
        self,
        key_id: str,
        private_key_pem: str,
        demo: bool = False,
        on_mid_update: Optional[Callable[[str, int], None]] = None,
        on_trade: Optional[Callable[[str, dict], None]] = None,
    ):
        self._key_id = key_id
        pem_bytes = (
            private_key_pem.encode()
            if isinstance(private_key_pem, str)
            else private_key_pem
        )
        self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        self._ws_url = DEMO_WS_URL if demo else PROD_WS_URL
        self._on_mid_update = on_mid_update
        self._on_trade = on_trade

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._books: dict[str, LocalOrderBook] = defaultdict(LocalOrderBook)
        self._flows: dict[str, TradeFlowTracker] = defaultdict(TradeFlowTracker)
        self._subscribed_tickers: set[str] = set()
        self._sid_to_ticker: dict[str, str] = {}  # subscription_id → ticker
        self._latest_ticker: str = ""  # Most recently subscribed ticker
        self._msg_id = 0
        self._running = False
        self._reconnect_delay = 2.0

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _auth_headers(self) -> dict:
        ts = str(int(time.time() * 1000))
        # WebSocket auth signs GET /trade-api/ws/v2
        path = "/trade-api/ws/v2"
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, "GET", path),
        }

    def get_book(self, ticker: str) -> LocalOrderBook:
        """Get the local orderbook for a ticker."""
        return self._books[ticker]

    def get_mid(self, ticker: str) -> int:
        """Get current mid-price for a ticker. Returns 50 if no data."""
        book = self._books.get(ticker)
        return book.mid_price_cents if book and book.is_ready else 50

    def get_flow(self, ticker: str) -> TradeFlowTracker:
        """Get trade flow tracker for a ticker."""
        return self._flows[ticker]

    def get_open_burst(self, ticker: str, window_open_time: float, threshold: int = 4, max_age_s: float = 15.0) -> Optional[str]:
        """Check if the first trades after window open cluster in one direction.

        Returns 'yes' or 'no' if threshold of first 5 trades agree, None otherwise.
        Only fires within max_age_s of window_open_time.
        """
        now = time.time()
        if now - window_open_time > max_age_s:
            return None

        flow = self._flows.get(ticker)
        if not flow or len(flow._trades) < threshold:
            return None

        # Only look at trades since window open
        recent = [(ts, side, ct, px) for ts, side, ct, px in flow._trades if ts >= window_open_time]
        if len(recent) < threshold:
            return None

        first_n = recent[:5]
        yes_ct = sum(1 for _, s, _, _ in first_n if s == "yes")
        no_ct = sum(1 for _, s, _, _ in first_n if s == "no")

        if yes_ct >= threshold:
            return "yes"
        elif no_ct >= threshold:
            return "no"
        return None

    async def connect(self) -> None:
        """Connect and start the message loop. Reconnects on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                if self._running:
                    logger.warning("KalshiWS: connection error: %s — reconnecting in %.0fs", e, self._reconnect_delay)
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 1.5, 30.0)

    async def _connect_and_listen(self) -> None:
        """Single connection attempt + message loop."""
        import ssl as _ssl
        try:
            import certifi
            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = _ssl.create_default_context()

        self._session = aiohttp.ClientSession()
        try:
            headers = self._auth_headers()
            self._ws = await self._session.ws_connect(
                self._ws_url,
                headers=headers,
                ssl=ssl_ctx,
                heartbeat=30,
            )
            logger.info("KalshiWS: connected to %s", self._ws_url)
            self._reconnect_delay = 2.0

            # Re-subscribe to any tickers
            for ticker in self._subscribed_tickers:
                await self._send_subscribe(ticker)

            # Message loop
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break

        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session:
                await self._session.close()
            self._ws = None
            self._session = None

    async def subscribe(self, ticker: str) -> None:
        """Subscribe to orderbook updates for a ticker."""
        self._subscribed_tickers.add(ticker)
        self._latest_ticker = ticker
        if self._ws and not self._ws.closed:
            await self._send_subscribe(ticker)

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe from a ticker."""
        self._subscribed_tickers.discard(ticker)
        self._books.pop(ticker, None)
        if self._ws and not self._ws.closed:
            self._msg_id += 1
            await self._ws.send_json({
                "id": self._msg_id,
                "cmd": "unsubscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": ticker,
                },
            })

    async def _send_subscribe(self, ticker: str) -> None:
        """Send subscription message for orderbook + trades."""
        self._msg_id += 1
        self._sid_to_ticker[str(self._msg_id)] = ticker
        await self._ws.send_json({
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "trade"],
                "market_ticker": ticker,
            },
        })
        logger.info("KalshiWS: subscribed to %s", ticker)

    async def _handle_message(self, data: dict) -> None:
        """Route incoming WebSocket messages."""
        msg_type = data.get("type", "")
        # Always route orderbook data to the latest subscribed ticker.
        # Kalshi reuses the same sid for all snapshots — sid-based routing
        # maps to stale tickers. We only trade one contract at a time.
        ticker = self._latest_ticker or data.get("market_ticker", "") or str(data.get("sid", ""))

        if msg_type == "orderbook_snapshot":
            book = self._books[ticker]
            orderbook_data = data.get("msg", data)
            try:
                book.apply_snapshot(orderbook_data)
                if self._on_mid_update:
                    self._on_mid_update(ticker, book.mid_price_cents)
                logger.info("KalshiWS: snapshot for %s (mid=%dc, yes_bids=%d, no_bids=%d)",
                            str(ticker)[-15:], book.mid_price_cents, len(book.yes_bids), len(book.no_bids))
            except Exception as e:
                import traceback
                logger.error("KalshiWS: snapshot parse error: %s\n%s", e, traceback.format_exc())

        elif msg_type == "orderbook_delta":
            book = self._books[ticker]
            old_mid = book.mid_price_cents
            delta_data = data.get("msg", data)
            book.apply_delta(delta_data)
            new_mid = book.mid_price_cents
            if new_mid != old_mid:
                self._flows[ticker].on_mid_update(new_mid)
                if self._on_mid_update:
                    self._on_mid_update(ticker, new_mid)

        elif msg_type == "trade":
            trade_msg = data.get("msg", data)
            # Feed into flow tracker
            try:
                trade_side = str(trade_msg.get("taker_side", trade_msg.get("side", ""))).lower()
                trade_count = int(float(trade_msg.get("count", trade_msg.get("count_fp", 1))))
                trade_price_raw = trade_msg.get("yes_price_dollars_fp") or trade_msg.get("yes_price") or trade_msg.get("price", "0.5")
                trade_price_cents = round(float(str(trade_price_raw)) * 100) if float(str(trade_price_raw)) < 1.0 else int(float(str(trade_price_raw)))
                flow = self._flows[ticker]
                # Taker buying YES = "yes" side
                if trade_side in ("yes", "buy"):
                    flow.on_trade("yes", trade_count, trade_price_cents)
                elif trade_side in ("no", "sell"):
                    flow.on_trade("no", trade_count, trade_price_cents)
            except Exception:
                pass
            if self._on_trade:
                self._on_trade(ticker, trade_msg)

        elif msg_type == "subscribed":
            # Map any sid from confirmation to ticker
            confirm_sid = str(data.get("sid", data.get("id", "")))
            if confirm_sid and confirm_sid in self._sid_to_ticker:
                pass  # Already mapped from subscribe call
            logger.info("KalshiWS: confirmed subscription to %s", data.get("msg", {}).get("channel", "?"))

        elif msg_type == "error":
            logger.error("KalshiWS: error: %s", data)

    async def close(self) -> None:
        """Shut down the WebSocket connection."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
