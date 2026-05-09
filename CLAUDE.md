# BTC Bias Engine — Current System Reference

**Last updated**: 2026-05-09 PT (post MOMENTUM_SCALP overhaul + 5 paralysis fixes)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live tier**: `MOMENTUM_SCALP` (single-tier — UNIFIED / DIRECTION / BB_PURE /
PENNY all retired/gated off in `user_config.py`).
**Status**: Live trading. Velocity-aware exit + multi-flip inverse re-entry.
Five paralysis-mode fixes shipped 2026-05-09 (commits `96c6bb2`, `53aaf55`).

> **For the AI agent inheriting this session**: the architecture cascade
> documented in older versions of this file (UNIFIED → DIRECTION →
> BB_PURE → PENNY) is **not what runs today**. Every one of those tiers
> is gated off in `user_config.py`. The only live entry path is
> MOMENTUM_SCALP. **If this doc and the code disagree, the code wins.**
> Verify with `grep -E "_ENABLED\s*=\s*True" user_config.py` and check
> the latest fills via direct Kalshi API (see `docs/NEW_INSTANCE_SETUP.md`
> Section 12).

This document is the operator-facing source of truth. Update it whenever
signal logic or config defaults materially change.

> **Setting up the engine on a new machine?** See [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md)

---

## What the engine does

Trades Kalshi `KXBTC15M` 15-minute BTC binary options through a single
entry tier (MOMENTUM_SCALP) with velocity-aware exit and multi-flip
inverse re-entry. Universal $15/window risk cap bounds total exposure.

### Cycle (per window)

```
[ new Kalshi 15m window ]
        │
        ▼
SCALP MARKET path  ← fires once per window in the first 30s
   • picks the favorite side (yes_ask vs no_ask, whichever is higher)
   • IOC at ask + SCALP_REENTRY_SLIP_C, capped at 95c
   • SCALP_CONTRACTS=5 per fill
   • populates _direction_position with tier="SCALP"
        │
        ▼
Per-tick exit evaluator (_scalp_manage / _scalp_fire_exit)
   1. SCALP NEAR-CERTAIN  — bid ≥ 90c → hold for $1 settlement
   2. SCALP PRE-EXPIRY    — profitable + ≤60s left → sell
   3. SCALP BTC-TRAIL     — BTC retraces by dynamic strike-distance
                            scaled threshold → sell + flip eval
   4. SCALP TRAIL         — bid-cents trail (phased 15/10/5/3) → sell
   5. SCALP LOSS-CUT      — bid ≥ 15c below entry → sell
        │
        ▼
On TRAIL or BTC-TRAIL exit only:
   SCALP REVERSAL-SCORE (4 components: time, btc, book, velocity)
   Total > 0.5 → INVERT
        │
        ▼
SCALP INVERSE re-entry  ← IOC opposite side at ask + slip
   On fill: re-populate SCALP state with new side, BTC hwm reset,
   trail re-armed. Next BTC-TRAIL fire can trigger another flip.
        │
        └─ loops until $15/window risk cap or PRE-EXPIRY hits
```

### Dynamic BTC trail (2026-05-09)

The BTC-TRAIL threshold scales with strike distance because a 15-minute
binary's price sensitivity to BTC peaks at the strike (highest delta)
and decays toward both tails. A flat trail under-reacts ATM and
over-reacts deep ITM.

```
trail_dollars = clamp(MIN, MAX, K × abs(btc_now - strike))
```

Defaults: K=0.4, MIN=$25, MAX=$100. ATM gets a tight $25 trail (catches
reversals fast); $200 from strike gets $80; $300+ caps at $100.

Falls back to `SCALP_BTC_TRAIL_DOLLARS = 60.0` if strike is unknown
(prob_engine not warmed up) or the dynamic flag is False.

---

## Critical config (live values, 2026-05-09)

