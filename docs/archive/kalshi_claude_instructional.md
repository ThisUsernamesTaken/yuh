# Claude Instructional: Building a Real, Fee-Aware Kalshi Trading Engine

## Goal

Design, audit, and improve a Kalshi algorithmic trading system so that:

1. **Reported PnL matches exchange reality**
2. **All open positions are tracked through exit or settlement**
3. **Fees, spreads, and binary payoff geometry are modeled explicitly**
4. **The engine prefers real edge over fake backtest/accounting edge**
5. **Every strategy is deployable on a regulated event-contract exchange**

This document is both a build guide and an execution checklist.

---

## Core principles

### 1) Exchange truth beats internal logs
If internal logs disagree with Kalshi fills, balances, or settlements, treat Kalshi as source of truth.

Required rule:
- Never declare PnL from a paper exit, inferred bid, or hypothetical fill.
- Realized PnL must come from one of:
  - confirmed exit fill,
  - confirmed settlement,
  - or explicit exchange-reported position change.

### 2) Binary contracts are not normal spot instruments
A Kalshi contract is a bounded $0/$1 payoff. Strategy design must respect:
- bid-only book representation,
- YES/NO symmetry,
- event expiry cliffs,
- fee drag,
- discrete ticks,
- and side-dependent interpretation of prices.

### 3) Fees and spread are first-class constraints
If a strategy wins by 1–2¢ gross but pays similar spread/fee drag, it does **not** have edge.
Every entry decision must clear a fee-aware minimum edge threshold.

### 4) Maker-first where possible
Prefer resting liquidity when the strategy permits it. Taker-heavy micro-scalping is much more likely to be destroyed by spread and fee drag.

### 5) No silent state resets
No code path may discard a live position reference without first attempting:
- exchange reconciliation,
- exit,
- or explicit orphan logging.

---

## Immediate lessons from the reviewed system

The existing BTC 15-minute engine audit revealed several highly important truths:

### What was genuinely useful
- Raising HFT edge floors improved screening quality.
- A model-vs-market sanity gate prevented absurd disagreements between model confidence and market price.
- Narrowing the entry band avoided mid-probability fee-heavy regions.
- Reducing dead code paths simplified the engine.

### What was misleading
- Reported HFT profitability was inflated by accounting logic.
- Many positions were not actually exited before window transitions.
- Logged exit prices were side-wrong for sell orders.
- Internal HFT PnL could look excellent while exchange balance did not improve.

### Operational conclusion
Before adding any more alpha logic, the engine must be made **accounting-correct, restart-safe, and fee-realistic**.

---

## Required architecture

## A. Truth model
Maintain three distinct ledgers:

### 1) `orders`
Tracks raw order lifecycle:
- order_id
- client_order_id
- ticker
- side
- action
- submitted price
- submitted quantity
- order_type
- post_only
- reduce_only
- status transitions
- timestamps

### 2) `positions`
Tracks live and historical position state:
- position_id
- ticker
- instrument side exposure
- entry order_id
- entry fill price
- entry quantity
- current quantity
- realized exit order_id
- realized exit price
- settlement value
- final state (`open`, `closed`, `settled`, `orphaned`)

### 3) `pnl_recon`
Tracks authoritative realized economics:
- source (`exit_fill`, `settlement`, `exchange_balance_recon`)
- gross PnL
- fees
- net PnL
- reconciliation hash / audit note

Rule:
- Strategy logs may record **intent**.
- Only `pnl_recon` may record **realized PnL**.

---

## B. Market data model
Kalshi book handling must be explicit.

For each market:
- `best_yes_bid`
- `best_yes_ask = 1 - best_no_bid`
- `best_no_bid`
- `best_no_ask = 1 - best_yes_bid`
- spread on the side actually traded
- tick size / valid price levels
- time to close
- last trade
- volume / open interest

Rules:
- Use fixed-point or decimal-safe arithmetic.
- Never use floating-point shortcuts for prices or fractional quantities.
- Never assume sell-side average fill prices can be interpreted identically to buy-side fields without side normalization.

---

## C. Strategy separation
Split the engine into distinct modules:

### 1) Market-making / passive execution
- maker-first quotes
- queue-position aware
- inventory-limited
- low-churn, low-write-rate

### 2) Event-driven directional trading
- external-data or model-driven
- fee-aware entry threshold
- strict model-vs-market sanity check

### 3) Microstructure scalp / latency edge
Only keep this module if it survives **exchange-truth** reconciliation.
If it cannot overcome spread + fees after reconciliation, disable it.

---

## Highest-priority fixes

## Priority 0 — stop fake profitability
These are mandatory before any new strategy work.

### Fix 1: No more phantom exits
For every open position, enforce exactly one of:
- exit filled,
- settled by exchange,
- or explicitly marked as orphan requiring reconciliation.

Required protections:
- On window transition, attempt to close old-ticker exposure before switching context.
- On no-contract gap, reconcile open positions before clearing state.
- On restart, query exchange positions and recover any open exposure.

Acceptance criteria:
- `NULL` exit reason count goes to zero for new trades.
- Every historical position can be classified as closed, settled, or orphaned.

### Fix 2: Side-correct exit price accounting
For sell orders, normalize exchange-reported fill economics to the actual contract side being sold.

