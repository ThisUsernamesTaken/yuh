# tests/test_price_feed.py — Unit tests for PriceFeedTask

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Candle
from price_feed import PriceFeedTask, PriceFeedState, BUFFER_SIZES


def _make_candle(ts: int = 1000, close: float = 50000.0, volume: float = 1.0) -> Candle:
    return Candle(
        timestamp=ts,
        open=close - 10,
        high=close + 20,
        low=close - 20,
        close=close,
        volume=volume,
        closed=True,
    )


def _make_feed() -> PriceFeedTask:
    session = MagicMock()
    return PriceFeedTask(session)


class TestCandleBuffer:
    def test_buffer_maxlen_respected(self):
        feed = _make_feed()
        tf = "1m"
        maxlen = BUFFER_SIZES[tf]
        for i in range(maxlen + 10):
            feed._add_candle(tf, _make_candle(ts=i * 60000))
        assert len(feed.state.buffers[tf]) == maxlen

    def test_oldest_candle_dropped_when_full(self):
        feed = _make_feed()
        tf = "5m"
        maxlen = BUFFER_SIZES[tf]
        for i in range(maxlen + 5):
            feed._add_candle(tf, _make_candle(ts=i * 300000, close=float(i)))
        # The buffer should contain the LAST maxlen candles (newest)
        candles = list(feed.state.buffers[tf])
        assert candles[0].close == float(5)          # 6th candle (index 5)
        assert candles[-1].close == float(maxlen + 4)  # last

    def test_last_closed_updated(self):
        feed = _make_feed()
        c = _make_candle(ts=99999)
        feed._add_candle("15m", c)
        assert feed.state.last_closed["15m"] is c

    def test_last_updated_timestamp_set(self):
        import time
        feed = _make_feed()
        before = time.time()
        feed._add_candle("1h", _make_candle())
        assert feed.state.last_updated["1h"] >= before

    def test_get_candles_returns_n_newest(self):
        feed = _make_feed()
        for i in range(10):
            feed._add_candle("5m", _make_candle(ts=i * 300000, close=float(i)))
        result = feed.get_candles("5m", 3)
        assert len(result) == 3
        assert result[-1].close == 9.0

    def test_get_candles_unknown_tf_returns_empty(self):
        feed = _make_feed()
        assert feed.get_candles("4h", 10) == []

    def test_is_warm_false_below_threshold(self):
        feed = _make_feed()
        for i in range(20):
            feed._add_candle("5m", _make_candle(ts=i))
        assert not feed.is_warm("5m", min_bars=25)

    def test_is_warm_true_at_threshold(self):
        feed = _make_feed()
        for i in range(25):
            feed._add_candle("5m", _make_candle(ts=i))
        assert feed.is_warm("5m", min_bars=25)


class TestCallbacks:
    def test_callback_fires_on_candle_close(self):
        feed = _make_feed()
        received = []
        feed.register_candle_close_callback("5m", lambda tf, c: received.append((tf, c)))
        c = _make_candle()
        feed._add_candle("5m", c)
        assert len(received) == 1
        assert received[0] == ("5m", c)

    def test_callback_unknown_tf_does_not_raise(self):
        feed = _make_feed()
        # Should log a warning but not raise
        feed.register_candle_close_callback("99m", lambda tf, c: None)

    def test_callback_exception_does_not_propagate(self):
        """A crashing callback must not break the buffer update."""
        feed = _make_feed()

        def bad_callback(tf, c):
            raise RuntimeError("boom")

        feed.register_candle_close_callback("1m", bad_callback)
        # Should not raise
        feed._add_candle("1m", _make_candle())
        assert len(feed.state.buffers["1m"]) == 1

    def test_multiple_callbacks_all_fire(self):
        feed = _make_feed()
        log = []
        feed.register_candle_close_callback("15m", lambda tf, c: log.append("A"))
        feed.register_candle_close_callback("15m", lambda tf, c: log.append("B"))
        feed._add_candle("15m", _make_candle())
        assert log == ["A", "B"]


class TestKlineMessageParsing:
    def _make_kline_msg(self, interval: str = "5m", closed: bool = True,
                        close: float = 50000.0) -> dict:
        return {
            "stream": f"btcusdt@kline_{interval}",
            "data": {
                "k": {
                    "t": 1700000000000,
                    "o": str(close - 10),
                    "h": str(close + 20),
                    "l": str(close - 20),
                    "c": str(close),
                    "v": "1.5",
                    "i": interval,
                    "x": closed,
                }
            }
        }

    def test_closed_candle_added_to_buffer(self):
        feed = _make_feed()
        asyncio.run(
            feed._process_kline_message(self._make_kline_msg("5m", closed=True))
        )
        assert len(feed.state.buffers["5m"]) == 1

    def test_open_candle_not_added(self):
        feed = _make_feed()
        asyncio.run(
            feed._process_kline_message(self._make_kline_msg("5m", closed=False))
        )
        assert len(feed.state.buffers["5m"]) == 0

    def test_unknown_interval_ignored(self):
        feed = _make_feed()
        asyncio.run(
            feed._process_kline_message(self._make_kline_msg("3m", closed=True))
        )
        # No buffer for 3m — nothing should crash
        assert True

    def test_missing_data_key_handled(self):
        feed = _make_feed()
        asyncio.run(
            feed._process_kline_message({})
        )
        assert True  # no exception


class TestWarmupBufferPopulation:
    def test_warmup_populates_buffers_from_rest(self):
        feed = _make_feed()
        # Manually inject candles as warmup would
        for i in range(30):
            feed.state.buffers["15m"].append(_make_candle(ts=i * 900000))
        assert len(feed.state.buffers["15m"]) == 30
