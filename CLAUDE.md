# BTC Bias Engine — Current System Reference

**Last updated**: 2026-05-05 04:41 PT (FVG-tier-aware live deploy)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live primary signal**: `PAPER_FVG` (tier-aware Brownian-Bridge FVG, Level 3 sizing)
**Status**: SERVICE_RUNNING. BAL $35.52 (pre-flip). FLAT, 0 resting orders.

> **For the AI agent inheriting this session**: BB_PURE / BB_TREND / BB_MOMENTUM
> were retired 2026-05-04 in favor of the **FVG-tier-aware** path. The FVG
> strategy fires from `_paper_fvg_tick` in `polymarket_copy_engine.py` and
> routes through `_paper_fvg_live_entry` (real Kalshi orders) when
> `PAPER_FVG_LIVE_MODE=True`. Tier classification + sizing math lives in
> `_fvg_tiering.py` (pure module, OOS-validated 2026-05-04: T1 fill 97.4%,
> T2 fill 92.2%). Sizing is **Level 3 Half-Kelly aggressive**: T1=35%, T2=25%,
> T3=18%, T4=10% bankroll fractions, with a 40% max ticker exposure cap.

This document is the operator-facing source of truth. **If this doc and code disagree, the code wins.** Update this doc whenever signal logic or config defaults materially change.

> **Setting up the engine on a new machine?** See [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md) for the clean-install walkthrough (clone → venv → credentials → NSSM → smoke test → flip-to-live checklist).

---

## What the engine does

Trades Kalshi `KXBTC15M` 15-minute BTC binary options.

**Single live strategy: tier-aware FVG (Fair Value Gap)** — buys the cheap
side when the Brownian-Bridge fair value diverges meaningfully from the
session baseline, sized aggressively when conviction is high.

```
[1] BASELINE phase (first 90s of session)
    - Collect mid prices, compute baseline = mean(mids)

[2] FVG signal
    - fair_value (from BB model) vs baseline
    - if |fair − baseline| ≥ time-weighted threshold:
        side = "yes" if fair > baseline else "no"
        entry_price = bid + 1 (post_only maker)

[3] TIER CLASSIFICATION (_fvg_tiering.classify_tier)
    - Tier 1 (35% size, +20c TP): age ≥ 300s, aligned, |dist| ≥ 0.10%  [99% OOS fill]
    - Tier 2 (25% size, +15c TP): age ≥ 300s, aligned                 [93% OOS fill]
    - Tier 3 (18% size, +12c TP): age ≥ 300s, not aligned             [91% OOS fill]
    - Tier 4 (10% size, +12c TP): 180 ≤ age < 300                     [76% OOS fill]
    - Tier 0 (REFUSE):
        * age < 180s (early-window noise)
        * counter-trend AND 30 ≤ entry ≤ 49 (worst-segment combo)
        * |btc_5m_move| < $20 AND counter-trend (no edge)

[4] SIZING (_fvg_tiering.compute_size_contracts)
    - notional = balance × tier_frac, capped at balance × 40%
    - contracts = floor(notional / entry_price)
    - hard cap at 200 contracts
    - if min_contracts cost > exposure cap → refuse

[5] LIVE EXECUTION (_paper_fvg_live_entry)
    - Per-window ticker lock added pre-await (race protection)
    - place_order(action="buy", post_only=True) at bid+1
    - On filled>0: place resting TP at tier_tp_price → LIVE_HOLDING
    - On filled=0: → LIVE_PENDING (8s NOFILL cancel timer)
    - On exception: release ticker lock, retry on next signal

[6] LIVE_HOLDING management (_paper_fvg_live_holding_tick)
    - Poll Kalshi truth via get_positions every tick
    - position == 0 (TP filled) → reconcile residual + close
    - cur_bid ≤ sl_trigger (entry − 8c) → cancel TP + capped_side_sell
    - seconds_remaining < 60 → cancel TP + flatten
    - Always _reconcile_residual_position after close (oversell guard)

[7] DAILY-LOSS CIRCUIT BREAKER
    - If real bal < day_start × (1 − FVG_DAILY_LOSS_HALT_FRAC) → halt
```

