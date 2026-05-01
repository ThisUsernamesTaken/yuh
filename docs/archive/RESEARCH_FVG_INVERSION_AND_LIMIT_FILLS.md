# RESEARCH — Fair Value Gap Inversion, Leading Indicators, and Limit Order Fill Analysis
## Do Not Implement

**Generated**: 2026-04-11  
**Account**: 725e4156-1c27-4d16-87ab-011ae9e460eb  
**Engine status at time of research**: Running (PolymarketCopyEngine, TA_FORCED primary)

---

## 1. Live Account Snapshot

| Field | Value |
|---|---|
| **Available balance** | $78.24 (7,824c) |
| **Portfolio value** | $0.00 |
| **Open positions** | None |

### Current KXBTC15M Contract

```
Ticker:   KXBTC15M-26APR111845-45
Status:   active
Minutes to expiry: ~13.6

Orderbook (live at pull time):
  Orderbook mid:    21.5c  ← true market-implied YES probability
  Contract display: YES bid/ask 26/27c  ← stale or spread-adjusted
  Spread:           1c (tight, high liquidity)
  Imbalance:        -0.273 (NO-heavy — bearish 15m window)
  Microprice:       21.4c

  Top YES bid:  21c × 771 contracts
  Top NO  bid:  78c × 1,349 contracts
  YES depth:    5 levels → 21c, 20c, 19c, 18c, 17c
  NO  depth:    5 levels → 78c, 77c, 76c, 75c, 74c
  Total YES liq: 109,929 contracts
  Total NO  liq: 247,195 contracts (2.25× heavier on NO side)
```

**Interpretation**: The market is pricing YES at ~21.5c — an 78.5% probability BTC closes DOWN this 15m candle. The NO-heavy book (2.25× liquidity differential) suggests directional flow is strongly bearish. Any engine signal suggesting YES entry here would be swimming against deep liquidity.

### Recent Fills (last 48h — Kalshi settlements)

The following table lists every KXBTC15M settlement in the past 48 hours. Both YES and NO counts appear in the same record because the engine sometimes holds both sides simultaneously (flip-invert + laddering).

| Time (UTC) | Ticker | Result | YES cnt | YES cost | NO cnt | NO cost | Net est. |
|---|---|---|---|---|---|---|---|
| 22:15 Apr11 | ...1815-15 | **yes** | 30 | $29.37 | 30 | $13.09 | -$12.46 |
| 22:00 Apr11 | ...1800-00 | **no**  | 16 | $14.08 | 16 | $6.56  | -$7.52  |
| 21:45 Apr11 | ...1745-45 | **yes** | 0  | $0.00  | 20 | $11.00 | -$11.00 |
| 21:30 Apr11 | ...1730-30 | **no**  | 23 | $1.80  | 23 | $20.70 | +$18.90 |
| 21:00 Apr11 | ...1700-00 | **no**  | 3  | $1.41  | 3  | $1.32  | +$1.68  |
| 20:45 Apr11 | ...1645-45 | **no**  | 7  | $1.05  | 7  | $5.66  | +$6.95  |
| 19:30 Apr11 | ...1530-30 | **yes** | 3  | $2.19  | 9  | $1.03  | +$0.81  |
| 19:15 Apr11 | ...1515-15 | **yes** | 7  | $3.63  | 7  | $1.79  | +$3.37  |
| 19:00 Apr11 | ...1500-00 | **yes** | 0  | $0.00  | 3  | $1.71  | -$1.71  |
| 18:45 Apr11 | ...1445-45 | **yes** | 20 | $14.40 | 20 | $4.62  | +$5.60  |
| 18:15 Apr11 | ...1415-15 | **yes** | 11 | $2.20  | 11 | $8.25  | -$8.25* |

> *Both YES and NO held simultaneously at settlement = doubled exposure, partial hedging.

