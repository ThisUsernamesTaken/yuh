# Decision: safety + ops overhaul (DCA hardening, manual-trade capture, alpha pipeline, OPS dashboard)

**Date:** 2026-04-28
**Status:** final (retroactive — captures the day's work in one record)
**Author:** Claude (sole agent; Codex unavailable)

## Why this record

A single conversation produced ~12 distinct landed changes across safety, alpha,
operability, and portability. Without one consolidated note, future me / Codex
will have to reconstruct the rationale from log-line archaeology. This is the
record.

## Context

The day opened in a hole. The user's running balance went $1,095 → $840
(drawdown) → $1,217 (recovery) — **net +$117** but only because they
intervened manually at the bottom. The engine itself held the bag during the
$255 drawdown leg. Two failure modes drove it:

1. **GHOST/SCALP DCA accumulation.** Engine logged 9ct, Kalshi truth was 71ct.
   The DCA path retried place_order on partial fills and the retries
   accumulated rather than replacing. Already-resting partials sat un-cancelled
   while new attempts stacked.
2. **Cancel-then-place oversell race.** A SWEEP placed at 12:51:20.484 was
   followed by an ORPHAN CATCH at 12:51:20.543 (60 ms later). The catch read a
   stale Kalshi positions API response, saw a residual that no longer existed,
   and double-sold — leaving us short 4ct on a YES contract. Settlement
   produced +$0.28 with no balance impact (the orphan order was likely
   rejected) but the failure mode is a timed bomb.

After the day's fix-up, the user's directive was: *"Glad I can do this solo.
Now to use algos to simply accumulate when I'm not here."* That's the
North Star — algorithmic accumulation overnight, with explicit manual-trade
respect during the day.

The user then pushed for **alpha-pipeline expansion** ("A, B, C, D all sound
like they're providing massive alpha") and **operations work** ("get the
terminal monitor updated… as a primary engine dashboard"). Those rolled into
the same session.

## Decisions

### Safety — DCA + oversell hardening

1. **GHOST/SCALP DCA single-shot.** `_scalp_dca_fired = True` is stamped
   *immediately* after place_order, not after fill confirmation. Cancels any
   resting limit on unfilled remainder. Eliminates the retry-stack failure
   mode at its root.
2. **Bounded close sweep replaces ORPHAN CATCH.** The new logic:
   cancels resting orders → polls Kalshi truth in a bounded loop → caps total
   sells at `original_count` (entry size) → tolerates residual budget so we
   don't double-sell into a stale read. Replaces the 60ms-race-vulnerable
   poll-then-sell pattern.
3. **Manual-detection gates on the safety reconciler.** Three gates prevent
   the reconciler from flattening user-placed positions:
   - size > 150ct → likely manual (engine never sizes that high)
   - ticker untouched by engine this window → manual
   - Kalshi count > 1.5× engine's recent fill → manual

### Manual trade capture (primary user alpha)

User's manual trades are the strongest signal we have. We need to see what
state the engine was in when the user found a profitable entry.

- `manual_fills` table writes on every detected user fill via
  `_snapshot_engine_state()`: BTC price + 5/30/300s velocities, ms-tick
  velocity (100/250/500ms windows), book state, FVG, regime, drawdown,
  pressure score, session age.
- Auto-place limit-sell TP at +10% of capital deployed on every manual buy.
  User said: *"all I'm really looking for is 10% of what I enter with."*
- Kalshi field naming gotcha: fills API uses `count_fp` (fixed-point
  decimal string) and `*_price_dollars`, not `count` / `*_price`. ISO
  timestamps for `created_time`. Parser tries both shapes for forward
  compat.

### Alpha pipeline (A/B/C/D)

User authorized for live trading today:

- **A) Cross-side arbitrage detector.** Polls `best_yes_ask + best_no_ask`
  every 0.5s; fires when sum < 97c (after fees ≈ 95c net edge). Hedge-buys
  both sides at ARB_FRACTION_OF_BALANCE (10%, capped 200ct). Emergency
  unwind on partial-fill imbalance.
- **B) Microprice limit entries.** Uses
  `(bid·ask_size + ask·bid_size)/(bid_size+ask_size)` to refine maker bid
  pricing. Capped at `ask−1` so we never accidentally cross the spread.
