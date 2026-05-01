# Scalp Mode Revert Guide

**Deployed**: 2026-04-14 (afternoon, post -$80 day)
**Thesis**: engine has no 15-min direction-prediction edge. Capture FVG-to-fair-value snap with small fast exits.
**Written**: 2026-04-14 during user "what are we fundamentally doing wrong" pushback.

This document captures exactly what to flip back to if scalp mode fails.

---

## Revert trigger conditions

Flip back if any of the following after 30+ trades:
1. **Win rate < 55%** (scalp needs 56%+ to beat slippage-widened losses)
2. **Net < $0 over 30 trades** even with 60%+ WR (slippage eating more than modeled)
3. **User call** — "grinding pace" subjective verdict

---

## What scalp mode changed (and how to revert each)

### 1. TP from fair-value band → fixed +5c
**File**: `polymarket_copy_engine.py` around line 6115
**Current (scalp)**:
```python
tp_price = avg_entry + 5   # fixed +5c scalp target
tp_price = min(tp_price, 95)
```
**Revert to (yesterday)**:
```python
_vol_tp = prob_tp.volatility if prob_tp and prob_tp.is_ready else 0.30
_pressure_tp = abs(self._last_pressure.score)
_base_band = 10
_pressure_bonus = int(_pressure_tp * 10)
_vol_bonus = int(min(_vol_tp, 0.50) * 10)
_tp_band = _base_band + _pressure_bonus + _vol_bonus
_tp_band = max(8, min(_tp_band, 25))

tp_price = avg_entry + _tp_band

if prob_tp and prob_tp.is_ready and prob_tp.fair_value > 0:
    if signal.kalshi_side == "yes":
        _fv_tp = int(prob_tp.fair_value)
    else:
        _fv_tp = 100 - int(prob_tp.fair_value)
    tp_price = min(tp_price, _fv_tp)

tp_price = max(tp_price, avg_entry + 5)
tp_price = min(tp_price, 95)
```

### 2. Dynamic TP ratchet disabled
**File**: `polymarket_copy_engine.py` around line 6987
**Current (scalp)**: `if False and tp_ids and not pos.get("_profit_trail_exited", False):`
**Revert**: Remove `False and ` — back to `if tp_ids and not pos.get("_profit_trail_exited", False):`

### 3. DCA disabled for all TA_FORCED
**File**: `polymarket_copy_engine.py` around line 5407
**Current (scalp)**:
```python
_dca_mode = False  # SCALP: no DCA on TA_FORCED
```
**Revert**:
```python
_dca_mode = True  # default: DCA enabled
```
(Keep the FVG_SMALL override which sets it False for that path only.)

### 4. Peak-giveback tightened (3c peak, 40% giveback)
**File**: `polymarket_copy_engine.py` around line 7556
**Current (scalp)**:
```python
_min_give = max(2, _math_pgb.ceil(_peak_profit * 0.4))
if (_peak_profit >= 3 and _give_back >= _min_give
        and bid >= entry - 2):
```
**Revert to** (moderate catch — prevents giveback on noise):
```python
_min_give = max(5, _math_pgb.ceil(_peak_profit * 0.6))
# Also require prob drop as corroborating signal
_prob_dropped_pgb = True
if _prob_pk and _prob_pk.is_ready:
    _cur_p_pgb = _prob_pk.probability * 100 if pos["side"] == "yes" else (100 - _prob_pk.probability * 100)
    _peak_p_pgb = pos.get("_peak_prob", _cur_p_pgb)
    _prob_dropped_pgb = (_peak_p_pgb - _cur_p_pgb) >= 3
if (_peak_profit >= 8 and _give_back >= _min_give
        and bid >= entry - 2 and _prob_dropped_pgb):
```

### 5. Hard stop at entry-5c
**File**: `polymarket_copy_engine.py` around line 7556 (section labeled "SCALP HARD STOP")
**Revert**: remove the entire `SCALP HARD STOP` block (search for `CopyEngine SCALP STOP` log line).

### 6. Sizing (user_config.py)
**Current (scalp)**:
```python
SIZING_BALANCE_FRACTION = 0.15
SIZING_MAX_DOLLARS = 13.00
```
**Revert to yesterday's**:
```python
SIZING_BALANCE_FRACTION = 0.30   # or up to 0.50 for aggressive
SIZING_MAX_DOLLARS = 30.00
```

---

## What STAYS (data-backed, kept under revert)

These additions survived the 48h analysis and should not be reverted:

| Filter/gate | Why it stays |
|---|---|
| **Reversal-risk filter** (RSI≥75 + pressure neutral + vel against) | Saved -$21.84 on 14:31 bad-top entry |
| **Low-vol skip** (vol<25%, unit bug fixed) | 48h data: low-vol = -$48 on 15 trades |
| **NO-vs-uptrend regime gate** | 48h data: NO during +5% BTC = -$43 on 38 trades |
| **Time-aware entry cap** (75/65/59c by session age) | Matches observed session dynamics |
| **Conviction boost sizing** (up to 3.5x, cap 28ct) | Data-neutral, amplifies bigger-wins principle |
| **Thesis/reversal exits DISABLED** | 48h data: -$192 on 20 trades |
| **DCA discriminator gate** (6 rules) | Blocks bad-context DCA but allows legit reversion plays |
| **DCA tier cap at 2** (was 3) | Prevents the 72ct cascades |
| **Mandatory exit, catastrophic exit, hard-floor-30c** | Pre-existing, unchanged |

---

## Yesterday's engine one-liner

> "Brownian Bridge identifies mispricing → size big (25-50 contracts) → set TP at fair-value band (10-25c above entry, capped at fair value) → DCA if bid dips but thesis intact → hold to expiry. 60-75% WR, +$79 net over 52 trades."

## Scalp mode one-liner

> "Same entry math → size 15% of balance (~25 ct) → TP at exactly +5c → hard stop at -5c → small symmetric 1:1 risk → target 50+ trades/day × ~$1 net per win."

---

## Monitor results

After 30 trades on scalp mode, compare:
- Average $/trade
- Win rate
- Largest loss
- Daily $ outcome

If scalp shows <$0.20/trade avg net, **revert**. Yesterday averaged $1.52/trade at 75% WR.
