"""Tests for ORPHAN-FLATTEN recency protection (P0b, 2026-05-02 evening).

Live postmortem: Trade 9 at 18:15 PT — BB_PURE FILL confirmed 50 NO at 18:15:36.
At 18:16:05 (29s later), FLAT-CONFIRMED fired on a single Kalshi=0 cache blip
and cleared engine state. 3s after that, ORPHAN-FLATTEN polled, saw the
(still-real) 50 NO position re-appear with no `_open_position`, and crossed
at bid-5c. Locked in $2.50 loss on a healthy trade.

Fix P0b: orphan-flatten now respects `_recent_placement_tickers`. If a ticker
has had any engine place_order in the last `ORPHAN_FLATTEN_RECENT_S` seconds
(default 90), it is SKIPPED — let protective_maintain handle it. This was the
original 2026-04 behavior; the c044d04 "remove recency protection" commit
removed it to handle side-flips, but it caused this bug.

(Side-flip handling now relies on FLAT-CONFIRMED multi-reading + sell-helper
OVERSELL-GUARD to prevent the original side-flip pattern that c044d04 was
trying to fix.)
"""
from __future__ import annotations

import asyncio
import time
import polymarket_copy_engine as pce


class _FakeBook:
    is_ready = True
    best_yes_bid = 39
    best_no_bid = 61
    best_yes_ask = 40
    best_no_ask = 62


class _FakeWS:
    def get_book(self, ticker):
        return _FakeBook()


class _FakeClient:
    def __init__(self, positions: list[dict]):
        self._positions = positions
        self.placed_orders: list[dict] = []
        self.cancelled_ids: list[str] = []

    async def get_positions(self):
        return self._positions

    async def _request(self, method, path, params=None, **_):
        if path == "/portfolio/orders":
            return {"orders": []}
        return {}

    async def cancel_order(self, oid):
        self.cancelled_ids.append(oid)
        return True

    async def get_order(self, oid):
        class _O:
            order_id = oid
            status = "filled"
            filled_count = 0
        return _O()

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)

        class _O:
            order_id = f"oid_{len(self.placed_orders)}"
            count = kwargs.get("count", 0)
            filled_count = kwargs.get("count", 0)

        return _O()


def _make_engine(positions, recent_placements=None, open_position=None):
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _FakeClient(positions)
    eng._kalshi_ws = _FakeWS()
    eng._open_position = open_position
    eng._shutting_down = False
    eng._recent_placement_tickers = recent_placements or {}
    return eng


def _set_uc(monkeypatch, **kwargs):
    orig = pce._uc

    def _patched(name, default=None):
        if name in kwargs:
            return kwargs[name]
        return orig(name, default)

    monkeypatch.setattr(pce, "_uc", _patched)


# Helper to run ONE iteration of the orphan-flatten loop (the loop itself
# is infinite — we extract the body via the exception path).
async def _one_iter(eng, monkeypatch):
    """Run a single orphan-flatten iteration by patching asyncio.sleep
    to raise after the first call. The loop's await-sleep at the end of
    each iteration becomes our exit point."""
    orig_sleep = asyncio.sleep
    call_count = {"n": 0}

    async def _patched_sleep(seconds):
        call_count["n"] += 1
        # The loop has TWO sleeps: one initial 15s boot wait, one in the
        # while loop. Skip the boot, then raise after the in-loop sleep.
        if call_count["n"] == 1:
            return  # boot sleep — no-op
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", _patched_sleep)
    try:
        await eng._orphan_flatten_loop()
    except asyncio.CancelledError:
        pass
    finally:
        monkeypatch.setattr(asyncio, "sleep", orig_sleep)


def test_orphan_skipped_when_recent_placement(monkeypatch):
    """The actual bug: position appears on a ticker we placed an order on
    recently. Orphan-flatten must SKIP rather than cross at bid-5c."""
    now = time.time()
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-T", "position_fp": "-50"}],  # 50 NO
        recent_placements={"KXBTC15M-T": now - 30.0},  # placed 30s ago
        open_position=None,  # state cleared (FLAT-CONFIRMED fired)
    )
    _set_uc(
        monkeypatch,
        ORPHAN_FLATTEN_ENABLED=True,
        ORPHAN_FLATTEN_POLL_S=1.0,
        ORPHAN_FLATTEN_RECENT_S=90.0,
        ORPHAN_FLATTEN_OFFSET_C=5,
    )
    asyncio.run(_one_iter(eng, monkeypatch))
    assert eng._client.placed_orders == [], (
        "tickers with recent placements must be skipped — let "
        "protective_maintain handle them, not orphan-flatten"
    )


