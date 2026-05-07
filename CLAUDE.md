# BTC Bias Engine — Current System Reference

**Last updated**: 2026-05-06 (DIRECTION refactor + improvements)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live primary signal**: `DIRECTION` (sign-aligned distance + momentum, hold to settlement)
**Status**: SERVICE_RUNNING. BAL $78.73. FLAT, 0 resting orders.

> **For the AI agent inheriting this session**: as of 2026-05-06 PT, the
> live primary signal is **direction-following with conviction sizing**:
> when BTC is ≥0.10% past strike with 5-min momentum agreeing, buy that
> side at ask + slippage (IOC taker) and hold to settlement.
> - Pure module: `direction_strategy.py` (decision + conviction multiplier)
> - Engine handler: `_direction_tick` in `polymarket_copy_engine.py`
> - **Position state: `self._direction_position` (NOT `self._open_position`)** —
>   this is the architectural fix. Legacy exit paths (VWAP_EXIT,
>   PROB_COLLAPSE, MRC_FORCE_EXIT, DOMINANT_UPGRADE, etc.) all gate on
>   `_open_position is not None` so they auto-skip. Prevents the 4
>   hijack incidents that plagued the first deploy attempts.
>
> **Backtest evidence** (scripts/backtest_strategy_comparison.py, n=197):
> DIRECTION 0.10%/$10 confirmed highest-alpha strategy:
> 90.5% win rate / +$3.93 mean per trade / +$331 corpus / 1.1% top-win-skew
> (extremely robust). Other variants (cheap-underdog, mean-reversion,
> BB-model-edge, momentum-only) ranked lower by alpha density and total $.
>
> **Sizing:** `DIRECTION_CONTRACTS=3` base × `conviction_multiplier`
> (0.7-2.0×) → 3-6ct per fire. Smaller than original `5` because
> Kalshi 15m ask depth at typical entry prices (14c-70c) is consistently
> below 5-10ct. Multiplier saturates at 2.0× on dist≥0.20% AND mom≥$40.
>
> **Execution:** IOC at `ask + DIRECTION_TAKER_SLIPPAGE_C` (default 3c).
> Walks through 1-2 depth tiers when ask is thin. Hold-to-settlement:
> Kalshi auto-credits $1 per winning contract at expiry; engine places
> NO exit orders.
>
> **Today's lessons (2026-05-05 to 2026-05-06):**
> 1. FVG-tier (`PAPER_FVG_LIVE_MODE`) had FLAT-CONFIRMED bug + stale-cache
>    premature-close → -19% loss → permanently disabled, do not re-enable
>    without re-validation
> 2. DIRECTION via `_open_position` was hijacked 4 times by legacy exit
>    paths (SYNC_RECLAIM, VWAP_EXIT, DOMINANT_UPGRADE, etc.) — fixed by
>    refactoring to `self._direction_position` (commit `cc07690`)
> 3. **`_uc()` caches config at module import** — config changes require
>    engine restart. Don't trust "no restart needed" claims unless you've
>    verified `_uc` re-reads the user_config module.
> 4. Maker-mode (`post_only=True` at ask-1) is wrong for momentum
>    strategies — only fills on reversal = adverse selection. IOC with
>    slippage tolerance is the right execution model.

This document is the operator-facing source of truth. **If this doc and code disagree, the code wins.** Update this doc whenever signal logic or config defaults materially change.

> **Setting up the engine on a new machine?** See [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md) for the clean-install walkthrough (clone → venv → credentials → NSSM → smoke test → flip-to-live checklist).

---

## What the engine does

Trades Kalshi `KXBTC15M` 15-minute BTC binary options.

**Single live strategy: direction-following.** Buy whichever side BTC is
moving when it's meaningfully past strike with momentum agreeing. Hold
to settlement. The exit IS the settlement — Kalshi auto-credits $1.00
per contract to balance if the direction was right, $0 otherwise.

```
[1] Inputs (every tick)
    - btc_price        from price_feed (_btc_last_price)
    - strike           from prob_engine.strike (parsed from ticker)
    - btc_5m_move      from tape_pressure.btc_move_300s
    - book.best_yes_ask / best_no_ask  from kalshi_ws

[2] Decision (direction_strategy.evaluate)
    - dist_pct = (btc - strike) / strike
    - YES: dist_pct >= +0.10% AND btc_5m_move >= +$10
    - NO:  dist_pct <= -0.10% AND btc_5m_move <= -$10
    - else: skip

[3] Pre-fire gates
    - Per-window ticker lock: skip if already entered this 15-min window
    - Entry-time cap: skip if session_age >= 600s (minute 10)
    - Daily-loss halt: skip if bal < day_start × (1 - 0.20)
    - Bankroll: require bal >= 1.5× cost (avoid insufficient_balance on retry)
    - Post-failure cooldown: 5s per-ticker after any place_order rejection

[4] EXECUTION
    - Pre-await: add ticker lock, stamp recent_placement_tickers
    - place_order(action="buy", side=signal.side, price=ask,
                  count=DIRECTION_CONTRACTS, post_only=False,
                  time_in_force="immediate_or_cancel")
    - IOC ensures: fill at ask if depth available, else auto-cancel.
      No stale resting orders, no maker bag-holds.
    - On exception: release ticker lock + cooldown timer (lock-release)
    - On filled=0: IOC cancelled. Release lock + cooldown for retry.
    - On filled>0: log + persist row in direction_trades. NO post-entry
      tracking — position settles at expiry, Kalshi handles it.
```

