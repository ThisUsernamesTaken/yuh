# indicators.py — Incremental indicator implementations (no look-ahead)
# All indicators process one value at a time in chronological order.

from collections import deque
from typing import Optional


class EMACalc:
    """Exponential Moving Average — incremental, matches Pine Script ta.ema()."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.k: float = 2.0 / (length + 1)
        self._ema: Optional[float] = None

    def update(self, price: float) -> float:
        """Feed one price, returns current EMA value."""
        if self._ema is None:
            self._ema = price
        else:
            self._ema = price * self.k + self._ema * (1.0 - self.k)
        return self._ema

    @property
    def value(self) -> Optional[float]:
        return self._ema

    def reset(self) -> None:
        self._ema = None


class RSICalc:
    """Wilder's RSI — incremental, matches Pine Script ta.rsi().

    Returns 50.0 during warmup (< length bars processed).
    """

    def __init__(self, length: int) -> None:
        self.length = length
        self._prev_close: Optional[float] = None
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._warmup_gains: list[float] = []
        self._warmup_losses: list[float] = []
        self._bars: int = 0

    def update(self, close: float) -> float:
        """Feed one close price, returns current RSI value."""
        if self._prev_close is None:
            self._prev_close = close
            self._bars += 1
            return 50.0

        change = close - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._prev_close = close
        self._bars += 1

        if self._avg_gain is None:
            # Still in warmup
            self._warmup_gains.append(gain)
            self._warmup_losses.append(loss)

            if len(self._warmup_gains) >= self.length:
                # First full period — seed with simple average
                self._avg_gain = sum(self._warmup_gains) / self.length
                self._avg_loss = sum(self._warmup_losses) / self.length
                self._warmup_gains = []
                self._warmup_losses = []
            else:
                return 50.0
        else:
            # Wilder's smoothing
            self._avg_gain = (self._avg_gain * (self.length - 1) + gain) / self.length
            self._avg_loss = (self._avg_loss * (self.length - 1) + loss) / self.length

        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @property
    def bars_processed(self) -> int:
        return self._bars

    def reset(self) -> None:
        self._prev_close = None
        self._avg_gain = None
        self._avg_loss = None
        self._warmup_gains = []
        self._warmup_losses = []
        self._bars = 0


class SMACalc:
    """Simple Moving Average — rolling window, incremental."""

    def __init__(self, length: int) -> None:
        self.length = length
        self._buffer: deque[float] = deque(maxlen=length)

    def update(self, value: float) -> Optional[float]:
        """Feed one value, returns SMA or None if buffer not yet full."""
        self._buffer.append(value)
        if len(self._buffer) < self.length:
            return None
        return sum(self._buffer) / self.length

    @property
    def value(self) -> Optional[float]:
        if len(self._buffer) < self.length:
            return None
        return sum(self._buffer) / self.length

    @property
    def is_ready(self) -> bool:
        return len(self._buffer) >= self.length

    def reset(self) -> None:
        self._buffer.clear()
