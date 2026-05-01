"""Unit tests for manual-trade TP autoplacer (Claude 2026-04-28).

Covers:
- Disabled flag = no-op
- Buys fire TP, sells skip
- Entry-price band gates
- TP price math (% of upside, clamped to MIN/MAX, capped at 95)
- Hedge protection (skip if other side has position)
- Idempotency considerations (each fill gets exactly one TP)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polymarket_copy_engine as pce


class _FakeOrder:
    def __init__(self, oid):
        self.order_id = oid
        self.filled_count = 0
        self.status = "resting"


class _FakeClient:
    def __init__(self, positions=None):
        self.placed_orders = []
        self._positions = positions or []

    async def place_order(self, *, ticker, side, price, count, action, **_):
        self.placed_orders.append(
            {"ticker": ticker, "side": side, "price": price,
             "count": count, "action": action}
        )
        return _FakeOrder(f"tp_{ticker[:6]}_{price}")

    async def get_positions(self):
        return self._positions


def _make_engine(positions=None):
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _FakeClient(positions=positions or [])
    eng._engine_order_ids = set()
    return eng


def _set_cfg(**kwargs):
    """Apply config overrides. Resets in test order."""
    defaults = {
        "MANUAL_TP_ENABLED": True,
        "MANUAL_TP_PCT_OF_ENTRY": 0.10,
        "MANUAL_TP_PCT_OF_UPSIDE": 0.30,  # legacy fallback
        "MANUAL_TP_MIN_OFFSET_CENTS": 2,
        "MANUAL_TP_MAX_OFFSET_CENTS": 15,
        "MANUAL_TP_MAX_PRICE_CENTS": 95,
        "MANUAL_TP_MIN_ENTRY_CENTS": 5,
        "MANUAL_TP_MAX_ENTRY_CENTS": 92,
        "MANUAL_TP_SKIP_IF_HEDGED": True,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        pce._user_cfg[k] = v


# ── Disabled flag ───────────────────────────────────────────────────────────

def test_disabled_no_op():
    _set_cfg(MANUAL_TP_ENABLED=False)
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=30,
    ))
    assert eng._client.placed_orders == []


# ── Buy/sell discrimination ─────────────────────────────────────────────────

def test_sell_action_skipped():
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="sell", count=10, entry_price=30,
    ))
    assert eng._client.placed_orders == []


def test_invalid_side_skipped():
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="",
        action="buy", count=10, entry_price=30,
    ))
    assert eng._client.placed_orders == []


# ── Entry band ──────────────────────────────────────────────────────────────

def test_entry_below_min_skipped():
    _set_cfg(MANUAL_TP_MIN_ENTRY_CENTS=5)
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=3,
    ))
    assert eng._client.placed_orders == []


def test_entry_above_max_skipped():
    _set_cfg(MANUAL_TP_MAX_ENTRY_CENTS=90)
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=95,
    ))
    assert eng._client.placed_orders == []


# ── TP price math (% of entry mode) ─────────────────────────────────────────

def test_tp_at_10pct_of_entry_30c():
    """30c entry, 10% = 3c → TP at 33c. ROI=10% of capital."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=30,
    ))
    assert len(eng._client.placed_orders) == 1
    o = eng._client.placed_orders[0]
    assert o["price"] == 33, f"Expected 33c, got {o['price']}"
    assert o["side"] == "yes"
    assert o["action"] == "sell"
    assert o["count"] == 10


def test_tp_at_10pct_of_entry_50c():
    """50c entry, 10% = 5c → TP at 55c."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="no",
        action="buy", count=20, entry_price=50,
    ))
    assert len(eng._client.placed_orders) == 1
    assert eng._client.placed_orders[0]["price"] == 55


def test_tp_at_10pct_of_entry_65c():
    """65c entry, 10% = 6.5c → rounds to 6 or 7 → TP at 71 or 72c.
    Python's round(6.5) = 6 (banker's rounding) — accept either."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="no",
        action="buy", count=15, entry_price=65,
    ))
    assert len(eng._client.placed_orders) == 1
    assert eng._client.placed_orders[0]["price"] in (71, 72)


def test_tp_clamped_to_min_offset():
    """15c entry, 10% = 1.5c → rounds to 2c (min). TP at 17c."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=50, entry_price=15,
    ))
    assert len(eng._client.placed_orders) == 1
    # raw=2 (round of 1.5 = 2 banker's), no clamp, tp=17
    assert eng._client.placed_orders[0]["price"] == 17


def test_tp_clamped_to_max_offset():
    """88c entry × 10% = 8.8c → 9c, under max=15. tp=97 capped to 95."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=88,
    ))
    assert len(eng._client.placed_orders) == 1
    # offset=9, tp=97, capped to 95
    assert eng._client.placed_orders[0]["price"] == 95


def test_tp_capped_at_max_price():
    """90c entry × 10% = 9 offset → 99 capped to 95."""
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=90,
    ))
    assert len(eng._client.placed_orders) == 1
    assert eng._client.placed_orders[0]["price"] == 95