**Critical observation**: The 22:15 settlement (result=YES, we held 30 NO contracts costing $13.09 + 30 YES at $29.37 = $42.46 total) returned only $30.00 (30 × $1.00 YES payout). Net loss: **-$12.46 on a WIN**. This indicates the engine simultaneously entered both sides — the original NO position was not fully exited before a flip or second entry. The YES entries at ~$0.98/contract ($29.37 / 30) suggest late reconciliation or near-expiry buying.

---

## 2. Seven-Day Performance Summary

```sql
-- Methodology: wins counted as status IN ('won','exited_win')
-- OR status='reconciled_settled' AND pnl > 0
```

| Date | Trades | Wins | WR% | PnL ($) |
|---|---|---|---|---|
| 2026-04-11 (partial) | 47 | 32 | 68.1% | -$12.31 |
| 2026-04-10 | 39 | 29 | 74.4% | -$2.91 |
| 2026-04-09 | 56 | 41 | 73.2% | **+$7.84** |
| 2026-04-08 | 141 | 65 | 46.1% | -$16.70 |
| 2026-04-07 | 75 | 49 | 65.3% | -$43.64 |
| 2026-04-06 | 47 | 40 | 85.1% | -$18.26 |
| 2026-04-05 | 41 | 24 | 58.5% | -$34.75 |
| **7-day total** | **446** | **280** | **62.8%** | **-$120.73** |

**The WR/PnL paradox**: 62.8% WR across 7 days, yet -$120.73 PnL. This is not a WR problem — it is a **payout asymmetry** problem. The engine frequently enters at 50-65c, where a loss costs more than a win earns. At 60c entry: loss = -60c, win = +40c. Required breakeven WR = 60%, and the engine is only at 62.8% — barely above water by WR alone, but position sizing amplifies losses disproportionately on down swings.

### Status Breakdown (last 7 days)

| Status | Count | Avg PnL (c) | Total PnL (c) |
|---|---|---|---|
| reconciled_settled | 379 | -35.78c | -13,560c |
| lost | 56 | -45.75c | -2,562c |
| won | 45 | +51.82c | +2,332c |
| exited_win | 9 | +64.22c | +578c |
| exited_loss | 3 | -408.00c | -1,224c |
| extreme_exit | 2 | -319.50c | -639c |
| reconciled_closed | 4 | +905.61c | +3,622c |
| unfilled | 6 | 0 | 0 |

`reconciled_settled` dominates (379/499 = 76% of settled trades) and averages **-35.78c**. These are positions held to expiry that settled at 0c. The 4 `reconciled_closed` trades show +905c avg — these appear to be TP exits captured just before contract close. The 3 `exited_loss` trades average **-408c** — these are large position multi-contract stops.

### Balance Trajectory (recent)

| Time (UTC) | Balance | Side | Entry | Contracts | Event |
|---|---|---|---|---|---|
| 22:14 Apr11 | **$78.24** | — | — | — | window_change |
| 22:03 Apr11 | $89.38 | NO | 60c | 3 | entry_TA_FORCED |
| 21:50 Apr11 | $89.38 | NO | 41c | 16 | entry_TA_FORCED |
| 21:32 Apr11 | $95.94 | NO | 55c | 20 | entry_TA_FORCED |
| 21:22 Apr11 | $85.84 | NO | **90c** | 23 | entry_TA_FORCED |
| 21:14 Apr11 | $106.55 | — | — | — | window_change |
| 20:46 Apr11 | $104.87 | YES | 47c | 3 | entry_TA_FORCED |
| 20:31 Apr11 | $103.58 | NO | **82c** | 3 | entry_TA_FORCED |
| 18:31 Apr11 | $91.35 | YES | **72c** | 20 | entry_TA_FORCED |
| 18:05 Apr11 | $96.95 | NO | **75c** | 11 | entry_TA_FORCED |

**Entry range violation — CRITICAL FINDING**: TA_FORCED is configured for 40–55c entries but the balance snapshots show entries at **60c, 72c, 75c, 82c, 90c**. The 90c NO entry (23 contracts × 90c = $20.70 risk on a 10c YES probability contract) and 82c NO entry are extreme. Either the `limit_price` recorded reflects the fill price after a ladder adjusted to a different level, or the TA_FORCED range check is being bypassed. This requires investigation in the code at the TA entry approval block.

