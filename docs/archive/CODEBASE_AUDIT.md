# BTC Bias Engine — Codebase Audit
**Date:** 2026-04-01
**Auditor:** Claude Sonnet 4.6
**Scope:** All active files per CLAUDE.md
**Engine state:** PolymarketCopyEngine live, single strategy TA_FORCED, shadow MTF

---

## Executive Summary

The engine is **functional and trades live**, but the codebase is in a **transitional state** that has accumulated significant technical debt from iterative strategy changes. The core trading loop works. The main risks are:

1. **A silent NameError in the partial-TP fill path** that would cause the position count to silently not update, potentially leading to incorrect subsequent management decisions.
2. **A NameError in `_poll_kalshi_tape`** if the Kalshi WebSocket ever comes online — `book` is unset in one code path but referenced afterward.
3. **~400 lines of dead code** (disabled stops, disabled strategies, disabled exits) wrapped in `if False:` blocks that make the live behavior extremely hard to audit. The file has phantom features that look active but never execute.
4. **The TA_FORCED strategy ignores TA for ~70% of windows** — when `mid >= 55` or `mid <= 45`, it follows market direction unconditionally, not TA. The TAScorer and all its candle-fetching infrastructure runs to produce a result that's only consulted when the market is flat (45-55c).
5. **Daily P&L is not persisted** — a service restart resets the circuit breaker.

Overall health: **MODERATE RISK**. No issues will cause silent data corruption or ruinaway trading, but several bugs would be triggered if inactive code paths were re-enabled, and the current partial-TP fill path has a live crash risk.

---

## Per-File Findings

---

### `polymarket_copy_engine.py` (~3900 lines)

#### CRITICAL

**C1 — NameError in partial TP fill path (`_manage_position` ~line 3956)**
```python
pos["count"] = unfilled   # ← 'unfilled' is never defined
```
`unfilled` appears in the partial-TP branch (`total_filled < sell_target`) but is never assigned in scope. `unfilled_sells` is computed two lines earlier but is not the same variable. If any TP order ever partially fills (a real scenario at expiry), this throws `NameError`. It is silently swallowed by `except Exception: pass` at line 3974 — position count is never updated, subsequent TP checks operate on stale count.

**Fix:** Replace `unfilled` with `total_count - total_filled`.

---

**C2 — `book` variable referenced before assignment in `_poll_kalshi_tape` (~line 1739)**
```python
# Branch 1: ws_mid valid → 'book' never assigned
if ws and contract.ticker in ws._subscribed_tickers:
    ws_mid = ws.get_mid(...)
    if ws_mid and ws_mid != 50:
        self._kalshi_tape.mid_price_cents = ws_mid   # ← book NOT assigned here
    else:
        book = await ...                              # ← assigned in 'else' only
else:
    book = await ...

# Later (always runs):
if window_age <= 12.0 and book.mid_cents ...          # ← NameError if ws_mid valid
```
`kalshi_ws` import currently always fails (file not in repo), so `ws` is always `None` and this path never executes. But the failure mode is real and will bite the moment `kalshi_ws.py` is added. When it does, any window where the WebSocket delivers a valid mid will crash `_poll_kalshi_tape` silently.

**Fix:** Initialize `book = None` before the branch and guard the open-momentum check.

---

**C3 — `_evaluate_ta_forced_signal` ignores TA for ~70% of windows**
```python
mid = tape.mid_price_cents
if mid >= 55:
    final_dir = "up"      # ← unconditional, no TA consulted
elif mid <= 45:
    final_dir = "down"    # ← unconditional, no TA consulted
else:
    final_dir = ta.direction  # ← TA only consulted in 45-55c range
```
The strategy is named "TA_FORCED" and the CLAUDE.md describes it as "1m TA fallback", but for any market that has picked a directional lean (mid outside 45-55), the TA module result is completely ignored. Candle fetching, EMA/RSI computation, and the TAScorer state machine all run continuously to produce a result that is discarded whenever the market has conviction.
**Consequence:** The `TA_FORCED_MIN_ENTRY_CENTS = 40` and `TA_FORCED_MAX_ENTRY_CENTS = 55` entry band gates then contradict the direction logic — if `mid >= 55`, `final_dir = "up"` and `kalshi_side = "yes"`, but the entry band check requires `entry_price` (the shallow bid) to be 40-55c. A 58c YES ask would be skipped by the range check. This creates a signal that fires direction from mid price but can never enter if mid is above 55c.
**Implication:** Currently the engine essentially just follows market direction when clear, and uses TA direction only in flat markets. This may be intentional but is undocumented and the naming/comments are misleading.

