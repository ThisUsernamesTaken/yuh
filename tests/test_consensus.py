# tests/test_consensus.py — Unit tests for the ConsensusLayer

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from consensus import ConsensusLayer
from models import BiasResult


def make_result(tf: str, bull: float, bear: float) -> BiasResult:
    return BiasResult(
        timeframe=tf,
        timestamp=0,
        cycle_return_pct=0.0,
        ema_spread_pct=0.0,
        rsi_bias=0.0,
        candle_pressure=0.0,
        rel_vol_clamped=1.0,
        raw_score=0.0,
        score=bull - bear,
        bull_conf=bull,
        bear_conf=bear,
    )


class TestConsensusLayer:
    def test_all_bullish_gives_call(self):
        layer = ConsensusLayer()
        results = {
            "1m":  make_result("1m",  80.0, 0.0),
            "3m":  make_result("3m",  80.0, 0.0),
            "5m":  make_result("5m",  80.0, 0.0),
            "15m": make_result("15m", 80.0, 0.0),
            "1h":  make_result("1h",  80.0, 0.0),
        }
        sig = layer.compute(results, 0)
        assert sig.direction == "CALL"
        assert sig.confidence > 0
        assert sig.aligned_count == 5

    def test_all_bearish_gives_put(self):
        layer = ConsensusLayer()
        results = {
            "1m":  make_result("1m",  0.0, 80.0),
            "3m":  make_result("3m",  0.0, 80.0),
            "5m":  make_result("5m",  0.0, 80.0),
            "15m": make_result("15m", 0.0, 80.0),
            "1h":  make_result("1h",  0.0, 80.0),
        }
        sig = layer.compute(results, 0)
        assert sig.direction == "PUT"
        assert sig.aligned_count == 5

    def test_empty_results_gives_none(self):
        layer = ConsensusLayer()
        sig = layer.compute({}, 0)
        assert sig.direction == "NONE"
        assert sig.confidence == 0.0

    def test_mixed_signals_partial_alignment(self):
        layer = ConsensusLayer()
        results = {
            "1m":  make_result("1m",  0.0, 60.0),   # bearish
            "3m":  make_result("3m",  0.0, 60.0),   # bearish
            "5m":  make_result("5m",  70.0, 0.0),   # bullish
            "15m": make_result("15m", 70.0, 0.0),   # bullish — highest weight
            "1h":  make_result("1h",  70.0, 0.0),   # bullish
        }
        sig = layer.compute(results, 0)
        # 15m (0.30) and 1h (0.25) and 5m (0.20) are bullish → CALL should win
        assert sig.direction == "CALL"
        assert sig.aligned_count == 3

    def test_confidence_clamped_to_100(self):
        layer = ConsensusLayer()
        results = {
            "1m":  make_result("1m",  100.0, 0.0),
            "3m":  make_result("3m",  100.0, 0.0),
            "5m":  make_result("5m",  100.0, 0.0),
            "15m": make_result("15m", 100.0, 0.0),
            "1h":  make_result("1h",  100.0, 0.0),
        }
        sig = layer.compute(results, 0)
        assert sig.confidence <= 100.0

    def test_weighted_average_math(self):
        """Verify the Fourier weighted average computation."""
        weights = {"1m": 0.5, "3m": 0.5}
        layer = ConsensusLayer(weights=weights)
        results = {
            "1m": make_result("1m", 60.0, 0.0),   # signed = +60
            "3m": make_result("3m", 20.0, 0.0),   # signed = +20
        }
        sig = layer.compute(results, 0)
        expected_fourier = (60.0 * 0.5 + 20.0 * 0.5) / 1.0  # = 40
        assert abs(sig.fourier_score - expected_fourier) < 1e-10

    def test_single_tf_alignment_ratio(self):
        layer = ConsensusLayer(weights={"15m": 1.0})
        results = {"15m": make_result("15m", 50.0, 0.0)}
        sig = layer.compute(results, 0)
        assert sig.alignment_ratio == 1.0

    def test_bucket_assignment(self):
        layer = ConsensusLayer(weights={"15m": 1.0})

        for bull, expected_bucket in [(10.0, 0), (30.0, 1), (60.0, 2), (80.0, 3)]:
            results = {"15m": make_result("15m", bull, 0.0)}
            sig = layer.compute(results, 0)
            assert sig.bucket == expected_bucket, f"Expected bucket {expected_bucket} for conf {sig.confidence}"
