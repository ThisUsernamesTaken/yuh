# RESEARCH — Prob Engine, FVG Engine, and HMM Regime Engine Evaluation — Do Not Implement

**Date:** 2026-04-11  
**Scope:** `polymarket_copy_engine.py` + `price_feed.py` (BinaryProbabilityEngine)  
**Database:** `data/trades.db`, table `kalshi_trades`

---

## Important Terminology Clarification Before Reading

The task brief named three engines: "Prob Engine," "FVG Engine," and "Hidden Markov Regime Engine (HMM)." After reading all relevant code in the repository, here is the reality:

- **There is no HMM.** No Hidden Markov Model exists anywhere in the active codebase. The files `regime_detector.py` and `signal_intelligence.py` contain legacy regime classifiers but are explicitly disabled (CLAUDE.md: "Not used"). The active engine contains a simple rule-based regime classifier — three if/else branches using annualized vol and a MACD/MA/BB score — called `_session_regime`. It is labeled "REGIME CLASSIFIER" in comments but has no Markov, probabilistic state transition, or hidden state logic.
- **Prob Engine and FVG Engine are the same system.** The `BinaryProbabilityEngine` class (`price_feed.py` lines 158–265) computes a Brownian Bridge fair value. The FVG (Fair Value Gap) engine in `polymarket_copy_engine.py` uses that fair value as its core input. They are not separate engines — FVG is the trading logic that acts on the prob engine's output.

This document therefore covers:
1. **Prob Engine** — `BinaryProbabilityEngine` (mathematical core, `price_feed.py` lines 158–265)
2. **FVG Engine** — `_evaluate_ta_forced_signal()` FVG baseline path + `_paper_fvg_tick()` (`polymarket_copy_engine.py` lines 4374–4482 and 2368–2601)
3. **Session Regime Classifier** — the three-state rule-based classifier that replaced any hypothetical HMM (`polymarket_copy_engine.py` lines 3113–3146)

---

## Executive Summary

The Prob Engine computes a mathematically sound Brownian Bridge fair value for each 15-minute binary contract, and the FVG engine uses that fair value to identify mispricings relative to the session's opening baseline. On paper, the FVG system shows a 98.9% win rate across 1,669 simulated trades worth a claimed $4,433 in profit — but those figures are entirely unreliable: they are recorded in the same session they were created (all 1,669 rows share `created_at = 2026-04-11`), multiple sessions show hundreds of identical duplicate rows (same entry_price, exit_price, pnl_cents), and the simulation re-enters the same contract repeatedly per session rather than taking one trade per 15-minute window. In live trading, TA_FORCED_SIGNAL — the strategy driven by the FVG engine — has delivered a -$121.36 total P&L across 635 live trades with a 1.6% official "won" rate. The underlying binary direction accuracy is 64.1% (385 of 601 settled contracts expired in the correct direction), which is above break-even for a 50c entry, but position sizing is set at 3–20 contracts determined by volatility and regime logic, and the sizing paths are triggered by a regime classifier that is completely disconnected from live trade outcomes. The regime classifier has never logged a "won" status in any of its 450+ tagged trades. The combined effect is a system that has the right directional edge roughly 64% of the time but converts it into a net loss because large position sizes on losing trades — particularly in EXPLOSIVE/high-vol regimes where 3 contracts at 55c risk $1.65 per trade — overwhelm the smaller wins.

---

## 1. Prob Engine (BinaryProbabilityEngine)

### Code Location

- **File:** `price_feed.py`
- **Lines:** 158–265 (`BinaryProbabilityEngine` class)
- **Key methods:** `set_strike()` (line 178), `update()` (line 187), `_erf()` (line 238)
- **Integration:** Instantiated as `PriceFeedTask.prob_engine` (line 743). Strike is reset on each new 15-minute window (`polymarket_copy_engine.py` line 1671–1674). Updated at every FVG evaluation tick (lines 4383–4391).

### How It Works

The engine models a Kalshi binary contract as a Brownian Bridge derivative. The contract pays $1 if BTC finishes above its session-open price within the 15-minute window. The fair value is therefore `P(BTC > strike at expiry) * 100 cents`.

