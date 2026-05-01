# ta_module.py — Real-time Technical Analysis scorer for BTC 1m candles
#
# Ports the Pine Script confidence model to Python for live integration
# with the cross-venue flow engine. Computes a directional score from
# EMA spread, RSI, volume, cycle return, and candle pressure on 1m BTC
# data. The score maps to confidence tiers (STRONG/MEDIUM/WEAK/MIMIC)
# that drive position sizing.
#
# Usage:
#   scorer = TAScorer()
#   for candle in candles:
#       result = scorer.update(candle)
#   # result.direction, result.confidence_tier, result.confidence
#
# The engine calls scorer.reset() at each 15m window boundary,
# then feeds closed 1m candles from Binance.

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from indicators import EMACalc, RSICalc, SMACalc, KSTCalc, ADXCalc
from models import Candle

logger = logging.getLogger(__name__)

# ─── Score component weights (match Pine Script model) ───
W_CYCLE_RETURN = 120.0
W_EMA_SPREAD = 25.0       # Was 200 (swapped with candle_pressure — matched back to profitable zip)
W_RSI_BIAS = 15.0
W_CANDLE_PRESSURE = 200.0  # Was 15 — this is the dominant signal in the profitable version
W_REL_VOL = 10.0

# ─── Confidence tier thresholds ───
TIER_STRONG = 75.0
TIER_MEDIUM = 50.0
TIER_WEAK = 20.0
TIER_MIMIC = 1.0      # Any non-zero lean


@dataclass
class TASignalResult:
    """Output of the TA scorer for one 1m candle update."""
    direction: str              # "up" or "down" (or "flat" if score == 0)
    composite_score: float      # EMA(2)-smoothed, range roughly -100 to +100
    raw_score: float            # Pre-smoothing score
    score_velocity: float       # 1-bar rate of change of composite_score
    confidence: float           # abs(composite_score), clamped 0-100
    confidence_tier: str        # "STRONG", "MEDIUM", "WEAK", "MIMIC", "NONE"
    tier_mult: float            # 4.0, 2.0, 1.0, 0.5, 0.0

    # Component breakdown (for logging / diagnostics)
    ema_spread_pct: float
    rsi_7: float
    rel_volume: float
    cycle_return_pct: float
    candle_pressure: float
    bars_in_cycle: int
    kst_value: float = 0.0
    kst_signal: float = 0.0
    kst_bullish: bool = False
    adx_value: float = 0.0
    adx_trending: bool = False
    adx_choppy: bool = True

    timestamp: float = 0.0


def _classify_tier(confidence: float) -> tuple[str, float]:
    """Map confidence to tier name and position size multiplier."""
    if confidence >= TIER_STRONG:
        return "STRONG", 4.0
    if confidence >= TIER_MEDIUM:
        return "MEDIUM", 2.0
    if confidence >= TIER_WEAK:
        return "WEAK", 1.0
    if confidence >= TIER_MIMIC:
        return "MIMIC", 0.5
    return "NONE", 0.0


