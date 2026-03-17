# tests/test_bias_engine.py — Unit tests for the BiasEngine

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from bias_engine import BiasEngine
from models import Candle


def make_candle(ts_ms: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> Candle:
    return Candle(timestamp=ts_ms, open=o, high=h, low=l, close=c, volume=v)


# 15m cycle in milliseconds
CYCLE_MS = 15 * 60 * 1000
# 1m bar in milliseconds
BAR_MS = 60 * 1000


class TestBiasEngine:
    def test_instantiation(self):
        eng = BiasEngine("15m")
        assert eng.timeframe == "15m"

    def test_first_candle_no_decided_signal(self):
        eng = BiasEngine("15m")
        c = make_candle(0, 100, 101, 99, 100.5)
        result = eng.update(c)
        assert result.bars_in_cycle == 1
        assert not result.decided_call
        assert not result.decided_put

    def test_bars_in_cycle_increments(self):
        eng = BiasEngine("15m")
        ts = 0
        for i in range(5):
            c = make_candle(ts + i * BAR_MS, 100, 101, 99, 100.5)
            result = eng.update(c)
        assert result.bars_in_cycle == 5

    def test_new_cycle_resets_bars(self):
        eng = BiasEngine("15m")
        # Fill first cycle
        for i in range(15):
            c = make_candle(i * BAR_MS, 100, 101, 99, 100.5)
            eng.update(c)
        # Start second cycle
        c = make_candle(CYCLE_MS, 100, 101, 99, 100.5)
        result = eng.update(c)
        assert result.bars_in_cycle == 1

    def test_decision_only_after_decision_start_bar(self):
        """Signals should not be decided before DECISION_START_BAR (default=3)."""
        eng = BiasEngine("15m")
        # First 2 bars — strong up move
        for i in range(2):
            c = make_candle(i * BAR_MS, 100, 115, 99, 115.0, v=1000.0)
            result = eng.update(c)
        assert not result.decided_call, "Should not decide before bar 3"

    def test_bull_conf_is_non_negative(self):
        eng = BiasEngine("15m")
        for i in range(20):
            c = make_candle(i * BAR_MS, 100 + i, 101 + i, 99 + i, 100.5 + i)
            result = eng.update(c)
        assert result.bull_conf >= 0.0
        assert result.bear_conf >= 0.0

    def test_conf_capped_at_100(self):
        eng = BiasEngine("15m")
        for i in range(50):
            c = make_candle(i * BAR_MS, 100, 200, 99, 200.0, v=10000.0)
            result = eng.update(c)
        assert result.bull_conf <= 100.0

    def test_candle_pressure_flat_candle(self):
        """When high == low, candle_pressure should be 0."""
        eng = BiasEngine("15m")
        c = make_candle(0, 100.0, 100.0, 100.0, 100.0)
        result = eng.update(c)
        assert result.candle_pressure == 0.0

    def test_reset_clears_cycle(self):
        eng = BiasEngine("15m")
        for i in range(5):
            eng.update(make_candle(i * BAR_MS, 100, 101, 99, 100.5))
        eng.reset()
        c = make_candle(0, 200, 201, 199, 200.5)
        result = eng.update(c)
        assert result.bars_in_cycle == 1
        assert result.decided_conf == 0.0

    def test_decided_conf_monotonically_increases_within_cycle(self):
        """Within a single cycle, decided_conf should never decrease."""
        eng = BiasEngine("15m")
        prev_conf = 0.0
        for i in range(15):
            c = make_candle(i * BAR_MS, 100, 115, 99, 115.0, v=500.0)
            result = eng.update(c)
            assert result.decided_conf >= prev_conf - 1e-10
            prev_conf = result.decided_conf

    def test_cycle_return_pct_resets_on_new_cycle(self):
        eng = BiasEngine("15m")
        # First cycle — price goes up
        for i in range(5):
            c = make_candle(i * BAR_MS, 100, 110, 99, 110.0)
            result = eng.update(c)
        assert result.cycle_return_pct > 0

        # New cycle starts at a lower price
        c_new = make_candle(CYCLE_MS, 90, 91, 89, 90.5)
        result_new = eng.update(c_new)
        # cycle_return_pct should be based on the new cycle's open
        assert abs(result_new.cycle_return_pct) < 5.0  # Should be small since it's the first bar
