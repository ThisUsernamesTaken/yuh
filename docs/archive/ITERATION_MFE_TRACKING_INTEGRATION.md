# Iteration: Native MFE/MAE Tracking Integration

**Status**: Stage 1 complete (historical backfill). Stage 2 pending.  
**File to implement**: `polymarket_copy_engine.py`, `signal_logger.py`  
**Do not implement until ready to restart the engine.**

---

## 1. Background

MFE (Maximum Favorable Excursion) and MAE (Maximum Adverse Excursion) measure how far a trade moved *in your favor* and *against you* before it closed. They answer the most important TP/stop calibration questions:

- Is the current TP level (65/70/75c sell ladder) capturing the full move?  
  → If median MFE peaks at 82c but TP ladder tops out at 75c, the runner tier is leaving money on the table.
- Are stops triggering before positions recover?  
  → If MAE median is 12c but MFE median is 18c, positions that dip 12c usually go on to win.
- What is the trade quality ratio?  
  → MFE/MAE > 1.5 indicates directional edge. MFE/MAE < 1.0 indicates thesis failing early.

The swing analysis of historical trades showed that the engine was consistently right on direction but capped too early on winners. Quantifying MFE/MAE per entry band will let you calibrate TP levels and decide whether any stop at all is justified.

---

## 2. Two-Stage Approach

### Stage 1 — Historical backfill (already complete)

Script: `btc-bias-engine/backfill_mfe_mae.py`

Reads all closed trades from `kalshi_trades`, fetches 1-minute Kalshi candlesticks for each trade's window, computes MFE/MAE in cents, writes results to `trade_excursions` table.

Run: `python backfill_mfe_mae.py`  
Dry run: `python backfill_mfe_mae.py --limit 5`

The `trade_excursions` table is the source of truth for the SQL analysis queries in section 6.

**Limitation of Stage 1**: Candlestick data may be unavailable for very old or expired contracts (404). The backfill handles these gracefully (tombstone row, candles_count=0).

### Stage 2 — Native in-engine tracking (this iteration)

Track HWM/LWM in real time as the engine polls the order book, then persist them to `kalshi_trades` at trade close. This gives tick-by-tick accuracy instead of 1-minute candle granularity, and will continue to accumulate data going forward without relying on the Kalshi candlestick API.

---

## 3. Stage 2 Specific Changes

### 3a. `signal_logger.py` — Add columns to kalshi_trades

**File**: `btc-bias-engine/signal_logger.py`

**Step 1**: Add four columns to the `_CREATE_KALSHI_TRADES_TABLE` constant (around line 32). Add after the last existing column:

```python
    hwm_cents         INTEGER,   -- highest yes_bid seen while position was open (cents)
    lwm_cents         INTEGER,   -- lowest  yes_bid seen while position was open (cents)
    mfe_cents         INTEGER,   -- max favorable excursion: YES=(hwm-entry), NO=(entry-lwm)
    mae_cents         INTEGER,   -- max adverse excursion:  YES=(entry-lwm), NO=(hwm-entry)
```

**Step 2**: Add these four columns to the migrations list (around line 189) so existing databases get the columns on next startup:

```python
("kalshi_trades", "hwm_cents",  "INTEGER"),
("kalshi_trades", "lwm_cents",  "INTEGER"),
("kalshi_trades", "mfe_cents",  "INTEGER"),
("kalshi_trades", "mae_cents",  "INTEGER"),
```

**Step 3**: Update `log_kalshi_outcome()` signature (around line 283) to accept the new values:

```python
async def log_kalshi_outcome(
    self,
    order_id: str,
    is_win: bool,
    pnl: float,
    filled_count: int,
    result: str,
    status_override: str = "",
    hwm_cents: Optional[int] = None,   # ADD
    lwm_cents: Optional[int] = None,   # ADD
    mfe_cents: Optional[int] = None,   # ADD
    mae_cents: Optional[int] = None,   # ADD
) -> None:
```

**Step 4**: Update the `UPDATE kalshi_trades` statement inside `log_kalshi_outcome()` to include the new columns:

```python
await db.execute(
    """
    UPDATE kalshi_trades
    SET status=?, pnl=?, filled_count=?, result=?,
        hwm_cents=?, lwm_cents=?, mfe_cents=?, mae_cents=?
    WHERE order_id=?
    """,
    (status, pnl, filled_count, result,
     hwm_cents, lwm_cents, mfe_cents, mae_cents,
     order_id),
)
```

---

### 3b. `polymarket_copy_engine.py` — Fix HWM tracking gaps

**File**: `btc-bias-engine/polymarket_copy_engine.py`

#### Problem

The current HWM/LWM tracking block lives at lines **6064–6074**, inside the NO-BOUNCE EXIT section near the *bottom* of `_manage_position`. This means:

1. The tracking only runs after the sell-ladder check, the priced-in check, the profit-lock check, and several other early-return paths. If any of those early paths fire, the HWM/LWM values **never get updated** for that poll cycle.
2. Several position initialization sites do not set `low_water_bid` (e.g., lines 676–679, 1353–1357, 2330–2334, 4746–4750). When those positions close via an early-exit path, `low_water_bid` defaults to 0 or `entry`, producing invalid MAE.

#### Fix A — Move HWM/LWM update to the top of the position polling block

Find the section in `_manage_position` where `bid` is first fetched from the order book (after the Kalshi position sync block, around line 5643). Immediately after `bid` is assigned, add an unconditional HWM/LWM update:

```python
# ── MFE/MAE: unconditional update every poll cycle ──
_hwm = pos.get("high_water_bid", entry_cents)
_lwm = pos.get("low_water_bid", entry_cents)
if bid > 0:
    if bid > _hwm:
        pos["high_water_bid"] = bid
    if _lwm == 0 or bid < _lwm:
        pos["low_water_bid"] = bid
```

Remove (or keep as secondary) the existing block at lines 6064–6074. Having it in both places is harmless but redundant.

#### Fix B — Initialize `low_water_bid` in all position dicts

Search for all `"high_water_bid":` assignments (there are ~7 sites) and add `"low_water_bid": <same_value>,` on the next line wherever it is missing. Sites to patch:

| Approx. line | Context |
|---|---|
| 678 | Wallet/copy entry initialization |
| 1356 | Sniper entry |
| 2333 | INSTANT tier entry |
| 4749 | PENDING_FILL initialization |
| 5617 | SYNCED ghost position (disabled path, but safe to add) |
| 5793 | FLIP_REENTRY position |

Line **4868** already has `"low_water_bid": entry_price,` — leave it alone.

---

### 3c. `polymarket_copy_engine.py` — Pass HWM/LWM to `_record_trade_outcome`

#### Current state

`_record_trade_outcome` (line 5374) already reads `high_water_bid` and `low_water_bid` from `self._open_position` and writes them to `trade_decisions` (lines 5389–5416). However, it does **not** pass them to `log_kalshi_outcome`, so `kalshi_trades` never gets populated.

#### Fix — Compute and pass excursions inside `_record_trade_outcome`

In `_record_trade_outcome`, after the existing MFE/MAE block (after line 5416), compute the excursion values and pass them to `log_kalshi_outcome`:

```python
# Compute excursion values to persist to kalshi_trades
_hwm_c = None
_lwm_c = None
_mfe_c = None
_mae_c = None
try:
    if self._open_position:
        _pos_entry = self._open_position.get("entry_cents", 0)
        _pos_side  = self._open_position.get("side", "yes")
        _hwm_c = self._open_position.get("high_water_bid")
        _lwm_c = self._open_position.get("low_water_bid")
        if _hwm_c and _lwm_c and _pos_entry:
            if _pos_side == "yes":
                _mfe_c = _hwm_c - _pos_entry
                _mae_c = _pos_entry - _lwm_c
            else:
                _mfe_c = _pos_entry - _lwm_c
                _mae_c = _hwm_c - _pos_entry
except Exception:
    pass  # Never block a trade close on excursion math
```

Then update the `log_kalshi_outcome` call (around line 5442):

```python
await self._signal_logger.log_kalshi_outcome(
    order_id=order_id,
    is_win=is_win,
    pnl=pnl,
    filled_count=count,
    result=result,
    status_override=status_override,
    hwm_cents=_hwm_c,    # ADD
    lwm_cents=_lwm_c,    # ADD
    mfe_cents=_mfe_c,    # ADD
    mae_cents=_mae_c,    # ADD
)
```

Because this is inside a `try/except Exception` block (line 5441), any failure is already silenced. The non-blocking pattern is preserved.

---

## 4. Per-Exit-Path Checklist

All exits funnel through `_record_trade_outcome`. The checklist below confirms which exit paths call it, so nothing is missed when testing Stage 2.