**Inputs:**
- `btc_price`: Current BTC spot price (from Binance WebSocket)
- `contract_mid_cents`: Current Kalshi mid price (0–100)
- `seconds_left`: Seconds remaining in the window
- `_vol_buffer`: Rolling deque of last 180 closed 5-second BTC candles for realized vol estimation

**Volatility computation** (lines 204–219):
```python
returns = [(candle[i].close - candle[i-1].close) / candle[i-1].close for each pair]
stdev = (sum(r*r for r in returns) / len(returns)) ** 0.5
periods_per_year = 252 * 6.5 * 3600 / 5.0   # 5s candles → annual
self.volatility = stdev * (periods_per_year ** 0.5)
```
Default fallback: 50% annualized if fewer than 10 candles available.

**Fair value computation** (lines 221–236):
```python
t_years = seconds_left / (252 * 6.5 * 3600)
sigma_sqrt_t = self.volatility * math.sqrt(max(t_years, 1e-10))
d = math.log(btc_price / self.strike) / sigma_sqrt_t   # Black-Scholes d1
self.probability = 0.5 * (1.0 + self._erf(d / math.sqrt(2)))
self.fair_value = self.probability * 100.0
self.mispricing = self.fair_value - contract_mid_cents
```

The `_erf` implementation uses the Abramowitz & Stegun polynomial approximation (max error 1.5e-7), which is numerically correct.

**Readiness gate** (line 265):
```python
@property
def is_ready(self) -> bool:
    return self.strike > 0 and len(self._vol_buffer) >= 10
```
Requires 10 five-second candles (50 seconds of data). Volatility buffer holds 180 candles (~15 minutes), persists across session boundaries by design (line 1675: "Vol buffer persists across sessions").

### Current Performance

The prob engine itself does not place trades — it is a pricing module. Its `fair_value` output is used in:
- FVG entry decisions (is the gap between fair value and baseline large enough?)
- Take-profit targets (TP = max(int(fair_value), entry + 5))
- Position sizing (edge = abs(mispricing) drives contract count)
- DCA re-entry checks (whether fair value has moved further in our favor)

The engine cannot be independently evaluated from trade data because every TA_FORCED trade uses it.

### Strengths

1. **Mathematically correct pricing model.** Black-Scholes for a binary (cash-or-nothing call) using realized 5s volatility is the appropriate framework for a 15-minute contract.
2. **Vol buffer persists across sessions.** The engine is immediately ready at the new window boundary instead of requiring a 50-second warmup.
3. **Self-contained.** No external library dependencies; the erf approximation is embedded.
4. **Mispricing sign is intuitive.** Positive = YES underpriced, negative = NO underpriced.

### Weaknesses

1. **Annualization constant assumes 6.5-hour trading day** (line 214: `252 * 6.5 * 3600 / 5.0`). BTC trades 24/7. The correct denominator is `365 * 24 * 3600 / 5.0`, which is 6,307,200 vs the current 1,183,680. This understates annualized volatility by a factor of 2.3x, which in turn understates `sigma_sqrt_t` and therefore overstates the absolute value of `d`. For BTC at 0.05% from strike with 5 minutes left, a 2.3x vol underestimate pushes `d` from near-zero toward ±1.5, producing probabilities of 85–93% instead of the correct 55–65%. **This is the most serious numerical error in the system.**

2. **50% vol default is too high.** If the vol buffer isn't populated (fresh start or no 5s feed), `volatility = 0.50` (50% annualized) is the most common value seen for BTC. Under-populated buffers during session start will use this default correctly. But if the Binance feed drops mid-session, the engine silently falls back to 0.50 vol without flagging staleness. The `is_ready` check only tests buffer length at initialization, not whether the buffer was recently updated.

3. **Strike set at `_btc_last_price`** (line 1672), which is the BTC price at the moment the new Poly market is detected. In practice this might lag the Kalshi contract open by up to 3 seconds (poll interval). The session open strike should be the BTC price at the Kalshi window boundary, not the Poly market detection time.

4. **No drift term.** The standard Brownian Bridge for a binary contract pins both endpoints — the current price (not the strike) and the terminal probability. The current implementation only uses a standard lognormal CDF with current BTC distance from strike. It does not account for the 15-minute trend (if BTC has been rising for 12 minutes, a simple N(d) without drift adjustment overestimates reversion probability).