```python
# Live trading
PAPER_TRADING                          = False
MANUAL_FILLS_CAPTURE_ENABLED           = False  # engine ignores manual trades

# Master toggles
MOMENTUM_SCALP_ENABLED                 = True
SAFETY_OVERSELL_HARDENING              = True
ADMISSION_FILTER_ENABLED               = True

# All other entry tiers — RETIRED / OFF
UNIFIED_SCORER_ENABLED                 = False
DIRECTION_STRATEGY_ENABLED             = False
BB_PURE_MODE                           = False
BB_MOMENTUM_ENABLED                    = False
PENNY_MODE_ENABLED                     = False
TA_FORCED_ENTRY_ENABLED                = False
LATE_DOMINANT_ENABLED                  = False
SR_FADE_ENABLED                        = False
PAPER_FVG_LIVE_MODE                    = False
SNIPER_ENABLED                         = False
WALLET_COPY_ENABLED                    = False
ATM_REVERSION_ENABLED                  = False
ARB_DETECTOR_ENABLED                   = False
MICRO_PULLBACK_ENABLED                 = False
TP_LAYERED_ENABLED                     = False
SCALP_DCA_ENABLED                      = False

# SCALP entry knobs
SCALP_CONTRACTS                        = 5
SCALP_REENTRY_SLIP_C                   = 5
SCALP_MAX_RISK_DOLLARS                 = 5.0
SCALP_PLACE_MAX_AGE_S                  = 30.0   # only fires in first 30s of window
SCALP_IOC_COOLDOWN_S                   = 5.0    # min seconds between IOC retries
SCALP_MAX_CONTRACTS_WINDOW             = 10
SCALP_PRE_IOC_TICKER_LOCK_CHECK        = True   # 2026-05-08 retry-on-same-ticker fix
SCALP_INVERSE_REENTRY_ENABLED          = True
SCALP_REVERSAL_THRESHOLD               = 0.50

# SCALP cents-trail (phase-based on session age)
SCALP_TRAIL_C                          = 5
SCALP_TRAIL_PHASE1_C                   = 15    # 0-5m
SCALP_TRAIL_PHASE2_C                   = 10    # 5-10m
SCALP_TRAIL_PHASE3_C                   = 5     # 10-13m
SCALP_TRAIL_PHASE4_C                   = 3     # 13m+
SCALP_MAX_LOSS_C                       = 15
SCALP_NEAR_CERTAIN_C                   = 90
SCALP_PRE_EXPIRY_S                     = 60

# SCALP BTC-trail (dynamic strike-distance scaling, 2026-05-09)
SCALP_BTC_TRAIL_DOLLARS                = 60.0  # fallback when DYNAMIC=False
SCALP_BTC_TRAIL_DYNAMIC_ENABLED        = True
SCALP_BTC_TRAIL_DYNAMIC_K              = 0.4
SCALP_BTC_TRAIL_DYNAMIC_MIN            = 25.0
SCALP_BTC_TRAIL_DYNAMIC_MAX            = 100.0

# Universal safety (cross-tier — load-bearing safety net)
MAX_RISK_PER_WINDOW_DOLLARS            = 15.0   # gross $ per window cap
MAX_ENTRIES_PER_WINDOW                 = 99    # relaxed for multi-flip
MAX_TRADES_PER_SESSION_TICKER          = 99    # relaxed 2026-05-08
```

---

## Critical operational gotchas (read before debugging)

### 1. `_uc()` caches user_config at module import

The `_uc(name, default)` function reads from `_user_cfg`, built **once**
when the engine starts. **Editing `user_config.py` while the engine is
running has NO effect** until `nssm restart BTCBiasEngine`. After any
config change, restart and verify the new value appears in the next
relevant log line.

### 2. Position state lives on TWO objects

| Object | Used by | Exit logic |
|---|---|---|
| `self._direction_position` | SCALP fills (current) | `_scalp_manage` + `_scalp_fire_exit` (trail-aware, INVERSE_REENTRY hook) |
| `self._open_position` | Legacy/orphan adoptions | `_maintain_protective_order` (fixed-TP cancel-replace) |

**SCALP fills must end up on `_direction_position`.** If you see SCALP-
origin tickers on `_open_position`, that's a Fix-A regression — the
legacy path doesn't have INVERSE_REENTRY and uses fixed TPs (small-
margin profit-take pattern + cancel-replace race).

`SYNC_RECLAIM` and `ORPHAN_FLATTEN` consult
`self._direction_active_tickers` (persisted to
`data/direction_active.json` across restarts) to know which tickers
the SCALP path owns. If the ticker is in that set, those paths skip
adoption — log line: `SYNC RECLAIM SKIP-DIRECTION`.

### 3. STARTUP RECOVERY tier-aware routing (Fix A, 2026-05-09)

When the engine boots with an open Kalshi position, STARTUP RECOVERY
checks `_direction_active_tickers` (loaded from disk). If the ticker
matches, the position routes to `_direction_position` (trail-aware
SCALP exit layer) instead of legacy `_open_position` (fixed-TP cancel-
replace landmine). Pre-Fix-A this always went to legacy — caused the
00:17 PT 2026-05-09 paralysis loop.

