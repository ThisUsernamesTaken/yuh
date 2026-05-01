# BTC Bias Engine — Implementation Guide
**Generated**: 2026-04-02
**Sources**: CODEBASE_AUDIT.md, MTF_REVIEW.md, MTF_ACCURACY_AND_DYNAMIC_TP_ANALYSIS.md, TRADING_ANALYSIS_20260328.md
**Period analyzed**: 1,184–1,169 settled trades, 2026-03-13 to 2026-03-28

---

## 1. EXECUTIVE SUMMARY

The PolymarketCopyEngine is a **mean-reverting contrarian system** that follows smart Polymarket wallets into Kalshi KXBTC15M binary contracts. It is functional and live. The core wallet-flow edge works: CROSS_VENUE_FLOW YES entries are +$8.29 net, manual TP exits generate all the profit (+$34.24), and recent sessions (Mar 27–28) are showing 73–85% WR. However, the engine is sitting in a -$45 drawdown from its $35 peak due to three compounding problems: (1) the NO side loses massively below 44c (-$36.73) because asymmetric payouts destroy edge even at 57% WR; (2) the European session (08–14 UTC) bleeds -$29 across 291 trades without any filter; and (3) the new MTF system, if activated as-is, would **block the engine's best trades** — the engine is contrarian and the MTF measures the trend it fades, so the score is inverted relative to what you'd expect. The single biggest opportunity is the **inverted MTF filter**: instead of blocking trades that oppose the MTF trend, block trades that *align* with it. Combined with raising the NO floor to 48c and blocking European hours, the data suggests recovering most of the -$45 drawdown on a forward basis.

---

## 2. CRITICAL BUGS TO FIX

These are P0/P1 items from the codebase audit. Fix them in order. None require a service restart to take effect except where noted.

---

### BUG-01 — NameError: `unfilled` in partial TP fill path *(P0 — live crash risk)*

**File**: `polymarket_copy_engine.py` ~line 3956
**Function**: `_manage_position`
**What's broken**: When a TP order partially fills (fill count < target count), the code assigns `pos["count"] = unfilled` but `unfilled` is never defined. `unfilled_sells` is computed two lines earlier but is a different variable. The crash is silently swallowed by `except Exception: pass` at ~line 3974 — position count is never updated after a partial TP, causing all subsequent management logic to operate on a stale count.

**Exact fix**:
```python
# BEFORE (~line 3956):
pos["count"] = unfilled

# AFTER:
pos["count"] = total_count - total_filled
```

**Requires restart**: No (but takes effect immediately on next partial TP event).

---

### BUG-02 — NameError: `book` referenced before assignment in `_poll_kalshi_tape` *(P0 — will crash when kalshi_ws.py is added)*

**File**: `polymarket_copy_engine.py` ~line 1739
**Function**: `_poll_kalshi_tape`
**What's broken**: In the WebSocket path, `book` is only assigned in the `else` branch (when WS mid is invalid). If WS delivers a valid mid, `book` is never set but is referenced afterward. Currently masked because `kalshi_ws.py` doesn't exist yet — but the moment it's added, any window with a valid WS mid crashes this function silently.

**Exact fix**:
```python
# BEFORE (somewhere above the branch):
# book is never initialized

# AFTER — add this line before the if/else branch (~line 1734):
book = None  # guard against NameError if ws_mid path is taken

# AND guard the downstream usage (~line 1754):
if window_age <= 12.0 and book is not None and book.mid_cents ...
```

**Requires restart**: No.

---

### BUG-03 — MTF live-mode blocks PRIMARY/TREND_FOLLOW signals *(P1 — must fix before MTF_SHADOW_MODE = False)*

**File**: `polymarket_copy_engine.py` ~line 2941
**Function**: `_execute_signal` (MTF gate block)
**What's broken**: The live-mode MTF gate blocks ALL signal tiers including PRIMARY and TREND_FOLLOW on `is_opposing` or `NO_TRADE`. The architecture spec (ARCHITECTURE.md line 670) explicitly states PRIMARY and TREND_FOLLOW must never be blocked — only TA_FORCED. In live mode with this bug, a PRIMARY signal (highest validated WR) can be vetoed by a neutral MTF score during warmup.

**Exact fix**:
```python
# BEFORE (~line 2941-2956):
if not shadow:
    if _mtf_result.is_opposing:
        return
    if _mtf_result.action == "NO_TRADE":
        return

# AFTER:
_mtf_allow_block = signal.signal_tier not in ("PRIMARY", "TREND_FOLLOW")
if not shadow and _mtf_allow_block:
    if _mtf_result.is_opposing:
        return
    if _mtf_result.action == "NO_TRADE":
        return
# HIGH_CONFIDENCE size boost still applies to all tiers (leave that section unchanged)
```

**Requires restart**: Yes (code change).

---

### BUG-04 — Daily P&L resets on service restart *(P1 — loss limit protection gap)*

**File**: `polymarket_copy_engine.py` `__init__` and wherever `_daily_pnl` is updated
**Function**: `__init__`, `_update_daily_pnl` (or wherever P&L is incremented)
**What's broken**: `self._daily_pnl` is in-memory only. An NSSM restart (e.g., after a crash at -$13) resets the counter to 0. The engine can then lose another -$15 before halting again. Worst case: -$28 from a single bad day on two restart cycles.

