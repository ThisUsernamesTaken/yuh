# BTC Bias Engine — Comprehensive Trading Analysis
**Generated**: 2026-03-28
**Period**: 2026-03-13 → 2026-03-28 (15 days)
**Database**: `data/trades.db` (1,169 completed trades)

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total P&L | **-$9.96** |
| Peak Equity | $35.51 (Mar 19) |
| Current Drawdown | **-$45.47** from peak |
| Max Drawdown | -$68.17 |
| Total Trades | 1,169 |
| Overall Win Rate | 45.3% |
| Avg Win | +$0.697 |
| Avg Loss | -$0.592 |
| Win/Loss Ratio | 1.178 |

**Net assessment**: The engine is nearly breakeven on a P&L basis but sitting in a substantial drawdown. The core issue is not win rate — it's that the `CROSS_VENUE_FLOW` (PolymarketCopyEngine) strategy is buying the NO side at prices with a structurally skewed loss profile.

---

## 1. Daily P&L

| Date | Trades | WR% | Day P&L | Cum P&L |
|------|--------|-----|---------|---------|
| 2026-03-13 | 5 | 20.0% | -$0.96 | -$0.96 |
| 2026-03-14 | 12 | 33.3% | -$1.30 | -$2.26 |
| 2026-03-15 | 108 | 35.2% | -$6.77 | -$9.03 |
| 2026-03-16 | 29 | 44.8% | +$1.34 | -$7.69 |
| 2026-03-17 | 371 | 44.2% | +$0.35 | -$7.34 |
| 2026-03-18 | 64 | 31.2% | -$5.11 | -$12.45 |
| **2026-03-19** | **60** | **65.0%** | **+$31.06** | **+$18.60** |
| 2026-03-20 | 4 | 50.0% | +$1.05 | +$19.65 |
| 2026-03-21 | 12 | 58.3% | +$6.17 | +$25.82 |
| 2026-03-22 | 46 | 13.0% | -$2.63 | +$23.19 |
| 2026-03-23 | 99 | 52.5% | +$5.03 | +$28.22 |
| **2026-03-24** | **138** | **36.2%** | **-$21.99** | **+$6.23** |
| 2026-03-25 | 8 | 37.5% | -$1.87 | +$4.36 |
| **2026-03-26** | **99** | **42.4%** | **-$37.02** | **-$32.66** |
| 2026-03-27 | 80 | 73.8% | +$17.50 | -$15.16 |
| 2026-03-28* | 34 | 85.3% | +$5.20 | -$9.96 |

_*Mar 28 is partial day_

### Weekly Summary
| Week | Trades | WR% | P&L |
|------|--------|-----|-----|
| 2026-W11 (Mar 13–15) | 125 | 34.4% | -$9.03 |
| 2026-W12 (Mar 16–22) | 586 | 42.8% | +$32.22 |
| 2026-W13 (Mar 23–28) | 458 | 51.3% | -$33.15 |

**Key observation**: W12 was profitable even at 42.8% WR because win/loss sizing favored wins. W13 reversed this despite higher WR due to concentrated large losses on Mar 24 and 26.

---

## 2. Win Rate by Strategy

| Strategy | N | WR% | Avg Risk | Total P&L | P&L/Trade |
|----------|---|-----|----------|-----------|-----------|
| CROSS_VENUE_FLOW | 321 | 52.0% | $1.71 | **-$20.53** | -$0.064 |
| HFT_SCALP_STOP_LOSS | 250 | 36.8% | $0.44 | -$17.18 | -$0.069 |
| HFT_SCALP_TAKE_PROFIT | 183 | 53.6% | $0.44 | **+$17.37** | +$0.095 |
| TA_FORCED_SIGNAL | 67 | 49.3% | $0.56 | +$1.52 | +$0.023 |
| MIMIC_SMART_FLOW | 64 | 43.8% | $1.25 | **-$13.07** | -$0.204 |
| E-TRENDDN-LONDON | 47 | 34.0% | $0.29 | +$3.99 | +$0.085 |
| E-TRENDDN-ASIA | 40 | 40.0% | $0.37 | -$0.63 | -$0.016 |
| E-TRENDDN-NYOPEN | 32 | 46.9% | $0.50 | **+$13.13** | +$0.410 |
| TREND_FOLLOW | 17 | 70.6% | $0.96 | -$0.09 | -$0.005 |
| HFT_SCALP_TRAIL_STOP | 13 | 76.9% | $0.45 | +$4.13 | +$0.318 |
| E-TRENDUP-ASIA | 22 | 22.7% | $0.41 | -$2.94 | -$0.134 |
| E-UNKNOWN-ASIA | 11 | 9.1% | $0.44 | -$2.58 | -$0.235 |

