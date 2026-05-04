"""Tests for the BB_PURE alignment classifier + alignment-fallback tier.

Pure-module tests; no engine state, no async. Shipped 2026-05-03 as the
unit-level coverage for Option C (alignment gate + alignment-fallback
synthetic signal).

Reads the user's intent from the live notes:
    "if we can't see a reason to enter contrarian we should enter in
     alignment"

Two helpers under test:
    classify_alignment(side, btc_5m_change_usd, dead_zone_usd) -> str
    aligned_side_from_btc(btc_5m_change_usd, dead_zone_usd) -> Optional[str]
    build_alignment_fallback_signal(side, market_mid_cents, ...) -> BBSignal | None
"""
from __future__ import annotations

import pytest

from bb_pure import (
    BBSignal,
    classify_alignment,
    aligned_side_from_btc,
    build_alignment_fallback_signal,
    classify_position_alignment,
)


# ─── classify_alignment ─────────────────────────────────────────────────


def test_classify_alignment_yes_with_btc_up_is_aligned():
    assert classify_alignment("yes", btc_5m_change_usd=50.0) == "aligned"


def test_classify_alignment_no_with_btc_down_is_aligned():
    assert classify_alignment("no", btc_5m_change_usd=-50.0) == "aligned"


def test_classify_alignment_yes_with_btc_down_is_contrarian():
    assert classify_alignment("yes", btc_5m_change_usd=-50.0) == "contrarian"


def test_classify_alignment_no_with_btc_up_is_contrarian():
    assert classify_alignment("no", btc_5m_change_usd=50.0) == "contrarian"


def test_classify_alignment_dead_zone_returns_neutral():
    # ±$15 within default $20 dead zone → neutral regardless of side
    assert classify_alignment("yes", btc_5m_change_usd=15.0) == "neutral"
    assert classify_alignment("no", btc_5m_change_usd=-15.0) == "neutral"
    assert classify_alignment("yes", btc_5m_change_usd=0.0) == "neutral"


def test_classify_alignment_at_dead_zone_boundary_is_neutral():
    # |change| == dead_zone is treated as inside (use > strict comparison)
    assert classify_alignment("yes", btc_5m_change_usd=20.0) == "neutral"
    assert classify_alignment("no", btc_5m_change_usd=-20.0) == "neutral"


def test_classify_alignment_just_past_dead_zone_classifies():
    assert classify_alignment(
        "yes", btc_5m_change_usd=20.01) == "aligned"
    assert classify_alignment(
        "yes", btc_5m_change_usd=-20.01) == "contrarian"


def test_classify_alignment_custom_dead_zone():
    # Tighter dead zone → more classifications
    assert classify_alignment(
        "yes", btc_5m_change_usd=10.0, dead_zone_usd=5.0) == "aligned"
    # Looser dead zone → more neutrals
    assert classify_alignment(
        "yes", btc_5m_change_usd=30.0, dead_zone_usd=50.0) == "neutral"


def test_classify_alignment_handles_uppercase_side():
    assert classify_alignment("YES", btc_5m_change_usd=50.0) == "aligned"
    assert classify_alignment("No", btc_5m_change_usd=-50.0) == "aligned"


# ─── aligned_side_from_btc ──────────────────────────────────────────────


def test_aligned_side_btc_up_returns_yes():
    assert aligned_side_from_btc(btc_5m_change_usd=50.0) == "yes"


def test_aligned_side_btc_down_returns_no():
    assert aligned_side_from_btc(btc_5m_change_usd=-50.0) == "no"


def test_aligned_side_in_dead_zone_returns_none():
    assert aligned_side_from_btc(btc_5m_change_usd=10.0) is None
    assert aligned_side_from_btc(btc_5m_change_usd=-10.0) is None
    assert aligned_side_from_btc(btc_5m_change_usd=0.0) is None


def test_aligned_side_custom_dead_zone():
    assert aligned_side_from_btc(
        btc_5m_change_usd=10.0, dead_zone_usd=5.0) == "yes"
    assert aligned_side_from_btc(
        btc_5m_change_usd=-30.0, dead_zone_usd=50.0) is None


# ─── build_alignment_fallback_signal ────────────────────────────────────


_BASE_CONFIG = {
    "max_entry_cents": 55,
    "min_entry_cents": 5,
    "min_time_remaining_s": 90.0,
    "kelly_fraction": 0.10,
    "kelly_max_frac": 0.05,
    "max_contracts": 200,
}


def test_fallback_yes_at_cheap_price_fires():
    sig = build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    )
    assert sig is not None
    assert sig.side == "yes"
    assert sig.suggested_entry_cents == 35
    assert sig.alignment == "aligned_fallback"
    assert sig.contracts >= 1


def test_fallback_no_at_cheap_price_fires():
    # NO buy: entry = 100 - market_mid. mid=70 → NO entry @ 30
    sig = build_alignment_fallback_signal(
        side="no",
        market_mid_cents=70,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    )
    assert sig is not None
    assert sig.side == "no"
    assert sig.suggested_entry_cents == 30
    assert sig.alignment == "aligned_fallback"


def test_fallback_above_max_entry_returns_none():
    # YES at 60c is above the 55c cap
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=60,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_below_min_entry_returns_none():
    # YES at 4c is below the 5c floor
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=4,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_too_close_to_expiry_returns_none():
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=60.0,  # below 90s default
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_zero_balance_returns_none():
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=0.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_invalid_side_returns_none():
    assert build_alignment_fallback_signal(
        side="bogus",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_mid_zero_returns_none():
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=0,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_mid_one_hundred_returns_none():
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=100,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    ) is None


