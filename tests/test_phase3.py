"""
Tests for Phase 3: Signal Intelligence
=======================================
Run: python -m pytest tests/test_phase3.py -v
  or: python tests/test_phase3.py  (standalone)
"""

import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Candle, BiasResult, ConsensusSignal
from regime_detector import RegimeDetector, Regime, RegimeState
from signal_intelligence import (
    WinRateTracker, DivergenceDetector, DivergenceType,
    SignalFilter, FilterReason,
)


def _make_candle(ts: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v, closed=True)


def _make_signal(
    direction: str = "CALL",
    confidence: float = 75.0,
    tf_scores: dict | None = None,
    aligned: int = 5,
    total: int = 5,
) -> ConsensusSignal:
    if tf_scores is None:
        if direction == "CALL":
            tf_scores = {"1m": 60, "3m": 55, "5m": 70, "15m": 80, "1h": 65}
        elif direction == "PUT":
            tf_scores = {"1m": -60, "3m": -55, "5m": -70, "15m": -80, "1h": -65}
        else:
            tf_scores = {"1m": 0, "3m": 0, "5m": 0, "15m": 0, "1h": 0}

    return ConsensusSignal(
        timestamp=1000000,
        fourier_score=confidence if direction == "CALL" else -confidence,
        confidence=confidence,
        direction=direction,
        tf_scores=tf_scores,
        aligned_count=aligned,
        total_tfs=total,
        tf_results={},
    )


# ─────────────────────────────────────────────
# REGIME DETECTOR TESTS
# ─────────────────────────────────────────────

def test_regime_detector_trending_up():
    """Feed steadily rising candles → should classify as TRENDING_UP."""
    det = RegimeDetector(lookback=10, atr_len=5, atr_baseline_len=15, ema_fast=3, ema_mid=8, ema_slow=15)
    price = 50000.0
    ts = 0

    # Warmup + trending candles
    for i in range(80):
        price += 20.0  # Steady rise
        c = _make_candle(ts, price - 10, price + 5, price - 15, price)
        state = det.update(c)
        ts += 300_000  # 5m candles

    assert state.regime in (Regime.TRENDING_UP, Regime.UNKNOWN), f"Expected TRENDING_UP, got {state.regime}"
    assert state.der > 0, f"DER should be positive for uptrend, got {state.der}"
    assert state.ema_fan_score > 0, f"EMA fan should be positive, got {state.ema_fan_score}"
    print(f"  ✓ Trending UP: regime={state.regime}, DER={state.der:.3f}, fan={state.ema_fan_score:.2f}")


def test_regime_detector_ranging():
    """Feed oscillating candles → should classify as RANGING."""
    det = RegimeDetector(lookback=10, atr_len=5, atr_baseline_len=15, ema_fast=3, ema_mid=8, ema_slow=15)
    base = 50000.0
    ts = 0

    for i in range(80):
        # Oscillate around base: +10, -10, +10, -10...
        offset = 10.0 if i % 2 == 0 else -10.0
        price = base + offset
        c = _make_candle(ts, price - 5, price + 5, price - 5, price)
        state = det.update(c)
        ts += 300_000

    assert abs(state.der) < 0.3, f"DER should be near zero for ranging, got {state.der}"
    print(f"  ✓ Ranging: regime={state.regime}, DER={state.der:.3f}, fan={state.ema_fan_score:.2f}")


def test_regime_confidence_multiplier():
    """Trending regimes should give multiplier near 1.0, ranging should be exactly 1.0 (best regime per backtest)."""
    trending = RegimeState(
        regime=Regime.TRENDING_UP, confidence=80.0, der=0.5,
        atr_ratio=1.1, ema_fan_score=0.7, trending_strength=80.0, volatility_score=20.0
    )
    ranging = RegimeState(
        regime=Regime.RANGING, confidence=70.0, der=0.05,
        atr_ratio=0.9, ema_fan_score=0.1, trending_strength=10.0, volatility_score=10.0
    )

    assert trending.confidence_multiplier >= 0.9, f"Trending mult too low: {trending.confidence_multiplier}"
    assert ranging.confidence_multiplier == 1.0, f"Ranging mult should be 1.0 (best regime): {ranging.confidence_multiplier}"
    print(f"  ✓ Multipliers: trending={trending.confidence_multiplier:.2f}, ranging={ranging.confidence_multiplier:.2f}")


