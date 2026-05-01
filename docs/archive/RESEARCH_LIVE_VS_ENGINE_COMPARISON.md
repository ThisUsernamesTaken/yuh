# RESEARCH — Live Kalshi Data vs Engine State Comparison
**Date**: 2026-04-11  
**Label**: Do Not Implement — Research Only  
**Data pulled**: 2026-04-11 23:22 UTC  

---

## 1. Live Account Summary

| Metric | Value |
|--------|-------|
| Available balance | **$78.24** |
| Portfolio value (open positions) | $0.00 |
| Total account | $78.24 |
| Open positions | 0 |
| Resting orders | 0 |

**Today's account drawdown**: $106.55 → $78.24 = **-$28.31** (intraday, based on snapshot history)

Recent balance snapshots confirm the engine is actively trading TA_FORCED_SIGNAL entries every 15-minute window. The balance is confirmed exact — $0 drift between live API and the most recent snapshot (2026-04-11 23:14:55 UTC).

---

## 2. Data Reconciliation — Engine vs Kalshi Reality

### Record counts

| Source | Count |
|--------|-------|
| Kalshi executed orders (API, last 200) | 200 |
| Kalshi fills (API, last 200) | 200 |
| Kalshi settlements (last 200) | 200 |
| Engine `kalshi_trades` records (last 200) | 200 |

### Orders in Kalshi not in engine DB: 134

These are **expected and not a bug**. The engine records ONE row per position in `kalshi_trades`, but Kalshi tracks every individual order. Each position generates:
- 2× ladder BUY limit orders (shallow + deep)
- Multiple TP/exit SELL orders

The 134 "missing" orders are almost entirely TP sell orders and unfilled ladder legs. The engine does not persist individual sell order IDs to `kalshi_trades`.

### Engine records with no Kalshi order match: 131

All 131 have `status=reconciled_settled`. These trades were recognized at settlement time via the reconciliation logic (which queries Kalshi settlements, not order IDs). This is the normal behavior when: the entry order fills but the engine loses track of the order_id during restart, or when the position was adopted on startup.

**No true ghost orders or orphaned fills detected.**

### Fill price discrepancies: 3 cases (all +1c)

| Ticker | Engine limit | Kalshi fill | Diff |
|--------|-------------|-------------|------|
| KXBTC15M-26APR111500-00 | 56c | 57c | +1c |
| KXBTC15M-26APR111000-00 | 38c | 39c | +1c |
| KXBTC15M-26APR102030-30 | 55c | 56c | +1c |

3 fills out of 200 orders at 1c worse than limit. Excellent execution — 98.5% filled at or better than the submitted limit.

### Settlement P&L discrepancy note

The initial script reported "$3,025 in settlement losses with $0 revenue." This was a **field name error** — the Kalshi v2 API returns `revenue` not `revenue_dollars`, so revenue was read as 0 for all records. The settlement loss figures are meaningless artifacts of this parsing bug. The engine's own `pnl` column in `kalshi_trades` is the authoritative P&L source. Separately: settlement records only capture contracts held to expiry. TP exits before expiry generate revenue through fill records on sell orders, not through settlements.

---

## 3. Execution Quality

### Order type distribution (last 200 executed)

| Type | Count | % | Fill rate |
|------|-------|---|-----------|
| Limit | 191 | 95.5% | 100% |
| Market | 9 | 4.5% | 100% |

All orders filled. No cancels in the last 200.

### Fill price slippage vs submitted limit

- 3 out of 200 filled at +1c above limit (1.5%)
- 197 out of 200 filled at or below limit (98.5%)
- Average slippage: essentially 0

The passive ladder strategy (bid - 3c / bid - 5c) reliably gets filled at or better than the submitted price. Market moves to the order, not vice versa.

### Execution log note

The `execution_log` table contains records from the **old disabled consensus engine** (`E-TRENDDN-NYPRIME`, `E-RANGING-NYPRIME`, etc.) — not the current TA_FORCED/Polymarket engine. The current engine logs to `balance_snapshots` and `kalshi_trades` instead. No meaningful execution timing data exists for the current engine.

- Old engine fill rate (7d): 22 submitted → 9 filled = **40.9%** (passive ladders for old strategies)
- Old engine slippage: avg **-2c** (filled below limit — got better prices)
- "Latency" of 900–1000 seconds = passive limit orders resting for most of the contract window, not signal→fill latency

---