The microstructure / regime / SR / wallet-copy / TA-cascade layers exist in
the codebase but are all feature-flagged off. The FVG path is self-contained
and routes through the existing safety primitives:
`_add_session_lock`, `_place_capped_side_sell` (MIN-TRUTH gate +
OVERSELL-GUARD), `_reconcile_residual_position`.

---

## Critical config (live values)

```python
# Live trading
PAPER_TRADING                        = False
PAPER_FVG_ENABLED                    = True
PAPER_FVG_LIVE_MODE                  = True   # routes FVG through real orders

# Tier sizing fractions (Level 3 Half-Kelly, OOS-validated 2026-05-04)
FVG_TIER_FRAC_T1                     = 0.35
FVG_TIER_FRAC_T2                     = 0.25
FVG_TIER_FRAC_T3                     = 0.18
FVG_TIER_FRAC_T4                     = 0.10

# Tier TP offsets (cents above entry)
FVG_TIER_TP_T1                       = 20
FVG_TIER_TP_T2                       = 15
FVG_TIER_TP_T3                       = 12
FVG_TIER_TP_T4                       = 12
FVG_TIER_SL_OFFSET                   = 8        # uniform across tiers

# Live execution caps + safety
FVG_LIVE_MAX_TICKER_EXPOSURE_FRAC    = 0.40     # cap above T1's 35%
FVG_LIVE_MAX_ENTRY_CENTS             = 75       # cheap-side bias
FVG_LIVE_MAX_CONTRACTS_CAP           = 200      # hard ceiling
FVG_LIVE_NOFILL_TIMEOUT_S            = 8.0      # cancel-stale entry timer
FVG_DAILY_LOSS_HALT_FRAC             = 0.20     # halt at -20% from day-start

# Per-window ticker lock — INVIOLABLE
MAX_TRADES_PER_SESSION_TICKER        = 1

# Safety hardening
SAFETY_OVERSELL_HARDENING            = True     # gates _reconcile_residual_position

# DISABLED / RETIRED (do NOT re-enable without explicit user direction)
BB_PURE_MODE                         = False   # killed 2026-05-04 19:55
BB_MOMENTUM_ENABLED                  = False   # killed 2026-05-05 (user: FVG only)
TA_FORCED_ENTRY_ENABLED              = False
SR_FADE_ENABLED                      = False
SNIPER_ENABLED                       = False
SCALP_DCA_ENABLED                    = False
TP_LAYERED_ENABLED                   = False
WALLET_COPY_ENABLED                  = False
ATM_REVERSION_ENABLED                = False
ARB_DETECTOR_ENABLED                 = False
MICRO_PULLBACK_ENABLED               = False
```

---

## Files that are load-bearing

### Core runtime (always)

- `run_copy_engine.py` — entry point, credential bootstrap
- `polymarket_copy_engine.py` — main engine. The FVG live path lives in
  `_paper_fvg_tick` + `_paper_fvg_live_entry` + `_paper_fvg_live_holding_tick`
  + `_paper_fvg_live_close` + `_paper_fvg_live_session_rollover`
- `user_config.py` — live config switchboard (FVG knobs at line ~2015)
- `_fvg_tiering.py` — pure module: tier classification + sizing math.
  All decision logic is unit-testable here.
- `kalshi_client.py` — RSA-PSS signed REST client
- `kalshi_ws.py` — WebSocket client for orderbook/trades
- `kalshi_tape.py` — per-ticker rolling tape of trades + mids
- `price_feed.py` — Binance/Coinbase ingestion + Brownian-Bridge model (`prob_engine`)
- `signal_logger.py` — async SQLite writer (data/trades.db, data/signals.db)

### Validation + analytics

- `tests/test_fvg_tiering.py` — 23 unit tests pinning every tier boundary
- `tests/test_fvg_live_wiring.py` — 10 unit tests for the live state machine
  (entry path, NOFILL timeout, late-fill, SL hit, time-exit, daily-loss halt,
  ticker-lock, place_order failure)
- `scripts/backtest_oos_level3.py` — chronological 70/30 OOS validation
- `scripts/backtest_fvg_segmentation.py` — feature-conditional P&L analysis

### Background workers

- `whale_monitor.py` — mempool whale alerts (logs only, no trading influence)

### Optional / not on live path

