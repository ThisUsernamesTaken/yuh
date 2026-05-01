# Paper Engine Findings — 2026-04-14

Sample: 134 sessions over 72h with full settlement data.
Method: replays each session's signals through configurable strategies with realistic fill modeling.

---

## What's been deployed to live (data-validated)

Removed three filters that the paper engine showed COST us $26 of $33 potential P&L:

| Filter | Skipped | Would-have WR | Would-have $ |
|---|---|---|---|
| `lag_against` | 20 | 75% | +$13.47 |
| `low_vol` (vol < 25%) | 9 | 78% | +$5.40 |
| `vel_dead_zone` | 8 | 62% | +$4.96 |

Also lowered FVG threshold from 12c → 8c (paper showed 132 entries vs 81 with similar quality, $81 vs $54 total).

---

## Findings PENDING confirmation (sample too thin to act on)

### 1. Time-of-day is the strongest single signal

**Threshold for action**: need 200+ sessions per hour bucket (currently 4-8).

```
GREAT (≥80% WR):  06 (88%) | 14 (86%) | 15 (83%) | 17 (88%)
GOOD (60-80%):    00, 01, 04, 05, 07, 08, 09, 13, 18, 23
MEH  (50-60%):    02, 03, 11, 12, 19, 20
BAD  (<60%):      10 (50%, $0.21/tr) | 16 (40%, $0.47/tr) | 21 (50%, -$0.07/tr)
```

**Proposed filter when sample sufficient**:
```python
WORST_UTC_HOURS = {10, 11, 12, 16, 19, 20, 21}
if datetime.utcnow().hour in WORST_UTC_HOURS:
    skip
```

Paper projection at current sample:
- 91 entries (vs 133), 73% WR (vs 66%), $0.81/trade (vs $0.62)
- Total slightly lower ($74 vs $82) but quality up

### 2. The high-velocity-loser pattern

Loser profile (45 losses vs 88 wins):
- BTC velocity at entry: **losers avg +15.3/s, winners avg +4.2/s**
- 4x difference — clearest single discriminator

Hypothesis: late entries (high velocity = move already done) lose more. We're chasing.

**Threshold for action**: need 50+ losing trades to confirm the velocity-distribution pattern.

**Velocity ceiling experiment** (vel > N skips entry):
```
vel ≤ 20/s:  120 trades, $0.634/tr, $76 total
vel ≤ 15/s:  116 trades, $0.650/tr, $75 total  ← marginal benefit
vel ≤ 10/s:  116 trades, $0.650/tr, $75 total
vel ≤ 5/s:   114 trades, $0.622/tr, $71 total  (too aggressive)
```

**Conclusion**: marginal effect on its own. Maybe combine with time-of-day later.

---

## Findings REJECTED by data

These ideas seemed plausible but the data showed no benefit or active harm:

### Pressure must AGREE (vs current veto-only)
- Adding "pressure direction must equal trade direction" filter: $82 → $70 total
- Reduces entries from 133 → 117, doesn't lift WR enough
- **Don't deploy**

### Spread quality filter
- Kalshi spreads are consistently 2c on these contracts
- All thresholds tested produced identical results
- **No-op signal**

### High probability filter (require prob ≥ N)
- Trades fewer entries with slightly higher $/trade
- Total P&L decreases as threshold rises
- Volume sacrifice doesn't pay off
- **No deploy**

### "Single strong signal" paradox
- Earlier 86% WR finding on 1/4 indicators aligned: **didn't replicate**
- Fresh paper sample: only 13 entries, 54% WR
- Was likely sample-size noise
- **Don't deploy**

---

## Methodology notes

### Fill realism (FillSimulator)
- Top-of-book depth assumption: 20 contracts at best bid/ask
- Slippage beyond: 1c per 25 contracts
- Market sell extra slippage: +0.5c per 25
- Limit TP: fills if bid reaches/exceeds price during session

### Entry pricing (current paper assumption)
- Uses logged ASK at signal time
- For dip-timing variant: 40% chance of -2c improvement (probabilistic model)
- **REAL dip-timing not yet validated** — historical "60% BID MISS" warning in code

### Stop modeling
- Assumes -5c stop fires when settlement opposes our side
- Real-world: cooldown stop with 10s window may delay or skip some stops

### Limitations
- Counterfactual outcomes assume thesis-anchored TP exits
- Doesn't model mid-session bid trajectory (only entry, settlement, MFE/MAE)
- 134 sessions is small for hour-level statistics

---

## Re-validation schedule

When live engine accumulates 300+ sessions (estimated 5-7 days at current pace):

1. Re-run `paper_engine.experiments` with fresh data
2. Verify time-of-day pattern persists
3. Verify velocity-loser pattern persists
4. Decide on time-of-day filter implementation

If patterns hold at 300 samples, deploy time-of-day filter. If they collapse, they were noise.
