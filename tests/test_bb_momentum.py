"""Tests for bb_momentum (2026-05-03).

Pure-logic momentum strategy:
  - Detection: btc_move_300s + btc_move_30s same direction, magnitude
  - Entry: WITH the trend, cheap-side only
  - Exit: stall (velocity decay) or reversal (sign-flip)
"""
from __future__ import annotations

from bb_momentum import evaluate_entry, evaluate_stall

_BASE_ENTRY_CFG = {
    "min_btc_move_300s_dollars": 30.0,
    "min_btc_move_30s_dollars": 10.0,
    "require_same_direction": True,
    "max_entry_cents": 50,
    "min_entry_cents": 5,
    "min_time_remaining_s": 90.0,
    "kelly_fraction": 0.20,
    "kelly_max_frac": 0.05,
    "max_contracts": 200,
}

_BASE_STALL_CFG = {
    "stall_decay_ratio": 0.5,
    "stall_min_entry_velocity": 5.0,
}


def _eval_entry(**overrides):
    cfg = dict(_BASE_ENTRY_CFG)
    cfg.update(overrides.pop("config_overrides", {}))
    return evaluate_entry(
        btc_move_30s=overrides.pop("btc_30", 15.0),
        btc_move_300s=overrides.pop("btc_300", 50.0),
        yes_mid_cents=overrides.pop("yes_mid", 35),
        seconds_to_expiry=overrides.pop("seconds_to_expiry", 600.0),
        balance_dollars=overrides.pop("balance", 100.0),
        config=cfg,
    )


# ─── Detection: directional confirmation required ──────────────────────


def test_btc_up_with_confirmation_buys_yes():
    sig = _eval_entry(btc_30=15, btc_300=50, yes_mid=35)
    assert sig is not None
    assert sig.side == "yes"
    assert sig.suggested_entry_cents == 35


def test_btc_down_with_confirmation_buys_no():
    sig = _eval_entry(btc_30=-15, btc_300=-50, yes_mid=65)
    assert sig is not None
    assert sig.side == "no"
    # NO mid = 100 - 65 = 35
    assert sig.suggested_entry_cents == 35


def test_no_300s_trend_blocks():
    """5-min trend below threshold → no qualifying directionality."""
    sig = _eval_entry(btc_300=20)  # below 30 threshold
    assert sig is None


def test_no_30s_confirmation_blocks():
    """30s velocity below threshold → no recent confirmation."""
    sig = _eval_entry(btc_30=5)  # below 10 threshold
    assert sig is None


def test_mixed_directions_blocks_when_required():
    """5-min UP but 30s DOWN → reversal in progress, don't enter."""
    sig = _eval_entry(btc_30=-15, btc_300=50)
    assert sig is None


def test_mixed_directions_allows_when_not_required():
    """If require_same_direction=False, allow even with mixed signals."""
    sig = _eval_entry(
        btc_30=-15, btc_300=50, yes_mid=35,
        config_overrides={"require_same_direction": False},
    )
    assert sig is not None
    # Direction picked from 5min trend (UP) → buy YES
    assert sig.side == "yes"


# ─── Entry-price cap ───────────────────────────────────────────────────


def test_yes_too_expensive_blocks():
    """BTC up but YES already at 60c → no appreciation room."""
    sig = _eval_entry(btc_300=50, yes_mid=60)
    assert sig is None


def test_no_too_expensive_blocks():
    """BTC down but NO already at 60c (yes_mid=40) → no room."""
    sig = _eval_entry(btc_300=-50, yes_mid=40)
    assert sig is None


def test_yes_dust_blocks():
    """YES mid too low (4c) → no liquidity, settle risk."""
    sig = _eval_entry(btc_300=50, yes_mid=4)
    assert sig is None


# ─── Time gate ────────────────────────────────────────────────────────


def test_time_too_short_blocks():
    sig = _eval_entry(seconds_to_expiry=60.0)  # below 90s min
    assert sig is None


# ─── Sizing ────────────────────────────────────────────────────────────


def test_kelly_caps_at_kelly_max_frac():
    """Even huge favorable signals capped at kelly_max_frac=0.05."""
    sig = _eval_entry(yes_mid=10, balance=1000.0)
    assert sig is not None
    # bet = 0.05 × 1000 = $50, at 10c entry → 500 contracts
    # but max_contracts=200 caps it
    assert sig.contracts == 200


def test_balance_zero_blocks():
    sig = _eval_entry(balance=0)
    assert sig is None


# ─── Stall detection ──────────────────────────────────────────────────


def test_stall_velocity_halved_triggers_exit():
    """Entry vel +30, current vel +14 → ratio 0.47 < 0.5 → STALL."""
    v = evaluate_stall(
        entry_velocity_30s=30.0,
        current_velocity_30s=14.0,
        config=_BASE_STALL_CFG,
    )
    assert v.should_exit is True
    assert "STALL" in v.reason


def test_stall_velocity_persistent_no_exit():
    """Entry vel +30, current vel +25 → ratio 0.83 > 0.5 → continue."""
    v = evaluate_stall(
        entry_velocity_30s=30.0,
        current_velocity_30s=25.0,
        config=_BASE_STALL_CFG,
    )
    assert v.should_exit is False
    assert v.reason == "NONE"


def test_stall_sign_flip_triggers_reversal_exit():
    """Entry vel +30, current vel -10 → reversal."""
    v = evaluate_stall(
        entry_velocity_30s=30.0,
        current_velocity_30s=-10.0,
        config=_BASE_STALL_CFG,
    )
    assert v.should_exit is True
    assert "REVERSAL" in v.reason


def test_stall_sign_flip_to_noise_no_exit():
    """Entry vel +30, current vel -3 → flip but tiny mag (below
    stall_min_entry_velocity floor 5) → don't treat as reversal."""
    v = evaluate_stall(
        entry_velocity_30s=30.0,
        current_velocity_30s=-3.0,
        config=_BASE_STALL_CFG,
    )
    # Sign flip but below floor → not REVERSAL
    # However |current|=3 < 0.5*|entry|=15 → STALL fires
    assert v.should_exit is True
    assert "STALL" in v.reason


def test_stall_tiny_entry_velocity_skips_stall_check():
    """Entry vel +3 (below floor 5) → can't meaningfully assess decay."""
    v = evaluate_stall(
        entry_velocity_30s=3.0,
        current_velocity_30s=0.5,
        config=_BASE_STALL_CFG,
    )
    assert v.should_exit is False


def test_stall_negative_direction_decay():
    """Entry vel -30 (BTC down trade), current -10 → ratio 0.33 → STALL."""
    v = evaluate_stall(
        entry_velocity_30s=-30.0,
        current_velocity_30s=-10.0,
        config=_BASE_STALL_CFG,
    )
    assert v.should_exit is True
    assert "STALL" in v.reason


# ─── Defensive ────────────────────────────────────────────────────────


def test_yes_mid_zero_blocks():
    sig = _eval_entry(yes_mid=0)
    assert sig is None


def test_yes_mid_100_blocks():
    sig = _eval_entry(yes_mid=100)
    assert sig is None


def test_seconds_to_expiry_zero_blocks():
    sig = _eval_entry(seconds_to_expiry=0)
    assert sig is None