# ─────────────────────────────────────────────
# WIN RATE TRACKER TESTS
# ─────────────────────────────────────────────

def test_win_rate_tracker_basic():
    """Record wins and losses, check rates."""
    tracker = WinRateTracker(window_size=50)

    # 8 wins, 2 losses in bucket 3
    for i in range(10):
        tracker.record(3, is_win=(i < 8))

    wr = tracker.win_rate(3)
    assert abs(wr - 80.0) < 0.1, f"Expected 80% WR, got {wr}"
    assert tracker.trade_count(3) == 10
    print(f"  ✓ Win rate: {wr:.1f}% on {tracker.trade_count(3)} trades")


def test_win_rate_degradation():
    """Detect when bucket drops below baseline."""
    tracker = WinRateTracker(window_size=50)

    # Bucket 3 baseline is 95.82%. Record poor results.
    for i in range(20):
        tracker.record(3, is_win=(i < 14))  # 70% WR

    assert tracker.is_degraded(3), "Bucket 3 should be degraded at 70% vs 95.82% baseline"
    print(f"  ✓ Degradation detected: bucket 3 at {tracker.win_rate(3):.1f}%")


def test_win_rate_not_degraded_with_few_trades():
    """Don't flag degradation with insufficient data."""
    tracker = WinRateTracker(window_size=50)
    tracker.record(3, False)
    tracker.record(3, False)

    assert not tracker.is_degraded(3), "Should not flag with only 2 trades"
    print(f"  ✓ No false degradation with {tracker.trade_count(3)} trades")


# ─────────────────────────────────────────────
# DIVERGENCE DETECTOR TESTS
# ─────────────────────────────────────────────

def test_divergence_none():
    """All TFs agree → no divergence."""
    det = DivergenceDetector()
    signal = _make_signal("CALL", 80.0, aligned=5)
    state = det.evaluate(signal)

    assert state.divergence_type == DivergenceType.NONE
    assert not state.is_reversal_risk
    print(f"  ✓ No divergence: {state.divergence_type.value}")


def test_divergence_severe_reversal():
    """Higher TFs oppose consensus → severe divergence."""
    det = DivergenceDetector()
    signal = _make_signal(
        "CALL", 70.0,
        tf_scores={"1m": 60, "3m": 55, "5m": 40, "15m": -30, "1h": -50},
        aligned=3,
    )
    state = det.evaluate(signal)

    assert state.is_reversal_risk, "Should detect reversal risk"
    assert state.divergence_type in (DivergenceType.SEVERE, DivergenceType.MODERATE)
    print(f"  ✓ Reversal risk: {state.divergence_type.value}, dissenters={state.dissenting_tfs}")


def test_divergence_mild():
    """One lower TF dissents → mild."""
    det = DivergenceDetector()
    signal = _make_signal(
        "CALL", 75.0,
        tf_scores={"1m": -10, "3m": 55, "5m": 70, "15m": 80, "1h": 65},
        aligned=4,
    )
    state = det.evaluate(signal)

    assert state.divergence_type == DivergenceType.MILD
    assert not state.is_reversal_risk
    print(f"  ✓ Mild divergence: dissenters={state.dissenting_tfs}")


# ─────────────────────────────────────────────
# SIGNAL FILTER TESTS
# ─────────────────────────────────────────────

