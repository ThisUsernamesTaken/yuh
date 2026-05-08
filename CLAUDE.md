# BTC Bias Engine — Current System Reference

**Last updated**: 2026-05-07 (multi-tier overhaul + fill-rate tuning)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live tiers (cascade order)**: `UNIFIED` → `DIRECTION` → `BB_PURE` (gated off when UNIFIED on) → `PENNY_MODE`
**Status**: Multi-tier architecture with universal safety layer. Active exit management on all DIRECTION-class fills.

> **For the AI agent inheriting this session**: as of 2026-05-07 PT, the
> engine runs **four entry tiers** in cascade with a universal $15/window
> risk cap and 1-entry-per-window count cap. All fills route through the
> same exit layer (5 rules: hold-certain, wall-exit, trail-exit, loss-cut,
> pre-expiry). DIRECTION's "holds to settlement" promise is **NO LONGER
> ACCURATE** — the exit layer is active by default.
>
> - Pure modules: `direction_strategy.py`, `unified_scorer.py`
> - Engine handlers: `_unified_tick`, `_direction_tick`, `_evaluate_bb_pure_signal`,
>   `_evaluate_penny_signal`, `_direction_manage_exit`
> - **Position state**: `self._direction_position` for ALL tiers
>   (DIRECTION + UNIFIED + PENNY all use it; BB_PURE uses
>   `self._open_position`). The `tier` field distinguishes them.
> - **Universal safety**: `_check_window_safety()` + `_record_window_fill()`
>   wrapped around every place_order in every tier
>
> **Today's lessons (2026-05-07)**:
> 1. Manual user trading at 12:02 PT (308ct YES @ 22c) crashed BAL to
>    $6.22 — engine's DIRECTION_DAILY_LOSS_HALT engaged correctly at
>    11:54:45. Position settled YES → +$236 → BAL recovered to $79.72.
> 2. `MANUAL_FILLS_CAPTURE_ENABLED = False` — engine no longer adopts
>    manual trades into state. Manual trades stay invisible to engine.
> 3. Confirm-with-2nd-fetch BAL protocol: Kalshi shows transient low
>    BAL during settlement (07:52 case: $11.29 → $84.79 in 90s). Always
>    confirm a catastrophic BAL drop with a 2nd fetch ~10s later before
>    stopping engine.
> 4. Depth wall is structural at 1-2ct per Kalshi 15m offer-side tier.
>    Solution: `DIRECTION_CONTRACTS=1` (effective max 1ct after multiplier
>    cap of 1.5x). Trade-off: 50% smaller per-trade $ but ~100x higher
>    fill rate.

This document is the operator-facing source of truth. **If this doc and
code disagree, the code wins.** Update this doc whenever signal logic or
config defaults materially change.

> **Setting up the engine on a new machine?** See [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md)

---

## What the engine does

Trades Kalshi `KXBTC15M` 15-minute BTC binary options through 4 entry
tiers, each with its own thesis. Universal $15/window risk cap +
1-entry-per-window count cap means **only one tier fills per window**.

```
[ tick cycle ]  
   │
   ├─ _unified_tick()           ← Tier 0: composite 8-component scorer
   │                              (bb_mispricing+momentum+lag+book+
   │                               taker_flow+ta+timing+wall)
   │                              Strict gates: ev≥3c, conf≥0.25
   │
   ├─ _direction_tick()         ← Tier 1: dist≥0.10% + mom≥$10 momentum
   │                              IOC at ask+slip (adaptive, max 12c)
   │
   ├─ _evaluate_bb_pure_signal  ← Tier 2: BB-fair-value mispricing scalper
   │                              (gated off when UNIFIED enabled)
   │
   └─ _evaluate_penny_signal()  ← Tier 3: ≤12c asymmetric long-tail bets
                                  (when 1+2+3 all decline)
```

After fill, **`_direction_manage_exit()`** runs every tick on
`self._direction_position`, evaluating in order:

1. **C HOLD-CERTAIN**: bid ≥ 90c → hold for $1 settlement
2. **A WALL-EXIT**: opposing aggressor ≥30ct/s for 3s (if profitable)
3. **B TRAIL-EXIT**: phase-gated trailing stop on hwm_bid:
   - 0-5m  → 15c trail
   - 5-10m → 8c trail
   - 10-13m → 5c trail
   - 13m+ or ≤120s left → 3c trail
