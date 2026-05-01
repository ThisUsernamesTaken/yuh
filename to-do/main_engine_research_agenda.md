# Main Engine Research Agenda
# 2026-03-17 — Post-Analysis After Sessions 10–11

This document synthesises the research brief (probability-economics framing), the live data
analysis from the execution_log and kalshi_trades DB, and direct code inspection of
signal_intelligence.py, consensus.py, and regime_detector. Each section leads with
what the data shows, then what to test, then what to change if the test confirms it.

---

## Operating Reality

The main engine (E-* strategies, execution_log pathway) has produced 16 fills across 4 days
against 770 signal evaluations — a 2% fill rate. Every one of those fills lost except one.
This means the engine is:
- Rarely trading (correct to be selective, wrong about what to select)
- Highly confident when it does trade (60–83% adjusted_confidence)
- Systematically wrong (15/16 fills = losses, all same-side, all same regime)

HFT is carrying the P&L. Main engine is a net drag.

---

## Problem 1 — The Model Is Wildly Disconnected From Market Prices

### What the data shows

Every main engine fill has a large gap between model confidence and market-implied probability:

| Fill | Side | Model Conf | Market Price | Gap |
|------|------|-----------|--------------|-----|
| YES  | yes  | 82.6%     | 7¢ ask       | +75.6¢ |
| YES  | yes  | 66.2%     | 9¢ ask       | +57.2¢ |
| YES  | yes  | 79.4%     | 40¢ ask      | +39.4¢ |
| YES  | yes  | 60.1%     | 32¢ ask      | +28.1¢ |
| NO   | no   | 83.5%     | 41¢ implied  | +42.5¢ |
| NO   | no   | 82.4%     | 46¢ implied  | +36.4¢ |
| NO   | no   | 67.4%     | 42¢ implied  | +25.4¢ |

The market said 7–9% probability; the model said 82–66%. The market was right. This is not a
close call — it is consistent, large, and directionally wrong.

In a liquid prediction market populated by competing forecasters, the market price is a
reasonable prior. Diverging from it by 40–75 percentage points without significant private
information is not alpha — it is model error.

### Root cause hypothesis

The `adjusted_confidence` is derived from `bull_conf - bear_conf` (Fourier-weighted EMA
differences), then floored to the `_REGIME_SESSION_WR` table value (59–67%). This output
is never compared to the market price. The engine asks "does our signal say UP?" — it never
asks "does the market already price this in at 90¢, making our UP signal valueless?"

### Research test

For every evaluation row in execution_log:
- Compute `model_edge = adjusted_confidence / 100 - (best_ask / 100)` for YES,
  or `adjusted_confidence / 100 - ((100 - best_bid) / 100)` for NO
- Plot distribution of `model_edge` for submitted vs. not-submitted rows
- Expected finding: submitted rows cluster at the largest model_edge values (engine picks
  the trades where it most disagrees with the market)
- If model_edge > 0.20 predicts losses consistently, the model's "strong conviction"
  is anti-correlated with realized outcomes — it is confidently wrong precisely where
  it is most certain

### Implementation

Add `market_implied_prob` to execution_log. Gate entry on:
```
model_implied_prob = adjusted_confidence / 100
market_implied_prob = (best_ask / 100) for YES, or ((100 - best_bid) / 100) for NO
market_adjusted_edge = model_implied_prob - market_implied_prob - fee_rate * p * (1-p)
```
Only submit when `market_adjusted_edge > 0` with a minimum threshold.
Use market price as the baseline probability. Treat the model as a residual corrector.

---

## Problem 2 — Regime Detector Labels BTC Uptrend as TRENDING_DOWN

### What the data shows

Live fills by regime and outcome (today):
- 10 NO fills labeled TRENDING_DOWN: 9 lost (expiry=YES, BTC went UP)
- 4 YES fills labeled TRENDING_UP: 4 lost (entry at 7–31¢, deep OTM)
- 1 NO fill labeled TRENDING_DOWN: won (BTC went down)

Regime distribution from execution_log during today's trading session (UTC 3–14h):
- TRENDING_DOWN dominated the 3–11h window: 35, 30, 17, 16 approved signals
- During this same window BTC was in a sustained uptrend (confirmed by YES contracts
  resolving at 100 for every NO bet)

The regime is NOT random noise. It is confidently, persistently wrong about direction
during a sustained BTC uptrend.

### Root cause hypotheses

