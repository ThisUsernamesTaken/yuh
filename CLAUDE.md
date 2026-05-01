# BTC Bias Engine - Current System Reference

**Last updated**: 2026-04-30 (PM patch — microstructure gating)
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Live primary signal**: `TA_FORCED` (FVG / Brownian-Bridge engine)
**Status**: Live trading enabled (`PAPER_TRADING = False`)

This document is the operator-facing source of truth for the current live engine. If this doc and code disagree, the code wins. Update this doc whenever signal logic or config defaults materially change.

## Microstructure gating layer (2026-04-30 PM, default OFF)

Five-component upgrade to entry/exit decisions, designed after morning's
$15 hold-to-expiry losses + repeating "catch falling knife / sell the bottom"
pattern. Built behind feature flags — engine behavior is **unchanged** until
flags are enabled. Phased rollout per user direction.

| Phase | Flag | Component | What it does |
|---|---|---|---|
| 1 | `KALSHI_TAPE_ENABLED` | `kalshi_tape.py` | Records every WS trade event into per-ticker ms-resolution deque. Pure data layer, no behavior change. Master prerequisite for phases 2-4. |
| 2 | `STOP_REQUIRES_PERSISTENCE` | `_evaluate_stop_persistence()` | Bid touch alone no longer fires stop. Requires duration (5s default, scaled by session minute) AND volume confirmation (≥10ct traded at-or-below trigger in 10s) AND defers if thick bid resting at trigger. Pre-expiry trigger exempt — always fires. |
| 3a | `ENTRY_FLOW_GATE_ENABLED` | `_evaluate_entry_filter()` | Blocks TA_FORCED entry when adverse flow > 60% of recent volume AND no exhaustion detected, OR when adverse-side velocity > 2c/sec. Catches "falling knife" entries. |
| 3b | `BOOK_DENSITY_GATE_ENABLED` | `LocalOrderBook.density()` + entry filter | Blocks entry when same-side bid stack < 50ct OR opposite-ask dominance > 4×. Catches thin-book setups. |
| 4 | `PROTECTIVE_ORDER_MODE` | `_maintain_protective_order()` | Replaces bid-check stop entirely. Maintains a single resting Kalshi-side sell at TP price (when bid > entry) or SL price (when bid ≤ entry). Re-pegs only when state flips or count changes. Atomic trigger — survives engine crash / API revoke / cache lag. |

**Per-session-minute strictness:** entry/stop thresholds scale by session
minute via `SESSION_STRICTNESS_BUCKETS` and `SESSION_STOP_PERSIST_BUCKETS_S`.
0–3 min uses 1.5× strict (high-uncertainty zone); 13–15 min uses 0.6× looser
(pre-expiry exit gates).

**Observability:** every gate decision (pass/block/defer/fire) is written
to `data/trades.db` `gate_decisions` table with full input snapshot. Use
post-hoc joins against `kalshi_trades` to correlate gate decisions with
trade outcomes and tune thresholds empirically.

**Activation status (2026-04-30 PM):** ALL 5 FLAGS ON. Activated together
rather than phased per user direction: paper testing is inaccurate; real
execution behavior is the learning signal.

```python
KALSHI_TAPE_ENABLED          = True
STOP_REQUIRES_PERSISTENCE    = True
ENTRY_FLOW_GATE_ENABLED      = True
BOOK_DENSITY_GATE_ENABLED    = True
PROTECTIVE_ORDER_MODE        = True
```

### How the layers interact during a trade lifecycle

A typical TA_FORCED entry now flows through this sequence:

```
[1] Signal cascade fires → candidate trade (side, entry_px, ct)

[2] ENTRY FLOW GATE evaluates trade tape:
        - Adverse-side flow share over last 5s
        - Mid velocity over last 5s
        - Adverse-side deceleration check (vs 15s baseline)
        Block if catching a knife.

[3] BOOK DENSITY GATE evaluates orderbook:
        - Same-side bid stack < 50ct → block (thin support)
        - Opposite-side dominance > 4× → block (one-sided book)

[4] If all gates pass → place_order placed at limit price.

[5] Cache-lag window (Kalshi positions API may return 0 for 30-180s):
        - Bid-check stop logic runs but DEFERS via Patch #11
        - Position kept in _open_position, not orphaned
        - SYNC RECONCILE BACKFILL fires once Kalshi reflects the position
        - Stop is then "armed"

[6] Once Kalshi-confirmed (truth_ct > 0):
        Protective-order mode activates → maintains a single resting
        Kalshi-side limit sell at TP price (when bid > entry) or SL
        price (when bid <= entry). Re-pegs only on state flip / count
        change, debounced 2s.
        
        While protective active: pos["_protective_active"] = True →
        legacy bid-check stops skip (gated on `not _protective_active`).

[7] Stop persistence:
        For positions where protective mode hasn't taken over yet (e.g.,
        still in cache-lag window), bid-check stops still run BUT with
        new persistence requirements:
            - Bid ≤ trigger for ≥ N seconds (N varies 1-8s by session minute)
            - ≥ 10ct traded at-or-below trigger in 10s
            - Defer if thick bid (≥ 100ct) below trigger
        Pre-expiry trigger always fires (no persistence required).

[8] Pre-expiry forced flatten:
        At < 60s remaining, protective mode force-sells @ 1¢ regardless.

[9] Every gate decision logged to data/trades.db gate_decisions for
    post-hoc analysis (correlate pass/block with subsequent outcomes).
```

### Why both bid-check stops AND protective-order mode coexist

Cache lag is the gap. Protective-order mode requires `truth_ct > 0` from
Kalshi `get_positions()`, which can take 30-180s after fill. During that
window, the protective layer can't see the position to protect it. The
bid-check stop (with persistence + deferred-on-zero) IS the protection
during cache lag; protective mode takes over once Kalshi confirms.

Both gated on `_protective_active` flag — they don't double-fire.

### Observability via `gate_decisions` table

Every gate (entry_flow, book_density, stop_persistence, bb_pure_signal)
writes a row with full input snapshot. Example queries for tuning:

```sql
-- Block / pass distribution per gate
SELECT gate, decision, COUNT(*) FROM gate_decisions
WHERE ts_ms > strftime('%s','now','-12 hours')*1000
GROUP BY gate, decision;

-- Which entries got blocked, joined with what would have been the trade
SELECT gd.ts_ms, gd.ticker, gd.reason, gd.side, gd.entry_cents
FROM gate_decisions gd
WHERE gd.gate='entry_flow' AND gd.decision='block';
```

---

## v2 Refactor: BB_PURE_MODE (Session 1 shipped, default OFF)

Founding philosophy: trade pure Brownian-Bridge mispricing. Built but
not yet active — implementation matrix:

| File | Status | Purpose |
|---|---|---|
| `bb_pure.py` | ✓ shipped | Pure math: `evaluate(market_mid, fair_yes, secs_to_exp, balance, config) → BBSignal` |
| `tests/test_bb_pure.py` | ✓ 22/22 pass | Pins down each decision rule including founding-doc canonical example |
| `_evaluate_bb_pure_signal()` on engine | ✓ shipped | Reads BB model + book + window time, calls `bb_pure.evaluate()`, logs to gate_decisions |
| `BB_PURE_*` config knobs | ✓ shipped, all OFF | Master flag + 8 tuning params |
| Cascade integration | ⏳ Session 2 | Wire `_evaluate_bb_pure_signal()` to bypass TA_FORCED cascade when ON |
| Fair-value-anchored protective order | ⏳ Session 2 | TP/SL targets from fair value updates, not entry-relative |
| Cull retired tiers | ⏳ Session 4 | Once BB_PURE proves out, remove SR_FADE / SNIPER / wallet copy / DOMINANT mid-trade upgrade etc. |

