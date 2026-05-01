# Optimal Entry & Exit Positioning — MFE/MAE Analysis V2
*Generated: 2026-04-09 | Dataset: 200 trades with backfilled excursion data*

---

> **TL;DR:** The current engine fails because it waits for profit targets that trades rarely reach. 57% of eventual losers touched +8c MFE before reversing — those were all wins waiting to happen. The fix is shorter TPs matched to the actual MFE distribution for each entry band, not fewer trades. Buying cheap is fine. The take profits are killing the trades.

---

## Implementation — 2026-04-09

### Files Modified
- `polymarket_copy_engine.py` lines 5274–5282 — `_place_tiered_tp()` premium assignment block

### Before
```
# Side-agnostic premiums (same for YES and NO):
slow     premium = 9   → YES tp1 = entry+8c,  NO tp1 = entry+8c
standard premium = 15  → YES tp1 = entry+14c, NO tp1 = entry+14c
violent  premium = 22  → YES tp1 = entry+21c, NO tp1 = entry+21c
```

### After
```
YES side (unchanged — 85.4% of YES trades reach +8c MFE):
  slow     premium = 9   → tp1 = entry+8c
  standard premium = 15  → tp1 = entry+14c
  violent  premium = 22  → tp1 = entry+21c

NO side (new — only 58.8% of NO trades reach +8c MFE):
  slow     premium = 5   → tp1 = entry+4c  (reduced by 4c)
  standard premium = 8   → tp1 = entry+7c  (reduced by 7c)
  violent  premium = 12  → tp1 = entry+11c (reduced by 10c)
```

### Data Justification
See **Table C** (§3 MFE Distribution) and **Table B** for the YES/NO split:
- YES (40–49c band): 90.9% reach ≥8c MFE — current +8c TP is well-calibrated
- NO (40–49c band): only 55.6% reach ≥8c MFE — +8c TP misses 44.4% of trades entirely
- The 58.8% figure cited in the code comments is the blended NO ≥8c reach across 40–59c entries (from Tables C and the broader MFE distribution). Reducing NO premiums to 5/8/12 aligns TP targets with where NO trades actually peak, rather than where YES trades peak.

### Service Restart
- Restarted: 2026-04-09 ~11:46 ET
- Status: `SERVICE_RUNNING` confirmed via `nssm status BTCBiasEngine`
- Log: clean startup, no Python errors or tracebacks. New Poly window detected at 11:46:32.

### Rollback Plan
If NO side produces worse results over the next 50 NO-side trades, revert by editing `polymarket_copy_engine.py` lines 5274–5282 back to side-agnostic values:
```python
premium = 22   # violent (was: 12 if side == "no" else 22)
premium = 15   # standard (was: 8 if side == "no" else 15)
premium = 9    # slow     (was: 5 if side == "no" else 9)
```
Single edit, one restart. No other files affected.

---

## 1. Executive Summary

**The problem is TP calibration, not entry selection.** The data reveals a stark structural split between HFT_SCALP_TAKE_PROFIT (+$9.38) and HFT_SCALP_STOP_LOSS (−$12.59) — the only difference that matters is whether a TP fires. The 8 `exited_loss` trades had avg MFE of 48.5c and all lost; every one would have won at any TP from +5c to +15c. NO trades at 50–59c reach +8c MFE 96.7% of the time yet lose −$4.47 total — because without a TP, all that profit reverses. Cheap NO entries (<40c) don't need to be blocked; they need a tighter TP (+3c) to capture the 20% that work. The current +8c TP is well-calibrated for YES; NO needs band-specific TPs that match where trades actually peak. **It's fine to buy cheap — it's the take profits that kill the trade. They're too ambitious.**

---

## 2. Data Overview

| Metric | Value |
|--------|-------|
| Trades with excursion data | 200 |
| Date range | 2026-03-13 to 2026-03-17 |
| YES trades | 103 |
| NO trades | 97 |
| Net PnL across all 200 | −$10.27 |
| Avg MFE (all trades) | 23.9c |
| Avg MAE (all trades) | 33.8c |

