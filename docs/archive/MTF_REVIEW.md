# MTF System — Code Review
**Date**: 2026-04-01
**Reviewer**: Claude (automated)
**Scope**: `price_feed.py`, `tf_analyzer.py`, `mtf_scorer.py`, integration points in `polymarket_copy_engine.py`, `signal_logger.py`, `user_config.py`
**Status**: Shadow mode. Not yet live. ~0 trades logged.

---

## 1. Spec vs Implementation Gap Analysis

### What changed from ARCHITECTURE.md

| Item | Spec | Implementation | Verdict |
|------|------|----------------|---------|
| Module names | `mtf_confluence.py` (monolithic) | Split: `tf_analyzer.py` + `mtf_scorer.py` | Better — cleaner SRP |
| 4h RegimeClassifier | Full class: 4h EMA + funding rate + OI → multiplier applied to score | **Dropped entirely** | Gap — see §7 |
| VWAP tracking | TFAnalyzer computes session VWAP, `above_vwap` field in TFSignal | Not implemented | Gap — §7 |
| Score range | 0–100 (per-side) | -1 to +1 (bidirectional) | Better — avoids two redundant evaluations |
| EMA pairs | (9,21) for 5m/15m | **(8,21)** for 1m/5m/15m | Minor divergence |
| TFSignal fields | `direction`, `confidence`, `above_vwap`, `structure` | `score` (float), `bb_squeeze`, `bb_position`, `candle_structure` | Richer — adds BB |
| Size multiplier table | ≥80: 1.0x, ≥60: 0.85x, ≥40: 0.65x, ≥20: 0.40x, <20: veto | ≥0.6: **1.5x boost**, ≥0.3: 1.0x, neutral: block | Completely different — no gradual downscale, only boost/block |
| Tier gating scope | Only TA_FORCED gated; PRIMARY/TREND_FOLLOW never blocked | **All tiers blocked** in live mode | **Critical regression risk** — §3 |
| TAScorer extensions | Add `consecutive_bull`, `consecutive_bear`, `ema_cross_bars` to `TASignalResult` | Not done | Gap — workaround in `_ta_result_to_tf_signal` |
| `_on_new_window()` soft reset | Add MTF soft reset at window boundary | Not implemented | Minor omission — not critical |
| `MTF_ENABLED` default | `False` in Phase 1 | **`True`** in user_config | Implementation is ahead of spec (Phase 2 already) |
| DB migration | `mtf_score REAL`, `mtf_regime TEXT` | Correctly added to migrations list (signal_logger.py:282-283) | ✓ |
| WebSocket 1m→TAScorer | PriceFeedTask replaces polling for 1m feed | 1m WS buffer populated but **TAScorer still uses polling** | Dual-feed overlap — §3 |
| 4h REST polling | 60m poll interval | **Dropped** (no 4h at all) | Gap |

### What was added beyond spec

- Bollinger Bands (squeeze detection + band position) in `TFAnalyzer` — not in spec, solid addition
- `ConfluenceResult.is_aligned` / `is_opposing` properties — cleaner than raw score comparison
- `MTFScorer.is_ready()` method — useful, but not called in the engine
- `warmup_status` diagnostic property
- `_build_regime()` uses 15m signal's regime tag to propagate SQUEEZE_ regimes upward — good touch

---

## 2. Signal Quality Assessment

### TFAnalyzer internal scoring

**Weights** (sum to 1.0): EMA 0.30 / RSI 0.25 / Candle 0.20 / Market structure 0.15 / Volume 0.10. Reasonable distribution; EMA dominating is appropriate for a trend-following system.

**EMA freshness decay** (`tf_analyzer.py:288`):
```python
freshness = max(_EMA_FRESHNESS_MIN, 1.0 - (self._ema_cross_bars - 1) * 0.04)
```
Floor is 0.35 — meaning a cross 17+ bars old still contributes `0.35 × 0.30 = 0.105` directional force. On the 1h timeframe, bar 17 = 17 hours after the cross. That's still 10.5% directional pull from a stale signal. The floor is too high for slow TFs. Consider `0.20` floor for 1h, or make it TF-dependent.