**Critical observations**:
- `CROSS_VENUE_FLOW` has 52% WR but *loses money* — see Section 10 for root cause
- `HFT_SCALP_STOP_LOSS` losing $17.18 is expected (these are the losers); `HFT_SCALP_TAKE_PROFIT` +$17.37 is the paired winners — they roughly cancel
- `MIMIC_SMART_FLOW` is -$0.204/trade — worst risk-adjusted performer at scale
- `E-TRENDDN-NYOPEN` is the highest P&L/trade at +$0.410 with reasonable sample size
- `E-UNKNOWN-ASIA` at 9.1% WR is a broken signal — avoid

---

## 3. Win Rate by Entry Price Band

| Band | N | WR% | Total P&L | P&L/Trade |
|------|---|-----|-----------|-----------|
| <35c | 278 | 29.5% | **+$7.86** | +$0.028 |
| 35–39c | 116 | 31.9% | -$7.27 | -$0.063 |
| 40–44c | 134 | 41.0% | -$10.58 | -$0.079 |
| 45–49c | 175 | 48.0% | +$8.90 | +$0.051 |
| 50–54c | 177 | 50.8% | +$3.58 | +$0.020 |
| 55–59c | 123 | 49.6% | -$12.04 | -$0.098 |
| 60–64c | 39 | 53.8% | -$2.87 | -$0.074 |
| 65–69c | 28 | 60.7% | -$2.66 | -$0.095 |
| **70c+** | **99** | **82.8%** | **+$5.12** | **+$0.052** |

**Observations**:
- `<35c` at 29.5% WR is net positive because of favorable payout odds (risking small to win large). Mainly HFT scalps.
- `55–64c` band is bleeding money despite 50–54% WR — fees + tight payout ratio (-EV at those prices if WR is just at breakeven)
- `70c+` is +EV: 82.8% WR on 99 trades. These are high-conviction entries near expiry or after a strong move
- **The 40–44c band is the worst by P&L/trade** — likely ladder fills at unfavorable mid-market prices

### CROSS_VENUE_FLOW Price Band Breakdown

| Band | N | WR% | P&L | Notes |
|------|---|-----|-----|-------|
| <35c | 57 | **8.8%** | **-$26.79** | ⚠️ Catastrophic |
| 35–39c | 26 | 26.9% | +$0.56 | Borderline |
| 40–44c | 25 | 28.0% | -$9.94 | ⚠️ Losing |
| 45–49c | 32 | 56.2% | +$10.16 | ✅ Profitable |
| 50–54c | 28 | 71.4% | +$6.79 | ✅ Good |
| 55–59c | 38 | 44.7% | -$11.93 | ⚠️ Losing |
| 60–64c | 24 | 79.2% | +$3.74 | ✅ Good |
| 65–69c | 21 | 57.1% | -$3.75 | Negative despite decent WR |
| **70c+** | **70** | **88.6%** | **+$10.63** | ✅ Best band |

**The sweet spot for CVF is 45–54c and 60c+. CVF below 44c is actively destructive (-$36.73 combined).**

---

## 4. Hold Time Analysis (Exit Type Comparison)

| Exit Type | Count | WR% | Total P&L | Avg P&L |
|-----------|-------|-----|-----------|---------|
| Won (market settled YES) | 464 | — | — | — |
| Lost (market settled NO) | 569 | — | — | — |
| **Exited win** (manual) | **88** | **68.8%** (vs 128 total manual) | **+$34.24** | — |
| Exited loss (manual) | 40 | — | -$5.25 | -$0.13 |

**Settlement vs Manual exit breakdown**:
- **Settled trades**: 44.9% WR, **-$30.58** total
- **Manual exits**: 50.8% WR (88W/40L = 68.8% when you isolate wins), **+$34.24** total

**The manual exit logic is the only profitable component.** The stop/TP triggers are generating all the profit; holding to expiry is net negative. This is expected for a mean-reverting binary market.

### Settled Loss Distribution
- Min loss: $-0.00 (zero fills)
- Max loss: -$5.40 (10 contracts @ 54c)
- Mean settled loss: -$0.62
- Total settled losses: **-$354.51** across 569 positions

---

## 5. Stop Loss Effectiveness

All 40 `exited_loss` records are small (max -$0.36, mean -$0.13) — these are **time-based early exits** (the `<6min + loss >4c`, `<3.5min + loss >2c` logic), not -8c hard stops.

**The -8c hard stop is NOT triggering in the database.** Hard stops would show as `exited_loss` with ~-$0.08 × count in dollar terms. The max exited_loss is -$0.36 (very small). This means either:
1. The -8c stop is rarely hit before time exits take over
2. Hard stops are settling as market `lost` positions (the market expired against us before the stop could fire)

**Hypothesis**: Most big losses are expired market outcomes, not stop-triggered exits. The engine is expiring into losing positions rather than cutting them early. The time exit logic at `<6min` and `<3.5min` is working but only generating small saves on marginal positions.

