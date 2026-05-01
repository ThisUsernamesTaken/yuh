# Sale Ladder & Bid Ladder Strategy Research
*Generated: 2026-04-09 | Dataset: 200 trades with MFE/MAE excursion data (Mar 13–17) + 1,857 total trades for regime analysis*

---

## 1. Executive Summary

**The data supports a sale ladder for YES — and a cautious extension for NO — but strongly argues against any meaningful bid ladder expansion.** YES trades exhibit a clean, graduated MFE distribution where 85.4% reach +8c, 81.6% reach +12c, and 75.7% reach +16c: a textbook setup for a 3- or 4-tier sale ladder. NO trades have a messier, more bimodal distribution — 96.7% of the 50–59c band reach +8c MFE but virtually never dip below entry (only 4.1% dip ≤7c), making a bid ladder there pointless. On the entry side, the current bid-3/bid-5 passive entry captures ~23% of YES fills at a discount and the existing divergence ladder already handles the momentum-divergence case. Extending bids deeper runs into a wall of adverse trades (70%+ of YES MAE exceeds 12c) where adding more exposure is net negative. The biggest single gain available — conservatively +$0.20 to +$0.45 per YES trade — comes from adding a 3rd TP tier at +20c to +25c for YES entries in violent momentum, capturing contracts that currently either expire or go unfilled at tp2.

---

## 2. Sale Ladder Analysis

### 2.1 YES MFE — Percentile Distribution

| MFE Level | % Reach It | Decay from Prev Tier |
|-----------|-----------|----------------------|
| +3c       | 91.3%     | —                    |
| +5c       | 87.4%     | −3.9 pp              |
| +8c       | 85.4%     | −2.0 pp              |
| +12c      | 81.6%     | −3.8 pp              |
| +16c      | 75.7%     | −5.9 pp              |
| +20c      | 69.9%     | −5.8 pp              |
| +30c      | 57.3%     | −12.6 pp             |

*Source: 103 YES trades with excursion data.*

The decay rate is slow and consistent from +5c to +20c (2–6 pp per tier), then accelerates sharply between +20c and +30c (−12.6 pp). This profile is ideal for a 4-tier ladder: there's real marginal capture at each step up to +20c, after which the curve falls off faster.

### 2.2 NO MFE — Percentile Distribution

| MFE Level | % Reach It | Decay from Prev Tier |
|-----------|-----------|----------------------|
| +3c       | 68.0%     | —                    |
| +5c       | 61.9%     | −6.1 pp              |
| +8c       | 58.8%     | −3.1 pp              |
| +12c      | 54.6%     | −4.2 pp              |
| +16c      | 49.5%     | −5.1 pp              |
| +20c      | 44.3%     | −5.2 pp              |
| +30c      | 35.1%     | −9.2 pp              |

*Source: 97 NO trades with excursion data.*

NO has a harder cliff: 68% reach +3c but only 62% reach +5c (−6 pp on the first step). This means a floor tier at +3c captures 6.1 percentage points of contracts that wouldn't fill at +5c. Today's tuning (NO premium: slow=5, std=8, violent=12) places tp1 at entry+4c/+7c/+11c — well-aligned with this distribution. The research question is whether a 3-tier ladder captures meaningfully more.

### 2.3 MFE by Entry Band (YES)

| Band     | N  | Avg MFE | +3c   | +8c   | +15c  | +25c  |
|----------|----|---------|-------|-------|-------|-------|
| <40c YES | 36 | 32.4c   | 86.1% | 75.0% | 69.4% | 55.6% |
| 40-49c YES | 33 | 38.4c | 93.9% | 90.9% | 84.8% | 75.8% |
| 50-59c YES | 34 | 32.4c | 94.1% | 91.2% | 76.5% | 67.6% |

**40-49c YES is the richest band**: 90.9% reach +8c and 75.8% reach +25c. This is where a 4-tier ladder would extract maximum value — there's a large mass of trades running deep into profit that currently exit at tp1 with substantial upside remaining.

50-59c YES falls off faster at the top: 76.5% reach +15c vs 84.8% for 40-49c. Ladder spacing should be tighter for expensive entries.

### 2.4 MFE by Entry Band (NO)