**Exact fix**:
```python
# In __init__, after initializing _daily_pnl:
_pnl_file = Path(ENGINE_DIR) / "data" / "daily_pnl.json"
try:
    _saved = json.loads(_pnl_file.read_text())
    if _saved.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        self._daily_pnl = _saved.get("pnl", 0.0)
        self._daily_pnl_date = _saved["date"]
except Exception:
    pass  # fresh start is fine

# In the method that updates _daily_pnl (after every trade outcome):
try:
    _pnl_file.write_text(json.dumps({
        "date": self._daily_pnl_date,
        "pnl": self._daily_pnl
    }))
except Exception:
    pass
```

**Requires restart**: Yes (code change, but no data loss).

---

### BUG-05 — `_manage_position` swallows exceptions at DEBUG level *(P1 — silent failures in live position mgmt)*

**File**: `polymarket_copy_engine.py` ~line 4519
**Function**: `_manage_position`
**What's broken**: The entire position management block — TP checks, position sync, mandatory close — is wrapped in `except Exception as e: logger.debug(...)`. A failed orderbook fetch or TP crash logs at DEBUG (invisible in production logs) and the engine silently skips that management cycle. For live money this is unacceptable.

**Exact fix**:
```python
# BEFORE:
except Exception as e:
    logger.debug("CopyEngine position mgmt error: %s", e)

# AFTER:
except Exception as e:
    logger.warning("CopyEngine position mgmt error: %s", e, exc_info=True)
```

**Requires restart**: Yes (code change).

---

### BUG-06 — `user_config.py` comment/value mismatch on `MIN_MINUTES_REMAINING` *(P2 — misleading docs)*

**File**: `user_config.py` line 142
**What's broken**: Comment says "4min minimum" but value is `2.0`.

**Exact fix**:
```python
# BEFORE:
MIN_MINUTES_REMAINING = 2.0         # 4min minimum. No stop losses = no risk of instant stop on late entries.

# AFTER:
MIN_MINUTES_REMAINING = 2.0         # 2min minimum. No stop losses = no risk of instant stop on late entries.
```

---

## 3. MTF SIGNAL INVERSION — THE KEY FINDING

### What the data shows

After simulating MTF scores on 1,184 historical KXBTC15M trades (2026-03-13 to 2026-03-28):

| Alignment | N | Win Rate | Total P&L | Avg P&L/Trade |
|-----------|---|----------|-----------|---------------|
| **MTF Aligned** (score confirms direction) | 321 | **36.4%** | **-$24.70** | -$0.077 |
| **MTF Neutral** (|score| < 0.3) | 796 | **47.7%** | **+$9.11** | +$0.011 |
| **MTF Opposing** (score contradicts direction) | 67 | **68.7%** | **+$3.56** | +$0.053 |

**WR delta: aligned vs opposing = -32.2pp.** This is not noise. 1,184 trades with a clean monotonic pattern.

The quintile breakdown makes it undeniable:

| MTF Quintile | Avg Score | YES WR | NO WR | Interpretation |
|---|---|---|---|---|
| Q1 (most bearish) | -0.380 | **74%** | 37% | BTC crashing → YES contracts WIN |
| Q3 (neutral) | -0.035 | 53% | 37% | Flat → slight YES edge |
| Q5 (most bullish) | +0.357 | 46% | **61%** | BTC rallying → NO contracts WIN |

### Why it's inverted

The engine is **not** a trend-following system. It copies smart Polymarket wallets that are systematically **mean-reverting** — they fade BTC momentum to profit from contract price reversion to 50c. When BTC is in a strong uptrend (MTF very bullish), smart wallets are entering NO contracts (BTC will be lower at window close = mean reversion). The MTF system correctly identifies the trend. The engine correctly fades it. Using MTF as a standard trend filter would block exactly the trades the engine wins on.

### Per-timeframe correlation breakdown

Point-biserial correlation between TF score direction and win probability:

| Timeframe | Weight | Correlation (rpb) | Direction | Top-Tertile WR | Bottom-Tertile WR |
|---|---|---|---|---|---|
| **1m** | 22% | **+0.088** | **Positive (weak)** | 52.3% | 42.4% |
| **5m** | 28% | **-0.128** | **Inverted** | 41.7% | 56.9% |
| **15m** | 33% | **-0.152** | **Strongest inverse** | 38.6% | 53.6% |
| **1h** | 17% | **-0.084** | **Inverted** | 43.9% | 52.0% |

**Critical**: 1m is the *only* TF with positive predictive value. It measures immediate microstructure at entry, which genuinely confirms or denies the entry timing. The 5m/15m/1h are all inversely correlated — these measure the trend being faded.

**The weight problem**: 5m (28%) + 15m (33%) = 61% of the composite score is on the two most inversely-correlated timeframes. The composite score is therefore a reliable *contrary* indicator — high score = engine's worst trades; low score = engine's best trades.

### What to do: inverted filter logic