**Hypothesis A: Sign inversion in regime mapping to trade side.**
If TRENDING_DOWN means the BiasEngine score is negative (bearish Fourier), and the
engine maps `direction=PUT → side=no`, then the engine bets NO when it sees a downtrend.
If that downtrend classification is lagging real price by one full cycle, every NO bet
fires at the bottom of a dip, just as BTC reverses up.

**Hypothesis B: 1h TF EMAs are lagging by 30–60 minutes.**
The warm-start fix (250 candles) went live today. Before that, the 1h BiasEngine cold-started
on 1 bar of data and produced a large negative score that persisted. Even after the fix,
an EMA(5) on hourly bars needs 5 hours to stabilize. During warm-up, the 1h score can be
inverted relative to actual current trend.

**Hypothesis C: Regime detector ATR threshold miscalibrated.**
If the ATR-based TRENDING_DOWN threshold fires too easily, many neutral or slowly-reverting
periods are labeled as downtrends.

### Research tests

**Test A — Direction congruence:**
For each filled trade:
- Record BTC spot direction over the 15-minute window before fill (e.g., `cycle_return_pct`)
- Record regime label
- Compute: `congruent = (regime=TRENDING_DOWN AND btc_return<0) OR (regime=TRENDING_UP AND btc_return>0)`
- If congruent < 50%, the regime detector is a coin flip or inverted

**Test B — Lag measurement:**
- Collect `(regime_label, timestamp)` and align with BTC spot candle data
- Compute: how many minutes earlier did BTC price change direction vs when regime label changed?
- If lag > 5 minutes on average, regime is a lagging indicator for the 15-minute contract horizon

**Test C — Regime accuracy backtest:**
Use `signals.db` historical signal data if available. For every regime label, look at the
actual BTC close direction in the following 15 minutes. A regime detector with edge should
show >55% accuracy.

### Implementation paths

If Test A confirms inversion: check the sign of `fourier_score` mapping to `direction` in
`consensus.py` and the downstream mapping from `direction` to trade `side` in `main.py`.
One sign error anywhere in that chain inverts all trades.

If Test B confirms lag: implement a fast-signal override — if 1m and 3m TFs have a strong
opposing signal (aligned_count ≥ 2 in opposite direction), suppress the 1h-dominated regime
label until the next 1h candle is live.

If Test C shows <50% accuracy: the Fourier-weighted score should be treated as non-informative.
Fall back to the market price as the sole probability input.

---

## Problem 3 — Signal-to-Payoff Mismatch: Directional Signal vs. Strike-Based Binary

### What the data shows

The 5 YES fills entered at market prices of 7¢, 9¢, 31¢, 32¢, 31¢. All lost.

A YES contract at 7¢ pays $0.93 if BTC closes above the strike. The market implies P=0.07.
For a directional "UP" signal to create edge here, the model must believe P > 0.07 — that
BTC will close above a specific price level within the remaining contract window.

Context for the 7¢ and 9¢ fills: these contracts had minutes_to_expiry in the range shown
by the regime data. A 7¢ contract with 10 minutes remaining means the market says there is
roughly 7% chance BTC moves enough to finish above strike. For that to be wrong by the model's
claimed 82%, BTC would need to be near the strike (which would price YES at 40–60¢, not 7¢).
At 7¢ YES, BTC is already significantly below the strike.

### The core mismatch

The model outputs "CALL = bullish momentum." It does not output "P(BTC > L at time T)."
These are related but not the same. A bullish momentum signal of 82% in a 10-minute window
does NOT mean BTC has an 82% chance of finishing above a level it currently sits 3% below.

This is the "cash-or-nothing digital option" framing from the brief. The correct target
variable for main-engine decisions is:

  P(BTC_close > K | current_spot, implied_vol, time_to_expiry)

Not: P(BTC_direction = UP).

### Research test

For each evaluation row, compute:
- `spot_to_strike_pct = (strike - btc_spot) / btc_spot * 100`
  (how far BTC needs to move to resolve YES)
- `atm_vol_est` — estimated annualized vol from regime_detector's ATR
- `p_itm_approx` — rough probability using normal approximation:
  `p_itm ≈ N(-(spot_to_strike_pct) / (atm_vol_est * sqrt(T_years)))`
- Compare `p_itm_approx` to `best_ask / 100` (market price)
- Compare both to `adjusted_confidence / 100` (model output)

If the market price tracks `p_itm_approx` closely (which it should for efficient markets)
and the model's output does not, then the model is not predicting the right thing.

### Implementation

This is not a filter — it is a target redefinition. Three steps:

1. **Compute moneyness** at decision time. Requires BTC spot price and contract strike (derivable
   from the Kalshi ticker format: `KXBTC15M-26MAR17NNNN` where NNNN is the strike level).
   Parse the strike from the ticker and track BTC spot from the existing price feed.

2. **Compute a vol-adjusted probability** as a model baseline:
   ```python
   import math, scipy.stats as st
   p_itm = st.norm.cdf((math.log(spot / strike)) / (vol_daily * math.sqrt(T_days)))
   # For put side: 1 - p_itm
   ```
   Use this as the "dumb baseline" — the market already knows this.

3. **Require model to beat baseline by at least fee_cents:**
   ```
   edge = model_prob - p_itm - fee_frac
   if edge < MIN_EDGE_THRESHOLD: reject
   ```

Without the strike and spot, the model is blind to contract geometry. This is the most
structural missing feature in the main engine.

---

## Problem 4 — `_REGIME_SESSION_WR` Table Is Fiction

### What the data shows

The `_REGIME_SESSION_WR` table in `signal_intelligence.py` drives `adjusted_confidence`:
- `TRENDING_DOWN × NY-Open = 67.4%`
- `TRENDING_UP × Asia = 64.2%`
- etc.

These are from a 90-day Pine Script backtest. Live data:
- TRENDING_DOWN fills: 1/11 won = 9.1% actual WR vs 59–67% table WR
- TRENDING_UP fills: 0/5 won = 0% actual WR vs 60–66% table WR

The table is off by 50–65 percentage points. Every entry the engine approves is approved
against a benchmark that doesn't exist. The floor `adjusted_conf = max(cell_wr, ...)` means
every approved signal has a minimum confidence of 59–67%, regardless of the raw signal.
This prevents the model from being appropriately uncertain.

### Research test

Query actual win rates per regime × session from execution_log:
```sql
SELECT regime, session,
       COUNT(*) as n,
       SUM(CASE WHEN expiry_outcome='yes' AND side='yes' THEN 1
                WHEN expiry_outcome='no'  AND side='no'  THEN 1
                ELSE 0 END) as wins
FROM execution_log
WHERE filled=1 AND expiry_outcome IS NOT NULL
GROUP BY regime, session
```
With 16 filled trades, n is too small for reliable per-cell estimates. However the pattern
is clear: the table needs a kill switch until 50+ settled trades exist per cell.

### Implementation

Two options:
1. **Disable the table floor entirely.** Let `adjusted_conf = signal.confidence * multiplier`.
   Stop pretending backtest WRs predict live performance.
2. **Replace with rolling actuals.** Wire `WinRateTracker` per regime × session cell.
   Disable a cell's floor until n ≥ 20 settled trades exist for that cell.

Both options reduce false confidence. Option 2 is more principled but requires sample size.
Option 1 is a one-line change that immediately stops the artificial confidence inflation.

---

## Problem 5 — WinRateTracker Baselines Are Unreachable

### What the data shows

`BASELINE_WR = {0: 67.57, 1: 75.56, 2: 84.97, 3: 95.82}`

Live adjusted_confidence values range 59–83%, placing most trades in buckets 1 and 2.
Live WR is 6.25% (1 win / 16 fills).

The degradation check triggers when `live_wr < baseline - 10%`:
- Bucket 1: flags degradation at WR < 65.56% — but live WR is ~6%, far below even
  the 10% threshold. This means the check has certainly flagged degradation already,
  but the system is too data-sparse (needs 10 trades per bucket) to reliably fire.
  At 16 total fills, some buckets may have < 10 trades.

The check design is correct in principle; the baselines are wrong in magnitude.

### Implementation

Recalibrate baselines to something defensible:
```python
# Replace backtest values with conservative live-derived estimates
BASELINE_WR = {
    0: 45.0,   # bucket 0: confidence < 25% (rare)
    1: 50.0,   # bucket 1: confidence 25-50%
    2: 55.0,   # bucket 2: confidence 50-75%
    3: 60.0,   # bucket 3: confidence > 75%
}
```
Or set all to 50.0 (coin flip baseline) as the degradation floor. Anything below coin-flip
is strictly worse than not trading. This gives the degradation check teeth immediately.

---

## Problem 6 — Submission Funnel Has an Ordering Anomaly

### What the data shows

Gate combination frequencies:
```
(approved=1, pricing=0, liquidity=1, exec=0, submitted=0): 314 rows
```

314 rows pass `liquidity_eligible=1` while `pricing_eligible=0`. If the gates are sequential
(pricing must pass before liquidity is checked), this is impossible. It means either:
- The gates are evaluated in parallel (each set independently)
- OR there's a code path where liquidity is set before pricing is checked