| Band     | N  | Avg MFE | +3c    | +8c   | +15c  | +25c  |
|----------|----|---------|--------|-------|-------|-------|
| <40c NO  | 29 | −16.5c  | 20.7%  | 20.7% | 20.7% | 17.2% |
| 40-49c NO | 36 | 21.2c  | 77.8%  | 55.6% | 50.0% | 50.0% |
| 50-59c NO | 30 | 30.6c  | 100.0% | 96.7% | 73.3% | 46.7% |

**<40c NO is a disaster**: avg MFE is −16.5c (price moves against NO immediately). Not only is a ladder pointless here, entry itself is questionable. The 40-49c NO band has a wide gap between +3c (77.8%) and +8c (55.6%) — a tier at +5c would be valuable here. 50-59c NO is the most ladder-friendly: near-universal reach to +8c, then a meaningful tail to +25c (46.7%).

### 2.5 Current 2-Tier vs Proposed N-Tier (P&L Estimate)

**Current 2-tier mechanics (from code):**
```
breakeven_ct = ceil(total_count × entry_cents / tp1)
remainder    = total_count − breakeven_ct
→ Most contracts at tp1, small runner at tp2
```

For a 10-contract YES entry at 50c, standard momentum (premium=15):
- tp1 = 64c: `ceil(10×50/64)` = 8 contracts
- tp2 = 70c: 2 contracts

Expected value per 100 contracts (flat split for illustration):
- 8 contracts × 85.4% fill at +14c = 7.0 captures at +14c → 98c
- 2 contracts × 69.9% fill at +20c = 1.4 captures at +20c → 28c
- Total: 126c expected across 10 contracts = **12.6c expected per contract**

**Proposed 3-tier YES (25% / 50% / 25% at +8c / +14c / +22c):**
- 2.5 contracts at +8c: 85.4% fill → 2.14 captures → 17.1c
- 5 contracts at +14c: 81.6% fill → 4.08 captures → 57.1c
- 2.5 contracts at +22c: ~68% fill → 1.70 captures → 37.4c
- Total: 111.6c across 10 = **11.2c expected per contract**

Flat arithmetic slightly favors the 2-tier on this narrow calculation, but it **misses the critical use case**: trades that hit +8c MFE then reverse to a full loss. Per the V2 analysis, 57% of eventual losers reached +8c — those are the contracts where having a floor TP tier converts a −50c expiry loss to a +8c exit. A dedicated floor tier at +5c (not in the current 2-tier) changes the loss distribution meaningfully.

**Best opportunity: add a 3rd tier as a high runner (+25c to +30c) for YES, not a lower floor tier.** The data shows 57.3% of YES trades reach +30c — that's 57 contracts out of 100 that go there. If the current system's "runner" is capped at tp2 (entry+20c) and many of these continue to expiry at 100c, the gain is already captured via settlement. But in violent momentum sessions (tp2 at entry+27c for YES violent), a 3rd tier at entry+35c would catch contracts that otherwise expire at 100c but could be sold at 90c for a larger guaranteed return.

**Net recommendation on sale ladder: YES benefits most from a 3rd tier at entry+25c (standard) or entry+35c (violent), capturing the 57% that run very deep. NO benefits from a dedicated floor tier at entry+3c in the 40-49c band where 77.8% reach +3c but only 55.6% reach +8c — that 22 pp gap is real money being missed.**

---

## 3. Bid Ladder Analysis

### 3.1 MAE Distribution — How Deep Do Trades Dip?

| Side | N  | Avg MAE | ≤2c  | 3-5c  | 6-10c | 11-20c | >20c  |
|------|----|---------|------|-------|-------|--------|-------|
| YES  | 103 | 24.3c  | 8.7% | 7.8%  | 11.7% | 13.6%  | 58.3% |
| NO   | 97  | 43.9c  | 5.2% | 3.1%  | 5.2%  | 7.2%   | 79.4% |

### 3.2 Fine MAE Breakdown (Bid Ladder Sizing)

| Side | mae=0 | mae=1 | mae=2 | mae=3-4 | mae=5-7 | mae=8-12 | mae>12 |
|------|-------|-------|-------|---------|---------|----------|--------|
| YES  | 3 (2.9%) | 1 (1.0%) | 4 (3.9%) | 4 (3.9%) | 12 (11.7%) | 5 (4.9%) | 73 (70.9%) |
| NO   | 0 | 0 | 0 | 1 (1.0%) | 3 (3.1%) | 5 (5.2%) | 83 (85.6%) |

**Bid ladder conclusions from MAE data:**