Acceptance criteria:
- A NO-side sell at ~21¢ must never be logged as ~79¢ unless explicitly tagged as YES-equivalent reference data.
- Reported exit price must match executable side economics.

### Fix 3: Reconcile logs to exchange balance daily
Add a reconciliation job that computes:
- starting balance
- deposits/withdrawals
- realized fill PnL
- settlement PnL
- fees
- ending balance

Acceptance criteria:
- daily reconciliation error should be near zero except for clearly documented unsettled/open exposure.

---

## Priority 1 — make entries realistic

### Fix 4: Fee-aware edge gating
Every entry must pass:

`expected_edge > spread_cost + estimated_fee + safety_buffer`

Do not use raw model edge.
Use **net edge after costs**.

Suggested policy:
- enforce higher minimum net edge for taker-style entries,
- stricter thresholds near 50¢ where fee burden is worse,
- stricter thresholds near expiry if the engine may be forced to cross the book to exit.

### Fix 5: Model-vs-market sanity gate
If the model says 82% and the market is 7¢, assume the model is wrong before assuming the market is asleep.

Suggested guardrail:
- reject entries when model-implied probability differs from market-implied probability by more than a configurable threshold.

This is not a substitute for alpha, but it is an excellent filter against pathological model outputs.

### Fix 6: Kill low-value micro-scalps
If the true realized edge after reconciliation is near zero or negative, disable or redesign the scalp strategy.

A strategy that repeatedly:
- buys around X,
- sells around X or X-1,
- and pays fees,
is not an edge engine.
It is a fee transfer engine.

---

## Priority 2 — improve execution quality

### Fix 7: Maker-first exits where possible
For non-urgent exits:
- post reduce-only orders on the side being flattened,
- allow limited repricing ladder,
- avoid immediate taker exits unless risk-off logic requires urgency.

### Fix 8: Queue-position and fill-probability tracking
For resting orders, track:
- queue position,
- time-to-fill,
- cancellation rate,
- fill probability by distance from touch,
- adverse selection after fill.

### Fix 9: Latency and write-budget controls
Track:
- REST latency,
- websocket staleness,
- decision-to-submit latency,
- submit-to-ack latency,
- cancel/replace rate,
- write-limit consumption.

Throttle aggressively under degraded conditions.

---

## Priority 3 — better research loop

## A. Required analytics dashboard
For each strategy and side:
- fill count
- true realized PnL
- fees
- average spread paid/captured
- adverse selection after fill
- win rate
- average hold time
- PnL by entry band
- PnL by minutes-to-expiry bucket
- PnL by UTC hour
- PnL by YES vs NO
- orphan count
- reconciliation error

## B. Strategy review buckets
At minimum, review PnL by:
- price band: `<10¢`, `10–20¢`, `20–35¢`, `35–42¢`, `42–50¢`, `>50¢`
- minutes to expiry: `0–3`, `3–6`, `6–10`, `10+`
- side: `YES`, `NO`
- execution style: `maker`, `taker`

## C. What to trust
Trust these in order:
1. exchange balances and settlements
2. reconciled fills
3. position ledger
4. strategy logs
5. subjective impressions

---

## Suggested implementation sequence

### Phase 1 — accounting correctness
1. normalize sell-side fill pricing
2. eliminate silent orphaning
3. startup position recovery
4. daily balance reconciliation
5. tag every PnL record with source-of-truth type

### Phase 2 — execution safety
1. reduce-only exits everywhere appropriate
2. maker-first exit ladder
3. write-budget throttling
4. kill switch on repeated desync or stale book

### Phase 3 — strategy pruning
1. disable modules with no genuine edge
2. preserve only strategies that survive exchange-truth reconciliation
3. retest HFT after accounting fix
4. if still negative after fees, shut it off

### Phase 4 — alpha improvements
1. event-driven external signal models
2. mutually-exclusive event basket arbitrage
3. passive market making in liquid markets
4. calibration and probability correction
5. volatility-adaptive thresholds

---

## Hard acceptance criteria

The engine is not “fixed” until all of these are true:

- No new open position is lost on restart or window transition.
- New trades do not produce `NULL` exit reasons.
- Reported realized PnL can be reconciled to exchange data.
- Sell-side exit prices are side-correct.
- Fee-adjusted net edge is used for all entries.
- Strategy-level profitability is positive **after** fees and reconciliation.
- Any strategy that fails this test is disabled.

---

## Claude execution instructions

When working on this codebase, follow these rules:

1. **Do not optimize around internal logs alone.**
2. **Do not assume an apparent win rate is real until reconciled to Kalshi truth.**
3. **Do not add new strategy complexity before closing accounting gaps.**
4. **When modifying order or PnL logic, write a small diagnostic script to verify the change against historical rows.**
5. **Any function that can clear, replace, or reset position state must first reconcile live exchange exposure.**
6. **For sell orders, validate that logged execution price reflects the actual side traded, not a YES-equivalent internal cost field.**
7. **Document every fix with: root cause, affected rows/trades, expected impact, and validation query.**

---

## Final recommendation

The most important insight is this:

> A Kalshi engine that is accounting-wrong will look brilliant right up until you compare it to real cash.

So the correct next move is not “more alpha.”
The correct next move is:

1. make accounting exact,
2. make execution state durable,
3. re-measure true edge,
4. then keep only the strategies that remain profitable after reality is applied.