---

**C4 — `_run_momentum_surfer` references uninitialized attributes**
`_run_momentum_surfer` (lines 3481-3574) references `self._surfer_position` and `self._momentum_surfer`, neither of which is initialized in `__init__`. If this method were ever called, it would immediately raise `AttributeError`. Currently it is never called (the call was removed), so it is dead code — but it is dead code that **looks like a live method** and would silently break if re-enabled.

---

#### HIGH

**H1 — Daily P&L not persisted across restarts**
`self._daily_pnl` is in-memory only. A service restart (manual or crash) resets it to 0. If the engine loses $13 and the NSSM service restarts (expected behavior after a crash), the daily loss limit starts fresh and can lose another $15 before the next halt. On a bad day: crash at -$13 → restart → lose another -$15 → crash at -$28 from start.
**Fix:** Write `_daily_pnl` and `_daily_pnl_date` to a file on each update; load on startup.

---

**H2 — `_manage_position` swallows all exceptions at DEBUG level (line 4519)**
```python
except Exception as e:
    logger.debug("CopyEngine position mgmt error: %s", e)
```
The entire position management function — including TP checks, position sync, and mandatory exit logic — is wrapped in a bare `except Exception` that logs at `DEBUG`. If the orderbook fetch fails or the TP check crashes, the engine silently continues to the next poll cycle with no position action. For live money, this should be at least WARNING.

---

**H3 — Signal evaluators maintained but never called**
`_flow_iteration` calls only `_evaluate_ta_forced_signal()` (line 1250). The following methods are fully implemented, maintain state, and are never called:
- `_evaluate_signal()` (PRIMARY tier)
- `_evaluate_mimic_signal()`
- `_evaluate_algo_signal()`
- `_evaluate_trend_follow_signal()`
- `_evaluate_pre_open_arb_signal()`
- `_evaluate_open_momentum_signal()`

These consume no CPU (not called), but they make reading `_flow_iteration` deeply confusing — a reader expects the signal cascade described in CLAUDE.md and in the function comments, but it doesn't exist.
**Comment at line 1254** says "Single strategy: TA_FORCED only. All other entry paths removed." — but they are NOT removed, just not called. This creates a high risk that a future edit accidentally re-enables one of them.

---

**H4 — `pos["signal_wallets"]` key references nonexistent attribute**
In `_execute_signal` at line 3347:
```python
"signal_wallets": list(flow.contributing_wallets) if hasattr(flow, 'contributing_wallets') else [],
```
`SmartFlowState` has no `contributing_wallets` field. `hasattr` guards it, so the result is always `[]`. The key is immediately redundant with `signal_wallet_names` computed two lines later. The `hasattr` guard masks what is effectively a broken feature silently.

---

**H5 — `_reconcile_pending_trades` called before KalshiClient has a session**
In `run()`, `await self._reconcile_pending_trades()` is at line 658, immediately before `async with aiohttp.ClientSession() as session:` at line 659. `KalshiClient._request` checks `if self._session is None: raise RuntimeError(...)`. Whether or not this is hit depends on how the client is initialized by the caller. If the client is used via its own context manager in the launch script, this is safe. If not, every startup will fail to reconcile pending trades with a RuntimeError caught at line 3806 (`except Exception as e: logger.warning(...)`). The warning would appear but reconciliation would silently do nothing.
**This needs verification against the launch script.**

---

**H6 — `_place_tiered_tp` has dead/undefined-variable legacy code**
Lines 3625-3626:
```python
if False:
    tier_counts.append(ct)    # 'ct' undefined
    remaining -= ct           # 'remaining' undefined
```
And `overnight` (line 3589, 3606) is computed twice and used by neither — it was used for different TP logic that no longer exists. The function body contains two separate `mid = getattr(...)` assignments (lines 3591 and 3596) that re-bind `mid_cents` — the first is immediately clobbered. This function was heavily refactored and the remnants are noise.

---

