"""Integration tests for _place_capped_side_sell (Phase 0.1).

Live test on 2026-05-01 23:45-23:53 PT exposed two gaps:

1. POSITION-ZERO GAP: when the engine was already flat after exit,
   _reconcile_residual_position kept calling the helper in a loop. The
   inventory cap allowed each call (resting=0 each time, position=0,
   capped_count = max(0, 0-0) = 0 SHOULD have blocked but wasn't gating
   on position alone). 11 sell-no orders piled up before manual stop.

2. >1 RESTING GAP: the helper had cap math but no abort-on-many-resting.
   _maintain_protective_order got that guard in commit 168dfd2; the
   helper needed the same protection.

Both fixed in 2026-05-02 Phase 0.1. These tests pin down the new guards.
"""
from __future__ import annotations

import asyncio
from typing import Any

from polymarket_copy_engine import PolymarketCopyEngine


class _FakeClient:
    """Minimal async-capable mock for Kalshi client."""

    def __init__(
        self,
        positions: list[dict] | None = None,
        resting: list[dict] | None = None,
    ):
        self._positions = positions or []
        self._resting = resting or []
        self.placed_orders: list[dict[str, Any]] = []
        self.cancelled_ids: list[str] = []

    async def get_positions(self):
        return self._positions

    async def _request(self, method: str, path: str, params=None, **_):
        if path == "/portfolio/orders":
            return {"orders": self._resting}
        return {}

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled_ids.append(order_id)
        return True

    async def get_order(self, order_id: str):
        return _FakeOrder(order_id, status="canceled")

    async def place_order(self, *, ticker, side, price, count, action, **_):
        oid = f"oid_{len(self.placed_orders)}"
        self.placed_orders.append(
            {"ticker": ticker, "side": side, "price": price, "count": count, "action": action}
        )
        return _FakeOrder(oid, count=count, filled_count=count)


class _FakeOrder:
    def __init__(self, oid: str, count: int = 0, filled_count: int = 0, status: str = "filled"):
        self.order_id = oid
        self.count = count
        self.filled_count = filled_count
        self.status = status


def _make_engine(client: _FakeClient) -> PolymarketCopyEngine:
    """Build a minimal engine instance bypassing __init__ side effects."""
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    eng._client = client
    return eng


# ── Test 1: position-zero gate ───────────────────────────────────────────

def test_position_zero_gate_blocks_sell_when_flat():
    """When Kalshi truth says position=0, the helper must NOT place any sell.

    Reproduces the residual-pileup bug from 2026-05-01 23:52 PT: helper was
    being called in a loop after exit, position was 0, but each call still
    placed an order because the cap math `max(0, 0 - 0) = 0` was the only
    block — and bugs in caller logic could pass non-zero requested_count.
    """
    client = _FakeClient(positions=[], resting=[])
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="no",
            price=10,
            requested_count=22,
            post_only=False,
            reason="UNITTEST",
        )
    )
    assert order is None
    assert count == 0
    assert client.placed_orders == [], "no sells must be placed when position=0"


def test_position_zero_gate_blocks_with_known_count_zero():
    """When known_position_count=0 is passed explicitly, helper must block.

    Caller-supplied count is trusted; zero means flat.
    """
    client = _FakeClient(positions=[], resting=[])
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=50,
            requested_count=10,
            post_only=True,
            reason="UNITTEST",
            known_position_count=0,
        )
    )
    assert order is None
    assert count == 0
    assert client.placed_orders == []


# ── Test 2: >1 resting OVERSELL-GUARD ────────────────────────────────────

def test_oversell_guard_cancels_all_when_more_than_one_resting():
    """If 2+ resting sells already exist on this ticker/side, helper must
    cancel them all and abort. Mirrors _maintain_protective_order's guard."""
    resting = [
        {"order_id": "old_1", "side": "no", "action": "sell", "remaining_count": 22},
        {"order_id": "old_2", "side": "no", "action": "sell", "remaining_count": 22},
        {"order_id": "old_3", "side": "no", "action": "sell", "remaining_count": 22},
    ]
    positions = [{"ticker": "KXBTC15M-T", "position": -22, "side": "no"}]
    client = _FakeClient(positions=positions, resting=resting)
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="no",
            price=10,
            requested_count=22,
            post_only=False,
            reason="UNITTEST",
            known_position_count=22,
        )
    )
    assert order is None, "must abort cycle when >1 resting"
    assert count == 0
    # All 3 stale orders cancelled
    assert set(client.cancelled_ids) == {"old_1", "old_2", "old_3"}
    # No new placement
    assert client.placed_orders == []


def test_one_resting_is_allowed_through_to_cap_math():
    """Exactly 1 resting sell is the normal protective case — let it through
    so the inventory cap can decide whether to add more."""
    resting = [
        {"order_id": "existing", "side": "yes", "action": "sell", "remaining_count": 10},
    ]
    positions = [{"ticker": "KXBTC15M-T", "position": 22, "side": "yes"}]
    client = _FakeClient(positions=positions, resting=resting)
    eng = _make_engine(client)
    # Position 22, resting 10, requested 12 → cap allows 12
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=70,
            requested_count=12,
            post_only=True,
            reason="UNITTEST",
            known_position_count=22,
        )
    )
    assert order is not None
    assert count == 12
    assert len(client.placed_orders) == 1
    assert client.cancelled_ids == [], "1 resting is fine, no cancel needed"


