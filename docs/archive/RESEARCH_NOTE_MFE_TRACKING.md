# RESEARCH NOTE — Implementing MFE/MAE Tracking for Take Profit Optimization — Do Not Implement Without Review

**Generated**: 2026-04-08  
**Depends on**: `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md`, `DYNAMIC_PROFIT_PROTECTION.md`  
**Blocks**: Data-driven TP optimization for all entry bands

---

## 1. What is MFE/MAE and Why We Need It

**MFE (Maximum Favorable Excursion)** — the highest contract bid price reached during a position's lifetime, regardless of exit price. For a YES position, this is the peak bid seen between entry and close.

**MAE (Maximum Adverse Excursion)** — the lowest contract bid price reached during a position's lifetime. The worst drawdown the position experienced before resolution.

**The blocking question** identified in `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md` Section 5:

> "A TP at 80¢ for a 45¢ entry might fill 0% or 40% of the time — both are plausible and we cannot distinguish."

Without MFE, we cannot answer:
- When I enter YES at 45¢, does the contract's bid actually reach 70¢ before expiry on winning trades?
- What fraction of 45¢-entry losing trades touched 65¢ on the way down?
- Is there a price level where, if we post a limit sell, it would fire on 80%+ of eventual winners?

Every TP recommendation in `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md` is built on structural intuition and sparse exit data. MFE converts that into a data-driven optimization problem with a clean answer.

**MAE is a secondary priority** but useful for stop calibration: it tells you how deep drawdowns go on losing trades before they close, confirming or contradicting the current "hold to expiry" decision.

---

## 2. Current State of HWM Tracking in the Engine

### 2.1 `high_water_bid` Already Exists

The position dict already has `high_water_bid`. It is initialized at fill time in at least six places:

| Line | Context |
|------|---------|
| 678 | Initial position dict construction (ladder fill) |
| 1356 | Fill detection path |
| 2210 | Flip-invert new position init |
| 4626 | Orphan position recovery (startup sync) |
| 5461 | Alternate fill path |
| 5637 | Flip-invert alternate init |

All initializations set `high_water_bid = entry_cents` (or the fill price), which is correct.

### 2.2 The Bug: HWM Only Updates in Conditional Branches

There are three places where `high_water_bid` is updated during position management:

**Location A — Line 5910–5914** (no-bounce exit block, pre-`if False`):
```python
hwm = pos.get("high_water_bid", entry)
if bid > hwm:
    pos["high_water_bid"] = bid
    hwm = bid
```
This update is **unconditional** — it runs every cycle regardless of flow state. This is the one place HWM is updated correctly.

**Location B — Line 6286–6288** (inside `if flow_supports:` block):
```python
hwm = pos.get("high_water_bid", pos["entry_cents"])
if bid > hwm:
    pos["high_water_bid"] = bid
```
**The bug**: this update only runs when `flow_supports` is True. When wallet flow goes stale (wallets silent, `flow_has_data = False`) the code falls through to the trailing stop block without updating HWM. If a contract runs from 45¢ to 78¢ during a silence window, HWM stays frozen at whatever it was when flow last confirmed. HWM is understated.

**Location C — Line 6454–6457** (inside `if False and effective_stop > 0:` disabled trailing stop):
```python
hwm = pos.get("high_water_bid", pos["entry_cents"])
if bid > hwm:
    pos["high_water_bid"] = bid
    hwm = bid
```
This block is permanently disabled. Never runs.

### 2.3 What Is Currently Logged vs. What's Missing

**Currently logged on exit** (via `log_kalshi_outcome` in `signal_logger.py` line 304):
```sql
UPDATE kalshi_trades SET status=?, pnl=?, filled_count=?, result=? WHERE order_id=?
```
No HWM, no MFE, no MAE. Zero excursion data written to DB.

**In-memory only**: `high_water_bid` lives in `self._open_position` dict. It is tracked in RAM during the position's lifetime, but is discarded when the position closes — never flushed to the DB.

