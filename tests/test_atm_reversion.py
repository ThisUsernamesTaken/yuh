"""Unit tests for atm_reversion decision logic.

Covers:
- Strike-distance gate (entry rejected outside max_strike_dist_pct)
- Discount-mode entries (yes-side, no-side, edge-tie)
- Min-edge / min-fair / max-entry rejections
- Bias-mode disabled by default; opt-in path
- Exit priorities: target_c > profit_target_c > strike_escape > time
- Kalshi taker fee shape (concave around 50c)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atm_reversion import (
    AtmContext,
    evaluate,
    evaluate_exit,
    kalshi_taker_fee_c,
    STRATEGY_LABEL,
)


# Sweep-validated production candidate config
DEFAULTS = dict(
    max_strike_dist_pct=0.030,
    max_entry_c=35,
    min_fair_c=47.0,
    min_edge_c=8.0,
)


def _ctx(btc, strike, yes_bid_c, yes_ask_c, fair_yes_c=50.0):
    return AtmContext(
        btc_price=btc, strike=strike,
        yes_bid_c=yes_bid_c, yes_ask_c=yes_ask_c,
        fair_yes_c=fair_yes_c,
    )


# ── Sanity ─────────────────────────────────────────────────────────────────

def test_strategy_label_is_pristine():
    assert STRATEGY_LABEL == "ATM_REVERSION_DISCOUNT"


# ── Kalshi taker fee shape ─────────────────────────────────────────────────

def test_taker_fee_zero_at_extremes():
    # 7 * 0 * 1 = 0; 7 * 1 * 0 = 0
    assert kalshi_taker_fee_c(0) == 0.0
    assert kalshi_taker_fee_c(100) == 0.0


def test_taker_fee_peaks_at_50c():
    # 7 * 0.5 * 0.5 = 1.75c
    assert abs(kalshi_taker_fee_c(50) - 1.75) < 1e-9
    # symmetry
    assert abs(kalshi_taker_fee_c(35) - kalshi_taker_fee_c(65)) < 1e-9
    # concave: f(50) > f(35) > f(20)
    assert kalshi_taker_fee_c(50) > kalshi_taker_fee_c(35) > kalshi_taker_fee_c(20)


def test_taker_fee_clamped():
    # negative or >100 should not blow up
    assert kalshi_taker_fee_c(-5) == 0.0
    assert kalshi_taker_fee_c(150) == 0.0


# ── Strike-distance gate ───────────────────────────────────────────────────

def test_rejects_when_btc_far_from_strike():
    # 0.05% off strike — outside default 0.030% gate
    ctx = _ctx(btc=100050, strike=100000, yes_bid_c=51, yes_ask_c=42)
    assert evaluate(ctx, **DEFAULTS) is None


def test_accepts_at_strike_zero_dist():
    # Tight book: yes_ask=35 satisfies max_entry_c=35 with edge=15c.
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=34, yes_ask_c=35)
    out = evaluate(ctx, **DEFAULTS)
    assert out is not None
    assert out.side == "yes"
    assert out.entry_c == 35
    assert out.kind == "discount"
    assert abs(out.strike_dist_pct) < 1e-9


def test_rejects_invalid_inputs():
    # zero strike
    assert evaluate(_ctx(btc=100000, strike=0, yes_bid_c=50, yes_ask_c=50),
                    **DEFAULTS) is None
    # zero btc
    assert evaluate(_ctx(btc=0, strike=100000, yes_bid_c=50, yes_ask_c=50),
                    **DEFAULTS) is None
    # crossed/empty book
    assert evaluate(_ctx(btc=100000, strike=100000, yes_bid_c=0, yes_ask_c=50),
                    **DEFAULTS) is None
    assert evaluate(_ctx(btc=100000, strike=100000, yes_bid_c=50, yes_ask_c=100),
                    **DEFAULTS) is None


def test_rejects_zero_cent_derived_no_ask():
    # Kalshi complementary book can report yes_bid=100, which implies
    # NO ask = 0. Paper/live observers must not record impossible 0c entries.
    assert evaluate(_ctx(btc=100000, strike=100000, yes_bid_c=100, yes_ask_c=26),
                    **DEFAULTS) is None


# ── Discount entries ───────────────────────────────────────────────────────

def test_yes_discount_fires_when_under_fair():
    # YES ask 32c, fair 50c → 18c edge. Min 8c required.
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=31, yes_ask_c=32)
    out = evaluate(ctx, **DEFAULTS)
    assert out is not None
    assert out.side == "yes"
    assert out.entry_c == 32
    assert out.edge_c == 18  # 50 - 32
    assert out.kind == "discount"


def test_no_discount_fires_when_under_fair():
    # YES bid 65, so NO ask = 100-65 = 35c. Fair NO = 50c. Edge = 15c.
    # YES ask 80c is too high to qualify as discount.
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=65, yes_ask_c=80)
    out = evaluate(ctx, **DEFAULTS)
    assert out is not None
    assert out.side == "no"
    assert out.entry_c == 35
    assert out.edge_c == 15
    assert out.kind == "discount"


def test_edge_tie_prefers_yes():
    # A symmetric 35c/35c "both sides discounted" state implies
    # YES bid=65c and derived YES ask=35c. Kalshi WS derives YES ask
    # from best NO bid, so crossed/complementary states are valid to
    # observe and must be execution-verified separately.
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=65, yes_ask_c=35)
    out = evaluate(ctx, **DEFAULTS)
    assert out is not None
    # Tie → yes wins (yes_edge >= no_edge branch)
    assert out.side == "yes"


def test_rejects_when_edge_too_thin():
    # YES ask 43c → edge=7c < 8c minimum
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=42, yes_ask_c=43)
    out = evaluate(ctx, **DEFAULTS)
    assert out is None


def test_rejects_when_entry_too_expensive():
    # YES ask 36c → above 35c cap
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=35, yes_ask_c=36)
    out = evaluate(ctx, **DEFAULTS)
    assert out is None


def test_rejects_when_fair_too_low():
    # fair_yes=46 < min_fair_c=47 → YES side blocked
    ctx = _ctx(btc=100000, strike=100000, yes_bid_c=30, yes_ask_c=31, fair_yes_c=46.0)
    out = evaluate(ctx, **DEFAULTS)
    # fair_no = 100-46 = 54 ≥ 47, no_ask = 70 — too expensive for max_entry_c
    # so still no fire
    assert out is None


# ── Bias mode (opt-in only) ────────────────────────────────────────────────

def test_bias_disabled_by_default():
    # Off-strike + thin edge. Discount gate fails (entry too expensive).
    # With bias disabled (default), no fire.
    ctx = _ctx(btc=100020, strike=100000, yes_bid_c=58, yes_ask_c=60)
    out = evaluate(ctx, **DEFAULTS)
    assert out is None


def test_bias_mode_fires_on_off_strike():
    # btc above strike, yes underpriced relative to fair=50
    # 0.020% above strike (within max_strike_dist_pct=0.030)
    ctx = _ctx(btc=100020, strike=100000, yes_bid_c=46, yes_ask_c=48)
    out = evaluate(
        ctx, **DEFAULTS,
        bias_enabled=True, max_bias_entry_c=62, min_bias_edge_c=1.0,
    )
    assert out is not None
    assert out.kind == "bias"
    assert out.side == "yes"
    assert out.entry_c == 48


# ── Exit priorities ─────────────────────────────────────────────────────────

EXIT_DEFAULTS = dict(
    target_c=49,
    profit_target_c=8,
    stop_strike_dist_pct=0.080,
    force_exit_age_s=840,
)


def test_exit_target_wins_over_profit():
    # YES bid 49c >= target_c 49 — fires target reason even though
    # profit_target (entry+8 = 40) is also satisfied.
    decision = evaluate_exit(
        side="yes", entry_c=32,
        current_yes_bid_c=49, current_yes_ask_c=50,
        btc_price=100000, strike=100000, age_s=60,
        **EXIT_DEFAULTS,
    )
    assert decision.should_exit
    assert decision.reason == "target_49"


def test_exit_profit_target():
    # YES bid 40 = entry 32 + 8 (profit), but below target 49
    decision = evaluate_exit(
        side="yes", entry_c=32,
        current_yes_bid_c=40, current_yes_ask_c=41,
        btc_price=100000, strike=100000, age_s=60,
        **EXIT_DEFAULTS,
    )
    assert decision.should_exit
    assert decision.reason == "profit_8"


def test_exit_strike_escape():
    # 0.10% off strike → above 0.080 stop
    decision = evaluate_exit(
        side="yes", entry_c=32,
        current_yes_bid_c=30, current_yes_ask_c=31,
        btc_price=100100, strike=100000, age_s=60,
        **EXIT_DEFAULTS,
    )
    assert decision.should_exit
    assert decision.reason == "strike_escape"


def test_exit_time_cap():
    decision = evaluate_exit(
        side="yes", entry_c=32,
        current_yes_bid_c=33, current_yes_ask_c=34,
        btc_price=100000, strike=100000, age_s=900,
        **EXIT_DEFAULTS,
    )
    assert decision.should_exit
    assert decision.reason == "time"


def test_exit_no_signal_means_hold():
    decision = evaluate_exit(
        side="yes", entry_c=32,
        current_yes_bid_c=33, current_yes_ask_c=34,
        btc_price=100010, strike=100000, age_s=60,
        **EXIT_DEFAULTS,
    )
    assert not decision.should_exit


def test_exit_no_side_uses_inverted_book():
    # Held NO at 35c. Same-side bid = 100 - yes_ask.
    # If yes_ask=51, no_bid=49 → hits target_c=49.
    decision = evaluate_exit(
        side="no", entry_c=35,
        current_yes_bid_c=49, current_yes_ask_c=51,
        btc_price=100000, strike=100000, age_s=60,
        **EXIT_DEFAULTS,
    )
    assert decision.should_exit
    assert decision.reason == "target_49"
