# Multi-Timeframe Price Action — Architecture Notes
## Code-Level Design for KXBTC15M Integration

**Companion to**: `METHODOLOGY.md`
**Last updated**: 2026-04-01

---

## 1. System Data Flow

### Current Flow (baseline)

```
Binance REST (60s poll)
  └─ fetch_binance_1m_candles()
       └─ TAScorer.update(candle)         [ta_module.py:134]
            └─ TASignalResult             [composite_score, direction, confidence_tier]
                 └─ _evaluate_ta_forced_signal()   [polymarket_copy_engine.py:2537]
                      └─ CrossVenueSignal → _execute_signal()
```

### Target Flow (post-integration)

```
Binance WebSocket (real-time)
  └─ PriceFeedTask (async)
       ├─ 1m kline → CandleBuffer[1m] → TAScorer.update()     [existing path, faster]
       ├─ 5m kline → CandleBuffer[5m] → TFAnalyzer[5m]
       └─ 15m kline → CandleBuffer[15m] → TFAnalyzer[15m]

Binance REST (60s / 15m poll)
  ├─ 1h OHLCV → CandleBuffer[1h] → TFAnalyzer[1h]
  └─ 4h OHLCV → CandleBuffer[4h] → RegimeClassifier

Binance Futures REST (5m poll)
  └─ FundingRate + OpenInterest → RegimeClassifier

                    ┌─────────────────────────────────────┐
                    │        MTFConfluenceScorer           │
                    │  TAScorer (1m) ──── weight: 20%      │
                    │  TFAnalyzer[5m] ─── weight: 25%      │
                    │  TFAnalyzer[15m] ── weight: 30%      │
                    │  TFAnalyzer[1h] ─── weight: 15%      │
                    │  RegimeClassifier ── weight: 10%     │
                    │                                      │
                    │  get_score(side) → float 0–100       │
                    └──────────────┬──────────────────────┘
                                   │
                    PolymarketCopyEngine._execute_signal()
                    ├─ PRIMARY signal → MTF filter → size_mult
                    ├─ TREND_FOLLOW  → MTF filter → size_mult
                    ├─ MIMIC         → MTF filter → size_mult
                    ├─ ALGO          → MTF filter → size_mult
                    └─ TA_FORCED     → MTF gate (score ≥ 40 required)
```

---

## 2. Proposed Module Structure

### 2.1 `price_feed.py` — Real-Time OHLCV Feed

**Responsibility**: Maintain rolling candle buffers for all timeframes. Single source of truth for all price data.

```python
# price_feed.py

from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import asyncio
import aiohttp
from models import Candle

BINANCE_WS_URL = "wss://stream.binance.us:9443/stream"
BINANCE_REST_URL = "https://api.binance.us/api/v3/klines"

BUFFER_SIZES = {
    "1m":  60,   # 60 bars: enough for EMA(20) vol SMA + recent structure
    "5m":  50,   # 50 bars: ~4h history, EMA(21) warmup
    "15m": 30,   # 30 bars: ~7.5h history, EMA(21) + BB(20) warmup
    "1h":  72,   # 72 bars: 3 days, EMA(50) warmup
    "4h":  60,   # 60 bars: 10 days, EMA(50) warmup
}


@dataclass
class PriceFeedState:
    """Snapshot of current candle state across all timeframes.

    Read by MTFConfluenceScorer. Updated by PriceFeedTask.
    Thread-safe reads assumed (GIL + asyncio single-thread).
    """
    buffers: dict[str, deque[Candle]] = field(default_factory=dict)
    last_closed: dict[str, Optional[Candle]] = field(default_factory=dict)
    last_updated: dict[str, float] = field(default_factory=dict)
    ws_connected: bool = False
    ws_reconnect_count: int = 0


class PriceFeedTask:
    """Manages WebSocket subscription for 1m/5m/15m candles
    and REST polling for 1h/4h candles.

    Usage:
        feed = PriceFeedTask(session)
        asyncio.create_task(feed.run())
        state = feed.state  # read by MTFConfluenceScorer
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.state = PriceFeedState(
            buffers={tf: deque(maxlen=BUFFER_SIZES[tf]) for tf in BUFFER_SIZES},
            last_closed={tf: None for tf in BUFFER_SIZES},
            last_updated={tf: 0.0 for tf in BUFFER_SIZES},
        )
        self._ws_streams = ["btcusdt@kline_1m", "btcusdt@kline_5m", "btcusdt@kline_15m"]
        self._last_1h_fetch: float = 0.0
        self._last_4h_fetch: float = 0.0
        self._on_candle_close_callbacks: list = []  # notify engine on 1m close

    async def run(self) -> None:
        """Main loop: WebSocket for 1m/5m/15m, REST poll for 1h/4h."""
        ...

    async def _run_websocket(self) -> None:
        """Subscribe to combined stream, process kline messages."""
        ...

    async def _process_kline_message(self, msg: dict) -> None:
        """Parse Binance kline message, update buffer if candle closed."""
        ...

    async def _poll_higher_timeframes(self) -> None:
        """REST fetch 1h candles every 15m, 4h candles every 60m."""
        ...

    async def warmup(self) -> None:
        """On startup: fetch historical REST data for all timeframes."""
        ...

    def register_candle_close_callback(self, tf: str, fn) -> None:
        """Register callback to fire when a candle closes on given timeframe."""
        ...

    def get_candles(self, tf: str, n: int) -> list[Candle]:
        """Return last n closed candles for given timeframe."""
        return list(self.state.buffers[tf])[-n:]
```