4. **D LOSS-CUT**: unrealized loss ≥ 20c/contract → sell
5. **E PRE-EXPIRY**: profitable + ≤90s left → sell

Sells go through `_place_capped_side_sell` (MIN-TRUTH + OVERSELL-GUARD).

**Settlement remains the fallback**: if no exit rule fires, Kalshi
auto-credits $1 per winning contract at expiry. But active exits trigger
on the vast majority of profitable trades now.

---

## Critical config (live values, post 2026-05-07 afternoon tuning)

```python
# Live trading
PAPER_TRADING                        = False
MANUAL_FILLS_CAPTURE_ENABLED         = False  # engine ignores manual trades

# UNIVERSAL SAFETY LAYER (Phase 1, 2026-05-07)
MAX_RISK_PER_WINDOW_DOLLARS          = 15.0   # cap across ALL tiers
MAX_ENTRIES_PER_WINDOW               = 1      # one fill cross-tier per window

# UNIFIED SCORER (Tier 0, primary entry path)
UNIFIED_SCORER_ENABLED               = True
UNIFIED_MIN_EV_C                     = 3      # was 5; loosened for fire rate
UNIFIED_MIN_CONFIDENCE               = 0.25   # was 0.30; loosened
UNIFIED_MIN_SECONDS                  = 90
UNIFIED_MAX_CONTRACTS                = 5
UNIFIED_KELLY_CAP_FRAC               = 0.10
UNIFIED_TAKER_SLIPPAGE_C             = 5
UNIFIED_WEIGHTS = {
    "bb_mispricing":    0.15,
    "btc_momentum":     0.35,    # momentum-heavy preset
    "kalshi_lag":       0.15,
    "book_imbalance":   0.10,
    "taker_flow":       0.10,
    "ta_composite":     0.08,
    "session_timing":   0.07,
    "wall_consumption": 0.05,
}

# DIRECTION (Tier 1, momentum)
DIRECTION_STRATEGY_ENABLED           = True
DIRECTION_DIST_THRESHOLD_PCT         = 0.0010  # 0.10% from strike
DIRECTION_MOMENTUM_THRESHOLD_DOLLARS = 10
DIRECTION_MAX_OFFSET_S               = 600
DIRECTION_CONTRACTS                  = 1       # was 2; reduced to fit depth
DIRECTION_CONVICTION_SIZING_ENABLED  = True    # 0.7-1.5x multiplier
DIRECTION_TAKER_SLIPPAGE_C           = 5       # IOC at ask+5c
DIRECTION_MAX_SLIP_C                 = 12      # was 8; adaptive ceiling
DIRECTION_DEPTH_CHECK_ENABLED        = True    # pre-IOC depth scan
DIRECTION_ADAPTIVE_SLIP_ENABLED      = True
DIRECTION_NOFILL_COOLDOWN_S          = 30.0    # was 5s
DIRECTION_NOFILL_REQUIRE_ASK_DELTA_C = 2
DIRECTION_DAILY_LOSS_HALT_FRAC       = 0.20
DIRECTION_HALT_LOG_THROTTLE_S        = 60.0

# DIRECTION exit layer (replaces hold-to-settlement)
DIRECTION_EXIT_ENABLED               = True
DIRECTION_TRAIL_PHASE1_C             = 15      # 0-5m
DIRECTION_TRAIL_PHASE2_C             = 8       # 5-10m
DIRECTION_TRAIL_PHASE3_C             = 5       # 10-13m
DIRECTION_TRAIL_PHASE4_C             = 3       # 13m+ / ≤120s left
DIRECTION_MAX_LOSS_C                 = 20
DIRECTION_NEAR_CERTAIN_C             = 90
DIRECTION_PRE_EXPIRY_S               = 90
DIRECTION_WALL_EXIT_ENABLED          = True
DIRECTION_WALL_RATE_CTPS             = 30
DIRECTION_WALL_WINDOW_S              = 3.0

# BB_PURE (Tier 2, gated off when UNIFIED on)
BB_PURE_MODE                         = True    # re-enabled 2026-05-07
BB_PURE_MIN_SESSION_ELAPSED_S        = 180.0   # 3-min warmup
BB_PURE_MAX_CONTRACTS                = 5

# PENNY_MODE (Tier 3, asymmetric ≤12c)
PENNY_MODE_ENABLED                   = True
PENNY_MAX_PRICE_C                    = 12      # was 8
PENNY_MIN_PRICE_C                    = 3
PENNY_MAX_CONTRACTS                  = 10
PENNY_MIN_CONTRACTS                  = 5
PENNY_MAX_RISK_DOLLARS               = 1.0
PENNY_DAILY_LOSS_HALT_FRAC           = 0.20    # 2026-05-07 PT NEW: parity
                                                # with DIRECTION

# Per-window ticker lock — INVIOLABLE
MAX_TRADES_PER_SESSION_TICKER        = 1
SAFETY_OVERSELL_HARDENING            = True

# DISABLED / RETIRED (do NOT re-enable without explicit user direction)
PAPER_FVG_LIVE_MODE                  = False  # 2026-05-05 catastrophe
TA_FORCED_ENTRY_ENABLED              = False
SR_FADE_ENABLED                      = False
SNIPER_ENABLED                       = False
SCALP_DCA_ENABLED                    = False
TP_LAYERED_ENABLED                   = False
WALLET_COPY_ENABLED                  = False
ATM_REVERSION_ENABLED                = False
ARB_DETECTOR_ENABLED                 = False
MICRO_PULLBACK_ENABLED               = False
BB_MOMENTUM_ENABLED                  = False
```

