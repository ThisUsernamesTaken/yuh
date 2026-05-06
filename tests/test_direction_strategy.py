"""Unit tests for direction_strategy.

Pins every decision boundary against the OOS-validated thresholds
(scripts/backtest_direction.py) so the production code can't silently
drift away from the validated config.
"""
from __future__ import annotations

import pytest

from direction_strategy import (
    DEFAULT_DIST_THRESHOLD_PCT,
    DEFAULT_MOMENTUM_THRESHOLD,
    DirectionSignal,
    compute_contracts,
    daily_loss_halted,
    evaluate,
)


# ─── evaluate(): YES side ────────────────────────────────────────────────


def test_yes_clear_signal():
    """BTC above strike, momentum up — fires YES."""
    sig = evaluate(btc_price=100_100.0, strike=100_000.0, btc_5m_move=20.0)
    assert sig is not None
    assert sig.side == "yes"
    assert sig.dist_pct > 0
    assert sig.btc_5m_move == 20.0


def test_yes_at_dist_threshold_boundary():
    """Exactly +0.10% dist + threshold momentum — fires YES."""
    # 0.10% of 100,000 = 100 USD
    sig = evaluate(btc_price=100_100.0, strike=100_000.0,
                   btc_5m_move=10.0)
    assert sig is not None and sig.side == "yes"


def test_yes_just_below_dist_refuses():
    """Just below 0.10% dist — refuses."""
    sig = evaluate(btc_price=100_099.0, strike=100_000.0,
                   btc_5m_move=20.0)
    assert sig is None


def test_yes_just_below_momentum_refuses():
    """Above dist threshold but momentum below threshold — refuses."""
    sig = evaluate(btc_price=100_200.0, strike=100_000.0,
                   btc_5m_move=9.0)
    assert sig is None


# ─── evaluate(): NO side ─────────────────────────────────────────────────


def test_no_clear_signal():
    """BTC below strike, momentum down — fires NO."""
    sig = evaluate(btc_price=99_900.0, strike=100_000.0, btc_5m_move=-20.0)
    assert sig is not None
    assert sig.side == "no"
    assert sig.dist_pct < 0


def test_no_boundary():
    """Exactly -0.10% dist + -threshold momentum — fires NO."""
    sig = evaluate(btc_price=99_900.0, strike=100_000.0, btc_5m_move=-10.0)
    assert sig is not None and sig.side == "no"


def test_no_just_above_dist_refuses():
    """Just above (closer to strike) — refuses NO."""
    sig = evaluate(btc_price=99_901.0, strike=100_000.0, btc_5m_move=-20.0)
    assert sig is None


# ─── Mismatched-sign refusals ────────────────────────────────────────────


def test_above_strike_negative_momentum_refuses():
    """BTC above strike but moving down — refuses (sign mismatch)."""
    sig = evaluate(btc_price=100_500.0, strike=100_000.0, btc_5m_move=-20.0)
    assert sig is None


def test_below_strike_positive_momentum_refuses():
    """BTC below strike but moving up — refuses (sign mismatch)."""
    sig = evaluate(btc_price=99_500.0, strike=100_000.0, btc_5m_move=20.0)
    assert sig is None


# ─── Edge / invalid inputs ───────────────────────────────────────────────


def test_zero_strike_refuses():
    sig = evaluate(btc_price=100_000.0, strike=0.0, btc_5m_move=50.0)
    assert sig is None


def test_zero_btc_refuses():
    sig = evaluate(btc_price=0.0, strike=100_000.0, btc_5m_move=50.0)
    assert sig is None


def test_negative_inputs_refuse():
    sig = evaluate(btc_price=-100.0, strike=100_000.0, btc_5m_move=50.0)
    assert sig is None
    sig = evaluate(btc_price=100_000.0, strike=-100.0, btc_5m_move=50.0)
    assert sig is None


def test_zero_momentum_refuses():
    """Even far past strike, zero momentum doesn't fire (we wait for trend)."""
    sig = evaluate(btc_price=100_500.0, strike=100_000.0, btc_5m_move=0.0)
    assert sig is None


# ─── Custom thresholds ───────────────────────────────────────────────────


def test_tighter_dist_threshold():
    """0.20% dist threshold blocks a 0.15% signal."""
    sig = evaluate(
        btc_price=100_150.0, strike=100_000.0, btc_5m_move=20.0,
        dist_threshold_pct=0.0020,
    )
    assert sig is None
    # But a 0.25% one fires:
    sig = evaluate(
        btc_price=100_250.0, strike=100_000.0, btc_5m_move=20.0,
        dist_threshold_pct=0.0020,
    )
    assert sig is not None


def test_tighter_momentum_threshold():
    """$30 momentum threshold blocks a $20 signal."""
    sig = evaluate(
        btc_price=100_500.0, strike=100_000.0, btc_5m_move=20.0,
        momentum_threshold=30.0,
    )
    assert sig is None


# ─── compute_contracts() ─────────────────────────────────────────────────


def test_contracts_returns_flat_when_affordable():
    """Standard $50 BR + 50c entry: flat 5 contracts costs $2.50, well within 1.5× rule."""
    assert compute_contracts(5000, 50) == 5


def test_contracts_zero_balance():
    assert compute_contracts(0, 50) == 0
    assert compute_contracts(-100, 50) == 0


def test_contracts_zero_entry():
    assert compute_contracts(5000, 0) == 0


def test_contracts_too_small_bankroll():
    """If 1.5× cost > bankroll, refuse."""
    # 5 × 50c = 250c. 1.5× = 375c. Bankroll 300c → refuse.
    assert compute_contracts(300, 50) == 0


def test_contracts_just_at_bankroll_floor():
    """Exactly 1.5× cost: allowed (passes)."""
    # 5 × 50c = 250c → 1.5× = 375c. Bankroll 375c → pass.
    assert compute_contracts(375, 50) == 5


def test_contracts_custom_flat_size():
    """Override flat contracts to 10."""
    # 10 × 30c = 300c. 1.5× = 450c. Bankroll 500c → pass.
    assert compute_contracts(500, 30, flat_contracts=10) == 10


# ─── daily_loss_halted() ─────────────────────────────────────────────────


def test_no_halt_when_balanced():
    assert daily_loss_halted(5000, 5000) is False


def test_no_halt_when_up():
    assert daily_loss_halted(6000, 5000) is False


def test_halt_at_exact_threshold():
    """20% drawdown → halt fires at < 80% (strict less-than)."""
    # 5000 × 0.80 = 4000. balance 4000 (==) → no halt
    assert daily_loss_halted(4000, 5000) is False
    # balance 3999 (just below) → halt
    assert daily_loss_halted(3999, 5000) is True


def test_halt_zero_or_negative_inputs():
    assert daily_loss_halted(5000, 0) is False  # day-start unknown
    assert daily_loss_halted(5000, -100) is False
