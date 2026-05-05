"""Unit tests for ``_fvg_tiering`` — tier classification + Level 3 sizing.

The tier definitions came out of the OOS-validated 70/30 split shown in
``scripts/backtest_oos_level3.py`` (2026-05-04). These tests pin down
every boundary so the production code can't silently drift away from
the validated config.
"""
from __future__ import annotations

import pytest

from _fvg_tiering import (
    TIER_FRACTIONS,
    TIER_TP_OFFSETS_C,
    TIER_SL_OFFSET_C,
    FvgFeatures,
    classify_tier,
    compute_size_contracts,
    compute_tiered_sizing,
    tier_sl_price,
    tier_tp_price,
)


# ─── Tier classification ────────────────────────────────────────────────


def _feat(**kw) -> dict:
    """Build a default-aligned, default-late, default-far feature dict."""
    return {
        "session_age_s": kw.get("session_age_s", 400),
        "aligned": kw.get("aligned", True),
        "btc_dist_abs_pct": kw.get("btc_dist_abs_pct", 0.05),
        "btc_5m_move": kw.get("btc_5m_move", 30.0),
        "entry_c": kw.get("entry_c", 35),
    }


def test_tier_1_late_aligned_far():
    """The 99% fill segment: late_300+ & aligned & far_10+."""
    assert classify_tier(_feat(
        session_age_s=400, aligned=True, btc_dist_abs_pct=0.12,
    )) == 1
    # Boundary: exactly 300s + exactly 0.10% — both inclusive.
    assert classify_tier(_feat(
        session_age_s=300, aligned=True, btc_dist_abs_pct=0.10,
    )) == 1


def test_tier_2_late_aligned_not_far():
    """The 93% fill segment: late_300+ & aligned, dist < 0.10%."""
    assert classify_tier(_feat(
        session_age_s=400, aligned=True, btc_dist_abs_pct=0.05,
    )) == 2
    # Just below the 0.10% threshold:
    assert classify_tier(_feat(
        session_age_s=400, aligned=True, btc_dist_abs_pct=0.0999,
    )) == 2


def test_tier_3_late_not_aligned():
    """The 91% fill segment: late_300+ but not aligned."""
    # Counter-trend at high entry to sidestep the refuse predicate.
    assert classify_tier(_feat(
        session_age_s=400, aligned=False,
        btc_dist_abs_pct=0.05, entry_c=60, btc_5m_move=30.0,
    )) == 3


def test_tier_4_mid_window():
    """The 76% fill segment: 180 <= age < 300."""
    assert classify_tier(_feat(
        session_age_s=200, aligned=True, btc_dist_abs_pct=0.05,
    )) == 4
    # Just-on boundary:
    assert classify_tier(_feat(
        session_age_s=180, aligned=True, btc_dist_abs_pct=0.05,
    )) == 4
    # Just-below boundary triggers refuse:
    assert classify_tier(_feat(
        session_age_s=179, aligned=True, btc_dist_abs_pct=0.05,
    )) == 0


# ─── Refuse predicates ──────────────────────────────────────────────────


def test_refuse_early_window():
    """session_age < 180s → refuse, regardless of other features."""
    assert classify_tier(_feat(session_age_s=0)) == 0
    assert classify_tier(_feat(session_age_s=179)) == 0
    # 90s is well into the typical early-fire zone the strategy is most
    # vulnerable to in the dataset (66% of raw fires).
    assert classify_tier(_feat(session_age_s=90, aligned=True,
                                btc_dist_abs_pct=0.15)) == 0


def test_refuse_counter_midprice():
    """counter (not aligned) AND 30 <= entry_c <= 49 → refuse.

    This is the worst observed segment (-$0.045/trade in the
    segmentation analysis).
    """
    for entry in (30, 35, 40, 45, 49):
        assert classify_tier(_feat(
            session_age_s=400, aligned=False, entry_c=entry,
            btc_5m_move=30.0,  # so the flat-BTC predicate doesn't catch first
        )) == 0
    # Just outside the bucket:
    assert classify_tier(_feat(
        session_age_s=400, aligned=False, entry_c=29,
        btc_5m_move=30.0,
    )) != 0
    assert classify_tier(_feat(
        session_age_s=400, aligned=False, entry_c=50,
        btc_5m_move=30.0,
    )) != 0