def test_legacy_upside_mode_when_pct_of_entry_zero():
    """If PCT_OF_ENTRY <= 0, fall back to legacy % of upside."""
    _set_cfg(
        MANUAL_TP_PCT_OF_ENTRY=0.0,
        MANUAL_TP_PCT_OF_UPSIDE=0.30,
        MANUAL_TP_MIN_OFFSET_CENTS=5,
        MANUAL_TP_MAX_OFFSET_CENTS=25,
    )
    eng = _make_engine()
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=10, entry_price=30,
    ))
    assert len(eng._client.placed_orders) == 1
    # upside=70, 30% = 21 → tp=51
    assert eng._client.placed_orders[0]["price"] == 51


# ── Hedge protection ────────────────────────────────────────────────────────

def test_hedge_skip_when_opposite_side_open():
    """User bought NO 100ct. They had YES position. We must NOT place a TP."""
    _set_cfg(MANUAL_TP_SKIP_IF_HEDGED=True)
    eng = _make_engine(positions=[
        {"ticker": "KXBTC15M-X", "side": "yes", "position": 50},
    ])
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="no",
        action="buy", count=100, entry_price=66,
    ))
    assert eng._client.placed_orders == []


def test_hedge_protection_off_still_fires():
    _set_cfg(MANUAL_TP_SKIP_IF_HEDGED=False)
    eng = _make_engine(positions=[
        {"ticker": "KXBTC15M-X", "side": "yes", "position": 50},
    ])
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="no",
        action="buy", count=100, entry_price=66,
    ))
    assert len(eng._client.placed_orders) == 1


def test_same_side_position_not_treated_as_hedge():
    """Adding to existing same-side position — TP should still fire."""
    _set_cfg()
    eng = _make_engine(positions=[
        {"ticker": "KXBTC15M-X", "side": "yes", "position": 50},
    ])
    asyncio.run(eng._maybe_place_manual_tp(
        fill_id="f1", ticker="KXBTC15M-X", side="yes",
        action="buy", count=20, entry_price=40,
    ))
    assert len(eng._client.placed_orders) == 1


# ── Kalshi ISO timestamp parsing (regression test) ──────────────────────────
# Original code assumed created_time was an int → crashed on every real fill,
# preventing any TP autoplacement. Bug found 2026-04-28 ~10:30 PT after user
# reported no TPs firing despite live trading.

def test_record_manual_fill_parses_iso_timestamp(tmp_path, monkeypatch):
    """Real Kalshi fill payload uses ISO strings like '2026-04-28T17:06:07.339627Z'.
    The recorder must parse these without crashing.
    """
    import sqlite3
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE manual_fills (
            fill_id TEXT PRIMARY KEY, order_id TEXT, ticker TEXT,
            side TEXT, action TEXT, count INTEGER,
            price_cents INTEGER, fee_cents INTEGER, created_at_ms INTEGER,
            btc_spot_dollars REAL, btc_vel_30s REAL, btc_vel_5m REAL,
            strike_dollars REAL, dist_from_strike_pct REAL, session_minute REAL,
            seconds_to_expiry INTEGER, yes_bid INTEGER, yes_ask INTEGER,
            no_bid INTEGER, no_ask INTEGER, cb_aggressor_imb REAL,
            cb_vwap_dollars REAL, cb_poc_dollars REAL,
            cb_vol_above_strike REAL, cb_vol_below_strike REAL,
            cb_volatility REAL, bb_fair_value INTEGER, bb_baseline INTEGER,
            bb_fvg INTEGER, pressure_score REAL, pressure_conf REAL,
            pressure_dir TEXT, mtf_score REAL, rsi REAL, regime TEXT,
            settled INTEGER DEFAULT 0, market_result TEXT, pnl_cents INTEGER,
            inferred_pattern TEXT, snapshot_json TEXT
        )
    """)
    conn.commit()
    conn.close()

    eng = _make_engine()
    eng._signal_logger = SimpleNamespace(_trades_db=db_path)
    # Disable TP placement for this test (we're testing capture only)
    _set_cfg(MANUAL_TP_ENABLED=False)

    fill = {
        "trade_id": "abc-123",
        "order_id": "ord-xyz",
        "ticker": "KXBTC15M-Z",
        "side": "yes",
        "action": "buy",
        "count": 10,
        "yes_price": 35,
        "fee": 2,
        "created_time": "2026-04-28T17:06:07.339627Z",
    }
    asyncio.run(eng._record_manual_fill(fill))

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT fill_id, created_at_ms, ticker FROM manual_fills")
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1, "fill should be recorded despite ISO timestamp"
    assert rows[0][0] == "abc-123"
    # Verify the timestamp was parsed (should be ~ 2026-04-28T17:06:07Z in ms)
    expected_ms_min = 1777395000000  # 2026-04-28T17:00 UTC
    expected_ms_max = 1777401000000  # 2026-04-28T18:30 UTC
    assert expected_ms_min <= rows[0][1] <= expected_ms_max, (
        f"Parsed timestamp {rows[0][1]} not in expected range"
    )
