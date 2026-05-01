# tf_analyzer.py — Per-timeframe incremental technical analysis
#
# Consumes closed OHLCV candles one at a time via update().
# Returns a TFSignal with a normalized directional score (-1 to +1)
# and a regime tag string.
#
# Used by MTFConfluenceScorer. One TFAnalyzer instance per timeframe.
#
# Indicators computed:
#   - EMA cross (8/21 for 1m/5m/15m; 20/50 for 1h)
#   - RSI(14) with slope/divergence direction
#   - Bollinger Bands (20, 2σ) — squeeze detection + band position
#   - Volume-weighted momentum
#   - Candle microstructure (engulfing, pin bar, doji)
#   - Market structure (HH/HL vs LH/LL from recent swing points)

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

from indicators import EMACalc, RSICalc, SMACalc
from models import Candle


# ── TFSignal ──────────────────────────────────────────────────────────────────

@dataclass
class TFSignal:
    """Directional signal for a single timeframe.

    score:    -1.0 (strongly bearish) to +1.0 (strongly bullish)
    regime:   human-readable regime string for logging
    is_warm:  False means fewer than WARMUP_BARS processed — treat as unreliable
    """
    timeframe: str
    score: float                 # -1 to +1
    regime: str                  # e.g. "TREND_BULL", "RANGING", "SQUEEZE_BEAR"

    # Component breakdown (for logging / diagnostics)
    ema_cross: str               # "bull", "bear", "flat"
    ema_cross_bars: int          # bars since last cross (freshness)
    rsi: float                   # current RSI value
    rsi_direction: str           # "up", "down", "flat" (RSI slope)
    bb_squeeze: bool             # True when BB width is unusually narrow
    bb_position: float           # 0.0=lower band, 0.5=midline, 1.0=upper band
    candle_structure: str        # "engulf_bull", "engulf_bear", "pin_bull", "pin_bear", "doji", "normal"
    market_structure: str        # "bull" (HH/HL), "bear" (LH/LL), "flat"
    volume_surge: bool           # current bar volume > 1.5x SMA

    is_warm: bool


# ── Component weights (sum to 1.0) ────────────────────────────────────────────
_W_EMA = 0.30         # EMA cross direction + freshness
_W_RSI = 0.25         # RSI level + slope
_W_CANDLE = 0.20      # Candle microstructure (pressure + pattern)
_W_MARKET = 0.15      # Market structure (HH/HL or LH/LL)
_W_VOLUME = 0.10      # Volume-weighted momentum confirmation

# Minimum bars before signal is considered reliable
_WARMUP_BARS = 25

# Bollinger Band squeeze threshold: BBW = (upper-lower)/midline
_BB_SQUEEZE_THRESHOLD = 0.02   # < 2% of price = squeeze

# EMA freshness decay: score fades as cross ages
_EMA_FRESHNESS_MIN = 0.35      # Floor multiplier for very old crosses


# ── TFAnalyzer ────────────────────────────────────────────────────────────────

