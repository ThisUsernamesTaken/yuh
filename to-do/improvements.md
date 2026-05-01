# BTC Bias Engine — Improvements To-Do
# Generated: 2026-03-17 after full codebase review

---

## P0 — Critical (Actively Costing Money)

### 1. 1h TF Score Cap in `consensus.py`
**File:** `consensus.py:53`
**Problem:** `signed_score = result.bull_conf - result.bear_conf` ranges -100 to +100 with no per-TF cap.
The 1h BiasEngine saturates at ±100 and stays there for the full hour. At 25% weight, a -100
1h score injects -25 into the Fourier-weighted sum — enough to override 3 short-TF signals.
Live data confirms this: 10+ consecutive windows all emitting PUT while shorter TFs may be
neutral or reversing.
**Fix:** Cap each TF's signed_score contribution before weighting:
```python
signed_score = max(-50.0, min(50.0, result.bull_conf - result.bear_conf))
```
Or alternatively apply a staleness decay for the 1h TF based on bars since last 1h update.

### 2. 1h Warm-Start Depth Insufficient
**File:** `main.py:303` (`_warm_start`)
**Problem:** `limit=100` fetches only 100 1m candles (~100 minutes). A 1h BiasEngine needs
at least 60 1m candles per bar plus warm-up for its EMA(5)/EMA(13)/RSI(7). With 100 bars,
the 1h TF processes at most 1 completed 1h candle. Its indicators are essentially cold —
the first 1h candle locks in a near-arbitrary EMA spread that persists for the next hour.
**Fix:** Increase to 250 candles (covers 4+ full 1h bars with EMA warmup):
```python
params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 250}
```

---

## P1 — High Impact (Measurably Degrading Performance)

### 3. Regime Rejection Investigation
**File:** `hft_engine.py:497–508`
**Problem:** `HFT_REQUIRE_FAVORABLE_REGIME = False` should skip the regime/unknown gate entirely.
The DB shows 1,100 `regime_unfavorable` + 946 `regime_unknown` rejections. These are likely
historical rows from before the flag was set to False. Confirm with:
```sql
SELECT regime, COUNT(*) FROM hft_log WHERE reject_reason='regime_unfavorable'
  AND eval_ts_ms > [timestamp_when_flag_was_changed] GROUP BY 1;
```
If confirmed as legacy rows — no code fix needed, just document. If new rows are appearing —
find the secondary code path generating them.

### 4. SCALP_POLY Trigger Independence
**File:** `hft_engine.py:214–221`
**Problem:** Strategy 3 fires in `elif` branch — only when `signal is None or decision is None`.
Main engine updates `_hft_signal_ref` every 1m candle, so signal is almost never None during
active market hours. Strategy 3 only activates in complete signal gaps (overnight, weekends).
**Option A:** Convert to independent evaluation (both can run per tick):
```python
if signal is not None and decision is not None:
    await self._check_scalp_entry(book, contract)
if HFT_POLY_PRIMARY_ENABLED and POLY_ENABLED and self._poly_features is not None:
    if not self._scalp and not self._pending_entry:
        await self._check_poly_primary_entry(book, contract)
```
Note: if both run in the same tick, `_poly_features.update()` will be called twice — deduplicate
by calling it once and passing the result to both.
**Option B:** Keep elif but also evaluate when poly has strong signal divergence from candle.

### 5. kalshi_trades Pending Bloat
**File:** `main.py:_scrub_pending_orders`
**Problem:** `_scrub_pending_orders` skips rows where `contract.result is None` (contract not
yet settled). These accumulate as "pending" and pollute `ledger.py`. The 184 pending entries
seen in Session 10 are mainly from settled contracts from previous engine runs that weren't
resolved because the contract was still open at restart time and the scrub ran before settlement.
**Fix:** After the live session, run a reconciliation pass using:
```python
# Scrub all pending rows including expired-but-not-yet-settled
# Query Kalshi for fill status even if contract.result is None
# Mark filled orders as 'pending_settlement' instead of leaving as 'pending'
```
Or: separate HFT scalp entries from main engine entries — HFT scalps should not write to
`kalshi_trades` at all (they're already fully tracked in `hft_log`).

### 6. Synchronous DB Access Blocks Event Loop
**File:** `position_manager.py:162` (`_compute_dynamic_max_pct`)
**Problem:** Uses synchronous `sqlite3.connect()` inside a method called on every trade sizing.
This blocks the asyncio event loop while waiting for disk I/O.
**Fix:** Either:
a) Cache the result with a TTL (refresh every 60s or after each outcome recording):
```python
if time.time() - self._dynamic_cache_ts < 60.0:
    return self._dynamic_cache_pct
```
b) Switch to `aiosqlite` and make `size_trade()` async (larger refactor).
Option (a) is simpler and sufficient — dynamic sizing doesn't need sub-second freshness.

