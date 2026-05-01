# signal_logger.py — SQLite logging for all signals and trades

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite

try:
    from config import SIGNALS_DB_PATH, TRADES_DB_PATH
except ImportError:
    SIGNALS_DB_PATH = "data/signals.db"
    TRADES_DB_PATH = "data/trades.db"
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
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT NOT NULL UNIQUE,
    placed_at         TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    count             INTEGER NOT NULL,
    limit_price       INTEGER NOT NULL,
    dollar_risk       REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    pnl               REAL,
    filled_count      INTEGER,
    result            TEXT,
    strategy_name     TEXT,
    expected_wr       REAL,
    predicted_prob    REAL,
    fee_estimate_cents REAL,
    signal_wallets    TEXT,
    ladder_detail     TEXT,
    mtf_score         REAL,
    mtf_regime        TEXT,
    created_at        INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
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

# ─────────────────────────────────────────────────────────────────────────────
# Live Kalshi truth tables (2026-04-19) — authoritative ledger, NOT derived
# from DB kalshi_trades.pnl (which drifts from reality by $1000+ lifetime).
# These pull directly from Kalshi REST API every N seconds and become the
# source of truth for performance analytics and withdrawal detection.
# ─────────────────────────────────────────────────────────────────────────────

_CREATE_SETTLEMENT_LEDGER_TABLE = """
CREATE TABLE IF NOT EXISTS settlement_ledger (
    ticker              TEXT PRIMARY KEY,
    market_result       TEXT NOT NULL,         -- 'yes' | 'no' | 'void'
    yes_count           INTEGER NOT NULL DEFAULT 0,
    no_count            INTEGER NOT NULL DEFAULT 0,
    yes_total_cost      REAL NOT NULL DEFAULT 0.0,   -- cents
    no_total_cost       REAL NOT NULL DEFAULT 0.0,   -- cents
    value_cents         REAL NOT NULL DEFAULT 0.0,   -- payout per winning contract (cents)
    revenue_cents       REAL NOT NULL DEFAULT 0.0,   -- total payout (cents)
    fee_cents           REAL NOT NULL DEFAULT 0.0,
    pnl_cents           REAL NOT NULL DEFAULT 0.0,   -- yes_count*value + no_count*(100-value) - yes_cost - no_cost - fee
    settled_time        TEXT,                        -- ISO timestamp from Kalshi
    settled_ts_ms       INTEGER,                     -- parsed unix ms
    first_seen_ts_ms    INTEGER NOT NULL,            -- when engine first recorded this
    raw_json            TEXT                         -- full settlement record for forensics
)
"""

_CREATE_REAL_BALANCE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS real_balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    balance_cents   INTEGER NOT NULL,    -- cash balance from Kalshi API
    portfolio_cents INTEGER NOT NULL,    -- resting order exposure + open positions
    total_cents     INTEGER NOT NULL,    -- balance + portfolio = true account value
    source          TEXT NOT NULL        -- 'poll' | 'manual' | 'reconcile'
)
"""

_CREATE_LEDGER_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS ledger_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms               INTEGER NOT NULL,
    event_type          TEXT NOT NULL,        -- 'withdrawal' | 'deposit' | 'unexplained_drain' | 'unexplained_gain'
    balance_before      INTEGER NOT NULL,     -- cents (total = cash + portfolio)
    balance_after       INTEGER NOT NULL,
    delta_cents         INTEGER NOT NULL,     -- after - before (signed)
    expected_delta      INTEGER NOT NULL,     -- sum of settlement_ledger.pnl_cents between snapshots
    unexplained_cents   INTEGER NOT NULL,     -- delta - expected_delta
    window_seconds      INTEGER NOT NULL,     -- time between snapshots
    note                TEXT
)
"""

_CREATE_HOURLY_PERFORMANCE_TABLE = """
CREATE TABLE IF NOT EXISTS hourly_performance (
    hour_iso        TEXT PRIMARY KEY,    -- 'YYYY-MM-DD HH' UTC
    hour_ts_ms      INTEGER NOT NULL,    -- hour start unix ms
    settlement_cnt  INTEGER NOT NULL DEFAULT 0,
    win_cnt         INTEGER NOT NULL DEFAULT 0,
    loss_cnt        INTEGER NOT NULL DEFAULT 0,
    gross_pnl_cents REAL NOT NULL DEFAULT 0.0,
    fees_cents      REAL NOT NULL DEFAULT 0.0,
    net_pnl_cents   REAL NOT NULL DEFAULT 0.0,
    yes_cnt         INTEGER NOT NULL DEFAULT 0,
    no_cnt          INTEGER NOT NULL DEFAULT 0,
    updated_ts_ms   INTEGER NOT NULL
)
"""