**Integration point**: `PriceFeedTask` replaces the `fetch_binance_1m_candles` calls in `polymarket_copy_engine.py`. The existing `_ta_candle_task` method (around line 2354) would be simplified to just call `feed.get_candles("1m", 25)` and feed them to `TAScorer`.

### 2.2 `mtf_confluence.py` — Multi-Timeframe Analysis

**Responsibility**: Compute per-timeframe directional signals and combine into a confluence score.

```python
# mtf_confluence.py

from dataclasses import dataclass
from typing import Optional
from indicators import EMACalc, RSICalc, SMACalc
from models import Candle
from price_feed import PriceFeedState

# Timeframe weights — must sum to 100
TF_WEIGHTS = {
    "1m":  20,
    "5m":  25,
    "15m": 30,
    "1h":  15,
    "4h":  10,
}


@dataclass
class TFSignal:
    """Directional signal for a single timeframe."""
    timeframe: str
    direction: str          # "up", "down", "flat"
    confidence: float       # 0–100
    ema_cross: str          # "bull", "bear", "flat"
    ema_cross_bars: int     # bars since last cross (freshness)
    rsi: float              # current RSI value
    rsi_direction: str      # "up", "down" (slope)
    above_vwap: Optional[bool]   # None if VWAP not computed
    last_candle_pressure: float  # (close-open)/range of last closed bar
    structure: str          # "bull" (HH/HL), "bear" (LH/LL), "flat"
    is_warm: bool           # enough bars processed for reliable signal


@dataclass
class ConfluenceResult:
    """Output of MTFConfluenceScorer for one evaluation."""
    side: str               # "yes" or "no" — which side was evaluated
    score: float            # 0–100 confluence score
    tf_signals: dict[str, TFSignal]
    regime: str             # "IMPULSE_BULL", "IMPULSE_BEAR", "CHOP", etc.
    regime_multiplier: float   # 1.0–1.2 for favorable regime, 0.8 for chop
    veto: bool              # True if score < 20 (hard opposing structure)
    reasoning: str          # human-readable log string


class TFAnalyzer:
    """Incremental TA for a single timeframe.

    Wraps EMA, RSI, and structure tracking for one timeframe.
    Feed closed candles via update(). Call get_signal() for current state.
    """

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe

        # EMA pair: (9, 21) for 5m/15m; (20, 50) for 1h/4h
        _fast, _slow = (9, 21) if timeframe in ("5m", "15m") else (20, 50)
        self._ema_fast = EMACalc(_fast)
        self._ema_slow = EMACalc(_slow)

        self._rsi = RSICalc(14)
        self._vol_sma = SMACalc(20)

        # Structure tracking
        self._close_history: deque[float] = deque(maxlen=8)
        self._high_history: deque[float] = deque(maxlen=8)
        self._low_history: deque[float] = deque(maxlen=8)

        # EMA cross state
        self._ema_cross_direction: str = "flat"
        self._ema_cross_bars: int = 0
        self._prev_ema_above: Optional[bool] = None

        # VWAP (session)
        self._vwap_numerator: float = 0.0
        self._vwap_denominator: float = 0.0

        # RSI slope
        self._prev_rsi: Optional[float] = None

        self._bars_processed: int = 0
        self._last_signal: Optional[TFSignal] = None

    def update(self, candle: Candle) -> TFSignal:
        """Feed one closed candle, return current signal."""
        ...

    def reset_session(self) -> None:
        """Reset VWAP at session boundary (UTC midnight)."""
        self._vwap_numerator = 0.0
        self._vwap_denominator = 0.0

    def _compute_structure(self) -> str:
        """Classify market structure from recent highs/lows."""
        ...

    @property
    def is_warm(self) -> bool:
        return self._bars_processed >= 25  # enough for EMA(21) convergence

    def get_signal(self) -> Optional[TFSignal]:
        return self._last_signal


class RegimeClassifier:
    """4h-based macro regime classification.

    Consumes TFAnalyzer[4h] output + funding rate + OI signals.
    Updates slowly (changes every few hours at most).
    """

    REGIMES = {
        "IMPULSE_BULL",   # 4h EMA bull + recent swing high
        "PULLBACK_BULL",  # 4h EMA bull + retracing
        "IMPULSE_BEAR",   # 4h EMA bear + recent swing low
        "PULLBACK_BEAR",  # 4h EMA bear + retracing
        "CHOP",           # EMAs converging, no clear BOS
        "REVERSAL",       # CHoCH identified
    }

    def __init__(self) -> None:
        self._4h_analyzer = TFAnalyzer("4h")
        self._current_regime: str = "CHOP"
        self._regime_since: float = 0.0
        self._funding_rate: float = 0.0
        self._open_interest: float = 0.0
        self._prev_oi: float = 0.0

    def update_candle(self, candle: Candle) -> None:
        """Feed a 4h candle."""
        ...

    def update_funding(self, rate: float, oi: float) -> None:
        """Update funding rate and OI from Binance futures."""
        ...

    @property
    def regime(self) -> str:
        return self._current_regime

    @property
    def multiplier(self) -> dict[str, float]:
        """Returns {yes: float, no: float} size multipliers for current regime."""
        mult = {"yes": 1.0, "no": 1.0}
        if self._current_regime == "IMPULSE_BULL":
            mult["yes"] = 1.15
            mult["no"]  = 0.80
        elif self._current_regime == "IMPULSE_BEAR":
            mult["yes"] = 0.80
            mult["no"]  = 1.15
        elif self._current_regime == "CHOP":
            mult["yes"] = 0.90
            mult["no"]  = 0.90
        elif self._current_regime == "REVERSAL":
            mult["yes"] = 0.85
            mult["no"]  = 0.85
        return mult


class MTFConfluenceScorer:
    """Combines per-timeframe signals into a single confluence score.

    Usage in PolymarketCopyEngine:
        self._mtf = MTFConfluenceScorer()
        # In _execute_signal():
        result = self._mtf.evaluate(side=signal.kalshi_side)
        size_mult = _mtf_size_multiplier(result.score)
    """

    def __init__(self) -> None:
        # 1m is handled by existing TAScorer — we read its result directly
        self._5m_analyzer  = TFAnalyzer("5m")
        self._15m_analyzer = TFAnalyzer("15m")
        self._1h_analyzer  = TFAnalyzer("1h")
        self._regime       = RegimeClassifier()
        self._shadow_mode  = True  # Phase 2: log only, don't gate

    def on_candle(self, tf: str, candle: Candle) -> None:
        """Route closed candles to appropriate analyzers."""
        if tf == "5m":
            self._5m_analyzer.update(candle)
        elif tf == "15m":
            self._15m_analyzer.update(candle)
        elif tf == "1h":
            self._1h_analyzer.update(candle)
        elif tf == "4h":
            self._regime.update_candle(candle)

    def evaluate(self, side: str, ta_result=None) -> ConfluenceResult:
        """Compute confluence score for given side ("yes" or "no").

        Args:
            side: "yes" or "no"
            ta_result: TASignalResult from existing TAScorer (1m signal)
        """
        direction = "up" if side == "yes" else "down"

        # Collect signals
        signals: dict[str, TFSignal] = {}

        # 1m: use existing TAScorer result
        if ta_result is not None:
            signals["1m"] = _ta_result_to_tf_signal(ta_result)

        # 5m/15m/1h
        for tf, analyzer in [
            ("5m", self._5m_analyzer),
            ("15m", self._15m_analyzer),
            ("1h", self._1h_analyzer),
        ]:
            sig = analyzer.get_signal()
            if sig is not None:
                signals[tf] = sig

        # Compute weighted score
        score = 0.0
        for tf, weight in TF_WEIGHTS.items():
            if tf == "4h":
                # Handled by regime multiplier
                regime_mult = self._regime.multiplier[side]
                continue
            sig = signals.get(tf)
            if sig is None or not sig.is_warm:
                # Missing TF: treat as neutral (contribute 50% of weight)
                score += weight * 0.50
                continue
            tf_agreement = _compute_tf_agreement(sig, direction)  # 0.0 – 1.0
            score += weight * tf_agreement

        # Apply regime multiplier
        score = min(100.0, score * regime_mult)

        # Determine veto
        veto = score < 20.0

        result = ConfluenceResult(
            side=side,
            score=score,
            tf_signals=signals,
            regime=self._regime.regime,
            regime_multiplier=regime_mult,
            veto=veto,
            reasoning=_build_reasoning(signals, score, self._regime.regime),
        )

        return result

    def set_shadow_mode(self, enabled: bool) -> None:
        self._shadow_mode = enabled


def _compute_tf_agreement(signal: TFSignal, direction: str) -> float:
    """Compute 0.0–1.0 agreement score between a TF signal and a desired direction."""
    ...


def _ta_result_to_tf_signal(ta) -> TFSignal:
    """Convert existing TASignalResult to TFSignal for uniform handling."""
    ...


def _build_reasoning(signals, score, regime) -> str:
    """Build a compact log string for signal diagnostics."""
    ...


def mtf_size_multiplier(score: float) -> float:
    """Map confluence score to position size multiplier."""
    if score >= 80:  return 1.00
    if score >= 60:  return 0.85
    if score >= 40:  return 0.65
    if score >= 20:  return 0.40
    return 0.0  # veto
```

