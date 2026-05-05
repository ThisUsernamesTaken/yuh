"""Unit tests for A5 residual reconciler (PolymarketCopyEngine._reconcile_residual_position).

Uses lightweight mocks to exercise the three cases:
  1. Clean (Kalshi shows zero)          → RESIDUAL-CLEAN, no sell
  2. Same-side residual                  → RESIDUAL + market sell on expected side
  3. Inverse / oversell-to-short         → OVERSELL-DETECTED + market sell on held side
"""
import asyncio
import logging
import pytest

# Import engine class; we'll monkeypatch on instances, not invoke __init__.
import polymarket_copy_engine as pce


class _FakeClient:
    def __init__(self, positions: list[dict], bid_yes: int = 0, bid_no: int = 0):
        self._positions = positions
        self._orders: list[tuple] = []
        self._bid_yes = bid_yes
        self._bid_no = bid_no
        self._fetch_sequence = [positions]  # returned in order by successive calls

    async def get_positions(self):
        if len(self._fetch_sequence) > 1:
            return self._fetch_sequence.pop(0)
        return self._fetch_sequence[0]

    async def place_order(self, *, ticker, side, price, count, action, **_):
        self._orders.append((ticker, side, price, count, action))
        return _FakeOrder("oid_" + ticker[:6], count)

    async def _request(self, method, path, params=None, **_):
        # 2026-05-01: sell-cap helper queries /portfolio/orders before
        # placing. Tests assume no resting sells exist on the residual
        # ticker, so return an empty orders list.
        if path == "/portfolio/orders":
            return {"orders": []}
        return {}

    async def cancel_order(self, order_id):
        return True

    async def get_order(self, order_id):
        return _FakeOrder(order_id, 0)


class _FakeOrder:
    def __init__(self, oid, fc):
        self.order_id = oid
        self.filled_count = fc
        self.status = "filled"


class _FakeBook:
    def __init__(self, yes_bid=0, no_bid=0):
        self.best_yes_bid = yes_bid
        self.best_no_bid = no_bid


class _FakeWS:
    def __init__(self, books: dict):
        self._books = books

    def get_book(self, ticker):
        return self._books.get(ticker)


def _make_engine(positions, books=None, open_position=None):
    """Build an engine shell with only the attributes the reconciler touches.
    Bypasses the full __init__ so we can unit-test in isolation.
    """
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _FakeClient(positions)
    eng._kalshi_ws = _FakeWS(books or {})
    # 2026-04-28: ownership-gate added — reconciler now requires either
    # _open_position on the ticker OR a kalshi_trades row to allow flatten.
    # Tests set _open_position to mark the ticker as engine-owned.
    eng._open_position = open_position
    eng._signal_logger = None  # disables the kalshi_trades fallback
    return eng


def test_residual_clean_zero_position():
    eng = _make_engine(positions=[])
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
    assert eng._client._orders == [], "Should not place any order when flat"


def test_residual_clean_explicit_zero():
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-X", "position": 0, "side": "yes"}],
    )
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
    assert eng._client._orders == [], "Should not place any order on zero position"


def test_residual_same_side_market_sells():
    """Kalshi shows 3ct YES still held after limit TPs supposedly closed."""
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-X", "position": 3, "side": "yes"}],
        books={"KXBTC15M-X": _FakeBook(yes_bid=42, no_bid=57)},
        open_position={"ticker": "KXBTC15M-X", "count": 3, "side": "yes"},
    )
    # 2026-05-04 MIN-TRUTH FIX: _place_capped_side_sell now calls
    # get_positions for its own zero-gate, in addition to the reconciler's
    # initial check and post-sell verify. Provide enough sticky entries:
    #   call 1: reconciler initial check       → position=3
    #   call 2: sell-cap MIN-TRUTH zero-gate   → position=3
    #   call 3 (sticky): post-sell verify      → []
    eng._client._fetch_sequence = [
        [{"ticker": "KXBTC15M-X", "position": 3, "side": "yes"}],
        [{"ticker": "KXBTC15M-X", "position": 3, "side": "yes"}],
        [],
    ]
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
    assert len(eng._client._orders) == 1, "Should place exactly one sell order"
    tkr, side, price, count, action = eng._client._orders[0]
    assert tkr == "KXBTC15M-X"
    assert side == "yes"
    assert action == "sell"
    assert count == 3
    assert price == 41, f"Should sell at bid-1 (42-1=41), got {price}"