Boot log to look for:
```
STARTUP RECOVERY: found YES Nx ... — routing to _direction_position
                                       (DIRECTION-active on disk)
```

### 4. Manual trades are invisible to engine state

`MANUAL_FILLS_CAPTURE_ENABLED=False`. The manual-fills poller LOGS your
trades from the Kalshi UI but does NOT add them to engine state. This
means:
- You can manually trade alongside the engine without conflict
- Engine BAL checks may briefly read low during your settlement
  collateral periods (use confirm-with-2nd-fetch BAL protocol)

### 5. Confirm-with-2nd-fetch BAL protocol

Kalshi shows transient low BAL during settlement. Always confirm a
catastrophic BAL drop with a second fetch ~10s later before stopping
the engine. Genuine drops read the same on both fetches; settlement
artifacts recover within a few seconds.

### 6. IOC race condition

By the time a 50ms-old book read becomes an actual order, the offer
side may have been swept. `SCALP MARKET NOFILL` at slip=5c sometimes
means "depth was there 50ms ago, gone now" — not "no offer in price
range." `SCALP_REENTRY_SLIP_C` is the dial: raising it improves fill
rate at the cost of slippage.

### 7. Maker-mode is wrong for momentum strategies

Tested 2026-05-06: maker bids only fill when the market reverses =
adverse selection. SCALP uses IOC (`post_only=False`,
`time_in_force=immediate_or_cancel`) at ask + slippage. Don't change
this without explicit user direction.

### 8. SCALP fires once per window, in the first 30s only

`SCALP_PLACE_MAX_AGE_S = 30.0`. If the engine boots mid-window (e.g.,
3 minutes into a session), it skips the place-attempt and waits for
the next window flip. This is correct behavior — don't mistake it for
a bug.

After the initial fill, the per-window lock (`_entered_tickers_this_window`
+ `SCALP_PRE_IOC_TICKER_LOCK_CHECK`) prevents a second initial entry on
the same ticker. Only the INVERSE re-entry path can fire additional
fills, and that's gated behind a successful TRAIL/BTC-TRAIL exit.

---

## Files that are load-bearing

### Core runtime (always)

- `run_copy_engine.py` — entry point
- `polymarket_copy_engine.py` — main engine. Key sections:
  - SCALP MARKET handler (~line 5050) — entry path, includes Fix-C collision check
  - `_scalp_manage` / `_scalp_fire_exit` (~line 5800) — per-tick exit + INVERSE_REENTRY hook
  - SCALP BTC-TRAIL (~line 5681) — dynamic-trail evaluator
  - SCALP INVERSE re-entry helper (~line 5980) — opposite-side IOC
  - STARTUP RECOVERY (~line 1490-1591) — Fix A tier-aware routing
  - `_on_new_poly_window` (~line 2840) — Fix B per-window lock preserve, Fix E ghost-desync skip
  - `_maintain_protective_order` (~line 21000) — Fix D 404 handling
  - `_check_window_safety` / `_record_window_fill` (~line 4676) — universal $15/window cap
- `user_config.py` — live config switchboard (SCALP section ~line 2557)
- `kalshi_client.py` — RSA-PSS REST client (`KalshiOrder` at line 65)
- `kalshi_ws.py` — WebSocket orderbook + trades
- `kalshi_tape.py` — per-ticker rolling tape
- `tape_pressure.py` — pressure scoring + btc_move_300s
- `price_feed.py` — Binance/Coinbase + Brownian-Bridge prob_engine (provides `strike` for dynamic trail)
- `signal_logger.py` — async SQLite writer (`data/trades.db`, `data/signals.db`)

### Retired / dead code (kept for reference, do not invoke)

- `direction_strategy.py` — pure decision math for retired DIRECTION tier
- `unified_scorer.py` — pure 8-component scorer, retired UNIFIED tier
- `_evaluate_open_burst` (in main engine, ~line 11129) — defined but
  never called. Tags signals as TA_FORCED. Worth deleting in cleanup.

### Validation + analytics

- `tests/test_residual_reconciler.py` — oversell-guard tests
- `tests/test_place_capped_side_sell.py` — MIN-TRUTH + OVERSELL-GUARD tests
- `tests/test_direction_strategy.py` — 35 tests for retired DIRECTION
  module (still pass; left for reference)

---

## Service management (NSSM on Windows)

```powershell
nssm status BTCBiasEngine
nssm restart BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
```