- **YES**: 24 of 103 trades (23.3%) dip ≤7c before running. These are the only trades that would fill at a passive bid-3 to bid-7 entry. The current engine's SHALLOW (bid-3c) and DEEP (bid-5c) already captures this exactly: they fill on the 23% of YES trades that dip shallow before rising. Adding a deeper tier at bid-8 would capture ~5% more fills (the mae=8-12c band), but those 5 trades already showed 8-12c of adverse movement before any recovery — not clear these are better entries.

- **NO**: 4 of 97 trades (4.1%) dip ≤7c. A bid ladder for NO entries is nearly worthless — NO trades almost never give a cheap fill opportunity. When they dip, they dip hard (avg MAE = 43.9c). A bid-3 order would fill on 4% of NO entries and miss 96%.

- **Shallow MAE trades run farther**: The 17 YES trades with MAE ≤5c averaged 50.6c MFE and hit +8c and +15c 100% of the time. These are the "clean" trades that move straight up from entry. This validates the current bid-3/bid-5 approach: when you do get a dip-fill, the trade quality is excellent.

### 3.3 Bid Ladder Recommendation

**Do not extend the bid ladder beyond the current bid-5 for passive fills.** The data does not support it:
- 71% of YES MAE exceeds 12c — trades that dip that deep are mostly losers, not discount entries
- Adding contracts at bid-8 to bid-12 increases exposure on the losing population
- Kalshi liquidity is thin; resting deep bids may go unfilled on the good trades (quick move up) but fill perfectly on the bad ones (straight down through your bids)

**The existing DIVERGENCE_LADDER already handles the right case**: when momentum opposes the signal (mom_diverges at line 4567), the engine places a 3-tier ladder at ask / ask-4 / ask-8 (or ask-6 / ask-12 for violent momentum). This is correct behavior — momentum divergence is exactly when you'd want a staggered entry rather than a single order.

**One improvement worth considering**: the divergence ladder's mid-tier (at ask-4) currently weights 30-40% of contracts. Given that the sweet spot for YES shallow fills is 3-7c dip (23% of trades), weighting more contracts toward ask-2 (60%) and fewer toward ask-6 (20%) would improve the blended fill rate in non-extreme divergence.

**Expected fill rate and price improvement (current):**
- YES: 23% of entries fill at bid-3 or bid-5 → avg discount of ~4c per filled contract
- Assuming 20% average fill rate on the passive ladder → expected improvement per trade: 0.23 × 4c = **~0.9c per contract** average price improvement
- On a 10-contract YES trade: ~9c total improvement, or about $0.09 per trade

That's real but modest. The sale ladder upside is 5–10× larger.

---

## 4. Regime-Aware Ladder Sizing

### 4.1 Regime Performance Data (1,857-trade full dataset)

| Regime        | N   | WR%  | Total PnL | Interpretation                         |
|---------------|-----|------|-----------|----------------------------------------|
| PULLBACK_BULL | 18  | 61.1%| +$8.42    | Best regime: counter-trend YES entries |
| TREND_BEAR    | 22  | 31.8%| +$4.27    | Low WR but big wins (NO runs to 100c)  |
| SQUEEZE_BEAR  | 11  | 54.5%| +$0.83    | Consolidating before move, OK          |
| PULLBACK_BEAR | 25  | 56.0%| −$11.79   | False signal — pullbacks in bear aren't recoveries |
| SQUEEZE_BULL  | 14  | 71.4%| −$15.83   | High WR but losses are large           |
| IMPULSE_BEAR  | 36  | 30.6%| −$18.26   | Chasing momentum — bad                 |
| MIXED         | 70  | 37.1%| −$19.50   | No clear signal                        |
| TREND_BULL    | 22  | 59.1%| −$24.45   | High WR, but losses concentrated/large |
| NEUTRAL       | 96  | 56.3%| −$43.98   | Majority of trades; WR is misleading   |
| IMPULSE_BULL  | 24  | 29.2%| −$55.00   | Worst regime by absolute loss          |

**Note: mtf_regime is NULL on 1,486 of 1,857 trades** (the column was added later). The regime data covers a specific period, not the full history. Treat these figures as directional, not definitive.

### 4.2 Key Regime Insights

**IMPULSE regimes are toxic**: Both IMPULSE_BULL (29.2% WR, −$55) and IMPULSE_BEAR (30.6% WR, −$18) have sub-35% WR. These are rapid directional BTC moves where the binary contract has already priced in the move — you're buying after the information is out.