**H7 — Sell-flip in flow tracking inverts direction on any sell**
In `_poll_smart_trades` (lines 1377-1379):
```python
if trade_side == "SELL" and direction:
    direction = "down" if direction == "up" else "up"
```
When a tracked wallet sells their "Up" position, this counts as bearish signal. However, on Polymarket, most profitable wallets sell at expiry/near-expiry (they're taking profit on a winning trade). This sell could be happening at 0.95c because the outcome is already known. The `hold_rate` filter in the scorer removes wallets that sell more than 50% of trades, but individual-trade sell-flips still occur for the held wallets and could generate false bearish/bullish signals in the final minutes of a window. This is particularly dangerous near expiry.

---

#### MEDIUM

**M1 — `conviction` scoring in `_compute_position_size` has no effect (line 2828)**
The entire composite conviction scoring system (lines 2762-2825) computes `conv_score` from 5 signals, but then:
```python
budget_fraction = 1.0  # Use full SIZING_BALANCE_FRACTION (25%)
```
The score is computed, logged, and ignored. 100% of the budget is always used regardless of conviction. This makes the scoring dead logic. The comment "Flat sizing — 25% every trade. No conviction gating" suggests this was a deliberate decision, but the 60+ lines of scoring code remain as misleading dead weight.

---

**M2 — SmartFlowState.is_stale can remain False after window change**
`updated_at` is only set in `_recompute_smart_flow` when `self._smart_trades` is non-empty (line 1654):
```python
updated_at=now if self._smart_trades else self._smart_flow.updated_at,
```
When a new window starts and no smart trades have arrived yet, `self._smart_trades` is empty and `updated_at` carries forward the previous window's timestamp. `FLOW_STALE_S = 900.0` (15 min), so `is_stale` stays False for up to 15 minutes even though the flow data is from the previous window. The PRIMARY signal evaluator (currently disabled) would not gate on staleness correctly.

---

**M3 — `_discover_settled_btc_markets` makes up to 200 sequential HTTP requests**
The individual slug query loop (lines 998-1025) makes up to 200 GET requests with 0.15s sleeps between them. That's 30+ seconds of sequential HTTP requests. During this time the scorer coroutine is blocked (there's no `asyncio.sleep` — the `await asyncio.sleep(0.15)` IS the yielding). The flow loop still runs because scorer and flow are separate tasks, but this is a very slow path for something that runs every hour.
Additionally, `await asyncio.sleep(0.3)` per market analyzed (line 834) adds another 60 seconds for 200 markets. Full scorer runs take 15+ minutes.

---

**M4 — Import statements inside hot functions**
Multiple hot functions contain module-level imports:
- `import math as _math_size` inside `_compute_position_size` (called every signal)
- `import json as _json` inside `_execute_signal` (called every signal)
- `import math as _math_tp`, `import math as _math_dn`, `import math as _math_flip` inside `_place_tiered_tp` and `_manage_position`
- `from datetime import datetime, timezone, timedelta` appears 4+ times inside methods

Python caches these in `sys.modules` so they don't re-parse, but `from datetime import ...` inside a function creates new local bindings on every call. More importantly, these hide real imports from readers scanning the file header.

---

**M5 — `_wallet_composite_score` redefined on every scoring run**
The `_wallet_composite_score` inner function (line 891) is a module-level-quality function defined inside `_score_wallets`. It captures no closure state and is recreated every hour. Should be a static method or module-level function.

---

**M6 — `SIGNAL_MIN_FLOW_CONVICTION` comment disagrees with value**
`polymarket_copy_engine.py` line 128:
```python
SIGNAL_MIN_FLOW_CONVICTION = _uc("MIN_FLOW_CONVICTION", 0.75)  # RAISED to 75%...
```
`user_config.py` sets `MIN_FLOW_CONVICTION = 0.50`, so the effective value is 50%, not 75%. The comment in the engine is wrong and will mislead anyone debugging the signal gates.

---

**M7 — Two separate BTC candle fetch paths**
Both `_update_ta_candles` (REST REST poll every 60s) AND `PriceFeedTask` (WebSocket) collect 1m BTC candle data. The `TAScorer` is fed from the REST path; the `PriceFeedTask` buffers 60 candles for MTF but the 1m feed has no callback registered (only 5m/15m/1h are registered at line 678). So `PriceFeedTask` collects and discards 1m candles. The REST path is the only live source for `TAScorer`. This duplication is wasteful and the MTF 1m path is effectively unused.

---

**M8 — `_open_position` dict uses ad-hoc key names inconsistently**
The position dict is built with different keys in different code paths (new entry, recovery, sync, flip-invert). Keys like `_entry_lean`, `_addon_checked`, `_tp_decayed_3min`, `_tp_decayed_5min` are added dynamically mid-session. The lack of a defined schema means:
- `pos.get("_tp_decayed_3min", False)` must be used everywhere (never `pos["_tp_decayed_3min"]`)
- A typo in a key name silently uses the default and skips the guard logic
- Recovery/sync paths may be missing keys that later code expects