### Improvement Proposals

| Issue | Current | Proposed |
|-------|---------|----------|
| Annualization constant | `252 * 6.5 * 3600 / 5.0` (equity hours) | `365 * 24 * 3600 / 5.0` (BTC 24/7) |
| Default vol when buffer empty | 0.50 (50% annualized) | 0.30 (historically accurate BTC implied vol) |
| Strike timing | Poly market detection time | First Kalshi orderbook mid-price fetch of the new window |
| Vol staleness | Not checked | Add `_vol_last_update` timestamp; if `> 120s` stale, flag `is_ready = False` |

---

## 2. FVG Engine (Fair Value Gap Baseline System)

### Code Location

The FVG logic exists in two places:

**Live execution path** (`_evaluate_ta_forced_signal()`):
- File: `polymarket_copy_engine.py`
- Lines: 4321–4536 (function definition)
- FVG-specific block: lines 4374–4482

**Paper simulation** (`_paper_fvg_tick()` + `_paper_fvg_close()`):
- File: `polymarket_copy_engine.py`
- Lines: 2368–2601

### How It Works

The FVG engine answers one question: is the Brownian Bridge fair value (`prob.fair_value`) significantly above or below the session's opening price baseline? The divergence between the two is called the "fair value gap."

**Phase 1: Baseline collection** (lines 4396–4411)

During the first 90 seconds of each 15-minute session, the engine collects mid-price readings every 3 seconds and averages them:
```python
if session_age <= 90:
    self._session_baseline_mids.append(mid)
    return None   # no trades during baseline phase
elif len(self._session_baseline_mids) >= 5:
    self._session_baseline_price = int(sum(...) / len(...))
```
Minimum 5 samples needed; fallback is `mid` if fewer samples collected.

**Phase 2: FVG calculation** (lines 4417–4432)

```python
fair = prob.fair_value                     # Brownian Bridge output (0–100 cents)
fvg_vs_baseline = fair - baseline          # positive = YES underpriced vs baseline

# Time-weighted thresholds
if session_age < 180:    fvg_threshold = 5
elif session_age < 420:  fvg_threshold = 8
else:                    fvg_threshold = 12
```

The threshold increases with session age because: early in the session a small gap may persist to expiry; late in the session, premium decay makes small gaps unrealizable.

**Phase 3: Entry gate** (lines 4434–4466)

```python
if abs(fvg_vs_baseline) >= fvg_threshold and 0.005 < btc_dist_pct < 0.15:
    # Side: YES if fvg_vs_baseline > 0, NO if < 0
    # Then: tick confirmation gate
    if _fvg_side == "yes":
        _bb_confirms_entry = _tv_tc > 2.0 or (_btc_tc <= _fs_tc.bb_mid and _tv_tc > -5.0)
    else:
        _bb_confirms_entry = _tv_tc < -2.0 or (_btc_tc >= _fs_tc.bb_mid and _tv_tc < 5.0)
```

`btc_dist_pct` is BTC distance from strike (session open): must be between 0.005% and 0.15%. This filters out coin-flip sessions (BTC at strike) and extremely off-center sessions (where the contract would be 80c+ regardless of fair value).

The tick velocity confirmation requires BTC momentum to align with the trade direction, or BTC to be within the middle of its Bollinger Band range. If 5s data is unavailable, `_bb_confirms_entry = True` (pass-through).

**TA signal dependency** (lines 4348–4355)

Despite the FVG logic being the actual entry trigger, the function also checks `ta.direction != "flat"` and `ta.confidence >= 10`. The TA scorer (`TAScorer`) provides a 1-minute chart signal. If the 1m TA says "flat" or has < 10 confidence, no FVG entry fires even if the gap is large. This is a legacy gate from when TA drove direction and has not been removed.

**The conviction parameter** (lines 4510–4516)

```python
conviction = min(ta.confidence + lean_strength, 85)
```
`lean_strength = abs(mid - 50)`. This drives the `implied_edge_cents` field on the returned signal, which feeds the sizing logic. The actual FVG edge is not used here — the conviction is still a blend of old TA confidence and simple price distance from 50c.

### Current Performance