# TP GHOST CROSSINGS (Lever 2 — 2026-04-19)
# Records every time the bid crosses a resting TP price. Measures the gap
# between "paper crossing" and "actual fill" — the ~40% ghost-win rate
# that inflates paper_fvg_trades PnL vs live.
#
# Analysis query:
#   SELECT
#     ROUND(100.0 * SUM(filled) / COUNT(*), 1) as fill_pct,
#     COUNT(*) as total_crossings,
#     SUM(filled) as real_fills,
#     COUNT(*) - SUM(filled) as ghost_crossings
#   FROM tp_ghost_crossings;
_CREATE_TP_GHOST_CROSSINGS_TABLE = """
CREATE TABLE IF NOT EXISTS tp_ghost_crossings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,        -- 'yes' | 'no'
    entry_cents       INTEGER NOT NULL,
    tp_cents          INTEGER NOT NULL,     -- resting TP limit price
    contracts         INTEGER NOT NULL,     -- size of the resting TP at first crossing
    first_cross_ts_ms INTEGER NOT NULL,     -- when bid first >= tp_cents
    bid_max_cents     INTEGER NOT NULL,     -- highest bid seen during crossing
    cross_duration_ms INTEGER NOT NULL,     -- total time bid stayed >= tp_cents
    filled            INTEGER NOT NULL,     -- 0/1 — did the TP fill?
    filled_count      INTEGER NOT NULL DEFAULT 0,
    exit_reason       TEXT,                 -- 'tp_fill' | 'cancel' | 'position_closed' | 'ticker_expired'
    exit_mode         TEXT,                 -- 'HOLD_EXPIRY' | 'SCALP' | ...
    logged_ts_ms      INTEGER NOT NULL
)
"""

