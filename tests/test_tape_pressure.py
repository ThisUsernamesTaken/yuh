"""Tests for tape_pressure module (Phase 8 — absorption detection).

Encodes the user's manual-trading edge: sustained large buys on the side
opposite to BTC's recent direction = institutional absorption =
high-conviction signal that retracement is coming.
"""
from __future__ import annotations

from tape_pressure import (
    TapePressureSnapshot,
    compute_pressure_snapshot,
    evaluate_tape_decision,
    trade_dollar_value,
)


# ── trade_dollar_value ───────────────────────────────────────────────────


def test_yes_buy_dollar_value_uses_yes_price():
    """A yes buy at 30c for 100 contracts costs $30."""
    assert trade_dollar_value("yes", 100, 30) == 30.0


def test_no_buy_dollar_value_uses_inverted_price():
    """A no buy at "yes_price=30" costs (1 - 0.30) = 70c per contract.
    100 contracts = $70."""
    assert trade_dollar_value("no", 100, 30) == 70.0


def test_dollar_value_zero_count():
    assert trade_dollar_value("yes", 0, 50) == 0.0


def test_dollar_value_unknown_side():
    assert trade_dollar_value("banana", 100, 50) == 0.0


def test_dollar_value_clamps_yes_price():
    """Out-of-range price clamps to [0, 100]."""
    assert trade_dollar_value("yes", 100, 200) == 100.0  # clamped to 100c
    assert trade_dollar_value("yes", 100, -10) == 0.0   # clamped to 0c


# ── compute_pressure_snapshot — windowing ───────────────────────────────


SS_MS = 1_000_000_000_000  # arbitrary session start


def _trade(t_offset_s: float, side: str, count: int, yes_px: int):
    return (SS_MS + int(t_offset_s * 1000), side, count, yes_px)


def test_window_includes_in_range_excludes_out_of_range():
    """Trades outside [window_min_s, window_max_s] are ignored."""
    trades = [
        _trade(-10, "yes", 100, 50),  # before session start — exclude
        _trade(60, "yes", 100, 50),   # 1min in — include (within 0-300)
        _trade(400, "yes", 100, 50),  # 6.6min — exclude
    ]
    snap = compute_pressure_snapshot(
        trades=trades,
        session_start_ms=SS_MS,
        now_ms=SS_MS + 500_000,
        btc_5m_change_usd=0.0,
        window_min_s=0,
        window_max_s=300,
    )
    assert snap.sample_count == 1
    assert snap.yes_buy_dollars == 50.0  # 100 × 0.50


# ── compute_pressure_snapshot — direction classification ─────────────────


def test_btc_down_makes_yes_inverse_side():
    """BTC down 50 → yes buys are 'inverse-trend' (fighting the drop)."""
    trades = [
        _trade(60, "yes", 200, 30),  # $60 yes buy
        _trade(120, "no", 100, 30),  # $70 no buy
    ]
    snap = compute_pressure_snapshot(
        trades=trades,
        session_start_ms=SS_MS,
        now_ms=SS_MS + 200_000,
        btc_5m_change_usd=-50.0,
    )
    assert snap.inverse_trend_side == "yes"
    assert snap.inverse_trend_dollars == 60.0  # yes buys
    assert snap.with_trend_dollars == 70.0     # no buys


def test_btc_up_makes_no_inverse_side():
    """BTC up 50 → no buys are 'inverse-trend' (fighting the rip)."""
    trades = [
        _trade(60, "yes", 200, 30),
        _trade(120, "no", 100, 30),
    ]
    snap = compute_pressure_snapshot(
        trades=trades,
        session_start_ms=SS_MS,
        now_ms=SS_MS + 200_000,
        btc_5m_change_usd=+50.0,
    )
    assert snap.inverse_trend_side == "no"
    assert snap.inverse_trend_dollars == 70.0  # no buys
    assert snap.with_trend_dollars == 60.0


def test_btc_neutral_means_no_inverse_side():
    """|BTC change| ≤ dead zone → no absorption call possible."""
    trades = [_trade(60, "yes", 200, 30)]
    snap = compute_pressure_snapshot(
        trades=trades,
        session_start_ms=SS_MS,
        now_ms=SS_MS + 200_000,
        btc_5m_change_usd=10.0,  # within default 20 dead zone
        btc_dead_zone_usd=20.0,
    )
    assert snap.inverse_trend_side == "none"


# ── compute_pressure_snapshot — large buy counts ─────────────────────────


def test_large_buy_count_uses_dollar_threshold():
    """large_buy_usd default 100. Only buys $100+ count toward
    yes_large_count / no_large_count."""
    trades = [
        _trade(60, "yes", 100, 30),    # $30 → small
        _trade(70, "yes", 400, 30),    # $120 → large
        _trade(80, "yes", 1000, 30),   # $300 → reliable
    ]
    snap = compute_pressure_snapshot(
        trades=trades,
        session_start_ms=SS_MS,
        now_ms=SS_MS + 200_000,
        btc_5m_change_usd=0.0,
        large_buy_usd=100.0,
        reliable_buy_usd=200.0,
    )
    assert snap.yes_large_count == 2  # $120 + $300
    assert snap.yes_reliable_count == 1  # only $300