Do **not** use the standard filter (block opposing, allow aligned). Use the **inverted** filter:

```python
# In polymarket_copy_engine.py, replace the MTF gate block at ~line 2941.
# Apply AFTER the tier guard from BUG-03.

# Compute side-adjusted score: positive = MTF agrees with trade, negative = MTF opposes trade
_side_score = _mtf_score if signal.side == "yes" else -_mtf_score

if not shadow and _mtf_allow_block:
    # INVERTED LOGIC: Strong MTF alignment = engine's WORST entry
    # (Strong trend = engine is entering at momentum peak, mean-reversion fails)
    if _side_score >= 0.5:
        # Strong trend confirms our direction → veto for non-trend-follow tiers
        logger.info(
            "MTF INVERTED VETO: side=%s score=%.2f — high-conviction trend entry, "
            "contrarian engine at momentum peak, blocking",
            signal.side, _mtf_score
        )
        return

    elif _side_score >= 0.3:
        # Moderate alignment → reduce size
        _mtf_size_mult = 0.75
        logger.debug("MTF INVERTED SIZE_DOWN: score=%.2f, mult=0.75", _mtf_score)

    elif _side_score <= -0.3:
        # MTF opposes our direction → engine's sweet spot, boost size
        _mtf_size_mult = 1.25
        logger.debug("MTF INVERTED BOOST: score=%.2f, mult=1.25", _mtf_score)

    elif _side_score <= -0.5:
        # Strong MTF opposition → highest quality contrarian entry
        _mtf_size_mult = 1.5
        logger.debug("MTF INVERTED HIGH_BOOST: score=%.2f, mult=1.5", _mtf_score)
```

**Expected impact (from simulation)**:
- Blocks 321 aligned trades that contributed -$24.70 → saves those losses
- Boosts size on 67 opposing trades that contributed +$3.56 at 68.7% WR → amplifies edge
- Retains 796 neutral trades (+$9.11, 47.7% WR) — these are unchanged

### Important: validate on 200+ real shadow trades first

The simulation is a reconstruction using TFAnalyzer logic on historical Binance data. The real engine uses live WebSocket-fed state. Before enabling:

```sql
-- Run after 200+ real shadow trades in kalshi_trades:
SELECT
    CASE
        WHEN (side='yes' AND mtf_score >= 0.3) OR (side='no' AND mtf_score <= -0.3)
            THEN 'aligned'
        WHEN (side='yes' AND mtf_score <= -0.3) OR (side='no' AND mtf_score >= 0.3)
            THEN 'opposing'
        ELSE 'neutral'
    END AS alignment,
    COUNT(*) AS n,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending','unfilled')
GROUP BY 1;
```

**Go/no-go criteria**: Proceed with inverted live filter when aligned WR < 42% AND n ≥ 50 per bucket in real shadow data.

---

## 4. DYNAMIC TP TIED TO MTF CONFIDENCE

### MFE/MAE evidence by confidence bucket

BTC price movement during the 15m window, scaled to Kalshi cents (1% BTC move ≈ 2c Kalshi shift):

| Confidence | N | Win Rate | MFE p50 (BTC%) | MAE p50 (BTC%) | MFE/MAE Ratio | Total P&L |
|---|---|---|---|---|---|---|
| HIGH aligned (≥0.6) | 4 | 0.0% | 0.061% | **0.194%** | 0.31 | -$4.18 |
| MEDIUM aligned (0.3–0.6) | 317 | 36.9% | 0.114% | 0.164% | 0.70 | -$20.52 |
| NEUTRAL (|score| < 0.3) | 796 | 47.7% | 0.090% | 0.122% | 0.74 | +$9.11 |
| **OPPOSING** (score > 0.3 against side) | 67 | **68.7%** | **0.162%** | **0.086%** | **1.88** | +$3.56 |

**Key**: Opposing trades have MFE/MAE ratio of 1.88 — nearly 2:1 favorable excursion vs adversity. These entries travel the furthest with the least headwind. They warrant wider TPs to capture more of the move. Aligned trades have 0.31 ratio — they move against immediately.

### Proposed TP tiers per confidence level

| Confidence | Tier1 TP | Tier2 TP | Trail Activation | Trail Distance | Size Mult |
|---|---|---|---|---|---|
| OPPOSING strong (side_score ≤ -0.5) | **+10c** | **+16c** | +18c | 6c | 1.5x |
| OPPOSING moderate (side_score -0.3 to -0.5) | **+8c** | **+13c** | +15c | 5c | 1.25x |
| NEUTRAL (|side_score| < 0.3) | +7c | +11c | +15c | 5c | 1.0x |
| ALIGNED moderate (side_score 0.3–0.5) | +5c | +8c | +12c | 4c | 0.75x |
| ALIGNED strong (side_score ≥ 0.5) | **VETO** | — | — | — | 0x |

**Rationale for wider opposing TPs**: Opposing trades show MFE p50 = 0.162% BTC ≈ 3.2c Kalshi equivalent. Current Tier1 at +7c (entry×1.15 at 45c) often exceeds median MFE and exits prematurely. A Tier1 at +10c captures more of the contrarian reversion before momentum stalls.