### BB_PURE design rationale (founding philosophy)

The Brownian-Bridge probability engine in `price_feed.py:prob_engine` was
the founding indicator of the project — it models the 15-min binary as a
derivative under Brownian Bridge dynamics, computing fair YES probability
via Normal CDF (Abramowitz & Stegun erf). The current engine still has
this model intact but treats it as one input among ~8 in the TA_FORCED
composite cascade.

BB_PURE_MODE returns the engine to first principles:

```
fair_yes_cents (from BB model) vs market_mid_cents (from Kalshi)
    → edge_pp = fair - market
    → if |edge_pp| ≥ 8pp:
         side = underpriced side
         entry = cheap-side price
         contracts = quarter-Kelly × balance / entry, capped at 15% bankroll
```

That's the entire signal. No pressure score, no MTF, no regime classifier,
no wall-consumption detector. The microstructure gates from earlier this
afternoon (entry_flow, density, stop_persistence) stay in their proper
role: **execution-quality filters**, not signal generators.

Founding-doc canonical example, locked into a unit test:

```python
# fair=60%, market=80c → buy NO at 20c
sig = bb_pure.evaluate(market_mid_cents=80, fair_yes_cents=60, ...)
assert sig.side == "no"
assert sig.suggested_entry_cents == 20
assert sig.win_probability == 0.40
# kelly (quarter): 0.25 × full_kelly = 0.25 × 0.25 = 0.0625 (6.25% bankroll)
```

---

## What the engine does now

Trades Kalshi `KXBTC15M` 15-minute BTC binary options.

The current live entry tier is `TA_FORCED` — the FVG (Fair Value Gap) / Brownian-Bridge engine — which combines:

- Brownian-Bridge fair-value model from `price_feed.py`
- Fair-value-gap (FVG) detection vs session baseline
- Microstructure pressure (book imbalance, trade flow, BTC impulse)
- Wall-consumption detector (aggressor-side flow)
- Regime classifier (TRENDING_UP / DOWN / RANGING / VOLATILE / EXPLOSIVE / MEAN_REVERTING)
- 8¢-below-entry stop-loss on every claimed position
- Multi-tier TP ladder

The engine enters with maker-only limits, manages take-profit / stop-loss while in position, and runs a position reconciler that catches "GHOST race" mismatches between Kalshi truth and engine state.

`SR_FADE` and `SNIPER` tiers are retired (disabled). Wallet-copying logic remains in the codebase but is fully gated off — copy-trading is **prohibited from influencing engine behavior** per 2026-04-30 user directive. The `terminal_copy` module is not even imported unless both `WALLET_COPY_ENGINE_ENABLED` and `WALLET_COPY_TRADES_ENABLED` are True.

---

## Current live signal stack

### Active

1. `TA_FORCED` (FVG / Brownian-Bridge — primary)
   `TA_FORCED_ENABLED = True` and `TA_FORCED_ENTRY_ENABLED = True`. Fires when the BB fair-value gap is non-marginal, regime is favorable, microstructure pressure agrees, and a 5-min BTC trend gate passes (`DOMINANT_BTC_5M_THRESHOLD = 15.0`).

2. `LATE_DOMINANT` (final-3-min momentum)
   `LATE_DOMINANT_ENABLED = True`. Fires in the last 3 min of a window when BTC has moved decisively and the in-the-money side is priced 80–96¢. Confidence-composite gated.

3. `MANUAL_TP` (auto-TP on user manual fills)
   `MANUAL_TP_ENABLED = True`. Engine watches for fills it didn't place and lays a TP ladder on them.

4. `ARB_DETECTOR` (observability only)
   `ARB_DETECTOR_ENABLED = True` but `ARB_TRADES_ENABLED = False`. Scans for YES+NO < 97¢ mispricings and logs them; does not place orders.

### Disabled / retired

