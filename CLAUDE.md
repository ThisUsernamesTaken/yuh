# BTC Bias Engine — Current System Reference

**Last updated**: 2026-05-02 PT (strategic reset)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live primary signal**: `BB_PURE` (Brownian-Bridge fair-value engine)
**Status**: Live trading enabled (`PAPER_TRADING = False`); engine currently STOPPED pending operator restart.

This document is the operator-facing source of truth. **If this doc and code disagree, the code wins.** Update this doc whenever signal logic or config defaults materially change.

> **Setting up the engine on a new machine?** See [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md) for the clean-install walkthrough (clone → venv → credentials → NSSM → smoke test → flip-to-live checklist).

---

## What the engine does

Trades Kalshi `KXBTC15M` 15-minute BTC binary options.

**Single live strategy: `BB_PURE`** — pure Brownian-Bridge mispricing.

```
fair_yes_cents (from BB model in price_feed.py:prob_engine)
  vs market_mid_cents (from Kalshi WS)
  → edge_pp = fair − market
  → if |edge_pp| ≥ BB_PURE_MIN_EDGE_PP (8 default):
       side = underpriced side (buy cheap)
       contracts = quarter-Kelly × balance, capped at BB_PURE_KELLY_MAX_FRAC
       fire ONE order per ticker per 15-min window
```

That's the entire strategy. No regime classifier, no FVG composite, no wallet copy, no SR fade, no SCALP DCA. The microstructure layers exist but are **execution-quality filters**, not signal generators.

---

## Lifecycle of a trade

```
[1] BB model + Kalshi WS book → BBSignal (bb_pure.evaluate())

[2] STRATEGIC GATES (block early if market context is wrong)
    - Strike-distance: |BTC − strike| / BTC < 0.0004 (0.04%)
                       gamma is high near strike; far from strike, prices are noise
    - Time-of-day:     06:00 ≤ PT hour < 22:00 (no overnight)
    - Per-window lock: ticker hasn't been entered yet this window
    - Cheap-side cap:  entry ≤ BB_PURE_MAX_ENTRY_CENTS (55c default)

[3] MICROSTRUCTURE GATES (legacy, still active)
    - Entry-flow gate (ENTRY_FLOW_GATE_ENABLED)
    - Book-density gate (BOOK_DENSITY_GATE_ENABLED)
    - BTC stability gate / asymmetric vol cap
    - BTC adverse velocity gate
    - Range-position gate

[4] PHASE 8 TAPE PRESSURE (currently shadow OFF; data-only when on)
    - Pre-entry absorption signal (5-min lookback)
    - Live BLOCK gate behind BB_PURE_TAPE_GATE_ENABLED (False default)

[5] PLACE ORDER
    - Maker-bid entry: price = bid + 1, post_only = True (default)
    - Stale-entry cancel after 8s if NOFILL (BB_PURE_ENTRY_NOFILL_TIMEOUT_S)
    - Per-window ticker lock persisted to data/session_state.json

[6] PROTECTIVE_MAINTAIN (single resting Kalshi-side sell)
    - Three states: TP / SL / HOLD
      • TP   when bid > entry  → maker sell at FVG-close target
      • SL   when bid ≤ entry − sl_offset (8c) → cross-spread sell
      • HOLD otherwise → preserve existing TP, no flip
    - MFE-aware trail (re-arms TP above FVG-close as bid climbs)
    - MID-TRADE BTC velocity SL (sustained N polls of |vel| ≥ threshold)
    - >1-resting OVERSELL-GUARD (cancels all + aborts)
    - Position-zero gate (no sells when Kalshi truth = 0)
    - Cancel-first-verified (no place-then-pray)

[7] FLAT-CONFIRMED (clears engine state when Kalshi truly says we're flat)
    - Requires N=3 consecutive zero readings spaced over 6s
    - Defends against Kalshi cache blips that previously caused phantom positions

[8] ORPHAN-FLATTEN (last-resort cleanup, every 3s)
    - Skip active ticker (protective_maintain owns it)
    - Skip recently-placed tickers (within 90s — ORPHAN_FLATTEN_RECENT_S)
    - Cross-spread sell at bid − ORPHAN_FLATTEN_OFFSET_C (1c default)

[9] CLOSE & RESIDUAL SWEEP
    - _cancel_tp_order cancels TP, entry order_id, AND ANY resting buy on ticker
    - Post-close residual poll for 30s (POST_CLOSE_RESIDUAL_SWEEP_ENABLED)
```