**Important caveat**: The TP backtest showed Hold-to-Expiry outperforms all TP strategies by +$19 on this dataset. The engine currently runs in Hold-to-Expiry mode (all exits disabled per CLAUDE.md). **Do not re-enable TP logic until that decision is revisited consciously.** The dynamic TP table above is a design target for when TP exits are re-enabled — it should not be the first thing you implement.

### Code integration point in `_manage_position`

When TP exits are re-enabled, add this before `_place_tiered_tp` is called:

```python
# In _execute_signal, after computing _mtf_size_mult and _mtf_score:
_side_score = _mtf_score if side == "yes" else -_mtf_score

# TP tier selection based on MTF confidence
if _side_score <= -0.5:
    _tp_tier1_c = 10  # opposing strong — widest TP
    _tp_tier2_c = 16
    _trail_activation_c = 18
    _trail_distance_c = 6
elif _side_score <= -0.3:
    _tp_tier1_c = 8   # opposing moderate
    _tp_tier2_c = 13
    _trail_activation_c = 15
    _trail_distance_c = 5
elif _side_score >= 0.5:
    pass  # veto handled above — never reaches here
elif _side_score >= 0.3:
    _tp_tier1_c = 5   # aligned moderate — tight TP
    _tp_tier2_c = 8
    _trail_activation_c = 12
    _trail_distance_c = 4
else:
    _tp_tier1_c = 7   # neutral — current baseline
    _tp_tier2_c = 11
    _trail_activation_c = 15
    _trail_distance_c = 5

# Pass _tp_tier1_c/_tp_tier2_c into _place_tiered_tp(...)
```

---

## 5. CONFIG CHANGES

Complete before/after diff for `user_config.py`. All changes require a service restart.

### Change 1: Raise NO floor to 48c

```python
# BEFORE:
MIN_ENTRY_CENTS_NO = 40             # Same for NO.

# AFTER:
MIN_ENTRY_CENTS_NO = 48             # NO side: 48c floor. Data: CVF NO <44c is -$36.73 (82 trades). 45-54c is +$16.95.
```

**Why**: CVF NO entries below 44c have catastrophic P&L (-$36.73, including 0% WR at <35c). The asymmetric payout on NO contracts means a 57% WR still loses money if avg loss is 2.1× avg win — which it is when buying NO at 30–45c (full dollar_risk lost if YES settles). At 48c+, NO is profitable.

### Change 2: Raise YES ceiling to 90c

```python
# BEFORE:
MAX_ENTRY_CENTS = 55                # Original sweet spot — 83% WR in backtests

# AFTER:
MAX_ENTRY_CENTS = 90                # DATA: 70c+ band = 82.8% WR, 99 trades, +$5.12. Current 55c cap blocks the best band.
```

**Why**: The 70c+ band has 82.8% WR on 99 trades — the single best price band by win rate. CVF at 70c+ is 88.6% WR, +$10.63. The current 55c cap is blocking these entries entirely. Note: entries above 83c use the tight -5c stop (`HIGH_ENTRY_STOP_THRESHOLD = 83`) — this should remain in place.

### Change 3: Block European session hours

```python
# BEFORE:
BLOCKED_HOURS = set()                     # Trade 24/7 — no blocked hours

# AFTER:
BLOCKED_HOURS = set(range(8, 14))         # Block 08-13 UTC (European session). Data: -$28.99 across 291 trades, 41.6% WR.
```

**Why**: The European session (08–14 UTC) is the engine's worst period across all strategies. CVF at 11–12 UTC alone is -$13.63. Best hours (02 UTC: 77.8% WR, 17 UTC: 68.1% WR, 15 UTC: 56.7% WR) are all outside this block. The session block only affects new entries — position management continues.

**Note**: BLOCKED_HOURS currently has a stale comment in CLAUDE.md saying it was "cleared 2026-03-30" as TA_FORCED fills gaps. TA_FORCED can still fire outside blocked hours — this change only prevents new entries during the lossy European session.

### Change 4: Fix stale comment on MIN_MINUTES_REMAINING

```python
# BEFORE:
MIN_MINUTES_REMAINING = 2.0         # 4min minimum. No stop losses = no risk of instant stop on late entries.

# AFTER:
MIN_MINUTES_REMAINING = 2.0         # 2min minimum. No stop losses = safe to enter within 2min of expiry.
```

### Change 5: Document that TAKE_PROFIT_CENTS is dead config

```python
# BEFORE:
TAKE_PROFIT_CENTS = 5               # Sell at entry + 5c (e.g. buy 50c → sell 55c)

# AFTER:
TAKE_PROFIT_CENTS = 5               # NOTE: Not currently read by active engine. TP is hardcoded in _place_tiered_tp. Edit this file has no effect.
```

### Complete proposed `user_config.py` diff summary

| Setting | Current | Proposed | Impact |
|---|---|---|---|
| `MIN_ENTRY_CENTS_NO` | 40 | **48** | Block NO <48c, save ~$36.73 |
| `MAX_ENTRY_CENTS` | 55 | **90** | Capture 70c+ band (82.8% WR) |
| `BLOCKED_HOURS` | `set()` | `set(range(8, 14))` | Block European session (-$28.99) |
| `MIN_MINUTES_REMAINING` comment | "4min" | "2min" | Fix misleading docs |
| `TAKE_PROFIT_CENTS` comment | silent | Add dead-config warning | Prevent user confusion |