- **C) Wall consumption signal.** Per-ticker depth ring buffer; classifies
  AGGRESSIVE_BUY when top-3 ask shrinks ≥30 ct/s. Wired into TA_FORCED
  decision logic.
- **D) Kalshi tape POC.** Already in via `_kalshi_trades` deque + tape
  state — we expose it as a separate signal axis rather than only using it
  for pressure.

### Maker→Taker escalation (#6, ms-tick velocity)

Maker entries that don't fill within 5s timeout get escalated to taker.
Original implementation only timed-out passively. Added two fast-path
triggers for adverse motion:

- **Book-move:** if quote moves > 3c against our maker price.
- **ms-tick velocity:** if BTC tick-tracker reports adverse 500ms velocity
  ≥ $25/s. Polls every 200ms during the timeout window.

User's verbatim request: *"tick velocity at millisecond time frame."*
Implementation reuses `BTCTickTracker._prices` deque; added
`tick_velocity_ms_100/250/500` properties to `price_feed.py`. Threshold
configurable via `MAKER_TAKER_MS_VEL_THRESHOLD`.

### Day/night sizing + TA_FORCED stop loss

User: *"8c makes sense especially because we're bidding into entry, it's
been performing excellent we just needs to limit how much it trades to what
it currently has as caps between 9 PM and 8 AM."*

- Day window (8 AM–9 PM PT): 8% of balance, max 50ct.
- Night window: 5% of balance, max 30ct.
- Implemented in `_get_sizing_cap()` + new `_is_day_hours()` helper
  reading `TA_FORCED_DAY_HOURS_START/END` (8/21).
- `TA_FORCED_STOP_ENABLED = True`, `TA_FORCED_STOP_CENTS = 8`. Mirrors
  the existing `LATE_DOMINANT_STOP` mechanism.

### Portability (F:\\)

User wants drag-and-drop deployment to a separate Kalshi account on a
different machine. `F:\\btc-bias-engine` was prepped:

- `setup.ps1` (ASCII-clean PS5.1; em-dashes crash the parser).
- `.env.template` with all required env vars.
- `PORTABLE_SETUP.md` step-by-step guide.
- `credentials/README.txt` placeholder for the API PEM.
- robocopy excludes `venv/` to avoid copying the local Python env.

### OPS dashboard (monitor.py)

New 4th view "OPS" (key `o`). Shows:

- Today's per-tier P&L breakdown (n / ct / open / WR / pnl).
- Alpha feature firing status (ARB DETECTED, MICROPRICE-BID, MANUAL TP
  PLACED, BOUNDED CLOSE) over the last 2MB of `engine_history.log`.
- Recent events feed.

Help overlay (`h` or `?`) with key bindings. View cycle: positions →
tape → flow → OPS. Refresh key `r`.

### OPS data integrity (#7 — fixed twice)

The OPS view's `_today_perf_breakdown` had three latent bugs the user
would never have caught without ground-truth checking:

1. **`kalshi_trades.pnl` is unreliable.** Lifetime drift > $1,000.
   Authoritative source is `settlement_ledger.pnl_cents`. View now joins
   engine fills against settlement truth and falls back to "open" only for
   not-yet-settled tickers.
2. **ISO timestamp string-compare bug.** `placed_at` is stored as
   `2026-04-28T00:22:44.247097+00:00` while SQLite's `datetime('now',...)`
   returns `2026-04-28 16:00:00`. Lexicographically `T` (84) > ` ` (32),
   so the cutoff was effectively bypassed for any T-stamped row whose
   date matched. Fix: wrap in `datetime(placed_at)` for proper parse.
