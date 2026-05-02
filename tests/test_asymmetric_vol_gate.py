"""Tests for the asymmetric volatility gate (Phase 0.1.6 / plan Phase 6).

Tonight's loser pattern: BB_PURE bought NO at 24c right after BTC ripped
200+ in 2 min. Both sides got volatile, but the *direction* of the vol
mattered. Fading WITH the prevailing trend (counter-trend trade) is the
high-risk loser; going WITH the trend (rare for BB_PURE) is lower risk.

The asymmetric gate uses a stricter vol cap when our fade direction is
opposite to BTC's recent 5-min direction, looser when aligned.
"""
from __future__ import annotations

from protective_math import (
    classify_trend_orientation,
    select_vol_cap,
)


# ── classify_trend_orientation ───────────────────────────────────────────


def test_yes_with_btc_dropping_is_counter_trend():
    """BTC down 50, side=yes → fading the down-trend (mean-reversion)."""
    o = classify_trend_orientation(side="yes", trend_change_usd=-50.0, dead_zone_usd=20.0)
    assert o == "counter"


def test_no_with_btc_rising_is_counter_trend():
    """BTC up 50, side=no → fading the up-trend (mean-reversion)."""
    o = classify_trend_orientation(side="no", trend_change_usd=50.0, dead_zone_usd=20.0)
    assert o == "counter"


def test_yes_with_btc_rising_is_with_trend():
    """BTC up 50, side=yes → momentum-aligned (rare for BB_PURE)."""
    o = classify_trend_orientation(side="yes", trend_change_usd=50.0, dead_zone_usd=20.0)
    assert o == "with"


def test_no_with_btc_dropping_is_with_trend():
    """BTC down 50, side=no → momentum-aligned."""
    o = classify_trend_orientation(side="no", trend_change_usd=-50.0, dead_zone_usd=20.0)
    assert o == "with"


def test_dead_zone_treats_small_move_as_neutral():
    """|change| ≤ dead_zone → neutral, no asymmetry applied."""
    assert classify_trend_orientation(side="yes", trend_change_usd=15.0, dead_zone_usd=20.0) == "neutral"
    assert classify_trend_orientation(side="yes", trend_change_usd=-15.0, dead_zone_usd=20.0) == "neutral"
    assert classify_trend_orientation(side="no", trend_change_usd=10.0, dead_zone_usd=20.0) == "neutral"
    assert classify_trend_orientation(side="no", trend_change_usd=-10.0, dead_zone_usd=20.0) == "neutral"


def test_zero_change_is_neutral():
    assert classify_trend_orientation(side="yes", trend_change_usd=0.0, dead_zone_usd=20.0) == "neutral"


def test_unknown_side_is_neutral():
    """Defensive: bad input shouldn't crash, returns neutral."""
    o = classify_trend_orientation(side="banana", trend_change_usd=100.0, dead_zone_usd=20.0)
    assert o == "neutral"


def test_dead_zone_boundary_is_inclusive():
    """At exactly the dead zone, treat as neutral (not active counter/with)."""
    o = classify_trend_orientation(side="yes", trend_change_usd=20.0, dead_zone_usd=20.0)
    assert o == "neutral"


# ── select_vol_cap ───────────────────────────────────────────────────────


def test_counter_returns_strict_cap():
    cap = select_vol_cap(orientation="counter", cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 20


def test_with_returns_loose_cap():
    cap = select_vol_cap(orientation="with", cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 50


def test_neutral_returns_default_cap():
    cap = select_vol_cap(orientation="neutral", cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 30


# ── Composed scenario tests ──────────────────────────────────────────────


def test_tonight_24c_no_fade_scenario():
    """The actual loser from 2026-05-02 01:31 PT.

    BTC ripped ~+200 in 2min, BB_PURE saw NO at 24c with edge, fired.
    The BTC range over the 30s entry window was ~$80 (very volatile).
    With the new asymmetric gate, this should map to:
        - 5-min change: ~+200 (huge up move)
        - side=no → counter-trend
        - vol_cap = 20 (strict)
        - 30s range $80 > 20 → BLOCKED
    """
    o = classify_trend_orientation(side="no", trend_change_usd=200.0, dead_zone_usd=20.0)
    assert o == "counter"
    cap = select_vol_cap(orientation=o, cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 20
    # Recent 30s range that would have caught tonight's fire
    recent_range = 80
    blocked = recent_range > cap
    assert blocked, "the loser fade should be blocked by the strict counter cap"


def test_quiet_chop_fires_normally():
    """In dead-zone (BTC moved <$20 in 5min), use the default neutral cap.
    A 25c-range chop would fire under the default 30 cap."""
    o = classify_trend_orientation(side="yes", trend_change_usd=10.0, dead_zone_usd=20.0)
    assert o == "neutral"
    cap = select_vol_cap(orientation=o, cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 30
    recent_range = 25  # quiet chop
    blocked = recent_range > cap
    assert not blocked


def test_with_trend_lets_higher_vol_through():
    """If BTC ripped up and we're going YES (with trend), vol up to 50
    is acceptable because the move is in our favor."""
    o = classify_trend_orientation(side="yes", trend_change_usd=80.0, dead_zone_usd=20.0)
    assert o == "with"
    cap = select_vol_cap(orientation=o, cap_neutral=30, cap_counter=20, cap_with=50)
    assert cap == 50
    recent_range = 35  # would normally block under default 30
    blocked = recent_range > cap
    assert not blocked, "with-trend lets through higher vol"