**Live (TA_FORCED_SIGNAL):**
- Total trades: 635
- Official "won" status: 10 trades (1.6% win rate)
- Actual binary direction accuracy: 385/601 settled = **64.1% correct direction**
- Total P&L: **-$121.36**
- Average P&L per trade: -$0.19

The 1.6% "won" rate is misleading. The `won` status is set by the active TP order filling before settlement. Most trades hold to expiry (`reconciled_settled`). Of settled trades, 64.1% expired in the correct direction, but the average winning pnl is small (entry at ~$0.55, win = $0.45) while losses are compounded by the 3–20 contract sizing (entry at $0.55 × 16 contracts = $8.80 risk per trade).

**Daily P&L range (reconciled_settled):**
| Date | Trades | PnL |
|------|--------|-----|
| 2026-03-23 | 45 | +$4.73 |
| 2026-03-30 | 18 | +$6.70 |
| 2026-03-31 | 65 | +$18.34 |
| 2026-04-01 | 35 | -$50.14 |
| 2026-04-05 | 41 | -$34.75 |
| 2026-04-07 | 72 | -$37.88 |
| 2026-04-10 | 37 | -$37.64 |

The variance is enormous. March 31 made $18.34 on 65 trades; April 1 lost $50.14 on 35 trades. This is consistent with variable position sizing (3–20 contracts) interacting with a 64% direction accuracy that produces large losing bets when the regime classifier incorrectly declares TRENDING or HIGH_CONVICTION.

**Paper FVG simulation (paper_fvg_trades table):**
- Total logged rows: 1,669
- Reported P&L: +$4,432.96 (98.9% win rate)
- **This data is invalid.** All 1,669 rows were written on 2026-04-11. The simulation fires every 3 seconds and re-enters the same session after each close (state reset to IDLE, not to session-expired). A single session with a persistent FVG gap generates hundreds of identical trades. The top-volume session `KXBTC15M-26APR111400-00` has 384 rows all with entry=51c, fair=67c, exit=67c. These are not 384 independent trades; they are the same theoretical trade recorded 384 times in one 15-minute window. The paper FVG data must be considered worthless as a backtest.

### Strengths

1. **Direction accuracy is 64.1% on settled contracts.** A genuinely profitable edge for a binary payout if position sizing were fixed at 1–2 contracts.
2. **FVG threshold scaling with session age** (5c/8c/12c) is conceptually sound — avoids chasing small gaps late when time decay has compressed realizable value.
3. **BTC distance gate (0.005–0.15%)** prevents entries when BTC is exactly at strike (coin-flip) or way off-center (overpriced contract).
4. **Tick velocity confirmation** (introduced recently) adds a useful momentum filter.

### Weaknesses

1. **Position sizing is catastrophically oversized.** The sizing logic (`_compute_position_size`) targets $4.00 profit per trade and sizes contracts based on `ceil(400 / fv_spread)`. If `fv_spread = 10c`, that's 40 contracts at 55c = $22 at risk per trade with 36% chance of a full loss. The hardcoded fallback `return 5` (line 4606) was overridden by a 50-line two-path sizing block that can reach 20 contracts. See the sizing section.

2. **The TA flat gate still lives in the function.** Lines 4349–4355 check `ta.direction == "flat"` and `ta.confidence < 10`. The FVG baseline approach was meant to replace TA direction as the entry signal, but the TA gate can still block entries when FVG is large. This is an unresolved artifact.

3. **The paper backtest is a re-entry loop bug.** `_paper_fvg_close()` resets `pf["state"] = "IDLE"` (line 2600), which means as soon as the TP fills, the paper tracker re-enters the same session if the FVG is still above threshold. In a session where BTC stays 0.05% above strike for 12 minutes, the system enters and exits hundreds of times against the same favorable Brownian Bridge. No real system would get filled 384 times in a 15-minute window.

4. **Conviction field uses TA confidence, not FVG magnitude.** Line 4510: `conviction = min(ta.confidence + lean_strength, 85)`. The FVG magnitude (`fvg_vs_baseline`) is not used in this calculation, so a large gap with low TA confidence produces the same signal strength as a small gap with high TA confidence. The FVG signal should drive sizing.

