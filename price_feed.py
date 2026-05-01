# price_feed.py — Real-time OHLCV price feed for BTC/USDT
#
# Maintains rolling candle buffers for 1m/5m/15m/1h timeframes.
# WebSocket for 1m/5m/15m (low-latency, sub-100ms candle close events).
# REST polling for 1h (slow-moving, 15-min poll adequate).
#
# Usage:
#   feed = PriceFeedTask(session)
#   asyncio.create_task(feed.run())
#   feed.register_candle_close_callback("5m", my_fn)   # fn(tf, candle)
#   candles = feed.get_candles("15m", 20)

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import aiohttp

from models import Candle

logger = logging.getLogger(__name__)

# ── Load user config ──────────────────────────────────────────────────────────
_user_cfg: dict = {}
try:
    import importlib as _il
    _uc_mod = _il.import_module("user_config")
    _user_cfg = {k: v for k, v in vars(_uc_mod).items() if not k.startswith("_")}
except ImportError:
    pass


def _uc(name: str, default):
    return _user_cfg.get(name, default)


# ── Constants ─────────────────────────────────────────────────────────────────
BINANCE_WS_URL = "wss://stream.binance.us:9443/stream"  # Binance US (global blocked from US IPs)
BINANCE_REST_URL = "https://api.binance.us/api/v3/klines"

# Rolling buffer depths — enough for all indicator warmup requirements
BUFFER_SIZES: dict[str, int] = {
    "1m":  60,   # 60 bars: EMA(20) vol SMA + recent structure
    "5m":  50,   # ~4h history, EMA(21) warmup
    "15m": 30,   # ~7.5h history, EMA(21) + BB(20) warmup
    "1h":  72,   # 3 days, EMA(50) warmup
}

# Timeframes served by WebSocket vs REST
_WS_TIMEFRAMES = ["1m", "5m", "15m"]
_REST_TIMEFRAMES = ["1h"]

# Binance interval strings (match our TF key names directly)
_BINANCE_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}

# REST poll interval for 1h (seconds); 15 min is plenty
_1H_POLL_INTERVAL_S = 900.0

# WebSocket reconnect settings
_WS_RECONNECT_BASE_S = 5.0
_WS_RECONNECT_MAX_S = 60.0
_WS_HEARTBEAT_TIMEOUT_S = 30.0


@dataclass
class PriceFeedState:
    """Snapshot of current candle state across all timeframes.

    Read by MTFConfluenceScorer. Updated by PriceFeedTask.
    Single-threaded asyncio — no locking needed.
    """
    buffers: dict = field(default_factory=dict)          # tf → deque[Candle]
    last_closed: dict = field(default_factory=dict)      # tf → Optional[Candle]
    last_updated: dict = field(default_factory=dict)     # tf → float (epoch)
    ws_connected: bool = False
    ws_reconnect_count: int = 0


class BTCTickTracker:
    """Real-time BTC price tick tracker for reversion detection.

    Tracks last N BTC prices from aggTrade stream. Detects when BTC
    stops falling and starts rising (or vice versa) — the reversion signal.
    """

    def __init__(self, window: int = 10):
        self._prices: deque = deque(maxlen=window)
        self._last_update: float = 0.0
        self.last_price: float = 0.0

    def add_tick(self, price: float, ts: float) -> None:
        self._prices.append((ts, price))
        self.last_price = price
        self._last_update = ts

    @property
    def is_reversing_up(self) -> bool:
        """BTC was falling and just started rising. 3+ consecutive rising ticks."""
        if len(self._prices) < 5:
            return False
        prices = [p for _, p in self._prices]
        # Need: earlier prices falling, last 3 rising
        recent_3 = prices[-3:]
        if not (recent_3[0] < recent_3[1] < recent_3[2]):
            return False
        # And before that, prices were falling
        earlier = prices[-6:-3] if len(prices) >= 6 else prices[:3]
        return earlier[-1] > earlier[0]  # Was declining

    @property
    def is_reversing_down(self) -> bool:
        """BTC was rising and just started falling. 3+ consecutive falling ticks."""
        if len(self._prices) < 5:
            return False
        prices = [p for _, p in self._prices]
        recent_3 = prices[-3:]
        if not (recent_3[0] > recent_3[1] > recent_3[2]):
            return False
        earlier = prices[-6:-3] if len(prices) >= 6 else prices[:3]
        return earlier[-1] < earlier[0]  # Was rising

    @property
    def tick_velocity(self) -> float:
        """BTC price change per second over last 5 ticks."""
        if len(self._prices) < 2:
            return 0.0
        recent = list(self._prices)[-5:]
        dt = recent[-1][0] - recent[0][0]
        dp = recent[-1][1] - recent[0][1]
        return dp / max(dt, 0.1)

    def velocity_window(self, seconds_back: float) -> float:
        """BTC $/sec over the last `seconds_back` seconds (can be sub-second).

        Walks the tick deque from the most recent backward until it finds the
        first tick at-or-before now − seconds_back, then returns
        (last_price − that_price) / elapsed.

        For ms-level velocity, pass small windows (e.g. 0.1 = last 100ms,
        0.5 = last 500ms). Used by the maker→taker escalation as a faster
        confirmation signal than the multi-second `tick_velocity`.

        Returns 0.0 if not enough ticks in the window.
        """
        if len(self._prices) < 2 or seconds_back <= 0:
            return 0.0
        now_ts = self._prices[-1][0]
        cutoff = now_ts - seconds_back
        # Walk backward to find oldest tick still inside the window
        anchor_ts, anchor_price = self._prices[-1]
        # We want the OLDEST tick at-or-after cutoff
        for ts, p in self._prices:
            if ts >= cutoff:
                anchor_ts, anchor_price = ts, p
                break
        dt = self._prices[-1][0] - anchor_ts
        if dt <= 0:
            return 0.0
        dp = self._prices[-1][1] - anchor_price
        return dp / dt

    @property
    def tick_velocity_ms_100(self) -> float:
        """BTC $/sec over the last 100ms — fastest pulse."""
        return self.velocity_window(0.10)

    @property
    def tick_velocity_ms_250(self) -> float:
        """BTC $/sec over the last 250ms."""
        return self.velocity_window(0.25)

    @property
    def tick_velocity_ms_500(self) -> float:
        """BTC $/sec over the last 500ms — sub-second average."""
        return self.velocity_window(0.50)

    @property
    def tick_count_recent_1s(self) -> int:
        """How many ticks landed in the last 1 second. Useful as an
        activity / liquidity proxy — quiet tape <5 ticks/s, active 20+."""
        if not self._prices:
            return 0
        now_ts = self._prices[-1][0]
        cutoff = now_ts - 1.0
        return sum(1 for ts, _ in self._prices if ts >= cutoff)

    @property
    def is_stale(self) -> bool:
        return time.time() - self._last_update > 30.0


