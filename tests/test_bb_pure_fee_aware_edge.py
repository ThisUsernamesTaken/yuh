"""Tests for fee-aware dynamic edge threshold (2026-05-03).

Premise: Kalshi fee per contract ≈ 0.07 × P × (1−P) × 100¢ (bell-shape, peak at 50c).
Round-trip breakeven edge_pp = 2 × fee = 0.14 × p × (100−p)/100.
Effective floor = max(FEE_AWARE_FLOOR_PP, FEE_AWARE_MULT × breakeven).

Goals:
  - When the flag is OFF, behavior matches today's static 8pp floor exactly.
  - When ON, cheap entries (15c) require less edge than mid entries (50c).
  - Floor (default 4pp) prevents the threshold from collapsing to ~0 at price extremes.
"""
from __future__ import annotations

from bb_pure import evaluate

_BASE = {
    "min_edge_pp": 8.0,
    "max_entry_cents": 70,
    "min_entry_cents": 5,
    "min_time_remaining_s": 60.0,
    "kelly_fraction": 0.25,
    "kelly_max_frac": 0.15,
    "max_contracts": 200,
    # Fee-aware off by default in these tests; tests opt in per-case.
    "fee_aware_edge_enabled": False,
    "fee_aware_edge_mult": 2.0,
    "fee_aware_edge_floor_pp": 4.0,
}


def _eval(market, fair, **overrides):
    cfg = dict(_BASE)
    cfg.update(overrides)
    return evaluate(
        market_mid_cents=market,
        fair_yes_cents=fair,
        seconds_to_expiry=600.0,
        balance_dollars=200.0,
        config=cfg,
    )


# ─── Behavior preservation when flag is OFF ────────────────────────────


def test_static_path_8pp_edge_passes_when_disabled():
    """8pp edge clears the static 8pp floor; produces a signal."""
    sig = _eval(market=30, fair=38)
    assert sig is not None
    assert sig.side == "yes"


def test_static_path_7pp_edge_blocks_when_disabled():
    """7pp edge below the static 8pp floor returns None."""
    sig = _eval(market=30, fair=37)
    assert sig is None


# ─── Fee-aware loosens at cheap entries ────────────────────────────────


def test_fee_aware_passes_6pp_at_30c():
    """At entry=30c, breakeven=2.94pp; K=2 → 5.88pp threshold (clamped to floor=4).
    A 6pp edge clears the new threshold but would have failed the static 8pp gate."""
    sig = _eval(market=30, fair=36, fee_aware_edge_enabled=True)
    assert sig is not None
    assert sig.side == "yes"
    assert sig.suggested_entry_cents == 30
    assert abs(sig.edge_pp - 6.0) < 0.01


def test_fee_aware_blocks_5pp_at_30c():
    """5pp edge at 30c is below the 5.88pp K=2 threshold."""
    sig = _eval(market=30, fair=35, fee_aware_edge_enabled=True)
    assert sig is None


def test_fee_aware_passes_4pp_at_15c():
    """At entry=15c (NO side, market=85, fair=80), breakeven=1.79pp;
    K=2→3.57pp, but floor=4 wins. Edge=5pp clears 4pp floor."""
    sig = _eval(market=85, fair=80, fee_aware_edge_enabled=True)
    assert sig is not None
    assert sig.side == "no"
    assert sig.suggested_entry_cents == 15


def test_fee_aware_blocks_3pp_at_15c_due_to_floor():
    """At 15c the breakeven×K is below the 4pp floor; floor governs.
    A 3pp edge fails the 4pp floor."""
    sig = _eval(market=82, fair=79, fee_aware_edge_enabled=True)
    assert sig is None


# ─── Fee-aware tightens at mid prices ──────────────────────────────────


def test_fee_aware_blocks_6pp_at_50c():
    """At entry=50c, breakeven=3.5pp; K=2→7pp. A 6pp edge fails."""
    sig = _eval(market=50, fair=56, fee_aware_edge_enabled=True)
    assert sig is None


def test_fee_aware_passes_8pp_at_50c():
    """At entry=50c, 8pp clears the 7pp K=2 threshold."""
    sig = _eval(market=50, fair=58, fee_aware_edge_enabled=True)
    assert sig is not None
    assert sig.suggested_entry_cents == 50


# ─── Symmetry across YES/NO sides ─────────────────────────────────────


def test_fee_aware_symmetry_yes_vs_no_at_30c():
    """30c YES (market=30 fair=36) and 30c NO (market=70 fair=64) must
    behave identically — both have entry=30c, edge=6pp."""
    yes_sig = _eval(market=30, fair=36, fee_aware_edge_enabled=True)
    no_sig = _eval(market=70, fair=64, fee_aware_edge_enabled=True)
    assert yes_sig is not None
    assert no_sig is not None
    assert yes_sig.suggested_entry_cents == no_sig.suggested_entry_cents == 30
    assert abs(yes_sig.edge_pp - no_sig.edge_pp) < 0.01


# ─── Multiplier knob behavior ──────────────────────────────────────────


def test_fee_aware_mult_3x_blocks_6pp_at_30c():
    """K=3 raises threshold at 30c to 8.82pp; 6pp now blocks."""
    sig = _eval(market=30, fair=36,
                fee_aware_edge_enabled=True,
                fee_aware_edge_mult=3.0)
    assert sig is None


def test_fee_aware_mult_1x_passes_3pp_at_30c_above_floor():
    """K=1 → 2.94pp threshold, but floor=4 still wins. 3pp blocks."""
    sig = _eval(market=30, fair=33,
                fee_aware_edge_enabled=True,
                fee_aware_edge_mult=1.0)
    assert sig is None


def test_fee_aware_mult_1x_passes_5pp_at_30c():
    """K=1 with 5pp edge clears floor=4 and breakeven×1=2.94. Passes."""
    sig = _eval(market=30, fair=35,
                fee_aware_edge_enabled=True,
                fee_aware_edge_mult=1.0)
    assert sig is not None


# ─── Floor knob behavior ──────────────────────────────────────────────


def test_fee_aware_floor_zero_allows_breakeven_only():
    """floor=0, K=2 → at 15c, threshold = 3.57pp. 4pp passes."""
    sig = _eval(market=85, fair=81,
                fee_aware_edge_enabled=True,
                fee_aware_edge_floor_pp=0.0)
    assert sig is not None
    # 3pp below 3.57pp threshold: blocks
    sig2 = _eval(market=85, fair=82,
                 fee_aware_edge_enabled=True,
                 fee_aware_edge_floor_pp=0.0)
    assert sig2 is None


def test_fee_aware_floor_high_governs_everywhere():
    """floor=10 dominates breakeven everywhere. 8pp at 50c blocks."""
    sig = _eval(market=50, fair=58,
                fee_aware_edge_enabled=True,
                fee_aware_edge_floor_pp=10.0)
    assert sig is None


# ─── Defensive: degenerate inputs ──────────────────────────────────────


def test_zero_edge_returns_none_either_path():
    """Fair = market → no signal regardless of flag."""
    assert _eval(market=50, fair=50) is None
    assert _eval(market=50, fair=50, fee_aware_edge_enabled=True) is None


def test_entry_above_max_blocked_before_fee_check():
    """Entry-cents cap still governs even with permissive fee threshold."""
    sig = _eval(market=80, fair=90,
                fee_aware_edge_enabled=True,
                fee_aware_edge_floor_pp=0.0,
                max_entry_cents=70)
    # market=80, fair=90 → side=YES, entry=80 > 70 → blocked
    assert sig is None