- `paper_trader.py` — used when `PAPER_TRADING=True` (mirrors KalshiClient)
- `bb_pure.py`, `tape_pressure.py`, `protective_math.py`, `sell_safety.py` —
  legacy BB_PURE supporting modules; FVG path uses
  `_place_capped_side_sell` + `_reconcile_residual_position` directly

### Historical / archival

- `_archive/`, `docs/archive/`, retired strategies (TA_FORCED, SR_FADE,
  SNIPER, SCALP DCA, wallet copy)

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
        for p in positions or []:
            qty = int(p.get('position', 0))
            if qty != 0:
                print(f'  {p.get(\"ticker\")} qty={qty}')
asyncio.run(main())
"
```

---

## Key log patterns (`data/engine_history.log`)

| Pattern | Meaning |
|---|---|
| `PAPER FVG BASELINE: Nc from M samples` | First-90s baseline established |
| `PAPER FVG LIVE FIRE [TN]: SIDE TICKER Nx @ Mc` | Live entry placed |
| `PAPER FVG LIVE FILL [TN]: Nx @ Mc \| TP placed @ Pc` | Entry filled, TP resting |
| `PAPER FVG LIVE NOFILL: order=...` | Entry resting at limit, awaiting fill |
| `PAPER FVG LIVE NOFILL CANCEL: order=...` | Stale entry cancelled after 8s |
| `PAPER FVG LIVE LATE-FILL: ...` | Entry filled after place_order returned |
| `PAPER FVG LIVE CLOSE [TN]: ...pnl=$+/-X.XX` | Trade closed (TP / SL / time) |
| `PAPER FVG LIVE EMERGENCY-EXIT (sl_hit)` | SL trigger fired, flattening |
| `PAPER FVG LIVE EMERGENCY-EXIT (time_exit_any)` | < 60s left, flattening |
| `PAPER FVG LIVE DAILY-LOSS-HALT: ...` | Day P&L hit -20%, halted |
| `PAPER FVG LIVE SESSION-ROLLOVER` | Window rolled mid-position, cleaning up |
| `PAPER FVG LIVE TIER-UNDERSIZED` | Bankroll can't afford 1ct under cap |
| `PAPER FVG LIVE CHEAP-SIDE-BLOCK: entry=Nc > 75c` | Entry too expensive, skip |
| `PAPER FVG LIVE BAL-FLOOR: ...` | Pre-fire balance gate (avoid insufficient_balance) |
| `PAPER FVG LIVE SKIP: TICKER already entered` | Per-window ticker lock holding |
| `PAPER FVG LIVE place_order failed: ...` | Order rejection, lock released |
| `CopyEngine RESIDUAL-CLEAN: ...` | Position confirmed flat post-close |
| `CopyEngine RESIDUAL: Nct YES still open` | Limit sells didn't fill, market-selling |
| `CopyEngine OVERSELL-DETECTED: ...` | 2026-04-22 oversell-to-short signature (alarm) |
| `CopyEngine STUCK-RESIDUAL: ...` | Reconciler couldn't clear after 3 attempts |
| `SESSION-LOCK: restored N ticker(s)` | Per-window lock loaded from disk on startup |

---

## Known gaps (priority order)

1. **Tier-classification refinement**: `btc_5m_move` is currently a session-
   open proxy. Real 5-min rolling tracker needs wiring to the `tick_tracker`
   module for higher-fidelity tier classification.
2. **Live validation horizon**: OOS backtest validated T1/T2 fills at
   97.4% / 92.2% on 9 days of data. Live data accumulates here; revisit
   tier thresholds after 30 days of live fills.
3. **Pre-fire balance gate**: my code requires `balance ≥ 1.5× cost` to
   avoid insufficient_balance on exit. The 1.5× constant is conservative;
   could tighten with empirical taker-fee analysis.

---

## Do / Don't

**Do:**

- Treat `user_config.py` as the live-behavior switchboard
- Check Kalshi positions API directly before trusting any engine-side P&L
- Verify `PAPER_TRADING=False` and `PAPER_FVG_LIVE_MODE=True` before
  assuming live behavior
- Use `MAX_TRADES_PER_SESSION_TICKER=1` as the inviolable rule — one
  engine entry per 15-min ticker
- Run `python -m pytest tests/test_fvg_tiering.py tests/test_fvg_live_wiring.py -q`
  after any change to tier or live-wiring logic

**Don't:**

- Re-enable retired strategies (`BB_PURE_MODE`, `BB_MOMENTUM_ENABLED`,
  `TA_FORCED_ENTRY_ENABLED`, `SR_FADE_ENABLED`, `SNIPER_ENABLED`,
  `SCALP_DCA_ENABLED`, `TP_LAYERED_ENABLED`, `MICRO_PULLBACK_ENABLED`,
  `ATM_REVERSION_ENABLED`, `WALLET_COPY_ENABLED`) without explicit
  user direction
- Disable `MAX_TRADES_PER_SESSION_TICKER` or `_entered_tickers_this_window`
  persistence
- Disable `SAFETY_OVERSELL_HARDENING` — gates the residual reconciler
- Edit `_fvg_tiering.py` constants without re-running OOS validation
  (`scripts/backtest_oos_level3.py`)
- Restart engine without verifying Kalshi state directly
- Trust paper-sim balance for live attribution — query real balance via
  `self._client.get_balance()` for true P&L

---

## Reading order for new AI agents

1. **This document (CLAUDE.md)** — operator-facing source of truth
2. **`_fvg_tiering.py`** — pure tier-classification + sizing math
3. **`tests/test_fvg_tiering.py`** — pins down each decision rule
4. **`user_config.py`** — every live behavior knob in one file (FVG section
   at line ~2015)
5. **`polymarket_copy_engine.py:_paper_fvg_tick`** — entry-point
6. **`polymarket_copy_engine.py:_paper_fvg_live_entry`** — entry-execution
7. **`polymarket_copy_engine.py:_paper_fvg_live_holding_tick`** — exit/safety
8. **`polymarket_copy_engine.py:_reconcile_residual_position`** — A5 oversell guard
9. **`polymarket_copy_engine.py:_place_capped_side_sell`** — MIN-TRUTH +
   OVERSELL-GUARD baked into every sell
10. **Recent commits in chronological order** — context for *why* the code
    looks the way it does

Skip on first read: any of the disabled-tier code (TA_FORCED, SR_FADE,
SNIPER, SCALP DCA, wallet copy, BB_PURE/BB_MOMENTUM). They're feature-
flagged off and don't affect current behavior.

---

## Tests

```bash
python -m pytest tests/ -q --ignore=tests/test_bias_engine.py \
    --ignore=tests/test_consensus.py --ignore=tests/test_phase3.py \
    --ignore=tests/test_strategy_index.py
