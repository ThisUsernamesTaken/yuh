# Multi-Timeframe Price Action Methodology
## BTC 15-Minute Binary Contract (KXBTC15M) — Entry Decision Framework

**Document scope**: How to capture, analyze, and act on BTC price action across multiple timeframes
**Target contract**: Kalshi `KXBTC15M` — binary YES/NO on whether BTC is higher or lower at the end of each 15-minute window
**Last updated**: 2026-04-01

---

## 1. Current State Assessment

### 1.1 What Price Action Data the Engine Currently Uses

The `PolymarketCopyEngine` is primarily a **cross-venue flow-copy engine**, not a price action engine. Its price action inputs are narrow and deliberately secondary to wallet-flow signals.

#### Active data sources (as of 2026-03-30):

| Source | What it provides | How it's used |
|--------|-----------------|---------------|
| **Binance BTCUSDT 1m REST** (`fetch_binance_1m_candles`) | Last 25 closed 1m OHLCV candles | Feeds `TAScorer` for the `TA_FORCED` fallback tier |
| **Kalshi tape mid-price** (`KalshiTapeState.mid_price_cents`) | Current contract bid/ask midpoint | Direction filter in `_evaluate_ta_forced_signal()`: mid ≥55c → YES, ≤45c → NO |
| **Polymarket flow conviction** (`SmartFlowState.flow_conviction`) | WR-weighted wallet allocation ratio | Primary directional signal for all non-TA tiers |
| **Whale monitor** (`WhaleFlowState`) | Large mempool BTC transactions | TA inversion input (if whale opposes TA, downweight) |

#### Current `TAScorer` components (`ta_module.py:30-36`):

| Component | Weight | What it measures |
|-----------|--------|-----------------|
| Cycle return | 120.0 | % move from window open to current close |
| EMA spread | 200.0 | EMA(5) vs EMA(13) divergence |
| RSI bias | 25.0 | RSI(7) normalized around 50 |
| Candle pressure | 15.0 | (close−open) / range — bullish or bearish body |
| Relative volume | 10.0 | Current volume vs 20-bar SMA |

The composite is smoothed with EMA(2) and mapped to tiers: STRONG (≥75), MEDIUM (≥50), WEAK (≥20), MIMIC (≥1).

### 1.2 Limitations of the Current Approach

#### Critical gaps:

1. **Single timeframe only**: `TAScorer` processes only 1m candles. There is no 5m, 15m, 1h, or 4h awareness. A 1m signal firing into a 4h downtrend carries fundamentally different risk than one aligned with the higher structure.

2. **No structural context**: The engine knows nothing about nearby support/resistance levels, prior swing highs/lows, or whether the 15m candle is forming a rejection or continuation. A `TA_FORCED` buy signal at 48c while BTC sits at hourly resistance is treated identically to one at 48c while BTC bounces off a clean demand zone.

3. **No volume profile**: Relative volume is tracked (1m bar vs 20-bar SMA) but there is no concept of Point of Control, value area, or volume node clusters. These are predictive for 15m binary outcomes because contracts often pin near high-volume nodes.

4. **Candle fetch is polling, not streaming**: `fetch_binance_1m_candles` is a REST poll called every 60s (`TA_FORCED_CANDLE_INTERVAL_S = 60.0`). This means the TA state can be up to 59s stale when a signal fires. For a 15-minute contract this is significant.

5. **No regime detection**: The engine has no concept of whether BTC is trending, ranging, or at an inflection. A contract entered during a clean impulse leg should behave very differently from one entered during choppy consolidation.

6. **TA tier is a fallback, not a filter**: `TA_FORCED` only fires when all wallet tiers (PRIMARY, TREND_FOLLOW, MIMIC, ALGO) return None. This means the richest price action data is used only when smart money is silent — precisely when the market structure signal would be most valuable as confirmation for the wallet signals.

7. **Inversion logic is coarse**: The current inversion (`TA_INVERSION_ENABLED`) counts opposing signals (whale, tape, flow) but has no concept of timeframe confluence. A whale signal opposing TA has a different meaning than if 1h structure also opposes it.

---

## 2. Multi-Timeframe Price Action Framework

### 2.1 Why Multiple Timeframes Matter for KXBTC15M

The KXBTC15M contract has a fixed 15-minute resolution. The directional outcome is determined by whether BTC is higher or lower at window close. Each timeframe answers a different question about whether that outcome is probable:

| Timeframe | Question it answers | Relevance to 15m binary |
|-----------|---------------------|------------------------|
| **1m** | What is momentum doing right now? | Immediate entry timing; confirms or denies the trade within the first candle |
| **5m** | Is there a short-term trend developing? | ~3 bars per contract window; shows whether a move has follow-through |
| **15m** | What is the candle structure at contract resolution? | Direct alignment — this IS the contract's timeframe |
| **1h** | Where are the meaningful price levels? | Support/resistance that can halt or accelerate a 15m move |
| **4h** | What is the macro regime? | Determines whether longs or shorts have structural tailwind |

