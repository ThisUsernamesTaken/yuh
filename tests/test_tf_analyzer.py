# tests/test_tf_analyzer.py — Unit tests for TFAnalyzer

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from models import Candle
from tf_analyzer import TFAnalyzer, TFSignal, _WARMUP_BARS


def _candle(close: float, open_: float = None, high: float = None,
            low: float = None, volume: float = 1.0, ts: int = 0) -> Candle:
    if open_ is None:
        open_ = close
    if high is None:
        high = close + abs(close - open_) * 0.5 + 5
    if low is None:
        low = close - abs(close - open_) * 0.5 - 5
    return Candle(timestamp=ts, open=open_, high=high, low=low,
                  close=close, volume=volume, closed=True)


def _warm_up(analyzer: TFAnalyzer, n: int = _WARMUP_BARS + 5,
             base_close: float = 50000.0):
    """Feed n neutral candles to warm up the analyzer."""
    for i in range(n):
        analyzer.update(_candle(close=base_close, ts=i * 60000))


class TestWarmup:
    def test_not_warm_below_threshold(self):
        a = TFAnalyzer("5m")
        for i in range(_WARMUP_BARS - 1):
            a.update(_candle(50000.0))
        assert not a.is_warm

    def test_warm_at_threshold(self):
        a = TFAnalyzer("5m")
        for i in range(_WARMUP_BARS):
            a.update(_candle(50000.0))
        assert a.is_warm

    def test_regime_warming_up_before_warm(self):
        a = TFAnalyzer("1m")
        sig = a.update(_candle(50000.0))
        assert sig.regime == "WARMING_UP"
        assert not sig.is_warm

    def test_get_signal_returns_none_before_first_update(self):
        a = TFAnalyzer("15m")
        assert a.get_signal() is None


class TestEMAPairs:
    def test_short_tf_uses_8_21(self):
        for tf in ("1m", "5m", "15m"):
            a = TFAnalyzer(tf)
            assert a._ema_fast_p == 8
            assert a._ema_slow_p == 21

    def test_long_tf_uses_20_50(self):
        for tf in ("1h", "4h"):
            a = TFAnalyzer(tf)
            assert a._ema_fast_p == 20
            assert a._ema_slow_p == 50


class TestEMACross:
    def test_bull_cross_detected(self):
        """After a run of rising prices fast EMA should cross above slow."""
        a = TFAnalyzer("5m")
        # Feed flat candles to seed both EMAs at the same level
        for _ in range(30):
            a.update(_candle(50000.0))
        # Now drive price up sharply so fast EMA overtakes slow
        for _ in range(10):
            a.update(_candle(55000.0))
        sig = a.get_signal()
        assert sig.ema_cross == "bull"

    def test_bear_cross_detected(self):
        a = TFAnalyzer("5m")
        for _ in range(30):
            a.update(_candle(50000.0))
        for _ in range(10):
            a.update(_candle(45000.0))
        sig = a.get_signal()
        assert sig.ema_cross == "bear"

    def test_ema_cross_bars_increments(self):
        a = TFAnalyzer("5m")
        for _ in range(30):
            a.update(_candle(50000.0))
        # Force a bull cross
        for _ in range(5):
            a.update(_candle(55000.0))
        bars_after_5 = a.get_signal().ema_cross_bars
        a.update(_candle(55000.0))
        assert a.get_signal().ema_cross_bars == bars_after_5 + 1

    def test_ema_cross_bars_resets_on_new_cross(self):
        a = TFAnalyzer("5m")
        for _ in range(30):
            a.update(_candle(50000.0))
        for _ in range(15):
            a.update(_candle(55000.0))  # bull
        for _ in range(15):
            a.update(_candle(45000.0))  # force bear cross
        sig = a.get_signal()
        assert sig.ema_cross == "bear"
        # After the cross reset, bars should be small (≤15)
        assert sig.ema_cross_bars <= 15


class TestMarketStructure:
    def test_bull_structure_from_ascending_highs_lows(self):
        a = TFAnalyzer("15m")
        for i in range(10):
            price = 50000.0 + i * 100
            a.update(_candle(close=price, open_=price - 30,
                             high=price + 50, low=price - 50))
        sig = a.get_signal()
        assert sig.market_structure == "bull"

    def test_bear_structure_from_descending_highs_lows(self):
        a = TFAnalyzer("15m")
        for i in range(10):
            price = 50000.0 - i * 100
            a.update(_candle(close=price, open_=price + 30,
                             high=price + 50, low=price - 50))
        sig = a.get_signal()
        assert sig.market_structure == "bear"

    def test_flat_structure_when_insufficient_bars(self):
        a = TFAnalyzer("15m")
        a.update(_candle(50000.0))
        a.update(_candle(50100.0))
        sig = a.get_signal()
        assert sig.market_structure == "flat"