1. `SR_FADE` — `SR_FADE_ENABLED = False` (retired 2026-04-27 per user directive)
2. `SNIPER` — `SNIPER_ENABLED = False`
3. Wallet-driven tiers — `WALLET_COPY_ENABLED = False`, `WALLET_SCORING_ENABLED = False`, `WALLET_COPY_ENGINE_ENABLED = False`, `WALLET_COPY_TRADES_ENABLED = False`. The `terminal_copy.py` module is gated at the import call site and won't load unless ALL four are flipped on.
4. Layered TP — `TP_LAYERED_ENABLED = False`
5. Micro-pullback entry — `MICRO_PULLBACK_ENABLED = False`

---

## Critical config reality

```python
PAPER_TRADING = False

# Active tiers
TA_FORCED_ENABLED            = True
TA_FORCED_ENTRY_ENABLED      = True
TA_FORCED_STOP_ENABLED       = True   # 8c-below-entry stop-loss — never disable
TA_FORCED_STOP_CENTS         = 8      # cents below entry, NOT absolute price
LATE_DOMINANT_ENABLED        = True
LATE_DOMINANT_STOP_ENABLED   = True
MANUAL_TP_ENABLED            = True
ARB_DETECTOR_ENABLED         = True
ARB_TRADES_ENABLED           = False  # detector-only by user policy

# Retired
SR_FADE_ENABLED              = False  # 2026-04-27 — retired
SNIPER_ENABLED               = False
WALLET_COPY_ENABLED          = False
WALLET_SCORING_ENABLED       = False
WALLET_COPY_ENGINE_ENABLED   = False  # 2026-04-30 — copy-trading prohibited
WALLET_COPY_TRADES_ENABLED   = False  #                from influencing engine

# Execution
MAKER_ONLY                   = True
ADAPTIVE_BID_ENABLED         = True   # bid+1 on conf >= 0.75

# Dynamic safety (2026-04-29 patches)
DAILY_LOSS_FRACTION          = 0.20   # daily limit = 20% of balance
SIZING_CAP_BALANCE_FRAC_DAY  = 0.30   # day ct cap = 30% × balance
SIZING_CAP_BALANCE_FRAC_NIGHT= 0.10   # night ct cap = 10% × balance
KELLY_MAX_FRAC               = 0.15   # Kelly hard ceiling
SIZING_MAX_FRACTION          = 0.20   # absolute per-position ceiling
TRADE_COOLDOWN_SECONDS       = 3600   # 1-hour spacing between fills

# Static fallbacks (used pre-poll or if dynamic fractions = 0)
DAILY_LOSS_LIMIT             = 15.00
SIZING_HARD_CAP_CONTRACTS_DAY    = 25
SIZING_HARD_CAP_CONTRACTS_NIGHT  = 10
TA_FORCED_FIXED_MAX_CONTRACTS_DAY    = 15
TA_FORCED_FIXED_MAX_CONTRACTS_NIGHT  = 5

# Other safeties
SAFETY_OVERSELL_HARDENING    = True
POST_CLOSE_RESIDUAL_SWEEP_ENABLED = True   # 2026-04-29: 30s post-close polling
```

---

## Dynamic sizing (auto-scales with balance)

Patched 2026-04-29 — sizing now scales with the live polled balance instead of fixed dollar amounts. As the account grows or shrinks, all caps and the daily limit recompute on every check.

| Balance | Daily limit | Day ct cap | Night ct cap | Max position |
|---|---|---|---|---|
| $76 | $15.20 | 22 | 7 | $11.40 |
| $200 | $40.00 | 60 | 20 | $30.00 |
| $500 | $100.00 | 150 | 50 | $75.00 |
| $1000 | $200.00 | 300 | 100 | $150.00 |

**Implementation**:
- Module-level `_LIVE_BALANCE_DOLLARS` written by the balance poller (`polymarket_copy_engine.py:_poll_real_balance`)
- `_get_sizing_cap()` reads `SIZING_CAP_BALANCE_FRAC_DAY/_NIGHT × _LIVE_BALANCE_DOLLARS`
- New method `_get_dynamic_daily_loss_limit()` returns `balance × DAILY_LOSS_FRACTION` for the daily-loss circuit breaker