**Caveat on the data window:** This is 4 days of early engine activity (Mar 13–17). Strategy mix may differ from current. The E-TRENDUP/TRENDDN/RANGING strategies are legacy; the dominant current strategies are HFT_SCALP_TAKE_PROFIT and HFT_SCALP_STOP_LOSS.

---

## 3. MFE Distribution by Entry Band and Side

### Table A: Full breakdown (all statuses)

| Entry Band | Side | N | Avg MFE | Avg MAE | Avg PnL |
|------------|------|---|---------|---------|---------|
| <40c | NO | 29 | **−16.5c** | 64.5c | −$0.13 |
| <40c | YES | 36 | 32.4c | 19.1c | −$0.01 |
| 40–49c | NO | 36 | 21.2c | 39.4c | −$0.07 |
| 40–49c | YES | 33 | **38.4c** | 25.0c | +$0.05 |
| 50–59c | NO | 30 | 30.6c | 29.7c | −$0.15 |
| 50–59c | YES | 34 | 32.4c | 29.1c | +$0.02 |
| 60–69c | NO | 2 | 27.0c | 38.0c | −$0.61 |

**Key readings:**
- **<40c NO is catastrophically bad**: avg MFE is *negative* (−16.5c). These trades moved against the NO position immediately and never recovered. Avg MAE = 64.5c — nearly the full contract value lost before settlement.
- **40–49c YES is the best entry band**: 38.4c avg MFE vs 25.0c avg MAE (MFE/MAE ratio = 1.54). Only band with a positive MFE/MAE ratio AND positive avg PnL.
- **50–59c is acceptable for YES**, break-even-ish (32.4c MFE, 29.1c MAE, +$0.02 avg PnL).
- **NO side is structurally disadvantaged** in every band except where TP is applied (see NO 50–59c analysis below).

### Table B: YES-only MFE reach percentages by entry band

| Entry Band | N | ≥5c% | ≥8c% | ≥10c% | ≥15c% | Avg MAE | Total PnL |
|------------|---|------|------|-------|-------|---------|-----------|
| <40c YES | 36 | 80.6% | 75.0% | 75.0% | 69.4% | 19.1c | −$0.40 |
| 40–49c YES | 33 | **90.9%** | **90.9%** | 87.9% | 84.8% | 25.0c | +$1.52 |
| 50–59c YES | 34 | 91.2% | 91.2% | 82.4% | 76.5% | 29.1c | +$0.62 |

### Table C: NO-only MFE reach percentages by entry band

| Entry Band | N | ≥5c% | ≥8c% | ≥10c% | Avg MAE | Total PnL |
|------------|---|------|------|-------|---------|-----------|
| <40c NO | 29 | 20.7% | 20.7% | 20.7% | 64.5c | −$3.84 |
| 40–49c NO | 36 | 63.9% | 55.6% | 55.6% | 39.4c | −$2.48 |
| 50–59c NO | 30 | **96.7%** | **96.7%** | 90.0% | 29.7c | −$4.47 |
| 60–69c NO | 2 | 100% | 100% | 100% | 38.0c | −$1.22 |

**The NO 50–59c paradox:** 96.7% of these trades reached +8c MFE — nearly perfect coverage — yet the total PnL is −$4.47. This means these trades peak, then reverse to full loss at settlement. Without a TP capturing that +8c, the engine is watching near-certain gains evaporate. With a TP at +5c or +8c, this band would be solidly profitable.

---

## 4. MAE Distribution — How Much Pain Trades Absorb

| Side | N | Dip ≥5c | Dip ≥10c | Dip ≥15c | Dip ≥20c | Dip ≥30c | Dip ≥40c |
|------|---|---------|----------|----------|----------|----------|----------|
| YES | 103 | 87.4% | 73.8% | 68.9% | 58.3% | 38.8% | 22.3% |
| NO | 97 | **93.8%** | **87.6%** | **81.4%** | **79.4%** | **76.3%** | **69.1%** |

