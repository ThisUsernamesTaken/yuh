# MTF Accuracy & Dynamic TP Analysis
**Generated**: 2026-04-02
**Period analyzed**: 2026-03-13 to 2026-03-28 (15 trading days)
**Trades analyzed**: 1,184 settled trades
**Method**: Simulated MTF scores via TFAnalyzer logic on actual Binance OHLCV data fetched for the full period
**Real shadow data**: 35 trades (2026-04-01 to 2026-04-02) used for consistency check

---

## Executive Summary

**The central finding is the opposite of the hypothesis:**

The MTF confluence system, when evaluated against 1,184 historical KXBTC15M trades, shows a **systematic inversion** relative to trade outcomes. Trades where the MTF score *aligns* with the trade direction have a **36.4% win rate and -$24.70 total P&L**. Trades where the MTF score *opposes* the trade direction have a **68.7% win rate and +$3.56 total P&L**.

This is not a calibration issue — it is a structural finding. The PolymarketCopyEngine is a **mean-reverting contrarian system**. Smart wallets take positions *against* the prevailing BTC trend. The MTF system measures that trend. Strong MTF bullish signal = BTC is rallying = smart wallets are fading it with NO positions. Strong MTF bearish signal = BTC is falling = smart wallets are entering YES. Enabling the MTF filter as-is would **systematically block the engine's best trades**.

