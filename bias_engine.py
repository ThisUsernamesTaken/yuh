# bias_engine.py — Per-timeframe BiasEngine replicating Pine Script logic

from typing import Optional

from config import (
    FAST_EMA_LEN, SLOW_EMA_LEN, RSI_LEN, VOL_AVG_LEN, SCORE_SMOOTH_LEN,
    CYCLE_RETURN_W, EMA_SPREAD_W, RSI_BIAS_W, CANDLE_PRESSURE_W, REL_VOL_W,
    ENTRY_THRESHOLD, DECISION_START_BAR, TF_MINUTES,
)
from config_phase3 import (
    SCORE_VELOCITY_CAP, REQUIRE_VEL_ALIGN, LOCK_ON_FIRST_TOUCH, CONFIRM_BARS,
    FILTER_BAD_HOURS, BAD_UTC_HOURS,
)
from indicators import EMACalc, RSICalc, SMACalc
from models import Candle, BiasResult


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


class BiasEngine:
    """Single-timeframe bias scoring engine.

    Replicates the Pine Script signal model exactly:
    - Tracks 15m (or configured) cycle boundaries
    - Computes cycleReturnPct, emaSpreadPct, rsiBias, candlePressure, relVol
    - Produces a smoothed score and bull/bear confidence values
    - Tracks the highest-confidence decision within each cycle
    """

    def __init__(
        self,
        timeframe: str = "15m",
        velocity_cap: float = SCORE_VELOCITY_CAP,
        require_vel_align: bool = REQUIRE_VEL_ALIGN,
        lock_on_first_touch: bool = LOCK_ON_FIRST_TOUCH,
        confirm_bars: int = CONFIRM_BARS,
        filter_bad_hours: bool = FILTER_BAD_HOURS,
    ) -> None:
        self.timeframe = timeframe
        self._cycle_minutes: int = TF_MINUTES[timeframe]

        # 3rd-iteration filter settings
        self._velocity_cap = velocity_cap
        self._require_vel_align = require_vel_align
        self._lock_on_first_touch = lock_on_first_touch
        self._confirm_bars = confirm_bars
        self._filter_bad_hours = filter_bad_hours

        # Indicators
        self._ema_fast = EMACalc(FAST_EMA_LEN)
        self._ema_slow = EMACalc(SLOW_EMA_LEN)
        self._rsi = RSICalc(RSI_LEN)
        self._vol_sma = SMACalc(VOL_AVG_LEN)
        self._score_ema = EMACalc(SCORE_SMOOTH_LEN)

        # Cycle tracking
        self._cycle_open_price: Optional[float] = None
        self._current_cycle_ts: Optional[int] = None  # ms timestamp of cycle start
        self._bars_in_cycle: int = 0
        self._prev_score: Optional[float] = None

        # Consecutive qualifying bar streaks (for confirmBars logic)
        self._call_streak: int = 0
        self._put_streak: int = 0

        # Best decision within current cycle
        self._decided_call: bool = False
        self._decided_put: bool = False
        self._decided_conf: float = 0.0
        self._decided_snapshot: Optional[BiasResult] = None

    def _get_cycle_ts(self, candle_ts_ms: int) -> int:
        """Return the cycle-aligned timestamp for this candle (floor to cycle boundary)."""
        cycle_ms = self._cycle_minutes * 60 * 1000
        return (candle_ts_ms // cycle_ms) * cycle_ms

    def update(self, candle: Candle) -> BiasResult:
        """Process one closed candle and return the current BiasResult.

        Args:
            candle: A fully closed OHLCV candle at this engine's timeframe.

        Returns:
            BiasResult with current component values and cycle decision state.
        """
        # --- Cycle tracking ---
        cycle_ts = self._get_cycle_ts(candle.timestamp)
        new_cycle = cycle_ts != self._current_cycle_ts

        if new_cycle:
            self._current_cycle_ts = cycle_ts
            self._cycle_open_price = candle.open
            self._bars_in_cycle = 1
            self._call_streak = 0
            self._put_streak = 0
            self._decided_call = False
            self._decided_put = False
            self._decided_conf = 0.0
            self._decided_snapshot = None
        else:
            self._bars_in_cycle += 1

        # --- Indicators ---
        ema_fast = self._ema_fast.update(candle.close)
        ema_slow = self._ema_slow.update(candle.close)
        rsi_val = self._rsi.update(candle.close)
        vol_avg = self._vol_sma.update(candle.volume)

        # --- Components ---
        ema_spread_pct = (
            ((ema_fast - ema_slow) / candle.close) * 100.0
            if candle.close != 0.0 else 0.0
        )

        rsi_bias = (rsi_val - 50.0) / 50.0

        if vol_avg is not None and vol_avg != 0.0:
            rel_vol = candle.volume / vol_avg
        else:
            rel_vol = 1.0
        rel_vol_clamped = _clamp(rel_vol, 0.0, 3.0)

        cycle_return_pct = (
            ((candle.close - self._cycle_open_price) / self._cycle_open_price) * 100.0
            if self._cycle_open_price and self._cycle_open_price != 0.0 else 0.0
        )

        candle_range = candle.high - candle.low
        candle_pressure = (
            (candle.close - candle.open) / candle_range
            if candle_range != 0.0 else 0.0
        )

        # --- Raw score ---
        raw_score = (
            cycle_return_pct  * CYCLE_RETURN_W
            + ema_spread_pct  * EMA_SPREAD_W
            + rsi_bias        * RSI_BIAS_W
            + candle_pressure * CANDLE_PRESSURE_W
            + (rel_vol_clamped - 1.0) * REL_VOL_W
        )

        # --- Smoothed score ---
        score = self._score_ema.update(raw_score)

        # --- Score velocity (1-bar rate of change) ---
        score_velocity = score - self._prev_score if self._prev_score is not None else 0.0
        self._prev_score = score

        # --- Confidence ---
        bull_conf = _clamp(score, 0.0, 100.0)
        bear_conf = _clamp(-score, 0.0, 100.0)

        # --- 3rd-iteration filters ---
        from datetime import datetime, timezone
        utc_hour = datetime.fromtimestamp(candle.timestamp / 1000, tz=timezone.utc).hour

        after_decision_start = self._bars_in_cycle >= DECISION_START_BAR
        velocity_ok = abs(score_velocity) <= self._velocity_cap
        time_ok = (not self._filter_bad_hours) or (utc_hour not in BAD_UTC_HOURS)

        call_qualified = (
            after_decision_start
            and bull_conf >= ENTRY_THRESHOLD
            and velocity_ok
            and time_ok
            and ((not self._require_vel_align) or score_velocity > 0)
        )
        put_qualified = (
            after_decision_start
            and bear_conf >= ENTRY_THRESHOLD
            and velocity_ok
            and time_ok
            and ((not self._require_vel_align) or score_velocity < 0)
        )

        # Update streaks (reset on new cycle handled above)
        self._call_streak = self._call_streak + 1 if call_qualified else 0
        self._put_streak = self._put_streak + 1 if put_qualified else 0

        call_active = self._call_streak >= self._confirm_bars
        put_active = self._put_streak >= self._confirm_bars

        result = BiasResult(
            timeframe=self.timeframe,
            timestamp=candle.timestamp,
            cycle_return_pct=cycle_return_pct,
            ema_spread_pct=ema_spread_pct,
            rsi_bias=rsi_bias,
            candle_pressure=candle_pressure,
            rel_vol_clamped=rel_vol_clamped,
            raw_score=raw_score,
            score=score,
            bull_conf=bull_conf,
            bear_conf=bear_conf,
            decided_call=self._decided_call,
            decided_put=self._decided_put,
            decided_conf=self._decided_conf,
            bars_in_cycle=self._bars_in_cycle,
        )

        # --- Decision tracking with lock-on-first-touch ---
        already_decided = self._decided_call or self._decided_put

        if call_active:
            can_update = (not already_decided) if self._lock_on_first_touch else (bull_conf > self._decided_conf)
            if can_update:
                self._decided_call = True
                self._decided_put = False
                self._decided_conf = bull_conf
                self._decided_snapshot = result

        if put_active:
            can_update = (not already_decided) if self._lock_on_first_touch else (bear_conf > self._decided_conf)
            if can_update:
                self._decided_call = False
                self._decided_put = True
                self._decided_conf = bear_conf
                self._decided_snapshot = result

        result.decided_call = self._decided_call
        result.decided_put = self._decided_put
        result.decided_conf = self._decided_conf

        return result

    @property
    def decided_snapshot(self) -> Optional[BiasResult]:
        """The BiasResult snapshot from the moment the best decision was made."""
        return self._decided_snapshot

    def reset(self) -> None:
        """Reset all state (use when reinitializing the engine)."""
        self._ema_fast.reset()
        self._ema_slow.reset()
        self._rsi.reset()
        self._vol_sma.reset()
        self._score_ema.reset()
        self._cycle_open_price = None
        self._current_cycle_ts = None
        self._bars_in_cycle = 0
        self._prev_score = None
        self._call_streak = 0
        self._put_streak = 0
        self._decided_call = False
        self._decided_put = False
        self._decided_conf = 0.0
        self._decided_snapshot = None