**DB schema confirmation**: Query `pragma_table_info('kalshi_trades')` for any column matching `high`, `hwm`, `mfe`, `mae`, `max`, `min`, or `excursion` returns **zero rows**. The schema has no excursion columns.

### 2.4 Does `main.py` Track Excursion Data?

`main.py` is the disabled consensus engine orchestrator (CLAUDE.md: "Signal anti-correlated with market (0% WR day 1)"). It does not write to `kalshi_trades`. The `_outcome_poller` mentioned in prior audits is part of `main.py`'s infrastructure and is not active. No excursion data comes from that path.

---

## 3. What Needs to Change (Specific, Actionable)

### a) Fix HWM Update to Be Unconditional

**Current location**: Line 6286–6288 inside `if flow_supports:` block. The update happens only when wallet flow is actively supporting the position.

**Where it should move**: Extract the HWM update from inside `if flow_supports:` and place it at the **top of the main management cycle body** — before the flow check, before any exit logic. The pattern already exists correctly at line 5910 in the no-bounce block; replicate that pattern at the start of the manage loop.

Do not duplicate; move. The line 5910 update in the no-bounce block will still run earlier in the function. The line 6286 update inside `if flow_supports:` should be moved out so it runs regardless of flow state.

### b) Add `low_water_bid` Tracking Alongside `high_water_bid`

**Initialization**: In every place that initializes `high_water_bid` (the six lines listed in 2.1), add a parallel initialization: `"low_water_bid": entry_cents` (same value as entry at open).

**Update location**: Same place as the corrected HWM update in (a) — unconditional, at top of manage cycle. Pattern:
```
if bid < pos["low_water_bid"]:
    pos["low_water_bid"] = bid
```

### c) Log Both Values to `kalshi_trades` on Every Exit Path

`_record_trade_outcome` (line 5247) calls `signal_logger.log_kalshi_outcome`. That function currently takes only `order_id`, `is_win`, `pnl`, `filled_count`, `result`, `status_override`.

Two options:
1. Add `hwm_cents` and `lwm_cents` parameters to `log_kalshi_outcome` and pass them from `_record_trade_outcome`.
2. Add a separate `_flush_excursion_data` call before each exit that does its own DB update.

Option 1 is cleaner. `_record_trade_outcome` already has access to `self._open_position` where `high_water_bid` and `low_water_bid` live (see line 5273: `order_id = self._open_position.get("order_id")`).

**All exit paths that call `_record_trade_outcome`** (and would therefore log MFE/MAE automatically if the function is updated):

| Line | Status Written | Notes |
|------|---------------|-------|
| 2429 | `lost` | Stale position reconciliation |
| 5544 | `exited_win`/`exited_loss` | TP fill detected |
| 5692 | `expired_pending` | Contract expired while pending |
| 5733 | `stopped` | Hard stop |
| 5764 | `exited_win`/`exited_loss` | TP decay exit (<5min) |
| 5794 | `extreme_exit` | Extreme price exit |
| 5835 | `won` | Hold-to-expiry win (contract expired) |
| 5865 | `won` | Hold-to-expiry win (alternate path) |
| 5893 | `won`/`exited_loss` | No-bounce block outcomes |
| 5931 | `won`/`nobounce_exit` | No-bounce disabled block |
| 5999 | `extreme_exit` | Extreme exit second path |
| 6027 | `exited_win`/`exited_loss` | Mandatory exit (<3min) |
| 6065 | `exited_loss` | TP decay lower |
| 6100 | `stopped`/`exited_win` | Stop/win path |
| 6157 | `exited_win`/`exited_loss` | Wallet flip (disabled but present) |
| 6313 | `exited_loss` | Catastrophic stop (disabled) |
| 6369 | `exited_win`/`exited_loss` | BTC deviation stop (disabled) |
| 6420 | `exited_loss` | Wallet backstop (disabled) |
| 6482 | `exited_win`/`exited_loss` | Trailing stop (disabled) |

All 19 call sites feed through `log_kalshi_outcome`. Updating that one function captures all exit paths.