# WINDOW SNAPSHOTS (2026-04-22)
# Per-minute capture of within-window state so we can score signal accuracy
# at t=1m, 3m, 6m, 9m, 12m independent of whether we entered/exited at those
# points. PnL-at-settlement is noise; the real question is "at which minute
# was the engine's signal direction correct?" Written every ~60s by the
# fast loop while a ticker is active. Post-hoc join to settlement_ledger
# tells us per-minute directional accuracy and prob-engine Brier score.
_CREATE_WINDOW_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS window_snapshots (
    ticker               TEXT NOT NULL,
    t_offset_sec         INTEGER NOT NULL,    -- seconds since window open (0, 60, 120, ...)
    ts_ms                INTEGER NOT NULL,    -- wall-clock when captured
    btc_price_cents      INTEGER,             -- BTC USD price * 100 (int for precision)
    kalshi_mid_cents     INTEGER,
    kalshi_bid_cents     INTEGER,
    kalshi_ask_cents     INTEGER,
    kalshi_spread_cents  INTEGER,
    model_prob_yes       REAL,                -- 0-1 from Brownian Bridge prob engine
    fvg_signed_cents     INTEGER,             -- + = YES-favoring divergence
    pressure_score       REAL,                -- -1..+1 (from MarketPressure)
    pressure_confidence  REAL,                -- 0..1
    pressure_direction   TEXT,                -- 'yes' | 'no' | ''
    btc_5m_move          REAL,                -- $ change over last 5 min (directional context)
    rsi_1m               REAL,                -- current 1m RSI
    our_position_side    TEXT,                -- 'yes' | 'no' | null if flat
    our_position_entry_c INTEGER,             -- entry price cents if in position
    our_position_qty     INTEGER,
    would_signal_side    TEXT,                -- what the engine WOULD enter if flat ('yes'/'no'/'')
    regime               TEXT,                -- from RegimeClassifier
    drawdown_state       TEXT,                -- from DrawdownTracker
    PRIMARY KEY (ticker, t_offset_sec)
)
"""

# Phase D (2026-04-22): per-session terminal snapshot for cross-session priors.
# Written once per window at close/expiry. Downstream sessions seed their
# regime classifier priors and contract_sr candidate levels from the most
# recent row. Sessions are NOT independent — structure carries over.
_CREATE_SESSION_TERMINALS_TABLE = """
CREATE TABLE IF NOT EXISTS session_terminals (
    ticker                 TEXT PRIMARY KEY,
    window_close_ts_ms     INTEGER NOT NULL,
    terminal_mid_cents     INTEGER,
    terminal_dist_strike   INTEGER,       -- signed $(BTC - strike)
    terminal_regime        TEXT,
    terminal_vol_bps       REAL,
    terminal_pressure      REAL,
    detected_levels_json   TEXT           -- output of contract_sr.snapshot()
)
"""


# ── manual_fills (Claude 2026-04-28) ────────────────────────────────────────
# Captures every Kalshi fill whose order_id is NOT in the engine's known
# engine_order_ids — i.e. user manual trades. Each row snapshots the engine's
# full state at fill time so we can analyze what BTC + book conditions
# correlate with profitable manual entries, and reproduce the alpha
# algorithmically.
#
# Populated by the periodic fills poller in polymarket_copy_engine. Linked to
# settlement_ledger after expiry for outcome attribution.
_CREATE_MANUAL_FILLS_TABLE = """
CREATE TABLE IF NOT EXISTS manual_fills (
    fill_id              TEXT PRIMARY KEY,
    order_id             TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    side                 TEXT,             -- yes | no
    action               TEXT,             -- buy | sell
    count                INTEGER,
    price_cents          INTEGER,
    fee_cents            INTEGER,
    created_at_ms        INTEGER NOT NULL,
    -- engine state snapshot at fill time
    btc_spot_dollars     REAL,
    btc_vel_30s          REAL,
    btc_vel_5m           REAL,
    strike_dollars       REAL,
    dist_from_strike_pct REAL,
    session_minute       REAL,
    seconds_to_expiry    INTEGER,
    yes_bid              INTEGER,
    yes_ask              INTEGER,
    no_bid               INTEGER,
    no_ask               INTEGER,
    cb_aggressor_imb     REAL,
    cb_vwap_dollars      REAL,
    cb_poc_dollars       REAL,
    cb_vol_above_strike  REAL,
    cb_vol_below_strike  REAL,
    cb_volatility        REAL,
    bb_fair_value        INTEGER,
    bb_baseline          INTEGER,
    bb_fvg               INTEGER,
    pressure_score       REAL,
    pressure_conf        REAL,
    pressure_dir         TEXT,
    mtf_score            REAL,
    rsi                  REAL,
    regime               TEXT,
    -- linked at settlement
    settled              INTEGER DEFAULT 0,
    market_result        TEXT,
    pnl_cents            INTEGER,
    inferred_pattern     TEXT,             -- e.g. "hedge_arb", "cheap_scalp"
    snapshot_json        TEXT              -- raw blob for fields not above
)
"""

_CREATE_MANUAL_FILLS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_manual_fills_ticker ON manual_fills(ticker);
CREATE INDEX IF NOT EXISTS idx_manual_fills_created ON manual_fills(created_at_ms);
CREATE INDEX IF NOT EXISTS idx_manual_fills_settled ON manual_fills(settled);
"""