### 2.2 Timeframe-by-Timeframe Analysis

#### 2.2.1 — 1m: Immediate Momentum and Microstructure

**Role**: Entry timing, momentum confirmation, fast-failure detection

The 1m chart provides the most granular read on what the market is doing in the seconds around entry. Key signals:

- **EMA(5)/EMA(13) cross direction**: Already computed in `TAScorer`. A fresh cross in the direction of trade is a momentum confirmation. An aging cross losing slope is a warning.
- **1m candle pressure**: Bullish/bearish body ratio (`(close−open)/range`) — currently computed in `TAScorer`. A ≥0.6 bullish body entering a YES contract suggests genuine buyers; a doji or bearish body at entry is warning.
- **RSI(7) position**: Already in `TAScorer`. RSI >55 for YES entries and <45 for NO entries provides immediate momentum confirmation. RSI between 45–55 at entry means momentum is flat — lower-quality entry.
- **Volume surge**: Relative volume >1.5x average on the entry candle confirms genuine participation, not thin-market drift.
- **Microstructure (not currently implemented)**: Whether the last 3–5 1m closes are making higher highs/lows (YES) or lower highs/lows (NO). This is the most direct read on immediate momentum.

**Current coverage**: ✓ Partially implemented in `TAScorer`. Missing: microstructure (HH/HL vs LH/LL tracking), per-candle body analysis, directional consistency streak.

#### 2.2.2 — 5m: Short-Term Trend Confirmation

**Role**: Confirms whether a 1m momentum signal is part of a larger move or just noise

The 5m timeframe contains ~3 candles per KXBTC15M window. This is enough to identify a short-term trend or a reversal. Key signals:

- **EMA(9)/EMA(21) slope**: Is the short-term trend pointing in the same direction as the trade? A 5m EMA stack (9 above 21 for longs) that's been in place for 2+ bars means the trend is established, not nascent.
- **5m candle structure**: The last completed 5m candle tells whether the current bar is an inside bar (continuation indecision), a strong trend candle (momentum), or a reversal candle (opposing pressure). An engulfing candle against the trade direction on the 5m is a hard veto.
- **5m RSI(14)**: RSI in the 40–60 zone on a 5m chart means no strong momentum either direction. RSI >60 for YES or <40 for NO on 5m is a strong confirmation of entry direction.
- **VWAP relationship**: Is price above or below the 5m VWAP? For YES (long) entries, price should be above VWAP; for NO (short) entries, below. VWAP acts as a gravity line and indicates whether buying or selling pressure dominates the session.
- **Volume trend**: Are 5m volumes increasing (expanding trend) or decreasing (dying momentum)? Decreasing volume into a direction suggests limited follow-through potential.

**Current coverage**: ✗ Not implemented. No 5m OHLCV data is fetched or analyzed.

#### 2.2.3 — 15m: Direct Contract Alignment

**Role**: The definitive structural read — this IS the resolution timeframe

The 15m chart maps 1:1 to the contract. Understanding what the 15m candle is doing and where it sits relative to recent structure is the highest-value price action input available.

- **15m candle position within the bar**: At minute 5 of the window, the forming 15m bar shows whether the contract is trending (price moving steadily in one direction) or churning (whipsawing). A 15m bar that has moved 80% of its range in the first 5 minutes and is decelerating is different from one that's accelerating.
- **Prior 15m structure (last 4–8 bars)**: Is the current bar a continuation of a 15m trend, or is it at a turning point? Look for higher highs/higher lows (YES bias) or lower highs/lower lows (NO bias) in the prior 4 bars.
- **Key 15m levels**: Recent swing highs and lows on the 15m chart are meaningful resistance/support levels for binary outcomes. If price is approaching the high of the last 3 15m bars, a YES contract is running into resistance; a NO contract has a tailwind.
- **Candle pattern context**: Engulfing bars, pin bars (wicks), and doji patterns on the 15m at key levels are high-signal. A bearish engulfing on the 15m after a 3-bar rally = strong NO signal. A pin bar bounce at a 15m support = YES signal.
- **EMA(9)/EMA(21) on 15m**: Is the fast EMA above the slow? Is the gap expanding or contracting? An expanding EMA gap in the trade direction on the 15m is one of the cleanest trend signals available.
- **Cycle return (already tracked)**: `TAScorer` tracks `cycle_return_pct` from the window open price. This IS the 15m bar return — it measures the open-to-current progress of the contract bar. Extending this to track prior 15m bars' returns would give trend context.