### 2.3 Integration into `polymarket_copy_engine.py`

**Init changes** (~line 535–550):

```python
# After existing TA scorer init:
from price_feed import PriceFeedTask
from mtf_confluence import MTFConfluenceScorer

# In __init__():
self._price_feed = PriceFeedTask(self._session)
self._mtf = MTFConfluenceScorer()

# Register candle close callbacks to feed MTF scorer:
self._price_feed.register_candle_close_callback("5m",  self._mtf.on_candle)
self._price_feed.register_candle_close_callback("15m", self._mtf.on_candle)
self._price_feed.register_candle_close_callback("1h",  self._mtf.on_candle)
self._price_feed.register_candle_close_callback("4h",  self._mtf.on_candle)
```

**Startup task changes** (in `run()` method, ~line 1185):

```python
# Add price feed task alongside existing async tasks:
async def run(self):
    ...
    await asyncio.gather(
        self._price_feed.run(),         # NEW: WebSocket price feed
        self._poll_loop(),              # existing
        self._tape_poll_loop(),         # existing
        self._scorer_loop(),            # existing
        self._whale_loop(),             # existing
    )
```

**Signal execution changes** (in `_execute_signal()`, ~line 2700):

```python
def _execute_signal(self, signal: CrossVenueSignal) -> None:
    ...
    # NEW: MTF confluence filter
    ta = self._ta_scorer.last_result
    mtf_result = self._mtf.evaluate(side=signal.kalshi_side, ta_result=ta)

    if self._mtf.shadow_mode:
        # Shadow mode: log but don't gate
        logger.info(
            "CopyEngine MTF SHADOW: side=%s score=%.0f regime=%s veto=%s | %s",
            signal.kalshi_side, mtf_result.score, mtf_result.regime,
            mtf_result.veto, mtf_result.reasoning,
        )
        size_mult = 1.0  # no adjustment in shadow mode
    else:
        if mtf_result.veto and signal.signal_tier == "TA_FORCED":
            logger.info("CopyEngine MTF VETO: TA_FORCED blocked (score=%.0f)", mtf_result.score)
            return  # TA_FORCED requires minimum confluence
        size_mult = mtf_size_multiplier(mtf_result.score)

    # Apply size_mult to contract count calculation (BEFORE _place_ladder call)
    ...
```

