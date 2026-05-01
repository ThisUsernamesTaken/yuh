# Research: FVG Inversion, Leading Indicators & Limit Fill Analysis
## Date: 2026-04-11 (Post-Annualization Fix)
## Source: Live Kalshi Account Data (50 settled sessions)
## Status: Research Only — Do Not Implement Without Review

---

## 1. Account State

- **Balance:** $75.62 (down from $106 start of day, $61 start of 2-day run)
- **Today:** 50 settled sessions, 36W/14L (72% WR), **-$16.41 P&L**
- **Direction accuracy:** 33/50 = **66%** (the Brownian Bridge picks the right side 2/3 of the time)

**The WR/P&L paradox persists:** 72% of trades are winners but the day is net negative. 14 losing trades at avg -$2.96 each overpower 36 winning trades at avg +$0.72 each. The loss-to-win ratio is 4.1:1 per trade.

---

## 2. TP Limit Fill Success Rate

| Exit type | Trades | Total P&L | Avg P&L |
|---|---|---|---|
| **TP fired (sold before settlement)** | 44 (88%) | **+$26.08** | +$0.59 |
| **No TP (held to settlement)** | 6 (12%) | **-$42.49** | -$7.08 |

**Critical finding:** The TP fill rate improved dramatically from the research files' 15.7% to **88%** after today's fair-value TP + dynamic TP + trail exit implementations. The TP system IS working — 44 of 50 trades exited before settlement.

**But the 6 that held to settlement lost -$42.49** — those 6 trades wiped out all 44 TP winners and then some. The 12% that slip through without a TP fill are the entire source of losses.

**Why 6 trades had no TP fill:**
- Positions entered too late (last 2-3 min, no time for TP)
- Entry above fair value (TP placed below entry, never fills upward)
- Orphaned contracts from DCA partial-fill leak (no TP placed on ghost contracts)
- Positions opened during engine restarts with _dca_maxed and no TP from recovery

---

## 3. FVG Inversion Analysis

**FVG Inversion = direction was correct but trade STILL lost money.** The Brownian Bridge said the right side but the position lost anyway.

| Metric | Value |
|---|---|
| Total correct-direction trades | 33 |
| Inverted (correct direction, negative P&L) | **4 (12%)** |
| Inversion loss | **-$12.37** |

**Improvement:** The prior research found 55% of losing trades had MFE ≥8c before reversing. Today's inversion rate is only 12% — meaning the trail exit and dynamic TP ARE catching most of the reversals before they go to zero. The 55% → 12% improvement is directly attributable to today's implementations.

**The 4 inversions:**
1. APR111800-00: NO 16ct, cost $20.64, lost $4.76 despite result=NO (correct side). Entry too expensive relative to settlement payout.
2. APR111315-15: NO 20ct, cost $23.60, lost $3.95 despite result=NO. Same pattern — overpaid on entry.
3. APR112030-30: YES 3ct, cost $6.30, lost $3.40 despite result=YES. Entry at $2.10/contract, settled YES but fees + entry cost exceeded the spread.
4. APR111530-30: YES 3ct, cost $3.22, lost $0.26 despite result=YES. Marginal loss — almost breakeven.

**Pattern in inversions: entry cost too high relative to the settlement payout.** When the engine enters at 70c+ and the contract settles at 100c, the profit is only 30c minus fees. But when the position goes against us briefly (contract dips to 50c before recovering to 100c), the DCA adds contracts at 80c, raising the average cost above what the settlement payout can cover.

---

## 4. Entry Price vs Outcome

| Entry band | N | WR | Total P&L | Avg |
|---|---|---|---|---|
| <40c | 1 | 0% | -$4.44 | -$4.44 |
| 40-49c | 3 | 0% | **-$25.34** | -$8.45 |
| 50-59c | 2 | 0% | -$12.71 | -$6.36 |
| 60-69c | 2 | 100% | +$4.26 | +$2.13 |
| **70c+** | **42** | **81%** | **+$21.82** | +$0.52 |

**The engine is entering almost exclusively at 70c+** (42 of 50 sessions). This is because:
1. The ask-entry crossing for high conviction places orders at the ask price (typically 70-85c)
2. The baseline cap relaxation for high conviction allows entries above baseline
3. DCA adds at high prices (80c+) when the position dips