---

## 6. Best/Worst Hours of Day (UTC)

### Best Hours (WR% > 60%)
| Hour (UTC) | Trades | WR% | P&L |
|-----------|--------|-----|-----|
| 02:00 | 27 | 77.8% | +$6.45 |
| 17:00 | 72 | 68.1% | +$15.29 |
| 15:00 | 30 | 56.7% | +$11.70 |
| 19:00 | 55 | 56.4% | +$7.24 |
| 18:00 | 43 | 55.8% | +$6.05 |
| 05:00 | 64 | 54.7% | +$7.15 |

### Worst Hours (WR% < 35% OR significantly negative P&L)
| Hour (UTC) | Trades | WR% | P&L |
|-----------|--------|-----|-----|
| 14:00 | 55 | 30.9% | **-$12.37** |
| 16:00 | 63 | 34.9% | -$8.95 |
| 08:00 | 52 | 30.8% | -$5.77 |
| 11:00 | 58 | 36.2% | -$5.80 |
| 04:00 | 102 | 37.3% | -$5.76 |
| 12:00 | 46 | 39.1% | -$6.76 |

**Session mapping (UTC)**:
- 02–05 UTC = Asia overnight (strong, +$13.60)
- 08–14 UTC = European session (weakest, -$28.99)
- 15–19 UTC = US session open (strongest, +$51.48)
- 20–23 UTC = US late/overnight (mixed, -$12.17)

**Recommendation**: The European session (08–14 UTC) is consistently negative. Consider a session filter that tightens entry criteria or pauses during 08:00–14:00 UTC.

---

## 7. Drawdown Analysis

```
Equity curve milestones:
  Start:           $0.00
  W11 low:        -$9.03  (Mar 15)
  W12 peak:      +$35.51  (Mar 21 area)
  W13 current:    -$9.96  (Mar 28)

Max drawdown:    -$68.17
Current DD:      -$45.47 from peak ($35.51)
DD/Peak ratio:    128% (current), 192% (max)
```

**The drawdown is severe relative to peak gains.** The engine reached +$35 peak then gave back nearly $45. This is classic over-trading behavior where win/loss sizing is inconsistent enough that a few large concentrated losses (Mar 24: -$22, Mar 26: -$37) overwhelm weeks of small gains.

### Worst Single-Session Losses
- **Mar 26**: -$37.02 (99 trades, 42.4% WR) — CVF NO entries at high prices on a strong directional day
- **Mar 24**: -$21.99 (138 trades, 36.2% WR) — Similar pattern, European session heavy
- **Mar 18**: -$5.11 (64 trades, 31.2% WR)

### Consecutive Loss Streaks
- **Max: 39 consecutive losing trades** (likely during the Mar 26 session)

---

## 8. Position Sizing Analysis

| Contracts | Trades | WR% | Total P&L |
|-----------|--------|-----|-----------|
| 1 | 851 | 42.4% | -$14.99 |
| 2 | 132 | 47.0% | +$1.16 |
| 3 | 70 | 54.3% | +$18.58 |
| 4 | 43 | 65.1% | +$3.88 |
| 5 | 25 | 48.0% | -$14.06 |
| 6 | 18 | 61.1% | +$4.44 |
| 10+ | 28 | varied | varies |

**Key finding**: Trades sized at 3–4 contracts have the best risk-adjusted performance (54–65% WR, positive P&L). Single-contract trades (72.8% of all trades) are net negative at -$14.99.

**Why single contracts underperform**:
- `dollar_risk` ranges $0.03–$0.44 for single contracts
- Small sizing means fees represent a higher % of potential profit
- These are mostly HFT scalp entries and legacy signals at sub-optimal prices

**Sizing range**: $0.03–$11.60 per trade, mean $0.83. The 50% balance fraction cap rarely triggers (only 3 trades ≥$10, all profitable).

**Large trades ($10+)**: Only 3 trades, all winners (+$3.25 combined). The position sizing constraint is being too conservative — when the engine has high conviction at 45–64c with 3–6 contracts, returns improve markedly.

---

## 9. Signal-to-Trade Conversion

| Status | Count |
|--------|-------|
| Lost (expired) | 569 |
| Won (expired) | 464 |
| Exited win (manual) | 88 |
| Exited loss (manual) | 40 |
| Reconciled unknown | 8 |
| **Unfilled** | **7** |

- **Unfill rate**: 7/1,176 = **0.6%** — excellent ladder execution
- **Legacy signals (consensus engine)**: 9,588 signals logged — irrelevant, that system is disabled
- **PolymarketCopyEngine signal → trade conversion**: Not directly logged in signals.db (those are legacy consensus signals). CVF fires trades directly from Poly wallet flow events.