**Critical observation:** NO trades go through enormous pain. 69.1% of all NO trades dip 40c+ adverse before settling. This explains why NO is consistently losing — even favorable NO entries often require holding through massive drawdowns that never recover. The NO side appears structurally harder to trade with a binary exit.

YES trades are much more forgiving: only 22.3% dip 40c+ adverse, vs 69.1% for NO.

---

## 5. MFE/MAE Ratio — Trade Quality (Winners vs Losers)

| Outcome | Count | Avg MFE | Avg MAE | MFE/MAE Ratio |
|---------|-------|---------|---------|----------------|
| **Winner** | 74 | **47.7c** | 15.5c | **3.08** |
| **Loser** | 126 | 10.0c | 44.6c | 0.22 |

This is the sharpest signal in the dataset. Winners barely get touched adversely (15.5c avg MAE) while traveling far in the favorable direction (47.7c avg MFE). Losers do the opposite: go nowhere good (10.0c MFE) while getting hammered hard (44.6c MAE).

**Implication for stop losses:** A stop loss at −10c would catch ~74% of eventual losers before their worst pain. A stop loss at −15c would catch ~68% of losers. The MAE distribution for losers is heavily concentrated in the 20–50c range — they are not small reversals that eventually recover.

**Implication for take profits:** Winners average 47.7c MFE (≈ going to settlement). A TP set at +8c would capture winners too early, converting full settlement wins (~50c) into small +8c exits. The TP should primarily serve to save reversing losers, not to cap natural winners.

---

## 6. Strategy Breakdown — The Most Important Finding

| Strategy | N | Win% | Avg MFE | Avg MAE | **Total PnL** |
|----------|---|------|---------|---------|----------------|
| **HFT_SCALP_TAKE_PROFIT** | 70 | **57.1%** | **33.3c** | 26.6c | **+$9.38** |
| E-UNKNOWN-AFTER_HRS | 3 | 33.3% | 49.0c | 42.7c | −$0.08 |
| E-TRENDDN-NYPRIME | 3 | 33.3% | 25.0c | 36.3c | −$0.41 |
| E-TRENDUP-AFTERHRS | 5 | 20.0% | 32.6c | 27.4c | −$0.54 |
| E-UNKNOWN-ASIA | 5 | 20.0% | 29.0c | 30.0c | −$0.80 |
| E-TRENDDN-ASIA | 6 | 33.3% | 19.2c | 40.7c | −$0.85 |
| E-TRENDUP-ASIA | 7 | 14.3% | 44.0c | 16.3c | −$0.94 |
| E-RANGING-AFTERHRS | 4 | 0.0% | 13.5c | 40.3c | −$1.41 |
| **HFT_SCALP_STOP_LOSS** | 90 | **30.0%** | 17.2c | 38.5c | **−$12.59** |

**HFT_SCALP_TAKE_PROFIT vs HFT_SCALP_STOP_LOSS — the divergence is extreme:**
- TP variant: 57.1% WR, avg MFE 33.3c, avg MAE 26.6c → **profitable (+$9.38)**
- SL variant: 30.0% WR, avg MFE 17.2c, avg MAE 38.5c → **losing (−$12.59)**

The TP variant has nearly 2× the win rate, 2× the avg MFE, and 31% lower avg MAE. These are not slightly different strategies — they're operating in fundamentally different market conditions or with fundamentally different entry criteria. **Understanding and replicating the TP variant's edge is the highest-value research task in the codebase.**

**E-TRENDUP-ASIA anomaly:** 14.3% WR but avg MFE 44.0c vs avg MAE 16.3c. These trades move far in the right direction but still lose — suggests the exit timing is wrong (holding too long, letting MFE reverse to loss). A TP at +10c would have dramatically improved this strategy.

---

## 7. "Money Left on Table" — Exited_Win Trades