Set the dynamic fractions to 0.0 to fall back to static values.

---

## GHOST race fix (2026-04-29) — critical do-not-revert

**The bug**: When the engine placed a maker order that filled across multiple re-pegs, `_open_position` could lag behind Kalshi's truth. The legacy `SYNC RECONCILE` updated `count` and `entry_cents` but missed `original_entry_cents`, `strategy_name`, `tier`, and `original_count`. The stop-loss code requires `strategy_name` to gate and `original_entry_cents` to compute the trigger price. Missing fields = stop-loss silently no-ops. Position held to settlement on losing trades.

**Cost of the bug (parallel terminal)**: −$30.77 on a 53-contract YES @ 61¢ position (2026-04-29 13:47 PT, ticker `KXBTC15M-26APR291700-00`).

**Patches applied** (search the codebase for these comment markers):

| Patch | Marker | Behavior |
|---|---|---|
| #1 BACKFILL stop-loss-critical fields | `2026-04-29 GHOST-bug fix: backfill STOP-LOSS-CRITICAL fields` | Populates `original_entry_cents`, `original_count`, `strategy_name`, `tier`, `fill_time` on existing `_open_position` so stop-loss arms |
| #2 RECLAIM when `_open_position` is None | `2026-04-29 GHOST-bug fix: secondary safeguard` | When `_open_position` is None or wrong-ticker but `_entered_tickers_this_window` or `_recent_placement_tickers` says it's ours, reconstruct the position dict + place TPs |
| #3 TP-EXPANSION on count growth | `2026-04-29 TP-EXPANSION fix` | When kalshi_count grows ≥3 over `_tp_placed_for_count`, cancel stale TPs and re-place sized for the new count |
| #4 Suppress stale GHOST log | `2026-04-29 GHOST log suppression` | Legacy duplicate "GHOST IGNORED" log gated behind `_sync_handled_this_iter` flag |
| #5 Track placements (catches WS-event lag) | `2026-04-29 GHOST-bug fix #5` | `_recent_placement_tickers` stamped at place_order; read by RECLAIM. Catches the case where Kalshi fills but WS fill event never arrives |

**If any of these markers are missing from `polymarket_copy_engine.py`, the patch was reverted — re-apply.**

---

## Stop-loss semantics (read carefully)

`TA_FORCED_STOP_CENTS = 8` is **delta below entry**, NOT absolute price.

```python
_trigger_px_ta = _orig_entry_ta - _stop_drop_ta   # entry MINUS 8
if _bid_ta <= _trigger_px_ta:
    # FIRE STOP — flatten position
```

| Entry | Stop trigger (entry − 8¢) |
|---|---|
| 70¢ | bid ≤ 62¢ |
| 50¢ | bid ≤ 42¢ |
| 35¢ | bid ≤ 27¢ |
| 86¢ | bid ≤ 78¢ |

`LATE_DOMINANT_STOP_CENTS = 8` follows the same convention.