def test_residual_oversell_inverse_detected():
    """Expected YES, but Kalshi shows NO position — the oversell signature.
    This is the 2026-04-22 incident pattern we want the reconciler to catch.

    2026-05-04: NO inventory uses Kalshi's signed convention (position=-N
    for N contracts of NO). The MIN-TRUTH sell-cap reads via
    _get_verified_side_position_count which expects this signing — needs
    -5 here, not 5.
    """
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-X", "position": -5, "side": "no"}],
        books={"KXBTC15M-X": _FakeBook(yes_bid=30, no_bid=65)},
        open_position={"ticker": "KXBTC15M-X", "count": 5, "side": "yes"},
    )
    # 2026-05-04 MIN-TRUTH FIX: extra entry for sell-cap zero-gate.
    eng._client._fetch_sequence = [
        [{"ticker": "KXBTC15M-X", "position": -5, "side": "no"}],
        [{"ticker": "KXBTC15M-X", "position": -5, "side": "no"}],
        [],
    ]
    # Capture logs to confirm OVERSELL-DETECTED prefix emitted.
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    pce.logger.addHandler(handler)
    try:
        asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
    finally:
        pce.logger.removeHandler(handler)
    assert any("OVERSELL-DETECTED" in m for m in records), \
        "Expected OVERSELL-DETECTED log line"
    assert len(eng._client._orders) == 1, "Should sell the inverse position"
    tkr, side, price, count, action = eng._client._orders[0]
    assert side == "no", "Should sell on the HELD side (no), not expected (yes)"
    assert count == 5
    assert price == 64, f"Should sell NO at no_bid-1 (65-1=64), got {price}"


def test_residual_safety_flag_off_is_noop(monkeypatch):
    """When SAFETY_OVERSELL_HARDENING=False, reconciler returns immediately.
    The engine caches user_config values in _user_cfg at import time, so we
    patch that dict directly rather than the source module.
    """
    monkeypatch.setitem(pce._user_cfg, "SAFETY_OVERSELL_HARDENING", False)
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-X", "position": 7, "side": "no"}],
        books={"KXBTC15M-X": _FakeBook(yes_bid=30, no_bid=65)},
    )
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
    assert eng._client._orders == [], "Flag off should bypass all logic"


def test_residual_no_ticker_is_noop():
    eng = _make_engine(positions=[{"ticker": "X", "position": 10, "side": "yes"}])
    asyncio.run(eng._reconcile_residual_position("", "yes", "unittest"))
    assert eng._client._orders == []


# ── Manual-trade safety gates (Claude 2026-04-28) ───────────────────────────

def test_manual_oversize_position_skipped():
    """A 1164ct position can't be engine-placed (cap=100). Leave alone."""
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-Y", "position": 1164, "side": "no"}],
        books={"KXBTC15M-Y": _FakeBook(yes_bid=10, no_bid=85)},
        open_position={"ticker": "KXBTC15M-Y", "count": 30, "side": "no"},
    )
    pce._user_cfg["SIZING_HARD_CAP_CONTRACTS_DAY"] = 100
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-Y", "no", "unittest"))
    assert eng._client._orders == [], (
        "1164ct must NOT be flattened — it's >150ct cap, almost certainly user manual"
    )


def test_manual_untouched_ticker_skipped():
    """Engine has no _open_position on this ticker — must not flatten."""
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-Z", "position": 25, "side": "yes"}],
        books={"KXBTC15M-Z": _FakeBook(yes_bid=70, no_bid=25)},
        open_position=None,  # engine never traded this ticker
    )
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-Z", "yes", "unittest"))
    assert eng._client._orders == [], (
        "Untouched ticker must NOT be flattened — provably user manual"
    )


def test_manual_overfill_skipped():
    """Engine fill was 6ct but Kalshi shows 29ct. Excess is user manual.
    Mirrors the trade-3/trade-4 incident on APR281030-30 / APR281045-45.
    """
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-W", "position": 29, "side": "yes"}],
        books={"KXBTC15M-W": _FakeBook(yes_bid=42, no_bid=57)},
        open_position={"ticker": "KXBTC15M-W", "count": 6, "side": "yes"},
    )
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-W", "yes", "unittest"))
    assert eng._client._orders == [], (
        "29ct on a ticker the engine filled 6ct on must NOT be flattened — "
        "5x is way past the 1.5x engine_max_owned cap"
    )


def test_engine_owned_residual_still_flattens():
    """Engine fill was 30ct, residual is 8ct — within the 1.5x cap. Flatten.

    2026-05-04: NO inventory uses Kalshi's signed convention (position=-8
    for 8ct of NO held).
    """
    eng = _make_engine(
        positions=[{"ticker": "KXBTC15M-V", "position": -8, "side": "no"}],
        books={"KXBTC15M-V": _FakeBook(yes_bid=22, no_bid=75)},
        open_position={"ticker": "KXBTC15M-V", "count": 30, "side": "no"},
    )
    # 2026-05-04 MIN-TRUTH FIX: extra entry for sell-cap zero-gate.
    eng._client._fetch_sequence = [
        [{"ticker": "KXBTC15M-V", "position": -8, "side": "no"}],
        [{"ticker": "KXBTC15M-V", "position": -8, "side": "no"}],
        [],
    ]
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-V", "no", "unittest"))
    assert len(eng._client._orders) == 1, (
        "8ct residual on a ticker the engine filled 30ct on IS engine-owned, "
        "should be flattened normally"
    )


def test_residual_fetch_failure_graceful():
    """If get_positions raises, we log and return — we don't crash the engine."""
    class _ErroringClient:
        async def get_positions(self):
            raise RuntimeError("kalshi down")
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = _ErroringClient()
    eng._kalshi_ws = None
    # Should NOT raise.
    asyncio.run(eng._reconcile_residual_position("KXBTC15M-X", "yes", "unittest"))