Separately: a fresh cross (bars=1) gives full weight (1.0), but EMA crosses on bar 1 are often false signals, especially on noisy short TFs. The spec called out `ema_cross_bars == 1` as "strongest signal" — that's true for established trends entering a pullback, but not for chop. The BB squeeze detection provides some mitigation.

**RSI component** (`tf_analyzer.py:296`):
```python
rsi_norm = (rsi_val - 50.0) / 50.0   # -1 to +1
rsi_slope_adj = 0.1 if rsi_direction == "up" else ...
rsi_component = max(-1.0, min(1.0, rsi_norm + rsi_slope_adj))
```
RSI slope direction requires a 0.5-point change between bars (`tf_analyzer.py:151`). On 15m/1h, RSI moves slowly — this threshold is fine. On 5m, RSI can swing 5+ points per bar, so the 0.5 threshold is rarely "flat". Minor calibration issue only.

**Market structure** (`tf_analyzer.py:365`):
```python
high_early = sum(highs[:mid]) / mid
high_late  = sum(highs[mid:]) / (len(highs) - mid)
```
This is an **average-of-halves** comparison, not true swing point detection. A single outlier candle (flash spike) skews the average and can flip the classification. A flash crash followed by recovery on 5m would cause `high_late < high_early` → "bear" structure — while price is actively recovering. Real HH/HL detection requires tracking local swing highs/lows with a 2-bar lookback. This is the weakest component.

Buffer is `maxlen=8`. On 5m, that's 40 minutes of structure. On 1h, that's 8 hours. Both reasonable windows.

**BB squeeze threshold** (`tf_analyzer.py:65`):
```python
_BB_SQUEEZE_THRESHOLD = 0.02   # < 2% of price = squeeze
```
For BTC at $85k, this fires when the 20-bar BB band width is < $1,700. This is an extremely tight range — normal quiet overnight BTC sessions have ~$500-1000 bands. A 2% threshold would fire during extended consolidation only. Expect this to be active < 5% of the time. The squeeze detection should inform position sizing (reduce during squeeze, wait for breakout), but currently it only affects the regime label, not the score.

**Candle microstructure** (`tf_analyzer.py:326`):
Pattern detection is correct. Engulfing check requires exact directional filter (bearish engulf needs `prev_close > prev_open` at line 360) — avoids false engulf on doji continuation. Good.

Bonus scores: ±0.4 for engulfing, ±0.3 for pin bars. Applied after clamping pressure to [-1,1] with `max(-1.0, min(1.0, pressure + bonus))`. This means on a clean engulfing bar, candle score = min(1.0, 0.8 + 0.4) = 1.0. Full candle weight = 0.20 × 1.0 = **0.20**. Adequate signal.

**Volume-weighted momentum** (`tf_analyzer.py:314`):
```python
vol_clamp = min(rel_vol, 3.0)
if c.close > c.open:
    vol_component = (vol_clamp - 1.0) / 2.0   # 0 at 1x vol, +1 at 3x vol
```
Range: [0.0, +1.0] for bullish candles, [-1.0, 0.0] for bearish. At average volume (1x), contributes 0. This means high-volume trend moves get rewarded and low-volume drifts do not. Correct behavior. Weight 0.10 keeps it as a tiebreaker.

**MTFConfluenceScorer normalization** (`mtf_scorer.py:161`):
```python
active_weight = sum(w for tf, w in TF_WEIGHTS.items() if tf_scores.get(tf) is not None)
if active_weight < 0.10:
    confluence_score = 0.0
else:
    confluence_score = weighted_sum / active_weight
```
Cold TFs are excluded from denominator — this is mathematically correct and avoids the score collapsing toward zero during warmup. Well done.

The `active_weight < 0.10` floor means scoring requires at least one real TF. Since 1m is always assigned a score (from TAScorer), active_weight = 0.222 (1m fraction) as minimum. The 0.10 cutoff is never reached in practice. It's dead code as written, but harmless.

**1m TAScorer conversion** (`mtf_scorer.py:243`):
```python
score = max(-1.0, min(1.0, ta.composite_score / 100.0))
```
TAScorer's `composite_score` range in theory is unbounded (it's an EMA(2) of a raw weighted sum that can spike > 100). In practice, `/100` and clamp handles it.