**Stop-loss requires** the position dict to have `strategy_name in ("TA_FORCED_SIGNAL", "TA_FORCED")` and `original_entry_cents > 0`. The GHOST BACKFILL patch (#1) ensures these are populated on every claim — without it, stop-loss silently no-ops.

There is also an **implied-bid fallback** at both stop sites (2026-04-29): when the WS book's `best_yes_bid` / `best_no_bid` is 0 (empty side), the stop computes `effective_bid = max(actual_bid, 100 - opposite_side_ask)` so the stop arms even on thin books.

---

## Execution and exits

### Entry

- Maker-only entry: `MAKER_ONLY = True`
- Live entries rest at bid (or `bid+1` adaptive when `pressure.confidence >= 0.75`)
- Some entries are expected to never fill — maker-only acts as a filter against bad fades on flat tape
- Re-pegs occur when bid moves; old order cancelled and new placed
- **Known issue**: re-peg overrun — multiple sequential fills can accumulate beyond the cap. Mitigated by stop-loss + TP-EXPANSION patch but not eliminated.

### Position management

- TP ladder placed immediately on fill (and refreshed via TP-EXPANSION when count grows ≥ 3 ct)
- TP cap raised from 90¢ → 95¢ on 2026-04-29 to prevent guaranteed-loss TPs on 95¢ NO entries
- 8¢-below-entry stop-loss on every claimed position
- Trail-ratchet on profitable runs
- Mandatory flatten before expiry safety window
- Residual reconciler after exit cleans phantom contracts
- **Post-close residual sweep** (2026-04-29): after every close, an async task polls Kalshi positions every 2.5s for 30s and market-sells any late-arriving residual. Configured by `POST_CLOSE_RESIDUAL_SWEEP_ENABLED`.

---

## Safety layers that matter

### GHOST race patches (top priority)

See section above. **Never revert without understanding the loss case they prevent.**

### Oversell hardening

`SAFETY_OVERSELL_HARDENING = True` covers:
- Safer cancel/replace handling
- Gated DCA rebuild
- Ticker-lock protections (`MAX_TRADES_PER_WINDOW = 1` per ticker per window)
- Residual reconciler

Tests: `tests/test_residual_reconciler.py`

### Daily-loss circuit breaker

- Dynamic limit: 20% × current balance (recomputed on every check)
- Halt persists across restarts via `_reconcile_daily_pnl_on_startup()`
- Resets at UTC midnight

### SQLite WAL mode

`data/trades.db` and `data/signals.db` both run in `journal_mode=wal` with `busy_timeout=30000`, `synchronous=NORMAL`. Without this, window-rotation triggers concurrent writes that race for the file lock and produce `database is locked` errors.

Verify: `sqlite3 data/trades.db "PRAGMA journal_mode"` should return `wal`.

---

## Files that are currently load-bearing

### Core runtime

- `run_copy_engine.py` — entry point, credential bootstrap
- `polymarket_copy_engine.py` — main engine (signal eval, entry, exit, position reconciler — **all GHOST patches live here**)
- `user_config.py` — live config switchboard
- `kalshi_client.py` — RSA-PSS signed REST client
- `kalshi_ws.py` — WebSocket client for orderbook/trades
- `price_feed.py` — Binance/Coinbase price ingestion + Brownian-Bridge model
- `microstructure.py` — book pressure, trade flow, BTC impulse
- `regime.py` — regime classifier
- `edge_sizer.py` — Kelly-fraction sizing
- `signal_logger.py` — async SQLite writer for trades/signals/balance snapshots
- `whale_monitor.py` — mempool-based whale alerts
- `ta_module.py` — TA scoring used by TA_FORCED
- `mtf_scorer.py`, `tf_analyzer.py`, `indicators.py` — multi-timeframe support
- `models.py` — shared dataclasses
- `shadow_edge.py` — additive shadow-edge model (logs only)

### Optional / feature-flagged (NOT live)

- `paper_trader.py`, `sniper.py`, `sniper_signals.py`, `contract_sr.py`, `terminal_copy.py` — present but not on the live entry path

### Historical / archival (do NOT use as current behavior)

- `_archive/`, `docs/archive/`, `paper_engine/`, most of `scripts/`, `to-do/`, `settings/`

---

## PnL truth

Do not trust raw `kalshi_trades.pnl` at face value.

Authoritative sources:

1. **Kalshi positions API** (`KalshiClient.get_positions()`) — source-of-truth for current position
2. `real_balance_snapshots` (in `data/trades.db`) — high-frequency total_cents
3. `balance_snapshots` (in `data/trades.db`) — coarser, written on window changes
4. `settlement_ledger`
5. `scripts/reconcile_pnl.py` when trade-row pnl needs repair

The daily-PnL reconciler reads `real_balance_snapshots` first, falls back to `balance_snapshots`.

---

## Service management (NSSM on this machine)

```powershell
nssm status BTCBiasEngine
nssm restart BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
```

Required env vars:

- `KALSHI_API_KEY`
- `KALSHI_PRIVATE_KEY_PATH`
- `EXECUTE_TRADES=true`
- `KALSHI_DEMO=false`
- `PYTHONUNBUFFERED=1`

---

## Key log patterns (`data/engine_history.log`)

| Pattern | Meaning |
|---|---|
| `TA_FORCED FILL: ... Nx @ Mc` | Real entry filled |
| `SYNC RECONCILE BACKFILL: ... stop-loss now armed` | GHOST patch #1 fired — position properly claimed |
| `SYNC RECLAIM: ... claiming position so stop-loss can fire` | GHOST patch #2 fired — `_open_position` reconstructed from Kalshi truth |
| `SYNC RECONCILE TP: Nx placed (refreshed for grown count)` | TP-EXPANSION patch fired — TPs re-sized after re-peg overrun |
| `SELL TIER FILLED: Nx @ Mc (+Pc)` | TP exit |
| `RESIDUAL-CLEAN: ... reason=tp_ladder_sweep` | Position fully closed via TP — normal good-case exit |
| `RESIDUAL-CLEAN: ... reason=expiry_close` | **RED FLAG** — held to expiry; should not happen with patches in place |
| `TA_FORCED STOP: ... drop=8c P&L≈$-X.XX` | Stop-loss fired correctly |
| `DAILY-PNL RECONCILE: today's delta=$X (limit=$Y)` | Daily P&L state — limit should match 20% × current balance |
| `GHOST IGNORED: ... letting Kalshi settle` | External/manual position; engine left it alone (correct) |
| `terminal_copy DISABLED ...` | Confirms copy-trading is prohibited from influencing engine |
| `database is locked` | WAL mode regression — re-run WAL setup |

---

## Do / Don't

**Do:**

- Treat `user_config.py` as the live-behavior switchboard
- Check Kalshi positions API before trusting trade-row pnl reporting
- Verify `PAPER_TRADING`, `TA_FORCED_ENTRY_ENABLED`, and the GHOST patch markers before assuming live behavior
- Use the dynamic-fraction knobs (`DAILY_LOSS_FRACTION`, `SIZING_CAP_BALANCE_FRAC_DAY/_NIGHT`) as the primary tuning surface — they auto-scale with balance

**Don't:**

- Assume `SR_FADE` is the live primary tier (retired since 2026-04-27)
- Disable `TA_FORCED_STOP_ENABLED` — it's the loss cap on every position
- Disable the GHOST race patches — they prevent the catastrophic-loss case
- Re-enable `ARB_TRADES_ENABLED` (REST latency can't beat colocated HFT — user policy)
- Re-enable `SR_FADE_ENABLED` or `SNIPER_ENABLED` casually
- Re-enable copy-trading without explicit user directive (currently prohibited)
- Touch `DOMINANT_BTC_5M_THRESHOLD` unilaterally — user wants this gate restructured (microstructure-driven entries) but hasn't signed off on the change yet

---

## Cross-references

- **GHOST race patches**: `polymarket_copy_engine.py` (search `2026-04-29 GHOST-bug fix`)
- **Stop-loss code path**: TA_FORCED stop site (search `_trigger_px_ta`), LATE_DOMINANT stop site (search `_trigger_px_ld`)
- **Position reconciler**: search `SYNC RECONCILE` in `polymarket_copy_engine.py`
- **Dynamic sizing infrastructure**: `_LIVE_BALANCE_DOLLARS` + `_get_sizing_cap` + `_get_dynamic_daily_loss_limit`
- **Terminal-copy gate**: search `WALLET_COPY_ENGINE_ENABLED` in `polymarket_copy_engine.py` startup block