---

## 3. Fair Value Gap Analysis

### Definition

In the context of KXBTC15M binary contracts:

- **Fair value**: The "true" probability that BTC closes up in a 15-minute window, estimated by the engine's probability module (Brownian Bridge / Normal CDF via the Prob Engine, or TA-based confidence for TA_FORCED).
- **Contract price**: The market-implied probability — what Kalshi consensus buyers currently price at.
- **Fair Value Gap (FVG)**: The difference `predicted_prob × 100 − limit_price`. A positive FVG means the engine believes the contract is underpriced (edge exists). A negative FVG means the engine entered into an overpriced contract.
- **FVG Fulfillment**: The contract moves toward fair value — price increases (for a cheap YES entry) and either the TP limit fills or the contract settles at 100c.
- **FVG Inversion**: The contract starts moving toward fair value (price rises, giving positive MFE) but then reverses completely, ending at 0c (for a YES position). The gap "inverts" — what looked like mispricing turned out to be correct pricing, and we were on the wrong side.

### Fulfillment Rate: Cheap vs. Expensive Entries

```
Cheap entry  (<50c): engine believes contract is underpriced — "long the gap"
Expensive entry (≥50c): engine is following momentum or buying into consensus
```

| Entry Type | Side | FVG Fulfilled | FVG Inverted | TP Captured | Stopped | Success% | Total |
|---|---|---|---|---|---|---|---|
| cheap_entry (<50c) | NO  | 161 | 283 | 41 | 29 | **39.2%** | 515 |
| cheap_entry (<50c) | YES | 143 | 246 | 17 | 4  | **38.7%** | 413 |
| expensive_entry (≥50c) | NO  | 288 | 194 | 10 | 6  | **58.7%** | 508 |
| expensive_entry (≥50c) | YES | 336 | 155 | 6  | 5  | **67.5%** | 507 |

**Key insight — the FVG paradox**: Cheap entries (<50c) that the engine labels as "underpriced" lose **60–61% of the time**. Expensive entries (≥50c) win 59–68% of the time. This is the **opposite** of what a valid FVG edge predicts.

There are two explanations:
1. **The engine's predicted_prob is systematically too high** — it calls contracts "underpriced" when the market has it right. The Prob Engine or TA module is over-estimating upward probability, particularly for cheap (bearish-regime) contracts.
2. **Cheap entries are cheap for a reason** — at 30c YES, the market consensus says 70% probability BTC goes down. The engine's 55c predicted_prob is wrong. The FVG is an illusion.

The expensive entries win more not because of FVG edge but because the engine is accidentally momentum-following: when YES is at 65c, BTC is already trending up in that 15-minute window, and the engine is catching the tail end of a genuine trend.

### FVG Size vs. Performance

| FVG Size | Avg Gap | Trades | WR% | Total PnL (c) |
|---|---|---|---|---|
| small_fvg (<5c)   | 2.8c | 93  | 59.1% | -1,567c |
| medium_fvg (5-10c) | 7.4c | 79  | 51.9% | -4,656c |
| large_fvg (≥10c)  | 29.1c | 728 | 61.1% | -11,209c |

**Larger claimed FVGs have higher WR but worse total PnL**. This is a classic sign that: (a) win sizes are capped (TP at +15/+20%) but losses are proportional to entry cost (up to 100c loss), and (b) the large FVG entries tend to be at lower prices (cheap contracts, cheap YES at 30c) where losses are smaller in dollars but wins are also smaller and wins/losses are more frequent.

The 728 large-FVG trades show 61.1% WR but -$112.09 PnL — meaning the predicted_prob that generates these large claimed edges is **not predictive of actual settlement outcome**.

### Inversion Pattern: Losing Trades with Positive MFE

Of 114 losing trades with excursion data:

| MFE Threshold | Count Inversions | % of All Losers |
|---|---|---|
| MFE ≥ 3c | 78 | 68.4% |
| MFE ≥ 5c | 68 | 59.6% |
| MFE ≥ 8c | 63 | 55.3% |
| MFE ≥ 10c | 58 | 50.9% |

**55.3% of losing trades had MFE ≥ 8c before completely reversing to loss**. These are classic inversions — the FVG started closing (price moved toward fair value by 8c+) then the entire move unwound and the contract settled at 0c.

Sample inversion cases (worst 30 losers by MFE):

| Side | Entry | PnL(c) | MFE(c) | MAE(c) | Pattern |
|---|---|---|---|---|---|
| YES | 31c | -31c | 57c | 28c | had_profit |
| YES | 34c | -4c  | 54c | 31c | had_profit |
| YES | 45c | -21c | 54c | 5c  | had_profit |
| NO  | 52c | -25c | 50c | 31c | had_profit |
| NO  | 54c | -54c | 49c | 45c | had_profit |
| YES | 9c  | -9c  | 47c | 6c  | had_profit |
| YES | 42c | -42c | 46c | 39c | had_profit |
| YES | 53c | -28c | 46c | 5c  | had_profit |
| NO  | 43c | -37c | 42c | 56c | had_profit |
| YES | 58c | -36c | 41c | 57c | had_profit |

Notable: A YES entry at 31c reached MFE of 57c (a +26c move = +84% gain on the position) before reversing completely to a -31c loss. This is the most extreme inversion in the dataset — the contract moved from 31c to 88c then cratered to 0c.

### MFE/MAE Distribution by Outcome

| Outcome | MFE bucket | Count | Avg MAE | Avg PnL(c) |
|---|---|---|---|---|
| loss | 0-2c    | 38 | 56.4c | -33.9c |
| loss | 3-4c    | 10 | 47.4c | -44.3c |
| loss | 5-7c    | 5  | 39.4c | -36.0c |
| loss | 8-14c   | 18 | 44.9c | -54.3c |
| loss | 15c+    | 57 | 34.5c | -125.9c |
| win  | 0-2c    | 2  | 82.0c | +6.5c |
| win  | 15c+    | 70 | 14.4c | +51.7c |

The 57 losing trades with MFE ≥ 15c average **-125.9c PnL** — these are large multi-contract positions that ran deeply in favor then fully reversed. The avg MAE on these is 34.5c, meaning the bid dropped 34.5c from the entry point at their worst. These are the most damaging trades: they look like they're working, then catastrophically fail.

---

## 4. Leading Indicator Cross-Reference

### Available Indicator Columns

From `kalshi_trades`: `predicted_prob`, `expected_wr`, `mtf_score`, `mtf_regime`, `brier_score`, `log_loss`, `signal_wallets`  
From `trade_decisions`: `ta_confidence`, `ta_tier`, `ta_candle_pressure`, `ta_cycle_return`, `ta_ema_spread`, `ta_rsi`, `flow_conviction`, `was_inverted`

### predicted_prob Accuracy

```
Total trades:            1,943 (settled)
Has predicted_prob:        900 (46.3% coverage)
Avg predicted_prob:       54.88%
Avg entry price:          48.82c
Avg claimed edge:         +3.6c per trade
```

Only 46% of trades have a recorded predicted_prob. The other 54% (mostly older or strategy-specific entries) have NULL.

| Predicted Prob | Trades | WR% | Total PnL (c) |
|---|---|---|---|
| <50% | 495 | 56.6% | -5,054c |
| 50–55% | 25 | 64.0% | +651c |
| 55–60% | 16 | 75.0% | -675c |
| 60–65% | 28 | 64.3% | -3,123c |
| 65%+ | 336 | 64.0% | -9,232c |
| NULL | 1,043 | 44.2% | -9,846c |

