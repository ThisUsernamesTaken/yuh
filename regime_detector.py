"""
Regime Detector
===============
Classifies the current BTC market regime into one of:
  - TRENDING_UP    : Strong directional move higher
  - TRENDING_DOWN  : Strong directional move lower
  - RANGING        : Sideways chop, no clear direction
  - VOLATILE       : High ATR but no sustained direction (whipsaw)

The bias engine is momentum-based — it performs best in TRENDING regimes
and worst in RANGING/VOLATILE. The regime classification is used to:
  1. Gate signals: suppress or reduce confidence in unfavorable regimes
  2. Adjust position sizing: smaller stakes in VOLATILE, full stakes in TRENDING
  3. Alert the operator when regime shifts occur

Implementation uses three independent metrics that vote:
  - Directional Efficiency Ratio (DER): net displacement / total path length
  - ATR Expansion: current ATR vs longer-term ATR baseline
  - EMA Fan: alignment and spread of fast/medium/slow EMAs
"""

from dataclasses import dataclass
from enum import Enum
from collections import deque
from typing import Optional

from indicators import EMACalc, SMACalc
from models import Candle


class Regime(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


@dataclass
class RegimeState:
    """Snapshot of the current regime classification and component metrics."""
    regime: Regime
    confidence: float           # 0-100, how confident in this classification
    der: float                  # Directional Efficiency Ratio (-1 to +1)
    atr_ratio: float            # Current ATR / baseline ATR (>1 = expanding)
    ema_fan_score: float        # EMA alignment score (-1 to +1)
    trending_strength: float    # Combined trending score (0-100)
    volatility_score: float     # How volatile relative to baseline (0-100)

    @property
    def is_favorable(self) -> bool:
        """True if the regime favors our signal strategy.

        RANGING is included: 90-day backtest showed 67-71% WR in RANGING,
        which is our best regime. Only VOLATILE and UNKNOWN are unfavorable.
        """
        return self.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.RANGING)

    @property
    def confidence_multiplier(self) -> float:
        """Scale factor to apply to signal confidence based on regime.

        TRENDING: 1.0 (full confidence)
        RANGING:  0.3-0.6 (heavy discount)
        VOLATILE: 0.4-0.7 (moderate discount)
        UNKNOWN:  0.5 (neutral)
        """
        if self.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            # Scale with trending strength: 0.8 at weak trend, 1.0 at strong
            return 0.8 + (self.trending_strength / 100.0) * 0.2
        elif self.regime == Regime.RANGING:
            # Finding 2: RANGING is our best regime (67-71% WR in backtest).
            # Use multiplier 1.0 so confidence is not discounted.
            return 1.0
        elif self.regime == Regime.VOLATILE:
            return 0.4 + (1.0 - self.confidence / 100.0) * 0.3
        return 0.5


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


