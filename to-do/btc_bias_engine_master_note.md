# BTC Bias Engine — Master Note (Diagnostics & Action Plan)

## Summary

### HFT Engine — Healthy
- Fills: 310  
- Win Rate: ~60%  
- Net P&L: +$25.43  
- Avg P&L per fill: +$0.082  
- API ack latency: ~34ms (actual execution healthy; 4.1s = REST poll delay)

**Conclusion:** Execution + microstructure edge is working as intended.

---

### Main Engine — Crisis
- Actual WR: 36.4% (vs expected ~63.5%)  
- Net P&L: -$9.77  
- Behavior: Persistent directional bias (PUT-heavy)

**Root Signal Issue:**
- 1h timeframe contributes a constant **-100 bias**
- This injects a fixed bearish weight into the Fourier score
- Result: Engine is structurally locked into "NO" bets

---

## Observed Failures & Root Causes

### 1. 1H Timeframe Lock-In
**Symptom:**
- Continuous PUT bias
- Multiple consecutive losing windows
- No responsiveness to intraday reversals

**Root Cause:**
- 1H signal is:
  - Either stale
  - Or permanently saturated (-100)
- Overweighted in scoring system

**Fix:**
- Clamp or normalize 1H contribution:
```python
one_hour_score = max(min(score, 50), -50)
```
- OR introduce decay:
```python
weight = exp(-time_since_update / tau)
```
- OR disable temporarily for validation

---

### 2. SCALP_POLY Never Executes
**Symptom:**
- 0 entries recorded

**Root Cause:**
- Strategy placed under `elif` chain
- Candle strategy always active → blocks execution path

**Fix:**
- Convert to independent evaluation:
```python
if candle_signal:
    run_candle()

if poly_signal:
    run_poly()
```

---

### 3. Regime Filter Misbehavior
**Symptom:**
- ~2,000 rejects for:
  - regime_unfavorable
  - regime_unknown
- Despite config:
```python
HFT_REQUIRE_FAVORABLE_REGIME = False
```

**Root Cause Possibilities:**
- Secondary gate still enforcing regime
- Logging occurring even when not blocking
- Config not properly propagated

**Fix:**
- Trace full decision path:
```python
print("REGIME CHECK:", require_flag, regime_state)
```
- Ensure gating condition:
```python
if require_flag and regime != FAVORABLE:
    reject()
```

---

### 4. Stale "Pending" Trades in kalshi_trades
**Symptom:**
- 184 trades stuck in pending state
- ledger.py shows incorrect positions

**Root Cause:**
- Missing reconciliation between:
  - HFT execution log
  - kalshi_trades table

**Fix:**
- Add reconciliation job:
```sql
UPDATE kalshi_trades
SET status = 'filled'
WHERE order_id IN (SELECT order_id FROM hft_log WHERE filled=1);
```

- Or fully rebuild from source of truth:
```python
rebuild_positions_from_hft_log()
```

---

### 5. SQLite CLI Failure
**Symptom:**
```
sqlite3: Exit code 127
```

**Root Cause:**
- sqlite3 not installed / not in PATH

**Fix:**
- Use Python fallback:
```python
import sqlite3
conn = sqlite3.connect("trades.db")
```

---

## Priority Fix Order

### P0 — Critical (Blocking Profitability)
1. Fix 1H timeframe bias
2. Validate main engine signal integrity

### P1 — High Impact
3. Enable SCALP_POLY execution
4. Fix regime gating inconsistency

### P2 — Data Integrity
5. Repair kalshi_trades reconciliation
6. Fix ledger accuracy

---

## Next Session Plan

### Phase 1 — Signal Repair
- Disable or clamp 1H TF
- Re-run backtest / live observation

### Phase 2 — Execution Unlock
- Refactor strategy branching
- Confirm SCALP_POLY triggers

### Phase 3 — Debug Gating
- Trace regime filter path
- Align config vs behavior

### Phase 4 — Data Consistency
- Reconcile all trade tables
- Validate ledger output

---

## Success Criteria

- Main engine WR > 50%
- Removal of directional lock-in
- SCALP_POLY producing trades
- Regime rejects aligned with config
- Ledger matches actual positions

---

## Final Take

- **HFT layer = solid edge**
- **Main engine = structurally biased + misweighted**
- This is not a market failure — it's a **signal architecture failure**

Fix the signal weighting → profitability should normalize.