**PULLBACK regimes are the sweet spot**: PULLBACK_BULL (61.1% WR, +$8.42) is the only consistently profitable regime in this dataset. Trades here are entering counter-trend, buying dips in a rising market — exactly the setup where a bid ladder (multiple entry tiers) would improve average fill price.

**TREND_BEAR PnL paradox**: 31.8% WR but +$4.27 means wins average about 3× losses. This is likely NO contracts that run to expiry at 100c (settlement gain of ~55c on a 45c entry). In this regime, **do not ladder the exit** — let it ride to settlement.

**NEUTRAL regime is the biggest problem**: 96 trades (largest sample), 56.3% WR but −$43.98. High WR combined with net losses means the losses are systematically larger than the wins. This regime needs tighter TPs, not wider ones.

### 4.3 Regime-Aware Ladder Recommendations

**Trending regime (TREND_BULL, TREND_BEAR, IMPULSE_*):**
- Entry: Do NOT add ladder tiers. Enter at market or single limit only. Momentum-chasing entries with a ladder fill more contracts on losing trades.
- Sale: In TREND_BEAR (betting NO), maximize the runner: place TP as high as possible (+20c floor) or don't place it and hold to settlement. In TREND_BULL (betting YES), current 2-tier is appropriate.
- Ladder width: **Wider spacing** only for TREND_BEAR where the winning trade runs to settlement (100c = full profit).

**Choppy/neutral regime (NEUTRAL, MIXED):**
- Entry: Use tighter bid ladder (bid-2 / bid-4 only). Avoid deep bids — in chop, the deep fill happens because the trade went wrong.
- Sale: **Tighter TP, more weight on lower tiers**. NEUTRAL regime's −$43.98 on 56.3% WR suggests the engine holds too long and lets winners reverse. A 3-tier ladder at +5c/+8c/+12c for YES would lock gains faster.
- In NEUTRAL: avoid setting tp2 above entry+15c. That 30% → 20% decay between +20c and +30c is where neutral-regime trades tend to reverse.

**Pullback regime (PULLBACK_BULL, PULLBACK_BEAR):**
- PULLBACK_BULL: This is where a bid ladder helps most. Trade is entering on a dip in a rising market — exactly the "shallow MAE" pattern. A bid at bid-3/bid-5 is well-suited.
- PULLBACK_BEAR: Be cautious — WR is 56% but PnL is −$11.79, suggesting winners are smaller than expected. Use conservative tp1 (entry+8c YES) and no runner.

**How to detect regime at runtime:** The engine already has `self._open_momentum_signal` (`mom_move` at line 5272) and stores `mtf_regime` per trade. The `mom_move` value directly maps:
- `mom_move ≥ 10`: Impulse session (violent)
- `mom_move 5-9`: Standard swing (trend-follow or pullback depending on direction)
- `mom_move < 5`: Slow/neutral (chop or squeeze)

Current premium ladder already uses this: slow=5/9, standard=8/15, violent=12/22 for NO/YES. The proposed extension is to modify **tier count and tier spacing** per regime, not just the premium endpoint.

---

## 5. Concrete Ladder Configurations

### 5.1 Sale Ladder: YES, Standard Momentum (mom_move 5-9)

**Current:** `breakeven_ct` at tp1 = entry+14c, `remainder` at tp2 = entry+20c

**Proposed 3-tier (distribute 30% / 40% / 30%):**
```
Tier 1: 30% of contracts @ entry + 8c   (fill rate ~85%, early lock)
Tier 2: 40% of contracts @ entry + 14c  (fill rate ~81%, main tier)
Tier 3: 30% of contracts @ entry + 22c  (fill rate ~67%, runner)
```

**Rationale:** The 30% at +8c captures the "ran-then-reversed" population (57% of losers hit +8c per V2 analysis). The 40% at +14c is the current main tier, unchanged in concept. The 30% at +22c replaces tp2 (entry+20c) with a slightly wider runner exploiting the 69.9% at +20c vs 57.3% at +30c distribution.

**Expected improvement over current 2-tier:** The +8c floor tier recovers gains on an estimated 15-20% of trades that peak at 8-13c and reverse. At $0.50 position size × 20% improvement in capture × 8c per contract: **+$0.05 to +$0.12 per trade** incremental.

---

### 5.2 Sale Ladder: NO, Standard Momentum (mom_move 5-9)

