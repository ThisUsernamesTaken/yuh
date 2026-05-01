# State of the Engine — 2026-04-30 PM

A narrative explanation of what the BTC Bias Engine looks like right now,
why it looks that way, and what's open/unresolved.

This pairs with the surgical handoff docs (`01_*` through `09_*`) which
focus on file-by-file changes. This doc is the "why" — read it first if
you're cold to the codebase.

---

## The 1-paragraph version

The engine is a Kalshi `KXBTC15M` 15-minute BTC binary options trader.
The founding philosophy was simple: read the Brownian-Bridge fair-value
probability, compare to market mid, trade the mispricing. Over time it
accumulated a composite signal cascade (TA_FORCED + LATE_DOMINANT +
ATM_REVERSION_DISCOUNT + various tier upgrades) that demanded multiple
unrelated factors agree before firing. Today's session diagnosed two
systemic failure modes (catching falling knives, panic-selling on
sticky-bid wicks), shipped a 5-component microstructure-gating layer to
filter execution quality, then shipped Sessions 1+2 of a `BB_PURE_MODE`
refactor that returns the signal to founding-philosophy basics — fair
value vs market, Kelly-on-edge, microstructure as execution filters.

---

## What happened today (chronological)

### Early morning (00:00–06:00 PT)

Overnight 4/29→4/30 the engine took ~$45 in losses across 7 windows that
held to expiry at $0. Root cause: Kalshi's `get_positions()` API can lag
by 60–180 seconds after a fresh fill. The engine's bid-check stop logic
fires within seconds, but `truth_ct` from Kalshi returns 0 (cache lag),
so the engine "fires" the stop without actually placing a sell, then
clears `_open_position`. Position re-appears on Kalshi later, gets
tagged "user manual trading" by the SYNC MANUAL-DETECTED gate, and
rides to expiry.

### Morning (06:00–13:00 PT)

Shipped a series of patches addressing each failure mode:

- **Patch #11** — `_open_position` survives cache-lag window. After 180s
  if Kalshi still says 0, accept flat and clear cleanly. Previously
  cleared after 60s which was too aggressive.
- **Patch #15** — OVER-FILL gate fallback. When `_engine_recent_ct=0`
  but `_recent_placement_tickers` or `_entered_tickers_this_window` says
  it's ours, use `SIZING_HARD_CAP_CONTRACTS_DAY (25)` as the cap instead
  of `0+5`. Engine can claim its own 15ct positions during cache lag.
- **Patch #16** — penny sell on SYNC RESIDUAL FLATTEN. Replace `bid-1`
  with hard `1¢ post_only=False` to guarantee fill on sticky books.
- **Hybrid stop trigger** — bid OR mid OR fair OR pre-expiry. Mid-trigger
  catches sticky-bid case (yes_bid stays elevated while yes_ask crashes
  → mid drops → stop fires).
- **Pre-expiry forced flatten** — at <60s remaining, market sell @ 1¢
  unconditionally. Last-line-of-defense.

These are referenced in `08_engine_bugfixes_diffs.md`.

### Afternoon (13:00–17:00 PT)

User identified two systemic weaknesses:

1. **"Catching falling knives"** — engine's maker order rests at high
   limit, gets adverse-selected by retail dumping. Then immediate stop
   fires because mid trigger reads the lower real market.
2. **"Sells the bottom of speculative chop"** — single-tick bid wicks
   from one panicking participant trigger stops. Real moves and noise
   look identical to a bid-touch trigger.

Plus an architectural insight: instead of trying to *check* state every
cycle and react with a sell, we should keep a *resting* protective sell
on Kalshi's book at all times. Kalshi handles the trigger atomically.
No engine cycle, no `get_positions()` call, no cache lag.

That morphed into a 5-component microstructure-gating layer:

| Component | What it does |
|-----------|--------------|
| `KalshiTape` (`kalshi_tape.py`) | Per-ticker millisecond trade tape. WS trade events fed into ring buffer. Provides flow stats (yes_volume / no_volume / share / velocity / deceleration / absorption_score / volume_at_or_below) over arbitrary time windows. |
| `LocalOrderBook.density()` | Top-N depth across price levels. `volume_at_or_below(side, threshold)` — for stop-defer "thick bid" check. `imbalance_ratio()` — for entry-direction sanity. |
| `_evaluate_entry_filter()` | Composite entry gate. Reads tape (adverse flow share, velocity) + book density. Blocks entries that look like knife-catching. |
| `_evaluate_stop_persistence()` | Composite stop gate. Bid touch alone no longer fires stop — requires N seconds of persistence + ≥10ct volume confirmation in window + thick-bid defer if real support exists. Pre-expiry trigger exempt. |
| `_maintain_protective_order()` | Always-resting Kalshi-side sell at TP price (when bid > entry) or SL price (when bid ≤ entry). Re-pegs on state flip / count change with 2s debounce. Pre-expiry < 60s force-flattens @ 1¢. |