class RegimeDetector:
    """Classifies market regime from a stream of candles.

    Feed it closed candles at a single timeframe. Recommended: 5m or 15m.
    1m is too noisy for regime detection, 1h is too slow to react.

    Parameters:
        lookback:         Candles for DER calculation (default 20)
        atr_len:          ATR period (default 14)
        atr_baseline_len: Longer ATR period for baseline comparison (default 50)
        ema_fast:         Fast EMA for fan alignment (default 8)
        ema_mid:          Mid EMA for fan alignment (default 21)
        ema_slow:         Slow EMA for fan alignment (default 55)
        der_trend_thresh: DER above this = trending (default 0.35)
        der_range_thresh: DER below this = ranging (default 0.15)
        atr_vol_thresh:   ATR ratio above this = volatile (default 1.5)
    """

    def __init__(
        self,
        lookback: int = 20,
        atr_len: int = 14,
        atr_baseline_len: int = 50,
        ema_fast: int = 8,
        ema_mid: int = 21,
        ema_slow: int = 55,
        der_trend_thresh: float = 0.35,
        der_range_thresh: float = 0.15,
        atr_vol_thresh: float = 1.5,
    ) -> None:
        self._lookback = lookback
        self._atr_len = atr_len
        self._atr_baseline_len = atr_baseline_len
        self._der_trend_thresh = der_trend_thresh
        self._der_range_thresh = der_range_thresh
        self._atr_vol_thresh = atr_vol_thresh

        # Price history for DER
        self._closes: deque[float] = deque(maxlen=lookback + 1)

        # ATR components
        self._tr_buffer: deque[float] = deque(maxlen=atr_baseline_len)
        self._atr_short = EMACalc(atr_len)
        self._atr_long = EMACalc(atr_baseline_len)
        self._prev_close: Optional[float] = None

        # EMA fan
        self._ema_fast = EMACalc(ema_fast)
        self._ema_mid = EMACalc(ema_mid)
        self._ema_slow = EMACalc(ema_slow)

        # Bars processed
        self._bars: int = 0

        # Last state
        self._last_state: RegimeState = RegimeState(
            regime=Regime.UNKNOWN,
            confidence=0.0,
            der=0.0,
            atr_ratio=1.0,
            ema_fan_score=0.0,
            trending_strength=0.0,
            volatility_score=0.0,
        )

    @property
    def warmup_bars(self) -> int:
        """Minimum bars needed before regime classification is meaningful."""
        return max(self._lookback, self._atr_baseline_len, 55) + 5

    def update(self, candle: Candle) -> RegimeState:
        """Process one closed candle and return updated regime classification.

        Args:
            candle: A closed OHLCV candle.

        Returns:
            RegimeState with current classification and metrics.
        """
        self._bars += 1
        close = candle.close

        # ── True Range ──
        if self._prev_close is not None:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        else:
            tr = candle.high - candle.low
        self._prev_close = close

        self._tr_buffer.append(tr)
        atr_short = self._atr_short.update(tr)
        atr_long = self._atr_long.update(tr)

        # ── ATR ratio ──
        atr_ratio = atr_short / atr_long if atr_long > 0 else 1.0

        # ── Closes for DER ──
        self._closes.append(close)

        # ── DER (Directional Efficiency Ratio) ──
        # Net displacement / total path length over lookback
        der = 0.0
        if len(self._closes) >= self._lookback + 1:
            closes_list = list(self._closes)
            net_move = closes_list[-1] - closes_list[0]
            total_path = sum(
                abs(closes_list[i + 1] - closes_list[i])
                for i in range(len(closes_list) - 1)
            )
            if total_path > 0:
                der = net_move / total_path  # -1 to +1

        # ── EMA Fan ──
        ef = self._ema_fast.update(close)
        em = self._ema_mid.update(close)
        es = self._ema_slow.update(close)

        # Fan score: +1 when fast > mid > slow (bullish alignment)
        #           -1 when fast < mid < slow (bearish alignment)
        #            0 when mixed (no clear alignment)
        ema_fan_score = 0.0
        if ef > em > es:
            # Perfect bullish fan — score by spread magnitude
            spread = (ef - es) / es * 100.0 if es > 0 else 0.0
            ema_fan_score = _clamp(spread / 1.0, 0.0, 1.0)  # Normalize: 1% spread = full score
        elif ef < em < es:
            spread = (es - ef) / es * 100.0 if es > 0 else 0.0
            ema_fan_score = -_clamp(spread / 1.0, 0.0, 1.0)
        else:
            # Mixed — partial credit based on how many pairs are aligned
            pairs_bullish = int(ef > em) + int(em > es) + int(ef > es)
            pairs_bearish = int(ef < em) + int(em < es) + int(ef < es)
            ema_fan_score = (pairs_bullish - pairs_bearish) / 3.0

        # ── Classification ──
        abs_der = abs(der)
        abs_fan = abs(ema_fan_score)

        # Trending strength: combine DER and EMA fan (both directional)
        # Both range 0-1 in magnitude, average them
        trending_strength = _clamp(
            ((abs_der / self._der_trend_thresh) * 50.0 +
             (abs_fan) * 50.0),
            0.0, 100.0
        )

        # Volatility score: how much ATR has expanded
        volatility_score = _clamp(
            (atr_ratio - 1.0) / (self._atr_vol_thresh - 1.0) * 100.0,
            0.0, 100.0
        )

        # Decision tree
        if abs_der >= self._der_trend_thresh and abs_fan >= 0.3:
            # Strong directional move with EMA alignment
            if der > 0:
                regime = Regime.TRENDING_UP
            else:
                regime = Regime.TRENDING_DOWN
            confidence = trending_strength

        elif atr_ratio >= self._atr_vol_thresh and abs_der < self._der_trend_thresh:
            # High volatility but no sustained direction
            regime = Regime.VOLATILE
            confidence = volatility_score

        elif abs_der < self._der_range_thresh and abs_fan < 0.3:
            # Low efficiency + tangled EMAs = ranging
            regime = Regime.RANGING
            # Confidence: how strongly ranging (inverse of trending strength)
            confidence = _clamp(100.0 - trending_strength, 0.0, 100.0)

        elif abs_der < self._der_range_thresh:
            # Low DER but EMAs might be aligned — still ranging
            regime = Regime.RANGING
            confidence = _clamp(80.0 - trending_strength, 0.0, 100.0)

        elif atr_ratio >= self._atr_vol_thresh:
            # High vol with some direction — volatile trending
            # Classify by direction but lower confidence
            if der > 0:
                regime = Regime.TRENDING_UP
            else:
                regime = Regime.TRENDING_DOWN
            confidence = trending_strength * 0.6  # Discount for volatility

        else:
            # Ambiguous — default to unknown during warmup or transitions
            if self._bars < self.warmup_bars:
                regime = Regime.UNKNOWN
                confidence = 0.0
            else:
                # Lean toward the stronger signal
                if trending_strength > volatility_score:
                    regime = Regime.TRENDING_UP if der > 0 else Regime.TRENDING_DOWN
                    confidence = trending_strength * 0.5
                else:
                    regime = Regime.VOLATILE
                    confidence = volatility_score * 0.5

        state = RegimeState(
            regime=regime,
            confidence=confidence,
            der=der,
            atr_ratio=atr_ratio,
            ema_fan_score=ema_fan_score,
            trending_strength=trending_strength,
            volatility_score=volatility_score,
        )
        self._last_state = state
        return state

    @property
    def state(self) -> RegimeState:
        """Most recent regime classification."""
        return self._last_state

    @property
    def bars_processed(self) -> int:
        return self._bars

    def reset(self) -> None:
        self._closes.clear()
        self._tr_buffer.clear()
        self._atr_short.reset()
        self._atr_long.reset()
        self._ema_fast.reset()
        self._ema_mid.reset()
        self._ema_slow.reset()
        self._prev_close = None
        self._bars = 0
        self._last_state = RegimeState(
            regime=Regime.UNKNOWN, confidence=0.0, der=0.0,
            atr_ratio=1.0, ema_fan_score=0.0,
            trending_strength=0.0, volatility_score=0.0,
        )