**Current coverage**: ✓ Partially. `TAScorer` tracks cycle return (open-to-close progress within the current window) but fetches only 1m candles. No historical 15m OHLCV, no structure analysis, no candle patterns.

#### 2.2.4 — 1h: Trend Context and Support/Resistance Zones

**Role**: Identifies meaningful price levels and the intermediate-term trend direction

For KXBTC15M, the 1h chart provides 4 resolution bars per hourly candle. Understanding the 1h structure tells whether the short-term moves are with or against the dominant intermediate trend.

- **EMA(20)/EMA(50) on 1h**: Are these stacked bullishly (20 above 50) or bearishly? The 1h EMA stack is one of the most reliable and consistent trend direction filters. Long bias when stacked bull, short bias when stacked bear, cautious when converging.
- **1h support/resistance levels**: Recent 1h swing highs and lows define zones where price is likely to pause or reverse. A YES contract entered while BTC approaches a 1h swing high that held 3 times has lower probability than one entered at a fresh breakout above that level.
- **1h RSI divergences**: If price is making higher highs but 1h RSI is making lower highs, bullish momentum is weakening. The next few 15m contracts are statistically more likely to resolve DOWN. This is a regime input, not a trigger.
- **1h VWAP and POC**: Where did most hourly volume trade? Entering YES contracts when price is below hourly VWAP means fighting the flow of money; entering above VWAP means going with it.
- **1h range position**: Is BTC in the upper, middle, or lower third of the prior 1h bar's range? Upper third is extended (risk for YES), lower third is compressed (opportunity for YES on bounce).

**Current coverage**: ✗ Not implemented. No 1h data whatsoever.

#### 2.2.5 — 4h: Macro Bias and Regime Detection

**Role**: Determines whether longs or shorts have structural tail wind over the next several hours

The 4h chart is where trends live and die. A 15m YES contract entered into a 4h bullish impulse is structurally supported; the same contract entered into a 4h distribution phase faces headwinds even when short-term momentum looks fine.

- **4h market structure**: Higher highs + higher lows = bullish structure (YES bias). Lower highs + lower lows = bearish structure (NO bias). Break of structure (BOS) or change of character (CHoCH) signals a possible trend flip.
- **4h EMA(20)/EMA(50)**: Slow-moving, highly reliable trend indicator. Rarely changes, but when it does it signals multi-hour shifts that directly impact 15m contract win rates.
- **4h trend phase**: Is BTC in impulse (high-velocity, trending candles), pullback (retracing within a trend), or consolidation (ranging)? Each phase has different implications:
  - **Impulse phase** (4h): Strong directional YES or NO bias, wide entries acceptable, expect high WR
  - **Pullback phase** (4h): Counter-trend 15m moves are dangerous; trade WITH the 4h trend, not against it
  - **Consolidation phase** (4h): Mean-reversion (buy 45c, sell 55c) is higher-EV than trend-following
- **Funding rate and open interest** (relevant here): Persistently positive funding on perpetual futures means long bias in the 4h regime. High open interest during a price rally = structural support. High OI during decline = capitulation potential. These feed macro regime classification.
- **Daily bias**: Optionally, the daily close position relative to the daily open and daily EMA(20) provides a coarse YES/NO regime flag that persists for hours.

**Current coverage**: ✗ Not implemented. The legacy `bias_engine.py` in `_archive/` attempted multi-TF momentum scoring (it was disabled after 0% WR on day 1). The failure was attributed to the consensus/multi-TF approach being anti-correlated with market outcomes — however, the issue was likely **using TF consensus as the primary signal** rather than using it as a filter on top of the proven wallet-flow edge.

---

## 3. Data Sources and Capture Methods

### 3.1 Binance WebSocket (Real-Time Candles)

**Best option for low-latency 1m data. Extensible to all timeframes.**

Binance provides a public WebSocket stream with no authentication required:

```
wss://stream.binance.us:9443/stream?streams=btcusdt@kline_1m/btcusdt@kline_5m/btcusdt@kline_15m
```

The combined stream endpoint accepts multiple stream subscriptions in a single connection. Each message provides:
- `k.t` — kline open time (Unix ms)
- `k.o/h/l/c` — OHLC prices
- `k.v` — volume
- `k.x` — boolean: is the candle closed?

**Key implementation note**: The engine already uses `aiohttp` for async HTTP (`ta_module.py:246`). Adding a WebSocket task is a natural extension — `aiohttp.ClientSession.ws_connect()` handles this within the existing async loop.

**Advantages**:
- Sub-100ms latency on candle close events
- Free, no API key
- Already whitelisted by existing Binance domain access
- Eliminates the 60s polling lag in `fetch_binance_1m_candles`

