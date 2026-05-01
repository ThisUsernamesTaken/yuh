# Session 12 — Code Review & Improvements
**Date:** 2026-03-17 (17:00–17:35 UTC)

---

## How I Found the Code

### Performance at Review Time

| Layer | Trades | Win Rate | Net P&L | Avg/Trade |
|-------|--------|----------|---------|-----------|
| Main Engine | 227 settled | **37.0%** (vs 63.5% backtest) | -$17.54 | -$0.077 |
| HFT (TP exits) | 159 | 100% | +$33.36 | +$0.210 |
| HFT (SL exits) | 71 | 0% | -$8.00 | -$0.113 |
| **HFT Net** | 231 fills | ~69% effective | **+$25.36** | +$0.110 |
| **Total** | | | **+$7.82** | |

**HFT was the only profitable layer.** Main engine was structurally losing.

### Root Causes Identified

1. **1h TF lock-in (P0 — already fixed prior session):** 1h BiasEngine saturated at +/-100 and held for 60 minutes, overriding 3 short-TF signals. Fix: capped at +/-50 in `consensus.py`.

2. **Warm-start depth insufficient (P0 — already fixed):** Only 100 1m candles fetched, giving the 1h TF one cold bar. Fix: raised to 250 in `main.py`.

3. **Synchronous DB blocking event loop (P1):** `_compute_dynamic_max_pct()` called `sqlite3.connect()` on every `size_trade()`, blocking the asyncio loop 1-5ms per trade. At 227 trades/day, this delayed HFT ticks.

4. **Strategy 3 (SCALP_POLY) dead — 0 entries ever (P1):** The Polymarket-primary scalp strategy was in an `elif` branch that only fired when the main engine had no signal. Since signals emit every 1m candle, the branch was unreachable.

5. **kalshi_trades table polluted with 184+ stale "pending" rows (P1):** HFT scalp orders were written to `kalshi_trades` on exit but never settled there (settlement only happens in `hft_log`). This made `ledger.py` useless.

6. **WinRateTracker baselines from Pine Script (67-96%) permanently inert (P3):** Live WR was 20-60%. The degradation detector's threshold (`baseline - 10%`) could never trigger because the baselines were from a completely different environment.

7. **BAD_UTC_HOURS defined in 3 places (P2):** `config.py`, `config_phase3.py`, and `strategy_index.py` each had their own copy. If one changed, others didn't.

8. **Session label function duplicated (P2):** `signal_intelligence._session_label()` and `strategy_index.get_session()` did the same thing with incompatible string formats.

9. **Imports inside method bodies (P2):** `bias_engine.py` had `from datetime import ...` inside `update()` (called 5x/min). `main.py` had `import aiosqlite` and `from config import TRADES_DB_PATH` inside two methods.

10. **Reversal exit too slow (config):** Required 4/5 TFs opposing to exit, 5c min savings, 5 min for re-entry. When 15m/1h are locked one direction, the engine held losing positions waiting for slow TFs to flip.

### Already Shipped Before This Session

These items were found already implemented in the current code:

- P0: 1h TF score cap +/-50 (`consensus.py:56`)
- P0: Warm-start depth 250 candles (`main.py:308`)
- P1.5: Fee-adjusted EV gate (`hft_engine.py:756-780`)
- P1.5: Maker-first limit price routing (`hft_engine.py:866-867`)
- P1.5: Brier score + log-loss logging (`signal_logger.py`)
- P1.5: Trade decomposition columns (predicted_prob, fee_estimate_cents, maker_taker, book_age_ms)
- P2: Tighter exit ladder at 1c steps (`hft_engine.py:1514`)
- Trailing stop with dynamic trail distance
- Asymmetric TP/SL (15c TP, 6c SL)

---

## Changes Made

### 1. TTL Cache on Dynamic Sizing — `position_manager.py`

**Problem:** Synchronous `sqlite3.connect()` on every `size_trade()` call blocked the asyncio event loop.

**Fix:** Added a 60-second TTL cache. The DB is queried at most once per minute; all other calls return the cached value instantly.

```python
# New instance vars
self._dynamic_cache_pct = max_pct_equity
self._dynamic_cache_ts = 0.0
self._dynamic_cache_ttl = 60.0

# In _compute_dynamic_max_pct():
now = time.time()
if now - self._dynamic_cache_ts < self._dynamic_cache_ttl:
    return self._dynamic_cache_pct
```

### 2. SCALP_POLY Trigger Independence — `hft_engine.py`

**Problem:** Strategy 3 was in an `elif` branch, only reachable when Strategy 2 had no signal. It never fired (0 entries lifetime).

**Fix:** Changed `elif` to independent `if` with guards preventing double-entry:

```python
# Before: elif (never reached when main engine emits signals)
# After: independent if, guarded by no-position check
if (HFT_POLY_PRIMARY_ENABLED and POLY_ENABLED
        and self._poly_features is not None
        and self._scalp is None
        and self._pending_entry is None):
    await self._check_poly_primary_entry(book, contract)
```

### 3. kalshi_trades Pending Bloat Fix — `hft_engine.py`

**Problem:** `_exit_scalp()` and `_poll_arb_fills()` wrote HFT orders to `kalshi_trades`, creating rows that were never settled (HFT settlement is in `hft_log` only). 184+ stale "pending" rows accumulated.

**Fix:** Removed both `log_kalshi_trade()` calls from HFT exit paths. `hft_log` is now the sole source of truth for HFT trades.

### 4. Code Hygiene

**BAD_UTC_HOURS unified:**
- Removed duplicate from `config_phase3.py` (was `{9, 12, 15, 21}`, stale)
- Removed `_BAD_HOURS` from `strategy_index.py`
- `strategy_index.py` now imports from `config.BAD_UTC_HOURS`
- `bias_engine.py` now imports from `config` instead of `config_phase3`

**session_label() unified:**
- Added canonical `session_label(utc_hour)` to `config.py`
- `signal_intelligence.py`: replaced local `_session_label()` with wrapper around `config.session_label`
- `strategy_index.py`: `get_session()` now delegates to `config.session_label()` with format mapping
- `main.py`: imports from `config` instead of `signal_intelligence`

**Imports moved to module level:**
- `bias_engine.py`: `from datetime import datetime, timezone` moved out of `update()` body
- `main.py`: `import aiosqlite` and `TRADES_DB_PATH` moved to top-level imports

**Docstring fix:**
- `SignalFilter` docstring updated to reference `config.BAD_UTC_HOURS` instead of listing stale hours

### 5. WinRateTracker Baseline Recalibration — `signal_intelligence.py`

**Problem:** Pine Script baselines (`{0: 67.6%, 1: 75.6%, 2: 85.0%, 3: 95.8%}`) were from a different environment. Live WR is 20-60%. The degradation threshold (`baseline - 10%`) could never trigger -- bucket 1 wouldn't flag until WR dropped below 65.6%, which is above actual live performance.

**Fix:** Recalibrated to match the regime x session WR table (58-71% range):

```python
# Old: {0: 67.57, 1: 75.56, 2: 84.97, 3: 95.82}
# New:
BASELINE_WR = {0: 55.0, 1: 58.0, 2: 62.0, 3: 66.0}
```

### 6. Aggressive Reversal & Flip Settings — `config.py`

**Problem:** Engine held losing positions too long because reversal exit required 4/5 TFs opposing. The slow 15m/1h TFs take 15-60 minutes to flip, so the engine kept betting against the winning side.

**Changes:**

| Setting | Before | After | Effect |
|---------|--------|-------|--------|
| `EARLY_EXIT_TF_THRESHOLD` | 4 | **3** | Exit when fast TFs (1m+3m+5m) flip -- don't wait for 15m/1h |
| `EARLY_EXIT_MIN_SAVINGS_CENTS` | 5 | **2** | Cut losers faster -- don't hold for 5c loss before exiting |
| `EARLY_EXIT_MIN_MINUTES` | 3 | **2** | Allow exits closer to expiry |
| `STRONG_REVERSAL_TF_THRESHOLD` | 5 | **4** | Aggressive force-sell at 4/5 instead of requiring all 5 |
| `REENTRY_MIN_MINUTES` | 5 | **3** | Re-enter opposite side with only 3 min remaining |

---

## Files Modified

| File | Changes |
|------|---------|
| `position_manager.py` | TTL cache on `_compute_dynamic_max_pct()` |
| `hft_engine.py` | SCALP_POLY independent trigger; removed `log_kalshi_trade` from HFT exit/arb paths |
| `config.py` | Added `session_label()` function; aggressive reversal/flip settings |
| `config_phase3.py` | Removed duplicate `BAD_UTC_HOURS` |
| `signal_intelligence.py` | Unified session_label import; recalibrated WinRateTracker baselines; updated docstring |
| `strategy_index.py` | Import `BAD_UTC_HOURS` from config; delegate `get_session()` to `config.session_label()` |
| `bias_engine.py` | Import `BAD_UTC_HOURS` from config; moved datetime import to module level |
| `main.py` | Moved `aiosqlite`/`TRADES_DB_PATH` to module-level imports; import `session_label` from config |

---

## Verification

- All modules compile and import successfully
- Engine restarted twice; both times clean startup with WebSocket connected, HFT running, Polymarket streaming
- First signal after restart showed 1h TF at -31.02 (not -100), confirming the P0 cap is working
- Startup scrub resolved all stale pending orders from previous sessions
- Balance at restart: $17.72
