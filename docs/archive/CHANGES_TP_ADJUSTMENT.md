# TP Adjustment: +5c → +8c, Limit Orders Confirmed

**Date**: 2026-04-06
**Requested by**: User — wider TPs (+3c) for limit order sales

---

## What Changed

### Active code (takes effect immediately on restart)

| File | Line | Old | New |
|------|------|-----|-----|
| `user_config.py` | 17 | `TAKE_PROFIT_CENTS = 5` | `TAKE_PROFIT_CENTS = 8` |
| `polymarket_copy_engine.py` | 1325 | `tp_price = fill_px + 5` | `tp_price = fill_px + 8` |
| `polymarket_copy_engine.py` | 1333 | log `+5c` | log `+8c` |
| `polymarket_copy_engine.py` | 4182 | `tp_price = avg_entry + 5` | `tp_price = avg_entry + 8` |
| `polymarket_copy_engine.py` | 4192 | log `+5c` | log `+8c` |

### Installer defaults (affect fresh installs only, not running engine)

| File | Change |
|------|--------|
| `app.pyw` | `TAKE_PROFIT_CENTS = 5 → 8` |
| `app_local.pyw` | `TAKE_PROFIT_CENTS = 5 → 8` |
| `setup_panel.pyw` | `TAKE_PROFIT_CENTS = 5 → 8` |
| `deploy/setup.pyw` | `TAKE_PROFIT_CENTS = 5 → 8` |

### Dead code (consistency updates, no runtime effect)

| File | Change |
|------|--------|
| `polymarket_copy_engine.py` `_flexible_tp()` | All fixed-TP tiers shifted +3c (function is never called) |
| `polymarket_copy_engine.py` `_place_tiered_tp()` | `floor = entry_cents + 5 → 8` (function is inside `if False` block) |

---

## Why

User requested +3c wider TP targets to accommodate limit order fills. Limit orders
rest on the book and require more margin vs. the old +5c which was tight for passive
fills — especially during slow windows where bid needs to run further to hit the target.

---

## How the TP Logic Works Now

There are **two active TP paths**:

### 1. Main entry fill handler (`polymarket_copy_engine.py` ~line 4175)
Runs for every fill (new position AND scale-in after cancelling old TP):
```python
tp_price = avg_entry + 8
place_order(ticker, side, price=tp_price, count=total_count, action="sell")
# order_type defaults to "limit" — rests on Kalshi book
```
Example: entry at 48c → TP limit sell resting at **56c**

### 2. Sniper fill handler (`polymarket_copy_engine.py` ~line 1324)
Runs only when PRE_OPEN_ARB sniper fires at window boundary:
```python
tp_price = fill_px + 8
place_order(ticker, side, price=tp_price, count=filled, action="sell")
# order_type defaults to "limit"
```

### Order type: already limit
`place_order()` signature: `order_type: str = "limit"`. All TP calls omit `order_type`,
so they use the default — **already limit orders**, resting on the Kalshi book until
the bid reaches the target. No market sells for TPs.

`post_only` is NOT set (left `False`). This is correct: TP sells are placed ABOVE the
current bid and rest until the market moves up. There is no risk of crossing the spread
on placement, so `post_only` adds no value and could reject a valid fill if price gaps.

---

## Interaction with Other Exit Logic

| Exit type | Status | TP interaction |
|-----------|--------|----------------|
| Trailing stop | **DISABLED** (`if False`) | None |
| Catastrophic stop | **DISABLED** (`if False`) | None |
| Wallet backstop | **DISABLED** (`if False`) | None |
| Time exit (`< 1min`) | Active (mandatory safety close) | Cancels TP then market sells |
| Contract expiry | Active (settles at 0c or 100c) | If TP never fills, holds to settle |
| `_place_tiered_tp()` | **DISABLED** (`if False`) | Dead — momentum-scaled tiers not in use |

The TP limit order is the **primary and only active exit mechanism** for most trades.
Hold-to-expiry is the fallback if TP never fills.

---

## Config to Tweak if Further Adjustment Needed

To change the TP offset again, edit these two lines in `polymarket_copy_engine.py`:

```python
# Line ~1325 (Sniper path):
tp_price = fill_px + 8      # change 8 to desired offset

# Line ~4182 (Main entry path):
tp_price = avg_entry + 8    # change 8 to desired offset
```

`user_config.py: TAKE_PROFIT_CENTS` is kept in sync for documentation but is not
read by the engine — changing it alone has no effect on live trading.

---

## Impact Assessment

- Wider TP by +3c means **fewer fills** in choppy/flat windows
- But each fill **earns +$0.03 more per contract** vs. old target
- At 40-55c entry range (current config), +8c TP = 14-20% return vs. old 9-12%
- Breakeven win rate shifts: need ~42% WR (vs ~47% at +5c) to cover fees at typical sizing
- No stop losses active → holds are binary (TP fills or settles at expiry)