**Alternatives**:
- `wss://stream.binance.com:9443/...` (global endpoint; may be blocked in some jurisdictions)
- Coinbase Advanced Trade WebSocket (requires API key)
- Bybit public WebSocket (no auth, similar format)

### 3.2 REST API Polling for Historical OHLCV

For initial warm-up and historical context on higher timeframes, the existing REST approach works well. The `fetch_binance_1m_candles` pattern needs extending:

```
GET https://api.binance.us/api/v3/klines
  ?symbol=BTCUSDT
  &interval=5m        # or 15m, 1h, 4h
  &limit=50           # enough for indicator warmup
```

For 1h and 4h, poll frequency can be very low (every 15m for 1h, every 60m for 4h) since these timeframes don't change rapidly. The REST approach is perfectly adequate for 1h/4h given their slow update cadence.

**Warmup requirements by timeframe**:

| Timeframe | Bars needed for EMA/RSI warmup | Poll frequency |
|-----------|-------------------------------|----------------|
| 1m | 25 bars (already handled) | WebSocket (real-time) |
| 5m | 30 bars (150m of history) | WebSocket or 60s poll |
| 15m | 20 bars (5h of history) | On window open |
| 1h | 60 bars (60h of history) | Every 15m |
| 4h | 50 bars (200h of history) | Every 60m |

### 3.3 Order Book Depth Snapshots

The Kalshi client (`kalshi_client.py`) already fetches order book data for pricing decisions. For price action purposes, order book depth at the BTC spot level (Binance) provides additional context:

```
GET https://api.binance.us/api/v3/depth?symbol=BTCUSDT&limit=20
```

From this, key metrics are:
- **Bid/ask wall imbalance**: Ratio of total bid depth to ask depth in the top 20 levels. >1.5 bid-heavy suggests buyers are dominating; <0.67 means sellers are in control.
- **Spread**: Tight spread indicates liquidity and confidence; wide spread indicates uncertainty.
- **Absorption vs. rejection**: If price tests a level and large bid walls hold, it's support. If they're eaten through, it's a breakdown. This is hard to detect in snapshots but order flow (tape) would reveal it.

For KXBTC15M specifically, the spot order book is useful as a **YES/NO direction tiebreaker** when the Kalshi mid-price is near 50c (the ambiguous zone). A strong bid wall 2% below spot says buyers are defending the level → YES bias.

**Practical limitation**: Snapshot-based order book is less useful than a streaming order flow tape. Given the complexity, this is a Phase 3/4 addition.

### 3.4 Volume Profile Construction

Volume profile answers: "Where did the most trading happen?" The price level with the most volume (Point of Control, POC) is a gravitational center — price tends to return to it.

For KXBTC15M, a session-level volume profile (midnight UTC to now) provides:
- **POC**: Most-traded price → strong magnet effect on 15m outcomes
- **Value Area High (VAH)** and **Value Area Low (VAL)**: Where 70% of session volume traded. Contracts entered inside the value area are lower-probability breakouts; contracts at VAH/VAL extremes are higher-probability mean-reverting.

Construction method using REST OHLCV:
1. Fetch 1m OHLCV for the session (up to 1440 bars)
2. For each bar, distribute volume uniformly across the bar's price range
3. Bucket into $50 price bins (e.g., $98,000–$98,050)
4. POC = highest-volume bucket

This can be computed incrementally as new 1m candles arrive. The computation cost is low.

### 3.5 Funding Rate and Open Interest Signals

These are perpetual futures metrics that are highly predictive for 15m binary outcomes during trending regimes:

**Funding rate** (Binance BTCUSDT perpetual):
```
GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1
```
- Positive funding (>0.01%) → market is net long, longs are paying shorts → retail-heavy long crowding → potential for flash down
- Negative funding (<-0.01%) → shorts are crowded → short squeeze risk → potential for flash up
- Near-zero funding → balanced, no directional crowding signal

**Open interest**:
```
GET https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT
```
Combined with price direction:
- Rising OI + rising price = healthy bull trend (YES bias)
- Rising OI + falling price = aggressive selling, bear trend (NO bias)
- Falling OI + rising price = short covering (weak upside, be cautious with YES)
- Falling OI + falling price = long capitulation (NO entries, then watch for reversal)

**Update frequency**: Funding rate updates every 8 hours (at 00:00, 08:00, 16:00 UTC). Open interest changes continuously but polling every 5m is sufficient for a regime input.

---

## 4. Technical Indicators Per Timeframe

### 4.1 Indicator Matrix