Required env vars (in `credentials/kalshi.env`):

```
KALSHI_API_KEY=...
KALSHI_PRIVATE_KEY_PATH=...
EXECUTE_TRADES=true
KALSHI_DEMO=false
```

Direct Kalshi state check (bypass engine, query truth):

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

### SCALP entry / exit cycle

| Pattern | Meaning |
|---|---|
| `SCALP MARKET FILL: SIDE Nx @ Mc IOC (ask=Mc+Nc)` | Initial fill at window open |
| `SCALP MARKET NOFILL: SIDE IOC@Mc — retry in Ns` | IOC didn't fill, will retry after cooldown |
| `SCALP MARKET SKIP: ticker already entered this window` | Per-window lock fired (Fix B/C) |
| `SCALP MARKET SKIP: already on _open_position (tier=...)` | Fix-C collision check fired (legacy state owns ticker) |
| `SCALP MARKET SKIP: already on _direction_position (tier=...)` | Fix-C collision check fired (SCALP fill already exists) |
| `SCALP NEAR-CERTAIN: bid=Nc ≥ 90c` | Exit Rule 1 — holds for $1 settlement |
| `SCALP PRE-EXPIRY: side=X bid=Nc entry=Mc secs_left=N` | Exit Rule 2 condition met |
| `SCALP EXIT (PRE-EXPIRY): sold Nx ... (entry=Mc, +/-Nc/contract)` | PRE-EXPIRY exit completed |
| `SCALP BTC-TRAIL: btc_hwm=$X btc_now=$Y retrace=$Z >= trail=$T → exit` | Dynamic-trail fired (the `trail=$T` is your dynamic value) |
| `SCALP EXIT (BTC-TRAIL): sold ...` | BTC-TRAIL exit completed |
| `SCALP TRAIL: hwm=Mc bid=Nc trail=Tc → exit` | Cents-trail fired |
| `SCALP EXIT (TRAIL): sold ...` | Cents-trail exit completed |
| `SCALP LOSS-CUT: bid=Nc entry=Mc loss=Lc >= max=15c → exit` | Loss-cut fired |
| `SCALP RE-ENTRY RESET after X exit — state machine unlocked` | Post-exit state reset |
| `SCALP REVERSAL-SCORE: time=T btc=B book=K velocity=V → total=S → INVERT/SAME` | Flip decision computed |
| `SCALP INVERSE FILL: SIDE Nx @ Mc IOC` | Opposite-side flip filled |
| `SCALP INVERSE NOFILL: SIDE IOC@Mc — no liquidity` | Inverse failed (book was thin) |
| `SCALP INVERSE SKIP: WINDOW-CAP RISK ...` | $15 cap blocked further inversions |

### Boot / recovery / safety

| Pattern | Meaning |
|---|---|
| `STARTUP RECOVERY: found SIDE Nx ... — routing to _direction_position` | Fix-A tier-aware routing fired |
| `STARTUP RECOVERY: found SIDE Nx ... — locked DCA, TPs will be set` | Routed to legacy `_open_position` (non-direction-origin) |
| `SESSION-LOCK: restored N ticker(s) from disk` | Per-window lock recovered across restart |
| `DIRECTION-ACTIVE: restored N ticker(s) from disk` | SCALP-tier ticker set recovered across restart |
| `SYNC RECLAIM SKIP-DIRECTION: ticker is a DIRECTION-tier ticker` | SYNC RECLAIM correctly skipped a SCALP-owned ticker |
| `SYNC RECLAIM: Kalshi has Nx ... claiming position so stop-loss can fire` | SYNC RECLAIM adopted an orphan into legacy `_open_position` (should be rare post-Fix-A) |
| `WINDOW-CAP RISK: tier=X cost=$N + committed=$N > cap` | $15 cap enforced (expected after 5-6 flips) |
| `WINDOW-CAP UPDATE: tier=X +Nct@Mc=$N` | Post-fill counter update |
| `OVERSELL-DETECTED` / `STUCK-RESIDUAL` | Catastrophe signature — should NEVER appear in normal operation. File a P0 |
| `RESIDUAL-CLEAN: ticker (zero, reason=...)` | Position confirmed flat |
| `PROTECTIVE FLAT-CONFIRMED: ... — clearing engine state` | Position cleared after kalshi_truth=0 zero-streak ≥30s |

### Anti-patterns to grep for

```bash
# Cancel-replace paralysis loop (should NEVER spam after Fix D)
grep "PROTECTIVE cancel.*aborting cycle to avoid oversell" data/engine_history.log | tail
```