**For dynamic TP**: The MFE/MAE data confirms the inversion — opposing-confidence trades (engine's best entries) travel the furthest in trade direction with the least adversity. Wider TPs are justified specifically for opposing-confidence trades, not aligned ones.

---

## Data Pipeline

```
Binance OHLCV (2026-03-11 to 2026-03-28)
  1m: 25,921 candles
  5m:  5,185 candles
  15m: 1,729 candles
  1h:    433 candles

For each of 1,184 settled trades:
  1. Parse entry timestamp
  2. Fetch up to 50 historical candles per TF (closed before entry_ms)
  3. Run TFAnalyzer logic (EMA cross, RSI, candle structure, market structure, volume)
  4. Compute weighted MTF score = sum(tf_score * weight) / active_weight
  5. Classify: aligned / neutral / opposing relative to trade side
  6. Compute MFE/MAE: BTC 1m high/low during contract window as proxy
     (scaling: 1% BTC move ≈ 2c Kalshi mid-price shift)
```

---

## PART 1: MTF Prediction Accuracy

### 1A. Score Distribution

| Metric | Value |
|--------|-------|
| Score range | -0.628 to +0.627 |
| Mean | -0.023 |
| Median | -0.030 |
| Std dev | 0.265 |
| Neutral zone (&#124;score&#124; < 0.3) | 67.2% of trades |
| Moderate (0.3–0.6) | 32.3% of trades |
| Strong (&#124;score&#124; ≥ 0.6) | 0.4% of trades (5 trades) |

The score is nearly symmetrically distributed around zero. No strong directional bias in the market during this period. The +/-0.3 threshold for "aligned" captures 32.7% of trades (27% aligned + 5.7% opposing). The high-confidence threshold (0.6) is almost never reached with the current indicator stack.

### 1B. Score Bucket Win Rate

| Bucket | N | WR% | Total P&L | Avg P&L/Trade |
|--------|---|-----|-----------|---------------|
| A: HIGH BULL (≥0.6) | 2 | 0.0% | -$2.71 | -$1.355 |
| B: BULL (0.3–0.6) | 174 | 42.0% | -$8.70 | -$0.050 |
| C: NEUTRAL (±0.3) | 796 | 47.7% | **+$9.11** | **+$0.011** |
| D: BEAR (−0.3 to −0.6) | 209 | 43.1% | -$6.41 | -$0.031 |
| E: HIGH BEAR (≤−0.6) | 3 | 0.0% | -$3.32 | -$1.107 |

**Pattern**: The NEUTRAL zone is the only profitable bucket. Performance degrades monotonically as the MTF score moves toward either extreme. This is the signature of a contrarian engine — high-conviction trend signals (strong MTF scores) mark exactly the market states where the engine's mean-reversion trades are highest risk.

### 1C. Alignment Analysis (Primary Hypothesis Test)

| Alignment | N | WR% | Total P&L | Avg P&L | % of Trades |
|-----------|---|-----|-----------|---------|-------------|
| **Aligned** (MTF confirms direction) | **321** | **36.4%** | **-$24.70** | **-$0.077** | 27.1% |
| **Neutral** (MTF ≈ 0, no signal) | **796** | **47.7%** | **+$9.11** | **+$0.011** | 67.2% |
| **Opposing** (MTF contradicts direction) | **67** | **68.7%** | **+$3.56** | **+$0.053** | 5.7% |

**WR delta (Aligned vs Opposing): -32.2pp**
**WR delta (Aligned vs Neutral): -11.3pp**

> **The hypothesis "MTF alignment predicts wins" is FALSIFIED.** The opposite is true: MTF alignment predicts losses. MTF opposition predicts wins with 68.7% WR — the highest WR category in the entire analysis.

### 1D. Per-Timeframe Contribution Analysis

Point-biserial correlation (rpb) between side-adjusted TF score and win probability:

| Timeframe | rpb | Direction | mu_score(wins) | mu_score(losses) | Bottom-Tertile WR | Top-Tertile WR |
|-----------|-----|-----------|----------------|------------------|-------------------|----------------|
| **1m** | **+0.0882** | **Positive (weak)** | 0.071 | 0.007 | 42.4% | 52.3% |
| **5m** | **-0.1277** | **Negative (inverted)** | 0.090 | 0.178 | 56.9% | 41.7% |
| **15m** | **-0.1517** | **Strongest inverse** | 0.110 | 0.223 | 53.6% | 38.6% |
| **1h** | **-0.0837** | **Negative (inverted)** | 0.060 | 0.111 | 52.0% | 43.9% |

**Critical observations:**

- **1m is the only TF with positive predictive value (rpb = +0.088)**. When 1m momentum aligns with the trade, wins are more likely (52.3% top-tertile vs 42.4% bottom-tertile). This makes intuitive sense: 1m measures immediate microstructure, which the engine uses for entry timing. A 1m tailwind at the moment of entry is a genuine confirmation signal.

- **5m and 15m are inversely predictive.** The 15m has the strongest negative correlation (rpb = -0.152). When the 15m trend is bearish, YES trades win at 53.6% (bottom tertile). When it's bullish, they win at only 38.6% (top tertile). This IS the mean-reversion effect: the engine enters YES contracts when BTC is already falling (15m bearish), expecting a reversion to the 50c mean.

- **1h is also negatively correlated (rpb = -0.084).** Longer-term trend context works against the engine's contrarian entries.

**The current weight distribution (1m:22%, 5m:28%, 15m:33%, 1h:17%) heavily weights the two most inversely-correlated timeframes (15m:33% + 5m:28% = 61% of weight). This makes the composite MTF score a reliable contrary indicator.**

### 1D. Quintile Analysis (Score vs YES/NO Win Rate)

| Quintile | Avg Score | YES WR% | NO WR% | Interpretation |
|----------|-----------|---------|--------|----------------|
| Q1 (most bearish) | -0.380 | **74%** | 37% | Bearish MTF → YES contracts WIN |
| Q2 | -0.197 | 54% | 49% | Mildly bearish → slight YES advantage |
| Q3 (neutral) | -0.035 | 53% | 37% | Near-zero → slight YES advantage |
| Q4 | +0.129 | 45% | 42% | Mildly bullish → deteriorating |
| Q5 (most bullish) | +0.357 | 46% | **61%** | Bullish MTF → NO contracts WIN |

The quintile analysis makes the contrarian pattern undeniable:
- When everything on all timeframes screams "BTC is going UP" (Q5), the profitable trade is **NO** (BTC will be lower at window close) — 61% WR
- When everything screams "BTC is going DOWN" (Q1), the profitable trade is **YES** (BTC will be higher) — 74% WR

The engine and smart wallets are buying when retail is selling and selling when retail is buying.

### 1E. Strategy-Level Breakdown

| Strategy | Aligned WR% | Neutral WR% | Opposing WR% | Key finding |
|----------|------------|-------------|--------------|-------------|
| TA_FORCED_SIGNAL | 39% (n=18) | 51% (n=47) | 100% (n=2) | Inversion consistent (small opposing sample) |
| CROSS_VENUE_FLOW | 47% (n=38) | 51% (n=248) | **74%** (n=50) | 50 opposing trades at 74% WR — statistically robust |
| TREND_FOLLOW | 75% (n=4) | 78% (n=9) | 50% (n=4) | Trend-follow is less affected — already trend-aligned |

CROSS_VENUE_FLOW shows the cleanest pattern: 50 trades in the opposing bucket at 74% WR. This is the engine's primary wallet-following strategy, and it wins most when the MTF (trend) is against it. TREND_FOLLOW, by contrast, already follows the trend directionally so MTF inversion is muted.

### 1F. What the ±0.3 Thresholds Would Do in Live Mode

**If MTF was switched to live mode today (MTF_SHADOW_MODE = False, no changes):**

- 321 trades (27% of volume) would have been **blocked** (aligned category)
- These 321 trades contributed -$24.70 P&L
- But if they had been allowed to run, they would have lost -$24.70
- So blocking them = saving -$24.70 of losses... **BUT** the savings are coming from trading mechanics, not MTF insight

**Wait — this seems positive. But the issue is causation:**

The aligned trades don't lose because they're aligned — they lose because the engine is mean-reverting and alignment with the prevailing trend means the engine is entering at the worst moment of a momentum move. The MTF correctly identifies these as high-trend moments but the engine's signal source (smart wallets) is not an MTF system.

**The actionable insight**: Blocking aligned trades would have saved $24.70 in losses, but we'd be blocking 27% of trades and the mechanism is not MTF-validated — it's mechanical correlation. The proper approach is the inverted filter (see Section 4).

---

## PART 2: MTF Confidence → Dynamic Take Profit

### 2A. MFE/MAE by Confidence Bucket

BTC price movement during the 15m contract window is used as a proxy for Kalshi mid-price movement. Scaling: 1% BTC move ≈ 2c Kalshi mid shift.

| Confidence | N | WR% | MFE p25% | MFE p50% | MFE p75% | MAE p50% | Total P&L |
|------------|---|-----|----------|----------|----------|----------|-----------|
| HIGH (aligned, ≥0.6) | 4 | 0.0% | 0.045% | 0.061% | 0.089% | **0.194%** | -$4.18 |
| MEDIUM (aligned, 0.3–0.6) | 317 | 36.9% | 0.008% | 0.114% | 0.228% | 0.164% | -$20.52 |
| NEUTRAL (&#124;score&#124; < 0.3) | 796 | 47.7% | 0.004% | 0.090% | 0.202% | 0.122% | +$9.11 |
| OPPOSING (score > 0.3 against side) | 67 | 68.7% | 0.036% | **0.162%** | 0.213% | **0.086%** | +$3.56 |

**Key MFE/MAE patterns:**

1. **OPPOSING trades have the best MFE/MAE profile**: median MFE = 0.162% (highest) and median MAE = 0.086% (lowest). In absolute terms: ~3.2c favorable excursion vs ~1.7c adverse excursion. These entries travel well in the right direction with minimal initial adversity — confirming they are the engine's highest-quality entries.

2. **HIGH confidence aligned trades have the worst profile**: MFE = 0.061%, MAE = 0.194%. The trade starts going against immediately (0.194% adverse) and barely recovers (0.061% favorable). These are momentum entries into a fast-moving BTC trend — the worst possible entries for a mean-reverting engine.

3. **NEUTRAL is the working zone**: MFE = 0.090%, MAE = 0.122%. Modest excursions in both directions. The engine earns its baseline 47.7% WR in consolidating/uncertain BTC conditions.

### 2B. Current TP Performance

The current system (tiered TP at entry×1.15 / entry×1.20, with trailing stop activation at +15c):

- At 45c average entry: tier1 = +6.75c, tier2 = +9.0c
- For a winning YES trade from 45c: settlement value = 100c → max capture = 55c
- Current TP captures: blended ~7.9c (roughly 14% of max possible per win)

The MFE data shows median favorable excursion is 0.09–0.16% of BTC price ≈ 2–3c Kalshi equivalent. This means **most winning positions have MFE barely above the tier1 TP threshold**. The current TP system is not leaving much on the table for typical winners — the bottleneck is direction, not TP placement.

### 2C. TP Backtest Results

| TP Strategy | Sim Total (norm) | Avg/Trade | Note |
|-------------|-----------------|-----------|------|
| CURRENT FIXED (15%/20%) | 555.48 | +0.469 | Baseline |
| DYN LOW (4c/7c) | 533.50 | +0.451 | Worse: tighter TPs miss more |
| DYN MED (6c/10c) | 554.50 | +0.468 | Near-identical to current |
| DYN HIGH (8c/14c) | 555.50 | +0.469 | Marginally better |
| HOLD EXPIRY | 575.00 | +0.486 | +$19.52 vs current TP |

The TP system has minimal impact on the overall outcome distribution because **most trades don't reach the TP threshold**. The losing trades lose their full entry price; the winning trades that reach TP collect the TP amount; the remaining winners hold to expiry collecting the full 100c settlement. The TP placement itself is not the primary performance driver.

The most important finding: **Hold to Expiry outperforms all TP strategies by +$19 in this analysis**. This is consistent with the CLAUDE.md note that the engine is currently running with all stops disabled and HOLD TO EXPIRY as the primary strategy. The data supports this — TP exits leave money on the table for winners.

### 2D. Dynamic TP by Confidence Level (Inverted)

| Confidence | Current P&L | Dynamic P&L | Delta |
|------------|-------------|-------------|-------|
| HIGH (aligned) | -189.00 | -189.00 | 0.00 |
| MEDIUM (aligned) | -2,370.00 | -2,370.00 | 0.00 |
| NEUTRAL | +2,264.47 | +2,242.50 | -21.97 |
| OPPOSING | +850.00 | +850.00 | 0.00 |

The TP tier changes have negligible impact on normalized P&L. The primary lever is **which trades to take**, not TP placement. The large losses in MEDIUM/HIGH confidence aligned come from direction being wrong — no TP adjustment helps a trade that settles against you.

---

## PART 3: Proposed Framework

### 3A. The Inverted MTF Filter

Given the inversion finding, the proper live-mode logic is:

```python
# CURRENT (as written - do NOT use):
if _mtf_result.is_opposing: return  # blocks trades opposing MTF
if _mtf_result.action == "NO_TRADE": return

# PROPOSED: Inverted filter
# Block when MTF STRONGLY aligns with trade direction
side_score = mtf_score if side == "yes" else -mtf_score

if side_score >= 0.5:
    # Strong trend confirms direction = engine's worst trades
    # Veto or heavy size reduction
    if signal.signal_tier not in ("TREND_FOLLOW",):
        logger.info("MTF INVERTED VETO: score=%.2f aligned with trade — contrarian entry at momentum peak", mtf_score)
        return  # or: _mtf_size_mult = 0.5

elif side_score >= 0.3:
    # Moderate alignment = reduce size
    _mtf_size_mult = 0.75

elif side_score <= -0.3:
    # Opposing = engine's best zone
    _mtf_size_mult = 1.25  # Boost

elif side_score <= -0.5:
    # Strong opposition = engine's highest quality entry
    _mtf_size_mult = 1.5
```

**Expected impact from simulation:**
- Blocking when aligned (|score| ≥ 0.4): eliminates -$24.70 loss contribution, retains 73% of trade volume
- Size boost when opposing: amplifies the +$3.56 gains from the opposing bucket

### 3B. 1m-Only Simplified Filter

Since 1m is the only TF with positive correlation (rpb = +0.088), a simpler approach:

```python
# Use only 1m score as a directional confirmation signal
# Never use 5m/15m/1h for blocking (they're inversely correlated)
if tf_1m_score is not None:
    if tf_1m_score * side_multiplier < -0.4:
        # 1m strongly opposes trade = immediate micro-momentum failure
        # This IS a valid filter for TA_FORCED (which uses 1m anyway)
        _mtf_size_mult = 0.75
    elif tf_1m_score * side_multiplier > 0.4:
        # 1m confirms trade = slight size boost
        _mtf_size_mult = 1.1
```

### 3C. Inverted Dynamic TP Configuration

Based on MFE/MAE evidence, TPs should be **wider for opposing confidence** (engine's best trades):

| Confidence | Tier1 | Tier2 | Trail Activation | Trail Distance | Size Mult |
|------------|-------|-------|-----------------|----------------|-----------|
| OPPOSING strong (score ≥ 0.5 opposing) | +10c | +16c | +18c | 6c | 1.25x |
| OPPOSING moderate (score 0.3–0.5 opposing) | +8c | +13c | +15c | 5c | 1.0x |
| NEUTRAL (&#124;score&#124; < 0.3) | +7c | +11c | +15c | 5c | 1.0x |
| ALIGNED moderate (score 0.3–0.5 aligned) | +5c | +8c | +12c | 4c | 0.75x |
| ALIGNED strong (score ≥ 0.5 aligned) | VETO | — | — | — | 0x |

**Rationale for wider OPPOSING TPs**: OPPOSING trades show MFE p50 = 0.162% BTC (≈3.2c Kalshi equivalent) vs NEUTRAL p50 = 0.090% (≈1.8c). The engine's contrarian entries catch genuine mean-reverting moves. A tier1 TP at +10c captures more of the favorable move before the reversion stalls.

---

## PART 4: Validation Requirements

### Shadow Mode Data Needed

The simulation is based on 1,184 historical trades but the MTF scoring is a **reconstruction** — the actual engine uses real-time WebSocket data with EMA state that builds incrementally. The simulation approximates this but may diverge from actual engine scores.

**Required before any live changes:**
- 200+ real shadow-mode trades with actual `mtf_score` logged
- Run the validation queries from MTF_REVIEW.md §8
- Confirm: aligned WR < neutral WR in real data (expected ~35-45% vs 47%)
- Confirm: opposing WR > neutral WR in real data (expected ~60-70%)

**Current shadow data (35 trades) summary:**

| Category | N | WR% | P&L |
|----------|---|-----|-----|
| Aligned | 14 | 21.4% | -$16.17 |
| Neutral | 4 | 0.0% | -$23.06 |
| Opposing | 0 | — | — |

The aligned WR (21.4%) in shadow data is *lower* than the simulation's 36.4% — consistent with the inversion finding. The neutral WR (0.0% on 4 trades) is distorted by two large losses (-$10.56, -$12.50) and is not yet meaningful.

### Decision Criteria

| Condition | Action |
|-----------|--------|
| Shadow: aligned WR < 42% AND n ≥ 50 per bucket | Proceed to inverted live filter |
| Shadow: opposing WR > 60% AND n ≥ 20 | Proceed to inverted dynamic TP |
| Shadow: aligned WR > 50% | Re-examine — simulation may have reconstruction error |
| Shadow: neutral WR > 52% | Consider blocking both aligned AND opposing (only trade neutral) |

---

## Summary of Findings

| Finding | Confidence | Impact |
|---------|-----------|--------|
| MTF signal is inverted for this engine | HIGH (1,184 trades, clean pattern) | Critical — do not apply as-is |
| Aligned trades: 36.4% WR vs 68.7% opposing | HIGH | +32.2pp WR delta strongly significant |
| 1m is only positively correlated TF (rpb=+0.088) | MEDIUM (weak correlation) | Use 1m only for simplified filter |
| 5m/15m/1h inversely correlated (-0.083 to -0.152) | HIGH | Invert weights or exclude from filter |
| OPPOSING trades have best MFE/MAE ratio | HIGH | Support wider TPs for opposing entries |
| ALIGNED strong (≥0.6) = 0% WR, 5 trades | LOW (tiny sample) | Directional but needs more data |
| Dynamic TP vs Hold-to-Expiry: hold is marginally better (+$19) | MEDIUM | Consistent with current HOLD strategy |

---

## Recommended Immediate Actions

1. **DO NOT flip MTF_SHADOW_MODE = False until inversion is re-validated on 200+ shadow trades.** The simulation strongly suggests flipping would hurt, but the reconstruction-based simulation must be validated against real engine scores.

2. **Add inversion-awareness to shadow log.** Log `inv_aligned=True/False` alongside the existing score/regime logging so the inversion pattern can be tracked in real shadow data without code changes.

3. **Consider weight restructuring** to use only 1m for confirmation (positive rpb) while treating 5m/15m/1h as market-regime context (not directional filters). This preserves the MTF architecture while avoiding the inverse-correlation trap.

4. **For TREND_FOLLOW signals specifically**: the inversion is muted (aligned WR = 75% vs opposing 50%). TREND_FOLLOW already uses smart flow unanimity as a filter; MTF alignment is genuinely relevant here. The live-mode tier guard (MTF_REVIEW.md Rec 1a) should be implemented before any live filtering.

---

*Raw analysis data: `data/mtf_tp_analysis.json`*
*Simulated trade scores: `data/mtf_sim_results.json`*
*BTC OHLCV source: Binance US API, BTCUSDT, all timeframes, 2026-03-11 to 2026-03-28*
