# Claude Handoff: Best Engine Candidate

Date: 2026-04-25

## Executive Summary

The best-performing engine shape identified from the local directory, live/manual evidence, and external Kalshi constraints is **ATM Dislocation / Strike-Pin Reversion**.

This should become the next primary candidate engine, not SR_FADE as currently shaped.

Core idea:

- When BTC is very close to the Kalshi 15m strike, contract prices can overreact or temporarily dislocate.
- Buy the underpriced side while BTC is near strike.
- Exit on small limit/profit targets.
- Do **not** treat this as a default hold-to-expiry strategy.

This matches the user's manual winners better than SR_FADE and is more robust in the 30-day local replay.

## Ranking Of Candidate Engines

1. **ATM Dislocation / Strike-Pin Reversion**
   - Best balance of sample size, edge, and live/manual confirmation.
   - Entry when `abs(BTC - strike) <= ~0.02%`.
   - Buy the underpriced side.
   - Exit around `49c` or `entry +5c/+8c`.
   - Recommended as next primary engine candidate.

2. **Late Dominant Minute 11/12 Engine**
   - Very high win rate, but thinner edge and sample.
   - Good secondary engine, especially for conservative late-window entries.
   - Do not full-port this from backtest alone.

3. **SR_FADE**
   - Engineering is much safer now, but settlement-truth performance is still negative.
   - Keep small-size / research-only until it proves positive on settlement-truth labels.

4. **Open-Inversion / Volume Reversal**
   - Useful as a feature, not as standalone primary engine.
   - Broad rerun was weaker than ATM reversion.

## Local Evidence

Backtest source:

- `data/kalshi_external_backtest.db`
- 30-day replay using official Kalshi 1-minute candles and BTC 1-minute data.
- Entry modeled at executable ask.
- Exit modeled at same-side bid or settlement.
- This is not tick-perfect, but it is more realistic than settlement-only tests.

### ATM Discount

Near-strike discount setup:

- `abs(BTC - strike) <= 0.02%`
- discounted side ask `<= 40c`
- fair proxy `>= 47c`
- edge `>= 8c`

Results:

```text
ATM_DISCOUNT, target 49c:
n=627
WR=62.7%
avg=+6.13c/contract before fees
rough taker-entry net=+4.54c/contract

ATM_DISCOUNT, target 45c:
n=627
WR=66.5%
avg=+5.58c/contract before fees

ATM_DISCOUNT, profit +8c:
n=627
WR=71.5%
avg=+5.48c/contract before fees
```

### ATM Combined Discount + Bias

Combined ATM setup:

- discount entries as above
- plus bias entries where BTC is slightly above/below strike and the favored side is still underpriced

Results:

```text
ATM_ALL, profit +8c:
n=993
WR=75.7%
avg=+4.10c/contract before fees
rough taker-entry net=+2.37c/contract

ATM_ALL, profit +5c:
n=993
WR=78.9%
avg=+3.57c/contract before fees
rough taker-entry net=+1.84c/contract

ATM_ALL, target 49c:
n=993
WR=69.1%
avg=+3.30c/contract before fees
```

Interpretation:

- The edge is not just expiry prediction.
- The better shape is **buy dislocation, harvest repricing**.
- Limit/profit exits are important.
- Holding to settlement works less cleanly and adds unnecessary binary tail risk.

## Comparison Against Other Engines

### SR_FADE

Settlement-backed local live table showed:

```text
SR_FADE:
n=69
pnl=-$99.71
WR=78.3%
```

The high WR is misleading because many wins are small and occasional settlement/execution failures dominate P&L.

Conclusion:

- SR_FADE safety and cleanup work has improved a lot.
- But it has not earned primary-engine status.
- Keep it small until settlement-truth P&L turns positive.

### TA_FORCED_SIGNAL

Settlement-backed local live table showed:

```text
TA_FORCED_SIGNAL:
n=75
pnl=-$47.65
WR=66.7%
```

Conclusion:

- Not the best current candidate.
- Keep TA_FORCED evaluator alive only where it feeds shared data pipelines.

### Open-Inversion / Volume Reversal

Broad rerun:

```text
trades=883
WR=40.3%
fixed100 PnL=-372c
avg=-0.42c/trade
trail exits=462
settlement=421
```

Conclusion:

- Some parameter slices looked good earlier.
- As a broad primary engine, it is weaker than ATM reversion.
- Use open-inversion/volume as features inside the ATM selector, not as the main engine.

### Minute 11 Dominant Engine

Strong local backtest:

```text
minute=11
dom>=90
entry=55-90c
n=53
WR=96.2%
avg=+6.23c/contract
```

But:

- Smaller sample than ATM.
- It is a late-window expiry prediction engine, so sizing mistakes can compound hard.
- Good secondary module, not the first thing to full-port.