**Window reset changes** (in `_on_new_window()`, ~line 1281):

```python
def _on_new_window(self):
    ...
    self._ta_scorer.soft_reset()    # existing
    self._mtf._15m_analyzer  # soft reset cycle tracking (not full reset)
    # Note: 5m/1h/4h analyzers do NOT reset on 15m window — their state persists
```

---

## 3. Candle Microstructure Additions to Existing TAScorer

The existing `TAScorer` (`ta_module.py`) can be extended with two new tracking components without breaking the current interface. These are **additive only** — they add fields to `TASignalResult` but don't change existing fields.

### 3.1 Consecutive Close Direction Tracking

Add to `TAScorer.__init__()`:
```python
self._close_hist: deque[float] = deque(maxlen=5)
self._consecutive_bull: int = 0
self._consecutive_bear: int = 0
```

Add to `TAScorer.update()`:
```python
self._close_hist.append(c.close)
if len(self._close_hist) >= 2:
    if c.close > self._close_hist[-2]:
        self._consecutive_bull += 1
        self._consecutive_bear = 0
    elif c.close < self._close_hist[-2]:
        self._consecutive_bear += 1
        self._consecutive_bull = 0
```

Expose in `TASignalResult`:
```python
consecutive_bull: int = 0   # bars of consecutive higher closes
consecutive_bear: int = 0   # bars of consecutive lower closes
```