---

## P1.5 — Probability Economics (Fee-Aware EV)

These items are extracted from the research brief "Making BTC Bias Engine Improvements More Impactful."
The core insight: binary event contracts are probability instruments. Kalshi and Polymarket fees peak
near $0.50 (the max-uncertainty region). Raw directional accuracy improvements do not translate into
P&L unless they also improve fee-adjusted EV. Every gate and sizing decision should be re-examined
through that lens.

### 24. Fee-Adjusted EV Gate
**File:** `hft_engine.py` (`_check_scalp_entry`), `main.py` (main engine signal-to-trade path)
**Problem:** Current gating is directional confidence-based (`adj_conf > threshold`). Near the $0.50
price region, Kalshi fees are at their maximum — a trade that looks edge-positive on raw confidence
may be negative EV after fees. The two fee formulas (maker and taker) are both maximized near 50¢.
**Fix:** Compute fee-adjusted EV before every entry:
```python
# Taker fee on Kalshi is approximately: fee = p * (1-p) * FEE_RATE (simplified)
# Minimum edge threshold should scale with expected fee at that price level
fee_estimate = entry_price * (100 - entry_price) / 10000 * FEE_RATE_PCT
required_edge = BASE_EDGE_THRESHOLD + fee_estimate
if raw_edge < required_edge:
    reject("insufficient_fee_adjusted_edge")
```
**Impact:** Eliminates marginal trades in the 40–60¢ zone that are directionally right but
fee-negative. Especially relevant for main engine which trades full 15m windows (full fee exposure).

### 25. Volatility-Adaptive 1h TF Weighting
**File:** `consensus.py`, `config.py` (TF_WEIGHTS)
**Problem:** `TF_WEIGHTS["1h"] = 0.25` is a constant. Intraday BTC momentum research documents that
predictability strength correlates with volume/volatility intensity — slow TF signals are more
relevant when intraday volatility is low and trend is persistent; they are harmful when volatility
is high and fast TFs are "alive" with reversals.
**Fix:** Make 1h weight state-dependent, gated by the regime detector's ATR:
```python
# When ATR is elevated (high vol), reduce slow TF weight — fast signals dominate
# When ATR is compressed (trending/quiet), allow slow TF its full weight
vol_scalar = 1.0 - min(0.7, (current_atr / baseline_atr - 1.0) * 0.5)
adjusted_1h_weight = TF_WEIGHTS["1h"] * max(0.3, vol_scalar)
```
The P0 score cap (±50) is the immediate fix. This is the next-level upgrade after live data confirms
the cap is working.
**Dependency:** Requires ATR from regime_detector (already computed). Medium complexity.

### 26. Calibrated Probability Output from Consensus
**File:** `consensus.py`, `signal_intelligence.py`
**Problem:** `adjusted_confidence` is reported as a probability (e.g. 67.4%) but is derived from
`bull_conf - bear_conf` thresholds that were never calibrated against realized outcomes. The ML
calibration literature establishes that uncalibrated classifier outputs are systematically biased
(typically overconfident). Here, any output significantly above 50% may be systematically too
aggressive for fee-market decisions.
**Fix:**
1. Log `(predicted_conf, actual_outcome)` pairs persistently after each settlement.
2. After 100+ settlements per bucket, fit isotonic regression (or Platt scaling for small n):
```python
from sklearn.calibration import calibration_curve, CalibratedClassifierCV
```
3. Apply calibration map as a post-processing step between `adjusted_confidence` and the
   trade/no-trade threshold.