def test_orphan_flattens_when_no_recent_placement(monkeypatch):
    """Genuine orphan: no recent engine activity on this ticker.
    Orphan-flatten should still fire (the original use case)."""
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-X", "position_fp": "-50"}],
        recent_placements={},  # no placements anywhere
        open_position=None,
    )
    _set_uc(
        monkeypatch,
        ORPHAN_FLATTEN_ENABLED=True,
        ORPHAN_FLATTEN_POLL_S=1.0,
        ORPHAN_FLATTEN_RECENT_S=90.0,
        ORPHAN_FLATTEN_OFFSET_C=5,
    )
    asyncio.run(_one_iter(eng, monkeypatch))
    # Should have placed a sell to flatten (50 NO short → sell-no @ no_bid-5)
    assert len(eng._client.placed_orders) == 1, (
        "genuine orphan (no recent placement) must still be flattened"
    )
    o = eng._client.placed_orders[0]
    assert o["action"] == "sell"
    assert o["side"] == "no"


def test_orphan_skipped_when_active_position(monkeypatch):
    """Active engine position on the ticker — protective_maintain handles it."""
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-A", "position_fp": "30"}],
        recent_placements={},
        open_position={"ticker": "KXBTC15M-A", "side": "yes", "count": 30},
    )
    _set_uc(
        monkeypatch,
        ORPHAN_FLATTEN_ENABLED=True,
        ORPHAN_FLATTEN_POLL_S=1.0,
        ORPHAN_FLATTEN_RECENT_S=90.0,
        ORPHAN_FLATTEN_OFFSET_C=5,
    )
    asyncio.run(_one_iter(eng, monkeypatch))
    assert eng._client.placed_orders == [], (
        "active position must NOT be flattened by orphan-flatten"
    )


def test_orphan_flattens_when_recent_placement_too_old(monkeypatch):
    """Placement happened > recency window ago → ticker is no longer
    'recent' → orphan-flatten fires normally."""
    now = time.time()
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-T", "position_fp": "-50"}],
        recent_placements={"KXBTC15M-T": now - 200.0},  # 200s ago, > 90s window
        open_position=None,
    )
    _set_uc(
        monkeypatch,
        ORPHAN_FLATTEN_ENABLED=True,
        ORPHAN_FLATTEN_POLL_S=1.0,
        ORPHAN_FLATTEN_RECENT_S=90.0,
        ORPHAN_FLATTEN_OFFSET_C=5,
    )
    asyncio.run(_one_iter(eng, monkeypatch))
    assert len(eng._client.placed_orders) == 1, (
        "stale recent_placements (older than window) shouldn't protect "
        "from orphan-flatten"
    )


def test_today_actual_bug_replayed(monkeypatch):
    """The exact scenario from 2026-05-02 18:16 PT.

    BB_PURE FILL confirmed 50 NO @ 18:15:36 → engine has _open_position.
    FLAT-CONFIRMED fires on a Kalshi=0 blip @ 18:16:05 → engine clears state.
    Orphan-flatten polls @ 18:16:08 → sees 50 NO with no engine state.

    Pre-fix: ORPHAN-FLATTEN fires at bid-5c, locks $2.50 loss.
    Post-fix: skipped because ticker has recent placement (32s ago).
    """
    now = time.time()
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-26MAY022130-30", "position_fp": "-50"}],
        recent_placements={"KXBTC15M-26MAY022130-30": now - 32.0},  # the entry
        open_position=None,  # FLAT-CONFIRMED cleared it
    )
    _set_uc(
        monkeypatch,
        ORPHAN_FLATTEN_ENABLED=True,
        ORPHAN_FLATTEN_POLL_S=3.0,
        ORPHAN_FLATTEN_RECENT_S=90.0,
        ORPHAN_FLATTEN_OFFSET_C=5,
    )
    asyncio.run(_one_iter(eng, monkeypatch))
    assert eng._client.placed_orders == [], (
        "PRE-FIX: orphan-flatten would have crossed bid-5c here, locking "
        "$2.50 loss. POST-FIX: skipped because of recent placement protection."
    )