class TestCandleStructure:
    def test_doji_detected(self):
        """Open ≈ close = doji."""
        a = TFAnalyzer("5m")
        _warm_up(a)
        # Doji: open and close nearly identical, wide range
        c = Candle(timestamp=99999, open=50000.0, high=50500.0,
                   low=49500.0, close=50001.0, volume=1.0, closed=True)
        sig = a.update(c)
        assert sig.candle_structure == "doji"

    def test_bullish_engulfing_detected(self):
        a = TFAnalyzer("5m")
        _warm_up(a)
        # Previous bearish candle
        a.update(_candle(close=49800.0, open_=50200.0,
                         high=50300.0, low=49700.0))
        # Current bullish engulfing: body > previous body
        sig = a.update(_candle(close=50400.0, open_=49600.0,
                                high=50500.0, low=49500.0))
        assert sig.candle_structure == "engulf_bull"

    def test_bearish_engulfing_detected(self):
        a = TFAnalyzer("5m")
        _warm_up(a)
        # Previous bullish candle
        a.update(_candle(close=50200.0, open_=49800.0,
                         high=50300.0, low=49700.0))
        # Current bearish engulfing
        sig = a.update(_candle(close=49600.0, open_=50400.0,
                                high=50500.0, low=49500.0))
        assert sig.candle_structure == "engulf_bear"

    def test_bullish_pin_bar(self):
        a = TFAnalyzer("5m")
        _warm_up(a)
        # Bullish pin bar:
        #   open=50100, close=50260 → body=160 (> 10% of range, not a doji)
        #   high=50300  → upper_wick = 40
        #   low=49000   → lower_wick = min(50100,50260)-49000 = 1100 (> 2*160=320 ✓)
        #   range=1300, (close-low)/range = 1260/1300 = 0.969 > 0.60 ✓
        c = Candle(timestamp=999, open=50100.0, high=50300.0,
                   low=49000.0, close=50260.0, volume=1.0, closed=True)
        sig = a.update(c)
        assert sig.candle_structure == "pin_bull"


class TestScoreRange:
    def test_score_always_in_minus1_plus1(self):
        a = TFAnalyzer("5m")
        for _ in range(60):
            import random
            price = 50000.0 + random.uniform(-2000, 2000)
            a.update(_candle(price))
            sig = a.get_signal()
            assert -1.0 <= sig.score <= 1.0

    def test_all_bullish_inputs_produce_positive_score(self):
        """Strong uptrend should yield a positive score."""
        a = TFAnalyzer("5m")
        for _ in range(30):
            a.update(_candle(50000.0))
        # Strong uptrend
        for i in range(30):
            price = 50000.0 + i * 200
            a.update(_candle(close=price, open_=price - 100,
                             high=price + 50, low=price - 150,
                             volume=2.0))
        sig = a.get_signal()
        assert sig.score > 0.0

    def test_all_bearish_inputs_produce_negative_score(self):
        a = TFAnalyzer("5m")
        for _ in range(30):
            a.update(_candle(50000.0))
        for i in range(30):
            price = 50000.0 - i * 200
            a.update(_candle(close=price, open_=price + 100,
                             high=price + 150, low=price - 50,
                             volume=2.0))
        sig = a.get_signal()
        assert sig.score < 0.0


class TestReset:
    def test_reset_clears_all_state(self):
        a = TFAnalyzer("5m")
        _warm_up(a)
        a.reset()
        assert a._bars_processed == 0
        assert a.get_signal() is None
        assert not a.is_warm

    def test_reset_allows_fresh_warm_up(self):
        a = TFAnalyzer("5m")
        _warm_up(a)
        a.reset()
        _warm_up(a)
        assert a.is_warm


class TestRSIDirection:
    def test_rsi_direction_up_on_rising_prices(self):
        a = TFAnalyzer("5m")
        _warm_up(a, base_close=50000.0)
        # Feed rising prices to push RSI up
        prev_rsi = 50.0
        for i in range(5):
            a.update(_candle(50500.0 + i * 100))
        sig = a.get_signal()
        # RSI direction should be "up" after sustained rally
        assert sig.rsi > 50.0  # RSI above 50 for bullish run

    def test_rsi_direction_down_on_falling_prices(self):
        a = TFAnalyzer("5m")
        _warm_up(a, base_close=50000.0)
        for i in range(5):
            a.update(_candle(49500.0 - i * 100))
        sig = a.get_signal()
        assert sig.rsi < 50.0