**Trigger:** After 100+ main engine settlements (currently at ~176, may already be viable for
a first-pass Platt fit on the settled rows).
**Impact:** Stops the system from trading 67% confidence as if it were 67% probability when the
actual base rate at those scores is, say, 50%. Directly prevents the systematic over-trading
in low-edge regions that is likely compounding the WR crisis.

### 27. Probability Scoring (Brier Score + Log Loss) in Logging
**File:** `signal_logger.py`, `main.py` (post-settlement hooks)
**Problem:** The engine logs win/loss counts and P&L, but not probability scoring metrics. Win
rate alone cannot detect miscalibration — a model that says 67% confidence and wins 60% of the
time looks "good" on WR but is overconfident on every trade.
**Fix:** After each settlement, compute and log:
```python
# Brier score: (predicted_prob - outcome)^2 — lower is better, 0.25 = random
brier = (predicted_prob - outcome) ** 2
# Log loss: -[y*log(p) + (1-y)*log(1-p)]
import math
log_loss = -(outcome * math.log(predicted_prob + 1e-9) + (1-outcome) * math.log(1 - predicted_prob + 1e-9))
```
Store in `kalshi_trades` as `brier_score` and `log_loss` columns. Run a rolling 50-trade Brier
score — when it rises above 0.25 (random), trigger the same degradation alert as WinRateTracker.
**Impact:** Makes every future improvement measurable in calibration terms, not just P&L. Without
this, good changes and bad changes both look like noise in the P&L signal.

### 29. Trade Decomposition Dashboard (Per-Trade Economics Columns)
**File:** `signal_logger.py` (HFT log schema), `kalshi_trades` schema, `hft_engine.py` (log call sites)
**Problem:** When a trade loses, the current data does not let you distinguish between:
- Model error (predicted probability was wrong)
- Fill economics (edge was real but fees/spread erased it)
- Execution quality (stale book, bad limit price, slippage)

Without these columns, every regression looks like a signal problem. In reality, a binary
prediction-market strategy can have a correct probability model but still be unprofitable if it
is consistently trading in the wrong part of the fee curve or filling at stale prices.

**Add these columns to `hft_log` and `kalshi_trades`:**
```
predicted_prob      REAL   -- consensus model's p(YES) at entry time, 0.0–1.0
entry_price_cents   INT    -- actual fill price in cents
fee_estimate_cents  REAL   -- estimated fee paid (taker or maker formula)
maker_taker         TEXT   -- 'maker' or 'taker' (derived from fill vs limit)
spread_cents        INT    -- bid-ask spread at entry time
book_age_ms         INT    -- milliseconds since last book update
realized_net_edge   REAL   -- (outcome_cents - entry_price_cents) - fee_estimate_cents
brier_score         REAL   -- (predicted_prob - outcome)^2 per trade
log_loss            REAL   -- -[y*log(p) + (1-y)*log(1-p)] per trade
```

**Answers this enables:**
- "Is the model wrong or are fill economics bad?" → compare `brier_score` vs `realized_net_edge`
- "Are we consistently taker when we should be maker?" → aggregate `maker_taker` × `fee_estimate`
- "Does book staleness predict loss?" → correlate `book_age_ms` vs `realized_net_edge`
- "Which price bucket has best fee-adjusted edge?" → bucket `entry_price_cents` × mean `realized_net_edge`

**Implementation note:** `predicted_prob` should be logged at decision time before the order is
placed. `realized_net_edge` is computed post-settlement when outcome is known and backfilled.
`fee_estimate_cents` uses the Kalshi taker/maker fee formula at the known entry price.