---

## 6. ENTRY BAND OPTIMIZATIONS

### No floor at 48c for YES

The data shows YES entries below 45c are driven primarily by HFT scalps (< 35c band: 278 trades, 29.5% WR but net +$7.86 due to favorable odds). CVF YES below 35c is catastrophic (8.8% WR, -$26.79) but this is already blocked by the existing MIN_ENTRY_CENTS = 40. The current YES floor of 40c is reasonable — raising it to 45c costs you the 40–44c band (+$0 to slightly negative) without meaningful benefit. **Keep YES floor at 40c (current setting).**

### Full P&L by band table (CVF specifically)

| Band | N | CVF Win Rate | CVF P&L | Action |
|---|---|---|---|---|
| < 35c | 57 | 8.8% | **-$26.79** | Already blocked by MIN_ENTRY_CENTS=40 |
| 35–39c | 26 | 26.9% | +$0.56 | Borderline |
| 40–44c | 25 | 28.0% | -$9.94 | Losing but small sample |
| **45–49c** | 32 | **56.2%** | **+$10.16** | Sweet spot — keep |
| **50–54c** | 28 | **71.4%** | **+$6.79** | Best value zone |
| 55–59c | 38 | 44.7% | -$11.93 | Losing — but this is NO side bleeding |
| **60–64c** | 24 | **79.2%** | **+$3.74** | Strong — MAX_ENTRY=55 currently blocks this |
| **70c+** | 70 | **88.6%** | **+$10.63** | Best band — MAX_ENTRY=55 currently blocks this |

Raising MAX_ENTRY_CENTS to 90 unlocks the 60–64c (+$3.74) and 70c+ (+$10.63) bands while correctly applying the tight stop at 83c+ (mitigates high-entry risk).

### European session block: hourly P&L breakdown

| UTC Hour | Trades | Win Rate | P&L | Session |
|---|---|---|---|---|
| 02:00 | 27 | 77.8% | **+$6.45** | Asia overnight ✓ |
| 05:00 | 64 | 54.7% | +$7.15 | Asia ✓ |
| **08:00** | 52 | **30.8%** | **-$5.77** | **Europe → BLOCK** |
| **11:00** | 58 | **36.2%** | **-$5.80** | **Europe → BLOCK** |
| **12:00** | 46 | **39.1%** | **-$6.76** | **Europe → BLOCK** |
| **14:00** | 55 | **30.9%** | **-$12.37** | **Europe → BLOCK** |
| 15:00 | 30 | 56.7% | +$11.70 | US open ✓ |
| 17:00 | 72 | 68.1% | **+$15.29** | US ✓ |
| 18:00 | 43 | 55.8% | +$6.05 | US ✓ |

Total European 08–13 UTC: **-$28.99**, 291 trades, 41.6% WR. Total non-European hours: positive or near-zero.

### MIMIC already disabled

`MIMIC_ENABLED = False` in current `user_config.py`. No action needed. For reference: MIMIC_SMART_FLOW was -$13.07 total (-$0.204/trade, 64 trades) — the worst risk-adjusted strategy. Correctly disabled.

---

## 7. PAPER TRADING / BACKTEST USAGE

### Paper trading (zero-risk live market simulation)

**Activate by setting in `user_config.py`**:
```python
PAPER_TRADING = True
PAPER_STARTING_BALANCE = 100.0      # Virtual balance
PAPER_SLIPPAGE_CENTS = 1            # Simulated fill cost
```

Then start the engine normally:
```bash
python polymarket_copy_engine.py
# OR via NSSM (restart service after config change)
nssm restart BTCBiasEngine
```

**What happens**: `PaperTrader` intercepts all `place_order()` and `cancel_order()` calls. No real Kalshi orders are placed. All read-only calls (orderbook, positions, contract discovery) use the live market. Trades are logged to `data/signals.db` → `paper_trades` table.

**View paper results**:
```bash
cd C:\Trading\btc-bias-engine
python -c "
import sqlite3
conn = sqlite3.connect('data/signals.db')
rows = conn.execute('''
    SELECT DATE(placed_at), COUNT(*),
    ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS wr,
    ROUND(SUM(pnl),2) AS pnl
    FROM paper_trades
    WHERE status NOT IN (\"pending\",\"unfilled\")
    GROUP BY DATE(placed_at) ORDER BY 1
''').fetchall()
for r in rows: print(r)
"
```

**What to look for**:
- WR should be ≥ 47% (neutral MTF baseline) if changes are working
- NO side P&L should be positive (validating 48c floor)
- Sessions outside 08–13 UTC should outperform inside (validating session block)
- Run for at least 2 weeks / 100+ trades before drawing conclusions

**Critical safety check before going live**:
```python
# Verify PAPER_TRADING is False before restarting with real money:
python -c "import user_config; print('PAPER_TRADING =', user_config.PAPER_TRADING)"
```