def test_refuse_flat_counter():
    """|btc_5m_move| < 20 AND not aligned → refuse."""
    assert classify_tier(_feat(
        session_age_s=400, aligned=False, btc_5m_move=10.0, entry_c=60,
    )) == 0
    assert classify_tier(_feat(
        session_age_s=400, aligned=False, btc_5m_move=-15.0, entry_c=60,
    )) == 0
    # Aligned overrides the flat-BTC refuse:
    assert classify_tier(_feat(
        session_age_s=400, aligned=True, btc_5m_move=10.0,
    )) != 0
    # Strong move overrides:
    assert classify_tier(_feat(
        session_age_s=400, aligned=False, btc_5m_move=25.0, entry_c=60,
    )) != 0


# ─── Size computation ───────────────────────────────────────────────────


def test_size_contracts_tier_1_typical():
    """Tier 1 at $30 BR with 60c entry → 35% × $30 / $0.60 = 17.5 → 17ct."""
    contracts = compute_size_contracts(tier=1, balance_cents=3000, entry_c=60)
    # 3000 * 0.35 = 1050 cents notional
    # 1050 / 60 = 17.5, floored to 17
    assert contracts == 17


def test_size_contracts_tier_2():
    """Tier 2 at $50 BR with 50c entry → 25% × $50 / $0.50 = 25ct."""
    contracts = compute_size_contracts(tier=2, balance_cents=5000, entry_c=50)
    # 5000 * 0.25 = 1250
    # 1250 / 50 = 25
    assert contracts == 25


def test_size_contracts_tier_0_refused():
    """tier=0 always returns 0."""
    assert compute_size_contracts(tier=0, balance_cents=10000, entry_c=50) == 0


def test_size_contracts_zero_balance():
    """No balance → zero contracts."""
    assert compute_size_contracts(tier=1, balance_cents=0, entry_c=50) == 0
    assert compute_size_contracts(tier=1, balance_cents=-100, entry_c=50) == 0


def test_size_contracts_zero_entry_price():
    """Defensive: invalid entry price returns 0."""
    assert compute_size_contracts(tier=1, balance_cents=5000, entry_c=0) == 0


def test_size_contracts_min_floor_when_affordable():
    """When fractional sizing rounds to <1 contract but the min floor IS
    affordable, return exactly 1 contract.

    Tier 4 at $5 BR, 50c entry → 10% × $5 / $0.50 = 1.0 → exactly 1.
    Tier 4 at $4 BR, 50c entry → 10% × $4 / $0.50 = 0.8 → floor to 1.
      Floor cost = 50c. Max exposure cap = $4 × 40% = $1.60 = 160c.
      50c ≤ 160c → allowed; return 1.
    """
    assert compute_size_contracts(tier=4, balance_cents=400, entry_c=50) == 1
    assert compute_size_contracts(tier=4, balance_cents=500, entry_c=50) == 1


def test_size_contracts_min_floor_refused_when_too_expensive():
    """If the min-1-contract cost > exposure cap, refuse the trade.

    Tier 4 at $1 BR, 50c entry → 10% × $1 = 10c (can't afford 50c).
      Floor cost = 50c. Cap = $1 × 40% = 40c. 50c > 40c → refuse.
    """
    assert compute_size_contracts(tier=4, balance_cents=100, entry_c=50) == 0


def test_size_contracts_max_cap():
    """Hard cap at max_contracts_cap defends against absurd bankrolls."""
    # $5,000,000 BR with 50c entry: would be 35,000ct without cap
    huge_bal = 500_000_000
    contracts = compute_size_contracts(
        tier=1, balance_cents=huge_bal, entry_c=50,
        max_contracts_cap=200,
    )
    assert contracts == 200


