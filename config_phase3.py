# config_phase3.py — Additional constants for Phase 3 (Signal Intelligence)
#
# Import these alongside config.py, or merge into config.py when ready.
# Kept separate to avoid breaking existing Phase 1-2 code.

# ─────────────────────────────────────────────
# REGIME DETECTOR
# ─────────────────────────────────────────────

# Which timeframe to feed the regime detector (recommended: 5m or 15m)
REGIME_TIMEFRAME: str = "5m"

# DER (Directional Efficiency Ratio) thresholds
DER_TREND_THRESH: float = 0.35     # DER above this = trending
DER_RANGE_THRESH: float = 0.15     # DER below this = ranging

# ATR expansion threshold for VOLATILE classification
ATR_VOL_THRESH: float = 1.5        # Current ATR / baseline ATR above this = volatile

# Lookback periods
REGIME_LOOKBACK: int = 20           # Candles for DER calculation
REGIME_ATR_LEN: int = 14            # ATR period
REGIME_ATR_BASELINE_LEN: int = 50   # Longer ATR for baseline comparison
REGIME_EMA_FAST: int = 8            # Fast EMA for fan alignment
REGIME_EMA_MID: int = 21            # Mid EMA for fan alignment
REGIME_EMA_SLOW: int = 55           # Slow EMA for fan alignment

# ─────────────────────────────────────────────
# SIGNAL FILTER
# ─────────────────────────────────────────────

# Finding 6: Strategy E (TF Majority) achieves 63.5% WR without a confidence
# threshold. Gate is TF alignment >= 3, not raw confidence score.
MIN_SIGNAL_CONFIDENCE: float = 0.0

# Minimum TFs aligned with consensus direction to approve signal
MIN_TF_ALIGNMENT: int = 3           # Out of 5 total TFs

# Bad hours: UTC hours to skip
# Finding 3: 16h UTC = 68.5% WR (best hour) — removed from bad list
# Finding 4: 12h = 59.4%, 21h = 58.2% — added as weak
FILTER_BAD_HOURS: bool = True
BAD_UTC_HOURS: set[int] = {9, 12, 15, 21}

# Regime filtering
REQUIRE_FAVORABLE_REGIME: bool = True
ALLOW_VOLATILE_TRENDING: bool = True  # Allow VOLATILE if trending strength > 40

# Model degradation detection
CHECK_MODEL_DEGRADATION: bool = True
WIN_RATE_WINDOW: int = 100           # Rolling window size per bucket
DEGRADATION_THRESHOLD_PCT: float = 10.0  # Percentage points below baseline
DEGRADATION_MIN_TRADES: int = 10     # Minimum trades before checking

# ─────────────────────────────────────────────
# VELOCITY FILTERS (from Pine Script 3rd iteration)
# ─────────────────────────────────────────────
# These need to be wired into bias_engine.py

SCORE_VELOCITY_CAP: float = 30.0     # Block signals when |score - prev_score| > this
REQUIRE_VEL_ALIGN: bool = True       # Velocity must match signal direction
LOCK_ON_FIRST_TOUCH: bool = True     # Lock at first qualifying bar, not peak
CONFIRM_BARS: int = 1                # Consecutive qualifying bars needed (1=first touch)