Value: `consecutive_bull >= 3` is a strong momentum confirmation for YES entries. `consecutive_bear >= 3` for NO entries.

### 3.2 EMA Cross Freshness

Add to `TAScorer`:
```python
self._ema_cross_bars: int = 0
self._prev_ema_above: Optional[bool] = None
```

In `update()`:
```python
fast_above = ema_fast_val > ema_slow_val
if self._prev_ema_above is not None and fast_above != self._prev_ema_above:
    self._ema_cross_bars = 1  # fresh cross
else:
    self._ema_cross_bars += 1
self._prev_ema_above = fast_above
```

Value: `ema_cross_bars == 1` means the cross just happened — strongest signal. `ema_cross_bars > 5` means the trend is established. `ema_cross_bars > 15` means the signal is aging and a counter-cross is more likely.

---

## 4. Database Schema Extension

Add `mtf_score` and `mtf_regime` columns to `kalshi_trades` table for outcome analysis:

```sql
-- Migration: add MTF columns to existing trades table
ALTER TABLE kalshi_trades ADD COLUMN mtf_score REAL;
ALTER TABLE kalshi_trades ADD COLUMN mtf_regime TEXT;

-- Analysis query: WR by MTF score bucket
SELECT
    CASE
        WHEN mtf_score >= 80 THEN '80-100 (strong)'
        WHEN mtf_score >= 60 THEN '60-79 (good)'
        WHEN mtf_score >= 40 THEN '40-59 (moderate)'
        WHEN mtf_score IS NULL THEN 'pre-MTF'
        ELSE '0-39 (weak)'
    END AS mtf_bucket,
    COUNT(*) AS trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM kalshi_trades
WHERE status NOT IN ('pending', 'unfilled')
GROUP BY 1
ORDER BY 1;
```

This query will validate (or invalidate) the confluence score hypothesis after ~2 weeks of shadow-mode data collection.

---

## 5. File Dependency Map

```
price_feed.py
  ├── depends on: aiohttp, models.Candle
  └── provides: PriceFeedState, PriceFeedTask

mtf_confluence.py
  ├── depends on: indicators.{EMACalc, RSICalc, SMACalc}, models.Candle, price_feed.PriceFeedState
  ├── reads: ta_module.TASignalResult (from existing TAScorer)
  └── provides: MTFConfluenceScorer, ConfluenceResult, TFSignal, RegimeClassifier

polymarket_copy_engine.py (modified)
  ├── imports ADD: price_feed.PriceFeedTask, mtf_confluence.MTFConfluenceScorer
  ├── init ADD: self._price_feed, self._mtf
  ├── run() ADD: price feed task in asyncio.gather()
  ├── _execute_signal() MODIFY: MTF filter application
  └── _on_new_window() MODIFY: MTF soft reset

ta_module.py (minor extension, backward-compatible)
  ├── TASignalResult ADD: consecutive_bull, consecutive_bear, ema_cross_bars
  └── TAScorer ADD: cross freshness + consecutive close tracking

signal_logger.py (minor extension)
  └── INSERT: mtf_score + mtf_regime fields in trade record
```

No existing files are removed or broken. New files `price_feed.py` and `mtf_confluence.py` are additive. The only modifications to existing files are:
- `ta_module.py`: new fields on `TASignalResult` (backward-compatible dataclass extension)
- `polymarket_copy_engine.py`: new init attributes, new task, gate in `_execute_signal()`
- `signal_logger.py`: two new nullable DB columns

---

## 6. Testing Strategy

### 6.1 Unit Tests (add to `tests/`)