```python
rsi_direction = ("up" if ta.score_velocity > 0.5 else "down" if ... else "flat")
```
`score_velocity` is the TAScorer composite score's 1-bar rate of change — this is a reasonable proxy for RSI slope but is not the same quantity. This workaround exists because the TAScorer extensions (RSI slope, EMA cross bars) were not added. The `ema_cross_bars=0` hardcode in the returned TFSignal means the 1m EMA freshness signal is completely absent from the MTF score. This is the main quality gap from not doing the TAScorer extensions.

---

## 3. Integration Review

### Signal cascade placement

MTF evaluation happens at `polymarket_copy_engine.py:2918` — after tier dedup check (line 2912) but before entry price band selection (line 2970). Correct placement; avoids computing entry price for blocked trades.

### Shadow mode correctness

Shadow path logs and falls through. `_mtf_size_mult` initialized to 1.0 (line 2923), only changed in `HIGH_CONFIDENCE` live path (line 2958). Size multiplier at line 3172 gates on `_mtf_size_mult > 1.0` — no-op in shadow mode. ✓

### CRITICAL: All tiers blocked in live mode

The live-mode gating code (`polymarket_copy_engine.py:2941-2956`) blocks on `is_opposing` or `action == "NO_TRADE"` with a bare `return` — no tier check. This means in live mode, PRIMARY and TREND_FOLLOW signals can be vetoed by the MTF filter.

The architecture spec explicitly stated (ARCHITECTURE.md line 670):
> *"The MTF gate must never affect PRIMARY or TREND_FOLLOW signals in Phase 3 — only TA_FORCED."*

This is the single most important issue to fix before switching to live mode. The PRIMARY signal type has the highest validated WR. Blocking it on a neutral MTF score (mixed TFs, early warmup) would silently degrade the engine's edge.

**Fix needed**: Add a tier guard before the block path:
```python
if signal.signal_tier not in ("PRIMARY", "TREND_FOLLOW"):
    if _mtf_result.is_opposing: return
    if _mtf_result.action == "NO_TRADE": return
```

### WebSocket and event loop interaction

`price_feed_task` runs as a concurrent `asyncio.create_task`. The engine's poll loop uses `asyncio.sleep(3)`, which yields to the event loop and processes WS messages during the idle period. No timing interference. ✓

On WS disconnect, the reconnect waits using `asyncio.sleep(delay)` — non-blocking. REST task for 1h continues independently. ✓

### 1m duplicate feed

`_WS_TIMEFRAMES = ["1m", "5m", "15m"]` (`price_feed.py:53`) means the price feed subscribes to 1m klines via WebSocket and fills `state.buffers["1m"]`. However, the engine never calls `feed.get_candles("1m", ...)` and never registers a 1m callback. The TAScorer still uses `fetch_binance_1m_candles()` polling.

Effect: 1m WS data is collected and silently discarded — wasted bandwidth, no harm. Either wire up 1m WS to TAScorer callbacks (replacing the 60s polling with sub-100ms WS), or remove `"1m"` from `_WS_TIMEFRAMES`.

### `is_ready()` not called

`MTFScorer.is_ready()` (mtf_scorer.py:236) checks if ≥2 TFs are warm before trusting the score. This is exposed as a diagnostic but never called in `_execute_signal`. In the early minutes after startup, the MTF score could be based solely on 1m data (5m/1h not yet warm) and score as if fully reliable. The evaluation path handles this through active_weight normalization, so it's not dangerous — but logging `is_ready()` status in the shadow log would help calibration analysis.

---

## 4. Data Feed Reliability

### WebSocket reconnect

Exponential backoff: 5s → 10s → 20s → 40s → 60s (max). Cap at 60s is appropriate. ✓
Heartbeat every 20s via `aiohttp.ClientSession.ws_connect(heartbeat=20)`. ✓
`last_msg_ts` monitored against `_ws_timeout` (30s) — forces reconnect on silent connection. ✓
Buffer persists across reconnects — old data remains valid. ✓

### Gap detection after reconnect

