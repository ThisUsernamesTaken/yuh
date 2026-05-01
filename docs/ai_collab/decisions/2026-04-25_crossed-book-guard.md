# Decision: observe crossed/complementary Kalshi books as paper-only candidates

**Date:** 2026-04-25
**Status:** revised by Codex after user confirmed the jump was observed live
**Authors:** Codex + Claude

## Question

Should `atm_reversion.evaluate` and the live shadow tracker reject a Kalshi
book reading where `yes_bid >= yes_ask`?

## Context

During live monitoring on 2026-04-25, the paper engine saw examples such as:

```text
YES bid=74c
YES ask=1c
```

The user reported watching the same jump live and nearly manually trading it.

Important implementation detail from `kalshi_ws.py`:

```python
best_yes_ask = 100 - best_no_bid
best_no_ask = 100 - best_yes_bid
```

So `yes_bid >= yes_ask` is not necessarily a malformed one-sided book. It can
represent a complementary-market dislocation where both YES and NO bids imply
an apparent scalp opportunity. This may be the exact high-delta near-strike
pattern the user identified.

## Decision

Do **not** reject crossed/complementary readings in the ATM paper observer.

Instead:

- Continue observing and logging them as paper-only ATM candidates.
- Label them explicitly as crossed/complementary execution-verification rows
  in status/reporting.
- Do not promote them to live trading until real order tests prove fills are
  available near the displayed derived ask/bid.

## Test Coverage

- `tests/test_atm_reversion.py::test_edge_tie_prefers_yes`
- `tests/test_atm_reversion.py::test_rejects_invalid_inputs`

Both green at 21/21 ATM tests passing.

## Implications For Future Strategies

Any strategy that consumes Kalshi book quotes must distinguish:

- ordinary one-sided quote sanity
- complementary bid-derived ask dislocations
- actual executable fill proof

If we extend the live ATM tier, the first live step should be tiny-size
execution verification with explicit logs:

- signal yes_bid / yes_ask / no_bid / no_ask
- submitted side / price / count
- accepted/rejected
- filled_count / avg_fill_price
- time-to-fill

No size scaling from paper crossed-book PnL until those fills are proven.

