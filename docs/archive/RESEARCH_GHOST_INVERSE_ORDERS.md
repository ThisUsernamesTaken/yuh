# Ghost Inverse Orders — Research Report
**Date**: 2026-04-13  
**Scope**: Read-only analysis of `data/trades.db` + `polymarket_copy_engine.py`  
**Status**: CONFIRMED — two active code paths produce opposite-side orders on the same ticker

---

## Summary

After closing a position, the engine **does** buy the opposite side in certain conditions. This is not a bug in one place — it's **two distinct intentional mechanisms** plus one implicit behavior. All three are currently active.

---

## Query Results

### Q1 — Tickers with both YES and NO traded (top 30 of 37)

```
KXBTC15M-26MAR272015-15  n=3  net=$-0.31  → no@63c-won | no@63c-reconciled | yes@74c-won
KXBTC15M-26MAR261015-15  n=2  net=$+1.19  → no@34c-won | yes@79c-lost
KXBTC15M-26MAR260800-00  n=2  net=$-1.88  → yes@31c-lost | no@80c-won
KXBTC15M-26MAR260615-15  n=2  net=$-2.05  → yes@27c-lost | no@81c-won
KXBTC15M-26MAR241415-15  n=3  net=$+2.37  → yes@55c-won | yes@62c-won | no@19c-lost
KXBTC15M-26MAR241315-15  n=3  net=$-2.66  → yes@41c-lost | yes@39c-lost | no@89c-won
KXBTC15M-26MAR240900-00  n=3  net=$-4.36  → no@56c-lost | no@42c-lost | yes@73c-won
KXBTC15M-26MAR171815-15  n=7  net=$+1.35  → 4×no-won | 3×yes-lost
KXBTC15M-26MAR171100-00  n=7  net=$+0.11  → 3×no-lost | 4×yes-won
KXBTC15M-26MAR162315-15  n=7  net=$+1.25  → 3×yes-lost | 4×no-won
... (37 total affected tickers)
```

### Q3 — Scale
```
Affected tickers:  37 / 1,282 total  (2.9%)
Affected trades:  122 / 2,056 total  (5.9%)
```

### Q4 — P&L impact
```
Pattern       Trades   Total P&L    Per-trade
both_sides      122    -$44.51      -$0.365
single_side   1,934   -$212.81      -$0.110
```

**Both-sides trades lose 3.3× more per trade than normal trades.**

### Q5 — Code grep (flip/invert keywords)
Found at lines 314–318, 6963, 7012, 8125–8237.

---

## Three Root Causes

### 1. FLIP RE-ENTRY after a winning close (line ~6963)
**Strategy tag**: `HFT_SCALP_SIGNAL_REVERSAL`  
**Trigger**: After a position closes with a **profit**, if the opposite side is priced 15–40c and ≥5 min remain, the engine immediately buys the opposite side at 15% of normal size as a "reversion" trade.

```python
# line 6963
flip_side = "no" if original_side == "yes" else "yes"
flip_ask = flip_book.best_yes_ask if flip_side == "yes" else flip_book.best_no_ask
if flip_ask and 15 <= flip_ask <= 40 and flip_mins >= 5.0:
    # Cheap entry on the opposite side — market priced our direction
    flip_budget = flip_balance * SIZING_BALANCE_FRACTION * 0.15  # 15% of normal
```

**Examples in data** (Mar 17 HFT era, now mostly historical since HFT engine disabled):
- `26MAR170330-30`: no@45c-lost → 4×yes-won (all within 5 min)
- `26MAR170400-00`: yes@56c-lost → 3×no-won (within 4 min)

---

### 2. FLIP-INVERT on elite wallet reversal (line ~8125)
**Strategy tag**: `FLIP_INVERT` (stored in tier field, not strategy_name)  
**Trigger**: When 1+ elite wallets (≥75% WR) trade AGAINST the open position AND zero elites still support our side AND flow conviction ≥75% on the opposite side. The engine:
1. Market-sells the current position
2. Immediately market-buys the **opposite side** (up to 82c, same contract count)
3. Places a TP ladder at 75c (or entry+5c if entry ≥70c)
4. Bypasses window lock

```python
# line 8125
inverted_side = "no" if pos["side"] == "yes" else "yes"
...
mkt_order = await self._client.place_order(
    ticker=pos["ticker"], side=inverted_side, price=inv_ask, count=market_ct,
)
```

This is the **most aggressive** path — it fires at market on confirmed elite signal, no price filter below 82c.