The microstructure / regime / SR / wallet-copy / TA-cascade / FVG-tier
layers exist in the codebase but are all feature-flagged off. The
direction path is self-contained: only uses existing engine primitives
for ticker lock + balance check + book read + place_order.

**Why no exit logic:** Kalshi binary contracts settle at $0.00 or $1.00
at expiry. Our position is bought via IOC at the ask, then held. At
settlement, balance is auto-credited by Kalshi. No FLAT-CONFIRMED, no
SYNC RECLAIM, no residual reconciler needed because no exit orders are
placed. The only safety primitive needed is the per-window ticker lock.

---

## Critical config (live values)

```python
# Live trading
PAPER_TRADING                        = False

# DIRECTION-FOLLOWING (PRIMARY LIVE SIGNAL — 2026-05-06 final)
DIRECTION_STRATEGY_ENABLED           = True    # PRIMARY
DIRECTION_DIST_THRESHOLD_PCT         = 0.0010  # 0.10% from strike
DIRECTION_MOMENTUM_THRESHOLD_DOLLARS = 10      # $10 over 5min
DIRECTION_MAX_OFFSET_S               = 600     # entry only before minute 10
DIRECTION_CONTRACTS                  = 3       # base size (was 5; reduced
                                               # 2026-05-06 to match shallow
                                               # Kalshi 15m ask depth)
DIRECTION_CONVICTION_SIZING_ENABLED  = True    # apply 0.7-2.0× multiplier
DIRECTION_MIN_BANKROLL_X_COST        = 1.5     # need 1.5×cost in BAL
DIRECTION_DAILY_LOSS_HALT_FRAC       = 0.20    # halt at -20% from day-start
DIRECTION_POST_FAIL_COOLDOWN_S       = 5.0     # backoff after place_order fail
DIRECTION_TAKER_SLIPPAGE_C           = 3       # IOC at ask + 3c (walk depth)
                                               # NOTE: changing this REQUIRES
                                               # engine restart (_uc caches
                                               # config at module import).

# Per-window ticker lock — INVIOLABLE
MAX_TRADES_PER_SESSION_TICKER        = 1

# Safety hardening
SAFETY_OVERSELL_HARDENING            = True     # belt-and-braces (no exits placed
                                                # by direction strategy, but the
                                                # primitive defends if any ever
                                                # are added)

# DISABLED / RETIRED (do NOT re-enable without explicit user direction)
PAPER_FVG_LIVE_MODE                  = False   # 2026-05-05 06:27 PT EMERGENCY
                                               # DISABLE: stale-cache premature-
                                               # close + SYNC RECLAIM bug. See
                                               # to-do/LIVE_SESSION_NOTES_2026_05_02.md
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

## CRITICAL OPERATIONAL GOTCHAS (read before debugging)

### 1. `_uc()` caches user_config at module import

The `_uc(name, default)` function (`polymarket_copy_engine.py:105`) reads
from a `_user_cfg` dict that is built **once** when the engine starts:

```python
_user_cfg = {k: v for k, v in vars(_uc).items() if not k.startswith("_")}
```

**Implication:** editing `user_config.py` while the engine is running has
**NO effect** until the engine is restarted. Don't trust any "no restart
needed" claim unless the consuming code path explicitly re-reads the
file.

To apply a config change:
```powershell
# 1. Edit user_config.py
# 2. Save
# 3. Restart:
nssm restart BTCBiasEngine
# 4. Verify the new value is in the next FIRE log line
```

### 2. DIRECTION uses `self._direction_position`, NOT `self._open_position`

Every BB_PURE/TA_FORCED-era exit path in the engine (~20 of them inside
`_manage_position`, plus `VWAP_EXIT`, `MRC_FORCE_EXIT`,
`DOMINANT_UPGRADE`, etc.) gates on `self._open_position is not None`. If
DIRECTION fills populated `_open_position`, those paths see it and apply
the wrong exit logic — we hit this 4 times in 2 days (commits before
`cc07690`).

Fix: DIRECTION fills set `self._direction_position` (a separate
attribute). `_open_position` stays `None` for DIRECTION trades. Legacy
exit paths skip them by construction.

The only paths that scan Kalshi truth independent of `_open_position`
are `SYNC_RECLAIM` and `ORPHAN_FLATTEN`. Both consult
`self._direction_active_tickers` and skip DIRECTION-tier tickers.

**If a DIRECTION fill appears in `_open_position`, something is broken
upstream.** Don't add a guard — fix the upstream code path.

### 3. Maker-mode (`post_only=True` at ask-1) is a fail for momentum strategies

Tested 2026-05-06 18:00-18:25 PT. Maker bids only fill when the market
reverses toward our price = adverse selection. Don't try maker mode on
DIRECTION; use IOC at `ask + slippage`.

---

## Files that are load-bearing

### Core runtime (always)

- `run_copy_engine.py` — entry point, credential bootstrap
- `polymarket_copy_engine.py` — main engine. The DIRECTION live path is in
  `_direction_tick`. The FVG-tier path (now off) is in `_paper_fvg_tick`
  + `_paper_fvg_live_entry` etc.
- `user_config.py` — live config switchboard (DIRECTION knobs at line ~2060,
  FVG knobs at ~2015 but disabled)
- `direction_strategy.py` — pure module: direction-following decision math.
  All logic is unit-testable here.
- `_fvg_tiering.py` — pure module: legacy FVG tier classification (still
  imported by `_paper_fvg_tick` paper-sim path).
- `kalshi_client.py` — RSA-PSS signed REST client
- `kalshi_ws.py` — WebSocket client for orderbook/trades
- `kalshi_tape.py` — per-ticker rolling tape of trades + mids
- `tape_pressure.py` — pressure scoring; provides `btc_move_300s` for the
  direction strategy
- `price_feed.py` — Binance/Coinbase ingestion + Brownian-Bridge model (`prob_engine`).
  Provides `prob_engine.strike` for the direction strategy.
- `signal_logger.py` — async SQLite writer (data/trades.db, data/signals.db)

### Validation + analytics

- `tests/test_direction_strategy.py` — 25 unit tests pinning every decision
  boundary (YES/NO sides, dist + momentum thresholds, sign-mismatch refusals,
  invalid inputs, sizing, daily-loss halt)
- `scripts/backtest_direction.py` — corpus backtest on 197 settled markets,
  proves the 69-90% win rate and +$2-4/trade economics
- `scripts/backtest_cheap_trail.py` + `backtest_cheap_trail_deep.py` —
  alternative-strategy backtests (cheap-side + trailing TP) showing this
  approach is inferior to direction-following
- `tests/test_fvg_tiering.py` (23 tests) + `tests/test_fvg_live_wiring.py`
  (10 tests) — preserved for the now-disabled FVG path

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
| `DIRECTION FIRE: YES TICKER Nx @ Mc` | Direction entry placed (IOC) |
| `DIRECTION FILL: YES Nx @ Mc oid=...` | Entry filled (settles at expiry) |
| `DIRECTION IOC NOFILL: order=...` | IOC didn't take immediately, auto-cancelled |
| `DIRECTION DAILY-LOSS-HALT: bal=$X day_start=$Y` | Day P&L hit -20%, halted |
| `DIRECTION SIZING-REFUSE: bal=$X ask=Nc` | Bankroll < 1.5×cost, skip |
| `DIRECTION place_order failed: ...` | Order rejection, lock released, 5s cooldown |
| `DIRECTION: new session day YYYY-MM-DD` | First trade of the day |
| `CopyEngine RESIDUAL-CLEAN: ...` | Defensive primitive (direction places no exit orders) |
| `CopyEngine OVERSELL-DETECTED: ...` | 2026-04-22 catastrophe signature (should NEVER fire under DIRECTION) |
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
2. **`direction_strategy.py`** — pure decision math (~150 lines, the entire
   trading thesis)
3. **`tests/test_direction_strategy.py`** — 25 unit tests pinning every
   decision boundary
4. **`scripts/backtest_direction.py`** — settlement-driven backtest
   showing the 69-90% win rate / +$2-4/trade economics
5. **`user_config.py`** — every live behavior knob (DIRECTION section
   at ~2060)
6. **`polymarket_copy_engine.py:_direction_tick`** — engine integration
   (~150 lines: gates → evaluate → place_order → log)
7. **`polymarket_copy_engine.py:_add_session_lock`** — per-window
   ticker lock primitive
8. **Recent commits in chronological order** — context for *why* the code
   looks the way it does

Skip on first read: any of the disabled-tier code (TA_FORCED, SR_FADE,
SNIPER, SCALP DCA, wallet copy, BB_PURE/BB_MOMENTUM, FVG-tier-aware
LIVE_HOLDING). They're feature-flagged off and don't affect current
behavior. The FVG-tier path produced an early-AM 2026-05-05 catastrophe
loss (-19%) before being disabled — see
`to-do/LIVE_SESSION_NOTES_2026_05_02.md` 06:27 PT entry for the
postmortem.

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