class TAScorer:
    """Incremental 1m BTC technical analysis scorer.

    Feed closed 1m candles via update(). Call reset() at each 15m
    window boundary. The scorer maintains all indicator state and
    produces a directional confidence signal.
    """

    def __init__(self) -> None:
        # EMA spread
        self._ema_fast = EMACalc(5)
        self._ema_slow = EMACalc(13)

        # RSI
        self._rsi = RSICalc(7)

        # Volume (relative to 20-bar SMA)
        self._vol_sma = SMACalc(20)

        # Score smoothing (EMA-2, matches Pine `ta.ema(rawScore, 2)`)
        self._score_ema = EMACalc(2)

        # KST oscillator — multi-timeframe momentum
        self._kst = KSTCalc()

        # ADX — trend strength (not direction)
        self._adx = ADXCalc(14)

        # Score history for velocity
        self._prev_score: Optional[float] = None

        # 15m cycle tracking
        self._cycle_open_price: Optional[float] = None
        self._bars_in_cycle: int = 0

        # Last result
        self._last_result: Optional[TASignalResult] = None

    def reset(self) -> None:
        """Full reset — only used on first startup when no candle history exists."""
        self._ema_fast.reset()
        self._ema_slow.reset()
        self._rsi.reset()
        self._vol_sma.reset()
        self._score_ema.reset()
        self._prev_score = None
        self._cycle_open_price = None
        self._bars_in_cycle = 0
        self._last_result = None

    def soft_reset(self) -> None:
        """Window boundary reset — keep EMA/RSI/volume warm, only reset cycle tracking.

        BTC trends don't stop at Kalshi window boundaries. Carrying indicator
        state gives immediate directional signal on the first candle instead of
        guessing for 60 seconds while EMAs converge.
        """
        self._cycle_open_price = None
        self._bars_in_cycle = 0
        # Keep: _ema_fast, _ema_slow, _rsi, _vol_sma, _score_ema, _prev_score, _last_result

    def update(self, candle: Candle) -> TASignalResult:
        """Process one closed 1m candle and return the updated TA signal.

        Args:
            candle: A closed 1m OHLCV candle (from Binance klines).

        Returns:
            TASignalResult with direction, confidence, tier, and components.
        """
        c = candle
        self._bars_in_cycle += 1

        # Track cycle open (first candle in this 15m window)
        if self._cycle_open_price is None:
            self._cycle_open_price = c.open

        # ── Component 1: EMA spread ──
        ema_fast_val = self._ema_fast.update(c.close)
        ema_slow_val = self._ema_slow.update(c.close)
        ema_spread_pct = ((ema_fast_val - ema_slow_val) / c.close * 100.0
                          if c.close != 0 else 0.0)

        # ── Component 2: RSI bias ──
        rsi_val = self._rsi.update(c.close)
        rsi_bias = (rsi_val - 50.0) / 50.0  # Normalized to [-1, +1]

        # ── Component 3: Relative volume ──
        avg_vol = self._vol_sma.update(c.volume)
        if avg_vol and avg_vol > 0:
            rel_vol = c.volume / avg_vol
        else:
            rel_vol = 1.0
        rel_vol_clamped = max(0.0, min(rel_vol, 3.0))

        # ── Component 4: 15m cycle return ──
        if self._cycle_open_price and self._cycle_open_price != 0:
            cycle_return_pct = ((c.close - self._cycle_open_price)
                                / self._cycle_open_price * 100.0)
        else:
            cycle_return_pct = 0.0

        # ── Component 5: Candle pressure ──
        candle_range = c.high - c.low
        candle_pressure = ((c.close - c.open) / candle_range
                           if candle_range > 0 else 0.0)

        # ── Composite raw score ──
        raw_score = (
            cycle_return_pct * W_CYCLE_RETURN
            + ema_spread_pct * W_EMA_SPREAD
            + rsi_bias * W_RSI_BIAS
            + candle_pressure * W_CANDLE_PRESSURE
            + (rel_vol_clamped - 1.0) * W_REL_VOL
        )

        # ── EMA(2) smoothing ──
        score = self._score_ema.update(raw_score)

        # ── Velocity ──
        velocity = (score - self._prev_score) if self._prev_score is not None else 0.0
        self._prev_score = score

        # ── Confidence & tier ──
        confidence = max(0.0, min(abs(score), 100.0))
        tier_name, tier_mult = _classify_tier(confidence)

        # ── Direction ──
        if score > 0:
            direction = "up"
        elif score < 0:
            direction = "down"
        else:
            direction = "flat"

        # KST and ADX
        kst_val, kst_sig = self._kst.update(c.close)
        adx_val = self._adx.update(c.high, c.low, c.close)

        result = TASignalResult(
            direction=direction,
            composite_score=score,
            raw_score=raw_score,
            score_velocity=velocity,
            confidence=confidence,
            confidence_tier=tier_name,
            tier_mult=tier_mult,
            ema_spread_pct=ema_spread_pct,
            rsi_7=rsi_val,
            rel_volume=rel_vol_clamped,
            cycle_return_pct=cycle_return_pct,
            candle_pressure=candle_pressure,
            bars_in_cycle=self._bars_in_cycle,
            kst_value=kst_val,
            kst_signal=kst_sig,
            kst_bullish=self._kst.is_bullish,
            adx_value=adx_val,
            adx_trending=self._adx.is_trending,
            adx_choppy=self._adx.is_choppy,
            timestamp=candle.timestamp / 1000.0 if candle.timestamp > 1e12 else candle.timestamp,
        )

        self._last_result = result
        return result

    @property
    def last_result(self) -> Optional[TASignalResult]:
        return self._last_result

    @property
    def bars_processed(self) -> int:
        return self._bars_in_cycle

    @property
    def is_warm(self) -> bool:
        """True once we've processed enough bars for indicators to be meaningful."""
        return self._bars_in_cycle >= 3


# ─────────────────────────────────────────────
# Binance 1m candle fetcher
# ─────────────────────────────────────────────

async def fetch_binance_1m_candles(
    session: aiohttp.ClientSession,
    limit: int = 25,
) -> list[Candle]:
    """Fetch recent closed 1m BTC candles from Binance public API.

    Returns up to `limit` closed candles (the current open candle is
    stripped). The 25-candle default provides enough history for the
    20-bar volume SMA warmup plus current data.
    """
    url = "https://api.binance.us/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "limit": limit}

    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                logger.warning("TAModule: Binance klines HTTP %d", resp.status)
                return []
            data = await resp.json()
    except Exception as e:
        logger.debug("TAModule: Binance klines fetch error: %s", e)
        return []

    if not isinstance(data, list):
        return []

    candles: list[Candle] = []
    for row in data:
        # Binance kline format:
        # [open_time, open, high, low, close, volume, close_time, ...]
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            open_time_ms = int(row[0])
            close_time_ms = int(row[6])
            now_ms = int(time.time() * 1000)

            # Only include closed candles (close_time in the past)
            is_closed = close_time_ms < now_ms

            candles.append(Candle(
                timestamp=open_time_ms,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                closed=is_closed,
            ))
        except (ValueError, TypeError, IndexError):
            continue

    # Return only closed candles
    return [c for c in candles if c.closed]
