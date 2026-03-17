# signal_logger.py — SQLite logging for all signals and trades

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from config import SIGNALS_DB_PATH, TRADES_DB_PATH
from models import ConsensusSignal, TradeRecord

logger = logging.getLogger(__name__)

_CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     INTEGER NOT NULL,
    direction     TEXT NOT NULL,
    confidence    REAL NOT NULL,
    bucket        INTEGER NOT NULL,
    fourier_score REAL NOT NULL,
    aligned_count INTEGER NOT NULL,
    total_tfs     INTEGER NOT NULL,
    tf_scores     TEXT,       -- JSON
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""

_CREATE_KALSHI_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS kalshi_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL UNIQUE,
    placed_at     TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL,
    count         INTEGER NOT NULL,
    limit_price   INTEGER NOT NULL,
    dollar_risk   REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    pnl           REAL,
    filled_count  INTEGER,
    result        TEXT,
    strategy_name TEXT,
    expected_wr   REAL,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""

_CREATE_HFT_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS hft_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_ts_ms        INTEGER NOT NULL,
    ticker            TEXT NOT NULL,
    strategy          TEXT NOT NULL,     -- 'ARB' or 'SCALP'
    decision          TEXT NOT NULL,     -- 'entered' | 'rejected'
    reject_reason     TEXT,              -- why rejected (spread, regime, edge, coord, etc.)
    side              TEXT,              -- 'yes' | 'no' | null for arb
    expected_wr       REAL,              -- regime×session WR used as probability estimate
    market_ask_cents  INTEGER,           -- ask price at evaluation time
    spread_cents      INTEGER,           -- bid-ask spread at evaluation time
    gross_edge_cents  REAL,              -- expected_wr - market_ask (before friction)
    net_edge_cents    REAL,              -- gross_edge - spread (after friction proxy)
    yes_liq           INTEGER,           -- total YES-side contracts in book
    no_liq            INTEGER,           -- total NO-side contracts in book
    minutes_to_expiry REAL,
    regime            TEXT,
    order_id          TEXT,
    entry_limit_cents INTEGER,
    fill_cents        REAL,
    exit_cents        REAL,
    pnl               REAL,
    submit_ts_ms      INTEGER,
    ack_ts_ms         INTEGER,
    exit_ts_ms        INTEGER,
    created_at        INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""

_CREATE_EXECUTION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS execution_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_ts           INTEGER NOT NULL,
    contract_ticker     TEXT,
    side                TEXT,
    session             TEXT,
    regime              TEXT,
    utc_hour            INTEGER,
    strategy_name       TEXT,
    raw_confidence      REAL,
    adjusted_confidence REAL,
    expected_wr         REAL,
    position_scale      REAL,
    minutes_to_expiry   REAL,
    best_bid            INTEGER,
    best_ask            INTEGER,
    spread_cents        REAL,
    display_volume      INTEGER,
    quote_ts_ms         INTEGER,
    decision_ts_ms      INTEGER,
    ack_ts_ms           INTEGER,
    quote_age_ms        INTEGER,
    signal_approved     INTEGER NOT NULL DEFAULT 1,
    pricing_eligible    INTEGER NOT NULL DEFAULT 0,
    liquidity_eligible  INTEGER NOT NULL DEFAULT 0,
    execution_eligible  INTEGER NOT NULL DEFAULT 0,
    submitted           INTEGER NOT NULL DEFAULT 0,
    submit_price        INTEGER,
    submit_ts_ms        INTEGER,
    order_id            TEXT,
    filled              INTEGER NOT NULL DEFAULT 0,
    fill_price          REAL,
    fill_ts_ms          INTEGER,
    quote_3s_bid        INTEGER,
    quote_3s_ask        INTEGER,
    quote_10s_bid       INTEGER,
    quote_10s_ask       INTEGER,
    expiry_outcome      TEXT,
    pnl                 REAL,
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""

_CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL,
    side                TEXT NOT NULL,
    confidence          REAL NOT NULL,
    bucket              INTEGER NOT NULL,
    entry_time          INTEGER NOT NULL,
    resolve_time        INTEGER NOT NULL,
    cycle_open          REAL NOT NULL,
    cycle_close         REAL NOT NULL,
    is_win              INTEGER NOT NULL,
    stake               REAL NOT NULL,
    pnl                 REAL NOT NULL,
    equity_after        REAL NOT NULL,
    score               REAL,
    bull_conf           REAL,
    bear_conf           REAL,
    cycle_return_pct    REAL,
    ema_spread_pct      REAL,
    rsi_val             REAL,
    rel_vol             REAL,
    candle_pressure     REAL,
    fourier_score       REAL,
    aligned_count       INTEGER,
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
)
"""


class SignalLogger:
    """Async SQLite logger for consensus signals and resolved trades."""

    def __init__(
        self,
        signals_db: str = SIGNALS_DB_PATH,
        trades_db: str = TRADES_DB_PATH,
    ) -> None:
        self._signals_db = signals_db
        self._trades_db = trades_db
        self._initialized = False

    async def initialize(self) -> None:
        """Create database files and tables if they don't exist."""
        for path in (self._signals_db, self._trades_db):
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self._signals_db) as db:
            await db.execute(_CREATE_SIGNALS_TABLE)
            await db.commit()

        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(_CREATE_TRADES_TABLE)
            await db.execute(_CREATE_KALSHI_TRADES_TABLE)
            await db.execute(_CREATE_EXECUTION_LOG_TABLE)
            await db.execute(_CREATE_HFT_LOG_TABLE)
            await db.commit()
            # Migration: add columns that may be missing from older schema versions
            migrations = [
                ("kalshi_trades", "strategy_name",   "TEXT"),
                ("kalshi_trades", "expected_wr",     "REAL"),
                # Microstructure columns added in research paper upgrade
                ("hft_log",       "imbalance",          "REAL"),
                ("hft_log",       "microprice_cents",   "REAL"),
                ("hft_log",       "drift_1s_cents",     "REAL"),
                ("hft_log",       "drift_3s_cents",     "REAL"),
                ("hft_log",       "drift_10s_cents",    "REAL"),
                # Polymarket fusion columns
                ("hft_log",       "poly_up_mid",        "REAL"),
                ("hft_log",       "poly_prob_change_3s","REAL"),
                ("hft_log",       "poly_imbalance",     "REAL"),
                ("hft_log",       "poly_noise",         "REAL"),
                ("hft_log",       "fusion_fair_cents",  "REAL"),
            ]
            for table, col, defn in migrations:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                    await db.commit()
                except Exception:
                    pass  # Column already exists — safe to ignore

        self._initialized = True
        logger.info("SignalLogger initialized. signals=%s trades=%s", self._signals_db, self._trades_db)

    async def log_signal(self, signal: ConsensusSignal) -> None:
        """Persist a ConsensusSignal to the signals database."""
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self._signals_db) as db:
            await db.execute(
                """
                INSERT INTO signals
                    (timestamp, direction, confidence, bucket, fourier_score,
                     aligned_count, total_tfs, tf_scores)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.timestamp,
                    signal.direction,
                    signal.confidence,
                    signal.bucket,
                    signal.fourier_score,
                    signal.aligned_count,
                    signal.total_tfs,
                    json.dumps(signal.tf_scores),
                ),
            )
            await db.commit()

    async def log_kalshi_trade(
        self,
        order_id: str,
        placed_at: str,
        ticker: str,
        side: str,
        count: int,
        limit_price: int,
        dollar_risk: float,
        strategy_name: str = "E-DEFAULT",
        expected_wr: float = 63.5,
    ) -> None:
        """Persist a placed Kalshi order to kalshi_trades. Status starts as 'pending'."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO kalshi_trades
                    (order_id, placed_at, ticker, side, count, limit_price, dollar_risk,
                     strategy_name, expected_wr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, placed_at, ticker, side, count, limit_price, dollar_risk,
                 strategy_name, expected_wr),
            )
            await db.commit()

    async def log_kalshi_outcome(
        self,
        order_id: str,
        is_win: bool,
        pnl: float,
        filled_count: int,
        result: str,
        status_override: str = "",
    ) -> None:
        """Update a kalshi_trades row with settlement result.

        status_override: pass "exited_win" / "exited_loss" for early exits so
        they are distinguishable from normal expiry outcomes in the ledger.
        """
        if not self._initialized:
            await self.initialize()
        if status_override:
            status = status_override
        else:
            status = "won" if is_win else ("unfilled" if filled_count == 0 else "lost")
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                UPDATE kalshi_trades
                SET status=?, pnl=?, filled_count=?, result=?
                WHERE order_id=?
                """,
                (status, pnl, filled_count, result, order_id),
            )
            await db.commit()

    async def log_execution(self, **fields) -> int:
        """Insert a row into execution_log for an approved signal. Returns row id."""
        if not self._initialized:
            await self.initialize()
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" * len(fields))
        vals = list(fields.values())
        async with aiosqlite.connect(self._trades_db) as db:
            cur = await db.execute(
                f"INSERT INTO execution_log ({cols}) VALUES ({placeholders})", vals
            )
            await db.commit()
            return cur.lastrowid

    async def update_execution(self, row_id: int, **fields) -> None:
        """Update arbitrary columns on an execution_log row by id."""
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [row_id]
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                f"UPDATE execution_log SET {set_clause} WHERE id=?", vals
            )
            await db.commit()

    async def update_execution_by_order_id(self, order_id: str, **fields) -> None:
        """Update execution_log row matching order_id (set during submission)."""
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [order_id]
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                f"UPDATE execution_log SET {set_clause} WHERE order_id=?", vals
            )
            await db.commit()

    async def log_hft_eval(self, **fields) -> int:
        """Insert one row into hft_log for an HFT evaluation (entered or rejected).

        Returns the row id so the caller can update it with fill/exit data later.
        Required fields: eval_ts_ms, ticker, strategy, decision.
        All other columns are optional and default to NULL.
        """
        if not self._initialized:
            await self.initialize()
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" * len(fields))
        vals = list(fields.values())
        async with aiosqlite.connect(self._trades_db) as db:
            cur = await db.execute(
                f"INSERT INTO hft_log ({cols}) VALUES ({placeholders})", vals
            )
            await db.commit()
            return cur.lastrowid

    async def update_hft_row(self, row_id: int, **fields) -> None:
        """Update arbitrary columns on an hft_log row by id."""
        if not fields or not row_id:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [row_id]
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                f"UPDATE hft_log SET {set_clause} WHERE id=?", vals
            )
            await db.commit()

    async def log_trade(self, trade: TradeRecord) -> None:
        """Persist a resolved TradeRecord to the trades database."""
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT INTO trades
                    (trade_id, side, confidence, bucket, entry_time, resolve_time,
                     cycle_open, cycle_close, is_win, stake, pnl, equity_after,
                     score, bull_conf, bear_conf, cycle_return_pct, ema_spread_pct,
                     rsi_val, rel_vol, candle_pressure, fourier_score, aligned_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_id,
                    trade.side,
                    trade.confidence,
                    trade.bucket,
                    trade.entry_time,
                    trade.resolve_time,
                    trade.cycle_open,
                    trade.cycle_close,
                    1 if trade.is_win else 0,
                    trade.stake,
                    trade.pnl,
                    trade.equity_after,
                    trade.score,
                    trade.bull_conf,
                    trade.bear_conf,
                    trade.cycle_return_pct,
                    trade.ema_spread_pct,
                    trade.rsi_val,
                    trade.rel_vol,
                    trade.candle_pressure,
                    trade.fourier_score,
                    trade.aligned_count,
                ),
            )
            await db.commit()