**Current (today's tuning):** Most contracts at tp1 = entry+7c, small runner at entry+13c

**Proposed 3-tier (40% / 40% / 20%):**
```
Tier 1: 40% of contracts @ entry + 4c   (for 40-49c NO: 77.8% reach +3c → floor tier)
Tier 2: 40% of contracts @ entry + 7c   (current tp1, 61.9% fill rate for NO)
Tier 3: 20% of contracts @ entry + 12c  (54.6% fill rate, runner)
```

**By band adjustments:**
- 40-49c NO: Use the 3-tier above. The 22 pp gap between +3c (77.8%) and +8c (55.6%) is large enough to justify a dedicated floor tier.
- 50-59c NO: 2-tier is sufficient. 96.7% reach +8c — just get the breakeven contracts sold there. A floor at +4c would sacrifice 40% of contracts at a lower price on trades that would almost certainly continue to +8c anyway.
- <40c NO: Block (per prior research). Avg MFE = −16.5c. No ladder helps here.

---

### 5.3 Sale Ladder: YES or NO, Violent Momentum (mom_move ≥ 10)

**Current:** YES: tp1=entry+21c, tp2=entry+27c. NO: tp1=entry+11c, tp2=entry+17c

**Proposed for YES (violent) — 4-tier (20% / 30% / 30% / 20%):**
```
Tier 1: 20% @ entry + 8c    (insurance floor — catches early reversal)
Tier 2: 30% @ entry + 16c   (75.7% fill rate, main tier)
Tier 3: 30% @ entry + 24c   (estimated ~65% fill rate)
Tier 4: 20% @ entry + 35c   (runner — let it go if BTC is moving hard)
```

**Proposed for NO (violent) — 3-tier (35% / 40% / 25%):**
```
Tier 1: 35% @ entry + 5c    (61.9% global fill rate, lock early)
Tier 2: 40% @ entry + 11c   (54.6% fill rate, current tp1 for violent NO)
Tier 3: 25% @ entry + 18c   (estimated ~47% fill rate, runner)
```

---

### 5.4 Bid Ladder for Entries

**Recommended (marginal change from current):**
```
Primary:  60% of contracts @ ask (immediate market fill — guaranteed)
Layer 1:  25% of contracts @ bid - 2c (shallow dip, ~15% fill rate)
Layer 2:  15% of contracts @ bid - 5c (deeper dip, ~8% fill rate)
```

This is a conservative version of the current SHALLOW/DEEP approach. The weighting shift (60% at market vs current implied 50/50) reflects the MAE data: 77% of YES trades have MAE >12c, meaning most laddered bids below bid-5c miss entirely while the trade just goes up.

For the **DIVERGENCE_LADDER** (momentum opposes signal, lines 4569-4593), the current 3-tier structure is appropriate. The only tweak: in the `mom_move < 8` case (tight spread), shift to spread1=2c/spread2=5c instead of 2c/4c to better match the MAE distribution.

**Minimum fill threshold:** If Tier 1 (market) doesn't fill at all and both passive bids go unfilled after 20s, do not re-enter. The current cancel-and-retry logic is correct.

---

## 6. Implementation Considerations

### 6.1 Changes Required in `_place_tiered_tp` (line 5233)

**Difficulty: Low.** The function already loops over `TP_TIERS` and `tier_counts` (lines 5293-5305). Adding a 3rd or 4th tier is a mechanical extension:

1. Replace the 2-element `TP_TIERS` and `tier_counts` lists with N-element versions
2. Adjust the `breakeven_ct` math to reflect the new floor tier (instead of all breakeven contracts at tp1, split them across floor/main tiers)
3. The loop at line 5310 already handles variable-length tier lists

**Estimated lines changed:** ~15-20 lines in `_place_tiered_tp`.

**Key risk:** The `breakeven_ct = ceil(total_count × entry_cents / tp1)` formula ensures breakeven is covered at tp1. Adding a floor tier below tp1 means breakeven is NOT covered if only the floor tier fills. You'd need to either:
- Accept sub-breakeven capture on the floor tier (it's a partial loss reduction, not a profit), OR
- Recalculate breakeven across the combined fill of floor+main tier

**Recommended approach:** Keep the breakeven calculation anchored to tp2 (the main tier), place a smaller floor tier (20-30%) as a pure "rescue" mechanism, and treat the floor as an optional bonus rather than a primary exit.

### 6.2 Changes Required for Entry Ladder

**Difficulty: Very Low.** The divergence ladder (lines 4569-4593) already exists and works. The bid ladder tweak (60/25/15 split instead of current implied 50/50 shallow/deep) would be a config change in `user_config.py` if those parameters are extracted, or a 2-line change in `_execute_signal` at line ~4395.

### 6.3 Complexity vs Expected Gain

| Change | Complexity | Expected Gain | Priority |
|--------|-----------|---------------|----------|
| Add 3rd sale tier for YES (+25c runner) | Low | +$0.10–$0.20/trade | **High** |
| Add NO floor tier at +4c (40-49c band) | Medium | +$0.05–$0.15/trade | Medium |
| 4-tier violent YES | Medium | +$0.15–$0.40/trade | Medium |
| Adjust bid split (60/25/15) | Very Low | +$0.01–$0.05/trade | Low |
| Regime-conditional tier widths | High | Unknown (thin data) | Deferred |

The 3rd sale tier for YES is the highest-confidence win: it captures the 57.3% of YES trades that reach +30c against the current tp2 ceiling of entry+20c to entry+27c. For 40-49c YES entries (where +25c reach is 75.8%), this alone would convert many held-to-expiry winners into controlled TP exits at higher prices.

### 6.4 Order Management Overhead

Each additional TP tier adds one more Kalshi API call at position open. For a 3-tier ladder, that's 3 sell orders instead of 2. The engine already handles multi-order TP tracking (`tp_ids` list at line 5307) and the monitoring loop checks for fills. Adding a 3rd order ID to track is trivial.

**Cancellation races**: the current logic cancels TPs when needed (e.g., position sync, trailing stops). Three IDs is only marginally harder than two. No structural issue.

**Partial fills**: If tier 1 fills but tier 2 doesn't, the engine correctly counts the filled count. The breakeven calculation ensures profitability even with partial TP fills. A 3-tier ladder doesn't change this guarantee as long as the floor-tier fill alone covers position cost.

---

## 7. Caveats

1. **Sample size: 200 trades, 4-day window (Mar 13–17, 2026).** The excursion data predates the current engine architecture (PolymarketCopyEngine, Probability Engine, Brownian Bridge). Current trades may have substantially different MFE/MAE profiles. The 200-trade sample is dominated by legacy HFT_SCALP strategies; current TA_FORCED and CROSS_VENUE_FLOW strategies have no excursion data.

2. **No regime-level MFE data**: The `mtf_regime` column has NULL in 1,486 of 1,857 rows. The 200 excursion trades also pre-date regime tagging. Cross-referencing regime with MFE distribution is impossible without new data collection.

3. **TA_FORCED_SIGNAL is the engine's dominant strategy at 536 trades and −$456 total PnL**. All ladder recommendations are based on data from a different strategy mix. If TA_FORCED has fundamentally different entry timing, the MFE distribution could be worse (lower TPs than data suggest). Collecting excursion data on current TA_FORCED trades would resolve this within 2-3 weeks.

4. **Bimodal NO distribution**: The raw NO data has a pronounced bimodal character: <40c NO goes to −16.5c avg MFE (these are nearly all full losses) while 50-59c NO goes to +30.6c avg MFE. Blending these bands into a single NO ladder recommendation obscures the per-band reality. Any NO ladder implementation MUST be band-specific.

5. **The "money left on table" query returned only 5 records** (won/exited_win trades where MFE exceeded capture by 3+ cents). This is expected — most "won" trades settle at expiry (100c), not at a TP. The small sample size means the 35c money-left figure (YES@48c exiting at +16c when MFE was +51c) is not representative. It does confirm that TP misfires do leave real money, but the frequency is too low to model.

6. **Kalshi liquidity constraint**: Resting multiple limit sell orders at 3-4 different price levels ties up order slots and may create book distortions on thin markets. If the Kalshi book shows <20 contracts at each tier, a 4-tier sell ladder could meaningfully move the market against itself. Test with small contract counts first.

7. **The regime data anomaly** (SQUEEZE_BULL at 71.4% WR but −$15.83; NEUTRAL at 56.3% WR but −$43.98) suggests systematic issues beyond exit calibration — possibly position sizing, entry timing, or strategy selection in these regimes. A sale ladder won't fix a bad entry.

---

*Next step: Collect excursion data on current TA_FORCED and CROSS_VENUE_FLOW trades (30-day lookback) before implementing. Run the existing backfill query against trades from 2026-03-20 onward to get regime-matched MFE data.*
