"""Unit tests for Phase 3 shadow edge model.

Verifies that each additive component contributes the expected signed
magnitude, that the overall score is the sum of components, and that
pathological inputs (pre-warmup prob engine, missing MTF) degrade
gracefully.
"""
from shadow_edge import compute_shadow_edge, ShadowEdge


def test_degenerate_zero_inputs_returns_zero():
    """Pre-warmup prob engine (model_yes_prob == 0) must not produce
    a false-NO signal of -50 points."""
    edge = compute_shadow_edge(
        model_yes_prob=0.0, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=None, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.score == 0.0
    assert edge.side == ""
    assert edge.confidence == 0.0


def test_neutral_inputs_produce_neutral_score():
    """Model says 50 %, market at 50c, no pressure, no MTF, RSI 50."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=0.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.score == 0.0
    assert edge.side == ""


def test_bb_component_positive():
    """Model 60 %, market 50c → bb component = +10 points."""
    edge = compute_shadow_edge(
        model_yes_prob=0.60, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=0.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["bb"] == 10.0
    assert edge.score == 10.0
    assert edge.side == "yes"


def test_bb_component_clipped_at_plus_20():
    """Model 100 % vs market at 1c must not overwhelm the score."""
    edge = compute_shadow_edge(
        model_yes_prob=1.00, mkt_price_cents=1.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=0.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["bb"] == 20.0


def test_pressure_component():
    """Score +0.5 with confidence 0.8 → pressure = +4.0 points."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.5, pressure_confidence=0.8,
        mtf_score=0.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["pres"] == 4.0


def test_pressure_component_clipped():
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=1.0, pressure_confidence=1.0,   # 10.0 raw
        mtf_score=0.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["pres"] == 10.0


def test_mtf_component_missing():
    """mtf_score=None must not crash and contributes 0."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=None, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["mtf"] == 0.0


def test_mtf_component_positive():
    """MTF +50 → mtf component = +10 points."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=50.0, rsi=50.0, btc_5m_move=0.0,
    )
    assert edge.components["mtf"] == 10.0


def test_rsi_tilt_contributes_small_signal():
    """RSI 70 (bullish end) → tilt ~ +1.2."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=0.0, rsi=70.0, btc_5m_move=0.0,
    )
    assert edge.components["rsi"] == 1.2


def test_btc_5m_clipped():
    """BTC +$100 / 5m → btc component clipped at 5.0 points."""
    edge = compute_shadow_edge(
        model_yes_prob=0.50, mkt_price_cents=50.0,
        pressure_score=0.0, pressure_confidence=0.0,
        mtf_score=0.0, rsi=50.0, btc_5m_move=100.0,
    )
    assert edge.components["btc"] == 5.0


def test_score_is_sum_of_components():
    edge = compute_shadow_edge(
        model_yes_prob=0.60, mkt_price_cents=50.0,      # bb = +10
        pressure_score=0.5, pressure_confidence=0.6,    # pres = +3.0
        mtf_score=20.0,                                 # mtf = +4
        rsi=65.0,                                       # rsi = +0.9
        btc_5m_move=15.0,                               # btc = +2.5
    )
    expected = 10.0 + 3.0 + 4.0 + 0.9 + 2.5
    assert abs(edge.score - expected) < 0.01


def test_strong_no_signal():
    edge = compute_shadow_edge(
        model_yes_prob=0.25, mkt_price_cents=60.0,      # bb = -20 (clipped)
        pressure_score=-0.8, pressure_confidence=0.8,   # pres = -6.4
        mtf_score=-40.0,                                # mtf = -8
        rsi=30.0,                                       # rsi = -1.2
        btc_5m_move=-40.0,                              # btc = -5 (clipped)
    )
    assert edge.side == "no"
    assert edge.score < -30.0
    assert edge.confidence == 1.0   # any |score| >= 25 confidence = 1.0


def test_confidence_clipped_at_one():
    edge = compute_shadow_edge(
        model_yes_prob=1.0, mkt_price_cents=1.0,        # bb = +20
        pressure_score=1.0, pressure_confidence=1.0,    # pres = +10
        mtf_score=100.0,                                # mtf = +20
        rsi=100.0,                                      # rsi = +3
        btc_5m_move=100.0,                              # btc = +5
    )
    assert edge.confidence == 1.0


def test_log_fragment_format():
    edge = compute_shadow_edge(
        model_yes_prob=0.60, mkt_price_cents=50.0,
        pressure_score=0.5, pressure_confidence=0.6,
        mtf_score=10.0, rsi=55.0, btc_5m_move=9.0,
    )
    frag = edge.as_log_fragment()
    # Order and +/- sign formatting are part of the log contract
    assert "bb=" in frag and "pres=" in frag and "mtf=" in frag
    assert "rsi=" in frag and "btc=" in frag
    assert frag.count("=") == 5
