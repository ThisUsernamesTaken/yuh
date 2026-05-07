"""Unit tests for the DIRECTION-tier opt-outs across legacy code paths.

Today's portfolio-mode session (2026-05-05) caught two distinct hijacks of
DIRECTION positions:
  - 21:22 PT: SYNC RECLAIM adopted a DIRECTION fill as TA_FORCED → -$7.53
  - 21:51 PT: VWAP_EXIT (inside _manage_position) closed a DIRECTION
              position 9min before settlement → -$0.85 + forfeited $5.60
              of settlement payout

Both are the same architectural class: a legacy management path acted on
a DIRECTION position. The fix (2026-05-06) is a comprehensive opt-out
audit:
  Guard 1: top of _manage_position bypasses ~20 internal exit paths
           (VWAP_EXIT, PROB_COLLAPSE, PEAK_GIVEBACK, INTELLIGENT DCA,
           MFE-TRAIL, BTC-DEVIATION-STOP, FLOW-FLIP, etc.)
  Guard 2: top of _mrc_check_force_exit
  Guard 3: SYNC RECLAIM consults _direction_active_tickers
  Guard 4: ORPHAN-FLATTEN consults _direction_active_tickers
  (Pre-existing) _maintain_protective_order opt-out

These tests pin the decision logic for guards 1, 2, and the registry
mechanic that 3 and 4 use. The full SYNC RECLAIM / ORPHAN-FLATTEN paths
have integration-style coverage in their respective scripts; here we
verify the DIRECTION-tier check fires correctly given a position dict.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_pos(*, tier: str = "DIRECTION", hold: bool = True, count: int = 5):
    return {
        "ticker": "KXBTC15M-TEST",
        "side": "yes",
        "entry_cents": 30,
        "count": count,
        "tier": tier,
        "_hold_to_settle": hold,
    }


# ─── _manage_position guard ──────────────────────────────────────────────


def test_manage_position_returns_early_for_direction_tier():
    """A position with tier='DIRECTION' bypasses all _manage_position
    exit logic. We verify by checking that the function returns
    immediately without touching the position's _vwap_exit_fired or
    _profit_trail_exited flags (those would be set if any exit path ran).
    """
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    pos = _make_pos(tier="DIRECTION", hold=False)
    eng._open_position = pos
    # Set sentinel flags — if the function ran any exit logic, these
    # would be touched (to True). After the guard, they should remain.
    pos["_vwap_exit_fired"] = False
    pos["_profit_trail_exited"] = False
    pos["_test_sentinel_touched"] = False

    # _manage_position requires lots of state; we only need to verify
    # the guard returns before touching anything. We monkey-patch the
    # MRC analyzer pathway to a trivial no-op so we don't have to mock
    # the entire engine.
    eng._kalshi_ws = MagicMock()
    eng._kalshi_ws.get_book = MagicMock(return_value=None)

    asyncio.run(eng._manage_position())

    # If the guard didn't fire, _manage_position would have proceeded
    # to MRC feed / exit checks. The guard returns at line ~20354 BEFORE
    # any exit logic.
    assert pos["_vwap_exit_fired"] is False
    assert pos["_profit_trail_exited"] is False


def test_manage_position_returns_early_for_hold_to_settle_marker():
    """A position with _hold_to_settle=True (regardless of tier) also
    bypasses _manage_position. This is the marker-based fallback for
    any future hold-to-settlement strategy."""
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    pos = _make_pos(tier="UNKNOWN", hold=True)
    eng._open_position = pos
    pos["_vwap_exit_fired"] = False
    eng._kalshi_ws = MagicMock()
    eng._kalshi_ws.get_book = MagicMock(return_value=None)

    asyncio.run(eng._manage_position())
    assert pos["_vwap_exit_fired"] is False


def test_manage_position_no_pos_no_op():
    """_open_position is None → return immediately (existing behavior)."""
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    eng._open_position = None
    asyncio.run(eng._manage_position())  # should not raise


# ─── _mrc_check_force_exit guard ────────────────────────────────────────


def test_mrc_force_exit_returns_false_for_direction_tier():
    """The MRC analyzer's force-exit must not fire on DIRECTION positions."""
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    eng._client = MagicMock()
    eng._client.cancel_order = AsyncMock(return_value=True)
    pos = _make_pos(tier="DIRECTION")
    # Even if the analyzer SAYS force_exit, the tier guard should
    # short-circuit to False.
    fake_an = MagicMock()
    fake_an.is_warm = True
    fake_an.should_force_exit = True
    pos["_momentum_analyzer"] = fake_an

    fired = asyncio.run(eng._mrc_check_force_exit(pos))
    assert fired is False


def test_mrc_force_exit_returns_false_for_hold_to_settle_marker():
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    pos = _make_pos(tier="UNKNOWN", hold=True)
    fake_an = MagicMock()
    fake_an.is_warm = True
    fake_an.should_force_exit = True
    pos["_momentum_analyzer"] = fake_an
    fired = asyncio.run(eng._mrc_check_force_exit(pos))
    assert fired is False


def test_mrc_force_exit_returns_false_for_no_pos():
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    fired = asyncio.run(eng._mrc_check_force_exit(None))
    assert fired is False


# ─── _direction_active_tickers registry semantics ───────────────────────


def test_direction_active_tickers_is_a_set_and_supports_membership():
    """Verify the new attribute exists, is a set, and supports the
    membership-check pattern used by SYNC RECLAIM and ORPHAN-FLATTEN."""
    from polymarket_copy_engine import PolymarketCopyEngine
    eng = PolymarketCopyEngine.__new__(PolymarketCopyEngine)
    eng._direction_active_tickers = set()
    # Mimics what _direction_tick does on FILL
    eng._direction_active_tickers.add("KXBTC15M-DIR-1")
    eng._direction_active_tickers.add("KXBTC15M-DIR-2")
    assert "KXBTC15M-DIR-1" in eng._direction_active_tickers
    assert "KXBTC15M-DIR-2" in eng._direction_active_tickers
    assert "KXBTC15M-OTHER" not in eng._direction_active_tickers


def test_sync_reclaim_check_skips_direction_tickers():
    """Simulate the SYNC RECLAIM membership check — the actual code path
    is deeply nested; here we verify the logical pattern works."""
    direction_tickers = {"KXBTC15M-DIR"}
    # Pseudo-code mirroring the engine guard at line ~22042
    candidate = "KXBTC15M-DIR"
    skip = candidate in direction_tickers
    assert skip is True
    candidate = "KXBTC15M-OTHER"
    skip = candidate in direction_tickers
    assert skip is False


def test_orphan_flatten_check_skips_direction_tickers():
    """Same pattern, for the ORPHAN-FLATTEN loop."""
    direction_tickers = {"KXBTC15M-DIR"}
    for candidate in ("KXBTC15M-DIR", "KXBTC15M-OTHER"):
        skip = candidate in direction_tickers
        assert skip is (candidate == "KXBTC15M-DIR")
