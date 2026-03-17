# aggregator.py — CandleAggregator: resample 1m candles into higher timeframes

from typing import Optional

from config import TF_MINUTES
from models import Candle


class CandleAggregator:
    """Aggregates 1-minute candles into a higher timeframe.

    Emits a completed higher-TF candle when a new period boundary is crossed.
    The emitted candle reflects the OHLCV of the completed period.
    """

    def __init__(self, timeframe: str) -> None:
        """
        Args:
            timeframe: Target timeframe string, e.g. "3m", "5m", "15m", "1h".
                       Must be a key in TF_MINUTES.
        """
        if timeframe not in TF_MINUTES:
            raise ValueError(f"Unknown timeframe: {timeframe}. Valid: {list(TF_MINUTES)}")
        if timeframe == "1m":
            raise ValueError("CandleAggregator is not needed for 1m — feed 1m candles directly.")

        self.timeframe = timeframe
        self._period_ms: int = TF_MINUTES[timeframe] * 60 * 1000

        # In-progress candle state
        self._period_ts: Optional[int] = None   # Aligned open time of current period
        self._open: float = 0.0
        self._high: float = 0.0
        self._low: float = float("inf")
        self._close: float = 0.0
        self._volume: float = 0.0
        self._bar_count: int = 0

    def _period_for(self, ts_ms: int) -> int:
        """Floor a timestamp to the period boundary."""
        return (ts_ms // self._period_ms) * self._period_ms

    def update(self, candle: Candle) -> Optional[Candle]:
        """Feed a closed 1m candle. Returns a completed higher-TF candle or None.

        A completed candle is returned when a new period starts, representing
        the period that just finished.

        Args:
            candle: A closed 1m candle.

        Returns:
            Completed higher-TF Candle if a period just closed, else None.
        """
        period_ts = self._period_for(candle.timestamp)
        completed: Optional[Candle] = None

        if self._period_ts is None:
            # First candle ever
            self._period_ts = period_ts
            self._open = candle.open
            self._high = candle.high
            self._low = candle.low
            self._close = candle.close
            self._volume = candle.volume
            self._bar_count = 1

        elif period_ts != self._period_ts:
            # New period — emit the completed candle
            completed = Candle(
                timestamp=self._period_ts,
                open=self._open,
                high=self._high,
                low=self._low,
                close=self._close,
                volume=self._volume,
                closed=True,
            )
            # Start new period
            self._period_ts = period_ts
            self._open = candle.open
            self._high = candle.high
            self._low = candle.low
            self._close = candle.close
            self._volume = candle.volume
            self._bar_count = 1

        else:
            # Same period — merge
            self._high = max(self._high, candle.high)
            self._low = min(self._low, candle.low)
            self._close = candle.close
            self._volume += candle.volume
            self._bar_count += 1

        return completed

    def reset(self) -> None:
        self._period_ts = None
        self._open = 0.0
        self._high = 0.0
        self._low = float("inf")
        self._close = 0.0
        self._volume = 0.0
        self._bar_count = 0