Plus a **`gate_decisions` SQLite table** so every decision (pass / block
/ defer / fire) is logged with full input snapshot for post-hoc
correlation against trade outcomes.

### Late afternoon (17:00–18:00 PT)

User flagged that the engine is "burying" the founding philosophy under
~8 layers of composite signal. The Brownian-Bridge probability engine is
still alive but treated as one input among many. Built **Session 1** of
`BB_PURE_MODE`:

- `bb_pure.py` — pure-math `evaluate(market_mid_cents, fair_yes_cents,
  seconds_to_expiry, balance_dollars, config) → BBSignal | None`. Picks
  underpriced side, Kelly-on-edge sizes, filters on entry price band
  and time-to-expiry.
- `tests/test_bb_pure.py` — 22 unit tests pinning down each rule
  including the founding-doc canonical example (`fair=60%, market=80c
  → buy NO at 20c, win_p=0.40, kelly=0.0625`).
- `_evaluate_bb_pure_signal()` method — reads the engine's BB model
  + book + window time, calls `bb_pure.evaluate()`, logs to
  `gate_decisions`.
- All gated by `BB_PURE_MODE` flag, **default OFF**, no behavior change.

### Evening (18:00–18:50 PT)

Discovered the persistence gate had an infinite-loop bug — when the
window settled and bid dropped to 0c, the gate saw bid ≤ trigger,
demanded volume, found 0 (dead book), deferred forever. Engine got
stuck for 16+ minutes thinking it had an open position. Fixed via
`no_bid_data` early return + 5-min `defer_max_exceeded` cap.

Then user said "no fires this session?" — diagnosis: BB model has been
showing strong YES mispricing (+20pp) for the past hour, but the
DOMINANT direction gate refused to fire because BTC was flat. This is
exactly the founding-philosophy divergence — the BB model wants to
trade, the composite cascade refuses without BTC trend confirmation.

User chose option 2: ship Session 2 of BB_PURE_MODE — wire it into the
cascade so it bypasses the composite signal entirely.

Shipped:
- BB_PURE branch added BEFORE SR_FADE in the signal cascade
- `_execute_bb_pure_signal()` method — runs entry-flow gate, places
  marketable order at `best_ask` (capped at `suggested_entry_cents+2c`
  slippage), sets `_open_position` for protective-order mode handoff
- Flipped `BB_PURE_MODE = True`
- Restarted engine

**First BB_PURE trade fired at 18:45:34 PT** — YES 280x @ 45c ($126.00),
edge=53pp, fair=99c, market=46c, quarter-Kelly capped at 15% of
balance. The entry-flow gate even caught earlier attempts on the same
edge (4 BLOCK decisions for adverse_flow_share ≥0.68) before flow
eased and a 5th attempt fired.

---

## Current engine state (as of writing)

### Active flags

```python
PAPER_TRADING                = False
TA_FORCED_ENABLED            = True   # composite tier — fallback when BB has no edge
TA_FORCED_ENTRY_ENABLED      = True
LATE_DOMINANT_ENABLED        = True   # composite tier — fallback
SR_FADE_ENABLED              = False  # retired 2026-04-27
SNIPER_ENABLED               = False  # retired
WALLET_COPY_ENABLED          = False  # disabled
WALLET_COPY_ENGINE_ENABLED   = False  # 2026-04-29 prohibition

# 5 microstructure gates (all active)
KALSHI_TAPE_ENABLED          = True
STOP_REQUIRES_PERSISTENCE    = True
ENTRY_FLOW_GATE_ENABLED      = True
BOOK_DENSITY_GATE_ENABLED    = True
PROTECTIVE_ORDER_MODE        = True

# Founding philosophy (active)
BB_PURE_MODE                 = True

# Schedule
BLOCKED_HOURS = {22, 23, 0, 1, 2, 3, 4, 5, 6}  # ET, auto-DST
```

### Trade lifecycle now

