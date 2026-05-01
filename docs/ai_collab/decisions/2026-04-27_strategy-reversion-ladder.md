# Decision: strategy reversion ladder

**Date:** 2026-04-27
**Status:** final (retroactive — should have been filed when ATM was promoted on 2026-04-25)
**Author:** Claude (corrected in tandem with user after ATM under-fired live)

## Question

When the live primary strategy under-performs or fails, what's the
reversion order — and what triggers it?

## Context

I missed writing this note when ATM was promoted to live primary on
2026-04-25. The promotion was justified by ATM's strong backtest record
(1,202 trades, +3.21c/ct net, settlement-truth) but I didn't document
the fallback path if live ATM disappointed.

It did disappoint. After ~17 hours of live ATM-only mode (2026-04-26
13:00 PT through 04:00 PT 2026-04-27), only 1 live trade fired (+$0.79).
The user — correctly — pushed back: "the engine we ran before ATM was
better." They were right. The previous-previous engine (FVG / Brownian
Bridge) had:

- 591 paper trades over 2026-04-21 → 2026-04-23 (3 days)
- 89% win rate
- +17.91c/ct paper avg net (likely inflated by a now-fixed double-count
  bug; honest figure plausibly +6 to +9c/ct after de-duplication)
- ~200 trades/day fire rate
- A simpler decision predicate (BB fair value vs session baseline)

We walked away from FVG/BB for SR_FADE → ATM without documenting why,
and without a fallback rule for when to roll back. **That's the gap
this note fixes.**

## Decision

The reversion ladder, in order from preferred to last-resort:

1. **Current primary live tier** (whatever is `LIVE_STRATEGY_MODE` and
   `*_ENABLED=True` today)
2. **FVG / Brownian Bridge** — the historical primary that produced the
   strongest paper record before SR_FADE took over. Single-predicate
   strategy, high fire rate (~200/day in paper), uses
   `prob.fair_value` (Brownian Bridge) vs session baseline.
3. **SR_FADE** — the predecessor. Has more live evidence than FVG/BB
   has live evidence; mechanically more complex (TP ladder, MFE-lock,
   DCA, orphan reconciliation) but proven profitable on real money in
   the post-fix era (82% WR over the last ~17 trades; account took
   $217.76 → $226.04 in one session on 2026-04-25).
4. **TA_FORCED** — currently entry-disabled (`TA_FORCED_ENTRY_ENABLED=False`)
   but the data pipeline runs. Last-resort if all three above fail.

## Trigger conditions

A primary tier is **flunking** if any of these hold over a 24-hour
window:

- Live fills < 5 (the strategy isn't trading)
- Net P/L per session < +$0.50 average (the strategy isn't compensating
  the operator's attention)
- Any LIVE-RECONCILE-BLOCK that doesn't auto-clear within one window
- Two or more critical bugs surface in the live execution path
  (e.g. the 2026-04-26 broken market-fallback)

When **any two** of those hold, revert one rung down the ladder. Do
not stack failures.

## Rule for promoting a new primary

**A strategy must satisfy ALL of:**

1. ≥ 100 paper or live fills with settlement-truth attribution
2. Net per contract ≥ +2.5c after Kalshi fees
3. Mechanical predicates documented in a single source-of-truth module
   (no scattered exit logic across `_manage_position`, `_clear_position`, etc.)
4. A working live execution path (entry + exit + reconciliation)
5. **A written reversion-ladder note exists** stating the next-best
   primary if this one flunks. (This exact rule was missing for ATM.)

## Implications

- ATM should not have gone live without an explicit "if this flunks,
  revert to FVG/BB or SR_FADE" rule recorded.
- Going forward, every primary-promotion decision must be accompanied
  by a fallback record. Period.

## What this triggers right now

ATM has flunked under the trigger conditions above:
- 1 live fill in 17h (< 5 in 24h ✓)
- $0.79 day P&L (< $0.50/session ≠ but close)
- A critical bug in the market-fallback path (broken Kalshi API call)

**Action: revert to LEGACY mode (SR_FADE primary) immediately.** Fix
the FVG/BB G6 state-guard bug to start collecting honest paper data.
Plan FVG/BB live promotion in 2–3 days when fresh paper data validates
the original 89% / +6-9c/ct expectancy.
