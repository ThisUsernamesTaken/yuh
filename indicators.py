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


class _ROCCalc:
    """Rate of Change over n periods — internal helper for KSTCalc."""

    def __init__(self, period: int) -> None:
        self._period = period
        self._buf: deque[float] = deque(maxlen=period + 1)

    def update(self, price: float) -> Optional[float]:
        self._buf.append(price)
        if len(self._buf) < self._period + 1:
            return None
        old = self._buf[0]
        if old == 0.0:
            return 0.0
        return (price - old) / old * 100.0


class KSTCalc:
    """Know Sure Thing oscillator — multi-timeframe momentum.

    Standard parameters (Pine Script defaults):
      ROC periods: 10, 13, 15, 20
      SMA periods: 10, 13, 15, 20
      Signal SMA : 9
    Weights: 1, 2, 3, 4

    update() returns (kst_value, signal_value).
    is_bullish: kst > signal.
    Returns (0.0, 0.0) during warmup.
    """

    def __init__(self) -> None:
        self._roc1 = _ROCCalc(10)
        self._roc2 = _ROCCalc(13)
        self._roc3 = _ROCCalc(15)
        self._roc4 = _ROCCalc(20)
        self._sma1 = SMACalc(10)
        self._sma2 = SMACalc(13)
        self._sma3 = SMACalc(15)
        self._sma4 = SMACalc(20)
        self._signal_sma = SMACalc(9)
        self._kst: float = 0.0
        self._signal: float = 0.0

    def update(self, close: float) -> tuple[float, float]:
        r1 = self._roc1.update(close)
        r2 = self._roc2.update(close)
        r3 = self._roc3.update(close)
        r4 = self._roc4.update(close)

        rcma1 = self._sma1.update(r1 if r1 is not None else 0.0)
        rcma2 = self._sma2.update(r2 if r2 is not None else 0.0)
        rcma3 = self._sma3.update(r3 if r3 is not None else 0.0)
        rcma4 = self._sma4.update(r4 if r4 is not None else 0.0)

        if rcma1 is None or rcma2 is None or rcma3 is None or rcma4 is None:
            return 0.0, 0.0

        kst = rcma1 * 1 + rcma2 * 2 + rcma3 * 3 + rcma4 * 4
        self._kst = kst
        sig = self._signal_sma.update(kst)
        self._signal = sig if sig is not None else 0.0
        return self._kst, self._signal

    @property
    def is_bullish(self) -> bool:
        return self._kst > self._signal


class ADXCalc:
    """Wilder's Average Directional Index — trend strength (not direction).

    update(high, low, close) returns ADX value (0-100).
    is_trending: ADX > 25
    is_choppy:   ADX < 20
    Returns 0.0 during warmup.
    """

    def __init__(self, period: int = 14) -> None:
        self._period = period
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._prev_close: Optional[float] = None
        # Wilder smoothed values
        self._smooth_tr: Optional[float] = None
        self._smooth_pdm: Optional[float] = None
        self._smooth_ndm: Optional[float] = None
        self._adx: Optional[float] = None
        self._dx_buf: deque[float] = deque(maxlen=period)

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            self._prev_high = high
            self._prev_low = low
            self._prev_close = close
            return 0.0

        # True Range
        tr = max(
            high - low,
            abs(high - self._prev_close),
            abs(low - self._prev_close),
        )

        # Directional movement
        up_move = high - self._prev_high
        down_move = self._prev_low - low
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        ndm = down_move if (down_move > up_move and down_move > 0) else 0.0

        self._prev_high = high
        self._prev_low = low
        self._prev_close = close

        # Wilder smoothing
        k = 1.0 / self._period
        if self._smooth_tr is None:
            self._smooth_tr = tr
            self._smooth_pdm = pdm
            self._smooth_ndm = ndm
        else:
            self._smooth_tr = self._smooth_tr * (1 - k) + tr * k
            self._smooth_pdm = self._smooth_pdm * (1 - k) + pdm * k
            self._smooth_ndm = self._smooth_ndm * (1 - k) + ndm * k

        if self._smooth_tr == 0.0:
            return 0.0

        pdi = 100.0 * self._smooth_pdm / self._smooth_tr
        ndi = 100.0 * self._smooth_ndm / self._smooth_tr
        di_sum = pdi + ndi
        dx = 100.0 * abs(pdi - ndi) / di_sum if di_sum != 0.0 else 0.0

        self._dx_buf.append(dx)
        if len(self._dx_buf) < self._period:
            return 0.0

        if self._adx is None:
            self._adx = sum(self._dx_buf) / self._period
        else:
            self._adx = self._adx * (1 - k) + dx * k

        return self._adx

    @property
    def is_trending(self) -> bool:
        return self._adx is not None and self._adx > 25.0

    @property
    def is_choppy(self) -> bool:
        return self._adx is None or self._adx < 20.0