### 28. Maker-First Execution Routing (Elevation of P3 #16)
**File:** `hft_engine.py:672–674`
**Current priority:** P3 (architecture). **Revised priority:** P1.5
**Rationale:** Polymarket rebates makers in fee-enabled markets. Kalshi has a separate maker fee
formula. The research brief makes explicit that Polymarket redistributes fees to makers as rebates.
This means posting limit orders at bid+1 (when spread ≥ 2¢) isn't just aesthetically preferable —
it materially improves fee-adjusted EV for every HFT scalp.
**Fix:** (same as P3 #16, but implement now, not after Kalshi WS)
```python
if spread >= 2:
    limit = market_bid + 1   # post inside spread, capture maker rebate
else:
    limit = market_ask        # 1¢ spread: accept taker cost, fill immediately
```
**Impact:** At 310+ fills/session, shaving even 0.1¢ effective cost per fill is material.

---

## P2 — Data Integrity

### 7. Duplicate `import` Statements Inside Methods
**File:** `main.py:349,382` (`_restore_last_signal_ts`, `_scrub_pending_orders`)
**Problem:** `import aiosqlite` and `from config import TRADES_DB_PATH` are inside method bodies.
These run on every call, not once at module load. In Python this is fine for correctness but
a code smell and marginally slower.
**Fix:** Move both imports to the module-level imports section of `main.py`.

### 8. Duplicate `import datetime` in `bias_engine.py`
**File:** `bias_engine.py:157`
**Problem:** `from datetime import datetime, timezone` is inside `BiasEngine.update()`, called
for every single candle (~1/minute per TF × 5 TFs = high frequency). Module-level import
is already standard practice.
**Fix:** Move to module top.

### 9. Duplicate Session Label Logic
**Files:** `signal_intelligence.py:69–80` (`_session_label`) and `strategy_index.py:17–22` (`get_session`)
**Problem:** Two functions doing the same thing but returning different string formats:
- `_session_label()` → "Asia", "London", "NY-Open", "NY-Prime", "After-Hrs"
- `get_session()` → "ASIA", "LONDON", "NY_OPEN", "NY_PRIME", "AFTER_HRS"
The `_REGIME_SESSION_WR` table in `signal_intelligence.py` uses the hyphenated format.
The `StrategyIndex._table` uses uppercase underscored format.
This is a latent bug — they're not interchangeable.
**Fix:** Unify into a single function in `config.py` or a shared `utils.py` and replace
both call sites.

### 10. BAD_HOURS Defined in Three Places
**Files:** `signal_intelligence.py:368`, `strategy_index.py:14`, `config_phase3.py` (BAD_UTC_HOURS)
**Problem:** Bad hours filter is duplicated as `BAD_HOURS = {9, 12, 15, 21}` in
`SignalFilter`, `_BAD_HOURS = {9, 12, 15, 21}` in `strategy_index.py`, and `BAD_UTC_HOURS`
in `config_phase3.py`. If one changes, the others don't.
**Fix:** Single source of truth in `config.py`:
```python
BAD_UTC_HOURS: frozenset[int] = frozenset({9, 12, 15, 21})
```
Import from there everywhere.

### 11. Outdated Docstring in `SignalFilter`
**File:** `signal_intelligence.py:358`
**Problem:** Class docstring says "UTC 9, 15, 16" but the actual `BAD_HOURS` set is `{9, 12, 15, 21}`.
Finding comment at line 367 says "12h+21h added", but the docstring was never updated.
**Fix:** Update docstring to reflect `{9, 12, 15, 21}`.

---

## P3 — Architecture / Performance

### 12. Kalshi WebSocket Order Book (Highest-Impact Remaining Architecture Gap)
**File:** New `kalshi_market_data.py` (see research paper spec)
**Problem:** REST polling for Kalshi order book results in 1.5–4s data age. Adaptive polling
(Session 09) brought this to 1.5s minimum, but even 1.5s stale book causes the stale-book
gate to block valid entries. WebSocket would reduce this to ~0ms.
**Implementation order per research paper:**
1. Subscribe to Kalshi WS for the active contract
2. Maintain local snapshot + delta application
3. Sequence integrity check (detect missed messages → request full snapshot)
4. Reconnect with exponential backoff
5. Expose `book_age_ms` from last WS message timestamp
**Impact:** Removes the #1 operational bottleneck. Would eliminate the 4,151
`entry_price_out_of_band` rejections by having fresh prices; more importantly removes
the need for the adaptive poll rate hack.

### 13. WinRateTracker Baseline Calibration
**File:** `signal_intelligence.py:107`
**Problem:** `BASELINE_WR = {0: 67.57, 1: 75.56, 2: 84.97, 3: 95.82}` was set from an
early Pine Script backtest. Live performance shows 36–60% WR. The degradation threshold
(`baseline - 10%`) means bucket 1 doesn't flag as degraded until live WR drops below 65.56%
— which is above our actual performance. The degradation check is therefore too lenient to be
actionable.
**Fix:** Recalibrate baselines from the actual live `kalshi_trades` settlement data.
Also consider: should the degradation check pause trading, reduce size, or just log?
Currently it rejects signals but the check requires 10 trades per bucket minimum —
may never trigger with low sample sizes.

### 14. `_compute_momentum` in `polymarket_features.py` Calls `time.time()` Twice
**File:** `polymarket_features.py:184`
**Problem:** `now = time.time()` is called inside `_compute_momentum()` but `now` was already
captured at line 109 in `update()`. If clock advances between calls (negligible but inconsistent),
and more importantly it's redundant.
**Fix:** Pass `now` as a parameter to `_compute_momentum(now)` and `_change_over(seconds, mid, now)`.

### 15. `_change_over` Iteration Pattern is O(n) per call
**File:** `polymarket_features.py:160–174`
**Problem:** For each window (1s, 3s, 10s), iterates the entire `_history` deque to find
the baseline. Called 3× per `update()` tick with a 30-second deque that could hold ~7 entries
(at 4s poll rate) or ~20 entries (at 1.5s poll rate). Not a performance problem at this scale,
but the loop logic is subtle — finding the last element BEFORE `target_ts` by breaking when
`ts >= target_ts` and keeping the prior value.
**Clarify:** Add an inline comment explaining the loop logic. Or refactor using `bisect` on a
sorted timestamp list for O(log n).

### 16. HFT Limit Price Calculation is Always `market_ask` for 1¢ Spreads
**File:** `hft_engine.py:672–674`
```python
step = max(1, spread // 2)    # spread=1 → step=1
limit = min(market_ask, market_bid + step)  # min(ask, bid+1) = ask when spread=1
```
With 1¢ spreads (the common case in liquid contracts), this always resolves to `market_ask`.
The mid-spread calculation has no effect. This means we're always crossing the spread, never
posting passively. Given we want maker-first execution per the research paper, consider:
- Post at `market_bid + 1` (just inside spread) when spread ≥ 2¢
- Accept `market_ask` only when spread = 1¢ (already fair)
The current behavior is functionally fine since fills at `market_ask` are immediate, but it's
not the maker-biased execution the research paper specifies.

### 17. TP Hold Polling is Wasteful
**File:** `main.py:_service_tp_hold`
**Problem:** `_service_tp_hold` calls `get_order(tp_order_id)` on every 1m candle while
a TP hold is active. That's up to 3 API calls/minute for the 3-minute timeout window.
**Fix:** Cache the last `get_order` result and only re-poll every 30s unless a fill
confirmation signal arrives. Or use WS fill events when Kalshi WS is implemented.

### 18. Gate Numbering in `_check_scalp_entry` is Inconsistent
**File:** `hft_engine.py:482–669`
**Comments say:** Gate 0 → Gate 1 → Gate 2 → Gate 3 → Gate 4 → Gate 5 → Gate 3 (submit lock)
→ Gate 6 → Gate 4+5
The renumbering mid-function is confusing.
**Fix:** Renumber gates sequentially or use descriptive labels without numbers.

### 19. `position_manager.py` Has Mixed Sync/Async Patterns
**File:** `position_manager.py`
**Problem:** `execute()` is async (awaits API calls) but `size_trade()` is sync (blocks on DB).
The class is used from async context but has sync state mutations. Works correctly in asyncio
single-threaded model but makes the interface inconsistent and harder to reason about.
**Consider:** Make `size_trade()` async and cache DB calls.

---

## P4 — Future Features (Post Calibration Data)

### 20. Polymarket Calibration Loop
**File:** `signal_fusion.py` (FUSION_W_* constants)
**Trigger:** After 50+ `hft_log` rows with `poly_*` columns populated
**Work:** Query `drift_1s_cents - fill_cents` and `drift_3s_cents - fill_cents` grouped by
`fusion_fair_cents` bucket. Tune `FUSION_W_KALSHI_MICROPRICE`, `FUSION_W_POLY_SIGNAL`
weights to minimize adverse selection. Also tune `FUSION_POLY_ALPHA/BETA/GAMMA/DELTA`.

### 21. Live WR Feedback Loop into StrategyIndex
**File:** `strategy_index.py:34` (`live_wr: Optional[float] = None`)
**Problem:** `StrategyConfig.live_wr` field exists but is never set. The `effective_wr`
property always returns `expected_wr` (backtest value).
**Fix:** Wire `WinRateTracker` per-bucket outcomes into `StrategyIndex.live_wr` so position
sizing and edge checks automatically adapt as live data accumulates.

### 22. BTC Spot Alignment Feature Missing from Fusion
**File:** `signal_fusion.py:154–169`
**Problem:** The fused fair value uses `w_kalshi_microprice + w_kalshi_mid + w_poly_signal`.
The research paper specifies a `w_btc_spot_context` term. BTC spot alignment features
(`btc_move_since_window_start`, `poly_vs_spot_divergence`) are listed in the research paper
spec (Section 6.3) but are not implemented in `polymarket_features.py`.
**When to implement:** After Kalshi WS and Poly calibration — adds complexity, defer until
core is profitable.

### 23. Contract Equivalence Layer for Cross-Venue Arb
**File:** New `contract_equivalence.py` or inside `signal_fusion.py`
**Problem:** Any cross-venue signal comparison (Kalshi vs Polymarket "same BTC question") is
implicitly treated as semantically equivalent. In practice, Kalshi contracts settle under CFTC-
regulated exchange rules with specific contract specifications; Polymarket settles via the UMA
Optimistic Oracle, which can be disputed. Resolution timing, source, and criteria can differ even
for "same-sounding" questions.
**Risk:** A "same" question that settles differently turns an arb signal into a directional bet with
hidden basis risk.
**Fix:** Before `signal_fusion.py` computes cross-venue differences:
1. Add a `ContractEquivalence` enum: `STRICT | APPROXIMATE | INCOMPATIBLE`
2. Tag each fusion input with its equivalence class
3. Apply higher edge thresholds (or skip entirely) for APPROXIMATE pairs
**Priority:** P3, but must be done before cross-venue arb is relied on for sizing decisions.

### 23b. Options-Implied Probability Anchor
**File:** New `btc_options_anchor.py`, consumed by `signal_fusion.py`
**Problem:** The engine has no external probability benchmark. Without one, it can't distinguish
"my model says 67% because of a signal" from "my model says 67% because of a calibration artifact."
Deribit DVOL and option-chain implied distributions provide a market-derived probability that BTC
finishes above a given strike at expiry — structurally identical to a KXBTC15M YES price.
**Implementation:**
1. Pull Deribit option quotes for the nearest expiry bracketing the contract window
2. Compute risk-neutral probability via put-call parity or Black-Scholes inversion:
   `P_RN = N(-d2)` where d2 uses implied vol from at/near-the-money contracts
3. Use `P_RN` as a prior/sanity bound: if `consensus_prob` diverges from `P_RN` by >15pp,
   apply a confidence penalty before trade sizing
**Trigger:** P4 — after core calibration data exists. The anchor is only as useful as the
precision of the strike/expiry mapping to your specific KXBTC15M contracts.

### 23c. Order Group Controls Not Implemented
**File:** New — Kalshi order groups (from research paper Section 7.2 and 11.2)
**Problem:** Kalshi supports order groups that enforce a contracts-per-15s cap, preventing
burst risk. The research paper specifies `use_order_groups: true` with `order_group_contracts_limit: 20`.
Not implemented. Low priority while size is 1 contract per trade.
**When to implement:** When scaling to multi-contract positions.

---

## Quick Wins (< 30 min each)

- [x] **Cap TF score in consensus.py** — 2 lines, P0 *(shipped Session 10)*
- [x] **Increase warm_start limit to 250** — 1 line, P0 *(shipped Session 10)*
- [x] **Maker-first limit price in hft_engine.py** — 3 lines, P1.5 *(shipped Session 09)*
- [x] **Add fee-estimate to scalp entry gate** — ~8 lines, P1.5 *(shipped Session 09)*
- [x] **Add Brier score column to kalshi_trades** — schema + 2 log lines, P1.5 *(shipped Session 09)*
- [x] **Move `import aiosqlite` to module level in main.py** — trivial *(shipped Session 12)*
- [x] **Move `from datetime import ...` in bias_engine.py to module top** — trivial *(shipped Session 12)*
- [x] **Update SignalFilter docstring** (bad hours list) — 1 line *(shipped Session 12)*
- [x] **Cache dynamic sizing result** — ~10 lines *(shipped Session 12)*
- [ ] **Tighter exit ladder (1¢ steps instead of 3¢)** — `_exit_scalp()` step pattern, P2, better fill price on exits
- [ ] **HFT re-entry after TP in same direction** — when signal persists and >8 min to expiry, P2
- [ ] **Main engine maker-first entry** — limit at `bid+1` instead of always crossing at ask, P2

---

## Implementation Order (Revised)

This sequence minimizes the chance of improving one part of the system while remaining blind to
whether the improvement is real. Measurement comes immediately after the structural fix so that
every subsequent change has a signal to track against.

1. **P0 structural fixes** (#1, #2) — stop the 1h bleeding. Costs nothing to ship.
2. **Measurement layer** (#27 Brier/log-loss logging, #29 trade decomposition columns) — before
   anything else, instrument the system. Without per-trade `predicted_prob`, `fee_estimate`,
   `maker_taker`, `spread`, `book_age_ms`, `realized_net_edge`, you cannot tell whether a
   change improved signal quality or just shifted execution noise.
3. **Fee-EV gate** (#24) — fee structure is static and documented; implement immediately once
   you have the decomposition columns to verify it's rejecting the right trades.
4. **Maker-first routing** (#28) — 3-line change, material at 310+ fills/session.
5. **Kalshi WebSocket** (#12) — removes stale-book as a confound in all subsequent analysis;
   also provides the `book_age_ms` column in #29 with real precision.
6. **Post-hoc calibration** (#26) — once 100+ Brier-scored settlements exist with stable signals.
7. **Volatility-adaptive 1h weighting** (#25) — after calibration confirms the signal layer is
   trustworthy. The ATR scalar is only meaningful once the base signal is calibrated.
8. **Contract equivalence + options anchor** (#23, #23b) — last, when cross-venue arb is
   relied upon for sizing decisions.

---

## Summary Table

| # | Area | Priority | Files | Impact |
|---|------|----------|-------|--------|
| 1 | 1h TF score cap | P0 | consensus.py | Fixes persistent directional bias |
| 2 | Warm-start depth | P0 | main.py | Fixes 1h cold start saturation |
| 3 | Regime rejection audit | P1 | hft_engine.py | Confirm/close known bug |
| 4 | SCALP_POLY trigger | P1 | hft_engine.py | Activates dead Strategy 3 |
| 5 | kalshi_trades bloat | P2 | main.py, signal_logger.py | Fixes ledger output |
| 6 | Sync DB in async path | P1 | position_manager.py | Removes event loop block |
| 7-11 | Code hygiene | P2 | Various | Prevents future bugs |
| 12 | Kalshi WebSocket | P3 | New file | Removes REST bottleneck |
| 13 | WinRateTracker recal | P3 | signal_intelligence.py | Makes degradation check meaningful |
| 14-19 | Architecture cleanup | P3 | Various | Maintainability |
| 20-23d | Future features | P4 | Various | Post-calibration |
| 24 | Fee-adjusted EV gate | P1.5 | hft_engine.py, main.py | Stops fee-negative trades near 50¢ |
| 25 | Volatility-adaptive 1h weight | P1.5 | consensus.py | Slow TF dynamically scaled by ATR |
| 26 | Calibrated probability output | P1.5 | consensus.py | Corrects systematic overconfidence |
| 27 | Brier score logging | P1.5 | signal_logger.py | Makes calibration measurable |
| 28 | Maker-first routing (elevated) | P1.5 | hft_engine.py | Fee-adjusted EV improvement every fill |
| 23 | Contract equivalence layer | P3 | signal_fusion.py | Prevents false arb basis risk |
| 23b | Options-implied probability anchor | P4 | New file | External probability sanity bound |