| Indicator | 1m | 5m | 15m | 1h | 4h | Notes |
|-----------|----|----|-----|----|----|-------|
| EMA(5)/EMA(13) cross | ✓ (current) | | | | | Already in `TAScorer` |
| EMA(9)/EMA(21) | | ✓ | ✓ | | | Short-term structure |
| EMA(20)/EMA(50) | | | | ✓ | ✓ | Trend definition |
| RSI(7) | ✓ (current) | | | | | Already in `TAScorer` |
| RSI(14) | | ✓ | ✓ | ✓ | | Standard momentum |
| Relative volume | ✓ (current) | ✓ | | | | Volume surge detection |
| VWAP | | ✓ | ✓ | | | Session gravity |
| Bollinger Bands | | | ✓ | | | Squeeze/expansion |
| Cycle return | ✓ (current) | | | | | Already in `TAScorer` |
| Volume-weighted momentum | | ✓ | ✓ | | | Stronger than pure price |
| Market structure (HH/HL) | ✓ | ✓ | ✓ | ✓ | ✓ | Trend confirmation |
| BOS/CHoCH | | | | ✓ | ✓ | Regime change signal |
| Candle patterns | | ✓ | ✓ | | | Engulfing, pin bars |
| Funding rate | | | | ✓ | ✓ | Positioning signal |
| Open interest delta | | | | ✓ | ✓ | Trend health |

### 4.2 Indicator Details by Timeframe

#### 1m Indicators (Extend Existing TAScorer)

**EMA Cross Direction Consistency**: Track how many consecutive bars the fast EMA has been above (bull) or below (bear) the slow EMA. A fresh cross (1–2 bars) is weaker than an established cross (5+ bars). Add `_ema_cross_bars: int` counter to `TAScorer`.

**RSI Slope**: Not just the RSI value, but whether it's rising or falling. A rising RSI that just crossed 50 from below is stronger than an RSI stuck at 55 that's been declining for 3 bars.

**Microstructure (Higher Highs/Lows)**: Track the last 5 1m closes and determine if they form a sequence of higher lows (bull) or lower highs (bear). This is the most direct 1m trend confirmation. Implementation: maintain a `deque(maxlen=5)` of close prices; `all(b >= a for a, b in zip(closes, closes[1:]))` = higher lows.

**Volume-Weighted Close**: `(close * volume) / sum(volume over last N bars)` — a volume-weighted average close. If current close is above this, buying pressure is dominant.

#### 5m Indicators (New: Short-Term Trend)

**EMA(9)/EMA(21)**: The standard short-term trend pair. Bull when 9 > 21, bear when 9 < 21. EMA gap expanding = trend strengthening; contracting = trend weakening.

**RSI(14)**: Standard RSI on 5m. Readings:
- >60: momentum is bullish, YES entries have tailwind
- 40–60: neutral, use other signals to decide
- <40: momentum is bearish, NO entries have tailwind
- >70 or <30: overbought/oversold — entering momentum trades here carries mean-reversion risk

**VWAP**: Rolling 5m VWAP (session or per-window). Price above VWAP = buyers in control → YES bias. Below VWAP = sellers in control → NO bias. Most powerful when price tests and bounces/rejects VWAP.

**5m Candle Body Analysis**: Classify the last closed 5m candle:
- Bullish engulfing: body closes above prior bar's body, bullish
- Bearish engulfing: closes below prior bar's body, bearish
- Pin bar (hammer/shooting star): long wick, small body — reversal signal at key levels
- Doji: open ≈ close — indecision, don't enter until next candle confirms

**Volume trend**: Compare volume of last 3 5m bars. Increasing volume in direction of trade = trend is healthy. Decreasing volume = momentum fading, watch for reversal.

#### 15m Indicators (New: Contract-Aligned Structure)

**Bollinger Bands (20, 2)**: 20-bar SMA ± 2 standard deviations.
- Squeeze (bands narrowing): low volatility, expansion imminent — signal direction uncertain but big move likely
- Expansion (bands widening): trend in progress — enter in direction of expansion
- Price riding upper band: strong uptrend
- Price riding lower band: strong downtrend
- Price at upper band with decreasing momentum: reversal risk for YES entries

**15m Market Structure**: Track the last 8 15m bars' swing highs and lows. If the last 3 swing lows are each higher than the previous, this is a bullish structure (YES bias). The most recent structure break is a critical level — a close below the last higher low is a bearish structure break (CHoCH).

**15m EMA(9)/EMA(21)**: Same as 5m but on the contract's own timeframe. When the 15m EMA stack aligns with trade direction, probability of a correct binary outcome is materially higher.

**15m Volume Profile (In-Session)**: Construct a volume profile from the 15m bars of the current trading session (UTC midnight to now). The POC from this profile is a gravitational target — contracts near the POC resolve in a coin-flip; contracts near the session extremes (far from POC) have a higher probability of reverting toward the POC.