def test_fallback_sizing_capped_by_max_frac():
    # kelly_fraction=0.10 but kelly_max_frac=0.05 — sizing must respect cap
    sig = build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=1000.0,
        config=_BASE_CONFIG,
    )
    assert sig is not None
    # bet_dollars = 0.05 × $1000 = $50; at 35c per contract → 143ct
    # (max kelly_frac of 0.05 dominates the 0.10 kelly_fraction)
    assert sig.kelly_fraction == pytest.approx(0.05)
    assert sig.contracts == 143


def test_fallback_zero_kelly_returns_none():
    cfg = dict(_BASE_CONFIG)
    cfg["kelly_fraction"] = 0.0
    cfg["kelly_max_frac"] = 0.0
    assert build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=cfg,
    ) is None


def test_fallback_respects_max_contracts():
    cfg = dict(_BASE_CONFIG)
    cfg["max_contracts"] = 10
    sig = build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=10_000.0,
        config=cfg,
    )
    assert sig is not None
    assert sig.contracts == 10


def test_fallback_minimum_one_contract_when_sized():
    # tiny balance — sizing math gives < 1 contract, must round up to 1
    sig = build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=1.0,
        config=_BASE_CONFIG,
    )
    assert sig is not None
    assert sig.contracts >= 1


def test_fallback_signal_alignment_is_aligned_fallback():
    """Distinct from regular BB_PURE signals which are 'neutral' by default
    — downstream code can route on this tag."""
    sig = build_alignment_fallback_signal(
        side="yes",
        market_mid_cents=35,
        seconds_to_expiry=300.0,
        balance_dollars=100.0,
        config=_BASE_CONFIG,
    )
    assert sig is not None
    assert sig.alignment == "aligned_fallback"


def test_default_bb_signal_alignment_is_neutral():
    """Regression: existing bb_pure.evaluate paths should still produce
    BBSignal with alignment='neutral' (the new field's default)."""
    sig = BBSignal(
        side="yes",
        edge_pp=10.0,
        fair_yes_cents=50,
        market_mid_cents=40,
        suggested_entry_cents=40,
        win_probability=0.5,
        kelly_fraction=0.05,
        contracts=5,
        seconds_to_expiry=300.0,
        reason="test",
    )
    assert sig.alignment == "neutral"


# ─── classify_position_alignment (B1) ──────────────────────────────────


def test_position_align_yes_above_strike_is_aligned():
    """BTC above strike → YES likely settles ITM → buying YES is aligned."""
    assert classify_position_alignment(
        side="yes", btc_price=80000, strike=79900) == "aligned"


def test_position_align_no_below_strike_is_aligned():
    """BTC below strike → NO likely settles ITM → buying NO is aligned."""
    assert classify_position_alignment(
        side="no", btc_price=79900, strike=80000) == "aligned"


def test_position_align_yes_below_strike_is_contrarian():
    assert classify_position_alignment(
        side="yes", btc_price=79900, strike=80000) == "contrarian"


def test_position_align_no_above_strike_is_contrarian():
    assert classify_position_alignment(
        side="no", btc_price=80000, strike=79900) == "contrarian"


def test_position_align_within_deadband_is_neutral():
    # default deadband=$5; |delta|=$3 → neutral
    assert classify_position_alignment(
        side="yes", btc_price=79903, strike=79900) == "neutral"
    assert classify_position_alignment(
        side="no", btc_price=79897, strike=79900) == "neutral"


def test_position_align_at_deadband_boundary_is_neutral():
    assert classify_position_alignment(
        side="yes", btc_price=79905, strike=79900) == "neutral"


def test_position_align_just_past_deadband_classifies():
    assert classify_position_alignment(
        side="yes", btc_price=79906, strike=79900) == "aligned"
    assert classify_position_alignment(
        side="no", btc_price=79906, strike=79900) == "contrarian"


def test_position_align_zero_btc_returns_neutral():
    assert classify_position_alignment(
        side="yes", btc_price=0, strike=79900) == "neutral"


def test_position_align_zero_strike_returns_neutral():
    assert classify_position_alignment(
        side="yes", btc_price=80000, strike=0) == "neutral"


def test_position_align_handles_uppercase_side():
    assert classify_position_alignment(
        side="YES", btc_price=80000, strike=79900) == "aligned"
    assert classify_position_alignment(
        side="No", btc_price=79900, strike=80000) == "aligned"


def test_position_align_custom_deadband():
    # deadband=$50, |delta|=$30 → neutral
    assert classify_position_alignment(
        side="yes", btc_price=79930, strike=79900, deadband_usd=50.0
    ) == "neutral"
    # deadband=$1, |delta|=$30 → classified
    assert classify_position_alignment(
        side="yes", btc_price=79930, strike=79900, deadband_usd=1.0
    ) == "aligned"


def test_position_align_real_world_today_setups():
    """Reproduce today's actual blocked-by-strike-distance windows.
    All would have been allowed via POSITION_ALIGNED override:"""
    # Window 9:00-9:15 ET: BTC=$78,569, strike=$78,492 (above) — YES aligned
    assert classify_position_alignment(
        side="yes", btc_price=78569, strike=78492) == "aligned"
    # Window 10:45-11:00 ET: BTC=$80,151, strike=$79,951 (above) — YES aligned
    assert classify_position_alignment(
        side="yes", btc_price=80151, strike=79951) == "aligned"
    # Counter case: today's Trade #6 was NO when BTC was above strike → contrarian
    assert classify_position_alignment(
        side="no", btc_price=80151, strike=79951) == "contrarian"