**Finding**: Higher predicted_prob bands correspond to higher WR (56.6% → 75% as prob goes from <50% to 55-60%). However, the total PnL is negative across every bracket except 50–55%. The 65%+ bucket has 64% WR but -$92.32 PnL — 336 trades where we were "very confident" yet lost heavily. The avg entry in the 65%+ bucket is likely high (60c+), where losses are large per contract.

The 50–55% bucket (+651c / +$6.51) is the **only profitable predicted_prob range**. This is the range closest to "true uncertainty" — slight edge, tight entries, manageable losses.

### TA Confidence Tier vs Outcome (trade_decisions, recent)

| TA Tier | Examples | Pattern |
|---|---|---|
| STRONG (100%) | NO@37c, YES@41c, NO@36c, YES@53c, YES@72c | Often extreme entries (very low YES or very high YES) |
| STRONG (65–95%) | YES@65c, YES@63c, NO@60c, YES@65c | High YES/NO entries, mixed outcome |
| MEDIUM (54–72%) | NO@52c, NO@46c | Near mid-range entries |
| WEAK (17–32%) | NO@56c, NO@41c | Low conviction, near-mid entries |
| MIMIC | YES@72c, NO@43c, NO@37c | Wallet follow, mixed confidence |

**TA_STRONG fires on extreme market conditions** (very low YES price = strong bearish regime, or very high YES = strong bullish). These are the entries that violate the 40–55c configured range. STRONG signals at 100% confidence reflect "the TA says the trend is overwhelming" but the entry price is already at an extreme where the contract is near fully resolved.

### flow_conviction Predictiveness

`flow_conviction` is NULL for all recent trade_decisions entries — wallet flow is not being populated into the trade_decisions log. This means we cannot directly correlate flow conviction with FVG fulfillment from this table. The `signal_wallets` JSON in `kalshi_trades` would contain wallet flow data but requires additional parsing.

`was_inverted` is 0 in all 20 recent trade_decisions entries — the TA inversion logic is either not triggering or not being logged.

---

## 5. Limit Order Sale (Take-Profit) Success Rate

### TP Fill Rate by Side

| Side | TP Filled (exited_win) | Held to Settlement (won) | Total Wins | TP Fill% |
|---|---|---|---|---|
| YES | 23 | 206 | 229 | **10.0%** |
| NO  | 51 | 192 | 243 | **21.0%** |
| Combined | 74 | 398 | 472 | **15.7%** |

**84% of winning trades hold to settlement** rather than exiting via TP limit orders. The engine places TPs at entry × 1.15 (15% TP) and entry × 1.20 (20% TP), but only 15.7% of winners actually hit those limits. For NO-side trades, the TP fill rate (21%) is double that of YES (10%).

**Why NO fills 2× more**: NO contracts are priced higher (typically 55–75c), meaning they move faster in absolute cents when BTC direction is clear. A NO entry at 65c with TP at 75c (65 × 1.15) needs a 10c move. A YES entry at 40c with TP at 46c needs a 6c move. Despite smaller absolute movement, the YES TP fill rate is much lower — possibly because YES contracts near 40c are in bearish regimes where the reversal to YES is brief and volatile.

### Capture Efficiency (MFE-based)

| Side | Avg MFE (c) | Avg Captured (c) | Left on Table (c) | Capture Eff.% | N |
|---|---|---|---|---|---|
| YES | 52.3c | 53.3c | -1.0c | 102.0% | 39 |
| NO  | 39.3c | 51.7c | -12.4c | 131.6% | 26 |

**Capture efficiency >100% means the engine captures MORE than the highest mid-session bid observed**. This happens because `mfe_cents` tracks the peak bid during the active position, but settlement pays 100c — so a YES position bought at 40c that settles YES earns 60c, but if the peak mid-session bid was only 52c (MFE=12c), the settlement payout (60c) far exceeds the MFE.

The implication: **the engine is NOT capturing mid-session moves via TP — it is capturing settlement value**. The 102% YES capture efficiency and 131% NO capture efficiency are both artifacts of holding to settlement on winning trades, NOT of intelligent TP optimization.