```
[1] Cycle starts → _manage_position()
[2] If _open_position is set:
    → _maintain_protective_order() [PROTECTIVE_ORDER_MODE=True]
       → maintain resting Kalshi sell at TP (entry+5c) when bid>entry
                                       SL (entry-8c) when bid≤entry
       → mark pos["_protective_active"] = True so legacy stops skip
    → bid-check stops run (for cache-lag window where protective hasn't
      taken over yet); persistence gate filters single-tick wicks

[3] Signal cascade runs:
    [3a] BB_PURE branch (BB_PURE_MODE=True):
         → _evaluate_bb_pure_signal() reads BB model + book
         → if mispricing ≥ BB_PURE_MIN_EDGE_PP (8pp), build BBSignal
         → _execute_bb_pure_signal():
             → entry flow gate (BLOCK if adverse flow >60% / velocity bad)
             → book density gate (BLOCK if thin same-side)
             → place_order @ best_ask (capped at slippage)
             → set _open_position with strategy_name="BB_PURE"
             → return early → composite cascade SKIPPED
    [3b] If no BB signal: composite cascade runs
         (SR_FADE→LATE_DOMINANT→TA_FORCED) as before

[4] All gate decisions logged to data/trades.db gate_decisions
[5] Cycle ends; sleep ~0.4s; next cycle
```

### Live position right now (live update — was 280 YES @ 45c → resolved)

**18:45 PT BB_PURE FIRE #1:** `KXBTC15M-26APR302200-00`: 280 YES @ 45c,
$126 exposure. Edge=53pp, fair=99c. Window settled at 22:00 ET = 19:00
PT. Sells at yes_px=0.86 reported at 18:55 PT (before settlement) so
the engine exited at 86c × 280 = $240.80 vs $126.00 entry = **+$114.80
gross.** Account: $937 → $1044.

**19:00 PT BB_PURE FIRE #2:** `KXBTC15M-26APR302215-15`: 200 YES @ 36c,
$72 exposure. Edge=18pp, fair=54c. Currently +$13.32 mark-to-market.

**Account: $1017.69** (cash $988 + portfolio $29). Up **+$95** from
morning baseline ($922.66 → $1017.69) = **+10.3% intraday**.

**Critical observation:** despite no PROTECTIVE log lines on either
BB_PURE position, the first one CLOSED PROFITABLY. Two possibilities:
1. Protective order DID place but logged at a level my grep missed
2. Kalshi auto-settled the position at the favorable side
3. SOMETHING else (maybe the engine's tiered TP placement got placed
   by SYNC RECONCILE and filled when bid touched it)

The handoff doc still flags this as needing investigation. Engine is
profitable but a layer of defense isn't visible in logs.

---

## Known issues

### ⚠️ BB_PURE race condition (FIXED 19:22 PT)

**Symptom:** BB_PURE fired 11 times in 4 seconds on
`KXBTC15M-26APR302230-30`, opening ~1573 contracts when Kelly
sized 143. Account survived (BB was right, YES winning) but exposure
was 10× intended.

**Root cause:** `_execute_bb_pure_signal()` added to
`_entered_tickers_this_window` only AFTER `place_order` returned with
`filled > 0`. During the async await, multiple cycles ran the BB_PURE
evaluator and each saw "not in entered set" → fired.

**Fix:** Lock at function entry, before `place_order`:
```python
try:
    self._entered_tickers_this_window.add(ticker)
except Exception:
    pass
try:
    self._recent_placement_tickers[ticker] = time.time()
except Exception:
    pass
```

If place_order fails, no harm — ticker stays locked for the window
(consistent with single-trade-per-window invariant). Apply same fix
on secondary if you've shipped BB_PURE Session 2 already.

### 🚨 Protective-order mode NOT logging for the live BB_PURE position

After BB_PURE FILL at 18:45:34 placed 280 YES @ 45c, expected
`PROTECTIVE [TP]: ... @ 50c` log within seconds. Two minutes later, **no
PROTECTIVE log lines have appeared.** Position has `resting=0` on Kalshi
— no protective sell rest is on the book.

Possible causes (need to investigate):

1. **`_open_position` cleared between BB_PURE FILL and next manage cycle**
   — unlikely, no `position cleared` log lines after 18:45:34.