If that returns more than a few hits in a 1-second window, the loop is
back. Investigate Fix D regression.

---

## Architecture history (how we got here)

### What was tried and retired

The engine has had multiple entry tiers cycled through over months:

- `WALLET_COPY` — copied Polymarket smart-money wallets. Retired
  2026-04-29 after wallet-pool degradation.
- `TA_FORCED_SIGNAL` — TA-based momentum entries. Retired 2026-05-05 PT
  (`TA_FORCED_ENTRY_ENABLED = False`); SYNC RECLAIM still uses
  `TA_FORCED_SIGNAL` as a default tag for orphan positions, which is
  cosmetic but misleading.
- `SR_FADE` — support/resistance fade. Retired 2026-04-27.
- `BB_PURE` / `BB_TREND` / `BB_MOMENTUM` — Brownian-Bridge fair-value
  scalpers. All retired or feature-flagged off.
- `PAPER_FVG_LIVE_MODE` — fair-value gap entries. Caused -19% loss
  catastrophe 2026-05-05; permanently disabled.
- `UNIFIED` — 8-component composite scorer. Live briefly 2026-05-07,
  retired in favor of MOMENTUM_SCALP.
- `DIRECTION` — sign-aligned distance + momentum. Live 2026-05-06 to
  2026-05-08, retired in favor of MOMENTUM_SCALP. Backtest claimed
  90.5% WR but live was ~28% WR (n=7) — backtest had survivorship bias
  from assuming fills at historical mid when live pays ask+slip.
- `SNIPER`, `ARB_DETECTOR`, `MICRO_PULLBACK`, `ATM_REVERSION`,
  `LATE_DOMINANT`, `TP_LAYERED`, `SCALP_DCA` — all retired without
  replacement.

### Why MOMENTUM_SCALP is the current live tier

In thin overnight liquidity (weekends, off-hours), the side with the
higher ask is typically the one with directional momentum — market-
makers pull quotes from the cold side, leaving the favored side with
the deep book. Buying that side at IOC + small slip captures the
trend if it persists, and the BTC-TRAIL exit + INVERSE re-entry
captures the reversal when it doesn't.

The strategy is delta-aware (dynamic trail) and self-bounded (per-
window risk cap stops runaway flipping). It's the simplest tier that
exhibits the "follow the favorite, flip on reversal" pattern the user
trades manually.

### 2026-05-09 paralysis fixes (5 fixes, commits 96c6bb2 + 53aaf55)

A single root cause (count_filled typo, fixed in `13a9b8f`) had been
silently orphaning SCALP fills for an unknown number of sessions.
Every restart, those orphans got adopted into legacy `_open_position`
via SYNC RECLAIM, then the legacy fixed-TP cancel-replace path would
spam-loop. Five fixes shipped to make this unreachable through any
path:

| Fix | Where | What it does |
|---|---|---|
| A | STARTUP RECOVERY (~line 1548) | Tier-aware routing: if ticker is in `_direction_active_tickers` (disk-persisted), route to `_direction_position` instead of `_open_position` |
| B | `_on_new_poly_window` (~line 10141) | Window flip preserves any ticker with an active position (re-adds to `_entered_tickers_this_window`) |
| C | SCALP MARKET pre-IOC (~line 5215) | Refuses to fire if `_open_position` or `_direction_position` already owns the ticker |
| D | `_maintain_protective_order` (~line 21043) | Treats Kalshi `executed` status as terminal, clears stale order_id to break cancel-replace loop |
| E | `_on_new_poly_window` ghost-desync (~line 10191) | Only clears `_direction_position` if `_prev_scalp_ticker` is from a *different* window (not the current boot window) |

---

## Do / Don't

**Do:**

- Treat `user_config.py` as the live-behavior switchboard
- Check Kalshi positions API directly before trusting any engine-side P&L
- Treat `MAX_RISK_PER_WINDOW_DOLLARS = $15` as the load-bearing safety
  net (the per-ticker lock is now relaxed for SCALP)
- Use confirm-with-2nd-fetch BAL protocol before stopping on catastrophe
- Verify Fixes A-E + count_filled fix are present after `git pull`:
  `git log --oneline | head -10` should show `53aaf55`, `96c6bb2`,
  `13a9b8f`, `f4a0853` near the top

**Don't:**

- Re-enable retired strategies without explicit user direction (every
  retired tier has a catastrophe story; see `MEMORY.md`)