**Missing**: After a reconnect, there is no REST catch-up for missed 5m/15m candle closes. If the WS was down for >5 minutes, one or more 5m candles will be silently missed. The 5m/15m TFAnalyzers will not process those candles.

Impact: EMA/RSI state falls slightly behind reality. For EMA(8/21), missing 1-2 bars causes < 5% divergence from correct values. Market structure tracking loses those bars' highs/lows. This is minor but worth noting.

The 1h TF is protected — `_poll_rest_loop` runs every 15 min and adds any new candles via `_fetch_rest_new_candles`. The same approach should be applied to 5m/15m on reconnect.

### Warmup path

All 4 TFs warmed concurrently via `asyncio.gather` with 45s timeout (`price_feed.py:155`). If any TF fails, it logs a warning and continues with an empty buffer. MTFScorer handles empty buffers as "cold" and excludes them from the score. ✓

Callbacks are intentionally NOT fired during warmup (`price_feed.py:318`) to avoid premature scoring before buffers are full. ✓

### Stale data detection

`state.last_updated[tf]` tracks epoch of last candle update. This is stored but nothing reads it to detect stale feeds or emit alerts. Consider: if `last_updated["5m"]` is > 15 minutes ago during live trading hours, log a warning.

### Binance US endpoint hardcoded

`BINANCE_WS_URL = "wss://stream.binance.us:9443/stream"` and `BINANCE_REST_URL = "https://api.binance.us/api/v3/klines"` are hardcoded in `price_feed.py:41-42`. These work from US IPs but may fail from other regions (the engine is currently running locally so this is fine). Adding a `PRICE_FEED_ENDPOINT` config option would make this portable.

### When Binance is completely down

Warmup fails with warning → all non-1m TFs cold → confluence_score = 1m TA score only (active_weight = 0.222 normalized to score as-is) → signal quality degrades to current baseline. Engine continues trading at existing TA/wallet performance. ✓ Fail-safe is clean.

---

## 5. Edge Cases

### Low-volume periods (overnight, weekends)

Volume SMA needs 20 bars to converge. During low volume: rel_vol ≈ 1.0 throughout → volume component ≈ 0 for all bars. Score driven by EMA/RSI/structure only. This is correct behavior — volume confirmation absent, so don't penalize or reward for it.

### Flash crashes

A flash down-and-recovery (wick candle) on 5m:
1. Candle produces `pin_bear` (or `engulf_bear` if large body)
2. RSI spikes down
3. EMA cross potentially triggers → bear cross, freshness = 1

Score snaps to strongly negative. On the next candle (recovery), RSI rises, pressure flips bullish, but EMA cross takes several bars to reverse. The bear EMA state persists and decays slowly (35% floor means it never fully goes away within the same session). Result: **the MTF scorer will lag a recovery by 5-10 bars**.

For a 5m TFAnalyzer, that's 25-50 minutes of residual bear bias after a flash crash wick. This is actually conservative behavior for binary contracts — after a flash, it's not clear direction has reversed. Acceptable.

### Full consolidation (all TFs flat)

All components ≈ 0 → confluence_score ≈ 0 → `abs(score) < 0.3` → action = `NO_TRADE`.
Shadow mode: log and continue. Live mode: return (block trade).
This is the correct behavior — consolidation is high-uncertainty territory for directional binary contracts.

### Rapid regime change (4h reversal)

E.g., BTC drops through a key level causing a 4h structure break. The system would see: 5m/15m flip bearish quickly, 1h slower. Divergence → score near 0 to slightly negative. For YES trades: NO_TRADE / weakly opposing. Cautious. Correct.

### Cold start (first ~25 minutes)

At startup with warmup complete:
- 1m: warm immediately (TAScorer requires only 3 bars)
- 5m: warm after 25 candles = ~125 minutes
- 15m: warm after 25 candles = ~375 minutes
- 1h: warm after 25 candles = ~25 hours

This means the MTF system won't have meaningful multi-TF confluence for approximately 2 hours after the buffers finish filling (5m warms first). In practice, the warmup pre-fills buffers from REST at startup (50 candles for 5m, 30 for 15m, 72 for 1h), so `is_warm = True` on first signal evaluation. ✓