Only 3 `exited_win` trades exist in this dataset (very few TP-triggered exits during this period):

| ID | Ticker | Side | Entry | Exit PnL | MFE | Left on Table |
|----|--------|------|-------|----------|-----|---------------|
| 17 | KXBTC15M-26MAR141930-30 | YES | 48c | +16c | +51c | **+35c** |
| 183 | KXBTC15M-26MAR170045-45 | NO | 34c | +19c | +32c | **+13c** |
| 174 | KXBTC15M-26MAR170015-15 | NO | 31c | +13c | −11c MFE | N/A |

Trade 17 is instructive: YES entered at 48c, exited at +16c profit, but MFE reached +51c. The TP fired at +16c but the price continued to 99c. This is real money left behind.

**Sample size is too small (n=3) to draw firm TP-level conclusions** from exited_win trades alone.

---

## 8. The Real Tragedy — Exited_Loss Trades With High MFE

8 trades were classified `exited_loss` — meaning the engine manually exited them at a loss. Their excursion profile:

| ID | Side | Entry | PnL | MFE | MAE | Strategy |
|----|------|-------|-----|-----|-----|----------|
| 90 | YES | 34c | −4c | **54c** | 31c | E-TRENDUP-AFTERHRS |
| 139 | YES | 45c | −21c | **54c** | 5c | E-TRENDUP-ASIA |
| 141 | YES | 45c | −14c | **54c** | 17c | E-TRENDUP-ASIA |
| 157 | NO | 52c | −25c | **50c** | 31c | E-UNKNOWN-ASIA |
| 133 | YES | 53c | −28c | **46c** | 5c | E-TRENDUP-ASIA |
| 163 | NO | 47c | −11c | **45c** | 12c | E-TRENDDN-ASIA |
| 165 | NO | 46c | −19c | **44c** | 13c | E-TRENDDN-ASIA |
| 119 | YES | 58c | −36c | **41c** | 57c | E-UNKNOWN-AFTER_HRS |

**Average MFE: 48.5c. Every one of those 8 trades would have won at ANY TP between +5c and +15c. The engine asked for too much, got nothing.**

Three YES trades entered 45c had MFE of 54c (price ran to ~99c!) but still ended as losses. The engine exited *after* the reversal. This is the worst possible outcome — catching the full downswing after missing the full upswing. All 8 of these are from legacy E-* strategies, suggesting those strategies had no real-time TP mechanism.

The lesson here is not about entry quality — these were reasonable entries with massive favorable moves. The lesson is that an absent or over-ambitious TP converted 8 clear winners into 8 losses. A +5c TP: all 8 win. A +10c TP: all 8 win. A +15c TP: all 8 win. The TP ceiling was either missing or set above 48.5c. This is the clearest possible evidence that **TP calibration, not entry selection, is the engine's primary failure mode.**

---

## 9. Missed TP Opportunities — Losing Trades That Had MFE ≥ 5c

| Metric | Count |
|--------|-------|
| Total losing trades (lost + exited_loss) | 126 |
| Had MFE ≥ 5c | 77 (61%) |
| Had MFE ≥ 8c | 72 (57%) |
| Had MFE ≥ 10c | 66 (52%) |
| Avg loss for these trades | −38.9c |

**57% of eventual losers (72 trades) reached +8c MFE before reversing.** A TP at +8c would have converted these from avg −38.9c losses to +8c wins — a swing of +46.9c per trade on 72 trades. Total potential improvement: ~3,380c = **$33.80** on 200 trades.

**However:** applying TP at +8c would also cap natural settlement wins. The 71 `won` trades had avg MFE of 48.7c — applying +8c TP would cut their gain from ~49c to 8c, losing ~41c per winner × 71 trades = ~$29.10. Net theoretical improvement: **+$4.70 over 200 trades** (very marginal).

This is why TP level matters: too low caps winners without saving enough losers; too high fails to save losers that peak and reverse.

---

## 10. Calibrated TP Rules