---

## 10. Patterns in Losing Trades

### Root Cause: CVF NO Side Asymmetric Loss Profile

This is the **primary finding** of this analysis:

| | CVF YES | CVF NO |
|--|---------|--------|
| N | 207 | 114 |
| WR% | 49.3% | 57.0% |
| Avg Win | +$0.924 | +$0.879 |
| **Avg Loss** | **-$0.818** | **-$1.754** |
| Total P&L | **+$8.29** | **-$28.82** |

**CVF NO losses average 2.1× larger than CVF NO wins.** Despite a 57% win rate, the loss magnitude destroys edge. CVF YES is nearly breakeven (avg win slightly > avg loss).

**Why CVF NO losses are larger**: When buying NO at 30–45c (implying YES > 55–70c), a losing trade means the market expired YES — the full dollar_risk is lost. But wins return only 55–70c per dollar. The engine is not applying the floor restriction (`MIN_ENTRY_CENTS = 35` for NO) aggressively enough at the whale-flow prices.

**CVF NO at <35c**: 12 trades, **0% WR**, -$10.98 — absolute zero edge. These should never happen per config.

### Hourly Loss Concentration
Worst hours for CVF specifically:
- **04 UTC**: 48 trades, **29.2% WR**, -$7.92
- **08–09 UTC**: ~12 trades, ~25% WR, -$8.21 combined
- **11–12 UTC**: 44 trades, ~30% WR, -$13.63 combined

### Strategy × Side Problem Matrix

| Strategy | Side | N | WR% | P&L | Flag |
|----------|------|---|-----|-----|------|
| CROSS_VENUE_FLOW | NO | 114 | 57.0% | -$28.82 | ⚠️ Avg loss 2× avg win |
| HFT_SCALP_STOP_LOSS | NO | 161 | 32.9% | -$18.79 | ⚠️ Expected (stop exits) |
| MIMIC_SMART_FLOW | YES | 39 | 38.5% | -$7.39 | ⚠️ Low WR |
| E-TRENDUP-ASIA | YES | 20 | 25.0% | -$2.32 | ⚠️ Below breakeven |
| E-UNKNOWN-ASIA | NO | 8 | 0.0% | -$2.54 | 🚫 Zero edge |

---

## Summary: Key Actionable Findings

### Critical Fixes

1. **CVF NO entries below 44c must be blocked** — 82 trades below 44c NO side are losing $36.73 total (avg -$0.45/trade). The config has `MIN_ENTRY_CENTS = 35` but this isn't filtering CVF NO entries at <35c (12 trades, 0% WR, -$10.98).

2. **CVF NO avg loss > avg win is the #1 P&L killer** — 57% WR but -$28.82. Either raise NO floor to 45c (where it becomes +$6.11) or require 65%+ WR before entering NO.

3. **European session (08–14 UTC) filter needed** — -$28.99 P&L across 291 trades (41.6% WR). CVF at 12 UTC is -$12.70 alone. This is likely low-liquidity / high-spread period on Polymarket causing wallet signals to be noise.

4. **MIMIC_SMART_FLOW is -$13.07** (-$0.204/trade) — this strategy should be either disabled or require a higher conviction threshold.

### What's Working

5. **Manual exits generate all the profit** (+$34.24) — the TP/trailing stop/time exit logic is correctly cutting winners early and letting losers expire. Don't change this.

6. **3–6 contract sizing outperforms 1-contract** — WR of 54–65% vs 42% for singles. The engine should size up when CVF confidence is high.

7. **CVF YES side is viable** — 49.3% WR but +$8.29 total because avg win ($0.924) > avg loss ($0.818). Keep YES entries.

8. **70c+ entries are +EV** (82.8% WR, +$5.12, 99 trades) — these late-window/high-price entries are strong. The `MAX_ENTRY_CENTS = 64` config is blocking the best-performing price band. Raising the cap to 90c would capture more of these.

9. **Recent performance (Mar 27–28): 73.8% and 85.3% WR** — the engine appears to be recovering. The latest sessions are the strongest in the dataset.

---

## Proposed Config Changes

```python
# Current → Proposed
MIN_ENTRY_CENTS = 35          → 45    # Block sub-45c which bleeds money
MAX_ENTRY_CENTS = 64          → 90    # Capture 70c+ band (82.8% WR!)
# Add: NO_MIN_ENTRY_CENTS = 48        # NO side needs higher floor per asymmetry
# Add: SESSION_BLOCK_START = 8        # Block 08–14 UTC European session
# Add: SESSION_BLOCK_END = 14         # or tighten thresholds there
# MIMIC_SMART_FLOW: disable or require 65%+ wallet WR
```

---

_Analysis generated from `data/trades.db` (1,169 completed trades, 2026-03-13 to 2026-03-28)_