### d) New DB Columns Needed

Three columns on `kalshi_trades`:

| Column | Type | Definition |
|--------|------|-----------|
| `hwm_cents` | INTEGER | Peak bid price seen during position lifetime (raw, in cents) |
| `lwm_cents` | INTEGER | Trough bid price seen during position lifetime (raw, in cents) |
| `mfe_cents` | INTEGER | Maximum Favorable Excursion in cents, calculated as: for YES: `hwm_cents - entry_cents`; for NO: `entry_cents - lwm_cents` |

`mae_cents` (Maximum Adverse Excursion) can be derived at query time as: for YES: `entry_cents - lwm_cents`; for NO: `hwm_cents - entry_cents`. No need for a separate column — it's the inverse of MFE. Store raw hwm/lwm and compute mfe/mae in SQL.

### e) ALTER TABLE Migration in `signal_logger.py` `initialize()`

Add three entries to the `migrations` list at line 189:

```python
("kalshi_trades", "hwm_cents",  "INTEGER"),
("kalshi_trades", "lwm_cents",  "INTEGER"),
("kalshi_trades", "mfe_cents",  "INTEGER"),
```

This follows the exact pattern of every existing migration in the list (lines 190–209). Safe to add — the `try/except` at line 212 silently ignores columns that already exist. On next engine restart, all three columns are added to the existing DB without data loss.

---

## 4. Data Collection Timeline

### 4.1 Trades Per Day

From `kalshi_trades` (past 10 active trading days):

| Date | Resolved Trades |
|------|----------------|
| 2026-04-08 (partial) | 42 |
| 2026-04-07 | 69 |
| 2026-04-06 | 46 |
| 2026-04-05 | 37 |
| 2026-04-04 | 48 |
| 2026-04-03 | 45 |
| 2026-04-02 | 33 |
| 2026-04-01 | 35 |
| 2026-03-31 | 65 |
| 2026-03-30 | 49 |

**Average**: ~47 trades/day (TA_FORCED fills gaps, runs 24/7 since 2026-03-30). High days (65–69) correspond to active BTC movement.

### 4.2 Time to 200 Trades with MFE Data

At 47 trades/day: **~4–5 days** to reach 200 MFE-tracked trades.  
At 47 trades/day: **~10–11 days** to reach 500 trades (statistically meaningful per band).

No historical backfill is possible — MFE data only accumulates from the moment the engine is restarted after the fix lands. The current 1,703 resolved trades are all missing excursion data.

### 4.3 What Analysis Becomes Possible at 200 Trades

- **TP fill probability by entry band**: "If I post at 70¢ for a 45¢ entry, what fraction of trades pass through 70¢?" — answerable.
- **Optimal TP level per band**: Replace the theoretical ranges in `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md` Section 4 with empirical percentile targets.
- **MAE validation of hold-to-expiry**: Confirm that holding is right by showing that losing trades' MAE is already close to 0¢ (they were never recoverable), vs. catastrophic-exit trades where MAE showed deep drawdown.
- **Stop calibration**: If losing trades' LWM clusters around 5–10¢ below entry before recovering, that validates the current -8¢ stop config.

---

## 5. Analysis Queries to Run Once MFE Data Exists

### 5.1 "What % of trades at entry 40–49¢ reach a bid of 70¢+ before expiry?"

```sql
SELECT
    side,
    COUNT(*) AS total,
    SUM(CASE WHEN hwm_cents >= 70 THEN 1 ELSE 0 END) AS reached_70,
    ROUND(100.0 * SUM(CASE WHEN hwm_cents >= 70 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_reached_70
FROM kalshi_trades
WHERE limit_price BETWEEN 40 AND 49
  AND hwm_cents IS NOT NULL
  AND status NOT IN ('pending', 'unfilled', 'expired_pending')
GROUP BY side;
```

### 5.2 "What's the Median MFE by Entry Band?"