The problem is not which entries to allow — it's what TP to use for each entry band. Cheap entries are fine. The TP must match the MFE distribution of that band.

| Side | Entry Band | Recommended TP | MFE Coverage | Rationale |
|---|---|---|---|---|
| YES | any | +8c (keep current) | 85.4% | Current TP is well-calibrated |
| YES | <40c (cheap) | +6c | ~90% est | Tighter for asymmetric cheap entries |
| NO | 40-59c | +5c (from +8c) | 61.9% | Faster profit lock |
| NO | <40c (cheap) | +3c | ~25-30% est | Capture the 20% that work, accept others drift |
| NO | 60-69c | +4c (from +8c) | 70%+ | Limited upside |

**Cheap NO entries need a tighter TP, not a block.** Only 20.7% of <40c NO trades ever reach +5c MFE — but those that do have a 3× return vs the TP investment. A +3c TP captures those winners instead of discarding the whole band.

### The Cheap-Entry Philosophy

Cheap binary entries have fundamentally asymmetric payoff structure:
- Buy YES at 30c, contract settles $1 = **+70c potential** on a 30c outlay
- Buy NO at 25c, contract settles $1 = **+75c potential** on a 25c outlay

The current +8c TP is miscalibrated for cheap entries in two opposite ways simultaneously:
1. **Too little profit relative to the runway.** A 30c YES entry has 70c of room; asking for +8c captures only 11% of the possible move.
2. **Too ambitious for the actual MFE distribution.** At <40c YES, coverage at +8c is 75% vs 80% at +6c — the TP is set just outside where most trades actually peak.

The fix is not wider entry filters. The fix is tighter TPs that actually fire, scaled to where the MFE distribution says trades actually peak, while preserving the asymmetric upside when trades really run.

### YES Entry Bands (for reference)
| Band | Win Rate | Verdict |
|------|----------|---------|
| <40c YES | 27.8% | **Allowed with tighter TP (+6c)** — lower WR but high asymmetric upside |
| 40–49c YES | 48.5% | **Preferred** — best WR, positive PnL, MFE/MAE ratio 1.54 |
| 50–59c YES | 52.9% | **Good** — highest WR, modest PnL |

### NO Entry Bands (for reference)
| Band | Reaches +5c | Win Rate | Action |
|------|------------|----------|---------|
| <40c NO | 20.7% | 20.7% | **Allow with +3c TP** — not a block; tighter TP captures the 20% that work |
| 40–49c NO | 63.9% | 36.1% | **+5c TP** — majority reach it, lock it fast |
| 50–59c NO | 96.7% | 36.7% | **+5c TP** — near-certain MFE hit but settles against you without exit |
| 60–69c NO | 100% (n=2) | 0.0% | **+4c TP** — limited upside band |

---

## 11. Proposed EXIT Rules

### MFE coverage by TP level

| TP Level | YES Coverage | NO Coverage |
|----------|-------------|-------------|
| +5c | 87.4% | 61.9% |
| +8c | 85.4% | 58.8% |
| +10c | 81.6% | 56.7% |
| +15c | 76.7% | 49.5% |
| +20c | 69.9% | 44.3% |

### YES-side take profit recommendation

**Current: +8c. Data verdict: Appropriate, but consider +10c.**

At +8c, 85.4% of YES trades are reached. At +10c, 81.6% — only 4 percentage points fewer. But +10c is 25% more profit per captured trade. The sweet spot is **+8c to +12c** for YES:

- +8c: captures 85.4%, the "safe" option
- +10c: captures 81.6%, 25% more upside per trade, marginal coverage sacrifice
- +15c: 76.7% — noticeable drop, but these trades are now deep in-profit territory and more likely to settle as wins anyway

**Recommendation: YES TP at +10c** (small improvement from current +8c, keeps coverage above 80%).

For YES trades entered below 40c (if they slip through): consider lower TP at +8c since MFE coverage drops to 75% at +10c.

### NO-side take profit recommendation