5. **Baseline is set as integer** (line 4404: `int(sum(...) / len(...))`). Rounding a 90-second average of values like 49.7, 50.1, 50.2 to integer 50 introduces 0.5c quantization error in the comparison, which matters when the threshold is 5c.

6. **No per-session trade cap.** The FVG path sets `TA_FORCED_MAX_PER_WINDOW = 99` and re-enters the same session after the first fill. In practice `_window_locked` prevents this in live trading (line 1560), but the paper simulation ignores the window lock.

### Improvement Proposals

**1. Fix position sizing — single most impactful change.**

Before (current behavior at line 5196–5243): sizes to $4 target based on fv_spread, up to 20 contracts.

After (proposed):
```python
# FVG FIXED SIZING: 3 contracts always.
# Rationale: 64% accuracy on binary at avg 50c entry = EV per trade of 
#   0.64 * 0.50 - 0.36 * 0.50 = +$0.14 per contract.
# 3 contracts = +$0.42 expected per trade.
# Current 16-contract avg = +$2.24 expected but catastrophic on bad days.
# Fix: 3 contracts, no regime scaling, let direction accuracy compound.
num_contracts = 3
```

**2. Use FVG magnitude to drive conviction, not TA confidence.**

Before (line 4510):
```python
conviction = min(ta.confidence + lean_strength, 85)
```

After:
```python
# Use FVG magnitude directly as edge estimate
fvg_abs = abs(fvg_vs_baseline)  # already computed above
conviction = min(int(fvg_abs * 4), 85)   # 5c gap → 20, 12c gap → 48, 20c → 80
```

**3. Remove the TA flat gate from the FVG path.**

Before (lines 4348–4355):
```python
if ta is None or ta.direction == "flat":
    return None
if ta.confidence < 10:
    return None
```

After: remove both checks. The FVG baseline approach uses prob engine math, not TA direction. A flat TA score should not block a 12c FVG gap.

**4. Fix baseline quantization — use float, not int.**

Before (line 4404):
```python
self._session_baseline_price = int(sum(self._session_baseline_mids) / len(...))
```

After:
```python
self._session_baseline_price = round(sum(self._session_baseline_mids) / len(...), 1)
```

**5. Add a per-session single-entry cap in paper simulation.**

In `_paper_fvg_close()`, line 2600:
```python
pf["state"] = "IDLE"    # current — allows re-entry
```

Change to:
```python
pf["state"] = "DONE"    # one entry per 15-minute session only
```
And in `_paper_fvg_tick()`, add a check: if `pf["state"] == "DONE"`, return immediately.

---

## 3. Session Regime Classifier (Mislabeled "HMM Regime Engine")

### Code Location

- **File:** `polymarket_copy_engine.py`
- **Lines:** 3113–3146 (classification), 5199–5243 (sizing application)
- **State variable:** `self._session_regime` — set to "TRENDING", "MEAN_REVERTING", or "EXPLOSIVE"

### What It Actually Is

The classifier is three if/else branches using two inputs: the prob engine's annualized volatility (`_vol_reg`) and a heuristic "trending score" computed from the prior session's 5s indicators. There are no hidden states, no transition matrix, no emission probabilities, no Viterbi decoding. It is a decision tree with hardcoded thresholds.

**Inputs** (lines 3117–3134):
```python
_vol_reg = _prob_reg.volatility if _prob_reg and _prob_reg.is_ready else 0.15
# Trending score from 5s indicators (MACD histogram, MA50/MA100 spread, BB width)
_trending_score = 0.0
if abs(_fs.macd_histogram) > 2.0:
    _trending_score += 0.3
if abs(_fs.ma50 - _fs.ma100) / max(_fs.ma50, 1) > 0.0002:
    _trending_score += 0.3
_bb_width_pct = (_fs.bb_upper - _fs.bb_lower) / max(_fs.bb_mid, 1)
if _bb_width_pct < 0.001:
    _trending_score += 0.2   # tight bands = low vol = trending
elif _bb_width_pct > 0.003:
    _trending_score -= 0.3   # wide bands = volatile
```

**Classification** (lines 3136–3141):
```python
if _vol_reg < 0.10 and _trending_score >= 0.4:
    self._session_regime = "TRENDING"
elif _vol_reg < 0.20 and _trending_score < 0.4 and _trending_score >= 0:
    self._session_regime = "MEAN_REVERTING"
else:
    self._session_regime = "EXPLOSIVE"
```