## Live Manual Evidence

The user's manual winners looked like this:

- BTC was essentially pinned near the strike.
- Kalshi contract price temporarily discounted the eventual/right side.
- Contract repriced sharply once the market recognized the strike proximity.

Example observed from local ledger/shadow ticks:

- `KXBTC15M-26APR250815-15`
- User held roughly 402 YES-equivalent exposure.
- Settlement ledger showed about `+$80.44`.
- BTC was nearly exactly at strike while YES was priced in the 30s/40s before repricing.

This is the signature ATM dislocation setup, not a classic SR_FADE setup.

## External Constraints That Matter

Kalshi BTC contracts settle from the official BTC reference value around expiry. The local strategy should therefore respect strike distance and final-minute gamma.

Fee constraints:

- Taker fees are meaningful.
- Maker orders are cheaper, but maker-only entry can miss the exact dislocation.
- The engine should be fee-aware and only cross when expected edge comfortably exceeds fee + spread.

Sources checked:

- Kalshi API docs: `https://docs.kalshi.com/`
- Kalshi fee schedule: `https://kalshi.com/docs/kalshi-fee-schedule.pdf`
- Kalshi BTC contract terms: `https://kalshi-public-docs.s3.amazonaws.com/contract_terms/BTC.pdf`
- PredictionMarketBench paper: `https://arxiv.org/abs/2602.00133`

## Critical Bug In Current Shadow Monitor

`scripts/codex_live_shadow_monitor.py` currently has a live-paper issue:

- `atm_reversion_trades` is polluted.
- It repeatedly re-enters the same ticker after instant target exits.
- This makes the live paper table misleading.

Before using live shadow results, add:

```python
entered_atm_tickers: set[str]
```

Then block additional ATM entries for a ticker after the first ATM paper entry in that window.

Recommended invariant:

- Maximum one ATM paper trade per ticker.
- Reset the lock only when ticker changes.

## Recommended Implementation Order

1. **Fix the ATM shadow monitor**
   - Add one-trade-per-ticker lock.
   - Clear/restart the polluted shadow DB or mark old rows as invalid.
   - Continue collecting live paper data.

2. **Create a dedicated backtest script**
   - Suggested file: `scripts/atm_reversion_backtest.py`
   - Do not leave this as inline one-off code.
   - Parameters should include:
     - `max_strike_dist_pct`
     - discount entry cap
     - bias entry cap
     - fair-edge threshold
     - target cents
     - profit target cents
     - force-exit age
     - fee model

3. **Implement ATM_REVERSION as a separate live tier**
   - Feature flag: `ATM_REVERSION_ENABLED`
   - Start with small fixed sizing.
   - One trade per ticker.
   - Strict position mutex with existing live engine.
   - Settlement hold should be opt-in, not default.

4. **Exit logic**
   - Primary exits:
     - `entry +5c`
     - `entry +8c`
     - or same-side bid reaching `49c`
   - Stop/abort:
     - BTC escapes strike band, e.g. `abs(strike_dist_pct) >= 0.06%`
     - force flat near `840s` age
   - Do not rely on hard settlement unless the position is strongly favored and liquidity makes exit impossible.

5. **Execution**
   - Maker-first.
   - Bounded taker allowed only if:
     - spread is tight
     - edge exceeds fee + spread by a margin
     - strike distance remains inside band
   - Log fee-adjusted expected value at entry.

## Proposed Initial Live Config

```python
ATM_REVERSION_ENABLED = False  # paper first
ATM_MAX_STRIKE_DIST_PCT = 0.02
ATM_STOP_STRIKE_DIST_PCT = 0.06

ATM_DISCOUNT_MAX_ENTRY_CENTS = 40
ATM_DISCOUNT_MIN_FAIR_CENTS = 47
ATM_DISCOUNT_MIN_EDGE_CENTS = 8

ATM_BIAS_ENABLED = True
ATM_BIAS_MAX_ENTRY_CENTS = 62
ATM_BIAS_MIN_EDGE_CENTS = 1

ATM_TARGET_CENTS = 49
ATM_PROFIT_TARGET_CENTS = 5
ATM_STRETCH_PROFIT_TARGET_CENTS = 8

ATM_ONE_TRADE_PER_TICKER = True
ATM_FORCE_EXIT_AGE_S = 840

ATM_FIXED_SIZE_CONTRACTS = 5  # initial live validation only
ATM_MAX_SIZE_CONTRACTS = 20
```

## Bottom Line

The next engine should be:

```text
ATM_REVERSION:
  near strike
  buy underpriced side
  harvest repricing via limit exits
  avoid settlement tail risk
  size small until live paper and small-live samples agree
```

This is the strongest path toward profitability based on the full local directory, recent manual trades, and current Kalshi mechanics.
