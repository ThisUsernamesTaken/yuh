"""Tests for BB dual-regime strike-distance gate (2026-05-03).

Two qualifying regimes:
  MEAN_REVERSION: dist <= MAX_STRIKE_DIST_PCT (default 0.04%)
  TREND:          dist >= TREND_MIN_STRIKE_DIST_PCT (default 0.15%)
                  AND signal direction matches BTC's side
                  AND 5-min BTC momentum is not strongly reverting against trend

Mid-zone (between the two thresholds) blocks regardless of direction.

The pure decision logic (without engine state) is replicated here so we can
unit-test it deterministically. Live integration is exercised in the engine
(_evaluate_bb_pure_signal) and validated via backtest_dual_regime.py.
"""
from __future__ import annotations


def classify_regime(
    btc_price: float,
    strike: float,
    side: str,                       # "yes" or "no"
    btc_5m_move: float,              # signed dollars over 300s
    *,
    max_meanrev_pct: float = 0.0004,
    min_trend_pct: float = 0.0015,
    max_reversal_dollars: float = 50.0,
    trend_mode_on: bool = True,
) -> str:
    """Return 'MEAN_REVERSION' | 'TREND' | 'BLOCK' (mirror of engine logic)."""
    if btc_price <= 0 or strike <= 0:
        return "BLOCK"
    dist_pct = abs(btc_price - strike) / btc_price
    in_meanrev = dist_pct <= max_meanrev_pct

    in_trend = False
    trend_match = False
    persist_ok = False
    if trend_mode_on:
        in_trend = dist_pct >= min_trend_pct
        if in_trend:
            btc_above = btc_price > strike
            signal_dir_yes = (side == "yes")
            trend_match = (btc_above == signal_dir_yes)
            if btc_above:
                persist_ok = (btc_5m_move > -max_reversal_dollars)
            else:
                persist_ok = (btc_5m_move < max_reversal_dollars)

    if in_meanrev:
        return "MEAN_REVERSION"
    if trend_mode_on and in_trend and trend_match and persist_ok:
        return "TREND"
    return "BLOCK"


# ─── MEAN_REVERSION zone ────────────────────────────────────────────────


def test_meanrev_zone_passes_at_strike():
    # BTC right on strike → 0% distance, well within 0.04% cap.
    assert classify_regime(
        btc_price=78_000.0, strike=78_000.0, side="yes", btc_5m_move=0
    ) == "MEAN_REVERSION"


def test_meanrev_zone_passes_just_under_cap():
    # 0.039% distance, within cap.
    assert classify_regime(
        btc_price=78_030.0, strike=78_000.0, side="yes", btc_5m_move=0
    ) == "MEAN_REVERSION"


def test_meanrev_zone_passes_regardless_of_direction():
    # In mean-rev zone, NO signal allowed even when BTC slightly above strike
    # (mean-reversion is a contrarian thesis).
    assert classify_regime(
        btc_price=78_030.0, strike=78_000.0, side="no", btc_5m_move=0
    ) == "MEAN_REVERSION"


# ─── BLOCK (mid-zone) ───────────────────────────────────────────────────


def test_midzone_blocks_just_outside_meanrev():
    # 0.05% — past mean-rev cap (0.04%) but not yet trend zone (0.15%).
    assert classify_regime(
        btc_price=78_039.0, strike=78_000.0, side="yes", btc_5m_move=0
    ) == "BLOCK"


def test_midzone_blocks_at_exactly_0_10_pct():
    # 0.10% — past mean-rev cap, before trend min (0.15%).
    assert classify_regime(
        btc_price=78_078.0, strike=78_000.0, side="yes", btc_5m_move=50
    ) == "BLOCK"


# ─── TREND zone ─────────────────────────────────────────────────────────


def test_trend_zone_btc_above_strike_yes_passes():
    # BTC firmly above strike (0.30%), YES signal, momentum supports.
    assert classify_regime(
        btc_price=78_234.0, strike=78_000.0, side="yes", btc_5m_move=100
    ) == "TREND"


def test_trend_zone_btc_below_strike_no_passes():
    # BTC firmly below strike (0.50%), NO signal, momentum supports.
    assert classify_regime(
        btc_price=77_610.0, strike=78_000.0, side="no", btc_5m_move=-150
    ) == "TREND"


def test_trend_zone_direction_mismatch_blocks_yes_below():
    # BTC below strike, but signal says YES (contrarian) → block in trend.
    assert classify_regime(
        btc_price=77_610.0, strike=78_000.0, side="yes", btc_5m_move=-100
    ) == "BLOCK"


def test_trend_zone_direction_mismatch_blocks_no_above():
    # BTC above strike, but signal says NO (contrarian) → block in trend.
    assert classify_regime(
        btc_price=78_300.0, strike=78_000.0, side="no", btc_5m_move=100
    ) == "BLOCK"


def test_trend_zone_strong_reversal_blocks():
    # BTC currently $300 above strike but dropped $80 in last 5min — risky
    # to trend-follow YES, the reversal could carry through strike.
    assert classify_regime(
        btc_price=78_300.0, strike=78_000.0, side="yes", btc_5m_move=-80
    ) == "BLOCK"


def test_trend_zone_mild_reversal_passes():
    # BTC currently $300 above strike, mild $20 dip in last 5min.
    # Within MAX_REVERSAL_DOLLARS (50) → still trend-OK.
    assert classify_regime(
        btc_price=78_300.0, strike=78_000.0, side="yes", btc_5m_move=-20
    ) == "TREND"


def test_trend_zone_below_strike_strong_reversal_up_blocks():
    # BTC below strike but moving UP $80 → could rally through strike,
    # so NO trend-follow is risky.
    assert classify_regime(
        btc_price=77_700.0, strike=78_000.0, side="no", btc_5m_move=80
    ) == "BLOCK"


# ─── Trend mode disabled ────────────────────────────────────────────────


def test_trend_mode_off_falls_back_to_meanrev_only():
    # Far from strike, normally a TREND opportunity. With flag off, BLOCK.
    assert classify_regime(
        btc_price=78_300.0, strike=78_000.0, side="yes", btc_5m_move=100,
        trend_mode_on=False,
    ) == "BLOCK"


def test_trend_mode_off_meanrev_still_works():
    # Near strike, mean-reversion zone — should still pass.
    assert classify_regime(
        btc_price=78_010.0, strike=78_000.0, side="yes", btc_5m_move=100,
        trend_mode_on=False,
    ) == "MEAN_REVERSION"


# ─── Boundary precision ──────────────────────────────────────────────────


def test_meanrev_exactly_at_cap_passes():
    # 0.04% exactly — at the cap boundary, included.
    assert classify_regime(
        btc_price=78_031.2, strike=78_000.0, side="yes", btc_5m_move=0
    ) == "MEAN_REVERSION"


def test_trend_just_above_min_passes():
    # 0.16% — just above the 0.15% threshold, with direction match + persist.
    # (Note: dist = |btc-strike|/btc, not /strike. Using btc=78125, strike=78000
    #  gives dist = 125/78125 ≈ 0.0016 > 0.0015 cap.)
    assert classify_regime(
        btc_price=78_125.0, strike=78_000.0, side="yes", btc_5m_move=10
    ) == "TREND"


def test_zero_btc_or_strike_blocks():
    """Defensive: bad inputs should block, not crash."""
    assert classify_regime(
        btc_price=0, strike=78_000, side="yes", btc_5m_move=0
    ) == "BLOCK"
    assert classify_regime(
        btc_price=78_000, strike=0, side="yes", btc_5m_move=0
    ) == "BLOCK"