```sql
SELECT
    CASE
        WHEN limit_price < 40 THEN '<40c'
        WHEN limit_price < 50 THEN '40-49c'
        WHEN limit_price < 60 THEN '50-59c'
        WHEN limit_price < 70 THEN '60-69c'
        ELSE '70c+'
    END AS entry_band,
    side,
    COUNT(*) AS n,
    ROUND(AVG(mfe_cents), 1) AS avg_mfe,
    -- SQLite has no MEDIAN; use percentile approximation
    MIN(mfe_cents) AS min_mfe,
    MAX(mfe_cents) AS max_mfe
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL
  AND status NOT IN ('pending', 'unfilled', 'expired_pending')
GROUP BY entry_band, side
ORDER BY entry_band, side;
```

For true median, use Python after pulling the data:
```python
import sqlite3, statistics
conn = sqlite3.connect('data/trades.db')
rows = conn.execute("""
    SELECT limit_price, side, mfe_cents FROM kalshi_trades
    WHERE mfe_cents IS NOT NULL AND limit_price BETWEEN 40 AND 49
""").fetchall()
mfe_vals = [r[2] for r in rows]
print("Median MFE (40-49c):", statistics.median(mfe_vals))
```

### 5.3 "What TP Level Captures 80% of MFE?"

```sql
-- For each entry band, find the bid level T such that 80% of trades had hwm_cents >= T
-- Run this per band; adjust the WHERE clause
SELECT
    hwm_cents,
    COUNT(*) OVER () AS total,
    ROW_NUMBER() OVER (ORDER BY hwm_cents) AS rank,
    ROUND(100.0 * ROW_NUMBER() OVER (ORDER BY hwm_cents) / COUNT(*) OVER (), 1) AS percentile
FROM kalshi_trades
WHERE limit_price BETWEEN 40 AND 49
  AND side = 'yes'
  AND mfe_cents IS NOT NULL
  AND status NOT IN ('pending', 'unfilled', 'expired_pending')
ORDER BY hwm_cents;
-- The row where percentile >= 20 gives the 80th percentile MFE (i.e., 80% of trades reached AT LEAST this price)
-- Read from the bottom: find where cumulative count crosses 80% from the top
```

Simpler version — find the 20th percentile of hwm_cents (= price that 80% of trades exceeded):
```sql
SELECT hwm_cents
FROM (
    SELECT hwm_cents,
           NTILE(10) OVER (ORDER BY hwm_cents) AS decile
    FROM kalshi_trades
    WHERE limit_price BETWEEN 40 AND 49
      AND side = 'yes'
      AND mfe_cents IS NOT NULL
      AND status NOT IN ('pending', 'unfilled', 'expired_pending')
)
WHERE decile = 2  -- bottom 20% (10th-20th percentile) — the threshold 80% exceeded
ORDER BY hwm_cents DESC
LIMIT 1;
```

### 5.4 "MFE Distribution by Side (YES vs NO)"

```sql
SELECT
    side,
    COUNT(*) AS n,
    ROUND(AVG(mfe_cents), 1) AS avg_mfe,
    ROUND(AVG(CASE WHEN mfe_cents > 0 THEN mfe_cents END), 1) AS avg_mfe_winners_only,
    SUM(CASE WHEN mfe_cents >= 20 THEN 1 ELSE 0 END) AS trades_with_20c_mfe,
    SUM(CASE WHEN mfe_cents >= 30 THEN 1 ELSE 0 END) AS trades_with_30c_mfe,
    SUM(CASE WHEN mfe_cents >= 40 THEN 1 ELSE 0 END) AS trades_with_40c_mfe
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL
  AND status NOT IN ('pending', 'unfilled', 'expired_pending')
GROUP BY side;
```

### 5.5 "Correlation Between Entry Price and MFE/MAE Ratio"