---

## CRITICAL OPERATIONAL GOTCHAS (read before debugging)

### 1. `_uc()` caches user_config at module import

The `_uc(name, default)` function reads from `_user_cfg`, built **once**
when the engine starts. **Editing `user_config.py` while the engine is
running has NO effect** until `nssm restart BTCBiasEngine`.

### 2. Multi-tier position state on `self._direction_position`

DIRECTION, UNIFIED, and PENNY fills **all** populate
`self._direction_position` (with the `tier` field distinguishing them).
BB_PURE fills go on `self._open_position`.

The exit layer (`_direction_manage_exit`) treats all three uniformly —
trailing stop, wall exit, loss cut, pre-expiry, hold-certain all apply
regardless of tier.

Legacy exit paths gate on `self._open_position is not None` and
auto-skip DIRECTION-class fills. `SYNC_RECLAIM` and `ORPHAN_FLATTEN`
consult `self._direction_active_tickers` (added via `_add_direction_active`).

### 3. Active exit management — NOT pure hold-to-settlement

**Pre-2026-05-07**: DIRECTION held to settlement, no exit orders.
**Post-2026-05-07**: DIRECTION_EXIT_ENABLED=True applies 5 rules every
tick. Most profitable trades close via Rule B (trailing stop) before
expiry. The "SELL TIER FILLED" log line means Rule B fired.

To revert to pure hold-to-settlement: `DIRECTION_EXIT_ENABLED=False` +
restart.

### 4. Manual trades are invisible to engine state

`MANUAL_FILLS_CAPTURE_ENABLED=False` — the manual-fills poller still
LOGS user trades from the Kalshi UI but does NOT add them to
`self._direction_position` or any tier's state. This means:
- User can manually trade alongside engine without conflict
- Engine BAL checks may briefly read low during user's settlement
  collateral periods (use confirm-with-2nd-fetch BAL protocol)

### 5. Race condition in IOC fills

By the time a 50ms-old book read becomes an actual order, the offer
side may have been swept. NOFILLs at slip=5c often mean "depth was
there 50ms ago, gone now" — not "no offer in price range." Solution:
`DIRECTION_MAX_SLIP_C=12` for adaptive walking captures more cases.

### 6. Maker-mode is wrong for momentum strategies

Tested 2026-05-06: maker bids only fill when market reverses =
adverse selection. All entry tiers use IOC (`post_only=False`,
`time_in_force=immediate_or_cancel`) at ask + slippage.

---

## Files that are load-bearing

### Core runtime (always)

- `run_copy_engine.py` — entry point
- `polymarket_copy_engine.py` — main engine. Tier handlers:
  `_unified_tick`, `_direction_tick`, `_evaluate_bb_pure_signal`,
  `_evaluate_penny_signal`. Exit layer: `_direction_manage_exit`.
  Safety helpers: `_check_window_safety`, `_record_window_fill`.
- `user_config.py` — live config switchboard
- `direction_strategy.py` — pure decision math for DIRECTION
- `unified_scorer.py` — pure 8-component composite scorer
- `kalshi_client.py` — RSA-PSS REST client
- `kalshi_ws.py` — WebSocket orderbook + trades
- `kalshi_tape.py` — per-ticker rolling tape
- `tape_pressure.py` — pressure scoring + btc_move_300s
- `price_feed.py` — Binance/Coinbase + Brownian-Bridge prob_engine
- `signal_logger.py` — async SQLite writer