3. **PT-day window math.** Original `'-8 hours'` resolved to 16:00 UTC,
   not the 07:00 UTC PT-midnight we wanted. Fix:
   `'-7 hours','start of day','+7 hours'` — shifts now to PT, takes
   start of PT day, shifts the result back to its UTC representation.
4. **`filled_count` is NULL until settlement.** The schema's
   `filled_count` only gets populated by `log_kalshi_outcome` when a
   settlement reconciliation runs. The INSERT path stores
   `count = order.filled_count`. Fix: filter on `count > 0`, alias to
   `filled_count` so downstream code is unchanged.

After fixes, the view correctly attributes today's $37.14 net to
TA_FORCED tier (10 fills, 6W/3L, 66.7% WR, 1 still open).

### ARB observability (#9)

Detector started 2 times today (engine restarts) but fired 0 times. The
existing implementation was *silent* on every rejection path — we had no
log-line evidence whether sums were near-threshold (96c, missed on speed)
or far-from-threshold (101c, no opportunity at all).

Added two debounced INFO log lines:

- `ARB SCAN: yes_ask=Xc no_ask=Yc sum=Zc threshold=97c gap=+Nc` — every
  30s if within 5c of threshold ("near miss"), every 5min otherwise.
- `ARB SHALLOW: ... yes_depth=N no_depth=M (need 5 each) — skipped` —
  fires when sum is below threshold but liquidity insufficient. Lets us
  distinguish "no opportunities" from "opportunities we can't take".

This is **observability only** — no behavioral change. Tonight's data
will tell us whether to (a) tighten threshold, (b) reduce poll interval,
(c) adjust depth gate, or (d) accept ARB as low-frequency by design.

## Verification

- 295/295 tests pass (`tests/test_bias_engine.py`,
  `tests/test_consensus.py`, `tests/test_phase3.py`,
  `tests/test_strategy_index.py` excluded — pre-existing import failures
  for renamed/deleted modules, not regressions from this work).
- Live engine fired `TA_FORCED FILL: NO 11x @ 54c ($5.94)` at
  17:17:32 PT after these changes shipped; bounded close sweep flagged
  `RESIDUAL-CLEAN` 18s later. No GHOST/SYNC RECONCILE, no
  OVERSELL-DETECTED.
- New OPS view returns realistic numbers vs. the previous "all open / $0"
  output.

## Open questions / next steps

1. **ARB observability data.** After 24h of ARB SCAN logs, decide whether
   to tighten threshold or accept the strategy is structurally rare.
2. **Manual-fill pattern analysis.** With 2 manual fills captured tonight
   and `_snapshot_engine_state()` running, schedule a 1-week study to
   identify which engine-state features predict the user's edge. The
   user said this is *"my primary alpha"* — the manual_fills table is
   the highest-value research input we have.
3. **Maker→Taker escalation tuning.** ms-velocity threshold at $25/s is
   a guess. Watch the next 50 entries for false-positive escalations
   (we taker'd into the spread but the move was noise) vs true positives
   (we caught a real adverse motion before paying).
4. **Day/night cap is binary.** Could be smoothed across the 7→9 AM and
   8→10 PM transition windows. Defer until we see if the binary edge
   produces visible artifacts.

## Risk notes

- Bounded close sweep pays taker fee + spread on emergencies. This is
  the correct trade-off vs. unbounded short risk.
- Manual TP at 10% deployed assumes the user's manual entries are
  directionally correct. If a manual entry goes south, the TP becomes
  a stop-loss-of-zero (resting at +10% never hit, position rides to
  expiry). User accepted this trade-off explicitly.
- ARB observability adds two log lines per scan cycle (debounced).
  Negligible disk impact (~50 KB/day) but watch for log-rotation
  pressure if the scan rate is raised.
- OPS view's settlement_ledger join silently shows `0` pnl for any
  ticker that's been entered but not yet settled. This is correct
  behavior (we don't know the outcome yet) but reads as "engine is
  losing money" until the settlement runs. Document this in the OPS
  view UI eventually.