This means the TP limit orders placed at 15–20% profit are **structurally too tight** to fill in most winning trades, because the contract doesn't drift to those levels mid-session — it either stays flat (no fill) or jumps to 100c at settlement (captured by hold-to-expiry logic). The TP system as currently deployed has low utility.

### TP Price Optimization Analysis

Given that 84% of wins come from settlement (0c or 100c) rather than mid-session exits:

| TP Target Level | Estimated Fill Rate | Effect |
|---|---|---|
| Current: entry × 1.15 (15% profit) | ~10-21% | Rarely hits; most wins are settlement |
| Higher: entry × 1.30 (30% profit) | Lower (~5%) | Would fill even less often |
| Lower: entry × 1.08 (8% profit) | Estimated ~35-45% | Would fill more; locks in small gains |
| Floating near best bid - 2c | Very high (50%+) | Exits immediately on any favorable move |

A counter-intuitive recommendation emerges: **if the goal is to capture mid-session moves (vs. hold to settlement), the TP should be dramatically lower — e.g., entry × 1.05 to 1.08.** However, if the engine's strength is holding to settlement (current design), then TP orders are mostly irrelevant noise that occasionally exit a position that would have won at 100c.

---

## 6. Strategy Performance Summary

| Strategy | Trades | WR% | Total PnL (c) | Avg Entry (c) |
|---|---|---|---|---|
| HFT_SCALP_TAKE_PROFIT | 226 | 54.4% | **+592c** | 44.2c |
| HFT_SCALP_TRAIL_STOP | 13 | 76.9% | **+413c** | 45.2c |
| E-TRENDDN-LONDON | 47 | 38.3% | **+441c** | 28.6c |
| E-UNKNOWN-NY_PRIME | 13 | 76.9% | **+228c** | 42.7c |
| TREND_FOLLOW | 17 | 58.8% | -3c | 76.2c |
| CROSS_VENUE_FLOW | 418 | 50.0% | **-8,582c** | 53.7c |
| TA_FORCED_SIGNAL | 632 | 62.5% | **-12,136c** | 54.7c |
| HFT_SCALP_STOP_LOSS | 298 | 36.9% | -1,988c | 43.8c |
| MIMIC_SMART_FLOW | 64 | 43.8% | -1,715c | 49.9c |
| E-TRENDUP-ASIA | 22 | 36.4% | -2,337c | 38.7c |
| E-TRENDDN-NYPRIME | 15 | 46.7% | -1,115c | 35.4c |
| E-TRENDDN-NYOPEN | 32 | 62.5% | -536c | 41.4c |

**TA_FORCED_SIGNAL is the engine's primary driver of losses**: 632 trades (32.5% of all settled trades), 62.5% WR, but -$121.36 PnL. With avg entry at 54.7c, breakeven WR is ~54.7%. At 62.5% WR the math suggests profitability, but position sizing and entry range violations (60c+, 75c+, 90c entries logged above) are inflating per-trade losses.

**CROSS_VENUE_FLOW is the second largest loser**: 418 trades, 50.0% WR, -$85.82 PnL. At exactly 50% WR with avg 53.7c entries, this strategy is a breakeven-at-best system with fees tipping it negative.

---

## 7. Entry Price Band Analysis