### Validation + analytics

- `tests/test_direction_strategy.py` — 35 tests for direction module
- `scripts/backtest_direction.py` — n=197 corpus backtest (90.5% WR claim;
  see "Known divergence" below)
- `scripts/backtest_unified_scorer.py` — UNIFIED corpus backtest (71.5% WR)
- `RESEARCH_KALSHI_STRATEGY_ARCHETYPES.md` — strategy research doc

---

## Service management (NSSM on Windows)

```powershell
nssm status BTCBiasEngine
nssm restart BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
```

Required env vars (in `credentials/kalshi.env`):
- `KALSHI_API_KEY`
- `KALSHI_PRIVATE_KEY_PATH`
- `EXECUTE_TRADES=true`
- `KALSHI_DEMO=false`

Direct Kalshi state check:

```bash
cd /c/Trading/btc-bias-engine && python -c "
import asyncio, os, sys; sys.path.insert(0, '.')
from kalshi_client import KalshiClient
async def main():
    with open('credentials/kalshi.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k,v = line.strip().split('=',1)
                os.environ[k.strip()] = v.strip()
    pem = open(os.environ['KALSHI_PRIVATE_KEY_PATH']).read()
    async with KalshiClient(os.environ['KALSHI_API_KEY'], pem) as c:
        bal = await c.get_balance()
        print(f'BAL: \${bal.balance/100:.2f}')
        positions = await c.get_positions()
        flat = all(int(p.get('position',0)) == 0 for p in positions or [])
        print(f'Position: {\"FLAT\" if flat else \"NOT FLAT\"}')
asyncio.run(main())
"
```

---

## Key log patterns (`data/engine_history.log`)

| Pattern | Meaning |
|---|---|
| `UNIFIED FIRE: ...` / `UNIFIED FILL: ...` | Tier 0 entry |
| `UNIFIED DECLINE: side=X score=±N conf=N.NN ev=±Nc` | Tier 0 evaluated, didn't qualify |
| `DIRECTION FIRE: YES TICKER Nx @ Mc IOC (ask=Mc, slip=Sc)` | Tier 1 entry attempt |
| `DIRECTION FILL: YES Nx @ Mc oid=...` | Tier 1 entry filled |
| `DIRECTION IOC NOFILL: ... cooldown=30s + require ask Δ≥2c` | Tier 1 didn't fill |
| `DIRECTION DEPTH-SKIP: need=Nct, depth@+Mc=Nct` | Pre-IOC depth too shallow |
| `BB_PURE FIRE: ...` | Tier 2 entry (only when UNIFIED off) |
| `BB_PURE SIZE-CLAMP: kelly=Nct → cap=5ct` | Kelly sized over BB_PURE_MAX_CONTRACTS |
| `PENNY FIRE: ... max_risk=$N` | Tier 3 asymmetric entry attempt |
| `PENNY FILL: ... — holds to settle, no TP` | Tier 3 filled |
| `PENNY DAILY-LOSS-HALT: ...` | PENNY halted at -20% (NEW 2026-05-07) |
| `WINDOW-CAP RISK: tier=X cost=$N + committed=$N > cap` | $15 cap enforced |
| `WINDOW-CAP ENTRY: tier=X already entered N/M` | 1-per-window cap enforced |
| `WINDOW-CAP UPDATE: tier=X +Nct@Mc=$N` | Post-fill counter update |
| `DIRECTION HOLD-CERTAIN: bid=Nc ≥ 90c` | Exit Rule C active |
| `DIRECTION WALL-EXIT: ...` | Exit Rule A fired |
| `DIRECTION TRAIL-EXIT: hwm=Xc bid=Yc trail=Zc phase=N` | Exit Rule B fired |
| `DIRECTION LOSS-CUT: loss=Nc ≥ max=20c` | Exit Rule D fired |
| `DIRECTION PRE-EXPIRY: ...` | Exit Rule E fired |
| `SELL TIER FILLED: Nx @ Mc (+Nc)` | A DIRECTION-class exit completed (often Rule B) |
| `CopyEngine SYNC RECLAIM SKIP-DIRECTION: ...` | Refactor guard working — legacy path correctly skipped |

---

## Known divergence: backtest vs live