```
tests/test_price_feed.py
  - test_candle_buffer_maxlen: buffer respects maxlen, oldest dropped
  - test_candle_closed_filter: open candles not added to buffer
  - test_ws_reconnect: disconnection triggers reconnect, no data loss

tests/test_mtf_confluence.py
  - test_confluence_all_bull: all TFs bullish → score ~90 for YES
  - test_confluence_all_bear: all TFs bearish → score ~10 for YES
  - test_confluence_mixed: 2 bull / 2 bear TFs → score ~50
  - test_veto_threshold: score < 20 → veto=True
  - test_missing_tf: TF without warm data contributes neutral 50%
  - test_size_multiplier: score→multiplier mapping

tests/test_tf_analyzer.py
  - test_ema_cross_bull: fast>slow → "bull" cross
  - test_ema_cross_freshness: cross_bars increments, resets on new cross
  - test_structure_bull: 4 consecutive HH/HL → "bull" structure
  - test_structure_bear: 4 consecutive LH/LL → "bear" structure
  - test_rsi_direction: rising RSI gives "up" slope
```

### 6.2 Shadow Mode Validation

Before enabling MTF gating, run in shadow mode for **minimum 200 trades** (approximately 1–2 weeks at current volume). The shadow mode logs:

```
CopyEngine MTF SHADOW: side=yes score=72 regime=PULLBACK_BULL veto=False | 1m:up(62) 5m:up(81) 15m:up(55) 1h:up(40)
```

Post-collection analysis using the DB query in Section 4 should show:
- **Hypothesis to validate**: `mtf_score >= 60` trades have WR ≥ 5pp higher than `mtf_score < 40` trades
- **If validated**: Enable Phase 3 (TA_FORCED gate + size multiplier)
- **If not validated**: MTF score has no predictive value at this scale — do not implement gate

### 6.3 Regression Safety

The MTF gate must never affect PRIMARY or TREND_FOLLOW signals in Phase 3 — only TA_FORCED. Add this invariant as a test:

```python
def test_primary_signal_not_blocked_by_mtf():
    """PRIMARY tier signal must execute regardless of MTF score."""
    signal = CrossVenueSignal(signal_tier="PRIMARY", ...)
    # Even with score=0, PRIMARY should not be vetoed
    assert engine._mtf_should_block(signal) == False
```

---

## 7. Configuration Additions to `user_config.py`

```python
# user_config.py additions (Phase 2 onwards)

# MTF confluence control
MTF_ENABLED = False             # Phase 1: feed only (no scoring)
MTF_SHADOW_MODE = True          # Phase 2: score but don't gate
MTF_TA_FORCED_MIN_SCORE = 40    # Phase 3: minimum confluence for TA_FORCED
MTF_SIZE_ADJUSTMENT = True      # Phase 3: apply size multiplier to all tiers

# WebSocket vs REST for 1m candles
PRICE_FEED_WEBSOCKET = True     # True: use WS; False: fall back to REST poll
PRICE_FEED_WS_TIMEOUT_S = 30.0  # WS reconnect if no message for 30s
```

This keeps the MTF system gated behind explicit config flags — the existing behavior is unchanged when `MTF_ENABLED = False`.

---

## 8. Key Line References in Existing Engine

| Location | Line (approx.) | Relevance |
|----------|---------------|-----------|
| `TAScorer` init | `ta_module.py:87` | Where new fields are added |
| `TAScorer.update()` | `ta_module.py:134` | Where cross freshness + consecutive close tracking is added |
| `fetch_binance_1m_candles` | `ta_module.py:246` | Replaced by `PriceFeedTask` WebSocket in Phase 1 |
| `_ta_scorer` init | `polymarket_copy_engine.py:538` | Where `MTFConfluenceScorer` init is added |
| `_on_new_window()` | `polymarket_copy_engine.py:1281` | Soft reset for 15m TFAnalyzer |
| `_evaluate_ta_forced_signal()` | `polymarket_copy_engine.py:2537` | MTF score gate inserted here in Phase 3 |
| `_execute_signal()` | `polymarket_copy_engine.py:~2700` | Size multiplier application point |
| `TA_FORCED_ENABLED` | `polymarket_copy_engine.py:296` | Existing gate; MTF gate is in addition to this |
| `_ta_candle_task` | `polymarket_copy_engine.py:~2350` | Replaced by `PriceFeedTask` registration |
| `asyncio.gather()` in `run()` | `polymarket_copy_engine.py:~1185` | `price_feed.run()` added here |