| Band | Side | Trades | WR% | Total PnL (c) | Avg Entry |
|---|---|---|---|---|---|
| 35–39c | NO  | 245 | 32.2% | **+256c** | 27.9c |
| 35–39c | YES | 205 | 29.8% | -1,132c | 27.4c |
| 40–44c | NO  | 102 | 44.1% | -2,596c | 42.3c |
| 40–44c | YES | 96  | 41.7% | -5,170c | 42.2c |
| 45–49c | NO  | 168 | 46.4% | -421c  | 47.2c |
| 45–49c | YES | 112 | 52.7% | -1,101c | 47.2c |
| 50–54c | NO  | 190 | 54.2% | -8,620c | 52.2c |
| 50–54c | YES | 141 | 60.3% | **+3,184c** | 51.9c |
| 55–59c | NO  | 158 | 56.3% | -1,658c | 56.5c |
| 55–59c | YES | 152 | 65.1% | -2,172c | 56.6c |
| 60–64c | NO  | 72  | 61.1% | -2,372c | 61.6c |
| 60–64c | YES | 77  | 71.4% | -4,550c | 61.9c |
| 65–69c | NO  | 25  | 68.0% | -935c  | 66.6c |
| 65–69c | YES | 44  | 68.2% | -72c   | 66.4c |
| 70c+   | NO  | 63  | 71.4% | **+87c**  | 78.0c |
| 70c+   | YES | 93  | 78.5% | -7c    | 79.5c |

**Notable findings:**

1. **35–39c NO is the ONLY consistently profitable cheap-entry band** (+256c, 32.2% WR). At 35c entry, a win pays +65c, loss costs -35c. Breakeven WR = 35%. Even at 32.2% WR it's marginally profitable. The edge here may be real: very cheap NO contracts (YES at 35c) settle at 0c often enough to matter.

2. **50–54c YES is strongly profitable** (+3,184c, 60.3% WR). At 52c avg entry, breakeven = 52%. Actual WR = 60.3% = solid edge. This is the sweet spot.

3. **60–64c YES: 71.4% WR but -$45.50 PnL**. Math check: 77 trades × 62c avg entry → win pays 38c, loss costs 62c. Breakeven WR = 62%. At 71.4% WR: expected 71.4% × 38c - 28.6% × 62c = 27.1c - 17.7c = +9.4c per contract. But total is -$45.50... This is a sizing issue. Large position counts at 62c entries where a single loss on 15–20 contracts = $9–12 loss, while a win on the same position = $5.70–7.60 gain. The wins are structurally smaller in dollar terms due to the payout cap.

4. **70c+ is barely break-even despite 71–78% WR** — high entry prices where losses are catastrophic (losing 70c+/contract) but wins are small (gaining <30c/contract).

---

## 8. Synthesis: Entry/Exit Framework

### When the FVG is Likely Real

Based on the data, the most reliable setups are:

1. **YES entry, 50–54c range, predicted_prob 50–57%**: This is the one profitable predicted_prob bucket (+651c for 50–55% prob) aligned with the profitable entry band. Small claimed edge, near-50 entry, slight directional lean.

2. **YES entry, 50–54c, TA MEDIUM tier (50–70% confidence)**: STRONG tier is associated with extreme entries outside the configured range. MEDIUM tier at mid-range likely represents genuine uncertainty where the edge is real.

3. **NO entry, 35–39c (YES at 61–65c)**: The only consistently profitable cheap-entry band. When the market is bullish (YES at 65c), buying NO at 35c has breakeven WR of 35% and achieves 32.2%. Edge is thin but positive.

### When the FVG is Likely Inverted

1. **cheap YES entries (<50c) against a strongly bearish orderbook**: When NO liquidity is 2× YES liquidity (like the current book), the market is enforcing a bearish price. Buying cheap YES is fighting a 2× size advantage.

2. **STRONG confidence TA entries at extreme prices (35c or 75c+)**: These correlate with the out-of-range entries that generate large losses. STRONG TA fires when the trend is extreme — precisely when a binary contract has most of its probability already resolved.

3. **Large FVG (≥10c) at cheap prices**: Despite 61.1% WR, these trades generate -$112 total PnL. The large claimed edge is a model artifact, not a market edge.

### Position Sizing Recommendations

The core problem: **at 25% balance per trade, a single losing trade on a full position (e.g., 20 contracts × 55c = $11 cost) represents a significant balance drawdown**. The 25% sizing was calibrated for a $200 balance (max $50 trade). At $78 current balance:
- 25% = $19.50 max position
- At 55c, that's ~35 contracts
- A loss wipes $19.50 (25% of balance)
- A win gains only ~$16 (45c payout)
- Recovery requires multiple wins to offset one loss