---

## Critical config (live values)

```python
# Live trading
PAPER_TRADING = False
BB_PURE_MODE = True
TA_FORCED_ENTRY_ENABLED = False  # legacy, retired

# Strategic gates (2026-05-02 reset, encode user-validated alpha)
BB_PURE_STRIKE_DISTANCE_GATE_ENABLED = True
BB_PURE_MAX_STRIKE_DIST_PCT          = 0.0004  # ±0.04%
BB_PURE_TRADING_HOURS_GATE_ENABLED   = True
BB_PURE_TRADING_HOUR_START_PT        = 6
BB_PURE_TRADING_HOUR_END_PT          = 22
BB_PURE_PT_UTC_OFFSET_H              = -7.0    # PDT; -8 in winter

# Entry execution
BB_PURE_ENTRY_MODE                   = "maker_bid_plus_1"  # post_only at bid+1
BB_PURE_ENTRY_NOFILL_TIMEOUT_S       = 8.0     # cancel stale unfilled entry
BB_PURE_MIN_EDGE_PP                  = 8.0
BB_PURE_MIN_ENTRY_CENTS              = 5
BB_PURE_MAX_ENTRY_CENTS              = 55      # cheap-side bias
MAX_TRADES_PER_SESSION_TICKER        = 1       # one trade per 15-min window

# Sizing (2026-05-02 reset, tight while shaking out)
BB_PURE_KELLY_FRACTION               = 0.25    # quarter-Kelly base
BB_PURE_KELLY_MAX_FRAC               = 0.05    # cap = 5% bankroll per trade
SIZING_MAX_FRACTION                  = 0.08    # absolute ceiling
SIZING_HARD_CAP_CONTRACTS_DAY        = 8       # static fallback
DAILY_LOSS_FRACTION                  = 0.20    # halt at 20% bankroll loss

# Protective layer
PROTECTIVE_ORDER_MODE                = True
PROTECTIVE_SL_OFFSET_C               = 8       # trigger at entry−8c
PROTECTIVE_FLAT_CONFIRM_S            = 30.0    # cache-lag confirmation window
PROTECTIVE_FLAT_CONFIRM_COUNT        = 3       # N consecutive 0-readings req'd
PROTECTIVE_FLAT_CONFIRM_SPAN_S       = 6.0     # AND span ≥ Ms
PROTECTIVE_MID_TRADE_ADVERSE_VEL     = 20.0    # $/s threshold
PROTECTIVE_MID_TRADE_PERSIST_COUNT   = 3       # consecutive polls req'd

# Cleanup paths (2026-05-02 reset, softened)
ORPHAN_FLATTEN_ENABLED               = True
ORPHAN_FLATTEN_POLL_S                = 3.0
ORPHAN_FLATTEN_RECENT_S              = 90.0    # skip recently-placed tickers
ORPHAN_FLATTEN_OFFSET_C              = 1       # was 5c — softened for our size

# DISABLED / RETIRED
TA_FORCED_ENTRY_ENABLED              = False  # legacy multi-tier composite
LATE_DOMINANT_ENABLED                = True   # but rarely fires
SR_FADE_ENABLED                      = False
SNIPER_ENABLED                       = False
SCALP_DCA_ENABLED                    = False  # hard-killed 2026-05-02
TP_LAYERED_ENABLED                   = False
WALLET_COPY_*                        = False  # all four
ARB_DETECTOR_ENABLED                 = False  # 2026-05-02 reset
ATM_REVERSION_ENABLED                = False  # 2026-05-02 reset
PAPER_FVG_ENABLED                    = False  # 2026-05-02 reset
BB_PURE_TAPE_SHADOW_ENABLED          = False  # 2026-05-02 reset
BB_PURE_TAPE_EXIT_SHADOW_ENABLED     = False  # 2026-05-02 reset
BB_PURE_TAPE_GATE_ENABLED            = False  # never enabled live
```

---

## Files that are load-bearing

### Core runtime (always)