class BtcVolumeTracker:
    """Aggregates BTC spot trades into a price-bucketed volume profile.

    Fed from Coinbase Exchange WS `matches` channel: per-trade price, size,
    aggressor side, and timestamp. Maintains a bounded rolling deque of
    trades. Bucket aggregation is computed on demand (small windows, cheap).

    Bucket size is configurable (default $10 = 0.013% of BTC at $77k).

    Coinbase ``match`` semantics: the message's ``side`` field indicates the
    MAKER side. We invert at ingest time so ``side`` stored in this tracker
    is the AGGRESSOR (taker) side — the side that consumed liquidity.
    Aggressor=buy means a market buy lifted the offer (bullish pressure).
    Aggressor=sell means a market sell hit the bid (bearish pressure).
    """

    __slots__ = ("_trades", "_bucket_dollars", "_max_age_s", "_last_update")

    def __init__(self, max_age_s: float = 900.0, bucket_dollars: float = 10.0) -> None:
        # 15-min default window covers a full Kalshi BTC15M session.
        self._trades: deque = deque()  # (ts, price, size, aggressor_side)
        self._bucket_dollars = max(0.01, float(bucket_dollars))
        self._max_age_s = float(max_age_s)
        self._last_update: float = 0.0

    # ── Ingest ────────────────────────────────────────────────────────────

    def on_match(self, *, price: float, size: float,
                  maker_side: str, ts: float) -> None:
        """Record one Coinbase match. ``maker_side`` is the WS field value."""
        if price <= 0 or size <= 0:
            return
        # Invert maker → aggressor.
        aggressor = "sell" if maker_side == "buy" else "buy"
        self._trades.append((float(ts), float(price), float(size), aggressor))
        self._last_update = float(ts)
        # Evict stale entries (cheap during normal flow; bounded by deque growth).
        cutoff = float(ts) - self._max_age_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    # ── Internals ─────────────────────────────────────────────────────────

    def _bucket(self, price: float) -> float:
        """Floor price to nearest bucket boundary."""
        if self._bucket_dollars <= 0:
            return float(price)
        return float(int(price / self._bucket_dollars) * self._bucket_dollars)

    def _iter_window(self, since_ts: Optional[float] = None,
                     window_s: Optional[float] = None):
        """Yield (ts, price, size, aggressor) for trades in the requested
        window. ``since_ts`` overrides ``window_s`` if both are provided."""
        if since_ts is None and window_s is not None:
            since_ts = (self._last_update or time.time()) - float(window_s)
        if since_ts is None:
            for tup in self._trades:
                yield tup
        else:
            for tup in self._trades:
                if tup[0] >= since_ts:
                    yield tup

    # ── Aggregations ──────────────────────────────────────────────────────

    def total_volume(self, *, since_ts: Optional[float] = None,
                      window_s: Optional[float] = None) -> float:
        return sum(t[2] for t in self._iter_window(since_ts, window_s))

    def vwap(self, *, since_ts: Optional[float] = None,
              window_s: Optional[float] = None) -> Optional[float]:
        num = 0.0
        den = 0.0
        for _, price, size, _ in self._iter_window(since_ts, window_s):
            num += price * size
            den += size
        if den <= 0:
            return None
        return num / den

    def point_of_control(self, *, since_ts: Optional[float] = None,
                          window_s: Optional[float] = None) -> Optional[float]:
        """Return the price-bucket boundary with the highest volume."""
        buckets: dict = {}
        for _, price, size, _ in self._iter_window(since_ts, window_s):
            key = self._bucket(price)
            buckets[key] = buckets.get(key, 0.0) + size
        if not buckets:
            return None
        return max(buckets.items(), key=lambda kv: kv[1])[0]

    def cumulative_volume_above(self, price: float, *,
                                  since_ts: Optional[float] = None,
                                  window_s: Optional[float] = None) -> float:
        return sum(t[2] for t in self._iter_window(since_ts, window_s)
                   if t[1] > price)

    def cumulative_volume_below(self, price: float, *,
                                  since_ts: Optional[float] = None,
                                  window_s: Optional[float] = None) -> float:
        return sum(t[2] for t in self._iter_window(since_ts, window_s)
                   if t[1] < price)

    def aggressor_imbalance(self, *, since_ts: Optional[float] = None,
                              window_s: Optional[float] = None) -> Optional[float]:
        """(buy_aggressor_size - sell_aggressor_size) / total_size in [-1, +1].
        Positive = aggressive buying; negative = aggressive selling. None if
        no trades in the window."""
        buy = 0.0
        sell = 0.0
        for _, _, size, side in self._iter_window(since_ts, window_s):
            if side == "buy":
                buy += size
            elif side == "sell":
                sell += size
        total = buy + sell
        if total <= 0:
            return None
        return (buy - sell) / total

    # ── Status helpers ────────────────────────────────────────────────────

    @property
    def is_stale(self) -> bool:
        if self._last_update <= 0:
            return True
        return time.time() - self._last_update > 30.0

    @property
    def trade_count(self) -> int:
        return len(self._trades)


class FiveSecCandle:
    """Single 5-second OHLCV candle."""
    __slots__ = ('open', 'high', 'low', 'close', 'volume', 'ts')
    def __init__(self, price: float, ts: float):
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume = 1
        self.ts = ts

    def update(self, price: float):
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += 1