### Emergency Exit Signals (Inversion Early Warning)

The MFE data shows 55% of losing trades had MFE ≥ 8c before reversing. A potential leading indicator for inversion:

- **orderbook imbalance flip**: if the imbalance changes sign (was positive/bullish → becomes negative) after the position is entered, the FVG is inverting
- **microprice drift**: if microprice drops ≥5c from the HWM without a TP fill, the bid is retreating  
- **NO liquidity spike**: sudden increase in NO-side bid depth relative to YES signals market consensus shifting against the position

These are not currently logged or acted upon in the engine.

---

## 9. Implementation Notes

*These are research-derived suggestions only — do not implement without code review.*

### Priority 1: Investigate TA_FORCED Entry Range Violation

`limit_price` values of 60c, 72c, 75c, 82c, 90c are appearing under `strategy_name = 'TA_FORCED_SIGNAL'` despite `TA_FORCED_MAX_ENTRY_CENTS = 55`. Possible causes:
- The config check compares YES mid price, not the ladder fill price (bid-3c or bid-5c shifts)
- The `limit_price` column stores something other than the strict limit order price
- The config was different when these trades were placed

This needs a code audit at the TA_FORCED entry approval path in `polymarket_copy_engine.py`.

### Priority 2: Filter to Profitable Bands Only

The data suggests entering ONLY in:
- YES: 50–54c range (only profitable YES cheap band)
- NO: 35–39c range (only profitable NO cheap band)
- Both sides at 70c+ (break-even, high WR, but position size must be minimal)

All other bands are net-negative. The current 40–55c TA_FORCED range includes 40–49c (losses for both sides) and 50–55c (profitable for YES only).

### Priority 3: predicted_prob Usage

Only 46% of trades have predicted_prob logged. The 50–55% probability bucket is the only profitable range (+651c). Tightening the probability filter to require `predicted_prob` between 50–57% (with minimum confidence, not STRONG extreme) would filter out the majority of losing trades at extreme prices.

### Priority 4: Simultaneous Position Problem

The API settlement data shows both YES and NO counts in the same settlement record (e.g., 30 YES + 30 NO at 22:15). This indicates the engine holds opposing positions simultaneously at expiry. This is a structural loss multiplier — costs on both sides, revenue on only one. The flip-invert mechanism needs post-flip cleanup verification to confirm the original position is fully exited before the new one is entered.

### Priority 5: TP Strategy

Current TP (entry × 1.15) fills 15.7% of the time. Given settlement is the primary exit mechanism, consider:
- **Option A**: Remove TP entirely for hold-to-expiry positions — simplifies execution and reduces order management overhead
- **Option B**: Place TP much lower (entry × 1.05–1.08) to capture early favorable moves before inversion
- **Option C**: Replace TP with a bid-tracking exit — sell when `best_yes_bid > entry × 1.10` using a market sell, avoiding limit order non-fill risk

### Priority 6: Inversion Detection

Given 55% of losers show MFE ≥ 8c before reversing, a mid-session inversion detector based on:
1. bid dropped ≥5c from MFE (position currently tracked)
2. orderbook imbalance changed sign (needs to be tracked)
3. microprice declining below entry

...could exit half the current "inversion" trades early, saving ~5c per contract on 63 trades = meaningful PnL recovery.

---

## Data Notes

- Database: `btc-bias-engine/data/trades.db`, table `kalshi_trades`
- Total trades in DB: 1,956 (all-time)
- Excursion data available: 200 trades (recently added)
- `pnl` column is in **dollars** (not cents); multiply × 100 for cents
- `limit_price` column is in **cents** (e.g., 55 = 55¢)
- `reconciled_settled` status = position held to expiry, settled at 0c or 100c
- `reconciled_closed` status = position closed via external reconciliation (4 trades, avg +905c — likely TP fills during reconciliation)
- API pulls confirmed balance of $78.24 (live, no positions open at time of research)