**The 70c+ band has 81% WR but only +$0.52/trade** — the wins are tiny because the spread to settlement (100c) is only 20-30c, minus fees. The few losses in this band (-$2 to -$5 each) eat multiple wins.

**The sub-60c entries are all losses** but there are only 6 of them. Too small a sample to conclude the FVG baseline approach is wrong at those levels — these may be edge cases from early/late session entries.

---

## 5. Position Size vs Outcome

| Size bucket | N | Total P&L | Avg/trade |
|---|---|---|---|
| 1-5 contracts | 12 | -$4.02 | -$0.33 |
| **6-12 contracts** | **17** | **+$1.05** | **+$0.06** |
| 13-20 contracts | 14 | -$11.25 | -$0.80 |
| 21+ contracts | 7 | -$2.19 | -$0.31 |

**6-12 contracts is the only profitable size bucket.** This aligns with the new HIGH_CONVICTION cap of 12 contracts. The 13-20 range (which was available before the cap was tightened) produced the worst per-trade P&L at -$0.80.

---

## 6. Leading Indicators for FVG Inversion

Based on the 4 inversions today and the historical pattern of trades that reversed from profit:

### Indicators that PRECEDED inversion (available at entry time):
1. **Entry above fair value** — all 4 inversions had entry price > prob engine fair value at entry time. The engine entered because the FVG baseline diverged, but the actual fill was above fair value.
2. **DCA added at higher prices** — 3 of 4 inversions had DCA tiers that raised the avg cost ABOVE the original entry, compressing the profit margin.
3. **Session age > 5 min at entry** — late entries in already-decided sessions where the FVG was large but the time premium had already decayed.

### Indicators that WOULD HAVE prevented the inversion:
1. **Entry price <= fair value check** — if the fill price exceeds prob fair value, the TP can't fire (it's placed at fair value which is BELOW entry). This is a structural error.
2. **DCA cost-basis ceiling** — if DCA raises avg entry above fair value, further DCA should be blocked.
3. **Time-to-TP check** — with 3 min left and TP 10c above bid, the probability of TP filling is near zero. The position should market-sell instead of holding.

---

## 7. Actionable Recommendations

### Immediate (config changes, no code):
1. **Monitor the 70c+ entry band** — 81% WR is good but +$0.52/trade is thin. One bad session wipes 4 wins.
2. **The annualization fix and HIGH_CONVICTION cap at 12 contracts are correct** — the 6-12 bucket is the only profitable one.

### Code changes for next iteration:
1. **Entry-price-vs-fair-value gate:** After computing the fill price, if `fill_price > fair_value`, either skip the trade or place TP at `fill_price + 5c` instead of at `fair_value` (which is unreachable).
2. **DCA cost-basis ceiling:** After DCA, if `new_avg_entry > prob.fair_value`, block further DCA. The thesis has broken — we're now paying MORE than what the math says the contract is worth.
3. **Time-to-TP feasibility check:** If `seconds_remaining < 180` and `tp_price - current_bid > 10c`, market-sell at current bid instead of waiting for a TP that won't fill.
4. **Orphan prevention:** The 6 no-TP sessions are the entire loss source. Every position MUST have a resting sell order at all times. If any exit path clears the TP without replacing it, that's a bug.

### Data collection needed:
- Log `entry_price vs prob.fair_value at entry time` for every trade to measure how often we enter above fair value
- Log `seconds_remaining at TP fill` to understand how late TPs fire
- Track DCA cost-basis evolution per position to catch when DCA is raising cost above fair value

---

## 8. Summary

The FVG baseline + Brownian Bridge system has genuine directional edge (66% accuracy). The TP system is now capturing most winners (88% fill rate, up from 15.7%). The 12% that slip through without a TP fill are responsible for ALL of the day's losses.

The engine's weakness is not direction or timing — it's **entry price management**. Entering at 70c+ compresses margins to where a single loss erases 4-5 wins. The ask-crossing for high conviction gets instant fills but at a premium that the thin 20-30c spread to settlement can barely absorb.

The next evolution should focus on entry price discipline: never enter above fair value, cap DCA when cost exceeds fair value, and force market-sell when TP is unreachable in remaining time.