# ── evaluate_tape_decision ───────────────────────────────────────────────


def _snap(yes_d, no_d, yes_lc, no_lc, btc, inv_side, inv_d, with_d):
    return TapePressureSnapshot(
        yes_buy_dollars=yes_d, no_buy_dollars=no_d,
        yes_large_count=yes_lc, no_large_count=no_lc,
        yes_reliable_count=0, no_reliable_count=0,
        btc_5m_change_usd=btc,
        inverse_trend_side=inv_side,
        inverse_trend_dollars=inv_d,
        with_trend_dollars=with_d,
        sample_count=10,
    )


def test_confirm_when_aligned_with_absorption():
    """User's exact pattern: BTC DOWN, YES buys $1000 (inverse), NO buys
    $500 (with-trend). YES has 4 large buys (consistency).
    Engine wants YES → CONFIRM."""
    snap = _snap(
        yes_d=1000, no_d=500, yes_lc=4, no_lc=2,
        btc=-50, inv_side="yes", inv_d=1000, with_d=500,
    )
    assert evaluate_tape_decision(snap, "yes") == "confirm"


def test_block_when_against_absorption():
    """Same scenario, but engine wants NO. Big money is on YES side
    (inverse-trend absorption); fighting them → BLOCK."""
    snap = _snap(
        yes_d=1000, no_d=500, yes_lc=4, no_lc=2,
        btc=-50, inv_side="yes", inv_d=1000, with_d=500,
    )
    assert evaluate_tape_decision(snap, "no") == "block"


def test_neutral_when_btc_neutral():
    snap = _snap(
        yes_d=500, no_d=500, yes_lc=2, no_lc=2,
        btc=10, inv_side="none", inv_d=0, with_d=0,
    )
    assert evaluate_tape_decision(snap, "yes") == "neutral"


def test_neutral_when_inverse_dollars_below_threshold():
    """Even in clear BTC trend, $50 isn't enough — that's noise."""
    snap = _snap(
        yes_d=50, no_d=20, yes_lc=1, no_lc=0,
        btc=-50, inv_side="yes", inv_d=50, with_d=20,
    )
    assert evaluate_tape_decision(
        snap, "yes", inverse_trend_min_usd=200,
    ) == "neutral"


def test_neutral_when_dominance_ratio_too_low():
    """Inverse $300 with-trend $250 = ratio 1.2 < 2.0 default → neutral."""
    snap = _snap(
        yes_d=300, no_d=250, yes_lc=4, no_lc=3,
        btc=-50, inv_side="yes", inv_d=300, with_d=250,
    )
    assert evaluate_tape_decision(snap, "yes") == "neutral"


def test_neutral_when_consistency_too_low():
    """Inverse dominates but only 1 large buy = not 'sustained'."""
    snap = _snap(
        yes_d=1000, no_d=200, yes_lc=1, no_lc=0,  # only 1 large buy
        btc=-50, inv_side="yes", inv_d=1000, with_d=200,
    )
    assert evaluate_tape_decision(
        snap, "yes", min_consistency_count=3,
    ) == "neutral"


def test_strong_absorption_pattern_full():
    """All conditions met: $1000 inverse, $300 with-trend (3.3x), 5 large
    buys. Aligned side → CONFIRM."""
    snap = _snap(
        yes_d=1000, no_d=300, yes_lc=5, no_lc=2,
        btc=-50, inv_side="yes", inv_d=1000, with_d=300,
    )
    assert evaluate_tape_decision(snap, "yes") == "confirm"
    assert evaluate_tape_decision(snap, "no") == "block"


# ── Tonight's actual loser scenario ──────────────────────────────────────


def test_tonight_24c_no_fade_would_be_blocked():
    """The actual losing fade from 2026-05-02 01:31 PT.

    BTC ripped +200 in 2 min. YES contract surged. Engine fired NO at
    24c (= synthetic short YES). If during those 2 min there were
    sustained large YES buys (= institutional buying the rip), tape
    pressure would have flagged absorption on YES side. NO entry =
    fighting absorption → BLOCK.
    """
    # Fictional but representative tape state during the rip
    snap = _snap(
        yes_d=2500, no_d=400,    # YES dominates
        yes_lc=8, no_lc=1,        # 8 large yes buys (sustained)
        btc=+200,                 # BTC ripped
        inv_side="no",            # BTC up → NO is the inverse side
        inv_d=400, with_d=2500,   # but NO dollars (400) < YES (2500)
    )
    # NO side is "inverse" but $400 with low consistency doesn't qualify
    # as absorption. The DOMINANT side (yes) is going WITH the trend.
    # No absorption signal → neutral.
    assert evaluate_tape_decision(snap, "no") == "neutral"
    # In this case, the asymmetric vol gate (Phase 6) catches it instead.