One cold-start scenario that CAN produce bad signals: if warmup REST calls fail but the engine proceeds. Then 5m/15m/1h all start cold, TFAnalyzers begin updating from WS, and `is_warm=False` for the first 25 bars. Score is normalized against active TFs only (just 1m). No bad signal produced — just lower-quality scoring.

### Contract window boundaries

The 15m TFAnalyzer has no concept of Kalshi window boundaries. Its state is continuous through window transitions. The `soft_reset()` in TAScorer resets cycle tracking (correct) but the MTF analyzers have no corresponding reset. This is **correct** — BTC trends don't respect Kalshi windows.

---

## 6. Scoring Calibration

### Are ±0.3 thresholds appropriate?

The ±0.3 dead band means a trade is blocked (in live mode) unless the weighted score is > 30% directionally aligned. Given that the TF weights are normalized:
- For a YES trade to pass, the weighted average of 4 TF scores must be ≥ +0.3.
- A scenario: 15m at +0.5 (bullish), all others flat (0.0). Weighted score = 0.333 × 0.5 + others × 0 / 0.333 = 0.5 × (active_weight = 0.333) / 0.333 = +0.50. → NORMAL (passes).
- A scenario: all TFs mildly bullish at +0.20. Score ≈ +0.20 → NO_TRADE. Would block a trade where every TF is mildly bullish.

The ±0.3 threshold will likely block 30-40% of trades in live mode based on this analysis. That's aggressive. The shadow data will reveal the actual distribution — **do not go live before checking the score histogram**.

### Should YES/NO have different thresholds?

Historical data shows NO-side is profitable only in 48-59c band, with lower general WR. The engine already restricts NO entry bands more tightly. Applying a tighter MTF threshold for NO (e.g., -0.40 instead of -0.30) would add a third layer of NO filtering. This may or may not improve performance. Flag this for evaluation once shadow data is available.

### When only 2 of 4 TFs are warm

Example: 1m (always available) + 5m only warm. active_weight = 0.222 + 0.278 = 0.500. If 1m = +0.6, 5m = +0.5: confluence = (0.222×0.6 + 0.278×0.5) / 0.500 = (0.133 + 0.139) / 0.500 = +0.544. → NORMAL action. Good — two aligned bullish TFs give a passing score.

If 1m = +0.6 (bullish) but 5m = -0.4 (bearish): confluence = (0.133 - 0.111) / 0.500 = +0.044. → NO_TRADE. Correct — conflicting signals at only 2 TFs warrants caution.

### The 1.5x size boost

Applies only in live HIGH_CONFIDENCE (score ≥ 0.6). The boost is capped by SIZING_MAX_DOLLARS (`mtf_scorer.py:188` + engine line 3173). At a typical 50c entry with $20 balance: normal sizing = 10 contracts ($10). 1.5x = 15 contracts ($7.50). Still under $50 cap. ✓
The boost is structurally safe.

### Calibration concern: score distribution unknown

Until shadow mode runs, we don't know if the score distribution is appropriate. Common failure modes:
- **Score always near 0**: Indicators cancel each other → everything NO_TRADE → live mode would halt almost all trading
- **Score always near ±1**: System is too confident → threshold too easy, not filtering
- **Score distribution bimodal**: Good sign — means the indicators are discriminating

Run the histogram query from §8 after 50+ trades to get a first read.

---

## 7. Recommendations (Prioritized)

### Priority 1 — Must fix before going live

**1a. Add tier guard to live-mode MTF block** (`polymarket_copy_engine.py:2941`):
PRIMARY and TREND_FOLLOW signals must never be blocked by the MTF filter. The current code applies `return` to ALL tiers. Add:
```python
_mtf_allow_block = signal.signal_tier not in ("PRIMARY", "TREND_FOLLOW")
if not shadow and _mtf_allow_block:
    if _mtf_result.is_opposing: return
    if _mtf_result.action == "NO_TRADE": return
```
The HIGH_CONFIDENCE size boost can still apply to all tiers.

**1b. Validate score distribution before flipping `MTF_SHADOW_MODE = False`**:
Run `MTF_SHADOW_MODE = True` until 200+ settled trades. Run the WR-by-score-bucket query. If aligned trades do not show ≥ 3pp WR improvement over opposing, do not enable live mode.

