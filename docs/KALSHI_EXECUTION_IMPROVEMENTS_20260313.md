# Kalshi Contract Purchasing Improvements
**Date:** 2026-03-13 (Session 05)
**Scope:** All changes relate to how the engine selects, prices, and executes Kalshi contracts.

---

## Summary of 8 Improvements

### 1. Regime × Session Win Rate Table (signal_intelligence.py)

**Problem:** A single flat 63.5% floor was used for all `adjusted_confidence` calculations,
regardless of which regime/session we were in. This meant:
- RANGING × NY-Prime (71.3% WR) was sized the same as TRENDING_DOWN × NY-Prime (58.0% WR)
- Edge checks used the wrong confidence → wrong go/no-go decisions at edge cases

**Fix:** Added `_REGIME_SESSION_WR` lookup table (empirical from 90-day analysis) and
`regime_session_wr(regime, utc_hour)` function. The cell-specific WR now floors
`adjusted_confidence` instead of the flat 63.5%.

```python
# Example values (full table in signal_intelligence.py):
RANGING    × NY-Prime  = 71.3%  ← best cell
RANGING    × London    = 67.5%
TREND_DOWN × NY-Open   = 67.4%
RANGING    × NY-Open   = 66.8%
TREND_DOWN × NY-Prime  = 58.0%  ← lowest reliable cell
```

**Impact:** Position sizing now scales appropriately with expected WR per context.
A RANGING × NY-Prime trade gets sized with 71.3% confidence floor vs 58.0% for
TRENDING_DOWN × NY-Prime.

---

### 2. Fill Guarantee: Limit Price = Ask (position_manager.py)

**Problem:** We were placing limit orders at `(bid + ask) / 2` — the mid-market price.
This puts our order on the bid side of the book and it may **never fill** before the
contract expires. If YES_bid=40¢ and YES_ask=44¢, our mid order of 42¢ sits below the
ask and waits for a seller willing to accept 42¢.

**Fix:** Changed limit price to `ask * 100` (rounded to cents). We now cross the spread
and buy at the ask price, matching immediately against existing resting sellers.

```python
# BEFORE (may not fill):
mid = (bid + ask) / 2.0
limit_price = round(mid * 100)  # e.g. 42¢ when ask=44¢

# AFTER (fills immediately):
limit_price = round(ask * 100)  # e.g. 44¢ — at the ask
```

**Tradeoff:** We pay ~1-2¢ more per contract but guarantee execution. For a $0.57 trade,
the difference is negligible. Unfilled orders are wasted opportunities.

---

### 3. Bid-Ask Spread Filter (position_manager.py)

**Problem:** Wide bid-ask spreads indicate illiquid contracts where the implicit slippage
cost is high. If bid=20¢ and ask=40¢, the 20¢ spread means we're immediately giving
up 10¢ of value at entry.

**Fix:** Added `max_spread_cents` filter (default 20¢). Contracts with `(ask - bid) * 100 > 20`
are rejected before sizing.

```python
spread_cents = (ask - bid) * 100.0
if spread_cents > self._max_spread_cents:
    logger.info("Trade rejected: spread %.0f¢ > max %.0f¢", ...)
    return None
```

**Config:** `MAX_SPREAD_CENTS = 20.0` in `config.py`. Most liquid KXBTC15M contracts
trade with 2-5¢ spreads. Contracts early in their session may have wider spreads.

---

### 4. Volume Gate (position_manager.py)

**Problem:** Very new or unpopular contracts can have near-zero volume. Low volume means
no counterparties for our order and potential for poor fills even at ask price.

**Fix:** Added `min_volume` filter (default 25 total contracts traded). Contracts with
`volume < 25` are rejected.

```python
if contract.volume < self._min_volume:
    logger.info("Trade rejected: volume %d < min %d", ...)
    return None
```

**Config:** `MIN_CONTRACT_VOLUME = 25` in `config.py`.

---

### 5. Minimum Minutes Remaining: 2.0 → 4.0 (config.py, position_manager.py)