### Backtest (TA_FORCED historical simulation)

**Run against last 30 days of BTC data**:
```bash
cd C:\Trading\btc-bias-engine
python backtest.py --days 30
```

**Run shorter window**:
```bash
python backtest.py --days 7 --symbol BTCUSDT
```

**What it does**:
1. Fetches historical 1m BTC/USDT OHLCV from Binance REST
2. Groups candles into 15m windows (matching Kalshi KXBTC15M)
3. Runs TAScorer on the preceding 1m candles (same logic as TA_FORCED live)
4. Determines true direction from 15m candle close >= open
5. Applies current config filters (entry bands from user_config, blocked hours)
6. Compares OLD config (35–64c) vs NEW config (YES 40–90c / NO 48–59c)
7. Saves `data/backtest_results_{timestamp}.json`

**What to look for**:
```
NEW CONFIG vs OLD CONFIG comparison:
  Total P&L:    NEW > OLD by ~10-15% (entry band changes)
  Win Rate:     Should improve ~2-3pp with blocked hours
  Max Drawdown: Should reduce ~20% with session filter
  Trades/day:   Will drop ~20% (session block removes European volume)

Hourly breakdown (NEW vs OLD):
  08-13 UTC:    $0 (blocked) vs negative (improvement)
  15-19 UTC:    Unchanged or better (US session unaffected)

Entry band breakdown:
  40-44c:       Some losses but low volume — acceptable
  45-54c:       Should be most profitable band
  70c+:         New entries here — validate 80%+ WR in backtest
```

**Backtest limitations**: The backtest only covers TA_FORCED signals. It does not simulate CROSS_VENUE_FLOW (wallet signals). The wallet-flow edge cannot be backtested against historical data (Polymarket wallet activity is not stored). Use paper trading for CVF validation.

### MTF shadow data validation queries

After 200+ real shadow trades, run these against `data/trades.db`:

```sql
-- Score distribution (should be roughly bell curve centered near 0)
SELECT
    ROUND(mtf_score * 10) / 10 AS score_bucket,
    COUNT(*) AS trades
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1 ORDER BY 1;

-- Primary hypothesis: aligned vs opposing WR (expect aligned < neutral < opposing)
SELECT
    CASE
        WHEN (side='yes' AND mtf_score >= 0.3) OR (side='no' AND mtf_score <= -0.3)
            THEN 'aligned'
        WHEN (side='yes' AND mtf_score <= -0.3) OR (side='no' AND mtf_score >= 0.3)
            THEN 'opposing'
        ELSE 'neutral'
    END AS alignment,
    COUNT(*) AS n,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(pnl), 2) AS total_pnl
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending', 'unfilled')
GROUP BY 1;

-- Go/no-go thresholds:
-- PROCEED with inverted filter: aligned WR < 42% AND n >= 50 per bucket
-- HALT: if aligned WR > 50% (simulation may have reconstruction error, re-examine)
-- NEUTRAL ONLY: if neutral WR > 52% (consider blocking both aligned and opposing)
```

---

## 8. IMPLEMENTATION ORDER

Work through phases in order. Do not skip to a later phase until the prior phase is validated. Each phase is tagged with its risk profile and whether a service restart is required.

---

### PHASE 1: Critical Bug Fixes
**Risk**: Low (fixes silent bugs, no behavior change in happy path)
**Restart required**: Yes (code changes)
**Do this first — these are live crashes waiting to happen**

- [ ] **1.1** Fix BUG-01: Replace `unfilled` with `total_count - total_filled` in `_manage_position` (~line 3956)
- [ ] **1.2** Fix BUG-02: Initialize `book = None` before the WebSocket branch in `_poll_kalshi_tape` (~line 1734); add `book is not None` guard on downstream reference
- [ ] **1.3** Fix BUG-05: Change `logger.debug` to `logger.warning` in the `_manage_position` exception handler (~line 4519)
- [ ] **1.4** Fix BUG-06: Update stale comment on `MIN_MINUTES_REMAINING` in `user_config.py`
- [ ] **1.5** Restart service: `nssm restart BTCBiasEngine` (admin PowerShell)
- [ ] **1.6** Verify in logs: no new `NameError` in first 30 min; `WARNING` messages appear for position management errors (if any occur)

---

### PHASE 2: Config Changes
**Risk**: Medium (live trading behavior changes)
**Restart required**: Yes
**Expected impact**: +$20–30 forward P&L reduction in losses from entry band changes; -20% trade volume from session block**

- [ ] **2.1** Edit `user_config.py`: raise `MIN_ENTRY_CENTS_NO` from 40 → **48**
- [ ] **2.2** Edit `user_config.py`: raise `MAX_ENTRY_CENTS` from 55 → **90**
- [ ] **2.3** Edit `user_config.py`: set `BLOCKED_HOURS = set(range(8, 14))`
- [ ] **2.4** Run backtest to validate: `python backtest.py --days 14`
  - Confirm: NEW config P&L > OLD config P&L in backtest output
  - Confirm: 08–13 UTC shows $0 (blocked)
  - Confirm: 70c+ entries appear in backtest and show >75% WR