### Priority 2 — Important gaps

**2a. 5m/15m REST catch-up on WS reconnect**:
After `_run_websocket()` exits abnormally, fetch the last few 5m/15m candles via REST to fill gaps. Currently only 1h has a periodic REST fallback.

**2b. Wire unused 1m WS buffer or remove it**:
Either register a callback on the 1m WS feed to route into TAScorer (replacing the 60s polling with real-time updates — significant latency improvement), or remove `"1m"` from `_WS_TIMEFRAMES` to stop wasting bandwidth on data that isn't used.

**2c. Log `is_ready()` status in shadow log**:
Add `scorer_ready=%s` to the shadow log line so the analysis query can filter for trades where the scorer was fully warm. Cold-TF trades should not be included in the go/no-go calibration.

### Priority 3 — Signal quality improvements

**3a. Restore 4h macro regime**:
The biggest spec gap. A lightweight version requires only:
- REST poll for 4h candles every 60 min (already handled by architecture spec)
- Add `"4h"` to buffer, route to a standalone TFAnalyzer("4h")
- Use 4h score as a `regime_multiplier` (1.15x bull, 0.85x bear, 0.90x chop)
- Apply after the weighted average, before action classification

This adds macro context that the current 1h-max system can't provide.

**3b. Fix market structure detection** (`tf_analyzer.py:365`):
Replace average-of-halves with swing point tracking. Track local peaks/troughs with a 2-bar lookback: a swing high is `high[i] > high[i-1] and high[i] > high[i+1]`. Three swing highs trending down = LH structure → bear. This is immune to flash spike distortion.

**3c. Add VWAP to TFAnalyzer**:
METHODOLOGY.md §2.2.2 and §2.2.3 rate VWAP as high-value for 5m/15m. Implementation: track cumulative `(hlc3 × volume)` and `volume` since UTC midnight, reset in `reset_session()`. Add `above_vwap: bool` to TFSignal, use it as a score component (±0.1 adjustment, ~10% weight).

**3d. Add TAScorer extensions** (`ta_module.py`):
The architecture spec called for `ema_cross_bars` and consecutive close tracking. Currently, `_ta_result_to_tf_signal` sets `ema_cross_bars=0` always, meaning the 1m EMA freshness signal is absent from MTF scoring. Adding the proper fields to `TASignalResult` would improve 1m signal quality in the MTF context.

**3e. EMA freshness floor by TF**:
The `_EMA_FRESHNESS_MIN = 0.35` floor is too high for 1h (17-hour-old cross still has 35% weight). Consider per-TF floors: 1m/5m: 0.35, 15m: 0.30, 1h: 0.20.

### Priority 4 — Operational improvements

**4a. Stale data alert**:
If `state.last_updated["5m"]` is > 10 minutes old during active trading, log a WARNING. Currently stale feeds fail silently — the 5m score just goes stale.

**4b. Configurable endpoint for non-US deployment**:
Add `PRICE_FEED_WS_URL` and `PRICE_FEED_REST_URL` to user_config with `.us` defaults. No user impact until needed.

---

## 8. Validation Plan

### Phase: Shadow mode data collection (current)

Run with `MTF_SHADOW_MODE = True` until **200 settled trades** with non-null `mtf_score`. At current engine volume (5-15 trades/day), this is 2-4 weeks.

Every signal evaluation logs to the SHADOW line:
```
CopyEngine MTF SHADOW: side=yes score=+0.42 regime=TREND_BULL action=NORMAL | [1m:+0.55(TA_SCORER) 5m:+0.38(TREND_BULL) 15m:+0.31(PULLBACK_BULL) 1h:+0.21(TREND_BULL)] → +0.42 TREND_BULL
```

The `mtf_score` and `mtf_regime` are stored in `kalshi_trades` at entry time.

### SQL queries for validation

**Query 1 — Score distribution (run after 50+ trades)**:
```sql
SELECT
    ROUND(mtf_score * 10) / 10 AS score_bucket,
    COUNT(*) AS trades
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1 ORDER BY 1;
```
Expected healthy distribution: roughly bell-curve centered near 0, tails below -0.5 and above +0.5. If > 60% of scores are in ±0.1, the scoring isn't discriminating.