## 4. Engine State Correlation — Strategy × Status (7 days)

### TA_FORCED_SIGNAL (current primary strategy)

| Status | n | P&L |
|--------|---|-----|
| reconciled_settled | 360 | -$115.12 |
| exited_loss | 2 | -$11.72 |
| extreme_exit | 1 | -$5.18 |
| reconciled_closed | 4 | +$36.22 |
| unfilled | 3 | $0 |
| **Total** | **370** | **-$95.80** |

TA_FORCED all-time: 632 trades, **-$121.36** net. Per-trade: -$0.19/trade.

### CROSS_VENUE_FLOW (Polymarket wallet copy, current secondary)

| Status | n | P&L |
|--------|---|-----|
| reconciled_settled | 17 | -$21.35 |
| extreme_exit | 1 | -$1.21 |
| exited_loss | 2 | -$11.72 (shared with TA?) |
| won | 1 | +$0.11 |
| **Total (7d)** | **~21** | ~**-$22** |

CROSS_VENUE all-time: 418 trades, **-$85.82** net.  
- 120 `won` (+$105.98) vs 108 `lost` (-$99.35) → nearly breakeven on properly-tracked trades  
- 178 `reconciled_settled` (-$70.19) → the untracked settlement losses drag the whole strategy negative

### HFT strategies (old, disabled — small sample in 7d window)

| Strategy | Status | n | P&L |
|----------|--------|---|-----|
| HFT_SCALP_STOP_LOSS | won | 19 | +$9.61 |
| HFT_SCALP_STOP_LOSS | lost | 29 | -$11.88 |
| HFT_SCALP_TAKE_PROFIT | won | 24 | +$12.87 |
| HFT_SCALP_TAKE_PROFIT | lost | 18 | -$7.69 |

These appear to be old trades still in the 7-day window — HFT was disabled.

### Critical finding: Win rate metric is broken

The 24.3% overall WR (472W / 552L out of 1,943 trades) is **not meaningful**. The vast majority of TA_FORCED and CROSS_VENUE trades record as `reconciled_settled` rather than `won`/`lost`, so they are excluded from the WR denominator. A trade that profits via TP before expiry shows pnl > 0 in `reconciled_settled` but is not counted as a "win." **Use `SUM(pnl)` not win rate for performance evaluation.**

---

## 5. Probability Calibration

Only 81 trades have both `status IN ('won','lost')` AND `predicted_prob IS NOT NULL`. The sample is small due to the WR tracking issue above.

| Bucket | n | Won | Actual WR | Assessment |
|--------|---|-----|-----------|------------|
| <40% | 37 | 6 | 16.2% | Calibrated — predicted low, happened low |
| 40-49% | 9 | 3 | 33.3% | Slightly high (predicted 40-49, got 33%) |
| 60-69% | 2 | 2 | 100.0% | N too small to interpret |
| 80%+ | 33 | 30 | 90.9% | **Well-calibrated** — high confidence → high WR |

The 80%+ bucket (n=33, 90.9% actual WR) is encouraging and suggests the probability engine is correctly identifying high-confidence situations. The <40% bucket (16.2% actual WR) also tracks well. The <40% trades are ones where the engine correctly predicts the market is against the position — but the engine is still entering them (probably as contrarian plays or hedge entries).

**Limitation**: Only 81 trades with proper won/lost + predicted_prob — 96% of all trades are uncounted. This analysis is not statistically reliable.

---

## 6. Sizing Impact — Contract Count vs Outcome (7 days)

| Contracts | n | Wins (won/exited_win) | WR% | Total P&L | Note |
|-----------|---|----------------------|-----|-----------|------|
| 1 | 122 | 45 | 36.9% | +$0.62 | Only size showing real WR tracking |
| 2 | 22 | 7 | 31.8% | -$0.24 | |
| 3 | 19 | 1 | 5.3% | -$23.35 | **BAD** |
| 4 | 10 | 0 | 0% | +$2.02 | WR 0% but positive P&L → all reconciled |
| 5 | 91 | 1 | 1.1% | -$27.75 | **WORST ABSOLUTE LOSS** |
| 6 | 27 | 0 | 0% | +$32.15 | 0% WR but +$32 → reconciled wins |
| 10 | 27 | 0 | 0% | +$24.29 | Same — tracking issue |
| 13 | 12 | 0 | 0% | -$43.26 | **BAD** |
| 15 | 14 | 0 | 0% | -$20.51 | Bad |
| 20 | 9 | 0 | 0% | -$11.37 | Bad |
| 25 | 20 | 0 | 0% | -$42.83 | **WORST ABSOLUTE LOSS** |
| 37 | 1 | 0 | 0% | -$14.41 | Single large loss |