| Exit path | Status | Approx. line | Calls `_record_trade_outcome`? |
|---|---|---|---|
| **Sell ladder fill** (TP 65/70/75c tiers) | **ACTIVE** | ~5700 | Yes — `"won"` or `"exited_loss"` |
| **Hard close** (<0.5 min left) | **ACTIVE** | ~6187 | Yes — `"won"` or `"exited_loss"` |
| **Mandatory exit** (underwater, 3 min left) | **ACTIVE** | ~6225 | Yes — `"exited_loss"` |
| **Stale position purge** (window change) | **ACTIVE** | ~2552 | Yes — `"lost"` |
| **Flip-invert wallet exit** | Disabled (`weighted_flip=False`) | ~6317 | Yes — `"exited_win"/"exited_loss"` |
| **MTF reversal exit** | Check engine state | ~5920 | Yes — `"mtf_reversal"/"won"` |
| **Stopped** (catastrophic stop) | Disabled | ~5889 | Yes — `"stopped"` |
| **Priced-in exit** (≥75c) | Disabled (`if False`) | ~5991 | Yes — `"won"` |
| **Profit lock** (≥12 min profitable) | Disabled (`if False`) | ~6021 | Yes — `"won"` |
| **Time exit** (≥10 min) | Disabled (`if False`) | ~6049 | Yes — `"won"/"time_exit"` |
| **No-bounce exit** (hwm < entry+8 by min 7) | Disabled (`if False`) | ~6091 | Yes — `"won"/"nobounce_exit"` |
| **Mid-extreme exit** | Disabled (`if False`) | ~6159 | Yes — `"extreme_exit"` |
| **Expiry settlement** | Via reconcile | ~5498 | Yes — `"reconciled_unknown"` |
| **Expired pending** (contract expired) | Via reconcile | ~5848 | Yes — `"expired_pending"` |

**All active paths already call `_record_trade_outcome`.** The single change in section 3c covers all of them.

---

## 5. Validation Plan

### After restart

1. Wait for the first completed trade after the engine restarts.

2. Query the database:
   ```sql
   SELECT ticker, side, limit_price AS entry_cents,
          hwm_cents, lwm_cents, mfe_cents, mae_cents, status
   FROM kalshi_trades
   WHERE hwm_cents IS NOT NULL
   ORDER BY id DESC
   LIMIT 5;
   ```
   Expected: `hwm_cents` is populated (≥ entry_cents for YES, ≤ entry_cents for NO) and `mfe_cents` is non-negative.

3. Verify `lwm_cents` is populated and `mae_cents` is non-negative.

4. Spot-check logic:
   - For a YES trade with `entry_cents=50`: `hwm_cents >= 50`, `mfe_cents = hwm_cents - 50`, `mae_cents = 50 - lwm_cents`.
   - For a NO trade with `entry_cents=45`: `lwm_cents <= 45`, `mfe_cents = 45 - lwm_cents`, `mae_cents = hwm_cents - 45`.

### Cross-validate Stage 1 vs Stage 2

For trades placed after the Stage 2 restart, both `trade_excursions` (Stage 1 source) and `kalshi_trades` (Stage 2 source) should have excursion data. Compare them:

```sql
SELECT
    k.id,
    k.ticker,
    k.mfe_cents    AS live_mfe,
    e.mfe_cents    AS candle_mfe,
    k.mfe_cents - e.mfe_cents AS mfe_diff
FROM kalshi_trades k
JOIN trade_excursions e ON e.trade_rowid = k.rowid
WHERE k.mfe_cents IS NOT NULL AND e.mfe_cents IS NOT NULL
ORDER BY k.id DESC
LIMIT 20;
```

Expected: `mfe_diff` within ±5c (live tracking is tick-level; candles are 1-minute averages, so they naturally diverge slightly on fast-moving contracts).

If `mfe_diff` is consistently large (> 10c), investigate whether the HWM update moved to the top of the loop correctly (Fix A).

---

## 6. SQL Analysis Queries

Run these after at least 50 live-tracked trades accumulate in `kalshi_trades`.

### Median MFE by entry band
```sql
SELECT
    CASE
        WHEN limit_price < 40 THEN '<40c'
        WHEN limit_price < 50 THEN '40-49c'
        WHEN limit_price < 60 THEN '50-59c'
        ELSE '60c+'
    END AS entry_band,
    COUNT(*)                                    AS trades,
    ROUND(AVG(mfe_cents), 1)                    AS avg_mfe,
    -- SQLite has no PERCENTILE_CONT — use AVG as proxy for median
    ROUND(AVG(mae_cents), 1)                    AS avg_mae,
    ROUND(AVG(CAST(mfe_cents AS REAL) / NULLIF(mae_cents, 0)), 2) AS mfe_mae_ratio,
    SUM(CASE WHEN pnl > 0 THEN 1 END) * 100 / COUNT(*) AS win_pct
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY entry_band
ORDER BY entry_band;
```