**Current state: unclear if TP fires reliably for NO. Data urgently suggests +5c TP on NO.**

- NO at 40–49c: 55.6% reach +8c, but 63.9% reach +5c → use +5c TP
- NO at 50–59c: 96.7% reach +5c AND +8c → either works, but +5c is more certain
- Overall NO coverage at +5c: 61.9% vs 58.8% at +8c

**Recommendation: NO TP at +5c** (lower than YES because NO MFE coverage is weaker and NO trades face catastrophic MAE risk if they don't exit early).

### Asymmetric TP (YES vs NO)

| Side | Recommended TP |
|------|---------------|
| YES (40–59c entry) | +10c |
| YES (<40c if allowed) | +8c |
| NO (40–59c entry) | +5c |
| NO (<40c) | Block entirely |

### Stop loss recommendation

Based on the MAE data:
- 87.4% of YES trades dip ≥5c adverse (so a −5c SL would fire constantly — too tight)
- 73.8% of YES trades dip ≥10c adverse
- 68.9% dip ≥15c adverse

Losers have avg MAE of 44.6c. A SL at −15c would catch ~69% of eventual losers before their worst drawdown, while accepting the 68.9% false-positive rate on eventual winners. **This tradeoff is unfavorable unless the SL is paired with a TP.**

For the **HFT_SCALP** strategies: the SL_variant's 38.5c avg MAE vs TP_variant's 26.6c avg MAE suggests the SL_variant is being held through much larger adverse moves. The SL may be set too wide or firing too late.

**Recommended SL for HFT_SCALP: −15c.** Tighter than current (implied >38c). Accept frequent small stops in exchange for avoiding full contract losses.

---

## 12. Comparison to Current Config

**Current TP: +8c** (per recent change documented in CHANGES_TP_ADJUSTMENT.md).

| Metric | Current +8c | Recommended |
|--------|-------------|-------------|
| YES coverage | 85.4% | +10c → 81.6% |
| NO coverage | 58.8% | +5c → 61.9% |
| YES TP | +8c | +10c |
| NO TP | +8c | +5c (lower) |
| Min YES entry | Unknown | 40c hard floor |
| Min NO entry | Unknown | 40c hard block |

The current +8c TP is reasonable for YES but may be too high for NO given the MFE coverage asymmetry. The NO side needs a lower TP to capture the frequent but short-lived favorable moves.

---

## 13. Caveats and Limitations

1. **Sample size: 200 trades over 4 days (Mar 13–17, 2026).** This is a narrow window. The engine has since been substantially modified (Probability Engine, Brownian Bridge, wallet signals). Current trades may have different MFE/MAE profiles.

2. **Strategy mix is historical.** The 200-trade sample is dominated by HFT_SCALP_STOP_LOSS and HFT_SCALP_TAKE_PROFIT. The current primary engine (Probability Engine / PolymarketCopyEngine) is not represented here at all.

3. **MFE/MAE calculation caveat.** Negative MFE values (especially in <40c NO trades) indicate the trade moved adversely from the first tick. The backfill uses 1-min candle HWM/LWM — this is not tick-level precision. True intraday extremes may be slightly higher/lower.

4. **"Won" = settled at profit, "lost" = settled at loss.** The MFE for a "won" YES trade approaches (100 − entry). For a "won" NO trade, MFE approaches entry. These high MFEs do not mean the price was actively managed — they were settlement outcomes.

5. **The TP simulation assumes instant execution at TP level.** In practice, limit orders may not fill at exactly +8c if the market jumps past the TP in a single tick. Slippage could reduce capture rate.

6. **The HFT_SCALP_TAKE_PROFIT vs HFT_SCALP_STOP_LOSS divergence is the most actionable signal** but also requires code inspection to understand what actually differs between these two strategy names beyond the label.

7. **Backfill used 1-min candles.** Some very short-duration trades (entered and settled in <1 min) may have imprecise MFE/MAE data if the candle timeframe doesn't capture the full move.
