"""Test the pre-fire balance gate (2026-05-03).

Live postmortem 2026-05-02: BAL drained, then a wayward phantom 26-NO
position appeared. ORPHAN-FLATTEN tried to flatten via cross-spread sell
but Kalshi returned `insufficient_balance` 50+ times. Position rode to
expiry, lost the full entry cost.

Pre-fire gate: require live BAL ≥ entry_cost × multiplier before placing.
"""
from __future__ import annotations


def _bal_block(cost_dollars: float, bal_dollars: float, multiplier: float) -> bool:
    """Return True if entry should be BLOCKED (mirrors engine logic)."""
    if cost_dollars <= 0:
        return False
    if bal_dollars <= 0:
        # No BAL data — don't false-block (defensive)
        return False
    required = cost_dollars * multiplier
    return bal_dollars < required


def test_passes_when_bal_covers_required_headroom():
    # Cost $5, mult 2 → require $10. BAL $20 — passes.
    assert not _bal_block(cost_dollars=5.0, bal_dollars=20.0, multiplier=2.0)


def test_blocks_when_bal_below_required():
    # Cost $5, mult 2 → require $10. BAL $8 — blocks.
    assert _bal_block(cost_dollars=5.0, bal_dollars=8.0, multiplier=2.0)


def test_blocks_at_exact_threshold_boundary():
    # Cost $5, mult 2 → require $10. BAL $10 → not less than → passes.
    # BAL $9.99 → blocks.
    assert not _bal_block(cost_dollars=5.0, bal_dollars=10.0, multiplier=2.0)
    assert _bal_block(cost_dollars=5.0, bal_dollars=9.99, multiplier=2.0)


def test_zero_cost_does_not_block():
    """Defensive: cost=0 shouldn't block anything."""
    assert not _bal_block(cost_dollars=0.0, bal_dollars=5.0, multiplier=2.0)


def test_zero_bal_does_not_false_block():
    """Defensive: missing BAL data shouldn't false-block (engine state
    might be initializing)."""
    assert not _bal_block(cost_dollars=5.0, bal_dollars=0.0, multiplier=2.0)


def test_high_multiplier_blocks_smaller_bal():
    # Cost $5, mult 5 → require $25. BAL $20 — blocks (was passing at mult=2).
    assert _bal_block(cost_dollars=5.0, bal_dollars=20.0, multiplier=5.0)


def test_low_bal_scenario_today():
    """The exact scenario at session resumption: BAL $73, signal wants
    a $4 entry. With mult=2, requires $8. $73 >> $8 → passes (good)."""
    assert not _bal_block(cost_dollars=4.0, bal_dollars=73.12, multiplier=2.0)


def test_low_bal_scenario_after_one_loss():
    """If BAL drops to $7, a $4 entry needs $8 → blocks. Prevents
    insufficient_balance cascade post-loss."""
    assert _bal_block(cost_dollars=4.0, bal_dollars=7.0, multiplier=2.0)