```sql
SELECT
    limit_price AS entry_cents,
    side,
    COUNT(*) AS n,
    ROUND(AVG(mfe_cents), 1) AS avg_mfe,
    -- MAE for YES = entry - lwm_cents; for NO = hwm_cents - entry
    ROUND(AVG(CASE WHEN side='yes' THEN limit_price - lwm_cents
                   ELSE hwm_cents - limit_price END), 1) AS avg_mae,
    ROUND(AVG(CAST(mfe_cents AS REAL) /
              NULLIF(CASE WHEN side='yes' THEN limit_price - lwm_cents
                          ELSE hwm_cents - limit_price END, 0)), 2) AS mfe_mae_ratio
FROM kalshi_trades
WHERE mfe_cents IS NOT NULL
  AND lwm_cents IS NOT NULL
  AND status NOT IN ('pending', 'unfilled', 'expired_pending')
GROUP BY limit_price, side
HAVING n >= 5
ORDER BY limit_price, side;
```

---

## 6. How MFE Data Feeds Back into TP Optimization

Once MFE data exists, the TP optimization becomes a single well-defined calculation:

**Step 1**: For each entry band (e.g., YES 40–49¢), sort all trades by `hwm_cents` ascending.

**Step 2**: Find the 20th percentile of `hwm_cents` — call it `T`. By definition, 80% of trades in this band saw `hwm_cents >= T`.

**Step 3**: Post the limit sell at `T` cents. On 80% of trades, the contract's bid will pass through `T` during the window and the limit sell will fill (assuming there's a buyer at that price, which on Kalshi is more reliable at round numbers).

**Step 4**: The remaining 20% of trades whose HWM never reached `T` hold to expiry. Some resolve at 100¢ (wins), some at 0¢ (losses). Their EV is captured in the hold-to-expiry data.

**Worked example** (hypothetical, using expected MFE ranges):

Suppose after 300 trades, YES 40–49¢ shows:
- 80th percentile HWM = 68¢ (i.e., 80% of trades in this band saw the bid reach 68¢+)
- Current engine posts TP at ~52¢ (×1.15 of 45¢ entry)
- MFE-calibrated TP: post at **68¢**

At 45¢ entry, posting at 68¢ captures **23¢/contract** instead of 7¢. Even if the limit sell only captures 65% of trades that pass through (thin book), it's still materially better than the current 7¢.

**The 20% of trades that don't reach 68¢** — these mostly resolve at 0¢ (losing direction). They would have been losses under either approach. The TP at 68¢ doesn't make them worse; they just hold to expiry as before.

**The tradeoff**: Posting at the 80th percentile MFE means you're leaving the top 20% of moves on the table (contracts that run from 45¢ to 90¢+). This is correct — the data already shows (Section 2.4 of `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md`) that only 11% of trades exit mid-session. The 90¢ runners will mostly hold to expiry anyway since they move too fast for a resting limit to catch.

**Implementation note**: MFE-calibrated TPs should be re-evaluated monthly as the entry distribution shifts. The engine's current MIN_ENTRY_CENTS=35, MAX_ENTRY_CENTS=82, with TA_FORCED constrained to 40–55¢, means the 40–55¢ band will have the best sample size fastest.

---

## Summary: Implementation Sequence

1. **Add `low_water_bid` initialization** to the six position-init sites (alongside existing `high_water_bid` inits)
2. **Move HWM update unconditional** — extract line 6286–6288 from inside `if flow_supports:`, place before the flow check
3. **Add LWM update unconditional** — same location as corrected HWM update
4. **Add migration columns** to `signal_logger.py` `initialize()` migrations list: `hwm_cents`, `lwm_cents`, `mfe_cents`
5. **Update `log_kalshi_outcome`** to accept and write `hwm_cents`, `lwm_cents`, `mfe_cents`
6. **Update `_record_trade_outcome`** to pass `pos["high_water_bid"]`, `pos["low_water_bid"]`, and calculated `mfe` to `log_kalshi_outcome`
7. **Restart engine** — all three columns are added to the DB via migration on next startup
8. **Wait 10 days** — run Section 5 queries — replace theoretical TP ranges in `RESEARCH_OPTIMAL_TAKE_PROFIT_LEVELS.md` with empirical values