95 rows pass all four gates but are not submitted. With avg spread=1.3¢ and avg time=10 min,
there is no obvious microstructure reason not to submit. A possible cause: another position
is already open (main engine position limit = 1), blocking concurrent entry.

### Research test

1. Query the 95 non-submitted execution-eligible rows and compare their timestamps to
   nearby filled rows — if they cluster within 15 minutes of a live position, the position
   limit is the blocker.
2. For the 314 liquidity-without-pricing rows, look at the code in main.py that sets these
   flags sequentially to find where the ordering breaks.

---

## Problem 7 — Time-of-Day Gate Is Incomplete

### What the data shows

Current `BAD_HOURS = {9, 12, 15, 21}` in `SignalFilter`.

HFT data by hour:
```
17h UTC: 33% WR (-$1.31)
18h UTC: 40% WR (-$0.06)
19h UTC: 22% WR (-$0.37)
20h UTC: 17% WR (-$0.23)
21h UTC: 0%  WR (-$0.28)
```

Hours 17–21 UTC (1–5pm ET / US prime session) are consistently negative for HFT.
Hour 21 is already blocked. Hours 17–20 are not blocked. All four are below breakeven.

From kalshi_trades hourly breakdown:
- Hour 14 UTC: 12.5% WR, -$8.39 (worst hour all-time)
- Hour 22 UTC: 6.3% WR, -$5.30
- Hour 18 UTC: 0% WR, -$4.41

Hours 14, 18, 22 are not blocked. Hours 14 and 18 are structurally losing in both
main engine and HFT data.

### Research test

For each UTC hour, compute separately:
1. Main engine (execution_log, filled, expiry_outcome not null) WR + net P&L
2. HFT (hft_log, decision=entered) WR + net P&L
3. Number of trades in each

Determine whether hours 14, 17–20, 22 are consistently negative or just low-sample noise.
With current data sizes (10–40 trades per hour) a 30-trade sample showing 0% WR is
statistically significant (p < 0.001 for true WR = 50%).

### Implementation