- [ ] **2.5** Restart service: `nssm restart BTCBiasEngine`
- [ ] **2.6** Monitor for 24 hours: confirm no entries during 08–13 UTC in logs; confirm 70c+ entries appearing
- [ ] **2.7** After 3 days / 30+ trades: check daily P&L — should be improved vs pre-change baseline

---

### PHASE 3: MTF Tier Guard (prerequisite for Phase 4)
**Risk**: Low (only affects what happens when MTF_SHADOW_MODE = False, which it isn't yet)
**Restart required**: Yes
**This is a required fix, but has no trading impact until Phase 4**

- [ ] **3.1** Fix BUG-03: Add tier guard to MTF gate in `_execute_signal` (~line 2941):
  ```python
  _mtf_allow_block = signal.signal_tier not in ("PRIMARY", "TREND_FOLLOW")
  if not shadow and _mtf_allow_block:
      # ... existing block logic
  ```
- [ ] **3.2** Restart service: `nssm restart BTCBiasEngine`
- [ ] **3.3** Verify in logs: shadow mode logging still appears every signal; no unexpected change in trade frequency
- [ ] **3.4** Continue accumulating MTF shadow data. Check count:
  ```sql
  SELECT COUNT(*) FROM kalshi_trades WHERE mtf_score IS NOT NULL AND status NOT IN ('pending','unfilled');
  ```
  Must reach ≥200 before proceeding to Phase 4.

---

### PHASE 4: MTF Inversion Filter (live mode)
**Risk**: High (significant behavior change — blocks 27% of current trade volume)
**Restart required**: Yes
**Do NOT proceed until Phase 3 is complete AND 200+ shadow trades are validated**

- [ ] **4.1** Run the shadow validation queries from Section 7 against real data
  - Confirm: aligned WR < 42% (real data)
  - Confirm: opposing WR > 55% (real data, at least directionally)
  - If aligned WR > 50%: **STOP — do not proceed, investigate reconstruction error**
- [ ] **4.2** Replace the MTF gate logic in `_execute_signal` with the inverted filter from Section 3 (the `_side_score` based block/size-adjust logic)
- [ ] **4.3** Leave `MTF_SHADOW_MODE = True` for one more week — run inverted logic as shadow first to confirm shadow logs show the expected veto pattern
- [ ] **4.4** Verify shadow logs show `MTF INVERTED VETO` firing on aligned signals; `MTF INVERTED BOOST` firing on opposing signals
- [ ] **4.5** Set `MTF_SHADOW_MODE = False` in `user_config.py`
- [ ] **4.6** Restart service: `nssm restart BTCBiasEngine`
- [ ] **4.7** Monitor first 20 live-mode trades:
  - Check: no PRIMARY or TREND_FOLLOW signals are being blocked
  - Check: TA_FORCED veto fires on strong aligned signals
  - Check: trade volume drops ~25% (aligned trades are being blocked)
  - After 50 trades: run alignment query again — confirm aligned WR still below neutral

---

### PHASE 5: Paper Mode Validation Period
**Risk**: Zero (simulation only)
**Use this to validate combined Phase 2+3+4 changes before committing real capital to new config**

- [ ] **5.1** Set `PAPER_TRADING = True`, `PAPER_STARTING_BALANCE = 100.0`
- [ ] **5.2** Set all Phase 2 config changes in place
- [ ] **5.3** Keep `MTF_SHADOW_MODE = True` (paper mode + shadow = safest validation)
- [ ] **5.4** Run for minimum 2 weeks / 100+ paper trades
- [ ] **5.5** Check paper P&L weekly:
  ```sql
  SELECT DATE(placed_at), COUNT(*),
         ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS wr,
         ROUND(SUM(pnl),2) AS pnl
  FROM paper_trades WHERE status NOT IN ('pending','unfilled')
  GROUP BY 1 ORDER BY 1;
  ```
- [ ] **5.6** Decision criteria to go live:
  - Paper WR ≥ 48% (above neutral baseline)
  - Paper P&L trend positive (not just W12-style streak)
  - NO side P&L ≥ 0 (48c floor working)
  - 08–13 UTC shows $0 (session block working)
- [ ] **5.7** Before disabling paper mode: `python -c "import user_config; assert not user_config.PAPER_TRADING, 'Paper mode still on!'"` — add this as a pre-flight check

---

### PHASE 6: Dynamic Profit Protection (Time-Scaled Trailing Profit Lock)
**Risk**: Medium — new exit path, but additive-only (no existing code changed)
**Restart required**: Yes
**Full spec**: See `DYNAMIC_PROFIT_PROTECTION.md`
**Do NOT proceed until Phase 5 paper trading is complete and CVF is validated profitable**

The core problem: hold-to-expiry is optimal on average, but positions that reach a meaningful high-water mark (HWM ≥ entry + 8c) and then reverse in the final minutes give back all gains plus the full loss. The profit lock adds a time-scaled trailing stop that only activates when the position was actually profitable — it does nothing for positions that never moved in our favor.

**How it works**: Track HWM unconditionally. Once HWM ≥ entry + 8c, activate a trail whose distance shrinks as time runs out:

| Phase | Minutes since fill | Trail | Behavior |
|---|---|---|---|
| 0 | 0–5 min | 12c | Ignore — early moves are noise |
| 1 | 5–8 min | 8c | Wide — trend still developing |
| 2 | 8–12 min | 5c | Medium — direction should be established |
| 3 | 12+ min | 3c | Tight — any reversal is likely real |
| Final | <60s left + profit ≥ 15c | immediate | Lock profit before settlement race |

Near-certain exception: if bid ≥ 90c, trail is suppressed (settlement is near-certain).

**Implementation checklist**:

- [ ] **6.1** Add `PROFIT_LOCK_*` config block to `user_config.py` (see spec Section 3.4), with `PROFIT_LOCK_ENABLED = False` and `PROFIT_LOCK_SHADOW = True` initially
- [ ] **6.2** Add 13 config constant loads to `polymarket_copy_engine.py` (near the `SIGNAL_STOP_CENTS` block, ~line 200–300)
- [ ] **6.3** Add `_profit_lock_phase()` helper method (~line 3861, near `_place_tiered_tp`)
- [ ] **6.4** Add unconditional HWM update after line 4283 (`profit_cents = bid - entry`)
- [ ] **6.5** Insert shadow mode profit lock block after line 4401 (TP decay lower block end) — log `PROFIT LOCK SHADOW` events but don't sell
- [ ] **6.6** Restart service: `nssm restart BTCBiasEngine`
- [ ] **6.7** Run in shadow mode 5–7 days. Validate:
  ```bash
  grep "PROFIT LOCK SHADOW" data/engine_history.log | grep "would_exit=True" | head -30
  ```
  - Phase 0 exits should be rare (trail = 12c — almost impossible to trigger)
  - Phase 2/3 exits should show positive `would_pnl` on average
  - No exits when bid ≥ 90c (near-certain suppression working)
- [ ] **6.8** Tune trail distances if needed: if Phase 2 (5c at 8–12min) shows >30% false positives (would-exit trades that would have settled favorably), raise Phase 2 trail to 7c
- [ ] **6.9** Add DB columns (optional but recommended):
  ```sql
  ALTER TABLE kalshi_trades ADD COLUMN hwm_cents INTEGER;
  ALTER TABLE kalshi_trades ADD COLUMN exit_trail_phase INTEGER;
  ```
  Update `signal_logger.py` `log_kalshi_outcome()` to accept and write these fields
- [ ] **6.10** Enable live mode: set `PROFIT_LOCK_ENABLED = True`, `PROFIT_LOCK_SHADOW = False`
- [ ] **6.11** Restart service and monitor first 20 exits:
  - `PROFIT LOCK EXIT` appears in logs (not just SHADOW)
  - No exits at Phase 0 trail (trail = 12c should almost never fire)
  - `exited_win` count in DB increasing
- [ ] **6.12** After 50 profit-lock exits, run impact query (from spec Section 6.3) to measure actual vs projected improvement

**Expected impact**: ~30% of CVF `lost` trades were profitable at some point during the window. Converting those from -$1.19/trade to +$0.30/trade implies +$70 improvement per historical period. Actual impact depends on HWM distribution — shadow mode data will give a precise estimate before going live.

**What this does NOT do**: Does not affect losing positions (never reached HWM threshold). Does not fire on positions above 90c. Does not replace TPs — the 63c/70c resting orders remain active and take priority if they fill first.

---

## Quick Reference: What Breaks What

| Change | Requires Restart | Risk | Expected P&L Impact |
|---|---|---|---|
| BUG-01 (unfilled NameError) | Yes | Low | Prevents silent position count corruption |
| BUG-02 (book NameError) | Yes | Low | No current impact; prevents future crash |
| BUG-03 (MTF tier guard) | Yes | Low | No trading impact until Phase 4 |
| BUG-04 (daily P&L persist) | Yes | Low | Correct loss limit across restarts |
| BUG-05 (debug→warning) | Yes | Low | Visibility improvement only |
| Raise NO floor 40→48c | Yes | Medium | Expected -$36/15day cycle reduction in losses |
| Raise MAX_ENTRY 55→90c | Yes | Medium | Expected +$10-15 capture from 70c+ band |
| Block 08-13 UTC | Yes | Medium | Expected -$29 loss avoidance per 15-day cycle |
| MTF tier guard | Yes | Low | Prerequisite for Phase 4 |
| MTF inverted filter (live) | Yes | High | Expected -$24.70 loss avoidance from blocking aligned trades |
| Dynamic TP per confidence | Yes | High | Small impact vs Hold-to-Expiry; implement last |
| Profit lock (shadow mode) | Yes | Low | Validation only — measures HWM/trail behavior |
| Profit lock (live mode) | Yes | Medium | Expected +$50-70 per historical period on CVF |

---

*All line numbers are approximate from the 2026-04-01 audit. Verify exact locations before editing.*
*Generated from: CODEBASE_AUDIT.md · MTF_REVIEW.md · MTF_ACCURACY_AND_DYNAMIC_TP_ANALYSIS.md · TRADING_ANALYSIS_20260328.md*