**Backtest claim** (`scripts/backtest_direction.py`, n=197): 90.5% WR,
+$3.93/trade.

**Live observation** (n=7 fills under fully-improved arch): ~28% WR,
-$0.79 avg.

**Hypothesis** (unverified): backtest assumes fills at historical mid;
live pays ask+slip. On a 30c entry, 5c slip = 16.7% extra cost
compounding into win-rate degradation. **Recommended next investigation**:
re-run backtest with realistic IOC fill simulation (assume only 30%
of signals fill, fills happen 5c above mid). If WR drops to ~30%,
backtest had survivorship bias. If WR stays at 90%, there's a live
execution bug.

**Don't trust the 90.5% claim** until re-validated with realistic fills.
The UNIFIED scorer's 71.5% backtest claim has the same caveat.

---

## Do / Don't

**Do:**
- Treat `user_config.py` as the live-behavior switchboard
- Check Kalshi positions API directly before trusting any engine-side P&L
- Use `MAX_TRADES_PER_SESSION_TICKER=1` as inviolable
- Use confirm-with-2nd-fetch BAL protocol before stopping on catastrophe
- Run `python -m pytest tests/test_direction_strategy.py -q` after any
  direction-strategy change

**Don't:**
- Re-enable retired strategies without explicit user direction
- Disable `MAX_TRADES_PER_SESSION_TICKER` or `_entered_tickers_this_window`
- Disable `SAFETY_OVERSELL_HARDENING`
- Disable `MAX_RISK_PER_WINDOW_DOLLARS` (universal $15 cap)
- Disable `MANUAL_FILLS_CAPTURE_ENABLED=False` (engine should NOT adopt
  user's manual trades)
- Trust the 90.5% backtest WR until realistic-fill backtest validates
- Set `DIRECTION_CONTRACTS > 1` without first confirming 15m offer-side
  depth has improved (still 1-2ct/tier as of 2026-05-07)

---

## Reading order for new AI agents

1. **This document (CLAUDE.md)** — operator-facing source of truth
2. **`AI_COLLAB_LOG.md`** — chronological architecture decisions
3. **`unified_scorer.py`** — Tier 0 pure decision math (~500 lines)
4. **`direction_strategy.py`** — Tier 1 pure decision math
5. **`tests/test_direction_strategy.py`** — 35 tests pinning DIRECTION decisions
6. **`scripts/backtest_unified_scorer.py`** — UNIFIED backtest
7. **`user_config.py`** — every live behavior knob
8. **`polymarket_copy_engine.py`** key sections:
   - `_check_window_safety`, `_record_window_fill` (~line 4499)
   - `_evaluate_penny_signal` (~line 4600)
   - `_unified_tick` (~line 4760)
   - `_direction_tick` (~line 5180)
   - `_direction_manage_exit` (~line 5800 — exit rules A-E)
9. **Recent commits in chronological order** — context for *why*

---

## Tests

```bash
python -m pytest tests/ -q --ignore=tests/test_bias_engine.py \
    --ignore=tests/test_consensus.py --ignore=tests/test_phase3.py \
    --ignore=tests/test_strategy_index.py
```

Expect ~588 passing. The 35-test `test_direction_strategy.py` suite
specifically validates the DIRECTION decision math (sides, thresholds,
sign-mismatch refusals, multiplier saturation, sizing).

---

## 2026-05-07 afternoon tuning (this update)

Six changes shipped to address the depth-wall miss-rate problem (376+
NOFILLs / 0 fills on a single signal under prior tuning):

1. `DIRECTION_CONTRACTS`: 2 → **1** (effective max 1ct after 1.5x cap)
2. `DIRECTION_MAX_SLIP_C`: 8 → **12** (race-condition resilience)
3. `UNIFIED_MIN_EV_C`: 5 → **3** (loosened from "strict" preset)
4. `UNIFIED_MIN_CONFIDENCE`: 0.30 → **0.25**
5. `PENNY_MAX_PRICE_C`: 8 → **12** (more asymmetric setups)
6. `PENNY_DAILY_LOSS_HALT_FRAC`: NEW **0.20** (parity with DIRECTION)

Engine code change: PENNY now reads `_direction_state.day_start_balance_c`
and refuses to fire if BAL has dropped 20% from day-start (mirroring
DIRECTION's halt). Throttled log every 60s.

**Restart required** for `_uc()` cache to pick up new values.