- Disable `MAX_RISK_PER_WINDOW_DOLLARS` (load-bearing safety net)
- Disable `SAFETY_OVERSELL_HARDENING`
- Disable `MANUAL_FILLS_CAPTURE_ENABLED=False` (engine should NOT adopt
  user's manual trades)
- Set `MOMENTUM_SCALP_ENABLED=False` without a replacement strategy
  enabled — doing so leaves the engine running but never trading
- Modify the SCALP path's per-window lock without modeling whether
  multi-flip indefinite re-entry still works
- Trust older comments in `polymarket_copy_engine.py` referencing
  retired tiers as live (BB_PURE, TA_FORCED, DIRECTION, etc.)

---

## Reading order for new AI agents

1. **This document (CLAUDE.md)** — operator-facing source of truth
2. **`docs/NEW_INSTANCE_SETUP.md`** — install + paper validation +
   pre-live checklist
3. **`user_config.py`** — every live behavior knob (SCALP section ~2557)
4. **`polymarket_copy_engine.py`** key sections:
   - SCALP MARKET handler (~line 5050)
   - `_scalp_manage` / `_scalp_fire_exit` (~line 5800)
   - SCALP BTC-TRAIL (~line 5681)
   - SCALP INVERSE re-entry (~line 5980)
   - STARTUP RECOVERY (~line 1490-1591)
   - `_check_window_safety` / `_record_window_fill` (~line 4676)
5. **`kalshi_client.py`** — `KalshiOrder` dataclass (line 65),
   `_parse_order` (line 666). The `filled_count` vs `count_filled`
   typo at SCALP fill-detection sites was the silent-bug source for
   weeks; verify your tree has commit `13a9b8f` if SCALP behavior
   looks broken.
6. **Recent commits in chronological order** (`git log --oneline -20`)
   — context for *why* current state exists
7. **`MEMORY.md`** (in `~/.claude/projects/.../memory/`) — prior
   catastrophe lessons (oversell, unauthorized strategies, balance
   tracking)

---

## Tests

```bash
python -m pytest tests/ -q --ignore=tests/test_bias_engine.py \
    --ignore=tests/test_consensus.py --ignore=tests/test_phase3.py \
    --ignore=tests/test_strategy_index.py
```

Expect ~588 passing with a small number of pre-existing failures in
tests that reference retired tiers. Treat those as known noise unless
the count spikes upward (i.e., a new failure that wasn't there before).

The most relevant tests for SCALP correctness:

- `tests/test_residual_reconciler.py` — A5 oversell-guard
- `tests/test_place_capped_side_sell.py` — MIN-TRUTH + OVERSELL-GUARD

---

## Recent material changes (newest first)

### 2026-05-09 PT — paralysis fixes + dynamic trail

- Fix E: `_on_new_poly_window` ghost-desync skip (commit `53aaf55`)
- Fixes A/B/C/D: STARTUP RECOVERY tier-aware routing, window-flip
  ticker-lock preservation, SCALP MARKET collision check, PROTECTIVE
  cancel-loop terminal-status fix (commit `96c6bb2`)
- Dynamic BTC trail scaled by strike distance (commit `f4a0853`)
- SCALP IOC fill detection: `count_filled` → `filled_count`
  (commit `13a9b8f`)

### 2026-05-08 PT — SCALP overhaul

- `MOMENTUM_SCALP_ENABLED = True` becomes the single live entry tier
- All other tiers gated off
- `MAX_TRADES_PER_SESSION_TICKER` relaxed to 99 (was 1)
- `MAX_ENTRIES_PER_WINDOW` relaxed to 99 (was 1)
- `BB_PURE_PER_TICKER_LOCK_ENABLED = False` (BB_PURE was briefly
  re-enabled then re-retired)
- `SCALP_PRE_IOC_TICKER_LOCK_CHECK = True` (fixes retry-on-same-ticker)
- INVERSE_REENTRY_ON_CLOSE briefly enabled then disabled

### 2026-05-07 PT — multi-tier cascade era (retired)

- Universal $15/window cap + 1-entry-per-window count cap (count cap
  later relaxed to 99)
- DIRECTION + UNIFIED + BB_PURE + PENNY all live in cascade
- DIRECTION_EXIT_ENABLED=True (active 5-rule exit layer)
- This entire architecture was replaced by single-tier MOMENTUM_SCALP
  the next day

### Earlier history

See git log + `to-do/MONITORING_*.md` for behavioral observations from
specific live sessions.