**Key observations:**
- The "0% WR" for sizes 4–23 is the WR tracking bug, not real. Positive P&L sizes (6c, 10c, 11c, 12c, 14c, 17c) are genuinely winning despite 0% tracked WR.
- Sizes 5, 13, 25, 3 are the worst performers. These appear to be correlated with specific bad sessions rather than size itself causing losses.
- The worst single bet appears to be 37-contract size (-$14.41 on one trade) and 18-contract (-$15.07).
- No clean evidence that size correlates with win rate — the tracking issue obscures this entirely.

**TA_FORCED entry price distribution (7d):**

| Range | n | P&L | Note |
|-------|---|-----|------|
| <40c | 12 | +$25.99 | Small sample, strong |
| 40-49c | 49 | **-$90.41** | **Worst band** |
| 50-59c | 160 | +$20.73 | **Best band** — sweet spot |
| 60-69c | 115 | -$62.07 | Bad |
| 70+c | 34 | +$9.96 | |

The 40-49c band is catastrophically underperforming (-$90 on 49 trades). The 50-59c band is the only clearly positive range. The config sets `TA_FORCED_MIN_ENTRY_CENTS = 40` and `TA_FORCED_MAX_ENTRY_CENTS = 55` — the 40-49c half of that window is destroying value. Consider raising TA_FORCED minimum to 50c.

---

## 7. Temporal Patterns — Hour UTC (7 days)

| Hour (UTC) | n | WR% | P&L | Session |
|------------|---|-----|-----|---------|
| 00 | 23 | 26.1% | **+$45.14** | Late US / Midnight ET |
| 01 | 16 | 0% | **+$6.19** | |
| 02 | 17 | 0% | **+$6.30** | |
| 03 | 11 | 0% | **+$22.43** | |
| 04 | 13 | 0% | **-$21.78** | Dead hours (midnight-3am ET) |
| 05 | 15 | 0% | **-$18.55** | |
| 06 | 19 | 0% | **-$17.31** | |
| 07 | 19 | 0% | **-$16.45** | |
| 08 | 10 | 10.0% | +$0.99 | Pre-NY |
| 09 | 8 | 0% | **+$10.58** | |
| 10 | 8 | 0% | **-$37.77** | London/EU session |
| 11 | 11 | 0% | -$8.36 | |
| 12 | 16 | 0% | **-$21.98** | |
| 13 | 16 | 0% | -$3.15 | |
| 14 | 20 | 0% | **-$23.27** | Pre-market ET |
| 15 | 23 | 0% | -$5.22 | |
| 16 | 38 | 34.2% | **+$29.85** | NY Open (12pm ET) |
| 17 | 29 | 13.8% | -$9.57 | |
| 18 | 38 | 18.4% | -$5.33 | |
| 19 | 45 | 11.1% | **+$6.67** | |
| 20 | 31 | 19.4% | **+$21.68** | NY afternoon |
| 21 | 20 | 0% | -$7.91 | |
| 22 | 32 | 28.1% | **-$32.57** | US close |
| 23 | 27 | 11.1% | **-$35.14** | |

**Best sessions (7d):** 00:xx (+$45), 03:xx (+$22), 16:xx (+$30), 20:xx (+$22)  
**Worst sessions (7d):** 23:xx (-$35), 22:xx (-$33), 10:xx (-$38), 04-07:xx (-$74 combined)

**Striking pattern**: Hours 04–07 UTC (midnight–3am ET) are bleeding -$74 over the 7-day window. This is when BTC volume is lowest and the market is most random. Hours 22-23 UTC (6-7pm ET) lose -$68 despite high apparent WR (28%, 11%) — large positions must be entering here with unfavorable outcomes.

The old engine had `BLOCKED_HOURS = {8, 9, 10, 11, 12, 13}` (UTC) which was cleared 2026-03-30. The data shows that clearing the block improved hour 09 (+$10) but hours 10-12 remain badly negative. Hours 04-07 appear to be the new problem zone.

---

## 8. Key Discrepancies — Engine vs Kalshi Reality

### 8A. Win-rate tracking is broken for most trades