#### 1h Indicators (New: Trend Context)

**EMA(20)/EMA(50)**: The gold-standard intermediate trend indicator for crypto. Bull when 20 > 50 and gap is expanding. Bear when 20 < 50 and gap is expanding. Converging = trend losing strength.

**1h Support/Resistance Levels**: Identify the 3 most recent swing highs and lows on the 1h chart. These are high-confidence levels:
- If BTC is within 0.1% of a prior 1h swing high: resistance approaching — YES contracts face headwind
- If BTC just broke above a prior 1h swing high: fresh breakout — YES contracts have structural tailwind
- Same logic inverted for swing lows and NO contracts

**1h RSI(14) Divergences**:
- Bullish divergence: price making lower lows, RSI making higher lows → upward reversal likely → YES bias
- Bearish divergence: price making higher highs, RSI making lower highs → downward reversal likely → NO bias
- Not a trigger, but a sustained divergence over 4+ bars on the 1h chart is a meaningful regime-level input

**1h Candle Patterns at Key Levels**: A bearish pin bar at 1h resistance is a strong regime input (NO bias for the next several 15m contracts). An engulfing bullish bar at 1h support is strong YES regime context. These patterns compound the value of nearby support/resistance.

#### 4h Indicators (New: Macro Regime)

**EMA(20)/EMA(50) on 4h**: Rarely wrong over the span of a few hours. Changes in 4h EMA alignment often signal multi-hour trend reversals. Once the 4h turns, it tends to stay.

**4h Market Structure (BOS/CHoCH)**:
- **Break of Structure (BOS)**: New 4h swing high (bullish BOS) or new 4h swing low (bearish BOS). Confirms the trend is continuing.
- **Change of Character (CHoCH)**: After a series of higher highs, the first lower high = early warning of trend reversal. After a series of lower lows, the first higher low = potential reversal.

**4h Trend Phase Classification**: Based on the above:
```
IMPULSE_BULL  → 4h EMA bullish stack + BOS (new swing high) → YES bias, wide entry range
PULLBACK_BULL → 4h EMA bullish stack + shallow retracement → YES near support, careful timing
IMPULSE_BEAR  → 4h EMA bearish stack + BOS (new swing low) → NO bias, wide entry range
PULLBACK_BEAR → 4h EMA bearish stack + shallow retracement → NO near resistance
CHOP          → Converging EMAs, no clear BOS → tight entry range, prefer 40-55c mean reversion
REVERSAL      → CHoCH identified → transition phase, directional bias reduced
```

**Funding Rate Integration**: Feed the Binance perpetual funding rate into the 4h regime. Extreme positive funding (>0.05%) during a 4h impulse bull = dangerous longs (leveraged longs will be liquidated). Extreme negative funding during bear = dangerous shorts.

---

## 5. Integration Architecture

### 5.1 Design Philosophy: Filter, Not Replace

The existing wallet-flow signal cascade (PRIMARY → TREND_FOLLOW → MIMIC → ALGO → TA_FORCED) has empirically positive expected value. The multi-timeframe price action framework should:

1. **Act as an entry filter** on wallet-flow signals — confirming that price action is supportive before committing
2. **Provide regime context** — increasing position sizing in favorable regimes, reducing in adverse ones
3. **Improve TA_FORCED signal quality** — turning the 1m TA fallback into a multi-timeframe confluence trade
4. **Provide inversion intelligence** — when wallet flow says YES but 4h structure is hard bear, downsize or skip

It should **NOT** become a competing entry system that fires independently — the disabled `bias_engine.py` / `consensus.py` failure proved that price-action-only approaches on this market are anti-correlated with outcomes.

### 5.2 Confluence Scoring Model

The core concept is a **Confluence Score** (0–100) that aggregates directional agreement across timeframes, weighted by relevance to the 15m binary outcome.

#### Weighting rationale:

| Timeframe | Weight | Rationale |
|-----------|--------|-----------|
| 1m | 20% | Most volatile, high noise; still provides immediate momentum |
| 5m | 25% | Short-term trend; 3 bars per window gives enough signal |
| 15m | 30% | Direct contract alignment; highest predictive value |
| 1h | 15% | Trend context; moves slowly but when it moves, it matters |
| 4h | 10% | Regime only; very slow-moving, used as a filter not a trigger |

#### Confluence score calculation:

For a proposed **YES** trade:
```
score = 0
if 1m_direction == "up":   score += 20 * (1m_confidence / 100)
if 5m_direction == "up":   score += 25 * (5m_confidence / 100)
if 15m_direction == "up":  score += 30 * (15m_confidence / 100)
if 1h_direction == "up":   score += 15 * (1h_confidence / 100)
if 4h_regime in ("IMPULSE_BULL", "PULLBACK_BULL"):  score += 10

# Scale 0–100
confluence_score = score  # already 0–100 by construction
```

