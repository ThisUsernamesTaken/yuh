"""Tests for the cache-lag race fix in _maintain_protective_order
(Phase 0.1.7, 2026-05-02 13:32 PT live postmortem).

Bug: BB_PURE bought 40ct YES at 51c. ~100s later the protective sell
filled and closed the position cleanly at effective 64c (+~$5 profit).
The protective sell's order_id wasn't in `_engine_order_ids` so it was
labeled "MANUAL FILL". Engine state still thought +40 YES.

47s after the actual close, the MID-TRADE BTC velocity SL fired
(BTC dropping at -22.9 $/s). Engine placed a new sell-yes 40ct at 72c
to flatten its phantom +40 YES. Kalshi already had us at 0 YES — so
the matching engine treated the new sell-yes as opening a SHORT YES
position. User saw a synthetic +40 NO position appear and had to
manually flatten.

Fix: after fill_time + PROTECTIVE_FLAT_CONFIRM_S seconds, if Kalshi's
positions API explicitly returns 0, treat the position as closed.
Clear engine state and stop.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import polymarket_copy_engine as pce


class _FakeClient:
    def __init__(self, kalshi_count: int):
        self._kalshi_count = kalshi_count
        self.cancelled: list[str] = []
        self.placed_orders: list[dict[str, Any]] = []

    async def get_positions(self):
        return [
            {
                "ticker": "KXBTC15M-T",
                "position": self._kalshi_count if self._kalshi_count >= 0
                            else 0,
                "position_fp": str(self._kalshi_count),
                "side": "yes",
            }
        ]

    async def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True

    async def get_order(self, oid):
        return _FakeOrder(oid, status="filled")

    async def _request(self, method, path, params=None, **_):
        if path == "/portfolio/orders":
            return {"orders": []}
        return {}

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        return _FakeOrder("oid_x", count=kwargs.get("count", 0),
                          filled_count=kwargs.get("count", 0))

    async def get_balance(self):
        class _B: balance = 20000
        return _B()


class _FakeOrder:
    def __init__(self, oid, count=0, filled_count=0, status="filled"):
        self.order_id = oid
        self.count = count
        self.filled_count = filled_count
        self.status = status


def _make_engine(kalshi_count: int, fill_age_s: float, eng_count: int = 40):
    """Minimal engine shell with the bare attributes _maintain_protective_order
    touches before its FLAT-CONFIRM check."""
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _FakeClient(kalshi_count)
    eng._kalshi_ws = None  # protective will return False before book check
    eng._signal_logger = None
    eng._open_position = {
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "count": eng_count,
        "original_entry_cents": 51,
        "entry_cents": 51,
        "strategy_name": "BB_PURE",
        "fill_time": time.time() - fill_age_s,
        "tp_order_ids": [],
        "_resting_buy_ids": [],
        "_protective_order_id": "old-tp",
        "_protective_order_px": 81,
        "_protective_order_count": 40,
    }
    eng._closed_tickers = set()
    eng._shutting_down = False
    eng._pre_expiry_consolidating = False
    eng._window_start_time = time.time() - 30.0
    return eng


def _set_uc(monkeypatch, **kwargs):
    """Patch the _uc helper in polymarket_copy_engine to return our values."""
    orig = pce._uc

    def _patched(name, default=None):
        if name in kwargs:
            return kwargs[name]
        return orig(name, default)

    monkeypatch.setattr(pce, "_uc", _patched)


def test_flat_confirm_clears_when_kalshi_zero_after_window(monkeypatch):
    """Kalshi truth = 0, fill_age = 60s (>30s window) → clear and return."""
    eng = _make_engine(kalshi_count=0, fill_age_s=60.0)
    _set_uc(
        monkeypatch,
        PROTECTIVE_ORDER_MODE=True,
        KALSHI_TAPE_ENABLED=True,
        PROTECTIVE_FLAT_CONFIRM_S=30.0,
    )
    result = asyncio.run(eng._maintain_protective_order())
    assert result is True, "must return True (owned, no action)"
    assert eng._open_position is None, "engine state must be cleared"
    assert eng._client.placed_orders == [], "no new sells placed"


def test_no_clear_when_kalshi_zero_inside_lag_window(monkeypatch):
    """Kalshi 0 but fill_age < 30s → still trust engine state."""
    eng = _make_engine(kalshi_count=0, fill_age_s=10.0)
    _set_uc(
        monkeypatch,
        PROTECTIVE_ORDER_MODE=True,
        KALSHI_TAPE_ENABLED=True,
        PROTECTIVE_FLAT_CONFIRM_S=30.0,
    )
    asyncio.run(eng._maintain_protective_order())
    # Position should NOT be cleared during the lag window
    assert eng._open_position is not None, (
        "position must NOT be cleared inside the cache-lag window — "
        "Kalshi might be lagging behind a recent BUY fill"
    )


def test_no_clear_when_kalshi_still_positive(monkeypatch):
    """Kalshi shows +40 → genuinely still holding, don't clear."""
    eng = _make_engine(kalshi_count=40, fill_age_s=120.0)
    _set_uc(
        monkeypatch,
        PROTECTIVE_ORDER_MODE=True,
        KALSHI_TAPE_ENABLED=True,
        PROTECTIVE_FLAT_CONFIRM_S=30.0,
    )
    asyncio.run(eng._maintain_protective_order())
    # Falls through normal flow; position not cleared
    assert eng._open_position is not None


def test_today_actual_scenario_cleared(monkeypatch):
    """The actual scenario from 2026-05-02 13:32 PT, replayed.
    Engine state +40 YES, Kalshi 0, fill_age ~120s. With the fix,
    engine clears state instead of placing a phantom-flatten sell."""
    eng = _make_engine(kalshi_count=0, fill_age_s=120.0, eng_count=40)
    _set_uc(
        monkeypatch,
        PROTECTIVE_ORDER_MODE=True,
        KALSHI_TAPE_ENABLED=True,
        PROTECTIVE_FLAT_CONFIRM_S=30.0,
    )
    asyncio.run(eng._maintain_protective_order())
    assert eng._open_position is None
    assert eng._client.placed_orders == [], (
        "Pre-fix: engine would have placed a sell-yes 40ct that Kalshi "
        "would treat as a NEW SHORT YES, creating the synthetic +40 NO "
        "the user had to manually flatten. Post-fix: zero placements."
    )