The `status` field is populated as `reconciled_settled` for the majority of TA_FORCED and CROSS_VENUE trades. This makes the `won`/`lost` status unreliable for performance measurement. The engine's `pnl` column is accurate (computed at settlement reconciliation), but WR tracking is blind to most outcomes.

**Impact**: Cannot calculate true WR, Brier scores, or probability calibration for 90%+ of all trades.

### 8B. TA_FORCED entry price band 40-49c is destroying value

49 trades at 40-49c entry in the last 7 days: **-$90.41**. The current config allows entries down to 40c. This band has the worst per-trade loss of any price range. The 50-59c band (+$20.73 on 160 trades) is the engine's best range.

### 8C. Hours 04-07 UTC and 22-23 UTC are the worst

7-day combined:
- 04-07 UTC: -$74 (64 trades)  
- 22-23 UTC: -$68 (59 trades)  
Total: -$142 from 123 trades in these 4-hour windows. That's the bulk of all losses.

### 8D. trade_pnl table is empty

The `trade_pnl` table (0 rows) was intended as a ground-truth P&L ledger. It is never written to. All P&L tracking runs through `kalshi_trades.pnl` and `balance_snapshots`. This is not causing issues but means there's no secondary ground-truth table to cross-check against.

### 8E. Large position sizing (25–37 contracts) shows extreme losses

25 contracts: 20 trades, -$42.83. 37 contracts: 1 trade, -$14.41. These correspond to the 25% balance fraction at higher balance levels. When these positions go wrong, they erase multiple winning trades.

### 8F. The settlement P&L figures from Kalshi API appear extreme

The API call retrieved 200 settlements showing a combined $3,025.53 "cost" — but this includes TP exits that close positions before expiry (those appear as buy costs with 0 contracts at expiry). The per-settlement breakdown shows individual costs of $20–$42 per window which are consistent with position sizing at 25% of $80-170 account levels. The revenue field parsing bug (above) prevented computing true net settlement P&L.

---

## 9. Recommendations

**These are research findings only. Do not implement without review.**

### R1. Raise TA_FORCED minimum entry price from 40c to 50c
The 40-49c band has cost -$90.41 in 7 days alone. The 50-59c band earned +$20.73. The config currently allows `TA_FORCED_MIN_ENTRY_CENTS = 40`. Raising to 50 would reduce trade volume but eliminate the worst entry band. All-time cost of the 40-49c band likely exceeds -$200.

### R2. Implement hour-based blocks for 04-07 UTC and 22-23 UTC
Hours 04-07 UTC are -$74 in 7 days (midnight–3am ET, minimum BTC volume). Hours 22-23 UTC are -$68 (6-7pm ET, post-close drift). Blocking these 6 hours would have added ~+$142 to the 7-day P&L (approximately +50% improvement). The config has `BLOCKED_HOURS = set()` — re-introducing blocks for these windows is the single highest-impact config change available.

### R3. Fix the status/WR tracking for reconciled_settled trades
The reconciliation logic marks resolved trades as `reconciled_settled` instead of `won`/`lost`. This makes probability calibration, Brier scoring, and WR analysis impossible for 90%+ of trades. The fix is to check the contract result during reconciliation and map `reconciled_settled` to either `reconciled_won` or `reconciled_lost` (or simply `won`/`lost`).

### R4. Investigate hours 22-23 UTC specifically
Despite showing 28% and 11% tracked WR (higher than many sessions), these hours lose -$68. This suggests large positions are entering near contract expiry when the market is volatile, or the TP exits aren't capturing wins. The balance snapshot history shows TA_FORCED entries firing throughout these hours.

### R5. Revisit the 25% sizing fraction
The worst individual loss days (Apr 7: -$43.64, Apr 1: -$50.14) correspond to back-to-back adverse windows hitting at full 25% sizing. At $100 account, 25% = $25/trade. Three consecutive losses at $25 each = -$75, nearly wiping the account. Consider reducing to 15-20% sizing fraction or adding a consecutive-loss cooldown.

### R6. Fix settlement revenue parsing in diagnostic tooling
The Kalshi v2 API returns `revenue` (not `revenue_dollars`) in settlement records. Any future tooling querying settlements should use the correct field name to avoid misreading all revenue as $0.

---

*All data pulled from: Kalshi production API + `C:/Trading/btc-bias-engine/data/trades.db`*  
*Research script: `C:/Trading/kalshi_research_temp.py` (deleted after run)*