class TFAnalyzer:
    """Incremental TA scorer for a single timeframe.

    Feed closed candles via update(). Call get_signal() for the latest TFSignal.
    EMA pairs: (8, 21) for short TFs (1m, 5m, 15m); (20, 50) for longer (1h+).
    """

    _SHORT_TFS = ("1m", "5m", "15m")

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe

        # EMA pair selection
        fast_p, slow_p = (8, 21) if timeframe in self._SHORT_TFS else (20, 50)
        self._ema_fast = EMACalc(fast_p)
        self._ema_slow = EMACalc(slow_p)
        self._ema_fast_p = fast_p
        self._ema_slow_p = slow_p

        # RSI(14)
        self._rsi = RSICalc(14)

        # Volume SMA(20) for relative volume
        self._vol_sma = SMACalc(20)

        # Bollinger Bands: SMA(20) + rolling stddev
        self._bb_sma = SMACalc(20)
        self._bb_closes: deque[float] = deque(maxlen=20)

        # Market structure: track last 8 highs and lows
        self._highs: deque[float] = deque(maxlen=8)
        self._lows: deque[float] = deque(maxlen=8)
        self._closes: deque[float] = deque(maxlen=6)   # for engulfing detection

        # EMA cross state
        self._ema_cross_dir: str = "flat"      # "bull" / "bear" / "flat"
        self._ema_cross_bars: int = 0
        self._prev_ema_above: Optional[bool] = None

        # RSI slope
        self._prev_rsi: Optional[float] = None

        # Previous candle (for engulfing / pin bar pattern detection)
        self._prev_candle: Optional[Candle] = None

        # Bar counter
        self._bars_processed: int = 0
        self._last_signal: Optional[TFSignal] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, candle: Candle) -> TFSignal:
        """Feed one closed candle; return updated TFSignal."""
        c = candle
        self._bars_processed += 1

        # ── EMA cross ─────────────────────────────────────────────────────
        ema_fast_val = self._ema_fast.update(c.close)
        ema_slow_val = self._ema_slow.update(c.close)
        fast_above = ema_fast_val > ema_slow_val

        if self._prev_ema_above is None:
            # First bar
            self._prev_ema_above = fast_above
            self._ema_cross_dir = "bull" if fast_above else "bear"
            self._ema_cross_bars = 1
        elif fast_above != self._prev_ema_above:
            # Fresh cross
            self._ema_cross_dir = "bull" if fast_above else "bear"
            self._ema_cross_bars = 1
            self._prev_ema_above = fast_above
        else:
            self._ema_cross_bars += 1

        # ── RSI ───────────────────────────────────────────────────────────
        rsi_val = self._rsi.update(c.close)
        if self._prev_rsi is None:
            rsi_direction = "flat"
        elif rsi_val > self._prev_rsi + 0.5:
            rsi_direction = "up"
        elif rsi_val < self._prev_rsi - 0.5:
            rsi_direction = "down"
        else:
            rsi_direction = "flat"
        self._prev_rsi = rsi_val

        # ── Volume ────────────────────────────────────────────────────────
        avg_vol = self._vol_sma.update(c.volume)
        rel_vol = (c.volume / avg_vol) if avg_vol and avg_vol > 0 else 1.0
        volume_surge = rel_vol >= 1.5

        # ── Bollinger Bands ───────────────────────────────────────────────
        self._bb_closes.append(c.close)
        bb_mid = self._bb_sma.update(c.close)
        bb_squeeze = False
        bb_position = 0.5   # default: midline

        if bb_mid is not None and len(self._bb_closes) >= 20:
            closes_list = list(self._bb_closes)
            mean = sum(closes_list) / len(closes_list)
            variance = sum((x - mean) ** 2 for x in closes_list) / len(closes_list)
            stddev = math.sqrt(variance) if variance > 0 else 0.0
            bb_upper = bb_mid + 2.0 * stddev
            bb_lower = bb_mid - 2.0 * stddev
            bb_width = bb_upper - bb_lower

            # Squeeze: BBW < threshold fraction of price
            bb_squeeze = (bb_width / bb_mid) < _BB_SQUEEZE_THRESHOLD if bb_mid > 0 else False

            # Band position: 0=lower, 0.5=mid, 1=upper
            if bb_upper > bb_lower:
                bb_position = max(0.0, min(1.0, (c.close - bb_lower) / (bb_upper - bb_lower)))

        # ── Candle microstructure ─────────────────────────────────────────
        candle_structure = self._classify_candle(c)

        # ── Market structure (HH/HL vs LH/LL) ────────────────────────────
        self._highs.append(c.high)
        self._lows.append(c.low)
        self._closes.append(c.close)
        market_structure = self._classify_market_structure()

        # ── Composite score ───────────────────────────────────────────────
        score = self._compute_score(
            c=c,
            ema_fast_val=ema_fast_val,
            ema_slow_val=ema_slow_val,
            rsi_val=rsi_val,
            rsi_direction=rsi_direction,
            rel_vol=rel_vol,
            bb_position=bb_position,
            bb_squeeze=bb_squeeze,
            candle_structure=candle_structure,
            market_structure=market_structure,
        )

        # ── Regime tag ────────────────────────────────────────────────────
        regime = self._classify_regime(
            score=score,
            ema_cross=self._ema_cross_dir,
            bb_squeeze=bb_squeeze,
            market_structure=market_structure,
        )

        self._prev_candle = c

        signal = TFSignal(
            timeframe=self.timeframe,
            score=score,
            regime=regime,
            ema_cross=self._ema_cross_dir,
            ema_cross_bars=self._ema_cross_bars,
            rsi=rsi_val,
            rsi_direction=rsi_direction,
            bb_squeeze=bb_squeeze,
            bb_position=bb_position,
            candle_structure=candle_structure,
            market_structure=market_structure,
            volume_surge=volume_surge,
            is_warm=self.is_warm,
        )
        self._last_signal = signal
        return signal

    def get_signal(self) -> Optional[TFSignal]:
        """Return the most recently computed TFSignal (or None before first update)."""
        return self._last_signal

    @property
    def is_warm(self) -> bool:
        """True once enough bars are processed for indicators to converge."""
        return self._bars_processed >= _WARMUP_BARS

    def reset(self) -> None:
        """Full reset — clears all state."""
        self._ema_fast.reset()
        self._ema_slow.reset()
        self._rsi.reset()
        self._vol_sma.reset()
        self._bb_sma.reset()
        self._bb_closes.clear()
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._ema_cross_dir = "flat"
        self._ema_cross_bars = 0
        self._prev_ema_above = None
        self._prev_rsi = None
        self._prev_candle = None
        self._bars_processed = 0
        self._last_signal = None

    # ── Internal score computation ────────────────────────────────────────────

    def _compute_score(
        self,
        c: Candle,
        ema_fast_val: float,
        ema_slow_val: float,
        rsi_val: float,
        rsi_direction: str,
        rel_vol: float,
        bb_position: float,
        bb_squeeze: bool,
        candle_structure: str,
        market_structure: str,
    ) -> float:
        """Combine components into a normalized -1 to +1 score."""
        score = 0.0

        # ── EMA component (weight 0.30) ───────────────────────────────────
        # Direction: fast>slow = +1, fast<slow = -1
        # Freshness: score is strongest on a fresh cross, decays for old ones
        ema_dir = 1.0 if ema_fast_val > ema_slow_val else -1.0
        freshness = max(
            _EMA_FRESHNESS_MIN,
            1.0 - (self._ema_cross_bars - 1) * 0.04,
        )
        score += _W_EMA * ema_dir * freshness

        # ── RSI component (weight 0.25) ───────────────────────────────────
        # Normalized: (RSI - 50) / 50 → range -1 to +1
        # Add slope boost: RSI trending up/down adds ±0.1
        rsi_norm = (rsi_val - 50.0) / 50.0
        rsi_slope_adj = 0.1 if rsi_direction == "up" else (-0.1 if rsi_direction == "down" else 0.0)
        rsi_component = max(-1.0, min(1.0, rsi_norm + rsi_slope_adj))
        score += _W_RSI * rsi_component

        # ── Candle structure component (weight 0.20) ──────────────────────
        candle_score = _candle_structure_score(candle_structure, c)
        score += _W_CANDLE * candle_score

        # ── Market structure component (weight 0.15) ──────────────────────
        if market_structure == "bull":
            score += _W_MARKET * 1.0
        elif market_structure == "bear":
            score += _W_MARKET * -1.0
        # "flat" → 0 contribution

        # ── Volume-weighted momentum (weight 0.10) ────────────────────────
        # High volume in a direction reinforces that direction
        vol_clamp = min(rel_vol, 3.0)
        if c.close > c.open:
            vol_component = (vol_clamp - 1.0) / 2.0  # 0 at avg vol, +1 at 3x vol
        elif c.close < c.open:
            vol_component = -(vol_clamp - 1.0) / 2.0
        else:
            vol_component = 0.0
        score += _W_VOLUME * max(-1.0, min(1.0, vol_component))

        # Clamp to [-1, +1]
        return max(-1.0, min(1.0, score))

    def _classify_candle(self, c: Candle) -> str:
        """Classify the candle's microstructure pattern."""
        body = abs(c.close - c.open)
        candle_range = c.high - c.low
        if candle_range < 1e-8:
            return "normal"

        # Doji: body < 10% of range
        if body / candle_range < 0.10:
            return "doji"

        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low

        # Bullish pin bar: lower wick > 2x body, closes in upper 40% of range
        if lower_wick > 2.0 * body and (c.close - c.low) / candle_range > 0.60:
            return "pin_bull"

        # Bearish pin bar: upper wick > 2x body, closes in lower 40% of range
        if upper_wick > 2.0 * body and (c.high - c.close) / candle_range > 0.60:
            return "pin_bear"

        # Engulfing: current body engulfs previous candle's body
        if self._prev_candle is not None:
            prev_open = self._prev_candle.open
            prev_close = self._prev_candle.close
            prev_body_high = max(prev_open, prev_close)
            prev_body_low = min(prev_open, prev_close)
            cur_body_high = max(c.open, c.close)
            cur_body_low = min(c.open, c.close)
            if (cur_body_high > prev_body_high and cur_body_low < prev_body_low
                    and c.close > c.open and prev_close < prev_open):
                return "engulf_bull"
            if (cur_body_high > prev_body_high and cur_body_low < prev_body_low
                    and c.close < c.open and prev_close > prev_open):
                return "engulf_bear"

        return "normal"

    def _classify_market_structure(self) -> str:
        """Classify market structure from recent highs and lows.

        Uses the last 6 bars' highs and lows.
        Bullish: recent highs trending higher AND recent lows trending higher.
        Bearish: recent highs trending lower AND recent lows trending lower.
        """
        if len(self._highs) < 4 or len(self._lows) < 4:
            return "flat"

        highs = list(self._highs)
        lows = list(self._lows)

        # Compare first half vs second half average (simple trend proxy)
        mid = len(highs) // 2
        high_early = sum(highs[:mid]) / mid
        high_late = sum(highs[mid:]) / (len(highs) - mid)
        low_early = sum(lows[:mid]) / mid
        low_late = sum(lows[mid:]) / (len(lows) - mid)

        highs_rising = high_late > high_early
        lows_rising = low_late > low_early

        if highs_rising and lows_rising:
            return "bull"   # HH + HL
        if not highs_rising and not lows_rising:
            return "bear"   # LH + LL
        return "flat"

    def _classify_regime(
        self,
        score: float,
        ema_cross: str,
        bb_squeeze: bool,
        market_structure: str,
    ) -> str:
        """Map score + indicator state to a human-readable regime tag."""
        if not self.is_warm:
            return "WARMING_UP"

        if bb_squeeze:
            # Squeeze: pending breakout — tag direction of lean
            if score > 0.15:
                return "SQUEEZE_BULL"
            elif score < -0.15:
                return "SQUEEZE_BEAR"
            return "SQUEEZE_FLAT"

        if ema_cross == "bull" and market_structure == "bull":
            if score >= 0.5:
                return "TREND_BULL"
            return "PULLBACK_BULL" if score < 0.0 else "TREND_BULL"

        if ema_cross == "bear" and market_structure == "bear":
            if score <= -0.5:
                return "TREND_BEAR"
            return "PULLBACK_BEAR" if score > 0.0 else "TREND_BEAR"

        if abs(score) < 0.2:
            return "RANGING"

        return "TRENDING_BULL" if score > 0 else "TRENDING_BEAR"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _candle_structure_score(structure: str, c: Candle) -> float:
    """Map candle structure + candle pressure to -1 to +1."""
    # Base: (close-open)/range — continuous pressure
    candle_range = c.high - c.low
    if candle_range > 0:
        pressure = (c.close - c.open) / candle_range
    else:
        pressure = 0.0

    bonus = 0.0
    if structure == "engulf_bull":
        bonus = 0.4
    elif structure == "engulf_bear":
        bonus = -0.4
    elif structure == "pin_bull":
        bonus = 0.3
    elif structure == "pin_bear":
        bonus = -0.3
    elif structure == "doji":
        # Doji = indecision; reduce magnitude
        pressure *= 0.2

    return max(-1.0, min(1.0, pressure + bonus))
