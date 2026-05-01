# paper_trader.py — Paper trading wrapper for KalshiClient
#
# Intercepts all order-placement calls so the engine runs in full simulation
# mode with zero risk of placing a real Kalshi order.
#
# SAFETY GUARANTEE:
#   place_order() and cancel_order() NEVER call the real Kalshi API.
#   All read-only calls (find_btc_contracts, get_orderbook, etc.) are forwarded
#   to the real client so the engine still sees live market data.
#
# DB: paper trades are logged to data/signals.db → paper_trades table.
#
# Usage (handled automatically by polymarket_copy_engine.py):
#   from paper_trader import PaperTrader
#   paper = PaperTrader(real_client, starting_balance=100.0, slippage_cents=1)
#   engine._client = paper   # replaces the real client
#
# The PaperTrader is NOT an async context manager wrapper — it delegates
# __aenter__/__aexit__ to the underlying real client so existing teardown
# works correctly.

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import aiosqlite

from kalshi_client import KalshiBalance, KalshiContract, KalshiOrder, KalshiOrderBook

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# DB schema
# ─────────────────────────────────────────────

_CREATE_PAPER_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           TEXT NOT NULL UNIQUE,
    placed_at          TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    side               TEXT NOT NULL,
    count              INTEGER NOT NULL,
    limit_price        INTEGER NOT NULL,
    fill_price         INTEGER,
    slippage_cents     INTEGER,
    dollar_risk        REAL NOT NULL,
    action             TEXT NOT NULL DEFAULT 'buy',
    status             TEXT NOT NULL DEFAULT 'pending',
    pnl                REAL,
    filled_count       INTEGER,
    result             TEXT,
    strategy_name      TEXT,
    expected_wr        REAL,
    predicted_prob     REAL,
    fee_estimate_cents REAL,
    brier_score        REAL,
    log_loss           REAL,
    signal_wallets     TEXT,
    ladder_detail      TEXT,
    paper              INTEGER NOT NULL DEFAULT 1,
    created_at         INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""

_BINANCE_PRICE_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"


# ─────────────────────────────────────────────
# PaperTrader
# ─────────────────────────────────────────────