For **NO** trades, invert the direction checks.

#### Score interpretation:

| Score | Interpretation | Action |
|-------|---------------|--------|
| 80–100 | Strong confluence — all TFs aligned | Full position, wide entry range |
| 60–79 | Good confluence — 3–4 TFs aligned | Standard position |
| 40–59 | Moderate confluence — mixed signals | Half position, tighter entry range (47–53c) |
| 20–39 | Weak confluence — mostly misaligned | Skip unless PRIMARY/TREND signal is very strong |
| 0–19 | Opposing structure — TFs say the opposite | Hard veto on TA_FORCED; downsize wallet-flow trades |

### 5.3 Entry Filter vs. Entry Trigger

This distinction is critical to avoid repeating the `bias_engine.py` failure:

**Entry filter** (what the MTF framework should be):
- The wallet/flow signal TRIGGERS the trade decision
- The MTF confluence score FILTERS it (approve, reduce, or reject)
- Example: Wallet signal fires YES → MTF score = 70 → proceed at 80% position size

**Entry trigger** (what failed in `bias_engine.py`):
- The MTF framework itself generates the entry signal
- No wallet-flow validation
- Result: fired into adverse market conditions, anti-correlated outcomes

The signal cascade remains:
```
PRIMARY → (MTF filter) → execute or reduce
TREND_FOLLOW → (MTF filter) → execute or reduce
MIMIC → (MTF filter) → execute or reduce
ALGO → (MTF filter) → execute or reduce
TA_FORCED → (MTF filter as PRIMARY signal) → execute or reject
```

For `TA_FORCED` specifically, the MTF confluence score becomes the signal quality gate — it should only fire when score ≥ 40 (moderate confluence), not on any non-flat direction as it currently does.

### 5.4 Confidence Weighting by Timeframe Alignment

Each tier in the signal cascade already has position size implications. MTF confluence can modify the `tier_mult` used for sizing:

```python
# Proposed modification to _execute_signal():
mtf_score = self._mtf_confluence.get_score(signal.kalshi_side)

if mtf_score >= 80:
    size_multiplier = 1.0    # Full size
elif mtf_score >= 60:
    size_multiplier = 0.85   # Slight reduction
elif mtf_score >= 40:
    size_multiplier = 0.65   # Moderate reduction
elif mtf_score >= 20:
    size_multiplier = 0.40   # Significant reduction
else:
    size_multiplier = 0.0    # Block entry
```

Importantly, this does not change the entry price range logic or stop levels — only size. This avoids cascading changes to the well-tested execution path.

---

## 6. Implementation Roadmap

### Phase 1: Real-Time 5m/15m WebSocket (Weeks 1–2)

**Goal**: Replace the 60s REST poll with a live WebSocket for 1m candles, and add 5m/15m candle tracking.

**Scope**:
1. Create `price_feed.py` — a new async WebSocket task that subscribes to Binance `btcusdt@kline_1m`, `btcusdt@kline_5m`, and `btcusdt@kline_15m`
2. Maintain rolling OHLCV buffers for each timeframe (deques of `Candle` objects)
3. Add `EMACalc` and `RSICalc` instances for 5m and 15m (already available from `indicators.py`)
4. Expose a simple state object (`PriceFeedState`) with last-seen candle and computed EMA/RSI per timeframe
5. Connect to the existing engine via a shared `asyncio.Queue` or direct attribute reference on `PolymarketCopyEngine`
6. Eliminate `fetch_binance_1m_candles` polling (or keep as fallback if WebSocket disconnects)

**Key integration point**: `_on_new_window()` in `polymarket_copy_engine.py` (line ~1281) already calls `self._ta_scorer.soft_reset()`. The new feed would also reset the 5m/15m cycle tracking here.

**Success criteria**: 1m candles arrive within 2s of close; 5m/15m candles refresh on close; no polling lag.

**Risk**: WebSocket disconnections. Implement exponential backoff reconnection and fall back to REST poll if WebSocket has been down >30s.

### Phase 2: Build the Confluence Scorer (Weeks 3–4)

**Goal**: Implement `MTFConfluenceScorer` that produces a directional confluence score from 1m/5m/15m data.

**Scope**:
1. Create `mtf_confluence.py` with `MTFConfluenceScorer` class
2. Accept `PriceFeedState` as input
3. Compute per-timeframe direction signals (EMA cross, RSI position, candle structure)
4. Apply weighted confluence formula from Section 5.2
5. Add `get_score(side: str) -> float` method
6. Log confluence scores on each evaluation cycle (DEBUG level)
7. No wiring to execution yet — observe and validate in logs