# ── Test 3: cap math still works correctly ───────────────────────────────

def test_cap_math_reduces_requested_to_uncovered_inventory():
    """Position=30, resting=20, requested=20 → cap to 10 uncovered."""
    resting = [
        {"order_id": "p1", "side": "yes", "action": "sell", "remaining_count": 20},
    ]
    positions = [{"ticker": "KXBTC15M-T", "position": 30, "side": "yes"}]
    client = _FakeClient(positions=positions, resting=resting)
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=60,
            requested_count=20,
            post_only=True,
            reason="UNITTEST",
            known_position_count=30,
        )
    )
    assert order is not None
    assert count == 10
    assert client.placed_orders[0]["count"] == 10


def test_cap_blocks_when_resting_meets_or_exceeds_position():
    """All inventory already covered by resting orders → no new sell needed."""
    resting = [
        {"order_id": "p1", "side": "yes", "action": "sell", "remaining_count": 30},
    ]
    positions = [{"ticker": "KXBTC15M-T", "position": 30, "side": "yes"}]
    client = _FakeClient(positions=positions, resting=resting)
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=60,
            requested_count=10,
            post_only=True,
            reason="UNITTEST",
            known_position_count=30,
        )
    )
    assert order is None
    assert count == 0


# ── Test 4: repeated calls when flat (the actual bug scenario) ───────────

def test_repeated_calls_when_flat_never_place():
    """Reproduce 2026-05-01 23:52 scenario: helper called 11 times in 31s
    while position is flat. Every single call must return (None, 0)."""
    client = _FakeClient(positions=[], resting=[])
    eng = _make_engine(client)
    for _ in range(11):
        order, count = asyncio.run(
            eng._place_capped_side_sell(
                ticker="KXBTC15M-T",
                side="no",
                price=10,
                requested_count=22,
                post_only=False,
                reason="REPRODUCING-BUG",
            )
        )
        assert order is None
        assert count == 0
    assert client.placed_orders == [], "Phase 0.1 fix: zero placements when flat"


# ── Test 5: 2026-05-04 catastrophe regression ────────────────────────────


def test_min_truth_blocks_when_hint_positive_but_kalshi_flat():
    """Reproduce the 2026-05-04 catastrophe shape: caller passes a positive
    ``known_position_count`` hint (engine belief) while Kalshi truth is 0.

    Old code at line 15170-15174 trusted the hint and never queried Kalshi
    when the hint was provided — the position-zero gate became a no-op.
    Each MRC FORCE-EXIT call then placed a sell-yes that Kalshi auto-
    converted to "buy NO @ (100-price)c" via sell-to-open semantics,
    opening 143 phantom NO contracts and draining $68 of the account.

    New code (MIN-TRUTH FIX, 2026-05-04) ALWAYS queries Kalshi truth for
    the zero-gate; the hint can only further cap the truth, never override
    it. With Kalshi flat, the helper must return (None, 0) regardless of
    the hint."""
    # Kalshi positions empty → truth=0
    client = _FakeClient(positions=[], resting=[])
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-26MAY041730-30",
            side="yes",
            price=56,
            requested_count=13,
            post_only=False,
            reason="MRC FORCE-EXIT",
            known_position_count=13,  # the inflated engine-belief hint
        )
    )
    assert order is None, (
        "MIN-TRUTH FIX: must refuse the sell when Kalshi truth=0, "
        "regardless of the caller's hint. Without this, the dispatch "
        "session's MRC loop opened phantom NO contracts."
    )
    assert count == 0
    assert client.placed_orders == [], (
        "Catastrophe regression: zero orders must hit the wire when truth=0"
    )


def test_min_truth_caps_hint_to_kalshi_when_hint_exceeds_truth():
    """When the caller's hint is higher than Kalshi truth, cap to truth.

    Mirror of the catastrophe shape but with truth > 0: caller hint=20,
    Kalshi truth=5. The sell must be capped to 5, not 20.
    """
    positions = [{"ticker": "KXBTC15M-T", "position": 5, "side": "yes"}]
    client = _FakeClient(positions=positions, resting=[])
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=70,
            requested_count=20,
            post_only=True,
            reason="UNITTEST",
            known_position_count=20,  # caller wrongly believes 20
        )
    )
    assert order is not None
    assert count == 5, "must cap to Kalshi truth (5), not honor inflated hint (20)"
    assert client.placed_orders[0]["count"] == 5


def test_min_truth_uses_hint_when_hint_lower_than_truth():
    """When the caller's hint is LOWER than Kalshi truth, prefer the hint
    (residual-flatten use case: caller saw a smaller residual count an
    instant ago and wants to act on that)."""
    positions = [{"ticker": "KXBTC15M-T", "position": 30, "side": "yes"}]
    client = _FakeClient(positions=positions, resting=[])
    eng = _make_engine(client)
    order, count = asyncio.run(
        eng._place_capped_side_sell(
            ticker="KXBTC15M-T",
            side="yes",
            price=70,
            requested_count=10,
            post_only=True,
            reason="UNITTEST",
            known_position_count=10,  # caller saw exactly 10
        )
    )
    assert order is not None
    assert count == 10
    assert client.placed_orders[0]["count"] == 10