Move BAD_HOURS to config.py as a single source of truth (improvements.md #10):
```python
BAD_UTC_HOURS: frozenset[int] = frozenset({9, 12, 14, 15, 17, 18, 19, 20, 21})
```
Then gate HFT (separately from main engine) by checking UTC hour in `_current_poll_interval()`
or `_check_scalp_entry()`. HFT currently has no time-of-day gate at all.

---

## Problem 8 — HFT Entry Price Ceiling Not Enforced

### What the data shows

HFT WR by entry price bucket (hft_log, all time):
```
21–30¢: 85.7% WR, avg P&L +$0.35
31–40¢: 88.5% WR, avg P&L +$0.23
41–50¢: 65.4% WR, avg P&L +$0.065
51–60¢: 29.9% WR, avg P&L -$0.067   ← net losing
>60¢:    0%   WR, avg P&L -$0.20   ← catastrophic
```

117 trades entered at 51–60¢ produced -$7.74 net. 5 trades above 60¢ produced -$1.02.
Together: 122 trades, -$8.76. Total HFT net is +$29.99. Without these 122 trades it
would be +$38.75 — a 29% improvement.

The structural explanation: at 51–60¢ (near-mid), the binary contract is priced near 50¢.
Fees are highest here (proportional to p*(1-p)). The contract is most sensitive to small
moves. And the market's 50¢ price means no one knows which way it resolves. HFT edge
in the 21–40¢ zone comes from buying a contract that the market prices as "unlikely"
and that actually resolves — a mean-reversion trade, not a directional bet.

Above 50¢, HFT is buying "likely" outcomes. These cost more, resolve less often (in HFT's
time window), and have higher fees. The math is worse in every dimension.

### Implementation

One config constant:
```python
HFT_MAX_ENTRY_CENTS: int = 50  # Never enter above mid; edge collapses
```
Add to `_check_scalp_entry()` before the edge calculation:
```python
if market_ask > HFT_MAX_ENTRY_CENTS:
    return self._reject(contract, "entry_above_max_cents")
```
Test: start at 50, consider tightening to 48 after one session's data.

---

## Problem 9 — Main Engine Lacks BTC Spot and Contract Moneyness

### What the data shows

The execution_log has no `btc_spot`, no `contract_strike`, no `moneyness` column.
All 16 fills were made without any knowledge of how far BTC was from the contract's
strike level.

Two YES trades at 7¢ and 9¢ market prices were submitted. At 7¢ ask, the market already
priced the event at 7% probability. Buying at 7¢ with 82% model confidence means the engine
believed a near-zero probability event would happen. Without knowing the strike distance,
the engine had no information to dispute the market's 7% estimate.

### Research test

The Kalshi contract ticker encodes the strike. For `KXBTC15M-26MAR17NNNN`:
- Parse `NNNN` as the strike level in thousands (e.g., 84000, 83500)
- Match against BTC spot from the existing price feed at signal time
- Compute `spot_to_strike_pct = (strike - spot) / spot * 100`

Expected finding: YES fills at 7–9¢ will show `spot_to_strike_pct > 1.5%` — BTC needs
to move >1.5% up in the remaining window. At low volatility, that is genuinely a 5–10%
event, not an 82% event.

### Implementation

Parse the strike from the ticker in `main.py` when a contract is selected.
Compute a rough moneyness check before signal approval:
```python
strike_pct_away = abs(btc_spot - contract_strike) / btc_spot
# If YES at 7¢ but strike is 1.5% away with 10 min left → skip
# Rule of thumb: if market_price < 15¢ and strike_pct_away > 0.5% → skip
```
This is not a full options model — it is a sanity check that prevents buying near-zero
probability events.

---

## Research Priorities (Ordered)

These are falsifiable tests, in order of expected impact per effort:

| # | Test | What It Resolves | Effort |
|---|------|-----------------|--------|
| R1 | Compute `model_edge = model_conf - market_price` for all fills | Is the model anti-correlated with market? | 1h (SQL) |
| R2 | Check sign of BTC spot return vs regime label (last 15 min before fill) | Is regime inverted or lagging? | 2h |
| R3 | Parse strike from ticker, compute moneyness for all filled trades | Was engine buying hopeless OTM? | 2h |
| R4 | AUC of Fourier score vs expiry_outcome on execution_log | Does the score predict anything? | 1h (SQL+Python) |
| R5 | Plot `adjusted_confidence - market_price` distribution by profit/loss | Is high model-market gap predictive of loss? | 1h |
| R6 | Hour × WR for both HFT and main engine with n and p-value | Which hours to block with confidence? | 1h (SQL) |

---

## Implementation Priorities (Ordered by Expected P&L Impact)

| # | Change | Expected Impact | Complexity |
|---|--------|----------------|------------|
| I1 | `HFT_MAX_ENTRY_CENTS = 50` (no entries above mid) | +$8.76/session (eliminate losing bucket) | 5 min |
| I2 | Extend BAD_UTC_HOURS to include 14, 17–20 | Stop systematic session losses | 15 min |
| I3 | Disable `_REGIME_SESSION_WR` floor (stop inflating confidence) | Prevents overconfident bad entries | 10 min |
| I4 | Add `market_implied_prob` to execution_log and gate on `model_edge` | Main engine stops trading against market | 2h |
| I5 | Recalibrate WinRateTracker baselines to 50% floor | Degradation check fires at right threshold | 10 min |
| I6 | Parse strike from contract ticker + moneyness check | Prevents buying deep OTM YES/NO | 3h |
| I7 | Fix gate ordering anomaly in execution_log (pricing before liquidity) | Correct funnel metrics | 1h |
| I8 | Validate regime directional accuracy (R2 above) | Root cause of TRENDING_DOWN inversion | Investigative |

---

## What Not to Change Yet

- Fourier weights (TF_WEIGHTS) — don't touch until R4 confirms/denies the score has signal
- Trailing stop parameters — HFT is healthy in the 21–40¢ zone; these are working
- Regime detector ATR thresholds — test accuracy first (R2) before touching definitions
- WinRateTracker rolling window size — fix the baselines first, window size is secondary

---

## Key Open Questions

1. **Is the Fourier-weighted consensus score correlated at all with realized outcomes?**
   If AUC ≈ 0.50 on the execution_log settled rows, the directional layer has no signal
   and should be deactivated (HFT-only mode).

2. **Is the regime label inverted?** One check: on days when TRENDING_DOWN dominated
   regime labels, what was the BTC spot return over the full day? If positive, the label
   is wrong about macro direction.

3. **Are the 95 execution-eligible but non-submitted rows blocked by position limits?**
   If yes, the main engine is actually quite active in its evaluations but self-throttled.

4. **What is the contract strike for the filled YES trades at 7¢ and 9¢?**
   Parse tickers from execution_log and compute moneyness. If BTC was >1% below strike
   at those fills, those trades were structurally hopeless regardless of signal.