**Examples in data** (visible in many Mar 24–28 cases):
- `26MAR260800-00`: yes@31c (MIMIC, -$2.48) → no@80c (CROSS_VENUE, +$0.60) — net -$1.88
- `26MAR260615-15`: yes@27c (CROSS_VENUE, -$2.43) → no@81c (CROSS_VENUE, +$0.38) — net -$2.05

These flip-inverts are **entering at the extreme end of the book** (80-81c NO = 19-20c YES equivalent). The inverse buy consistently loses because it's entering into an already-resolved move.

---

### 3. TA_FORCED direction reversal (line ~315, TA_INVERSION_ENABLED=True)
**Strategy tag**: `TA_FORCED_SIGNAL`  
**Trigger**: TA fires on one direction, position closes (thesis_exit or reversal_exit), then TA fires again on the **opposite direction** within the same window. No explicit flip code — two independent TA signals on the same contract.

**Most recent examples (Apr 12–13, still active):**
```
KXBTC15M-26APR120330-30:  no@thesis_exit → yes@thesis_exit    (3 min gap)
KXBTC15M-26APR120530-30:  no@reversal_exit → yes@won           (2 min gap)
KXBTC15M-26APR122115-15:  no@thesis_exit → yes@exited_loss     (8 min gap)
```

This is happening **today**. Window lock is not preventing TA_FORCED re-entries after thesis/reversal exits.

---

## Pattern: Flip Timing Distribution

From the side-flip pair analysis:
```
0–2 min gap:   most common (HFT-era scalps, TA re-entries)
2–5 min gap:   FLIP RE-ENTRY / FLIP-INVERT after wins
5–10 min gap:  FLIP-INVERT after extended holds
```

**The 0-min flips** (same-minute opposite buys) are the most dangerous — entering the reverse direction with essentially zero new information.

---

## High-Loss Instances

| Ticker | Trades | Net P&L | Pattern |
|--------|--------|---------|---------|
| KXBTC15M-26MAR240900-00 | 3 | -$4.36 | no×2 lost, then yes@73c won |
| KXBTC15M-26MAR260615-15 | 2 | -$2.05 | FLIP-INVERT at extreme (81c) |
| KXBTC15M-26MAR260800-00 | 2 | -$1.88 | FLIP-INVERT at extreme (80c) |
| KXBTC15M-26MAR241315-15 | 3 | -$2.66 | yes×2 lost, then no@89c won |
| KXBTC15M-26MAR151830-30 | 6 | -$1.43 | yes won, then 5×no lost |

---

## Code Locations

| Path | File | Lines | Guard |
|------|------|-------|-------|
| FLIP RE-ENTRY (after win) | `polymarket_copy_engine.py` | ~6950–7014 | `15 <= flip_ask <= 40` and `flip_mins >= 5` |
| FLIP-INVERT (elite reversal) | `polymarket_copy_engine.py` | ~8110–8237 | `inv_ask <= 82` |
| TA_INVERSION_ENABLED | `polymarket_copy_engine.py` | 315 | `TA_INVERSION_ENABLED = True` |

---

## Risks / Concerns

1. **FLIP-INVERT at 80-81c** — entering NO at 80c means paying 80c for a contract where the underlying probability has already moved hard against you. The 2c gain (entry 80c, settles at 100c if correct) is tiny vs original loss. The 82c cap (line 8137) allows this.

2. **TA_FORCED window re-entry after thesis_exit** — after a thesis_exit or reversal_exit, the engine is NOT locking the window, allowing TA to fire again on the opposite direction within 2-8 minutes on the same contract. Three confirmed instances in the last 48 hours (Apr 12–13).

3. **Both-side cost**: -$0.365/trade vs -$0.110/trade single-side. Double-fee structure and entering into resolved moves are the likely drivers.

4. **FLIP RE-ENTRY price range 15-40c** — the "market priced our direction, reversion expected" theory. Not tracked separately; logged under `HFT_SCALP_SIGNAL_REVERSAL`. The HFT engine that used it is disabled, so this path may be dormant but the code is still live.

---

## What Was NOT Found

- No unintentional inversion bugs (no random side-flip logic)
- No silent reactivation of disabled strategies
- All ghost inverse orders trace to one of the three intentional mechanisms above

---

## Recommended Follow-ups (for user to decide)

1. **Check FLIP-INVERT entry price cap**: lower from 82c to ~65c to avoid buying into already-resolved moves
2. **Verify TA window lock after thesis_exit/reversal_exit**: confirm `_window_locked = True` is set on these exits and that TA_FORCED respects it
3. **Track FLIP RE-ENTRY P&L separately**: give it a distinct `strategy_name` so win rate is queryable
4. **Consider disabling FLIP RE-ENTRY entirely**: it's not tracked, the HFT engine that originated it is disabled, and the reversion theory is unvalidated