**Query 2 — WR by score bucket** (primary hypothesis test):
```sql
SELECT
    CASE
        WHEN mtf_score >= 0.6  THEN '(A) HIGH_BULL ≥0.6'
        WHEN mtf_score >= 0.3  THEN '(B) BULL 0.3–0.6'
        WHEN mtf_score >= -0.3 THEN '(C) NEUTRAL ±0.3'
        WHEN mtf_score >= -0.6 THEN '(D) BEAR -0.6–-0.3'
        ELSE                        '(E) HIGH_BEAR ≤-0.6'
    END AS mtf_bucket,
    COUNT(*)  AS trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl,
    ROUND(AVG(pnl), 3) AS avg_pnl_per_trade
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1 ORDER BY 1;
```

**Query 3 — Aligned vs opposing** (actionable go/no-go):
```sql
SELECT
    CASE
        WHEN (side = 'yes' AND mtf_score >= 0.3) OR (side = 'no' AND mtf_score <= -0.3) THEN 'aligned'
        WHEN (side = 'yes' AND mtf_score <= -0.3) OR (side = 'no' AND mtf_score >= 0.3) THEN 'opposing'
        ELSE 'neutral'
    END AS alignment,
    COUNT(*) AS trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1;
```

**Query 4 — Regime performance**:
```sql
SELECT
    mtf_regime,
    COUNT(*) AS trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY mtf_regime ORDER BY trades DESC;
```

**Query 5 — By strategy tier** (checks if MTF alignment pattern differs by tier):
```sql
SELECT
    strategy_name,
    CASE WHEN (side = 'yes' AND mtf_score >= 0.3) OR (side = 'no' AND mtf_score <= -0.3)
         THEN 'aligned' ELSE 'not_aligned' END AS alignment,
    COUNT(*) AS trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1, 2 ORDER BY 1, 2;
```

### Decision criteria

| Condition | Action |
|-----------|--------|
| < 200 settled trades | Continue shadow mode — insufficient data |
| Aligned WR ≥ Opposing WR + 5pp AND n ≥ 30 per bucket | Strong signal — proceed to live planning |
| Aligned WR ≥ Opposing WR + 3pp AND n ≥ 50 per bucket | Moderate signal — enable TA_FORCED gate only first |
| Aligned WR - Opposing WR < 2pp | MTF has no predictive edge — stay shadow indefinitely, investigate indicators |
| Opposing WR > Aligned WR | Inverted signal — score interpretation may be inverted, investigate |
| Score distribution: > 60% in ±0.1 | Thresholds too tight — lower to ±0.15 before going live |

### Pre-live checklist (when criteria are met)

1. **Fix tier guard** (Rec 1a) — must be done before ANY live-mode testing
2. Verify `is_ready()` status in logs — confirm both 5m and 15m are warm for all shadow-mode trades evaluated
3. Set `MTF_SHADOW_MODE = False` in user_config.py
4. Run for 1 full day (10-20 trades) with live gating; monitor log for unexpected trade blocks
5. Check: did any PRIMARY or TREND_FOLLOW signals get blocked? (They should not after Rec 1a fix)
6. Run Query 3 on first 20 live-mode trades — confirm alignment pattern holds

---

## Summary

The MTF implementation is solid for shadow mode. The `PriceFeedTask` WebSocket is well-structured, reconnect logic is robust, and the `TFAnalyzer` indicator stack is mathematically correct. The `MTFConfluenceScorer` normalization properly handles cold TFs.

**The one critical issue that must be fixed before going live**: the MTF block in live mode applies to all signal tiers including PRIMARY/TREND_FOLLOW, which the spec explicitly prohibited. Until this is fixed with a tier guard, **do not set `MTF_SHADOW_MODE = False`**.

Secondary issues worth addressing before or concurrent with live testing: 5m/15m WS gap recovery, the unused 1m WS buffer, and the missing `is_ready()` gate in the eval path.

The validation plan is well-defined (shadow queries, go/no-go criteria). The 200-trade minimum and the 5pp WR improvement requirement are appropriate thresholds given the 15-30 trade/week cadence.
