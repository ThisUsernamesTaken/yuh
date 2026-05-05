"""Unit test for the FVG live-mode wiring (2026-05-05).

Validates that ``_paper_fvg_live_entry`` plus ``_paper_fvg_live_holding_tick``
correctly route the tier-aware decision through ``self._client.place_order``,
maintain the ticker lock, and transition state cleanly. Mocks the Kalshi
client + WebSocket book so we can exercise the wiring without an engine
boot.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Ensure we import from project root, not site-packages
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeBalance:
    def __init__(self, balance_cents: int):
        self.balance = balance_cents


class _FakeOrder:
    def __init__(self, order_id: str = "OID-ENTRY", filled_count: int = 0):
        self.order_id = order_id
        self.filled_count = filled_count


class _FakeBook:
    def __init__(self, yes_bid=30, no_bid=70):
        self.best_yes_bid = yes_bid
        self.best_no_bid = no_bid


class _FakeWS:
    def __init__(self, book):
        self._book = book

    def get_book(self, ticker):
        return self._book


def _make_engine():
    """Construct a minimal stand-in for PolymarketCopyEngine that exposes
    the live FVG helpers. Avoids a full engine __init__ (which expects a
    bootstrapped client/db/etc.)."""
    from polymarket_copy_engine import PolymarketCopyEngine

    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)

    # Only the fields the live helpers touch:
    eng._paper_fvg = {
        "state": "IDLE",
        "session_id": "",
        "session_open_ts": 0.0,
        "baseline_mids": [],
        "baseline_price": 0,
        "btc_strike": 0.0,
        "entry_side": "",
        "entry_price": 0,
        "entry_ts": 0.0,
        "entry_btc": 0.0,
        "entry_fair": 0,
        "entry_prob": 0.0,
        "entry_edge": 0,
        "entry_vol": 0.0,
        "entry_btc_dist_pct": 0.0,
        "tp_price": 0,
        "sim_balance_cents": 0,
        "sim_contracts": 0,
        "hwm_bid": 0,
        "cycles_this_session": 0,
        "halted": False,
        "live_ticker": "",
        "live_side": "",
        "live_tier": 0,
        "live_entry_px": 0,
        "live_filled_count": 0,
        "live_entry_order_id": "",
        "live_entry_placed_ts": 0.0,
        "live_tp_order_id": "",
        "live_tp_px": 0,
        "live_sl_trig": 0,
        "live_entry_ts": 0.0,
        "live_balance_at_entry": 0,
        "live_day_start_balance": 0,
        "live_day_start_date": "",
    }
    eng._entered_tickers_this_window = set()
    eng._recent_placement_tickers = {}
    eng._add_session_lock = lambda t: eng._entered_tickers_this_window.add(t)
    eng._remove_session_lock = lambda t: eng._entered_tickers_this_window.discard(t)
    eng._client = MagicMock()
    eng._client.get_balance = AsyncMock(return_value=_FakeBalance(5000))  # $50
    eng._client.place_order = AsyncMock()
    eng._client.cancel_order = AsyncMock(return_value=True)
    eng._client.get_positions = AsyncMock(return_value=[])
    eng._kalshi_ws = _FakeWS(_FakeBook(yes_bid=30, no_bid=70))
    eng._reconcile_residual_position = AsyncMock()
    eng._place_capped_side_sell = AsyncMock(return_value=(_FakeOrder(), 0))
    return eng


# ─── Entry path ─────────────────────────────────────────────────────────


def test_live_entry_filled_places_tp_and_transitions_holding():
    """A buy that fills immediately → resting TP placed → state=LIVE_HOLDING."""
    eng = _make_engine()
    # Order fills 17 contracts immediately at our bid+1=31c
    eng._client.place_order.side_effect = [
        _FakeOrder(order_id="OID-ENTRY", filled_count=17),
        _FakeOrder(order_id="OID-TP", filled_count=0),
    ]

    fake_prob = MagicMock(probability=0.5, volatility=20.0)

    asyncio.run(eng._paper_fvg_live_entry(
        ticker="KXBTC15M-T1", side="yes", tier=1,
        baseline=30, fair=70, fvg_vs_baseline=40,
        prob=fake_prob, btc=110000.0, btc_5m_proxy=30.0, aligned=True,
        btc_dist_pct=0.12, session_age=400, seconds_remaining=500,
    ))

    pf = eng._paper_fvg
    assert pf["state"] == "LIVE_HOLDING", f"expected LIVE_HOLDING got {pf['state']}"
    assert pf["live_filled_count"] == 17
    assert pf["live_entry_order_id"] == "OID-ENTRY"
    assert pf["live_tp_order_id"] == "OID-TP"
    assert pf["live_tp_px"] == 31 + 20  # T1 TP = entry + 20
    assert pf["live_sl_trig"] == 31 - 8  # uniform SL = entry - 8
    # Ticker lock added
    assert "KXBTC15M-T1" in eng._entered_tickers_this_window
    # Two place_order calls: entry + TP
    assert eng._client.place_order.call_count == 2
    entry_call = eng._client.place_order.call_args_list[0]
    tp_call = eng._client.place_order.call_args_list[1]
    assert entry_call.kwargs["price"] == 31  # bid(30) + 1
    assert entry_call.kwargs["post_only"] is True
    # Sizing: T1 35% × $50 = $17.50 = 1750c. 1750c / 31c = 56 contracts
    # (well within the 40% exposure cap = 1736c which is just under, so
    # actually 1736/31 = 56 still — cap and frac coincide closely here).
    assert entry_call.kwargs["count"] == 56
    assert tp_call.kwargs["price"] == 51
    assert tp_call.kwargs["action"] == "sell"
    assert tp_call.kwargs["post_only"] is True
    # TP count uses actual fill count, not requested count
    assert tp_call.kwargs["count"] == 17


def test_live_entry_nofill_transitions_pending():
    """A buy with filled_count=0 → state=LIVE_PENDING (NOFILL timer)."""
    eng = _make_engine()
    eng._client.place_order.return_value = _FakeOrder(
        order_id="OID-ENTRY-RESTING", filled_count=0,
    )

    asyncio.run(eng._paper_fvg_live_entry(
        ticker="KXBTC15M-NF", side="yes", tier=2,
        baseline=30, fair=50, fvg_vs_baseline=20,
        prob=MagicMock(probability=0.5, volatility=20.0),
        btc=110000.0, btc_5m_proxy=15.0, aligned=True,
        btc_dist_pct=0.05, session_age=400, seconds_remaining=500,
    ))

    pf = eng._paper_fvg
    assert pf["state"] == "LIVE_PENDING"
    assert pf["live_entry_order_id"] == "OID-ENTRY-RESTING"
    assert pf["live_filled_count"] == 0
    # Ticker lock STAYS even on NOFILL (one attempt per session)
    assert "KXBTC15M-NF" in eng._entered_tickers_this_window
    # Only the entry place_order was called — no TP yet
    assert eng._client.place_order.call_count == 1


def test_live_entry_place_order_failure_releases_lock():
    """If place_order raises → ticker lock released, state stays IDLE."""
    eng = _make_engine()
    eng._client.place_order.side_effect = RuntimeError("post only cross")

    asyncio.run(eng._paper_fvg_live_entry(
        ticker="KXBTC15M-FAIL", side="yes", tier=1,
        baseline=30, fair=70, fvg_vs_baseline=40,
        prob=MagicMock(probability=0.5, volatility=20.0),
        btc=110000.0, btc_5m_proxy=30.0, aligned=True,
        btc_dist_pct=0.12, session_age=400, seconds_remaining=500,
    ))

    pf = eng._paper_fvg
    assert pf["state"] == "IDLE", f"state should remain IDLE on failure, got {pf['state']}"
    # Lock released
    assert "KXBTC15M-FAIL" not in eng._entered_tickers_this_window


def test_live_entry_blocks_on_existing_ticker_lock():
    """If ticker already in _entered_tickers_this_window → no place_order call."""
    eng = _make_engine()
    eng._entered_tickers_this_window.add("KXBTC15M-LOCKED")

    asyncio.run(eng._paper_fvg_live_entry(
        ticker="KXBTC15M-LOCKED", side="yes", tier=1,
        baseline=30, fair=70, fvg_vs_baseline=40,
        prob=MagicMock(probability=0.5, volatility=20.0),
        btc=110000.0, btc_5m_proxy=30.0, aligned=True,
        btc_dist_pct=0.12, session_age=400, seconds_remaining=500,
    ))

    eng._client.place_order.assert_not_called()
    assert eng._paper_fvg["state"] == "IDLE"


def test_live_entry_daily_loss_halt():
    """If real balance dropped 20%+ below day-start → halt + skip."""
    eng = _make_engine()
    eng._paper_fvg["live_day_start_balance"] = 10_000  # $100 day-start
    eng._paper_fvg["live_day_start_date"] = "2026-05-05"
    # Mock TODAY also as 2026-05-05 so the date-rollover branch doesn't
    # overwrite day_start_balance.
    import datetime as _dt
    _today = _dt.datetime.now().strftime("%Y-%m-%d")
    eng._paper_fvg["live_day_start_date"] = _today
    eng._client.get_balance = AsyncMock(return_value=_FakeBalance(7900))  # -21%

    asyncio.run(eng._paper_fvg_live_entry(
        ticker="KXBTC15M-HALT", side="yes", tier=1,
        baseline=30, fair=70, fvg_vs_baseline=40,
        prob=MagicMock(probability=0.5, volatility=20.0),
        btc=110000.0, btc_5m_proxy=30.0, aligned=True,
        btc_dist_pct=0.12, session_age=400, seconds_remaining=500,
    ))

    eng._client.place_order.assert_not_called()
    assert eng._paper_fvg["halted"] is True


# ─── LIVE_HOLDING exit path ─────────────────────────────────────────────


def test_live_holding_tp_filled_via_position_zero():
    """When Kalshi position drops to 0 (TP filled) → close + reconcile + IDLE."""
    eng = _make_engine()
    pf = eng._paper_fvg
    pf["state"] = "LIVE_HOLDING"
    pf["live_ticker"] = "KXBTC15M-T1"
    pf["live_side"] = "yes"
    pf["live_tier"] = 1
    pf["live_entry_px"] = 31
    pf["live_filled_count"] = 17
    pf["live_tp_px"] = 51
    pf["live_sl_trig"] = 23
    pf["live_tp_order_id"] = "OID-TP"
    pf["live_balance_at_entry"] = 5000
    eng._client.get_positions.return_value = []  # no position = TP filled

    asyncio.run(eng._paper_fvg_live_holding_tick(
        now=1000.0, seconds_remaining=300,
    ))

    assert pf["state"] == "IDLE"
    eng._reconcile_residual_position.assert_called_once()
    call = eng._reconcile_residual_position.call_args
    assert call.kwargs["ticker"] == "KXBTC15M-T1"
    assert "FVG-LIVE-CLOSE-tp_filled" in call.kwargs["reason"]


def test_live_holding_sl_hit_triggers_emergency_exit():
    """When current bid drops to or below sl_trig → cancel TP + capped sell."""
    eng = _make_engine()
    pf = eng._paper_fvg
    pf["state"] = "LIVE_HOLDING"
    pf["live_ticker"] = "KXBTC15M-SL"
    pf["live_side"] = "yes"
    pf["live_tier"] = 1
    pf["live_entry_px"] = 31
    pf["live_filled_count"] = 17
    pf["live_tp_px"] = 51
    pf["live_sl_trig"] = 23
    pf["live_tp_order_id"] = "OID-TP"
    pf["live_balance_at_entry"] = 5000

    eng._client.get_positions.return_value = [
        {"ticker": "KXBTC15M-SL", "position": 17, "side": "yes"},
    ]
    # Bid at 22 → below sl_trig=23 → SL fires
    eng._kalshi_ws = _FakeWS(_FakeBook(yes_bid=22, no_bid=78))

    asyncio.run(eng._paper_fvg_live_holding_tick(
        now=1000.0, seconds_remaining=300,
    ))

    assert pf["state"] == "IDLE"
    # TP cancel called
    eng._client.cancel_order.assert_called_with("OID-TP")
    # Capped sell called
    eng._place_capped_side_sell.assert_called_once()
    sell_call = eng._place_capped_side_sell.call_args
    assert sell_call.kwargs["side"] == "yes"
    assert sell_call.kwargs["price"] == 21  # bid(22) - 1
    assert sell_call.kwargs["post_only"] is False
    # Residual reconciler always runs after close
    eng._reconcile_residual_position.assert_called_once()


def test_live_holding_time_exit_under_60s():
    """When seconds_remaining < 60 → flatten regardless of bid."""
    eng = _make_engine()
    pf = eng._paper_fvg
    pf["state"] = "LIVE_HOLDING"
    pf["live_ticker"] = "KXBTC15M-TIME"
    pf["live_side"] = "yes"
    pf["live_tier"] = 1
    pf["live_entry_px"] = 31
    pf["live_filled_count"] = 17
    pf["live_tp_px"] = 51
    pf["live_sl_trig"] = 23
    pf["live_tp_order_id"] = "OID-TP"
    pf["live_balance_at_entry"] = 5000

    eng._client.get_positions.return_value = [
        {"ticker": "KXBTC15M-TIME", "position": 17, "side": "yes"},
    ]
    eng._kalshi_ws = _FakeWS(_FakeBook(yes_bid=40, no_bid=60))

    asyncio.run(eng._paper_fvg_live_holding_tick(
        now=1000.0, seconds_remaining=45,
    ))

    assert pf["state"] == "IDLE"
    eng._client.cancel_order.assert_called_with("OID-TP")
    eng._place_capped_side_sell.assert_called_once()


# ─── LIVE_PENDING ───────────────────────────────────────────────────────


def test_live_pending_late_fill_places_tp_and_holds():
    """If position appears via get_positions before timeout → place TP + LIVE_HOLDING."""
    eng = _make_engine()
    pf = eng._paper_fvg
    pf["state"] = "LIVE_PENDING"
    pf["live_ticker"] = "KXBTC15M-LATE"
    pf["live_side"] = "yes"
    pf["live_tier"] = 1
    pf["live_entry_px"] = 31
    pf["live_tp_px"] = 51
    pf["live_sl_trig"] = 23
    pf["live_entry_order_id"] = "OID-PEND"
    pf["live_entry_placed_ts"] = 1000.0
    pf["live_balance_at_entry"] = 5000

    eng._client.get_positions.return_value = [
        {"ticker": "KXBTC15M-LATE", "position": 17, "side": "yes"},
    ]
    eng._client.place_order.return_value = _FakeOrder(order_id="OID-TP-LATE", filled_count=0)

    asyncio.run(eng._paper_fvg_live_pending_tick(now=1003.0))  # 3s later

    assert pf["state"] == "LIVE_HOLDING"
    assert pf["live_filled_count"] == 17
    assert pf["live_tp_order_id"] == "OID-TP-LATE"
    eng._client.place_order.assert_called_once()


def test_live_pending_timeout_cancels():
    """If 8s elapsed and no fill → cancel + back to IDLE."""
    eng = _make_engine()
    pf = eng._paper_fvg
    pf["state"] = "LIVE_PENDING"
    pf["live_ticker"] = "KXBTC15M-TIMEOUT"
    pf["live_side"] = "yes"
    pf["live_entry_order_id"] = "OID-STALE"
    pf["live_entry_placed_ts"] = 1000.0

    eng._client.get_positions.return_value = []  # no fill

    asyncio.run(eng._paper_fvg_live_pending_tick(now=1009.0))  # 9s elapsed

    assert pf["state"] == "IDLE"
    assert pf["live_entry_order_id"] == ""
    eng._client.cancel_order.assert_called_with("OID-STALE")


if __name__ == "__main__":
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
