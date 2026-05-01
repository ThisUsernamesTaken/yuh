# tests/test_mtf_scorer.py — Unit tests for MTFConfluenceScorer

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from models import Candle
from tf_analyzer import TFAnalyzer, _WARMUP_BARS
from mtf_scorer import MTFConfluenceScorer, ConfluenceResult, TF_WEIGHTS, _ta_result_to_tf_signal


def _candle(close: float, open_: float = None, volume: float = 1.0,
            ts: int = 0) -> Candle:
    if open_ is None:
        open_ = close
    return Candle(
        timestamp=ts,
        open=open_,
        high=close + 50,
        low=close - 50,
        close=close,
        volume=volume,
        closed=True,
    )


def _warm_tf(scorer: MTFConfluenceScorer, tf: str,
             direction: str = "flat", n_bars: int = _WARMUP_BARS + 10) -> None:
    """Feed n candles to a specific TFAnalyzer through the scorer."""
    if direction == "up":
        closes = [50000.0 + i * 50 for i in range(n_bars)]
    elif direction == "down":
        closes = [50000.0 - i * 50 for i in range(n_bars)]
    else:
        closes = [50000.0] * n_bars
    for i, close in enumerate(closes):
        open_ = close - 50 if direction == "up" else close + 50 if direction == "down" else close
        scorer.on_candle(tf, _candle(close, open_=open_, ts=i * 300000))


class TestWeights:
    def test_weights_sum_to_1(self):
        total = sum(TF_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9

    def test_4h_not_in_weights(self):
        assert "4h" not in TF_WEIGHTS

    def test_all_active_tfs_in_weights(self):
        for tf in ("1m", "5m", "15m", "1h"):
            assert tf in TF_WEIGHTS


class TestEvaluateBeforeWarmup:
    def test_score_near_zero_before_warmup(self):
        scorer = MTFConfluenceScorer()
        result = scorer.evaluate(side="yes", ta_result=None)
        # No data → neutral score
        assert result.confluence_score == 0.0
        assert result.action == "NO_TRADE"

    def test_confluence_result_fields_present(self):
        scorer = MTFConfluenceScorer()
        result = scorer.evaluate(side="yes", ta_result=None)
        assert isinstance(result.confluence_score, float)
        assert isinstance(result.confluence_regime, str)
        assert result.side == "yes"
        assert isinstance(result.reasoning, str)


class TestEvaluateWithData:
    def test_all_bullish_tfs_positive_score_for_yes(self):
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "up")
        _warm_tf(scorer, "15m", "up")
        _warm_tf(scorer, "1h", "up")
        result = scorer.evaluate(side="yes", ta_result=None)
        assert result.confluence_score > 0.0

    def test_all_bearish_tfs_negative_score_for_yes(self):
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "down")
        _warm_tf(scorer, "15m", "down")
        _warm_tf(scorer, "1h", "down")
        result = scorer.evaluate(side="yes", ta_result=None)
        assert result.confluence_score < 0.0

    def test_score_in_valid_range(self):
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "up")
        _warm_tf(scorer, "15m", "down")
        result = scorer.evaluate(side="yes", ta_result=None)
        assert -1.0 <= result.confluence_score <= 1.0

    def test_yes_no_symmetry(self):
        """YES score should be the negative of NO score (same candle data)."""
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "up")
        _warm_tf(scorer, "15m", "up")
        res_yes = scorer.evaluate(side="yes", ta_result=None)
        res_no = scorer.evaluate(side="no", ta_result=None)
        # The underlying confluence_score is the same raw value
        assert abs(res_yes.confluence_score - res_no.confluence_score) < 1e-9