---

**M9 — `_session_block_logged_hour` and other "informal instance variables" set via getattr**
Several instance variables are first accessed via `getattr(..., default)` rather than being initialized in `__init__`:
- `_session_block_logged_hour` (line 1235)
- `_startup_skip` (line 645, but initialized at line 645 itself — OK)
- `_cap_logged_this_window`, `_range_skip_logged`, `_max_min_logged`, `_no_contract_logged`, `_mom_gate_logged` (all set conditionally in the flow loop)

These are all set before first use, but they're invisible from `__init__` and make the object state hard to audit.

---

**M10 — Kalshi WebSocket (`kalshi_ws.py`) missing from repo**
The engine imports `KalshiWebSocket` in a `try/except ImportError` (lines 572-587). This file is not in the active file list and presumably doesn't exist in the deployment. The WS always fails silently and `self._kalshi_ws = None`. All WebSocket-dependent logic (mid-price update, subscription, open momentum `book.mid_cents` reference) falls back to REST-only. But the code that checks `if ws and contract.ticker in ws._subscribed_tickers` is never True, so the `book` NameError (C2) is currently masked.

---

#### LOW

**L1 — ~400 lines of dead code (`if False:` blocks)**
Conservative count of dead code lines: trailing stop (~40 lines), catastrophic stop (~25 lines), wallet backstop (~25 lines), time exit block (~30 lines), hard stop (~25 lines), instant buy (~110 lines), momentum surfer (~95 lines), legacy split code (~10 lines). The `if False:` pattern is used consistently, which is good for discoverability, but the code should be either removed or moved to a feature-flag branch. At 3900 lines, the file is extremely hard to navigate.

---

**L2 — `signal = None` assigned then immediately overwritten (line 1247)**
```python
signal = None
signal = self._evaluate_ta_forced_signal()
```
Pointless.

---

**L3 — `_SyntheticOrder` local class defined inside `_execute_signal` (line 3207)**
```python
class _SyntheticOrder:
    def __init__(self, oid, fc):
        ...
```
A new class is defined every time `_execute_signal` runs the PRE_OPEN_ARB path. This is fragile, unconventional, and surprising. A `KalshiOrder` dataclass should be constructed directly instead.

---

**L4 — `_flexible_tp` defined but likely unused in active path**
`_flexible_tp` (lines 250-295) is a module-level function that computes VWAP-based TP targets. The active TP logic uses `_place_tiered_tp` (72c/78c tiers), not `_flexible_tp`. This function is defined, never called, and constitutes dead code.

---

**L5 — `TAKE_PROFIT_CENTS = 5` in user_config.py never used**
`user_config.py` line 37 defines `TAKE_PROFIT_CENTS = 5`. No code in the active engine reads this key via `_uc("TAKE_PROFIT_CENTS", ...)`. The TP logic is hardcoded in `_place_tiered_tp` (72c/78c). Misleading to users who might edit this expecting to change TP behavior.

---

### `kalshi_client.py` (~700 lines)

#### MEDIUM