def test_size_contracts_max_exposure_frac_caps_below_tier_frac():
    """If max_exposure_frac < tier_frac, the cap wins.

    Tier 1 frac = 35%. With max_exposure_frac=20%, the cap should
    constrain the notional to 20% bankroll, not the full 35%.
    """
    contracts = compute_size_contracts(
        tier=1, balance_cents=10000, entry_c=50,
        max_exposure_frac=0.20,
    )
    # 10000 * 0.20 = 2000c notional, 2000/50 = 40ct
    assert contracts == 40


# ─── TP/SL helpers ──────────────────────────────────────────────────────


def test_tp_price_per_tier():
    """Each tier has its validated TP offset."""
    assert tier_tp_price(1, 50) == 50 + TIER_TP_OFFSETS_C[1]   # 70
    assert tier_tp_price(2, 50) == 50 + TIER_TP_OFFSETS_C[2]   # 65
    assert tier_tp_price(3, 50) == 50 + TIER_TP_OFFSETS_C[3]   # 62
    assert tier_tp_price(4, 50) == 50 + TIER_TP_OFFSETS_C[4]   # 62
    # Tier 0 fallback +8c
    assert tier_tp_price(0, 50) == 58


def test_sl_price_uniform_offset():
    """SL is a uniform 8c offset across all tiers (per Phase 2 protective)."""
    assert tier_sl_price(1, 50) == 50 - TIER_SL_OFFSET_C   # 42
    assert tier_sl_price(4, 30) == 30 - TIER_SL_OFFSET_C   # 22
    # Floor at 1c (defends against very-cheap entries)
    assert tier_sl_price(1, 5) == max(1, 5 - 8)            # 1


# ─── Combined sizing decision ───────────────────────────────────────────


def test_compute_tiered_sizing_full_path_tier_1():
    """End-to-end: classify + size + price levels in one call."""
    feat = _feat(session_age_s=400, aligned=True, btc_dist_abs_pct=0.12,
                  entry_c=60)
    sizing = compute_tiered_sizing(feat, balance_cents=3000, entry_c=60)
    assert sizing.fires
    assert sizing.tier == 1
    assert sizing.contracts == 17       # 35% × 30 / 0.60
    assert sizing.tp_price_c == 60 + 20  # +20c offset for T1
    assert sizing.sl_price_c == 60 - 8
    assert sizing.notional_cents == 17 * 60  # 1020c


def test_compute_tiered_sizing_refused():
    """Refused trades return TieredSizing with tier=0 and a reason."""
    feat = _feat(session_age_s=60)  # too early
    sizing = compute_tiered_sizing(feat, balance_cents=5000, entry_c=50)
    assert not sizing.fires
    assert sizing.tier == 0
    assert sizing.contracts == 0
    assert "refuse" in sizing.rejection_reason.lower()


def test_compute_tiered_sizing_undersized_refused():
    """Trade meets tier criteria but bankroll can't even afford 1ct."""
    feat = _feat(session_age_s=400, aligned=True, btc_dist_abs_pct=0.12)
    # $0.50 bankroll, 60c entry — can't afford even 1 contract within cap
    sizing = compute_tiered_sizing(feat, balance_cents=50, entry_c=60)
    assert sizing.tier == 1                # signal qualifies
    assert sizing.contracts == 0           # but we can't size
    assert "size=0" in sizing.rejection_reason


# ─── Tier-fraction ordering invariant ───────────────────────────────────


def test_tier_fraction_ordering_invariant():
    """T1 size >= T2 >= T3 >= T4 (more confidence → larger size)."""
    fracs = [TIER_FRACTIONS[i] for i in (1, 2, 3, 4)]
    assert fracs == sorted(fracs, reverse=True), (
        "Tier sizing must be monotonically non-increasing — "
        "higher-confidence tiers must size at least as large."
    )


def test_tier_tp_offset_ordering_invariant():
    """T1 TP offset is the largest (most reliable fill at higher TP)."""
    assert TIER_TP_OFFSETS_C[1] >= TIER_TP_OFFSETS_C[2]
    assert TIER_TP_OFFSETS_C[2] >= TIER_TP_OFFSETS_C[3]
    assert TIER_TP_OFFSETS_C[3] >= TIER_TP_OFFSETS_C[4]