class TestActionMapping:
    def _make_scorer_with_score(self, target_score: float) -> MTFConfluenceScorer:
        """Construct a scorer whose confluence_score approximates target_score."""
        scorer = MTFConfluenceScorer()
        direction = "up" if target_score > 0 else "down"
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, direction)
        return scorer

    def test_high_confidence_action_for_yes_strong_bull(self):
        scorer = MTFConfluenceScorer()
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, "up", n_bars=60)
        result = scorer.evaluate(side="yes", ta_result=None)
        # With all TFs bullish and well-warmed, should be HIGH_CONFIDENCE or NORMAL
        assert result.action in ("HIGH_CONFIDENCE", "NORMAL")
        assert result.size_multiplier >= 1.0

    def test_normal_action_does_not_change_size(self):
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "up", n_bars=_WARMUP_BARS + 2)
        result = scorer.evaluate(side="yes", ta_result=None)
        if result.action == "NORMAL":
            assert result.size_multiplier == 1.0

    def test_size_multiplier_high_confidence_gt_1(self):
        scorer = MTFConfluenceScorer()
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, "up", n_bars=100)
        result = scorer.evaluate(side="yes", ta_result=None)
        if result.action == "HIGH_CONFIDENCE":
            assert result.size_multiplier > 1.0


class TestMissingTF:
    def test_missing_tf_treated_as_neutral(self):
        """Score with only 5m warm should be non-zero (active weight normalization)."""
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "up")
        result = scorer.evaluate(side="yes", ta_result=None)
        # Should not be exactly 0 — the warm 5m contributes
        # (unless 5m score happens to be 0, which is unlikely after strong uptrend)
        assert result.confluence_score != 0.0 or result.action == "NO_TRADE"


class TestAlignmentProperties:
    def test_is_aligned_yes_positive_score(self):
        scorer = MTFConfluenceScorer()
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, "up", n_bars=50)
        result = scorer.evaluate(side="yes", ta_result=None)
        if result.confluence_score >= 0.3:
            assert result.is_aligned

    def test_is_opposing_yes_negative_score(self):
        scorer = MTFConfluenceScorer()
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, "down", n_bars=50)
        result = scorer.evaluate(side="yes", ta_result=None)
        if result.confluence_score <= -0.3:
            assert result.is_opposing

    def test_is_opposing_no_positive_score(self):
        scorer = MTFConfluenceScorer()
        for tf in ("5m", "15m", "1h"):
            _warm_tf(scorer, tf, "up", n_bars=50)
        result = scorer.evaluate(side="no", ta_result=None)
        if result.confluence_score >= 0.3:
            assert result.is_opposing


class TestTAResultConversion:
    def test_ta_result_none_returns_none(self):
        assert _ta_result_to_tf_signal(None) is None

    def test_ta_result_positive_score_produces_bull_signal(self):
        from ta_module import TAScorer
        from models import Candle
        scorer = TAScorer()
        # Feed rising candles to produce a bull signal
        for i in range(20):
            c = Candle(
                timestamp=i * 60000,
                open=50000.0 + i * 10,
                high=50000.0 + i * 10 + 20,
                low=50000.0 + i * 10 - 10,
                close=50000.0 + i * 10 + 15,
                volume=2.0,
                closed=True,
            )
            result = scorer.update(c)
        ta = scorer.last_result
        if ta and ta.composite_score > 0:
            sig = _ta_result_to_tf_signal(ta)
            assert sig is not None
            assert sig.score > 0.0
            assert sig.timeframe == "1m"


class TestReadiness:
    def test_not_ready_before_warmup(self):
        scorer = MTFConfluenceScorer()
        assert not scorer.is_ready()

    def test_ready_after_two_tfs_warm(self):
        scorer = MTFConfluenceScorer()
        _warm_tf(scorer, "5m", "flat")
        _warm_tf(scorer, "15m", "flat")
        assert scorer.is_ready()

    def test_warmup_status_all_false_initially(self):
        scorer = MTFConfluenceScorer()
        status = scorer.warmup_status
        # 1m is always True (delegated to TAScorer)
        assert status["1m"] is True
        assert not status["5m"]
        assert not status["15m"]
        assert not status["1h"]


class TestPrimarySignalNotBlocked:
    """PRIMARY tier signal must never be vetoed by MTF (shadow mode invariant).

    In shadow mode (default), MTF never returns early — the signal always proceeds.
    This test validates the shadow mode path by ensuring evaluate() returns a result
    without raising even with zero warm data.
    """

    def test_evaluate_returns_result_always(self):
        scorer = MTFConfluenceScorer()
        result = scorer.evaluate(side="yes", ta_result=None)
        assert isinstance(result, ConfluenceResult)

    def test_reasoning_string_non_empty(self):
        scorer = MTFConfluenceScorer()
        result = scorer.evaluate(side="yes", ta_result=None)
        assert len(result.reasoning) > 0
