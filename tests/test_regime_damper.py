"""Unit tests for Phase 2 damper factors in regime.py.

Covers every cell of the regime × drawdown ladder and verifies the
defensive-fallback behavior when a bad enum slips in.
"""
from regime import (
    Regime, DrawdownState,
    REGIME_FACTORS, DRAWDOWN_FACTORS,
    damp_factors,
)


def test_regime_factors_aggressive_ladder():
    assert REGIME_FACTORS[Regime.STRUCTURED] == 1.10
    assert REGIME_FACTORS[Regime.CHOP] == 0.60
    assert REGIME_FACTORS[Regime.CHAOTIC] == 0.25


def test_drawdown_factors_aggressive_ladder():
    assert DRAWDOWN_FACTORS[DrawdownState.FLAT] == 1.00
    assert DRAWDOWN_FACTORS[DrawdownState.SOFT_DD] == 0.55
    assert DRAWDOWN_FACTORS[DrawdownState.HARD_DD] == 0.25


def test_damp_factors_best_case():
    rf, df = damp_factors(Regime.STRUCTURED, DrawdownState.FLAT)
    assert rf == 1.10 and df == 1.00
    assert round(rf * df, 3) == 1.10


def test_damp_factors_worst_case():
    rf, df = damp_factors(Regime.CHAOTIC, DrawdownState.HARD_DD)
    assert rf == 0.25 and df == 0.25
    # 0.0625 — the "basically stop trading" cell
    assert round(rf * df, 4) == 0.0625


def test_damp_factors_chop_flat_is_default_bucket():
    rf, df = damp_factors(Regime.CHOP, DrawdownState.FLAT)
    # CHOP is the default bucket per regime.py. FLAT is the default dd.
    # Product 0.60 — a 40% haircut on a normal trade day.
    assert round(rf * df, 3) == 0.60


def test_damp_factors_every_cell_product():
    """Full 3x3 grid — guards against silent edits."""
    expected = {
        (Regime.STRUCTURED, DrawdownState.FLAT):    1.10,
        (Regime.STRUCTURED, DrawdownState.SOFT_DD): 0.605,
        (Regime.STRUCTURED, DrawdownState.HARD_DD): 0.275,
        (Regime.CHOP,       DrawdownState.FLAT):    0.60,
        (Regime.CHOP,       DrawdownState.SOFT_DD): 0.33,
        (Regime.CHOP,       DrawdownState.HARD_DD): 0.15,
        (Regime.CHAOTIC,    DrawdownState.FLAT):    0.25,
        (Regime.CHAOTIC,    DrawdownState.SOFT_DD): 0.1375,
        (Regime.CHAOTIC,    DrawdownState.HARD_DD): 0.0625,
    }
    for (r, d), product in expected.items():
        rf, df = damp_factors(r, d)
        assert round(rf * df, 4) == round(product, 4), \
            f"{r}×{d} expected {product}, got {rf*df}"


def test_damp_factors_fallback_on_unknown_enum():
    """Defensive: if a bad value sneaks in, fall back to no-damping."""
    rf, df = damp_factors("not-a-regime", "not-a-dd")  # type: ignore
    assert rf == 1.0 and df == 1.0