**Problem:** With `min_minutes_remaining=2.0`, we could enter a contract with only 2
minutes left. At 2 min remaining:
- If the contract has already drifted strongly one way, the winner is pricing at 85-95¢
- Edge is negative for any entry (we can't profit at that price on 63.5% WR)
- We're risking capital on residual noise, not a clean directional read

**Fix:** Raised `MIN_MINUTES_REMAINING` from 2.0 to 4.0 minutes. At 4+ min remaining
there is still meaningful time for the underlying direction to play out.

```python
# config.py
MIN_MINUTES_REMAINING: float = 4.0   # was 2.0
```

---

### 6. Kalshi Trade Logging to DB (signal_logger.py, main.py)

**Problem:** `ledger.py` was trying to read from a `trades` table that had the wrong schema
(Phase 1/2 era fields from Pine Script era). No Kalshi-specific order data was ever
written to the DB. `python ledger.py` always showed "No trades recorded yet."

**Fix:**
1. Added `kalshi_trades` table to `trades.db` with Kalshi-specific columns:
   `order_id, placed_at, ticker, side, count, limit_price, dollar_risk, status, pnl, filled_count, result`

2. Added `log_kalshi_trade()` to `SignalLogger` — called immediately after a successful
   `place_order()` in `main.py`.

3. Updated `ledger.py` to read from `kalshi_trades` with P&L and W/L summary footer.

**Ledger output (once trades flow):**
```
Time (UTC)             Ticker                              Side  Qty  Price    Risk Status     P&L       Order ID
-----------------------------------------------------------------------------------------------------------------
2026-03-13 22:16:00    KXBTC15M-26MAR132230-30             NO      1    57c   $0.57 won        +$0.43    abc123-...
2026-03-13 22:01:00    KXBTC15M-26MAR132215-15             YES     1    42c   $0.42 lost       -$0.42    def456-...

Total trades shown: 2  |  Settled: 2  |  W/L: 1/1  |  P&L: +$0.01
```

---

### 7. Outcome Tracking Coroutine (main.py)

**Problem:** The engine had no mechanism to know if its trades won or lost. This meant:
- `PositionManager.equity` was never updated with actual P&L
- `WinRateTracker` was blind — degradation detection never fired
- `DailyState.realized_pnl` was always 0.0 — kill switch was useless
- `kalshi_trades.status` remained "pending" forever

**Fix:** Added `_outcome_poller()` background coroutine (runs every 60 seconds via
`asyncio.create_task`). It calls two sub-routines:

**`_check_and_cancel_stale_orders()`:** For any open orders where the contract has
< 2 minutes left, fetches the order status. If still "resting" (unfilled), cancels it
and records a zero-impact outcome to clean up tracking state.

**`_settle_expired_orders()`:** For contracts past their expiry + 90s grace period:
1. Fetches `contract.result` ("yes" or "no")
2. Fetches `order.filled_count` and `order.average_price`
3. Calculates P&L: `filled_count × (1.0 - fill_price/100)` for wins, `-fill_count × (fill_price/100)` for losses
4. Calls `position_manager.record_outcome()` → updates equity + daily P&L + kill switch
5. Calls `signal_filter.record_outcome()` → feeds `WinRateTracker` per confidence bucket
6. Calls `signal_logger.log_kalshi_outcome()` → updates `kalshi_trades.status` + pnl

```
Outcome: WIN | KXBTC15M-26MAR132230-30 | filled=1 @ 57¢ | pnl=+0.43 | equity=26.24
```

---

### 8. Stale Resting Order Cancellation (main.py)

**Problem:** If a limit order was placed at 57¢ but the market moved quickly to 30¢ (YES
crashed), no counterparty will accept our NO at 57¢. The order sits as "resting" until
the contract expires — then Kalshi cancels it automatically, but we've held capital for
the full duration doing nothing.

**Fix:** Built into `_outcome_poller` via `_check_and_cancel_stale_orders()`. Orders with
< 2 min remaining that are still "resting" are explicitly cancelled via `cancel_order()`.
The trade is recorded as unfilled (zero P&L) and removed from open orders tracking.

---

## Files Modified

| File | Changes |
|------|---------|
| `config.py` | Added `MIN_MINUTES_REMAINING=4.0`, `MAX_SPREAD_CENTS=20.0`, `MIN_CONTRACT_VOLUME=25` |
| `signal_intelligence.py` | Added `_REGIME_SESSION_WR` table, `_session_label()`, `regime_session_wr()` function; replaced flat 63.5% floor with `regime_session_wr(regime, utc_hour)` |
| `position_manager.py` | Limit price → ask; spread filter; volume filter; min_minutes → 4.0; removed `aggressive_price` param |
| `signal_logger.py` | Added `kalshi_trades` table; added `log_kalshi_trade()` and `log_kalshi_outcome()` |
| `ledger.py` | Reads from `kalshi_trades`; adds P&L column; adds W/L summary footer; fixed timestamp formatting |
| `main.py` | Added `import time`; `_outcome_poller()` coroutine with `asyncio.create_task`; `_check_and_cancel_stale_orders()`; `_settle_expired_orders()`; `log_kalshi_trade()` call after order placement |

---

## What Changes at Runtime

Before session-05:
- Limit orders placed at mid-market → often rested, sometimes expired unfilled
- No trade data in `kalshi_trades` DB → `ledger.py` always empty
- Equity tracking frozen at starting value
- WinRateTracker never received any data → degradation detection blind
- RANGING contracts were blocked → our best regime missed

After session-05:
- Orders placed at ask → immediate fills
- Every order logged to `kalshi_trades` immediately
- Outcome checked 60s after expiry → equity, WR tracker, and DB all updated
- Stale resting orders cancelled before expiry
- Cell-specific confidence floors → more accurate edge and sizing

---

## Remaining Next Steps

1. **Regime-aware count scaling** — Currently always `count=1`. Scale up in high-WR cells
   (RANGING × NY-Prime = 71.3%) and keep minimum in marginal cells.
2. **Re-backtest with new filters** — Run a `Strategy F` incorporating spread, volume,
   and timing filters to verify expected WR improvement numerically.
3. **Balance sync** — Pull live Kalshi balance at startup and sync `STARTING_EQUITY`
   so position sizing reflects actual account balance.