class BinaryProbabilityEngine:
    """Micro-Temporal Density Collapse — fair value for 15-min BTC binary contracts.

    Models the contract as a Brownian Bridge derivative. Computes the probability
    BTC finishes above the strike (session open) using the Normal CDF via
    Abramowitz & Stegun erf approximation.

    Fair value = P(BTC > strike at expiry) * 100 cents.
    Mispricing = fair_value - contract_mid. Positive = contract underpriced (buy YES).
    """

    def __init__(self):
        self.strike: float = 0.0          # BTC price at session open
        self.fair_value: float = 50.0     # 0-100 cents
        self.mispricing: float = 0.0      # fair_value - contract_mid
        self.probability: float = 0.5     # 0.0-1.0
        self.volatility: float = 0.0      # Annualized from recent 5s candles
        self.time_to_expiry_s: float = 900.0  # Seconds remaining
        self._vol_buffer: deque = deque(maxlen=180)  # 15 min of 5s candles = 180

    def set_strike(self, btc_price: float) -> None:
        """Set the strike price (BTC at session open)."""
        self.strike = btc_price

    def calibrate_strike(self, btc_spot: float, kalshi_mid_cents: float, seconds_left: float) -> None:
        """Back-calculate strike from Kalshi opening mid + Coinbase spot.

        Kalshi uses CF Benchmarks BRTI (60-source average), not Coinbase.
        The difference can be ~$100. At session open, the Kalshi mid tells us
        the market's implied probability. We solve the Brownian Bridge formula
        in reverse to find the strike K that produces that probability:

            P = CDF(ln(S/K) / (sigma * sqrt(T)))
            => d = inverseCDF(P)
            => K = S / exp(d * sigma * sqrt(T))
        """
        import math
        if btc_spot <= 0 or seconds_left <= 0:
            self.strike = btc_spot
            return

        # Clamp mid to avoid extreme inversions
        p = max(0.05, min(0.95, kalshi_mid_cents / 100.0))

        # Inverse Normal CDF (rational approximation)
        # Beasley-Springer-Moro algorithm
        d = self._inverse_norm_cdf(p)

        vol = self.volatility if self.volatility > 0 else 0.50
        t_years = seconds_left / (365 * 24 * 3600)
        sigma_sqrt_t = vol * math.sqrt(max(t_years, 1e-10))

        # K = S / exp(d * sigma * sqrt(T))
        self.strike = btc_spot / math.exp(d * sigma_sqrt_t) if sigma_sqrt_t > 0 else btc_spot

    @staticmethod
    def _inverse_norm_cdf(p: float) -> float:
        """Rational approximation of the inverse standard normal CDF."""
        import math
        if p <= 0.0:
            return -8.0
        if p >= 1.0:
            return 8.0
        if p == 0.5:
            return 0.0
        # Abramowitz & Stegun 26.2.23
        if p < 0.5:
            t = math.sqrt(-2.0 * math.log(p))
        else:
            t = math.sqrt(-2.0 * math.log(1.0 - p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        result = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)
        return result if p > 0.5 else -result

    def on_candle(self, candle) -> None:
        """Feed a 5s candle for volatility computation."""
        if candle.close > 0:
            self._vol_buffer.append(candle)

    def update(self, btc_price: float, contract_mid_cents: float, seconds_left: float) -> None:
        """Recompute fair value and mispricing.

        Args:
            btc_price: Current BTC spot price
            contract_mid_cents: Current Kalshi contract mid (0-100)
            seconds_left: Seconds until contract expires
        """
        if self.strike <= 0 or btc_price <= 0 or seconds_left <= 0:
            self.fair_value = 50.0
            self.mispricing = 0.0
            self.probability = 0.5
            return

        self.time_to_expiry_s = seconds_left

        # Compute realized volatility from 5s candle returns
        if len(self._vol_buffer) >= 10:
            returns = []
            candles = list(self._vol_buffer)
            for i in range(1, len(candles)):
                if candles[i-1].close > 0:
                    r = (candles[i].close - candles[i-1].close) / candles[i-1].close
                    returns.append(r)
            if returns:
                import math
                stdev = (sum(r*r for r in returns) / len(returns)) ** 0.5
                # Annualize: 5s candles, BTC trades 24/7 (not equity hours)
                # Old (WRONG): 252 * 6.5 * 3600 / 5.0 = equity hours, understates vol by 2.3x
                periods_per_year = 365 * 24 * 3600 / 5.0
                self.volatility = stdev * (periods_per_year ** 0.5)

        if self.volatility <= 0:
            self.volatility = 0.50  # Default 50% annualized if no data

        # Standardized distance to strike (Brownian Bridge)
        import math
        t_years = seconds_left / (365 * 24 * 3600)  # Convert seconds to BTC years (24/7)
        sigma_sqrt_t = self.volatility * math.sqrt(max(t_years, 1e-10))

        # d = (ln(S/K)) / (sigma * sqrt(T))
        d = math.log(btc_price / self.strike) / sigma_sqrt_t if sigma_sqrt_t > 0 else 0

        # Normal CDF via Abramowitz & Stegun erf approximation
        self.probability = 0.5 * (1.0 + self._erf(d / math.sqrt(2)))

        # Fair value in cents
        self.fair_value = self.probability * 100.0

        # Mispricing: positive = YES underpriced, negative = NO underpriced
        self.mispricing = self.fair_value - contract_mid_cents

    @staticmethod
    def _erf(x: float) -> float:
        """Abramowitz & Stegun approximation of erf(x). Max error 1.5e-7."""
        sign = 1 if x >= 0 else -1
        x = abs(x)
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (2.718281828 ** (-x * x))
        return sign * y

    @property
    def side(self) -> str:
        """Which side is underpriced based on fair value."""
        if self.mispricing > 5:  # YES underpriced by 5c+
            return "yes"
        elif self.mispricing < -5:  # NO underpriced by 5c+
            return "no"
        return ""  # No clear mispricing

    @property
    def edge_cents(self) -> float:
        """Absolute mispricing in cents."""
        return abs(self.mispricing)

    @property
    def is_ready(self) -> bool:
        return self.strike > 0 and len(self._vol_buffer) >= 10


class FiveSecIndicators:
    """5-second candle aggregator with Bollinger Bands, MACD, MA50, MA100.

    Fed tick-by-tick from Coinbase. Aggregates into 5s candles, computes
    indicators in real-time. Used as entry confirmation gate.
    """

    def __init__(self):
        from indicators import EMACalc, SMACalc
        # 5s candle buffer (keep 200 = 16.7 minutes)
        self._candles: deque = deque(maxlen=200)
        self._current_candle: FiveSecCandle = None
        self._candle_boundary: float = 0.0

        # Bollinger Bands (20, 2) on 5s closes
        self._bb_sma = SMACalc(20)
        self._bb_buffer: deque = deque(maxlen=20)

        # MACD (12, 26, 9) on 5s closes
        self._macd_fast = EMACalc(12)
        self._macd_slow = EMACalc(26)
        self._macd_signal = EMACalc(9)

        # MA50 and MA100 on 5s closes
        self._ma50 = SMACalc(50)
        self._ma100 = SMACalc(100)

        # Computed values (updated on each 5s close)
        self.bb_upper: float = 0.0
        self.bb_mid: float = 0.0
        self.bb_lower: float = 0.0
        self.bb_width: float = 0.0
        self.macd_line: float = 0.0
        self.macd_signal_line: float = 0.0
        self.macd_histogram: float = 0.0
        self.ma50: float = 0.0
        self.ma100: float = 0.0
        self.last_close: float = 0.0
        self.candles_processed: int = 0
        self.is_ready: bool = False  # True after 100 candles (~8.3 min warmup)
        self._on_candle_close = None  # Callback for level tracker

    def on_tick(self, price: float, ts: float) -> None:
        """Feed a BTC tick. Aggregates into 5s candles and updates indicators."""
        # Determine 5s boundary
        boundary = int(ts / 5.0) * 5.0

        if self._current_candle is None or boundary > self._candle_boundary:
            # Close previous candle and start new one
            if self._current_candle is not None:
                self._close_candle(self._current_candle)
            self._current_candle = FiveSecCandle(price, ts)
            self._candle_boundary = boundary
        else:
            self._current_candle.update(price)

    def _close_candle(self, candle: FiveSecCandle) -> None:
        """Process a completed 5s candle through all indicators."""
        self._candles.append(candle)
        close = candle.close
        self.last_close = close
        self.candles_processed += 1
        # Notify level tracker
        if self._on_candle_close:
            try:
                self._on_candle_close(candle)
            except Exception:
                pass

        # Bollinger Bands
        self._bb_buffer.append(close)
        bb_mean = self._bb_sma.update(close)
        if bb_mean is not None and len(self._bb_buffer) >= 20:
            variance = sum((p - bb_mean) ** 2 for p in self._bb_buffer) / len(self._bb_buffer)
            std = variance ** 0.5
            self.bb_upper = bb_mean + 2 * std
            self.bb_mid = bb_mean
            self.bb_lower = bb_mean - 2 * std
            self.bb_width = self.bb_upper - self.bb_lower

        # MACD
        fast = self._macd_fast.update(close)
        slow = self._macd_slow.update(close)
        self.macd_line = fast - slow
        self.macd_signal_line = self._macd_signal.update(self.macd_line)
        self.macd_histogram = self.macd_line - self.macd_signal_line

        # MAs
        ma50 = self._ma50.update(close)
        ma100 = self._ma100.update(close)
        if ma50 is not None:
            self.ma50 = ma50
        if ma100 is not None:
            self.ma100 = ma100

        self.is_ready = self.candles_processed >= 100

    @property
    def trend_up(self) -> bool:
        """MA50 > MA100 = uptrend."""
        return self.ma50 > self.ma100 > 0

    @property
    def trend_down(self) -> bool:
        """MA50 < MA100 = downtrend."""
        return 0 < self.ma50 < self.ma100

    @property
    def at_upper_bb(self) -> bool:
        """Price at or above upper Bollinger — overextended, don't chase."""
        return self.last_close >= self.bb_upper > 0

    @property
    def at_lower_bb(self) -> bool:
        """Price at or below lower Bollinger — potential dip buy."""
        return 0 < self.last_close <= self.bb_lower

    @property
    def bb_squeeze(self) -> bool:
        """Bands narrowing — volatility compression, breakout direction unknown."""
        if self.bb_mid <= 0:
            return False
        return self.bb_width / self.bb_mid < 0.001  # <0.1% width = squeeze

    @property
    def macd_bullish_cross(self) -> bool:
        """MACD line crossed above signal line."""
        return self.macd_histogram > 0 and self.macd_line > 0

    @property
    def macd_bearish_cross(self) -> bool:
        """MACD line crossed below signal line."""
        return self.macd_histogram < 0 and self.macd_line < 0

    @property
    def macd_fading(self) -> bool:
        """Histogram shrinking — momentum exhausting."""
        if len(self._candles) < 3:
            return False
        # Check last 3 histogram values
        recent = list(self._candles)[-3:]
        hists = []
        # Recompute histograms from candle closes would be expensive,
        # just check if current histogram is smaller than it was
        return abs(self.macd_histogram) < 0.5 and self.macd_histogram != 0

    def entry_allowed(self, side: str) -> tuple[bool, str]:
        """Check if 5s indicators confirm entry direction.

        Returns (allowed, reason). Only blocks when BTC is at Bollinger extreme
        (overextended). MA trend and MACD removed — too aggressive, blocked
        more winners than losers. The contract mid already picks direction.
        """
        if not self.is_ready:
            return True, "5s_warmup"

        # Block YES entry when BTC is at upper Bollinger (overextended up — chasing peak)
        if side == "yes" and self.at_upper_bb:
            return False, "5s_upper_bb"

        # Block NO entry when BTC is at lower Bollinger (overextended down — chasing bottom)
        if side == "no" and self.at_lower_bb:
            return False, "5s_lower_bb"

        return True, "5s_ok"

    # ── Dynamic Support/Resistance from 5s candle swing points ──

    def compute_sr_levels(self) -> dict:
        """Find dynamic S/R from swing highs/lows in the 5s candle buffer.

        A swing high = candle high > both neighbors' highs (local peak).
        A swing low = candle low < both neighbors' lows (local trough).
        Cluster within $30 tolerance. More touches = stronger level.

        Returns dict with 'support' and 'resistance' lists of (price, touches).
        """
        candles = list(self._candles)
        if len(candles) < 5:
            return {"support": [], "resistance": [], "nearest_support": 0, "nearest_resistance": 0}

        swing_highs = []
        swing_lows = []

        for i in range(2, len(candles) - 2):
            h = candles[i].high
            l = candles[i].low
            # Swing high: higher than 2 candles on each side
            if (h > candles[i-1].high and h > candles[i-2].high and
                h > candles[i+1].high and h > candles[i+2].high):
                swing_highs.append(h)
            # Swing low: lower than 2 candles on each side
            if (l < candles[i-1].low and l < candles[i-2].low and
                l < candles[i+1].low and l < candles[i+2].low):
                swing_lows.append(l)

        # Cluster within $30 tolerance
        def cluster(prices, tolerance=30.0):
            if not prices:
                return []
            prices.sort()
            clusters = []
            current = [prices[0]]
            for p in prices[1:]:
                if p - current[0] <= tolerance:
                    current.append(p)
                else:
                    clusters.append((sum(current) / len(current), len(current)))
                    current = [p]
            clusters.append((sum(current) / len(current), len(current)))
            # Sort by touches (strength) descending
            return sorted(clusters, key=lambda x: x[1], reverse=True)

        resistance = cluster(swing_highs)
        support = cluster(swing_lows)

        # Find nearest to current price
        price = self.last_close
        nearest_sup = 0
        nearest_res = 0
        if support and price > 0:
            nearest_sup = min(support, key=lambda x: abs(x[0] - price))[0]
        if resistance and price > 0:
            nearest_res = min(resistance, key=lambda x: abs(x[0] - price))[0]

        self.sr_levels = {
            "support": support[:5],  # Top 5 by strength
            "resistance": resistance[:5],
            "nearest_support": nearest_sup,
            "nearest_resistance": nearest_res,
        }
        return self.sr_levels

    def sr_context(self, price: float = 0) -> dict:
        """Get S/R context for a given BTC price (or last close).

        Returns distance to nearest support/resistance and whether
        price is near a level ($50 tolerance).
        """
        if not hasattr(self, 'sr_levels'):
            self.compute_sr_levels()

        p = price or self.last_close
        sr = self.sr_levels

        near_support = False
        near_resistance = False
        dist_to_support = 9999
        dist_to_resistance = 9999

        if sr["nearest_support"] > 0:
            dist_to_support = p - sr["nearest_support"]
            near_support = abs(dist_to_support) <= 50

        if sr["nearest_resistance"] > 0:
            dist_to_resistance = sr["nearest_resistance"] - p
            near_resistance = abs(dist_to_resistance) <= 50

        return {
            "near_support": near_support,
            "near_resistance": near_resistance,
            "dist_to_support": dist_to_support,
            "dist_to_resistance": dist_to_resistance,
            "support_levels": sr["support"],
            "resistance_levels": sr["resistance"],
        }


class BTCLevelTracker:
    """Tracks BTC support/resistance levels from 5s candle data.

    Maintains a 4-hour rolling buffer of 5s candles (2880 candles).
    Identifies levels where price reversed (swing highs/lows), clusters
    them, and detects when price touches a level with rejection signals.

    This is the foundation for level-based entries: buy the contract when
    BTC bounces off support, sell when it rejects at resistance.
    """

    def __init__(self):
        # 4hr buffer: 4*60*60/5 = 2880 candles
        self._candles: deque = deque(maxlen=2880)
        self._levels: dict = {"support": [], "resistance": []}
        self._last_recompute: float = 0.0
        self._recompute_interval: float = 30.0  # Recompute levels every 30s

        # Current setup state
        self.active_setup: dict = {}  # Populated when a tradeable setup detected
        self.last_price: float = 0.0

    def seed_from_1m_candles(self, candles) -> None:
        """Warm the level tracker from 1m REST candles on startup.

        Converts each 1m candle into a synthetic FiveSecCandle so the swing
        point detection works immediately. 25 candles = 25 minutes of levels.
        """
        for c in candles:
            synthetic = FiveSecCandle(c.open, c.timestamp / 1000.0 if c.timestamp > 1e12 else c.timestamp)
            synthetic.high = c.high
            synthetic.low = c.low
            synthetic.close = c.close
            synthetic.volume = int(c.volume / 12)  # Approximate per-5s volume
            self._candles.append(synthetic)
        if len(self._candles) >= 20:
            self._recompute_levels()
            self._last_recompute = time.time()
            logger.info("LevelTracker: seeded with %d 1m candles -> %d support, %d resistance levels",
                        len(candles), len(self._levels.get("support", [])),
                        len(self._levels.get("resistance", [])))

    def on_candle(self, candle) -> None:
        """Feed a closed 5s candle."""
        self._candles.append(candle)
        self.last_price = candle.close

        now = time.time()
        if now - self._last_recompute >= self._recompute_interval:
            self._recompute_levels()
            self._last_recompute = now

        self._detect_setup(candle)

    def _recompute_levels(self) -> None:
        """Find S/R levels from swing points in the 4hr buffer."""
        candles = list(self._candles)
        if len(candles) < 20:
            return

        swing_highs = []
        swing_lows = []

        # Use wider lookback for 5s candles — 5 candles each side (25s)
        for i in range(5, len(candles) - 5):
            h = candles[i].high
            l = candles[i].low

            is_swing_high = all(h >= candles[i+j].high for j in range(-5, 6) if j != 0)
            is_swing_low = all(l <= candles[i+j].low for j in range(-5, 6) if j != 0)

            if is_swing_high:
                swing_highs.append(h)
            if is_swing_low:
                swing_lows.append(l)

        # Cluster within $30
        def cluster(prices, tolerance=30.0):
            if not prices:
                return []
            prices.sort()
            clusters = []
            current = [prices[0]]
            for p in prices[1:]:
                if p - current[0] <= tolerance:
                    current.append(p)
                else:
                    clusters.append((sum(current) / len(current), len(current)))
                    current = [p]
            clusters.append((sum(current) / len(current), len(current)))
            return sorted(clusters, key=lambda x: x[1], reverse=True)[:10]

        self._levels = {
            "resistance": cluster(swing_highs),
            "support": cluster(swing_lows),
        }

    def _detect_setup(self, candle) -> None:
        """Check if current candle shows a level rejection setup.

        Setup = price within $30 of a tracked level + rejection candle
        (long wick away from level, close back toward it) + above-avg volume.
        """
        self.active_setup = {}
        price = candle.close
        body = abs(candle.close - candle.open)
        upper_wick = candle.high - max(candle.close, candle.open)
        lower_wick = min(candle.close, candle.open) - candle.low
        total_range = candle.high - candle.low

        if total_range < 1:  # Dead candle
            return

        # Average volume from recent candles
        recent = list(self._candles)[-60:] if len(self._candles) >= 60 else list(self._candles)
        avg_vol = sum(c.volume for c in recent) / max(len(recent), 1)

        # Check support levels — price touching from above, bouncing up
        for level_price, touches in self._levels.get("support", []):
            dist = price - level_price
            if 0 <= dist <= 50:  # Within $50 above support
                # Rejection: long lower wick (tested support and bounced)
                if lower_wick > body and lower_wick > upper_wick:
                    # Volume confirmation
                    vol_ok = candle.volume >= avg_vol * 0.8
                    self.active_setup = {
                        "type": "support_bounce",
                        "side": "yes",
                        "level": level_price,
                        "touches": touches,
                        "distance": dist,
                        "wick_ratio": lower_wick / total_range,
                        "volume_ratio": candle.volume / max(avg_vol, 1),
                        "volume_ok": vol_ok,
                        "price": price,
                        "ts": time.time(),
                    }
                    return

        # Check resistance levels — price touching from below, rejecting down
        for level_price, touches in self._levels.get("resistance", []):
            dist = level_price - price
            if 0 <= dist <= 50:  # Within $50 below resistance
                # Rejection: long upper wick (tested resistance and rejected)
                if upper_wick > body and upper_wick > lower_wick:
                    vol_ok = candle.volume >= avg_vol * 0.8
                    self.active_setup = {
                        "type": "resistance_reject",
                        "side": "no",
                        "level": level_price,
                        "touches": touches,
                        "distance": dist,
                        "wick_ratio": upper_wick / total_range,
                        "volume_ratio": candle.volume / max(avg_vol, 1),
                        "volume_ok": vol_ok,
                        "price": price,
                        "ts": time.time(),
                    }
                    return

    @property
    def support_levels(self):
        return self._levels.get("support", [])

    @property
    def resistance_levels(self):
        return self._levels.get("resistance", [])

    @property
    def has_setup(self) -> bool:
        return bool(self.active_setup)

    @property
    def candles_buffered(self) -> int:
        return len(self._candles)


class PriceFeedTask:
    """Manages WebSocket subscription for 1m/5m/15m candles,
    REST polling for 1h candles, and real-time BTC tick tracking.

    Start with asyncio.create_task(feed.run()).
    When MTF_ENABLED=False the task returns immediately (no-op).

    Callbacks registered via register_candle_close_callback() are invoked
    synchronously inside the asyncio event loop on each candle close.
    Callback signature: fn(tf: str, candle: Candle) -> None
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._symbol: str = _uc("PRICE_FEED_SYMBOL", "btcusdt").lower()
        self._ws_timeout: float = _uc("PRICE_FEED_WS_TIMEOUT_S", _WS_HEARTBEAT_TIMEOUT_S)

        self.state = PriceFeedState(
            buffers={tf: deque(maxlen=BUFFER_SIZES[tf]) for tf in BUFFER_SIZES},
            last_closed={tf: None for tf in BUFFER_SIZES},
            last_updated={tf: 0.0 for tf in BUFFER_SIZES},
        )

        # BTC tick tracker — window=500 holds ~10s of Coinbase ticks at 50/s
        self.tick_tracker = BTCTickTracker(window=500)
        # BTC volume profile — Coinbase matches channel feeds price/size/side
        # bins for VWAP, POC, cumulative-above/below, aggressor imbalance.
        # Default 15-min window matches one Kalshi BTC15M session; bucket
        # size $10 = ~0.013% of BTC at $77k.
        self.volume_tracker = BtcVolumeTracker(
            max_age_s=float(_uc("BTC_VOLUME_MAX_AGE_S", 900.0)),
            bucket_dollars=float(_uc("BTC_VOLUME_BUCKET_DOLLARS", 10.0)),
        )
        # 5s candle indicators (Bollinger, MACD, MA50, MA100)
        self.five_sec = FiveSecIndicators()
        # Level-based entry tracker (4hr S/R + setup detection)
        self.levels = BTCLevelTracker()
        # Binary probability engine (Brownian Bridge fair value)
        self.prob_engine = BinaryProbabilityEngine()
        # Wire 5s candle closes into level tracker AND prob engine
        def _on_5s_close(candle):
            self.levels.on_candle(candle)
            self.prob_engine.on_candle(candle)
        self.five_sec._on_candle_close = _on_5s_close

        # Combined stream names for WebSocket — add aggTrade for tick data
        self._ws_streams: list[str] = [
            f"{self._symbol}@kline_{tf}" for tf in _WS_TIMEFRAMES
        ] + [f"{self._symbol}@aggTrade"]

        # Per-timeframe callbacks: fn(tf, candle)
        self._callbacks: dict[str, list[Callable]] = {tf: [] for tf in BUFFER_SIZES}

        self._last_1h_poll: float = 0.0
        self._running: bool = True

    # ── Public API ────────────────────────────────────────────────────────────

    def register_candle_close_callback(self, tf: str, fn: Callable) -> None:
        """Register a callback that fires when a candle closes on the given TF.

        Callback signature: fn(tf: str, candle: Candle) -> None
        Multiple callbacks per TF are supported.
        """
        if tf in self._callbacks:
            self._callbacks[tf].append(fn)
        else:
            logger.warning("PriceFeed: unknown timeframe '%s' for callback", tf)

    def get_candles(self, tf: str, n: int) -> list[Candle]:
        """Return last n closed candles for the given timeframe (newest last)."""
        buf = self.state.buffers.get(tf)
        if buf is None:
            return []
        candles = list(buf)
        return candles[-n:] if n < len(candles) else candles

    def is_warm(self, tf: str, min_bars: int = 25) -> bool:
        """True if the buffer has at least min_bars for the timeframe."""
        return len(self.state.buffers.get(tf, [])) >= min_bars

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run forever: WebSocket + REST polling.

        When MTF_ENABLED=False this is a no-op (returns immediately).
        On WebSocket disconnect, buffers continue to be valid — REST poll
        for 1h keeps running; 1m/5m/15m go stale until WS reconnects.
        """
        if not _uc("MTF_ENABLED", True):
            logger.info("PriceFeed: MTF_ENABLED=False — price feed inactive")
            return

        # Warm up from REST before starting WebSocket (avoids 60-bar gap on startup)
        try:
            await asyncio.wait_for(self._warmup(), timeout=45)
        except asyncio.TimeoutError:
            logger.warning("PriceFeed: warmup timed out after 45s — continuing anyway")
        except Exception as e:
            logger.warning("PriceFeed: warmup error (non-fatal): %s", e)

        # 1h REST poll loop (runs concurrently with WebSocket)
        rest_task = asyncio.create_task(self._poll_rest_loop())
        # Coinbase real-time ticker — high-frequency BTC ticks for sniper signal
        coinbase_task = asyncio.create_task(self._run_coinbase_ticker())

        try:
            await self._run_ws_with_reconnect()
        except asyncio.CancelledError:
            raise
        finally:
            rest_task.cancel()
            coinbase_task.cancel()
            self.state.ws_connected = False

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def _run_ws_with_reconnect(self) -> None:
        """WebSocket with exponential backoff reconnect."""
        delay = _WS_RECONNECT_BASE_S
        while self._running:
            try:
                await self._run_websocket()
                # Clean close — reset backoff
                delay = _WS_RECONNECT_BASE_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state.ws_connected = False
                self.state.ws_reconnect_count += 1
                logger.warning(
                    "PriceFeed: WebSocket disconnected (reconnect #%d in %.0fs): %s",
                    self.state.ws_reconnect_count, delay, e,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                delay = min(delay * 2.0, _WS_RECONNECT_MAX_S)

    async def _run_websocket(self) -> None:
        """Open a combined stream WebSocket and process messages until disconnect."""
        streams = "/".join(self._ws_streams)
        url = f"{BINANCE_WS_URL}?streams={streams}"

        async with self._session.ws_connect(
            url,
            heartbeat=20,                                  # send ping every 20s
            timeout=aiohttp.ClientWSTimeout(ws_close=self._ws_timeout),
        ) as ws:
            self.state.ws_connected = True
            logger.info("PriceFeed: WebSocket connected to %d streams", len(self._ws_streams))
            last_msg_ts = time.time()

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        parsed = msg.json()
                        stream = parsed.get("stream", "")
                        if "aggTrade" in stream:
                            data = parsed.get("data", {})
                            price = float(data.get("p", 0))
                            ts = float(data.get("T", 0)) / 1000.0
                            if price > 0:
                                self.tick_tracker.add_tick(price, ts)
                                self.five_sec.on_tick(price, ts)
                        elif "kline" in stream:
                            # Feed tick tracker from EVERY kline message (not just closed)
                            data = parsed.get("data", parsed)
                            k = data.get("k", {})
                            if k:
                                kline_price = float(k.get("c", 0))
                                if kline_price > 0:
                                    _now = time.time()
                                    self.tick_tracker.add_tick(kline_price, _now)
                                    self.five_sec.on_tick(kline_price, _now)
                            else:
                                # Try top-level data
                                kline_price = float(data.get("c", 0)) if isinstance(data, dict) else 0
                                if kline_price > 0:
                                    self.tick_tracker.add_tick(kline_price, time.time())
                            await self._process_kline_message(parsed)
                        else:
                            await self._process_kline_message(parsed)
                        last_msg_ts = time.time()
                    except Exception as e:
                        logger.debug("PriceFeed: message parse error: %s", e)

                elif msg.type == aiohttp.WSMsgType.PING:
                    await ws.pong(msg.data)
                    last_msg_ts = time.time()

                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    logger.info("PriceFeed: WebSocket server-initiated close")
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning("PriceFeed: WebSocket error frame: %s", msg.data)
                    break

                # Heartbeat timeout — treat as stale connection
                if time.time() - last_msg_ts > self._ws_timeout:
                    logger.warning(
                        "PriceFeed: no message for %.0fs, forcing reconnect",
                        self._ws_timeout,
                    )
                    break

        self.state.ws_connected = False

    async def _process_kline_message(self, msg: dict) -> None:
        """Parse a Binance combined-stream kline message.

        Combined stream format: {"stream": "btcusdt@kline_1m", "data": {...}}
        The kline object is at msg["data"]["k"].
        """
        # Combined stream wraps individual stream in a "data" key
        data = msg.get("data", msg)
        if not isinstance(data, dict):
            return
        k = data.get("k")
        if not k:
            return

        # Only process closed candles (x=True means the candle is complete)
        if not k.get("x", False):
            return

        # Binance interval string maps 1:1 to our TF keys
        tf = k.get("i", "")
        if tf not in self.state.buffers:
            return

        try:
            candle = Candle(
                timestamp=int(k["t"]),
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
                closed=True,
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("PriceFeed: malformed kline for %s: %s", tf, e)
            return

        self._add_candle(tf, candle)

    # ── REST polling ──────────────────────────────────────────────────────────

    async def _poll_rest_loop(self) -> None:
        """Periodically poll Binance REST for 1h candles (every 15 min)."""
        while True:
            try:
                now = time.time()
                if now - self._last_1h_poll >= _1H_POLL_INTERVAL_S:
                    await self._fetch_rest_new_candles("1h", limit=5)
                    self._last_1h_poll = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("PriceFeed: 1h REST poll error: %s", e)
            await asyncio.sleep(60)

    async def _fetch_rest_new_candles(self, tf: str, limit: int) -> None:
        """Fetch recent candles from REST; add only those newer than buffer head."""
        last_ts = (
            self.state.last_closed[tf].timestamp
            if self.state.last_closed[tf] else 0
        )
        candles = await self._fetch_rest(tf, limit)
        new_count = 0
        for c in candles:
            if c.timestamp > last_ts:
                self._add_candle(tf, c)
                new_count += 1
        if new_count > 0:
            logger.debug("PriceFeed: %s REST poll added %d new candle(s)", tf, new_count)

    # ── Warmup ────────────────────────────────────────────────────────────────

    async def _warmup(self) -> None:
        """Bulk-fetch historical OHLCV for all TFs from REST (startup only).

        Callbacks are NOT fired during warmup to avoid premature scoring
        before all buffers are populated.
        """
        warmup_limits = {"1m": 60, "5m": 50, "15m": 30, "1h": 72}
        logger.info("PriceFeed: warming up candle buffers from REST...")

        tasks = {
            tf: self._fetch_rest(tf, limit)
            for tf, limit in warmup_limits.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for tf, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("PriceFeed: warmup %s failed: %s", tf, result)
                continue
            # Load into buffer without firing callbacks
            for c in result:
                self.state.buffers[tf].append(c)
            if result:
                self.state.last_closed[tf] = result[-1]
                self.state.last_updated[tf] = time.time()
            logger.info(
                "PriceFeed: %s warmed with %d candles (latest: %.2f)",
                tf, len(self.state.buffers[tf]),
                result[-1].close if result else 0.0,
            )

    # ── REST fetch primitive ──────────────────────────────────────────────────

    async def _fetch_rest(self, tf: str, limit: int) -> list[Candle]:
        """Fetch closed OHLCV candles from Binance REST API.

        Returns up to `limit` closed candles (current forming candle excluded).
        Requests limit+1 to ensure we have enough after stripping the open candle.
        """
        interval = _BINANCE_INTERVALS.get(tf, tf)
        symbol = self._symbol.upper()
        params = {"symbol": symbol, "interval": interval, "limit": limit + 1}

        try:
            async with self._session.get(
                BINANCE_REST_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("PriceFeed: REST %s HTTP %d", tf, resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            logger.debug("PriceFeed: REST %s fetch failed: %s", tf, e)
            return []

        if not isinstance(data, list):
            return []

        now_ms = int(time.time() * 1000)
        candles: list[Candle] = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            try:
                close_time_ms = int(row[6])
                if close_time_ms >= now_ms:
                    continue  # Still-forming candle — skip
                candles.append(Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    closed=True,
                ))
            except (ValueError, TypeError, IndexError):
                continue

        return candles

    # ── Buffer management ─────────────────────────────────────────────────────

    def _add_candle(self, tf: str, candle: Candle) -> None:
        """Append a closed candle to its buffer and fire all registered callbacks."""
        self.state.buffers[tf].append(candle)
        self.state.last_closed[tf] = candle
        self.state.last_updated[tf] = time.time()

        for fn in self._callbacks.get(tf, []):
            try:
                fn(tf, candle)
            except Exception as e:
                logger.debug("PriceFeed: callback error for %s: %s", tf, e)

    # ── Coinbase real-time ticker ────────────────────────────────────────────

    async def _run_coinbase_ticker(self) -> None:
        """Connect to Coinbase Advanced Trade WebSocket for real-time BTC ticks.
        Feeds tick_tracker with every trade — 10-50 ticks/second during active hours.
        This is the primary tick source for the sniper signal layer."""
        import json as _json
        _CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"
        _RECONNECT_BASE = 2.0
        _RECONNECT_MAX = 30.0

        delay = _RECONNECT_BASE
        while self._running:
            try:
                async with self._session.ws_connect(_CB_WS_URL, heartbeat=30) as ws:
                    # Subscribe to BTC-USD ticker AND matches.
                    # ticker  → tick_tracker + five_sec (price velocity)
                    # matches → volume_tracker (per-trade size + aggressor)
                    sub = {
                        "type": "subscribe",
                        "product_ids": ["BTC-USD"],
                        "channels": ["ticker", "matches"],
                    }
                    await ws.send_json(sub)
                    logger.info("PriceFeed: Coinbase ticker connected (BTC-USD)")
                    delay = _RECONNECT_BASE

                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = _json.loads(msg.data)
                                _t = data.get("type")
                                if _t == "ticker" and "price" in data:
                                    price = float(data["price"])
                                    if price > 0:
                                        _now = time.time()
                                        self.tick_tracker.add_tick(price, _now)
                                        self.five_sec.on_tick(price, _now)
                                elif _t in ("match", "last_match") and "size" in data and "price" in data:
                                    # 'match' fires on every trade; 'last_match'
                                    # fires once on subscribe with the most-
                                    # recent trade. Both have the same shape.
                                    try:
                                        _p = float(data["price"])
                                        _s = float(data["size"])
                                        _maker = data.get("side", "")
                                        if _p > 0 and _s > 0:
                                            self.volume_tracker.on_match(
                                                price=_p, size=_s,
                                                maker_side=_maker,
                                                ts=time.time(),
                                            )
                                    except (TypeError, ValueError):
                                        pass
                            except Exception:
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                            break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("PriceFeed: Coinbase ticker error: %s (reconnect in %.0fs)", e, delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                delay = min(delay * 2, _RECONNECT_MAX)