- `run_copy_engine.py` — entry point, credential bootstrap
- `polymarket_copy_engine.py` — main engine; the 20k-line file containing eval, entry, protective layer, reconciliation
- `user_config.py` — live config switchboard
- `bb_pure.py` — pure module: Brownian-Bridge math + Kelly sizing + tier resolution
- `kalshi_client.py` — RSA-PSS signed REST client
- `kalshi_ws.py` — WebSocket client for orderbook/trades
- `kalshi_tape.py` — per-ticker rolling tape of trades + mids
- `price_feed.py` — Binance/Coinbase ingestion + Brownian-Bridge model (`prob_engine`)
- `tape_pressure.py` — pure module: absorption / opposite-flow signals (Phase 8/8b, currently shadow OFF)
- `protective_math.py` — pure module: MFE-aware trail decision + asymmetric vol orientation
- `sell_safety.py` — pure module: inventory cap math + filter helpers for sell-placement
- `signal_logger.py` — async SQLite writer (data/trades.db, data/signals.db)

### Background workers

- `whale_monitor.py` — mempool whale alerts (logs only, no trading influence)

### Optional / not on live entry path

- `paper_trader.py`, `sniper.py`, `contract_sr.py`, `terminal_copy.py` — present but feature-flagged off
- `microstructure.py`, `regime.py`, `mtf_scorer.py`, `tf_analyzer.py`, `indicators.py`, `ta_module.py` — used by retired TA_FORCED tier; some helpers still imported

### Historical / archival (NOT current behavior)

- `_archive/`, `docs/archive/`, `paper_engine/`, most of `to-do/`, `settings/`

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
python -c "
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
        data = await c._request('GET','/portfolio/orders',params={'status':'resting','limit':50})
        print(f'Resting: {len(data.get(\"orders\",[]))}')