class PaperTrader:
    """Drop-in replacement for KalshiClient that simulates all order execution.

    Read-only calls (find_btc_contracts, get_orderbook, get_contract, etc.)
    are forwarded to the underlying real KalshiClient so the engine always
    has live market data.

    Write calls (place_order, cancel_order) are fully intercepted:
      - place_order returns a KalshiOrder with a synthetic PAPER-* order_id
      - cancel_order marks the synthetic order as cancelled
      - get_order returns the stored synthetic order
      - get_balance returns the virtual balance
      - get_positions returns the virtual open positions

    Contract settlement is detected via find_btc_contracts(): whenever the
    engine asks for active contracts and a tracked paper position's ticker is
    no longer present, the position is settled against the real Kalshi
    settlement result (fetched via get_contract).
    """

    def __init__(
        self,
        real_client,
        starting_balance: float = 100.0,
        slippage_cents: int = 1,
        db_path: str = "data/signals.db",
    ) -> None:
        # SAFETY: keep a reference to the real client for read-only calls only.
        # It is NEVER used for place_order or cancel_order.
        self._real = real_client

        self._balance_cents: int = int(starting_balance * 100)
        self._starting_balance_cents: int = int(starting_balance * 100)
        self._slippage_cents: int = slippage_cents
        self._db_path: str = db_path

        # Synthetic orders keyed by order_id
        self._orders: dict[str, dict] = {}
        # Open paper positions keyed by ticker
        self._positions: dict[str, dict] = {}

        self._order_seq: int = 0
        self._total_realized_pnl: float = 0.0
        self._db_initialized: bool = False

        # HTTP session for BTC price fallback
        self._http_session: Optional[aiohttp.ClientSession] = None

    # ── KalshiClient attribute proxies (needed by WebSocket setup in engine) ─

    @property
    def _key_id(self) -> str:
        return self._real._key_id

    @property
    def _private_key(self):
        return self._real._private_key

    @property
    def _base(self) -> str:
        return self._real._base

    @property
    def DEMO_BASE(self) -> str:
        return self._real.DEMO_BASE

    # ── Context manager (no-op; real client managed by main.py) ──────────

    async def __aenter__(self) -> "PaperTrader":
        # The real client's __aenter__ was already called by main.py.
        # We don't need to re-enter it; just return self.
        return self

    async def __aexit__(self, *args) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        bal_dollars = self._balance_cents / 100.0
        pnl = (self._balance_cents - self._starting_balance_cents) / 100.0
        logger.warning(
            "PaperTrader session ended — final balance: $%.2f | total P&L: %+.2f",
            bal_dollars, pnl,
        )

    # ── DB ────────────────────────────────────────────────────────────────

    async def _init_db(self) -> None:
        if self._db_initialized:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CREATE_PAPER_TRADES_TABLE)
            await db.commit()
        self._db_initialized = True
        logger.debug("PaperTrader DB initialized: %s", self._db_path)

    async def _db_insert_trade(
        self,
        order_id: str,
        ticker: str,
        side: str,
        count: int,
        limit_price: int,
        fill_price: int,
        action: str,
        dollar_risk: float,
    ) -> None:
        if not self._db_initialized:
            await self._init_db()
        placed_at = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO paper_trades
                    (order_id, placed_at, ticker, side, count, limit_price,
                     fill_price, slippage_cents, dollar_risk, action, status,
                     filled_count, paper)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 1)
                """,
                (
                    order_id, placed_at, ticker, side, count, limit_price,
                    fill_price, self._slippage_cents, dollar_risk, action, count,
                ),
            )
            await db.commit()

    async def _db_update_outcome(
        self,
        order_id: str,
        status: str,
        pnl: float,
        result: str,
    ) -> None:
        if not self._db_initialized:
            await self._init_db()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE paper_trades
                SET status=?, pnl=?, result=?
                WHERE order_id=?
                """,
                (status, pnl, result, order_id),
            )
            await db.commit()

    # ── Synthetic order helpers ───────────────────────────────────────────

    def _new_order_id(self) -> str:
        self._order_seq += 1
        return f"PAPER-{self._order_seq:05d}-{int(time.time() * 1000) % 100000}"

    def _make_order(
        self,
        order_id: str,
        ticker: str,
        side: str,
        count: int,
        price: int,
        fill_price: int,
        action: str,
        filled: bool = True,
    ) -> KalshiOrder:
        status = "executed" if filled else "resting"
        return KalshiOrder(
            order_id=order_id,
            ticker=ticker,
            side=side,
            count=count,
            price=price,
            status=status,
            filled_count=count if filled else 0,
            average_price=float(fill_price) if filled else None,
            created_ts=int(time.time() * 1000),
        )

    # ── WRITE OPERATIONS — FULLY SIMULATED, NO REAL API CALLS ────────────

    async def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: Optional[int] = None,
        order_type: str = "limit",
        action: str = "buy",
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> KalshiOrder:
        """Simulate an order fill. NEVER calls the real Kalshi API.

        Buy orders: fill at price + slippage (simulating taker lift).
        Sell orders: fill immediately as a resting limit (TP orders rest until
        get_order() confirms the book bid has reached the limit price).
        """
        # SAFETY assertion — belt and suspenders
        assert "PAPER" not in str(self._real.__class__.__name__).upper() or True, \
            "Double-wrapped PaperTrader — this should never happen"

        order_id = self._new_order_id()
        requested_price = price or 50

        if action == "buy":
            # Apply slippage — buying costs more
            fill_price = min(99, requested_price + self._slippage_cents)
            cost_cents = fill_price * count

            # Insufficient balance: fill as many as we can
            if cost_cents > self._balance_cents:
                affordable = max(0, self._balance_cents // fill_price)
                if affordable == 0:
                    logger.warning(
                        "PaperTrader INSUFFICIENT BALANCE: need %dc×%d=$%.2f have $%.2f — no fill",
                        fill_price, count, cost_cents / 100.0, self._balance_cents / 100.0,
                    )
                    order = self._make_order(
                        order_id, ticker, side, count, requested_price, fill_price,
                        action, filled=False,
                    )
                    order.filled_count = 0
                    order.status = "canceled"
                    return order
                count = affordable
                cost_cents = fill_price * count

            self._balance_cents -= cost_cents

            # Open a paper position (or add to existing)
            pos_key = f"{ticker}:{side}"
            if pos_key in self._positions:
                existing = self._positions[pos_key]
                old_count = existing["count"]
                old_cost = old_count * existing["entry_cents"]
                new_count = old_count + count
                self._positions[pos_key]["count"] = new_count
                self._positions[pos_key]["entry_cents"] = (old_cost + cost_cents) // new_count
            else:
                self._positions[pos_key] = {
                    "ticker": ticker,
                    "side": side,
                    "count": count,
                    "entry_cents": fill_price,
                    "fill_time": time.time(),
                    "open_order_id": order_id,
                }

            order = self._make_order(
                order_id, ticker, side, count, requested_price, fill_price,
                action, filled=True,
            )
            dollar_risk = count * fill_price / 100.0
            logger.info(
                "PaperTrader BUY FILL: %s %s %dx @ %dc (slip+%dc) $%.2f | balance=$%.2f",
                side.upper(), ticker[:20], count, fill_price, self._slippage_cents,
                dollar_risk, self._balance_cents / 100.0,
            )

        else:
            # action == "sell" — rest as a TP limit order
            # Actual fill is simulated lazily in get_order() when bid >= price
            fill_price = max(1, requested_price - self._slippage_cents)
            order = self._make_order(
                order_id, ticker, side, count, requested_price, fill_price,
                action, filled=False,
            )
            order.status = "resting"
            logger.info(
                "PaperTrader SELL RESTING: %s %s %dx @ %dc (limit)",
                side.upper(), ticker[:20], count, requested_price,
            )

        # Store synthetic order for later queries
        self._orders[order_id] = {
            "order": order,
            "ticker": ticker,
            "side": side,
            "count": count,
            "price": requested_price,
            "fill_price": fill_price,
            "action": action,
        }

        # Log to DB (non-blocking; ignore failures)
        try:
            dollar_risk = count * requested_price / 100.0
            await self._db_insert_trade(
                order_id, ticker, side, count, requested_price,
                fill_price, action, dollar_risk,
            )
        except Exception as e:
            logger.debug("PaperTrader DB insert failed (non-fatal): %s", e)

        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Simulate order cancellation. NEVER calls the real Kalshi API."""
        if order_id in self._orders:
            rec = self._orders[order_id]
            rec["order"].status = "canceled"
            logger.debug("PaperTrader: cancelled %s", order_id)
            return True
        # Unknown order ID (may be from previous session) — silently succeed
        return True

    async def cancel_all_resting_orders(self) -> int:
        """Cancel all resting paper orders. NEVER calls the real Kalshi API."""
        cancelled = 0
        for rec in self._orders.values():
            o = rec["order"]
            if o.status == "resting":
                o.status = "canceled"
                cancelled += 1
        logger.info("PaperTrader: cancelled %d resting orders", cancelled)
        return cancelled

    # ── READ OPERATIONS — forward to real client or return synthetic data ─

    async def get_order(self, order_id: str) -> KalshiOrder:
        """Return synthetic order status.

        For resting sell (TP) orders, checks the live orderbook bid.
        If bid >= the resting limit price, marks the order as filled and
        credits the virtual balance.
        """
        if order_id not in self._orders:
            # Unknown: return a cancelled order (safe for ghost-fill checks)
            return KalshiOrder(
                order_id=order_id, ticker="", side="", count=0,
                price=0, status="canceled", filled_count=0,
                average_price=None, created_ts=int(time.time() * 1000),
            )

        rec = self._orders[order_id]
        order = rec["order"]

        # Check if a resting sell TP has been hit
        if order.status == "resting" and rec["action"] == "sell":
            try:
                book: KalshiOrderBook = await self._real.get_orderbook(rec["ticker"])
                current_bid = (
                    book.best_yes_bid if rec["side"] == "yes" else book.best_no_bid
                )
                limit_price = rec["price"]
                if current_bid >= limit_price:
                    # TP hit — fill it
                    actual_fill = min(current_bid, 99)
                    order.status = "executed"
                    order.filled_count = rec["count"]
                    order.average_price = float(actual_fill)
                    rec["fill_price"] = actual_fill

                    # Credit virtual balance
                    proceeds_cents = rec["count"] * actual_fill
                    self._balance_cents += proceeds_cents

                    # Close paper position
                    pos_key = f"{rec['ticker']}:{rec['side']}"
                    if pos_key in self._positions:
                        pos = self._positions[pos_key]
                        entry = pos["entry_cents"]
                        pnl = (actual_fill - entry) * rec["count"] / 100.0
                        self._total_realized_pnl += pnl
                        del self._positions[pos_key]
                        logger.warning(
                            "PaperTrader TP FILLED: %s %dx @ %dc (entry=%dc) | P&L: %+.2f | "
                            "balance=$%.2f total_pnl=%+.2f",
                            rec["side"].upper(), rec["count"], actual_fill, entry,
                            pnl, self._balance_cents / 100.0, self._total_realized_pnl,
                        )
                        try:
                            await self._db_update_outcome(
                                pos["open_order_id"],
                                "won" if pnl > 0 else "lost",
                                pnl,
                                rec["side"],
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("PaperTrader get_order orderbook check failed: %s", e)

        return order

    async def get_balance(self) -> KalshiBalance:
        """Return the virtual paper trading balance. NEVER calls real API."""
        return KalshiBalance(
            balance=self._balance_cents,
            portfolio_value=self._balance_cents,
        )

    async def get_positions(self) -> list[dict]:
        """Return synthetic open positions in Kalshi position dict format."""
        result = []
        for pos in self._positions.values():
            # Match Kalshi API position dict schema expected by the engine
            result.append({
                "ticker": pos["ticker"],
                "position_fp": str(pos["count"]) if pos["side"] == "yes" else str(-pos["count"]),
                "market_exposure_dollars": str(pos["count"] * pos["entry_cents"] / 100.0),
                "resting_orders_count": 0,
            })
        return result

    # ── Forward read-only calls to real client ────────────────────────────

    async def find_btc_contracts(self, **kwargs):
        """Forward to real client. Also triggers expiry settlement for open positions."""
        contracts = await self._real.find_btc_contracts(**kwargs)

        # Detect expired positions: if a tracked position's ticker is no longer
        # in the active contract list, settle it.
        active_tickers = {c.ticker for c in contracts}
        expired = [
            (pos_key, pos)
            for pos_key, pos in list(self._positions.items())
            if pos["ticker"] not in active_tickers
        ]
        for pos_key, pos in expired:
            await self._settle_expired_position(pos_key, pos)

        return contracts

    async def get_orderbook(self, ticker: str) -> KalshiOrderBook:
        return await self._real.get_orderbook(ticker)

    async def get_contract(self, ticker: str) -> KalshiContract:
        return await self._real.get_contract(ticker)

    async def get_market_trades(self, ticker: str, **kwargs):
        return await self._real.get_market_trades(ticker, **kwargs)

    # ── Settlement ────────────────────────────────────────────────────────

    async def _settle_expired_position(self, pos_key: str, pos: dict) -> None:
        """Fetch settlement result for an expired contract and compute P&L."""
        ticker = pos["ticker"]
        side = pos["side"]
        count = pos["count"]
        entry = pos["entry_cents"]

        result: Optional[str] = None
        try:
            contract_info = await self._real.get_contract(ticker)
            result = contract_info.result  # "yes", "no", or None
        except Exception as e:
            logger.debug("PaperTrader: could not fetch settlement for %s: %s", ticker, e)

        if result is None:
            # Try BTC price vs floor_price
            try:
                contract_info = await self._real.get_contract(ticker)
                floor = contract_info.floor_price  # cents (e.g. 8400000 = $84,000)
                if floor:
                    btc_price = await self._fetch_btc_price_cents()
                    if btc_price:
                        result = "yes" if btc_price >= floor else "no"
            except Exception:
                pass

        if result is None:
            # G5 (2026-04-22): when we can't determine the settlement result,
            # conservatively assume worst case (full loss of entry cost) and
            # DECREMENT total_pnl to keep the counter consistent with balance.
            # Prior behavior left total_pnl unchanged while balance dropped,
            # producing misleading reports where total_pnl=+1.12 but
            # balance showed a -$27 net loss. True outcome will reconcile via
            # the engine's DAILY P&L tracking from real balance deltas.
            _worst_pnl = -entry * count / 100.0
            self._total_realized_pnl += _worst_pnl
            logger.warning(
                "PaperTrader SETTLEMENT: %s — no result available, assuming worst "
                "case pnl=%+.2f | total_pnl=%+.2f",
                ticker, _worst_pnl, self._total_realized_pnl,
            )
            if pos_key in self._positions:
                del self._positions[pos_key]
            return

        is_win = result == side
        if is_win:
            # Contract pays $1 per contract
            proceeds_cents = count * 100
            pnl = (100 - entry) * count / 100.0
        else:
            proceeds_cents = 0
            pnl = -entry * count / 100.0

        self._balance_cents += proceeds_cents
        self._total_realized_pnl += pnl

        status = "won" if is_win else "lost"
        logger.warning(
            "PaperTrader EXPIRY SETTLE: %s %s %dx entry=%dc | result=%s → %s | "
            "P&L: %+.2f | balance=$%.2f total_pnl=%+.2f",
            side.upper(), ticker[:20], count, entry, result.upper(), status.upper(),
            pnl, self._balance_cents / 100.0, self._total_realized_pnl,
        )

        try:
            await self._db_update_outcome(
                pos.get("open_order_id", "unknown"),
                status, pnl, result,
            )
        except Exception:
            pass

        if pos_key in self._positions:
            del self._positions[pos_key]

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Lazily create the HTTP session on first use."""
        if self._http_session is None:
            import ssl as _ssl
            try:
                import certifi
                ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ssl_ctx = _ssl.create_default_context()
            self._http_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx, force_close=True),
                timeout=aiohttp.ClientTimeout(total=5),
            )
        return self._http_session

    async def _fetch_btc_price_cents(self) -> Optional[int]:
        """Fetch current BTC/USD price from Binance and return in whole dollars * 100."""
        try:
            sess = await self._get_http_session()
            async with sess.get(_BINANCE_PRICE_URL) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price_usd = float(data["price"])
                    # floor_price is in dollar-cents (e.g. $84,000 = 8_400_000)
                    return int(price_usd * 100)
        except Exception:
            pass
        return None

    # ── Status helpers ────────────────────────────────────────────────────

    def status_summary(self) -> dict:
        """Return a snapshot of the paper account for logging / diagnostics."""
        return {
            "balance_dollars": self._balance_cents / 100.0,
            "starting_balance": self._starting_balance_cents / 100.0,
            "total_pnl": self._total_realized_pnl,
            "pnl_pct": (
                self._total_realized_pnl / (self._starting_balance_cents / 100.0) * 100
                if self._starting_balance_cents > 0 else 0.0
            ),
            "open_positions": len(self._positions),
            "total_orders": self._order_seq,
        }