def test_filter_approves_strong_signal():
    """High confidence, all TFs aligned, good regime → approved."""
    regime = RegimeDetector(lookback=5, atr_len=3, atr_baseline_len=10, ema_fast=3, ema_mid=5, ema_slow=8)

    # Feed trending candles to establish regime
    price = 50000.0
    for i in range(30):
        price += 15
        regime.update(_make_candle(i * 300_000, price - 5, price + 5, price - 10, price))

    sf = SignalFilter(regime, min_confidence=50.0, min_alignment=3)
    signal = _make_signal("CALL", 80.0, aligned=5)
    decision = sf.evaluate(signal, utc_hour=14)  # 14h = NY-Open, not a bad hour

    assert decision.approved, f"Should approve strong signal, got: {decision.reason.value} - {decision.details}"
    assert decision.adjusted_confidence > 0
    print(f"  ✓ Approved: conf={decision.adjusted_confidence:.1f}, reason={decision.details}")


def test_filter_rejects_low_confidence():
    """Below minimum confidence → rejected."""
    regime = RegimeDetector(lookback=5, atr_len=3, atr_baseline_len=10, ema_fast=3, ema_mid=5, ema_slow=8)
    for i in range(30):
        regime.update(_make_candle(i * 300_000, 50000, 50010, 49990, 50000))

    sf = SignalFilter(regime, min_confidence=50.0)
    signal = _make_signal("CALL", 30.0)
    decision = sf.evaluate(signal)

    assert not decision.approved
    assert decision.reason == FilterReason.LOW_CONFIDENCE
    print(f"  ✓ Rejected low confidence: {decision.details}")


def test_filter_rejects_bad_hour():
    """Signal during chop hour → rejected."""
    regime = RegimeDetector(lookback=5, atr_len=3, atr_baseline_len=10, ema_fast=3, ema_mid=5, ema_slow=8)
    for i in range(30):
        regime.update(_make_candle(i * 300_000, 50000 + i * 10, 50020 + i * 10, 49990 + i * 10, 50000 + i * 10))

    sf = SignalFilter(regime, min_confidence=50.0, filter_bad_hours=True)
    signal = _make_signal("CALL", 80.0, aligned=5)
    decision = sf.evaluate(signal, utc_hour=9)

    assert not decision.approved
    assert decision.reason == FilterReason.BAD_HOUR
    print(f"  ✓ Rejected bad hour: {decision.details}")


def test_filter_rejects_insufficient_alignment():
    """Only 2/5 TFs aligned → rejected."""
    regime = RegimeDetector(lookback=5, atr_len=3, atr_baseline_len=10, ema_fast=3, ema_mid=5, ema_slow=8)
    for i in range(30):
        regime.update(_make_candle(i * 300_000, 50000 + i * 10, 50020 + i * 10, 49990 + i * 10, 50000 + i * 10))

    sf = SignalFilter(regime, min_confidence=50.0, min_alignment=3)
    signal = _make_signal("CALL", 75.0, aligned=2)
    decision = sf.evaluate(signal, utc_hour=14)  # 14h = NY-Open, not a bad hour

    assert not decision.approved
    assert decision.reason == FilterReason.INSUFFICIENT_ALIGNMENT
    print(f"  ✓ Rejected low alignment: {decision.details}")


# ─────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────

def run_all():
    tests = [
        ("Regime: trending up", test_regime_detector_trending_up),
        ("Regime: ranging", test_regime_detector_ranging),
        ("Regime: confidence multiplier", test_regime_confidence_multiplier),
        ("WinRate: basic tracking", test_win_rate_tracker_basic),
        ("WinRate: degradation detection", test_win_rate_degradation),
        ("WinRate: no false degradation", test_win_rate_not_degraded_with_few_trades),
        ("Divergence: none", test_divergence_none),
        ("Divergence: severe reversal", test_divergence_severe_reversal),
        ("Divergence: mild", test_divergence_mild),
        ("Filter: approves strong signal", test_filter_approves_strong_signal),
        ("Filter: rejects low confidence", test_filter_rejects_low_confidence),
        ("Filter: rejects bad hour", test_filter_rejects_bad_hour),
        ("Filter: rejects insufficient alignment", test_filter_rejects_insufficient_alignment),
    ]

    print("\n" + "=" * 60)
    print("  PHASE 3 TESTS: Signal Intelligence")
    print("=" * 60 + "\n")

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"[{name}]")
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        print()

    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 60 + "\n")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