asyncio.run(main())
"
```

---

## Key log patterns (`data/engine_history.log`)

| Pattern | Meaning |
|---|---|
| `BB_PURE FIRE: SIDE Nx @ Mc` | Engine placed an entry order |
| `BB_PURE FILL: SIDE Nx @ Mc order=...` | Entry filled (full or partial) |
| `BB_PURE NOFILL: order=...` | Entry placed but no immediate fill |
| `BB_PURE NOFILL CANCEL: ...` | Stale-entry cancel fired (8s timeout) |
| `BB_PURE NOFILL late-fill: ...` | Order filled before timeout could cancel |
| `BB_PURE STRIKE-DIST-BLOCK: ...` | Strategic gate blocked (too far from strike) |
| `BB_PURE HOURS-BLOCK: ...` | Strategic gate blocked (overnight) |
| `BB_PURE SKIP: already entered this window` | Per-window ticker lock holding |
| `BB_PURE GATE-BLOCK: ...` | Microstructure gate blocked entry |
| `PROTECTIVE [TP]/[SL]/[HOLD]: ...` | Protective state transitions |
| `PROTECTIVE FLAT-CONFIRMED: ... zero_streak=N/3` | Engine cleared state via Kalshi-truth confirmation |
| `PROTECTIVE MID-TRADE-SL: ... streak=N/3` | Sustained adverse BTC velocity forced SL |
| `ORPHAN-SKIP: ... recent placement` | Recency protection prevented bad flatten |
| `ORPHAN-FLATTEN: ... orphan position auto-cleared` | Genuine orphan flattened |
| `RESIDUAL-CLEAN: ...` | Position confirmed flat post-close |
| `SESSION-LOCK: restored N ticker(s)` | Per-window lock loaded from disk on startup |

---

## Known gaps (priority order)

1. **A0: P&L attribution gap** — when PREFLIGHT-TP fills, the engine's WS event handler doesn't trigger the position-closed logger. Every successful exit closes silently → `kalshi_trades` table missing close rows → P&L unmeasurable from logs alone. **Highest priority for next coding session.**
2. **Pre-fire balance gate** — when BAL is low, ORPHAN-FLATTEN can fail with `insufficient_balance`. Need to block new entries when BAL < 2× projected entry cost.
3. **TA_FORCED stale tests** — `test_late_dominant.py` has 2 pre-existing failures unrelated to current strategy. Should be pruned.

---

## Do / Don't

**Do:**

- Treat `user_config.py` as the live-behavior switchboard
- Check Kalshi positions API directly before trusting any engine-side P&L
- Verify `PAPER_TRADING=False` and `BB_PURE_MODE=True` before assuming live behavior
- Use `MAX_TRADES_PER_SESSION_TICKER=1` as the inviolable rule — one engine entry per 15-min ticker

**Don't:**

- Re-enable shadow strategies (`PAPER_FVG`, `ATM_REVERSION`, `ARB_DETECTOR`, `BB_PURE_TAPE_*_SHADOW`) for live sessions — they obscure real signal
- Disable the `MAX_TRADES_PER_SESSION_TICKER` lock or the `_entered_tickers_this_window` persistence
- Re-enable `TA_FORCED_ENTRY_ENABLED`, `SR_FADE_ENABLED`, or `SNIPER_ENABLED` — retired tiers
- Tighten `PROTECTIVE_MID_TRADE_ADVERSE_VEL` below 20 — anything tighter cascades on routine BTC noise
- Tighten `BB_PURE_MAX_STRIKE_DIST_PCT` below 0.0004 — strategic gate validated at this threshold
- Restart engine without checking Kalshi state directly — `_open_position` and Kalshi truth can disagree

---

## How to interpret git history (for AI agents picking up the codebase)

The codebase has been through several iterations. Recent strategic-reset commits (2026-05-02 PT) reflect the **current intended architecture**. Earlier commits patch problems that the reset addressed at the source — read in chronological order to understand context, but **defer to the reset commits** for current behavior.

Key recent commits (chronological):

- `adc87f6` Strategic reset #1: strike-distance + time-of-day entry gates
- `fe7ba8c` Strategic reset #2: maker-bid entry default
- `557c142` Strategic reset #3: soften cleanup paths
- `9035834` Strategic reset #4: disable shadow strategies
- (sizing reduction next)
- (CLAUDE.md update — this commit)

Pre-reset patches (still in code, still active) are the safety stack:
- `7a3e3e4` P0a: FLAT-CONFIRMED requires N consecutive zero readings
- `cf756aa` P0b: ORPHAN-FLATTEN restores recency protection
- `2d62e4b` BB_PURE NOFILL: cancel stale entry orders after timeout
- `0ee3f1c` Phase 0.1.4 + 0.1.5: orphan-buy sweep + cheap-side cap (55c)
- `bdf91cb` Phase 0.1.3: hard kill SCALP DCA (BB_PURE-only mode)
- `4abf20b` Phase 0.1.2: persist per-window ticker lock across engine restarts
- `39d6c06` Phase 0.1.1: route ORPHAN-FLATTEN through `_place_capped_side_sell`
- `18fc7a0` Phase 0.1: position-zero gate + >1 OVERSELL-GUARD on sell helper

The pure helper modules (`bb_pure.py`, `tape_pressure.py`, `protective_math.py`, `sell_safety.py`) are the cleanest code. Read those first to understand the math.

---

## Tests

```bash
python -m pytest tests/ -q --ignore=tests/test_bias_engine.py \
    --ignore=tests/test_consensus.py --ignore=tests/test_phase3.py \
    --ignore=tests/test_strategy_index.py
```

Expect: ~421 passing, 2 pre-existing LATE_DOMINANT sizing failures (unrelated to current strategy — flagged for cleanup).

The 4 ignored test files reference modules deleted long ago (regime_detector, etc.). Safe to delete the tests too in a future cleanup commit.

---

## Reading order for new AI agents

1. **This document (CLAUDE.md)** — operator-facing source of truth
2. **`bb_pure.py`** — pure math, the entire trading thesis
3. **`tests/test_bb_pure.py`** — pins down each decision rule
4. **`user_config.py`** — every live behavior knob in one file (top-to-bottom)
5. **`polymarket_copy_engine.py:_evaluate_bb_pure_signal`** — entry-point in the engine
6. **`polymarket_copy_engine.py:_execute_bb_pure_signal`** — entry-execution path
7. **`polymarket_copy_engine.py:_maintain_protective_order`** — exit/safety layer
8. **`tape_pressure.py`, `protective_math.py`, `sell_safety.py`** — supporting pure helpers
9. **Recent commits in chronological order** — context for *why* the code looks the way it does

Skip on first read: any of the disabled-tier code (TA_FORCED, SR_FADE, SNIPER, SCALP DCA, wallet copy). They're feature-flagged off and don't affect current behavior.