# 2026-04-30: Gate-decision observability table.
# Every entry / stop / TP-flip gate writes a row here so we can correlate
# pass/block decisions with subsequent trade outcomes and tune thresholds.
_CREATE_GATE_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS gate_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    ticker          TEXT,
    gate            TEXT NOT NULL,        -- "entry_flow", "stop_persistence",
                                          -- "protective_order", "book_density"
    decision        TEXT NOT NULL,        -- "pass" | "block" | "defer" | "fire"
    reason          TEXT,                 -- short reason code
    side            TEXT,                 -- yes | no | (null)
    -- Inputs / context (sparse — rows leave irrelevant fields NULL)
    session_min     REAL,
    btc_spot        REAL,
    btc_vel_5m      REAL,
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    no_bid          INTEGER,
    no_ask          INTEGER,
    -- Flow snapshot (last 5/15s)
    flow5_yes_vol   INTEGER,
    flow5_no_vol    INTEGER,
    flow15_yes_vol  INTEGER,
    flow15_no_vol   INTEGER,
    velocity_5s     REAL,
    velocity_15s    REAL,
    -- Density snapshot (top-5 levels)
    yes_bid_density INTEGER,
    no_bid_density  INTEGER,
    density_below_trigger INTEGER,    -- bid volume <= trigger px
    -- Stop-specific
    bid_at_trigger_seconds REAL,      -- duration trigger has been touched
    trigger_volume  INTEGER,          -- volume at-or-below trigger in window
    -- Position context
    position_count  INTEGER,
    entry_cents     INTEGER,
    fill_age_s      REAL,
    -- Linkage
    linked_trade_id TEXT,             -- order_id or fill_id for joining
    -- Catch-all blob for fields not surfaced above
    snapshot_json   TEXT
)
"""

_CREATE_GATE_DECISIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_gate_decisions_ts ON gate_decisions(ts_ms);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_ticker ON gate_decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_gate ON gate_decisions(gate);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_decision ON gate_decisions(decision);
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
            await db.execute(_CREATE_SETTLEMENT_LEDGER_TABLE)
            await db.execute(_CREATE_REAL_BALANCE_SNAPSHOTS_TABLE)
            await db.execute(_CREATE_LEDGER_EVENTS_TABLE)
            await db.execute(_CREATE_HOURLY_PERFORMANCE_TABLE)
            await db.execute(_CREATE_TP_GHOST_CROSSINGS_TABLE)
            await db.execute(_CREATE_WINDOW_SNAPSHOTS_TABLE)
            await db.execute(_CREATE_SESSION_TERMINALS_TABLE)
            await db.execute(_CREATE_MANUAL_FILLS_TABLE)
            for stmt in _CREATE_MANUAL_FILLS_INDEXES.strip().split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            # 2026-04-30: gate decisions
            await db.execute(_CREATE_GATE_DECISIONS_TABLE)
            for stmt in _CREATE_GATE_DECISIONS_INDEXES.strip().split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            await db.commit()
            # Migration: add columns that may be missing from older schema versions
            migrations = [
                ("kalshi_trades", "strategy_name",      "TEXT"),
                ("kalshi_trades", "expected_wr",        "REAL"),
                ("kalshi_trades", "predicted_prob",     "REAL"),
                ("kalshi_trades", "fee_estimate_cents", "REAL"),
                ("kalshi_trades", "signal_wallets",     "TEXT"),
                ("kalshi_trades", "ladder_detail",      "TEXT"),
                ("kalshi_trades", "mtf_score",          "REAL"),
                ("kalshi_trades", "mtf_regime",         "TEXT"),
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

    async def log_gate_decision(self, **fields) -> None:
        """Persist a gate decision to gate_decisions for post-hoc analysis.

        Required fields: gate (str), decision (str), ts_ms (int).
        Everything else is optional and goes to the matching column or
        snapshot_json blob if no column exists.
        """
        if not self._initialized:
            await self.initialize()
        # Whitelist of columns we know about; anything else lands in
        # snapshot_json so the schema stays stable.
        known_cols = {
            "ts_ms", "ticker", "gate", "decision", "reason", "side",
            "session_min", "btc_spot", "btc_vel_5m",
            "yes_bid", "yes_ask", "no_bid", "no_ask",
            "flow5_yes_vol", "flow5_no_vol",
            "flow15_yes_vol", "flow15_no_vol",
            "velocity_5s", "velocity_15s",
            "yes_bid_density", "no_bid_density", "density_below_trigger",
            "bid_at_trigger_seconds", "trigger_volume",
            "position_count", "entry_cents", "fill_age_s",
            "linked_trade_id",
        }
        col_values = {k: fields[k] for k in fields if k in known_cols}
        extras = {k: fields[k] for k in fields if k not in known_cols}
        # Required defaults
        if "ts_ms" not in col_values:
            col_values["ts_ms"] = int(time.time() * 1000)
        if "gate" not in col_values:
            col_values["gate"] = "?"
        if "decision" not in col_values:
            col_values["decision"] = "?"
        if extras:
            try:
                col_values["snapshot_json"] = json.dumps(extras, default=str)
            except Exception:
                col_values["snapshot_json"] = str(extras)
        cols = list(col_values.keys()) + (["snapshot_json"] if "snapshot_json" not in col_values and not extras else [])
        # Build INSERT
        col_list = list(col_values.keys())
        placeholders = ", ".join("?" for _ in col_list)
        col_str = ", ".join(col_list)
        try:
            async with aiosqlite.connect(self._trades_db) as db:
                await db.execute(
                    f"INSERT INTO gate_decisions ({col_str}) VALUES ({placeholders})",
                    tuple(col_values[k] for k in col_list),
                )
                await db.commit()
        except Exception as e:
            logger.warning("log_gate_decision failed: %s", e)

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
        predicted_prob: Optional[float] = None,
        fee_estimate_cents: Optional[float] = None,
        signal_wallets: Optional[str] = None,
        ladder_detail: Optional[str] = None,
        mtf_score: Optional[float] = None,
        mtf_regime: Optional[str] = None,
    ) -> None:
        """Persist a placed Kalshi order to kalshi_trades. Status starts as 'pending'."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO kalshi_trades
                    (order_id, placed_at, ticker, side, count, limit_price, dollar_risk,
                     strategy_name, expected_wr, predicted_prob, fee_estimate_cents,
                     signal_wallets, ladder_detail, mtf_score, mtf_regime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, placed_at, ticker, side, count, limit_price, dollar_risk,
                 strategy_name, expected_wr, predicted_prob, fee_estimate_cents,
                 signal_wallets, ladder_detail, mtf_score, mtf_regime),
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

    # ─────────────────────────────────────────────────────────────────────
    # Live Kalshi truth ledger writers (2026-04-19)
    # ─────────────────────────────────────────────────────────────────────

    async def upsert_settlement(
        self,
        ticker: str,
        market_result: str,
        yes_count: int,
        no_count: int,
        yes_total_cost_cents: float,
        no_total_cost_cents: float,
        value_cents: float,
        revenue_cents: float,
        fee_cents: float,
        pnl_cents: float,
        settled_time: str,
        settled_ts_ms: int,
        first_seen_ts_ms: int,
        raw_json: str,
    ) -> bool:
        """Insert a Kalshi settlement. Returns True if newly inserted, False if ticker already existed.

        Uses INSERT OR IGNORE so replays are idempotent — the first time we see a
        settlement for a given ticker wins. This is the authoritative PnL source.
        """
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO settlement_ledger
                    (ticker, market_result, yes_count, no_count,
                     yes_total_cost, no_total_cost, value_cents, revenue_cents,
                     fee_cents, pnl_cents, settled_time, settled_ts_ms,
                     first_seen_ts_ms, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, market_result, yes_count, no_count,
                    yes_total_cost_cents, no_total_cost_cents,
                    value_cents, revenue_cents, fee_cents, pnl_cents,
                    settled_time, settled_ts_ms, first_seen_ts_ms, raw_json,
                ),
            )
            await db.commit()
            return cur.rowcount > 0

    async def log_real_balance(
        self,
        ts_ms: int,
        balance_cents: int,
        portfolio_cents: int,
        source: str = "poll",
    ) -> None:
        """Append a real-account balance snapshot from Kalshi API."""
        if not self._initialized:
            await self.initialize()
        total = int(balance_cents) + int(portfolio_cents)
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT INTO real_balance_snapshots
                    (ts_ms, balance_cents, portfolio_cents, total_cents, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts_ms, int(balance_cents), int(portfolio_cents), total, source),
            )
            await db.commit()

    async def log_ledger_event(
        self,
        ts_ms: int,
        event_type: str,
        balance_before: int,
        balance_after: int,
        expected_delta: int,
        window_seconds: int,
        note: str = "",
    ) -> None:
        """Record a withdrawal/deposit/unexplained drain event.

        delta = after - before; unexplained = delta - expected_delta.
        event_type:
          - withdrawal:       unexplained < -$1 (real outflow)
          - deposit:          unexplained > +$1 (real inflow)
          - unexplained_drain: smaller drains below threshold
          - unexplained_gain:  smaller gains below threshold
        """
        if not self._initialized:
            await self.initialize()
        delta = int(balance_after) - int(balance_before)
        unexplained = delta - int(expected_delta)
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT INTO ledger_events
                    (ts_ms, event_type, balance_before, balance_after,
                     delta_cents, expected_delta, unexplained_cents,
                     window_seconds, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts_ms, event_type, int(balance_before), int(balance_after),
                 delta, int(expected_delta), unexplained, int(window_seconds), note),
            )
            await db.commit()

    async def upsert_hourly_performance(
        self,
        hour_iso: str,
        hour_ts_ms: int,
        settlement_cnt: int,
        win_cnt: int,
        loss_cnt: int,
        gross_pnl_cents: float,
        fees_cents: float,
        net_pnl_cents: float,
        yes_cnt: int,
        no_cnt: int,
        updated_ts_ms: int,
    ) -> None:
        """Upsert the aggregate performance for a UTC hour.

        Recomputed every poll cycle from settlement_ledger — idempotent.
        """
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT INTO hourly_performance
                    (hour_iso, hour_ts_ms, settlement_cnt, win_cnt, loss_cnt,
                     gross_pnl_cents, fees_cents, net_pnl_cents,
                     yes_cnt, no_cnt, updated_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hour_iso) DO UPDATE SET
                    settlement_cnt=excluded.settlement_cnt,
                    win_cnt=excluded.win_cnt,
                    loss_cnt=excluded.loss_cnt,
                    gross_pnl_cents=excluded.gross_pnl_cents,
                    fees_cents=excluded.fees_cents,
                    net_pnl_cents=excluded.net_pnl_cents,
                    yes_cnt=excluded.yes_cnt,
                    no_cnt=excluded.no_cnt,
                    updated_ts_ms=excluded.updated_ts_ms
                """,
                (hour_iso, hour_ts_ms, settlement_cnt, win_cnt, loss_cnt,
                 gross_pnl_cents, fees_cents, net_pnl_cents,
                 yes_cnt, no_cnt, updated_ts_ms),
            )
            await db.commit()

    async def log_tp_ghost_crossing(
        self,
        ticker: str,
        side: str,
        entry_cents: int,
        tp_cents: int,
        contracts: int,
        first_cross_ts_ms: int,
        bid_max_cents: int,
        cross_duration_ms: int,
        filled: bool,
        filled_count: int,
        exit_reason: str,
        exit_mode: str = "",
    ) -> None:
        """Record a single TP-ghost-crossing event (Lever 2, 2026-04-19).

        Fires once per resting TP that the bid has crossed at least once.
        Comparing ``filled=1`` rows to ``filled=0`` rows reveals the gap
        between "paper crossing" and "actual fill" — the simulation
        artifact that inflates paper_fvg_trades PnL vs live.
        """
        if not self._initialized:
            await self.initialize()
        import time as _t
        async with aiosqlite.connect(self._trades_db) as db:
            await db.execute(
                """
                INSERT INTO tp_ghost_crossings
                    (ticker, side, entry_cents, tp_cents, contracts,
                     first_cross_ts_ms, bid_max_cents, cross_duration_ms,
                     filled, filled_count, exit_reason, exit_mode, logged_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, side, int(entry_cents), int(tp_cents), int(contracts),
                    int(first_cross_ts_ms), int(bid_max_cents), int(cross_duration_ms),
                    1 if filled else 0, int(filled_count),
                    exit_reason, exit_mode, int(_t.time() * 1000),
                ),
            )
            await db.commit()

    async def log_window_snapshot(
        self,
        ticker: str,
        t_offset_sec: int,
        ts_ms: int,
        btc_price_cents: Optional[int] = None,
        kalshi_mid_cents: Optional[int] = None,
        kalshi_bid_cents: Optional[int] = None,
        kalshi_ask_cents: Optional[int] = None,
        kalshi_spread_cents: Optional[int] = None,
        model_prob_yes: Optional[float] = None,
        fvg_signed_cents: Optional[int] = None,
        pressure_score: Optional[float] = None,
        pressure_confidence: Optional[float] = None,
        pressure_direction: Optional[str] = None,
        btc_5m_move: Optional[float] = None,
        rsi_1m: Optional[float] = None,
        our_position_side: Optional[str] = None,
        our_position_entry_c: Optional[int] = None,
        our_position_qty: Optional[int] = None,
        would_signal_side: Optional[str] = None,
        regime: Optional[str] = None,
        drawdown_state: Optional[str] = None,
    ) -> None:
        """Record a per-minute window state snapshot.

        Called ~once per 60s from the fast loop while a ticker is active.
        Post-hoc joins to settlement_ledger let us score WHERE in the 15-min
        arc the engine's signal direction was correct — decoupled from
        whether we actually entered at that moment. The goal is to see the
        signal-accuracy decay curve as the window ages.

        INSERT OR REPLACE means an exact re-fire at the same t_offset_sec
        overwrites (idempotent on re-runs across restarts).
        """
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db, timeout=15.0) as db:
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute(
                """
                INSERT OR REPLACE INTO window_snapshots
                    (ticker, t_offset_sec, ts_ms, btc_price_cents,
                     kalshi_mid_cents, kalshi_bid_cents, kalshi_ask_cents,
                     kalshi_spread_cents, model_prob_yes, fvg_signed_cents,
                     pressure_score, pressure_confidence, pressure_direction,
                     btc_5m_move, rsi_1m, our_position_side,
                     our_position_entry_c, our_position_qty,
                     would_signal_side, regime, drawdown_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, int(t_offset_sec), int(ts_ms),
                    btc_price_cents, kalshi_mid_cents, kalshi_bid_cents,
                    kalshi_ask_cents, kalshi_spread_cents,
                    model_prob_yes, fvg_signed_cents,
                    pressure_score, pressure_confidence, pressure_direction,
                    btc_5m_move, rsi_1m, our_position_side,
                    our_position_entry_c, our_position_qty,
                    would_signal_side, regime, drawdown_state,
                ),
            )
            await db.commit()

    async def log_session_terminal(
        self,
        ticker: str,
        window_close_ts_ms: int,
        terminal_mid_cents: Optional[int] = None,
        terminal_dist_strike: Optional[int] = None,
        terminal_regime: Optional[str] = None,
        terminal_vol_bps: Optional[float] = None,
        terminal_pressure: Optional[float] = None,
        detected_levels_json: Optional[str] = None,
    ) -> None:
        """Phase D: write one session_terminals row at window close."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db, timeout=15.0) as db:
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute(
                """
                INSERT OR REPLACE INTO session_terminals
                (ticker, window_close_ts_ms, terminal_mid_cents,
                 terminal_dist_strike, terminal_regime, terminal_vol_bps,
                 terminal_pressure, detected_levels_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, int(window_close_ts_ms),
                    terminal_mid_cents, terminal_dist_strike,
                    terminal_regime, terminal_vol_bps,
                    terminal_pressure, detected_levels_json,
                ),
            )
            await db.commit()

    async def fetch_latest_session_terminal(self) -> Optional[dict]:
        """Phase D: return the most recent terminal row (by close timestamp)."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db, timeout=15.0) as db:
            await db.execute("PRAGMA busy_timeout=15000")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT ticker, window_close_ts_ms, terminal_mid_cents,
                       terminal_dist_strike, terminal_regime, terminal_vol_bps,
                       terminal_pressure, detected_levels_json
                FROM session_terminals
                ORDER BY window_close_ts_ms DESC
                LIMIT 1
                """,
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetch_recent_hourly_performance(self, hours: int = 24) -> list[dict]:
        """Return the last N hours of aggregate performance, newest first."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT hour_iso, hour_ts_ms, settlement_cnt, win_cnt, loss_cnt,
                       gross_pnl_cents, fees_cents, net_pnl_cents,
                       yes_cnt, no_cnt, updated_ts_ms
                FROM hourly_performance
                ORDER BY hour_ts_ms DESC
                LIMIT ?
                """,
                (int(hours),),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def fetch_last_real_balance(self) -> Optional[dict]:
        """Return the most recent real balance snapshot, or None."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT ts_ms, balance_cents, portfolio_cents, total_cents, source
                FROM real_balance_snapshots
                ORDER BY ts_ms DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def sum_settlement_pnl_between(self, start_ts_ms: int, end_ts_ms: int) -> int:
        """Sum pnl_cents for settlements that actually settled in [start, end].

        Uses settled_ts_ms (when the market resolved on Kalshi's side) rather
        than first_seen_ts_ms (when the engine pulled it into the ledger).
        The former is the true balance-moving event — the latter spikes during
        backfill at startup and would produce false withdrawal/deposit flags.

        Falls back to first_seen_ts_ms only when settled_ts_ms is 0 (which
        should only happen for void settlements missing a settled_time).
        """
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._trades_db) as db:
            cur = await db.execute(
                """
                SELECT COALESCE(SUM(pnl_cents), 0)
                FROM settlement_ledger
                WHERE COALESCE(NULLIF(settled_ts_ms, 0), first_seen_ts_ms) >= ?
                  AND COALESCE(NULLIF(settled_ts_ms, 0), first_seen_ts_ms) <= ?
                """,
                (int(start_ts_ms), int(end_ts_ms)),
            )
            (total,) = await cur.fetchone()
            return int(round(float(total or 0.0)))