**M1 — `fill_cost` method has identical branches in ternary (line 231)**
```python
effective_price = (100 - price) if side == "yes" else (100 - price)
```
Both branches are identical. The YES and NO sides should compute cost differently (for NO, cost is `price` directly). This was likely a copy-paste error that was never caught because `fill_cost` is not called from the active engine (it's legacy HFT infrastructure).

---

**M2 — `get_balance` returns `balance` in cents, comment says "dollars" inconsistently**
The Kalshi API returns balance in cents. `KalshiBalance.balance` is documented as `int` (cents). In `_compute_position_size` (engine line 2736): `balance_dollars = bal.balance / 100.0` — this is correct. But the field name `balance` with no unit suffix (vs `portfolio_value`) could confuse future callers. Recommend: rename to `balance_cents` or add a `balance_dollars` property.

---

**M3 — `cancel_all_resting_orders` silently skips failures without count**
Line 613: individual cancel failures are caught with bare `except Exception: pass`. If a ghost order fails to cancel, it's silently ignored. The returned count only includes successes. The caller has no way to know if cleanup was complete.

---

**M4 — `_parse_btc_strike` heuristic is fragile**
The dollar/cents conversion heuristic `val < 1_000_000` (line 48) assumes BTC price will never exceed $10M. Fine today, not a good assumption for a multi-year system. More importantly, the fallback regex on the title string (line 55) parses market descriptions that could change format.

---

#### LOW

**L1 — `reduce_only` parameter documented but silently dropped (line 577)**
```python
# Note: reduce_only flag dropped — Kalshi requires IoC...
```
The parameter appears in the function signature with documentation, but the payload never includes it. A caller passing `reduce_only=True` gets silent no-op behavior. The parameter should either be implemented or removed from the signature.

---

### `whale_monitor.py` (~480 lines)

#### HIGH

**H1 — Change output UTXO classification is incorrect for Bitcoin**
`_classify_tx` "both sender and receiver" logic (lines 357-363):
```python
net = received_sats - sent_sats
if net > 0:
    return "inflow", net / SATS_PER_BTC
else:
    return "outflow", abs(net) / SATS_PER_BTC
```
In Bitcoin UTXOs, a wallet sending to an exchange always sends the full UTXO and receives change back to themselves. Example: wallet has 1 BTC UTXO, sends 0.1 BTC to exchange, receives 0.9 BTC change. In this transaction: `received_sats = 0.9 BTC`, `sent_sats = 1.0 BTC`, `net = -0.1 BTC` → classified as "outflow 0.1 BTC" ✓ OK here. But if the opposite: exchange receives 1.0 BTC from wallet UTXO, sends 0.5 BTC change back: `received = 0.5`, `sent = 1.0`, `net = -0.5` → classified as "outflow 0.5 BTC" (WRONG — this is an inflow to the exchange of 0.5 BTC net). The math may be correct for simple cases but is incorrect for multi-input transactions where a single watched address is one of many inputs. This is a systemic issue with on-chain whale classification that cannot be fully resolved without full transaction graph analysis.

---

#### MEDIUM

**M1 — REST poll session created inside task, not shared**
`_rest_poll_loop` creates its own `aiohttp.ClientSession` (line 381). The engine has its own session for Polymarket/Binance. Creating two sessions for two consumers adds overhead and potentially exhaust connection limits. Not critical given the 60s poll interval but worth noting.

---

**M2 — `_known_txids` pruning drops txids for txs still in window**
```python
if len(self._known_txids) > 10000:
    recent_ids = {tx.txid for tx in self._txs}
    self._known_txids = recent_ids
```
After pruning, only txids from the rolling `_txs` deque are kept. Txids older than `WHALE_WINDOW_S` (15 min) are removed from `_known_txids` even though the WS may re-deliver them (especially on reconnect). This could cause double-processing of transactions, inflating whale flow numbers.

---

#### LOW

**L1 — WebSocket subscribes to each address with 0.05s sleep between sends**
With 16 addresses, this is 0.8s of sequential subscription setup on every reconnect. Not critical but adds to reconnect latency.

---

### `signal_logger.py` (~620 lines)

#### MEDIUM

**M1 — `ALTER TABLE ADD COLUMN` migration runs on every startup**
~25 ALTER TABLE statements run on every engine start (lines 233-290). All catch exceptions silently. While correct (columns already exist = ignored), this adds ~25 SQLite round-trips to startup time. A simple schema version table would be cleaner and faster.

---

**M2 — `log_execution`, `update_execution`, `log_hft_eval`, `update_hft_row` never called from active engine**
These 4 methods (lines 410-477) are infrastructure for the disabled HFT engine. They add ~70 lines to an already long file and their table DDL is initialized on every startup. Not harmful, but increases code surface and confusion.

---

**M3 — `calibration_bucket_report` fee adjustment formula is unit-confused (line 566)**
```python
r[1] - (r[5] / 100.0)   # fee in cents → dollars
```
`fee_estimate_cents` is the fee for a single entry. The comment says "1-contract, $0.01/cent" which is correct: 1 cent fee on a 1-contract trade = $0.01. But the column value is computed as `entry_price * (100 - entry_price) / 10000.0 * 7.0` (engine line 3390), which for a 50c entry = `50*50/10000*7 = 1.75` cents. `1.75 / 100 = $0.0175` fee adjustment per trade. Given that pnl is in dollars and could be $0.50-$5.00, this fee adjustment is so small it's practically noise, but the calculation is correct.

---

#### LOW

**L1 — New aiosqlite connection per operation**
Every `log_kalshi_trade`, `log_kalshi_outcome`, etc. opens and closes a new aiosqlite connection. Connection overhead is ~1ms per operation. At 1 trade per 15min window this is negligible. If the engine were used at higher frequency, a persistent connection would matter.

---

### `ta_module.py` (~295 lines)

#### MEDIUM

**M1 — `fetch_binance_1m_candles` uses `binance.us` domain**
All API calls use `https://api.binance.us/api/v3/klines` (line 256) and `wss://stream.binance.us:9443/stream` (price_feed.py line 41). Binance.US is the US-based subsidiary with reduced trading pairs and different availability. If the user is outside the US, Binance.US may geo-block the IPs. `api.binance.com` is the global endpoint. This should at minimum be configurable.

---

**M2 — `is_warm` threshold of 3 bars is very low**
`TAScorer.is_warm` returns True after only 3 candles (line 239). With EMA periods of 5 and 13 bars, and RSI period of 7, the indicators are not meaningful at 3 bars. The EMA will be heavily biased to the initial price. The RSI is returning the warmup value of 50.0 until at least 7 bars are processed. Warming at 3 bars will produce unreliable scores that the engine acts on immediately at window start.

---

#### LOW

**L1 — `score_velocity` computed from EMA-smoothed score, not raw**
Velocity = `score - prev_score` where both are EMA-smoothed. This produces a smoother velocity but also introduces lag. In a momentum system, raw velocity would be more responsive. Minor.

---

### `indicators.py` (~120 lines)

**No bugs found.** Implementation is correct: EMA matches Pine Script formula, RSI correctly implements Wilder's smoothing with simple-average seed, SMA uses deque. Clean and well-commented.

**LOW — `SMACalc.value` property recomputes `sum()` on every access**
```python
@property
def value(self) -> Optional[float]:
    if len(self._buffer) < self.length:
        return None
    return sum(self._buffer) / self.length   # O(n) every time
```
`update()` already computes the sum. Cache the running sum and subtract/add at each update. Not performance-critical at SMA(20) but is an unnecessary O(n) operation in a per-candle hot path.

---

### `models.py` (~110 lines)

**No bugs found.** Clean dataclass definitions. `BiasResult`, `ConsensusSignal`, and `TradeRecord` are for the disabled consensus engine but don't cause any harm.

**LOW** — These three classes are never used by the active engine. They're imports waiting to fail if someone removes their dependencies. Could be cleaned out or moved to `_archive/`.

---

### `user_config.py` (~110 lines)

#### MEDIUM

**M1 — Comment/value mismatch on `MIN_MINUTES_REMAINING`**
```python
MIN_MINUTES_REMAINING = 2.0   # 4min minimum. No stop losses...
```
Comment says "4min minimum" but value is 2.0. The comment is stale from before the threshold was lowered.

---

**M2 — `TAKE_PROFIT_CENTS = 5` is dead configuration**
This setting is documented and commented but never read by the active engine. It suggests a user could tune TP size by editing this file, but the actual TP logic ignores it.

---

**M3 — `MAX_ENTRY_CENTS = 55` inconsistency with CLAUDE.md**
`user_config.py` sets `MAX_ENTRY_CENTS = 55`, but CLAUDE.md says `MAX_ENTRY_CENTS = 82`. The engine uses `_uc("MAX_ENTRY_CENTS", 64)` which reads from user_config → 55c. CLAUDE.md's documented config is wrong. Minor, but CLAUDE.md is the primary reference document and it's misleading.

---

#### LOW

**L1 — `SIZING_BALANCE_FRACTION` comment says "profitable with bid+1 entries"**
Line 25: "the original profitable setting — profitable with bid+1 entries catching bottoms". The current engine doesn't use bid+1 laddered entries — it uses market orders at ask. The comment describes a superseded strategy.

---

### `price_feed.py` (~410 lines)

#### MEDIUM

**M1 — `_warmup` uses `asyncio.gather` with dict ordering for zip**
```python
tasks = {tf: self._fetch_rest(tf, limit) for tf, limit in warmup_limits.items()}
results = await asyncio.gather(*tasks.values(), return_exceptions=True)
for tf, result in zip(tasks.keys(), results):
```
This relies on dict insertion order for `zip` to correctly match `tf` → `result`. In CPython 3.7+ dicts maintain insertion order, so this works. But `asyncio.gather` preserves input order in results, and `tasks.values()` / `tasks.keys()` are both in insertion order, so the zip is correct. No bug, but `dict(zip(tasks.keys(), results))` would be clearer intent.

---

**M2 — `_poll_rest_loop` sleeps 60s but polls every 900s**
The 1h poll loop sleeps 60s per iteration (line 295) but only fetches when `now - last_poll >= 900s`. This means 15 wakeups per poll, burning CPU wake-up cycles for no reason. Sleep 900s directly or sleep until the next scheduled poll time.

---

#### LOW

**L1 — 1m candles buffered but no callbacks registered for 1m**
`BUFFER_SIZES["1m"] = 60`, the buffer is filled from WebSocket, but no callback is ever registered for "1m" in the engine (only 5m/15m/1h are registered). 60 1m candles are maintained and silently discarded each window.

---

### `tf_analyzer.py` (~454 lines)

#### MEDIUM

**M1 — EMA freshness multiplier never reaches its floor for typical crosses**
```python
freshness = max(_EMA_FRESHNESS_MIN, 1.0 - (self._ema_cross_bars - 1) * 0.04)
```
At `ema_cross_bars = 17`, freshness = `max(0.35, 1.0 - 0.64)` = `max(0.35, 0.36)` = 0.36. Reaches the floor at 17 bars. A 17-bar-old EMA cross (17 minutes on 1m TF, 85 minutes on 5m TF, 4.25 hours on 15m TF) is still given 35% weight in the score. On 15m TF, this means a 4-hour-old EMA cross still contributes significantly to the signal. The freshness decay may be too slow for higher timeframes.

---

**M2 — `_classify_market_structure` uses 8-bar highs but only 4 needed for "flat" guard**
```python
self._highs: deque[float] = deque(maxlen=8)
...
if len(self._highs) < 4 or len(self._lows) < 4:
    return "flat"
```
With 8 bars in the deque and 4 minimum, the function can run on 4 bars or 8 bars with the same algorithm. The half/half split (`mid = len(highs) // 2`) with 4 bars gives `mid = 2`, comparing first 2 vs last 2. With 8 bars it compares first 4 vs last 4. The structure classification changes based on how full the deque is, creating inconsistent behavior during warmup.

---

#### LOW

**L1 — `_candle_structure_score` bonus for engulf can exceed ±1 before clamping**
`pressure + bonus`: `pressure` ∈ [-1, +1], `bonus` = ±0.4 → unclamped range is [-1.4, +1.4]. The `max(-1, min(1, ...))` clamp handles this but wastes range — the bonus is always partially clipped when pressure is strong.

---

### `mtf_scorer.py` (~355 lines)

#### MEDIUM

**M1 — `_uc()` helper duplicated in three files**
`price_feed.py`, `mtf_scorer.py`, and `polymarket_copy_engine.py` each define their own `_user_cfg` loader and `_uc()` function. They're identical 10-line patterns. If `user_config.py` changes structure, all three must be updated. Should be extracted to a `config.py` module.

---

**M2 — Missing TFs from `MTF_TIMEFRAMES` user config are silently ignored**
`user_config.py` has `MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h"]`. `MTFConfluenceScorer` hardcodes its analyzers for `("5m", "15m", "1h")`. If a user adds "4h" to `MTF_TIMEFRAMES`, nothing happens — it's never passed to `PriceFeedTask` or registered. The config key is effectively read-only (checked nowhere in the scorer or feed).

---

#### LOW

**L1 — `warmup_status` returns `{"1m": True}` hardcoded**
```python
status = {"1m": True}   # 1m is always "warm" (delegated to TAScorer)
```
The 1m signal comes from `TAScorer.is_warm` which requires 3 bars. If the engine starts and immediately evaluates before 3 bars, the 1m is treated as "warm" by `MTFConfluenceScorer` but the underlying TAScorer is not. Minor edge case.

---

---

## Architectural Observations & Technical Debt

### 1. Strategy Identity Crisis
The codebase was built as a multi-tier cross-venue signal cascade (PRIMARY → TREND → MIMIC → ALGO → TA_FORCED → FLIP_INVERT). All tiers except TA_FORCED have been disabled based on live data. The correct state of the codebase is: **this is a simple technical analysis engine that follows market direction when clear and uses TA scoring when the market is flat.** But the code is 3900 lines that describe a sophisticated multi-tier copy-trade system. The documentation (CLAUDE.md, comments, method names, signal tier variables) all describe the disabled architecture. Maintaining this mismatch is a significant cognitive burden and a bug-introduction risk.

### 2. "Hold to Expiry" Philosophy Requires Trust in Entry Logic
All stops, trailing stops, catastrophic stops, and wallet-flip exits are disabled. The engine holds every position to expiry unless a TP limit fills or the TP decay logic fires at 5-7 minutes. This is a valid high-conviction strategy, but it means: **the only protection against a catastrophic loss is the entry price band (40-55c) and the sizing cap ($50).** A single 50c entry on 5 contracts that expires at 0 loses $2.50 per contract × 5 = $12.50 — just under the $15 daily limit. Two bad trades in a day = halted. There is no intra-trade protection whatsoever. This is only acceptable if the TA_FORCED strategy has sufficiently high win rate.

### 3. TP Decay Logic Is the Real Exit Path
With all stops disabled, the actual exit logic is:
- TP fills at 72c/78c (wins)
- TP decay at 5 min: lower all TPs to entry+5c
- TP decay at 3 min: market sell if profitable (else hold)
- Mandatory exit at 3 min: market sell if underwater
- Contract expiry (could be 0c or 100c)

The TP decay at 3 minutes only fires if `bid <= entry` (underwater). If the position is profitable but TPs never filled (bid = entry+2, TPs at 72c), the position holds to expiry and could settle at 0c. This is a significant gap: **a position that is profitable (bid > entry) but hasn't hit the TP tiers is held to expiry with no protection.**

### 4. MTF System Needs Validation Before Use
The MTF system (price_feed, tf_analyzer, mtf_scorer) is well-designed and in shadow mode. The architecture is sound. The main gap is that `MTF_TIMEFRAMES` user config is decorative (not actually used to configure which timeframes are tracked), and the 1m signal is bridged via a conversion function that maps `composite_score / 100` to TFSignal — a reasonable approximation but with a different scale and indicators than the TFAnalyzer would produce natively.

### 5. Wallet Scorer Quality vs. Current Use
The wallet scorer (lines 808-970) is the most sophisticated part of the codebase: directional WR tracking, composite scoring that penalizes 100% WR wallets, scalper filtering, late-entry filtering. This is excellent work. **But the scorer output is completely unused in the current TA_FORCED strategy.** The wallet flow tracker runs, computes `SmartFlowState`, and that state is only used in `_compute_position_size` for a minor 5-point bonus. All that work to score wallets produces no entry signals today.

---

## Prioritized Action Items

### P0 — Fix before next trading session
1. **Fix `unfilled` NameError** in `_manage_position` partial-TP path (line 3956). Change `pos["count"] = unfilled` to `pos["count"] = total_count - total_filled`.
2. **Guard `book` variable** in `_poll_kalshi_tape` before the open-momentum tracker (line 1739). Initialize `book = None` before the WS/REST branch and skip the check if `book is None`.

### P1 — Fix this week
3. **Persist daily P&L** to a file (e.g., `data/daily_pnl.json`) and load on startup.
4. **Raise `_manage_position` exception level** from `DEBUG` to `WARNING`.
5. **Remove dead code** from `_flow_iteration` call site comment — either call the signal evaluators or remove them. The current state is deceptive.
6. **Fix `_place_tiered_tp`** double-rebind of `mid`, remove `if False:` legacy blocks with undefined vars, and remove unused `overnight` variable.

### P2 — Clean up soon
7. **Add `book = None` guard** and address the `_run_momentum_surfer` uninitialized attributes (either delete the method or add to `__init__`).
8. **Fix CLAUDE.md** `MAX_ENTRY_CENTS` documentation (says 82, should be 55 from user_config).
9. **Fix `MIN_MINUTES_REMAINING` comment** in user_config.py (says "4min minimum", is 2.0).
10. **Fix `SIGNAL_MIN_FLOW_CONVICTION` comment** in engine (says "RAISED to 75%", is 50% in user_config).
11. **Remove `TAKE_PROFIT_CENTS`** from user_config.py since it's never read by the engine.
12. **Move inline `import math`, `import json`** calls to module level.

### P3 — Technical debt (prioritize when time allows)
13. **Extract `_uc()` helper** to a shared `config.py`.
14. **Convert `_open_position` dict** to a dataclass to prevent key-name typos.
15. **Delete or archive unused signal evaluators** (PRIMARY, MIMIC, ALGO, TREND_FOLLOW, PRE_OPEN_ARB, MOMENTUM) — they're dead code that inflates the file by ~500 lines.
16. **Make `_wallet_composite_score`** a static/module-level function.
17. **Register 1m MTF callback** or remove the 1m buffer from `PriceFeedTask` (currently collecting data with no consumer).
18. **Consolidate BTC candle fetching** — either `TAScorer` uses the `PriceFeedTask` 1m buffer (remove the REST fetch from `_update_ta_candles`), or don't run `PriceFeedTask` at all for 1m.

---

*Audit complete. All findings are based on static analysis of the code as read on 2026-04-01.*