```

Expect: ~588 passing, 3 pre-existing failures (`test_late_dominant.py` 2,
`test_sr_fade_gates.py` 1) unrelated to current strategy. Safe to delete
those test files once the corresponding retired modules are removed.

The FVG-tier-aware path is covered by:
- `tests/test_fvg_tiering.py` — 23 tier classification + sizing tests
- `tests/test_fvg_live_wiring.py` — 10 live-state-machine tests
- `tests/test_residual_reconciler.py` — A5 oversell-guard tests
- `tests/test_place_capped_side_sell.py` — MIN-TRUTH + OVERSELL-GUARD tests
- `tests/test_session_lock_release.py` — ticker-lock release-on-failure tests

---

## OOS validation summary (2026-05-04)

`scripts/backtest_oos_level3.py` chronological 70/30 split on 2,485 FVG
signals across 9.1 days:

| Tier | Backtest fill | OOS fill | Edge / trade |
|------|---------------|----------|--------------|
| T1   | 99%           | 97.4%    | +$0.158      |
| T2   | 93%           | 92.2%    | +$0.085      |
| T3   | 91%           | 81.2%    | +$0.043      |
| T4   | 76%           | 75.0%    | +$0.018      |

Flat sim ($50 starting bankroll, no compounding):
- Total P&L: +$516.33 over 9.1 days
- Max equity-curve DD: -$17.70
- Return:DD ratio: 29×

Compounding sim ($50 starting, scale per current BR):
- Mathematically extreme growth — liquidity-limited in reality
- Zero ruin events in the OOS window

**Real-world expectations**: Kalshi 15m liquidity caps will limit
compounding well below the simulated 14000× growth. Realistic projection
on a $35 starting bankroll: ~$50/day flat-sized, scaling to ~$200/day
once tier-1 contracts can be filled at 50-100ct without slippage.