2. **`_maintain_protective_order()` returns False silently somewhere**
   — most likely. Suspects:
   - `truth_ct = 0` after cache-lag check (positions API showed
     `position=None` not 0; `int(None)` raises, exception swallowed,
     truth_ct stays at `pos.get("count", 0)` which should be 280)
   - `book.is_ready` returns False
   - `bid <= 0` returns False (we've seen bid=53 in scans)
3. **`place_order()` succeeded but the success log line `PROTECTIVE [TP]`
   isn't reached** — would mean an exception occurred AFTER place_order
   succeeded.

**Next step:** add tracing to `_maintain_protective_order()` so we can
see which code path is taken. Without the resting protective order, the
position relies on the legacy bid-check stops which DO have persistence
gating — so it's not unprotected, just not in the new architecture.

### Composite cascade still active in fallback

When BB_PURE finds no qualifying mispricing (edge < 8pp), the legacy
SR_FADE → LATE_DOMINANT → TA_FORCED cascade still runs. This is
intentional during BB_PURE rollout — gives us a safety net. But it
means the engine isn't 100% on founding philosophy yet.

Plan for Session 4 (after BB_PURE proves out):
- Cull SR_FADE entirely
- Cull SNIPER, ATM_REVERSION_DISCOUNT, DOMINANT mid-trade upgrade
- Possibly cull LATE_DOMINANT (BB model already prices in time decay,
  so a separate "last-3-min carry" tier is redundant)
- Result: cleaner codebase, single source of signal truth

---

## Why the engine is the way it is

The shape of the code reflects the history of how it was debugged.
Every layer responds to a specific incident:

- `MAX_TRADES_PER_WINDOW = 1` → from the 2026-04-22 cancel-race +
  DCA-rebuild incident that drained $172 → $8
- `SAFETY_OVERSELL_HARDENING` → same incident, plus the post-close
  residual reconciler
- `STARTUP RECOVERY` + `_entered_tickers_this_window` → from various
  zombie-state incidents
- All 5 GHOST race patches (#1-5) → from the 2026-04-29 hold-to-expiry
  $30.77 loss case
- All 4 microstructure gates (Phase 1-4) → from 2026-04-30 morning
  hold-to-expiry $15 loss + the "catching falling knives" pattern
- `STOP_REQUIRES_PERSISTENCE` `no_bid_data` early return → from the
  2026-04-30 PM "infinite defer on settled book" 16-min stuck incident
- `BB_PURE_MODE` → from the 2026-04-30 PM realization that the BB
  model has been screaming "buy YES" for an hour but the composite
  cascade refused

Each fix is justified individually but the cumulative system has lost
coherence. BB_PURE_MODE is the cleanup pass — get back to one signal
+ execution-quality filters, drop the rest.

---

## How to think about the engine when debugging

1. **Two layers of defense for any open position:**
   - Protective-order mode: always-resting Kalshi sell (atomic, survives
     engine death)
   - Bid-check stop: cycle-checked (Patch #11 deferred-on-zero handles
     cache lag)

2. **Any entry must pass:**
   - Signal: BB_PURE (mispricing ≥8pp) OR composite cascade
   - Execution gates: entry flow + book density (microstructure quality)
   - Window lock: 1 trade per window
   - Cooldown: `TRADE_COOLDOWN_SECONDS=3600`
   - Daily loss limit: 20% of balance dynamic

3. **Observability:**
   - `data/engine_history.log` for narrative
   - `data/trades.db kalshi_trades` for trade outcomes
   - `data/trades.db gate_decisions` for gate decisions (NEW)
   - `data/trades.db real_balance_snapshots` for ground-truth balance
   - Kalshi positions API (live, no DB)

4. **Common pitfalls:**
   - Don't trust `kalshi_trades.pnl` — use balance snapshots
   - Don't trust `_open_position.count` after restart — use Kalshi truth
   - Don't trust `get_positions()` for 60-180s after fresh fill (cache lag)
   - Don't trust `_recent_placement_tickers` after window rotation

---

## Files added today

| File | Lines | Purpose |
|------|-------|---------|
| `kalshi_tape.py` | 218 | ms-resolution trade tape |
| `bb_pure.py` | 158 | pure BB-mispricing math |
| `tests/test_bb_pure.py` | 211 | 22 unit tests |
| `InterNodesNotes/01_user_config_additions.md` | — | handoff: config knobs |
| `InterNodesNotes/02-04_*.py` | — | handoff: copies of new files |
| `InterNodesNotes/05-09_*.md` | — | handoff: per-file diffs + checklist |
| `InterNodesNotes/REFERENCE_polymarket_copy_engine.py` | 20,703 | full engine source for reference |

## Files modified today

| File | Why |
|------|-----|
| `polymarket_copy_engine.py` | All 5 gates wired, BB_PURE Session 1+2, 5+ patches from earlier today |
| `user_config.py` | ~30 new knobs |
| `kalshi_ws.py` | density helpers added to LocalOrderBook |
| `signal_logger.py` | gate_decisions table + writer |
| `CLAUDE.md` | architectural docs updated |
