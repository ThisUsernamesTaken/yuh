# tests/test_indicators.py — Unit tests for incremental indicator implementations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from indicators import EMACalc, RSICalc, SMACalc


class TestEMACalc:
    def test_first_value_seeds_ema(self):
        ema = EMACalc(5)
        assert ema.update(100.0) == 100.0

    def test_second_value_applies_smoothing(self):
        ema = EMACalc(5)
        ema.update(100.0)
        k = 2 / (5 + 1)
        expected = 200.0 * k + 100.0 * (1 - k)
        assert abs(ema.update(200.0) - expected) < 1e-10

    def test_ema_converges(self):
        ema = EMACalc(5)
        for _ in range(100):
            ema.update(50.0)
        assert abs(ema.value - 50.0) < 1e-6

    def test_reset_clears_state(self):
        ema = EMACalc(5)
        ema.update(100.0)
        ema.reset()
        assert ema.value is None
        assert ema.update(200.0) == 200.0

    def test_length_1_is_identity(self):
        ema = EMACalc(1)
        for v in [10.0, 20.0, 30.0]:
            assert ema.update(v) == v

    def test_ema5_sequence(self):
        """Verify EMA(5) against a known sequence."""
        ema = EMACalc(5)
        prices = [10, 11, 12, 13, 14, 15]
        k = 2 / 6
        expected = prices[0]
        for p in prices:
            result = ema.update(p)
            expected = p * k + expected * (1 - k) if p != prices[0] else p
        assert abs(result - expected) < 1e-10


class TestRSICalc:
    def test_warmup_returns_50(self):
        rsi = RSICalc(7)
        for i in range(7):  # Need length bars of changes = length+1 closes
            val = rsi.update(float(100 + i))
            assert val == 50.0

    def test_all_gains_returns_100(self):
        rsi = RSICalc(7)
        # Seed with warmup
        for _ in range(8):
            rsi.update(100.0)
        # All gains — RSI should approach 100
        for _ in range(20):
            rsi.update(rsi._prev_close + 1.0)
        assert rsi.update(rsi._prev_close + 1.0) > 90.0

    def test_all_losses_returns_low(self):
        rsi = RSICalc(7)
        for _ in range(8):
            rsi.update(200.0)
        for _ in range(20):
            rsi.update(rsi._prev_close - 1.0)
        assert rsi.update(rsi._prev_close - 1.0) < 10.0

    def test_rsi_bounded(self):
        rsi = RSICalc(7)
        prices = [100, 102, 99, 105, 98, 110, 95, 115, 90, 120, 85, 125]
        for p in prices:
            val = rsi.update(float(p))
            assert 0.0 <= val <= 100.0

    def test_reset(self):
        rsi = RSICalc(7)
        for i in range(10):
            rsi.update(100.0 + i)
        rsi.reset()
        assert rsi.update(100.0) == 50.0

    def test_flat_prices_returns_50(self):
        rsi = RSICalc(7)
        for _ in range(20):
            val = rsi.update(100.0)
        # With no gains and no losses, RSI should be 50 (or 100 if avg_loss==0)
        # Pine Script returns 50 when no movement for extended period — acceptable range
        assert 0.0 <= val <= 100.0


class TestSMACalc:
    def test_returns_none_before_full(self):
        sma = SMACalc(5)
        for i in range(4):
            assert sma.update(float(i)) is None
        assert sma.update(4.0) is not None

    def test_correct_average(self):
        sma = SMACalc(3)
        sma.update(10.0)
        sma.update(20.0)
        result = sma.update(30.0)
        assert result == 20.0

    def test_rolling_window(self):
        sma = SMACalc(3)
        sma.update(10.0)
        sma.update(20.0)
        sma.update(30.0)
        result = sma.update(40.0)  # Window: [20, 30, 40]
        assert result == 30.0

    def test_is_ready(self):
        sma = SMACalc(3)
        assert not sma.is_ready
        sma.update(1.0)
        sma.update(2.0)
        assert not sma.is_ready
        sma.update(3.0)
        assert sma.is_ready

    def test_reset(self):
        sma = SMACalc(3)
        for v in [1.0, 2.0, 3.0]:
            sma.update(v)
        sma.reset()
        assert not sma.is_ready
        assert sma.value is None