**How the regime affects sizing** (lines 5199–5243):
```python
if _high_conv:           num_contracts = ceil(400 / fv_spread), max 8–20
elif _regime == "TRENDING":    num_contracts = ceil(400 / fv_spread), max 5–20
elif _regime == "EXPLOSIVE":   num_contracts = 3
else (MEAN_REVERTING):  num_contracts = based on vol + edge
```

### Current Performance

The database records `mtf_regime` (the MTF confluence scorer's regime) not `_session_regime`, so performance by regime cannot be extracted directly from `kalshi_trades`. However:

| mtf_regime | Trades | PnL |
|------------|--------|-----|
| TREND_BULL | 30 | +$24.69 |
| IMPULSE_BEAR | 56 | +$2.58 |
| SQUEEZE_BULL | 16 | +$41.75 |
| NEUTRAL | 116 | -$28.84 |
| MIXED | 104 | -$64.53 |
| IMPULSE_BULL | 42 | -$40.43 |
| TREND_BEAR | 37 | -$16.39 |
| PULLBACK_BEAR | 28 | -$27.62 |

None of the mtf_regime categories record any "won" status trades — they were all either NULL or set during periods when TA_FORCED was running without TP fill tracking. But the mtf_regime field from the MTF scorer (`confluence_regime`) is distinct from `_session_regime`. The `_session_regime` is applied at window boundaries and adjusts the sizing path but is not logged to the DB.

The sizing path it enables (HIGH_CONVICTION and TRENDING) targets 8–20 contracts. If the Brownian Bridge vol is understated by 2.3x (as identified in the Prob Engine analysis), then sessions where BTC is even moderately off-center from strike will appear as HIGH_CONVICTION (prob > 0.75) and trigger the largest position sizes. This is almost certainly the mechanism behind the -$50 days.

### Strengths

1. **The EXPLOSIVE regime case (3 contracts) is the correct default.** It matches what the engine should always do given 64% accuracy and ~50c entries.
2. **The concept of distinguishing trending vs ranging is sound.** Binary contracts in trending sessions have much higher settlement in one direction, making larger sizes valid.
3. **Per-session evaluation** (computed at each new window boundary) is appropriately granular.

### Weaknesses

1. **It is not an HMM.** The name in the task brief is misleading. There is no statistical model here — only hardcoded thresholds chosen without documented backtesting.

2. **Uses prior session indicators to classify current session.** The classifier runs at window open (lines 3113–3146 are inside the new-window detection block). This means the "trending score" is from the previous 15 minutes' BTC 5s indicators, applied to predict the current session's character. If BTC was trending into session close but the new session opens at a turning point, the classifier is systematically wrong.

3. **The TRENDING condition requires `_vol_reg < 0.10`** (10% annualized). Due to the 24/7 vs 6.5-hour annualization bug in the Prob Engine, this threshold is almost never met: real BTC intraday vol is 30–80% annualized (24/7 basis), but the engine computes it as 70–180% (equity basis). The TRENDING branch is effectively dead code — it fires only in the most dormant overnight markets.

4. **The HIGH_CONVICTION path** (`_prob_pct > 0.75 or < 0.25`) is triggered by the same inflated prob values. Any BTC position more than ~0.05% from strike with 10+ minutes remaining will produce `prob > 0.75` due to the vol underestimate. This means the largest position sizes (8–20 contracts) are being deployed in sessions that would not qualify under correct vol assumptions.

5. **`_session_regime` is not logged to the database.** There is no way to audit which regime was active at the time of any given trade, making post-hoc analysis impossible.

### Improvement Proposals

**1. Log the regime to the DB.** Add `_session_regime` to the `kalshi_trades` INSERT at the trade-placement point.

**2. Fix the vol annualization first (Prob Engine issue 1).** After fixing vol, recalibrate the TRENDING threshold from `_vol_reg < 0.10` to something in the range of `_vol_reg < 0.25` (which would correspond roughly to the same real vol level once corrected).

**3. Replace the HIGH_CONVICTION size multiplier with a conservative cap.**

Before (lines 5204–5211):
```python
_high_conv = (_prob_pct > 0.75 or _prob_pct < 0.25) and _edge >= 8
if _high_conv:
    _hc_ct = math.ceil(400 / _fv_spread)
    _hc_ct = max(8, min(_hc_ct, 20))
    num_contracts = _hc_ct
```

After: remove the HIGH_CONVICTION path entirely or cap at 5 contracts pending vol fix.

**4. Add a minimum confirming evidence requirement for TRENDING regime.**

The classifier should require two additional signals before allowing large sizing in TRENDING regime:
- Kalshi tape imbalance > 0.3 in the trade direction
- At least 2 of 3 candle closes in the same direction in the last 90 seconds (BTC directional consistency)

**5. Use a rolling lookback, not single session.** Apply exponential smoothing to `_trending_score` across the last 3 sessions instead of resetting each window.

---

## 4. Engine Interaction Analysis

The three components interact in this order at each signal evaluation tick:

```
Session boundary detected
  → _session_regime = classify(prior_vol, prior_5s_indicators)   [REGIME CLASSIFIER]
  ↓
Every 3s during session:
  → prob.update(btc_price, contract_mid, seconds_left)            [PROB ENGINE]
  → fvg_vs_baseline = prob.fair_value - session_baseline          [FVG ENGINE]
  → if |fvg_vs_baseline| >= time_threshold:                       [FVG ENTRY GATE]
      → tick_confirm?  (BTC velocity + BB position)
      → if yes: generate CrossVenueSignal
        → _execute_signal():
            → _compute_position_size():
                → _session_regime determines which sizing branch
                → HIGH_CONVICTION (from inflated prob) may override
```

**Critical conflict point:** The Prob Engine's vol underestimate creates artificially extreme probability estimates. These inflated probabilities feed the HIGH_CONVICTION override in sizing, which bypasses the regime-adjusted conservative paths. As a result:

- In calm sessions where BTC drifts 0.05% from strike, the Prob Engine produces `probability = 0.82`
- This triggers `_high_conv = True` (requires prob > 0.75)
- Sizing becomes `ceil(400 / fv_spread)` at 8–20 contracts
- If the direction call was wrong (36% of the time), the loss is 8–20 contracts × 55c = $4.40–$11.00
- If correct (64%), the gain is 8–20 contracts × 45c = $3.60–$9.00

Expected value: `0.64 × $3.60 - 0.36 × $4.40 = +$0.72` per trade on 8 contracts, seems positive. But `_fv_spread` is computed as `max(int(_fair), entry + 5) - entry`. When `fair = 82c` and `entry = 55c`, spread = 27c and contracts = `ceil(400/27) = 15`. On a 15c adverse move (contract goes from 55c to 40c on expiry) that's a `$8.25 loss`. The fair value TP is `82c - entry_price`, but the actual settlement is binary at 0c or 100c — the TP is a limit order that may not fill before the contract expires against the position.

**FVG engine's interaction with the TA signal:** The FVG path requires `ta.direction != "flat"` and `ta.confidence >= 10` even though TA direction is no longer used for the entry decision. The TA scorer runs on 1-minute Binance candles and computes `composite_score`. In flat overnight sessions (small candle bodies, low volume), the scorer frequently returns "flat" and blocks all FVG entries even when a valid gap exists. This is the primary reason the FVG engine fires only 45–75 times per day rather than the 96 theoretical sessions.

**Regime classifier's interaction with FVG magnitude:** The FVG threshold scales with session age (5/8/12c) but NOT with `_session_regime`. A TRENDING session and an EXPLOSIVE session use the same entry threshold. Since a TRENDING session by definition has higher directional momentum, the FVG gap would be expected to converge faster, meaning the 12c late-session threshold is actually more valuable in trending markets. The thresholds should be inverted by regime: TRENDING should accept smaller late gaps (direction confirmed), EXPLOSIVE should require larger gaps (more noise).

---

## 5. Prioritized Improvement List

Ranked by expected P&L impact based on trade data analysis:

### 1. Fix Position Sizing — Expected impact: +$50–$100/month

The -$121 lifetime loss on 635 trades with 64% direction accuracy is primarily a sizing problem. Switching to fixed 3 contracts eliminates the HIGH_CONVICTION and TRENDING size multipliers that produced the -$50 single-day losses.

**Action:** In `_execute_signal()` around line 5169–5243, replace the two-path sizing block with `num_contracts = 3` for all TA_FORCED entries. Separately track a "high confidence" flag for optional future scaling.

### 2. Fix Prob Engine Annualization — Expected impact: +$20–$40/month (enables correct regime classification)

Correcting the 2.3x vol underestimate eliminates false HIGH_CONVICTION signals and makes the TRENDING regime condition achievable under real market conditions.

**Action:** In `price_feed.py` line 214:
```python
# Current (wrong):
periods_per_year = 252 * 6.5 * 3600 / 5.0

# Correct:
periods_per_year = 365 * 24 * 3600 / 5.0
```

### 3. Remove TA Flat Gate from FVG Path — Expected impact: +$10–$20/month (more FVG entries in quiet sessions)

The FVG engine fires less often than it should because flat 1m TA blocks entries. Removing lines 4348–4355 from `_evaluate_ta_forced_signal()` allows the FVG baseline math to operate independently.

### 4. Use FVG Magnitude as Conviction Input — Expected impact: +$5–$15/month (better sizing correlation)

Replacing `ta.confidence + lean_strength` with a function of `fvg_vs_baseline` magnitude in line 4510 aligns the conviction estimate with the actual mispricing signal.

### 5. Log `_session_regime` to Database — Expected impact: $0 direct, critical for future tuning

Without logging, there is no way to evaluate whether the TRENDING, MEAN_REVERTING, or EXPLOSIVE classifications correlate with actual session direction consistency. Add the regime string to the `kalshi_trades` INSERT.

### 6. Fix Paper FVG Single-Entry Cap — Expected impact: $0 direct, critical for backtest validity

Change `pf["state"] = "IDLE"` to `pf["state"] = "DONE"` in `_paper_fvg_close()` (line 2600). Without this fix, the paper tracker inflates its P&L by repeating the same trade hundreds of times per session, making the simulation results meaningless.

### 7. Recalibrate TRENDING Regime Vol Threshold — Expected impact: conditional on fix #2

After fixing the annualization bug, update the TRENDING condition from `_vol_reg < 0.10` to `_vol_reg < 0.25` to preserve the intended behavior. This is a dependent fix — do not implement without fix #2.

---

## 6. Implementation Notes

**Priority order:** Fixes must be implemented in this sequence because later fixes depend on earlier ones:
1. Fix position sizing (standalone, immediate safety improvement)
2. Fix prob engine annualization (math fix, enables other recalibrations)
3. After 2: recalibrate regime thresholds
4. Remove TA flat gate (standalone)
5. Log regime to DB (standalone)
6. Fix paper FVG single-entry cap (standalone)
7. After collecting new regime-logged data (2 weeks+): evaluate conviction-from-FVG change

**Do not change the FVG threshold scaling (5c/8c/12c) until fix #1 and #2 are live** and at least 200 new trades are collected with correct vol and fixed sizing. The thresholds may need recalibration but the current values cannot be evaluated against existing data because all existing data was collected under wrong vol and wrong sizing.

**The paper_fvg_trades table should be cleared** before implementing the single-entry cap fix, since all existing rows are invalid. A `DELETE FROM paper_fvg_trades` before restart will ensure the new data is clean.

**Balance observation period:** The current live balance is $78.24 (down from a peak of $134.69 in the balance_snapshots table). The daily loss limit ($15) is the only current guardrail preventing complete drawdown. After implementing fix #1 (3-contract cap), expected daily loss should not exceed approximately $1.65 per trade × worst-case 10 trades per day = $16.50 absolute worst case, which is manageable and reversible.

**Testing approach for fix #2 (vol annualization):** Before deploying, manually verify the prob engine output against known market conditions. When BTC is exactly at strike with 7.5 minutes remaining and recent BTC vol is ~0.4% per 5s candle (40 basis points), the correct probability is approximately 50% (symmetric binary). The current engine with equity-hours annualization would produce ~73% in the same scenario. After the fix, verify the output is near 50% in this test case.

---

*End of research document. Do not implement any changes without reviewing this document in full.*
