"""Tests for orphan-buy sweep on position close (Phase 0.1.4).

Live test 2026-05-02 01:15 PT exposed the bug: BB_PURE placed a 22ct
maker buy that partial-filled 1ct, the 1ct was exited via TP, but the
unfilled portion of the original buy (21ct) kept resting on Kalshi.
When BTC ticked through the bid again, those 21ct filled — a phantom
new position the engine didn't initiate.

Fix in _cancel_tp_order:
1. Include pos["order_id"] (the original entry order) in cancel set
2. Sweep ANY resting buy on the ticker via Kalshi truth (belt-and-
   suspenders for any path that placed a buy without tracking the id)
"""
from __future__ import annotations

import asyncio
from typing import Any

import polymarket_copy_engine as pce


class _FakeClient:
    def __init__(self, resting_orders: list[dict] | None = None):
        self._resting = resting_orders or []
        self.cancelled: list[str] = []

    async def cancel_order(self, oid: str):
        self.cancelled.append(oid)
        # Remove from resting if present
        self._resting = [o for o in self._resting if o.get("order_id") != oid]
        return True

    async def get_balance(self):
        class _B:
            balance = 20000
        return _B()

    async def _request(self, method, path, params=None, **_):
        if path == "/portfolio/orders":
            ticker = (params or {}).get("ticker", "")
            return {
                "orders": [o for o in self._resting if o.get("ticker") == ticker]
            }
        return {}


def _make_engine(open_position: dict, resting_orders: list[dict]) -> pce.PolymarketCopyEngine:
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _FakeClient(resting_orders)
    eng._open_position = open_position
    eng._shutting_down = False
    return eng


# ── Test 1: original entry order_id is cancelled on close ────────────────

def test_close_cancels_original_entry_order_id():
    """Tonight's bug: original 22ct maker buy was never cancelled when
    the 1ct partial-fill exited."""
    pos = {
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "order_id": "entry-buy-22ct",
        "tp_order_ids": ["tp-1"],
        "_resting_buy_ids": [],
    }
    eng = _make_engine(open_position=pos, resting_orders=[])
    result = asyncio.run(eng._cancel_tp_order())
    assert result is True
    # Both TP and entry order_id must be cancelled
    assert "tp-1" in eng._client.cancelled
    assert "entry-buy-22ct" in eng._client.cancelled


# ── Test 2: orphan-buy sweep finds buys not tracked in any list ──────────

def test_close_sweeps_untracked_orphan_buy():
    """Belt-and-suspenders: a buy order on this ticker that's NOT in
    `order_id` or `_resting_buy_ids` (e.g., from a stale code path)
    must still be swept via Kalshi-truth query."""
    pos = {
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "order_id": "tracked-entry",
        "tp_order_ids": [],
        "_resting_buy_ids": [],
    }
    resting = [
        {"order_id": "tracked-entry", "ticker": "KXBTC15M-T", "side": "yes", "action": "buy"},
        {"order_id": "untracked-orphan", "ticker": "KXBTC15M-T", "side": "yes", "action": "buy"},
    ]
    eng = _make_engine(open_position=pos, resting_orders=resting)
    asyncio.run(eng._cancel_tp_order())
    # Both must end up cancelled
    assert "tracked-entry" in eng._client.cancelled
    assert "untracked-orphan" in eng._client.cancelled


# ── Test 3: no false-positive sweep on opposite side or other ticker ─────

def test_sweep_ignores_other_side_and_other_tickers():
    """Sweep must NOT cancel buy orders on the opposite side or on
    different tickers (e.g., a separate position in another window)."""
    pos = {
        "ticker": "KXBTC15M-A",
        "side": "yes",
        "order_id": "entry-A",
        "tp_order_ids": [],
        "_resting_buy_ids": [],
    }
    resting = [
        {"order_id": "entry-A", "ticker": "KXBTC15M-A", "side": "yes", "action": "buy"},
        # Opposite side on same ticker — NOT ours, leave alone
        {"order_id": "other-side", "ticker": "KXBTC15M-A", "side": "no", "action": "buy"},
        # Different ticker — note: _request filters by ticker param so this
        # isn't returned in the query, but we test the filter holds
        {"order_id": "other-ticker", "ticker": "KXBTC15M-B", "side": "yes", "action": "buy"},
    ]
    eng = _make_engine(open_position=pos, resting_orders=resting)
    asyncio.run(eng._cancel_tp_order())
    assert "entry-A" in eng._client.cancelled
    assert "other-side" not in eng._client.cancelled
    assert "other-ticker" not in eng._client.cancelled


# ── Test 4: sweep ignores SELL orders (only buys are entry-side) ─────────

def test_sweep_ignores_sell_orders():
    """Resting SELLS are protective orders; the sweep must only cancel
    BUYS to avoid stomping on the protective layer's own sells."""
    pos = {
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "order_id": None,
        "tp_order_ids": [],
        "_resting_buy_ids": [],
    }
    resting = [
        {"order_id": "protective-sell", "ticker": "KXBTC15M-T", "side": "yes", "action": "sell"},
        {"order_id": "leftover-buy", "ticker": "KXBTC15M-T", "side": "yes", "action": "buy"},
    ]
    eng = _make_engine(open_position=pos, resting_orders=resting)
    asyncio.run(eng._cancel_tp_order())
    assert "leftover-buy" in eng._client.cancelled
    assert "protective-sell" not in eng._client.cancelled


# ── Test 5: shutdown gate skips all cancels ──────────────────────────────

def test_shutdown_gate_preserves_orders():
    """During shutdown, _cancel_tp_order must NOT cancel anything —
    let resting orders manage the position through window close."""
    pos = {
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "order_id": "entry-X",
        "tp_order_ids": ["tp-1"],
        "_resting_buy_ids": [],
    }
    eng = _make_engine(open_position=pos, resting_orders=[])
    eng._shutting_down = True
    result = asyncio.run(eng._cancel_tp_order())
    assert result is False
    assert eng._client.cancelled == [], "shutdown must preserve resting orders"