**Deliverable**: For 2 weeks, run the scorer in shadow mode — log what score it would have produced alongside each executed trade. Measure correlation between confluence score and trade outcome. Validate that score ≥ 60 trades have higher WR than score < 40 trades before enabling as a gate.

### Phase 3: Wire as Entry Filter (Week 5–6)

**Goal**: Connect `MTFConfluenceScorer` to the signal cascade as a soft filter on position sizing.

**Scope**:
1. Add `self._mtf_confluence: MTFConfluenceScorer` to `PolymarketCopyEngine.__init__()`
2. In `_execute_signal()`, retrieve MTF score and apply size multiplier (Section 5.4)
3. For `TA_FORCED` specifically: require score ≥ 40 as minimum gate; skip if score < 40
4. Log MTF score on every executed trade for outcome tracking
5. Add `mtf_score` column to `kalshi_trades` DB table for backtesting analysis

**Conservative option**: Start with soft sizing-only modification (no hard vetoes except on TA_FORCED). This preserves all wallet-flow entries while adding a size adjustment layer. Hard vetoes can be added after 2+ weeks of data.

**Hard veto option**: Block any signal (including wallet-flow) when MTF score < 20. Only implement this after shadow-mode data confirms the threshold.

### Phase 4: Higher Timeframes for Regime Context (Weeks 7–10)

**Goal**: Add 1h and 4h data for regime classification, and optionally funding rate/OI signals.

**Scope**:
1. Add 1h and 4h OHLCV to `price_feed.py` (REST poll since these don't need WebSocket latency)
2. Implement `RegimeClassifier` in `mtf_confluence.py`: classifies the 4h trend phase (Section 4.2, 4h indicators)
3. Use regime as a multiplier on the confluence score (e.g., `IMPULSE_BULL` → score × 1.2 for YES)
4. Add funding rate and OI signals from Binance futures API
5. Optionally: implement session-level volume profile (POC/VAH/VAL) for 15m context
6. Consider re-enabling `DEVIATION_STOP_ENABLED` for positions taken against the 4h regime (riskier entries)

**Scope limitation**: Do not attempt full market structure (BOS/CHoCH) detection in Phase 4 — this requires a more robust swing-point identification algorithm. Add it as a Phase 5 item after Phase 4 is stable.

---

## Appendix A: Key KXBTC15M-Specific Constraints

1. **Contracts expire every 15 minutes**: Price action signals have a hard expiry. A 4h resistance level is relevant only if it's close enough to BTC's current price to be tested within the current 15m bar. A level 2% away doesn't affect the current contract.

2. **Binary resolution**: The outcome is binary — BTC higher or lower than open at window close. Volume profile and VWAP signals are more predictive when the current price is near the resolution threshold (0.0% from open), less predictive when far from it. A 1% move already happened; the binary has >90% conviction.

3. **Entry timing matters**: The engine enters on passive ladders resting 3–5c below bid. For TA_FORCED, the entry typically happens in minutes 3–12 of the 15m window. Earlier entries have more of the contract's duration in their favor; later entries need faster resolution.

4. **40–55c sweet spot**: Engine data confirms 40–64c entries are the profitable range. This directly maps to market uncertainty — the window is most predictable when BTC hasn't already moved decisively. MTF confluence signals are most valuable in this price zone.

5. **No short selling**: The engine can buy YES (BTC up) or NO (BTC down) but does not hold both simultaneously. MTF framework should output a single directional recommendation or "no trade," not a spread.

6. **`TA_FORCED` uses inversion logic**: When broader signals (whale, tape, Poly flow) oppose the TA direction, the trade is flipped. MTF confluence should be wired to respect this inversion — if the confluence score says DOWN but inversion flips to UP, the MTF score for UP should be re-evaluated before executing.

---

## Appendix B: Failure Mode Reference

The legacy `bias_engine.py` and `consensus.py` (in `_archive/`) attempted multi-timeframe TA consensus as a standalone entry signal. It produced 0% WR on day 1. Post-mortem lessons:

- **TA consensus without flow anchor fails on binary contracts**: Pure price action on crypto 15m binaries is approximately coin-flip without market microstructure (order flow, smart money positioning) context.
- **Fourier-weighted TF fusion added complexity without edge**: The weighted consensus of multiple TF scores has no theoretical basis for being more predictive than individual TFs for a binary outcome.
- **The wallet-flow edge is real; price action edge alone is not**: The engine's demonstrated positive EV comes from Polymarket smart money's information advantage over Kalshi pricing. Price action without that anchor is untested noise.
- **Conclusion**: Price action belongs in the filter layer, not the signal layer.