### What % of trades had MFE above current TP levels?
```sql
-- TP ladder tops out at 75c. entry_cents varies. So the question is:
-- What % of trades peaked at (entry + X) cents? 
SELECT
    tp_gain,
    COUNT(*) AS trades,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_total
FROM (
    SELECT
        CASE
            WHEN mfe_cents >= 40 THEN '+40c (runner beyond TP3)'
            WHEN mfe_cents >= 25 THEN '+25-39c (hit TP3 at 75c)'
            WHEN mfe_cents >= 15 THEN '+15-24c (hit TP1/2)'
            WHEN mfe_cents >= 5  THEN '+5-14c (in-the-money, no TP fill)'
            ELSE '<5c (never really moved)'
        END AS tp_gain
    FROM kalshi_trades
    WHERE mfe_cents IS NOT NULL AND status NOT IN ('pending', 'unfilled')
) sub
GROUP BY tp_gain
ORDER BY tp_gain DESC;
```

### How far above current TP does the average MFE peak?
```sql
-- Current TP3 is 75c, so for a 50c entry the gain target is +25c.
-- What is the average MFE for each entry band vs. the TP target?
SELECT
    ROUND(limit_price, -1) AS entry_bucket,
    COUNT(*) AS n,
    ROUND(AVG(mfe_cents), 1) AS avg_mfe_c,
    -- TP3 target for this entry: (75 - entry)
    ROUND(AVG(mfe_cents) - (75 - limit_price), 1) AS avg_mfe_above_tp3
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL AND status NOT IN ('pending', 'unfilled')
  AND limit_price BETWEEN 35 AND 80
GROUP BY entry_bucket
HAVING n >= 10
ORDER BY entry_bucket;
```

### MFE vs MAE ratio by strategy (trade quality indicator)
```sql
SELECT
    COALESCE(strategy_name, 'unknown') AS strategy,
    COUNT(*) AS n,
    ROUND(AVG(mfe_cents), 1) AS avg_mfe,
    ROUND(AVG(mae_cents), 1) AS avg_mae,
    ROUND(AVG(CAST(mfe_cents AS REAL) / NULLIF(mae_cents, 0)), 2) AS mfe_mae_ratio,
    ROUND(AVG(pnl), 2) AS avg_pnl
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL
  AND mae_cents IS NOT NULL
  AND mae_cents > 0
  AND status NOT IN ('pending', 'unfilled')
GROUP BY strategy
HAVING n >= 5
ORDER BY mfe_mae_ratio DESC;
```

### Optimal TP calibration
```sql
-- What TP level would have captured the most value per trade?
-- Shows the distribution of MFE peaks to find the right TP ceiling.
SELECT
    mfe_bucket,
    COUNT(*) AS trades,
    ROUND(100.0 * SUM(COUNT(*)) OVER (ORDER BY mfe_order) / SUM(COUNT(*)) OVER (), 1) AS cumulative_pct
FROM (
    SELECT
        CASE
            WHEN mfe_cents < 10  THEN '00-09c'
            WHEN mfe_cents < 20  THEN '10-19c'
            WHEN mfe_cents < 30  THEN '20-29c'
            WHEN mfe_cents < 40  THEN '30-39c'
            WHEN mfe_cents < 50  THEN '40-49c'
            ELSE '50c+'
        END AS mfe_bucket,
        CASE
            WHEN mfe_cents < 10  THEN 1
            WHEN mfe_cents < 20  THEN 2
            WHEN mfe_cents < 30  THEN 3
            WHEN mfe_cents < 40  THEN 4
            WHEN mfe_cents < 50  THEN 5
            ELSE 6
        END AS mfe_order
    FROM kalshi_trades
    WHERE mfe_cents IS NOT NULL AND status NOT IN ('pending', 'unfilled')
) sub
GROUP BY mfe_bucket, mfe_order
ORDER BY mfe_order;
```

---

## 7. Risks and Rollback

### What happens if Stage 2 has a bug?

- All excursion code is inside `try/except Exception: pass` blocks in `_record_trade_outcome`.
- A bug in the excursion computation will silently produce `NULL` values in `kalshi_trades`, not a crash.
- The trade close, P&L logging, and status update all happen independently and will not be blocked.
- The engine will continue trading normally; only the new columns will be unpopulated.

### Rollback

1. The new columns (`hwm_cents`, `lwm_cents`, `mfe_cents`, `mae_cents`) are `NULL`-able with no DEFAULT. If Stage 2 is reverted, existing rows silently stay NULL — no data corruption.
2. `signal_logger.py` changes are backward-compatible: `log_kalshi_outcome` new params all default to `None`, so any call site not yet updated will work unchanged.
3. If the HWM update placement (Fix A) causes unexpected behavior, revert the move. The original block at lines 6064–6074 still fires on every poll cycle that reaches that section (all active paths do, since the disabled exits use `if False`).

### Data integrity note

Stage 1 (`trade_excursions`) and Stage 2 (`kalshi_trades` columns) are independent sources. Do not try to keep them in sync programmatically. Use Stage 1 for historical data, Stage 2 for live-forward data. The cross-validation query in section 5 bridges them for verification.
