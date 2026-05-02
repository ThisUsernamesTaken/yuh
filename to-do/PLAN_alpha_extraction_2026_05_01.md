# Alpha Extraction Plan — 2026-05-01

**Status**: drafted post-reflection on today's session. Engine stopped. BAL $220.02 (+$30.12 day). Day P&L was 100% from user's manual trades; engine net contribution ≈ $0 after the −$38 oversell wiped engine wins.

**Core thesis**: the engine's *wins* look like the user's manual trades. The engine's *losses* come from algorithmic over-confidence — generic fade math fired in conditions where the math doesn't hold. The path forward is **fewer, higher-conviction trades** matching the user's actual edge, not more knobs on the existing fader.

**Operating principle**: nothing ships without (a) data validation against `kalshi_trades` and (b) an explicit feature flag for rollback. We are out of free hot-patches.

---

## Pre-conditions before any phase ships

1. Engine remains stopped until Phase 0 completes.
2. No new entries on the live engine until Phase 0 + Phase 1 land.
3. User retains right to manually trade through any phase.
4. P&L kill-switch: if any single phase produces > $30 net loss in its first live session, **rollback the feature flag** and re-validate.
5. Each phase ships on its own commit. No bundled feature ships.
6. **Sizing reduction is bundled with the first live restart** (see "Sizing reduction" below). The first live test validates Phase 0 safety + Phase 3 MFE trail under reduced size, not pre-restart size.
7. **HARD RULE (added 2026-05-01 23:55 PT, clarified 2026-05-02 00:05 PT): one trade per 15-min window (ticker).** Once a ticker has had any engine fill in the current 15-min window, no further engine entries on that ticker for the rest of the window. **The engine remains continuously active across all windows** — when the window flips, the new ticker is unlocked and trading resumes immediately. The lock is per-ticker (= per-15-min-session), NOT global. Lock must persist across engine restarts so a mid-window restart can't re-enter a ticker we already traded.

   **What this rules out (engine-side):**
   - Same-ticker re-entry on opposite side after a clean exit (tonight's −$5 fade)
   - Pyramiding adds on the same ticker
   - Stop-loss → re-buy → stop-loss cycles within one window

   **What stays allowed:**
   - Engine entries on each new window's new ticker (24/7 operation)
   - User manual trades on any ticker, any time (no engine-side block on user actions)
   - Engine re-pegging the SINGLE protective sell as the trade evolves
   - Engine flattening residual positions on the locked ticker (cleanup is exit-side, not entry-side)

---

## Phase 0.1 — Residual-path safety + per-session lock (NEW, 2026-05-01 23:55 PT)

> **STATUS: BLOCKING all further live restarts.** Live test on 2026-05-01 23:45–23:53 PT
> exposed two gaps that Phase 0 MVP did not cover:
>
> 1. **Residual-flatten path lacks the >1-orders OVERSELL-GUARD.** When the position
>    was already flat after the fade trade crashed, `_reconcile_residual_position`
>    called `_place_capped_side_sell` repeatedly — it has the inventory cap but not
>    the abort-on-many-resting logic. 11 sell orders piled up over 31 seconds. None
>    filled (Kalshi caching may have helped) but it was a near-miss.
> 2. **Same-ticker re-entry was not blocked.** After the YES round-trip closed clean,
>    BB_PURE re-fired on NO (opposite side, same ticker) within 1.5 minutes. Lost $5.
>    The `_entered_tickers_this_window` lock was either cleared by the prior restart
>    or wasn't being respected by the BB_PURE preflight path.

### Implementation (next session, before any live restart)

| Item | Surface | Effort |
|---|---|---|
| `MAX_TRADES_PER_SESSION_TICKER = 1` config knob | `user_config.py` | trivial |
| Check `_entered_tickers_this_window` at every entry placement site BEFORE `place_order` — abort if ticker already in set | `polymarket_copy_engine.py` BB_PURE preflight site | small |
| Add ticker to `_entered_tickers_this_window` on FIRST FILL, not on signal eval (avoids race) | engine fill-event handler | small |
| Persist `_entered_tickers_this_window` across restarts (today's restart cleared the lock) | new — `data/session_state.json` or DB row | medium |
| Extend OVERSELL-GUARD (>1 resting → cancel all + abort) to `_place_capped_side_sell` | `polymarket_copy_engine.py` | small |
| Gate every sell helper on `position_count > 0` — flat = no new sells, ever | `_place_capped_side_sell` | trivial |

### Tests required

- `tests/test_sell_helper.py`: position=0 + repeated sell calls → 0 placements
- `tests/test_sell_helper.py`: 1 resting sell + new placement attempt → cancel-then-place, NEVER pile-up
- `tests/test_sell_helper.py`: 2+ resting sells → cancel all + abort cycle
- `tests/test_session_lock.py` (NEW): same-ticker entry attempt after first fill → blocked
- `tests/test_session_lock.py`: lock persists across simulated process restart

### Verification

- All tests pass
- Paper-mode simulation: force a fade scenario (entry, exit, opposite-side signal) → assert second entry blocked
- Live restart only after: tests pass + paper sim confirms lock + manual review of `_place_capped_side_sell` callers

### Sequencing

This is now the FIRST item in the next coding session. Phases 2/4/5/6/7 stay deferred. Phase 1.5b (BB_PURE attribution write path) can be bundled in this session OR deferred — Codex's discretion.

---

## Status updates (2026-05-01 23:00 PT, post Phase 1 mining)

| Phase | Status | Reason |
|---|---|---|
| Phase 0 | scope tightened to MVP per Codex notes | Sell-helper + inventory cap + cancel-status-confirmed is the smallest cut that prevents today's oversell. Full PositionManager refactor → Phase 0.5 |
| Phase 1 | DONE (read-only mining shipped) | `scripts/alpha_table.py` → `docs/alpha_table_2026_05_01.md` |
| Phase 1.5 | NEW — BB_PURE closure attribution | 42 closed rows are all TA_FORCED. BB_PURE closes aren't being written to `kalshi_trades`. Required before Phase 2 can be validated. |
| Phase 2 | **PAUSED** | Phase 1 found zero engine trades ≤35c. Cheap-side hypothesis was anchored on verbal recall, not data. |
| Phase 3 | **PROMOTED** to first-after-Phase-0 | 10.6% MFE capture is the strongest data signal. Trail bug-class is shared across strategies. |
| Phase 4-7 | unchanged (deferred until Phase 1.5 + 2 settle) | |

---

## Sizing reduction (agreed 2026-05-01 23:00 PT, bundled with next live restart)

The first live test of Phase 0 + Phase 3 ships with materially reduced size. Engine has
no validated alpha right now — restart should be cautious until the new safety + new
trail prove out over 1+ sessions.

| Knob | Current | Next-session value | Rationale |
|---|---|---|---|
| `BB_PURE_KELLY_MAX_FRAC` | 0.30 | **0.10** | Cap any single trade at 10% of bankroll |
| `BB_PURE_KELLY_TIER2_MAX_FRAC` | 0.10 | **0.06** | Inverted ladder (tier 2 = high fair = worse historical perf) |
| `BB_PURE_KELLY_TIER3_MAX_FRAC` | 0.05 | **0.03** | Smallest size on extreme-fair zone |
| `SIZING_HARD_CAP_CONTRACTS_DAY` | 25 | **10** | Hard contract-count floor regardless of math |
| `SIZING_MAX_FRACTION` | 0.20 | **0.08** | Absolute per-position ceiling |

**Effect at current bankroll ($220.02)**:
- Max single trade exposure: ~$66 → **~$22**
- Max contracts at 50c entry: ~132ct → **~44ct**
- Worst-case per-trade drawdown: ~$22
- −$30 phase kill-switch ≈ 14% of bankroll, hits well before damage compounds

**Compounds with Phase 4 theta-aware sizing** (later phase) so late-window trades will
size even smaller. These caps remain in effect until at least 2 stable live sessions
post-restart.

---

## Phase 0 — Architectural debt: centralized order management

**Why first**: today's −$38 oversell was caused by `_maintain_protective_order` and `orphan-flatten watchdog` and `pre-expiry consolidation` and `SCALP DCA` all having different views of the same position state. Until that's fixed, every new feature will eventually hit the same race. **Half-day surgery, but the cost of *not* doing it is another oversell.**

### 0.1 — Single-owner position state

Replace the 8 paths that mutate `_open_position` with a single `PositionManager` class.

**Surface**:
- `polymarket_copy_engine.py` lines that touch `self._open_position` (search: `_open_position[`, `_open_position.get`)
- New: `position_manager.py`

**Owners** (the only paths that may mutate):
- `PositionManager.on_fill(side, count, price, order_id, source)` — single entrypoint for any fill event
- `PositionManager.on_close(reason)` — single entrypoint for any close
- `PositionManager.on_protective_update(order_id, px, count)` — for protective re-pegs
- All read paths use `pm.snapshot()` (immutable view)

**Forbidden**: any other path writing to `_open_position` directly. Enforced by making it a property with setter raising `AssertionError`.

### 0.2 — Cancel-verified state machine for protective orders

Replace the current ad-hoc cancel-then-place / place-then-cancel patterns with a state machine:

```
States: NONE → PLACING → RESTING → CANCELLING → NONE
                            ↓
                          FILLED → (handled by PositionManager.on_fill)
```

Transitions only via `OrderStateMachine.transition(event)`. Cancel verifies via `get_order` status before transitioning to NONE. **No silent cancel failures possible.**

### 0.3 — Lock-free truth poller

Single async task polls Kalshi `/portfolio/positions` and `/portfolio/orders` every 1s. Writes to a thread-safe snapshot. All other paths read from the snapshot, never call Kalshi directly.

Eliminates the cache-lag vs Kalshi-truth races that GHOST patches kept band-aiding.

### 0.4 — Verification

- `tests/test_position_manager.py`: simulate concurrent on_fill, on_protective_update, on_close. Assert no state corruption.
- `tests/test_order_state_machine.py`: simulate cancel-failure, place-failure, race conditions. Assert states are always valid.
- **Live paper**: `PAPER_TRADING=True`, force a synthetic oversell scenario, assert `OVERSELL-DETECTED` log fires AND no double-place.

**Ships**: when both test suites pass + 1 paper session shows zero state-corruption logs.

---

## Phase 1 — Trade data mining (read-only, zero risk)

**Why before more strategy**: we have ~50+ live BB_PURE trades, ~28 commits worth of `gate_decisions`, ~weeks of `kalshi_trades`. Mining the data first answers most of Phase 2-7's "what threshold?" questions empirically rather than by guess.

### 1.1 — Build the honesty table

**New file**: `scripts/alpha_table.py`

For every closed trade in `kalshi_trades`, compute and bucket:

| Bucket dimension | Buckets |
|---|---|
| Entry price | 0-25c, 26-35c, 36-50c, 51-65c, 66-80c |
| Strike distance ($BTC - strike at fire) | <$10, $10-25, $25-50, $50-100, >$100 |
| Minute of window at fire | 0-2, 3-5, 6-8, 9-11, 12-14 |
| Regime at fire | STRUCTURED, CHOP, CHAOTIC |
| Realized vol bucket | <15bps, 15-25, 25-35, >35 |
| Day-of-week × hour-of-day | 24×7 grid |
| Conviction tier | T1, T2, T3 (today's INVERTED ladder) |

**Output**: per-cell stats (count, hit rate, avg P&L per trade, expected value).

**Decision rule**: a cell ships into the entry filter only if **count ≥ 8 AND hit rate ≥ 60% AND avg P&L > +$0.50**. Cells with count ≥ 8 AND hit rate ≤ 45% become **block-list** entries.

### 1.2 — MFE/MAE analysis

For every closed trade, reconstruct the in-position price track from `kalshi_ws` and compute:
- **MFE** (max favorable excursion in cents)
- **MAE** (max adverse excursion in cents)
- **Time-to-MFE** (when did the rip happen)

**Decision target**: how much of MFE did our exit capture? If median capture ratio < 60%, the trail is leaving real money. This calibrates Phase 3.

### 1.3 — "What if" backtests

For each candidate Phase 2-7 rule:
- Apply the rule retroactively to historical trades
- Compute counterfactual P&L (excluded losses + retained wins − retained losses)
- Output: rule-by-rule expected $ delta if applied to last N days

Doesn't prove forward, but disqualifies rules that didn't help in retrospect.

### 1.4 — Verification

- `python scripts/alpha_table.py --since 2026-04-29 > docs/alpha_table_2026_05_01.md`
- User reviews the table.
- Ships ≥ 1 phase ONLY for cells that survived the table.

---

## Phase 1.5 — BB_PURE closed-trade attribution

> **STATUS (2026-05-01 23:00 PT): NEW.** Phase 1 found that all 42 closed engine rows in
> `kalshi_trades` are `TA_FORCED_SIGNAL`. BB_PURE closes aren't being written natively.
> Required before Phase 2 can ever be validated against data.

Two sub-paths, can run in parallel:

### 1.5a — Read-only log-mining (no code changes, ships before Phase 0)

**New file**: `scripts/bb_pure_log_mine.py`

Parse `data/engine_history.log` for these event prefixes and reconstruct trade pairs by
ticker + time-window:
- `BB_PURE FILL` (entry)
- `SELL TIER FILLED` / `TP_FILLED` (exit)
- `TRAIL FIRED` (exit)
- `RESIDUAL-CLEAN` (exit / position close)
- `MANUAL_TP` (manual user exit)
- `OVERSELL-DETECTED` / `STUCK-RESIDUAL` (anomaly markers)

Output: a synthetic dataframe matching `kalshi_trades` schema. Re-run `alpha_table.py`
against the reconstruction. **This is read-only, ships in any session, and unblocks
Phase 2 calibration.**

### 1.5b — Native attribution fix (gated on Phase 0)

Fix the `kalshi_trades` write path so future BB_PURE closures are attributed natively.
Bundled with Phase 0 surgery because it requires touching `polymarket_copy_engine.py`
close-handling code. Engine restart needed to validate.

### Verification

- 1.5a: produces a markdown report with ≥ 30 reconstructed BB_PURE round trips, MFE/MAE
  fields populated where log data permits.
- 1.5b: 1 paper session post-Phase-0 produces a `kalshi_trades` row with
  `strategy_name="BB_PURE_SIGNAL"` for every closed BB_PURE position.

---

## Phase 2 — Cheap-side primary (highest-conviction change)

> **STATUS (2026-05-01 23:00 PT): PAUSED.** Phase 1 alpha-table mining found **zero engine
> trades ≤35c** in `kalshi_trades` since 2026-04-29. The hypothesis below was anchored on
> verbal recall of today's BB_PURE wins (26c, 30c, 32c entries) — but those trades aren't
> in the closed-trade DB. Cannot validate against data until Phase 1.5 (BB_PURE closure
> attribution) lands and the alpha-table is re-run on real BB_PURE data. **Do not ship.**

**Hypothesis**: the asymmetric payoff math says cheap entries dominate. Today's wins were 26c, 30c, 32c entries; today's catastrophic losses were 55c-65c entries. **Same edge ≠ same trade** — a 25c entry pays 3× upside vs a 75c entry. Only fire when the asymmetric payoff favors us.

### 2.1 — Implementation

**Surface**: `bb_pure.py` (entry math) + `user_config.py`

```python
# user_config.py
BB_PURE_CHEAP_ENTRY_MAX_CENTS  = 35    # only fire below this entry
BB_PURE_MODERATE_BLOCK_ENABLED = True  # block 36-65c entries entirely
BB_PURE_EXPENSIVE_BLOCK_CENTS  = 70    # already exists; keep
```

In `bb_pure.evaluate()` after side selection:
```python
if entry_cents > BB_PURE_CHEAP_ENTRY_MAX_CENTS:
    if BB_PURE_MODERATE_BLOCK_ENABLED:
        return None  # block moderate entries entirely
```

### 2.2 — Sizing on cheap entries

Cheap entries are where Kelly should size **larger**, not smaller:
```python
# user_config.py
BB_PURE_CHEAP_KELLY_MULT = 1.5    # multiply Kelly on entries < 25c
```

Compounds with existing tier caps. Hard ceiling remains `BB_PURE_KELLY_MAX_FRAC = 0.30`.

### 2.3 — Verification

- `tests/test_bb_pure_cheap_only.py`: feed signals at 30c, 45c, 65c. Assert only 30c fires when block enabled.
- **Phase 1 backtest**: rule must show ≥ +$15 expected P&L delta on last 5 days of trades.
- **Live cautious**: 1 day of fires only when block enabled. Compare hit rate vs prior baseline.

### 2.4 — Risk notes

- Reduces fire rate substantially. Could be 40-60% fewer trades.
- Acceptable: today's clean wins were exactly these cheap-side trades.
- If fires drop too low, raise `BB_PURE_CHEAP_ENTRY_MAX_CENTS` to 40 not 35.

---

## Phase 3 — MFE-aware trail (capture rip)

> **STATUS (2026-05-01 23:00 PT): PROMOTED to first-after-Phase-0.** Phase 1 mining showed
> 10.6% average MFE capture ratio on positive trades — strongest data signal surfaced. Even
> though the data is mostly TA_FORCED-flavored, the trail bug-class is shared because both
> strategies route exits through `_maintain_protective_order`. **Ship next session bundled
> with Phase 0 MVP and the sizing reduction.**

**Hypothesis**: the current `BB_PURE_TRAIL_DISTANCE_C = 3` flat trail misses 30-50% of the median MFE. User's manual exits today caught more upside than engine TP because user trailed harder when the rip was real.

### 3.1 — Implementation

**Surface**: `polymarket_copy_engine.py` `_maintain_protective_order` BB_PURE branch.

Track `pos["_bb_pure_mfe_cents"]` updated each cycle:
```python
mfe_now = max(pos.get("_bb_pure_mfe_cents", 0), bid - entry)
pos["_bb_pure_mfe_cents"] = mfe_now
```

When `mfe_now > BB_PURE_MFE_TRAIL_THRESHOLD_C` (default 8c):
```python
trail_distance = max(
    BB_PURE_TRAIL_DISTANCE_C,
    int(BB_PURE_MFE_TRAIL_RATIO * mfe_now)
)
trail_floor = max(entry + BB_PURE_TP_MIN_CENTS, bid - trail_distance)
```

`BB_PURE_MFE_TRAIL_RATIO = 0.4` locks 60% of MFE.

### 3.2 — Verification

- `tests/test_mfe_trail.py`: simulate a price track entry=30, peaks at 50, retraces to 35. Assert exit ≥ 42 (≥ 60% of MFE locked).
- **Phase 1 backtest**: rule must show ≥ +$10 expected P&L delta vs current flat trail.
- **Live cautious**: 5 trades with new trail. Compare median capture ratio vs old.

### 3.3 — Risk notes

- Trail-only change, doesn't affect entry. Safer than Phase 2.
- Could over-stay if MFE spikes then reverts hard. Mitigation: hard floor at `entry + BB_PURE_TP_MIN_CENTS` so we never go below initial profit lock.

---

## Phase 4 — Theta-aware sizing

**Hypothesis**: theta decay is convex near expiry. A 5-min-remaining trade should size smaller than a 13-min-remaining trade for the same edge. Today's −$36 and −$56 losses both fired with < 7 min left.

### 4.1 — Implementation

**Surface**: `bb_pure.evaluate()` post-Kelly.

```python
# user_config.py
BB_PURE_THETA_SIZING_ENABLED = True
BB_PURE_THETA_SIZING_EXPONENT = 0.5  # square-root scaling

# bb_pure.py
if BB_PURE_THETA_SIZING_ENABLED:
    theta_factor = (seconds_to_expiry / 900.0) ** BB_PURE_THETA_SIZING_EXPONENT
    fractional_kelly *= theta_factor
```

Composes with existing Kelly cap.

### 4.2 — Verification

- `tests/test_theta_sizing.py`: same edge, different time-to-expiry. Assert larger size at fresh window vs late.
- **Phase 1 backtest**: rule must reduce loss magnitude on late-window losers without killing win rate.

### 4.3 — Risk notes

- Reduces size at exactly the moments where edge is most uncertain. Safe.
- Alternative composition: don't fire at all if theta_factor < 0.6. Stricter, safer.

---

## Phase 5 — Strike-distance × time gate

**Hypothesis**: when |$BTC − strike| × seconds_remaining is small, the contract is effectively settled. Trading there is theta erosion only.

### 5.1 — Implementation

**Surface**: `bb_pure.evaluate()` early gate.

```python
# user_config.py
BB_PURE_SETTLED_BLOCK_THRESHOLD = 30000  # cents × seconds product

# bb_pure.py
strike_dist_dollars = abs(btc_price_dollars - strike_price)
strike_dist_cents = int(strike_dist_dollars * 100)
settled_score = strike_dist_cents * seconds_to_expiry
if settled_score < BB_PURE_SETTLED_BLOCK_THRESHOLD:
    return None  # contract effectively settled
```

Calibrate threshold from Phase 1 data.

### 5.2 — Verification

- Phase 1 backtest must show net positive on losers eliminated.

---

## Phase 6 — Volatility regime asymmetry

**Hypothesis**: current `BB_PURE_BTC_STABILITY_MAX_RANGE = 30` is symmetric. Mean-reversion fades require asymmetric vol: stricter when fading *with* prevailing direction, looser when fading *against* it.

### 6.1 — Implementation

**Surface**: `polymarket_copy_engine.py` BB_PURE evaluation gate (where stability is checked).

```python
# user_config.py
BB_PURE_VOL_STRICT_WITH_TREND = 20  # tight vol cap when fading with-trend
BB_PURE_VOL_LOOSE_AGAINST_TREND = 40  # looser cap when fading against
```

Determine "with-trend" via 5-min BTC return sign vs fade direction.

### 6.2 — Verification

- Phase 1 mining must show that "with-trend at high vol" is a loser bucket and "against-trend at high vol" is a winner bucket. If not, skip Phase 6.

---

## Phase 7 — Time-of-day filter (if data supports)

**Hypothesis**: low-volume hours (00-04 UTC) have unreliable BB calibration → poor edge.

### 7.1 — Implementation

Only ships if Phase 1 data shows specific hour buckets with hit rate < 45% AND count ≥ 10.

```python
# user_config.py
BB_PURE_HOUR_BLOCKLIST_UTC = [0, 1, 2, 3]  # filled from Phase 1
```

### 7.2 — Verification

- Phase 1 must produce specific hours, not guessed.

---

## Sequencing & gates (revised 2026-05-01 23:00 PT)

```
Phase 1 (data mining, READ-ONLY) ── DONE 2026-05-01
    ↓ alpha_table.md reviewed
    ↓ Findings: Phase 2 hypothesis NOT data-backed; MFE capture 10.6%

Phase 1.5a (BB_PURE log-mining) ← READ-ONLY, can ship anytime
    ↓ produces real BB_PURE trade data for Phase 2 calibration

NEXT CODING SESSION (single live restart at end):
    Phase 0 MVP (sell-helper + inventory cap + cancel-verified)
        ↓ tests pass
    Phase 3 (MFE-aware trail) ← PROMOTED, has data backing
        ↓ tests pass
    Sizing reduction (BB_PURE_KELLY_MAX_FRAC = 0.10, etc.)
        ↓ live restart with all three + −$30 kill-switch
        ↓ 1 live session live-stable

Phase 1.5b (native kalshi_trades attribution) ← bundled in Phase 0 surgery

Phase 2 (cheap-side primary) ← UNPAUSE only if Phase 1.5 data validates the cell
    ↓ 1 live session, ≥ 5 fires, no kill-switch trip

Phase 4 (theta sizing) ← only after Phase 3 stabilizes
    ↓ same gate
Phase 5 (settled gate) ← only if Phase 1.5 data shows the cell
    ↓
Phase 6 (vol asymmetry) ← only if data shows the asymmetry
    ↓
Phase 7 (time-of-day) ← only if data names specific hours
```

**Hard rules**:
- No phase ships if its predecessor isn't 1 session live-stable
- Any kill-switch trip → all subsequent phases freeze pending review
- Phase 0 MVP is non-negotiable — no entry features ship until sell-helper + inventory cap lands
- Phase 2 stays paused until Phase 1.5 produces real BB_PURE closure data
- Sizing reduction stays in effect for ≥ 2 stable live sessions before any cap is raised

---

## Files to create / modify

| Phase | Path | Type | Role |
|---|---|---|---|
| 0 MVP | `polymarket_copy_engine.py` | edit | Single sell-helper + inventory cap + cancel-status-confirmed (Codex MVP scope) |
| 0 MVP | `tests/test_sell_helper.py` | NEW | Cancel-failure / manual-fill / async-fill / shutdown-restart cases |
| 0.5 | `position_manager.py` | NEW | Full single-owner refactor (deferred, post-MVP) |
| 0.5 | `order_state_machine.py` | NEW | Cancel-verified order lifecycle (deferred) |
| 1 | `scripts/alpha_table.py` | DONE | ✓ Shipped 2026-05-01 |
| 1 | `docs/alpha_table_2026_05_01.md` | DONE | ✓ Shipped 2026-05-01 |
| 1.5a | `scripts/bb_pure_log_mine.py` | NEW | Read-only BB_PURE trade reconstruction from engine log |
| 1.5a | `docs/bb_pure_trades_2026_05_01.md` | NEW | Output of log mining |
| 1.5b | `polymarket_copy_engine.py` | edit | Native BB_PURE close → `kalshi_trades` write (bundled with Phase 0) |
| 2 | `bb_pure.py` | edit | Cheap-only gate + cheap Kelly mult (PAUSED) |
| 2 | `tests/test_bb_pure_cheap_only.py` | NEW | Gate test (PAUSED) |
| 3 | `polymarket_copy_engine.py` | edit | MFE tracking + dynamic trail distance |
| 3 | `tests/test_mfe_trail.py` | NEW | Trail behavior test |
| 3 | `user_config.py` | edit | `BB_PURE_MFE_TRAIL_THRESHOLD_C`, `BB_PURE_MFE_TRAIL_RATIO` |
| Sizing | `user_config.py` | edit | `BB_PURE_KELLY_MAX_FRAC=0.10`, tier caps, `SIZING_HARD_CAP_CONTRACTS_DAY=10`, `SIZING_MAX_FRACTION=0.08` |
| 4 | `bb_pure.py` | edit | Theta-aware sizing (deferred) |
| 5 | `bb_pure.py` | edit | Strike-distance × time gate (deferred) |
| 6 | `polymarket_copy_engine.py` | edit | Asymmetric vol gate (deferred) |
| 7 | `bb_pure.py` | edit | Hour blocklist (deferred) |

---

## Explicit non-goals

- **Not adding new entry tiers**. BB_PURE only.
- **Not rewriting the BB model**. The Brownian-Bridge math is sound.
- **Not adding new strategies** (sniper, wallet copy, OU fade, capitulation). Plan-deck previously called for some of these — they stay frozen.
- **Not shipping any feature without Phase 1 validation**.
- **Not raising the per-position bankroll cap** above current 30%.
- **Not running fast-iteration ship-on-live without paper smoke**. The 28-commits-in-3-days pattern stops here.

---

## Risk notes

- **Phase 0 is the highest-effort phase by far**. Real risk is it takes 1.5 days, not the half-day estimate. If so, it still ships first — every alternative compounds the architectural debt.
- **Phase 2 may reduce fire rate to < 5/day**. That's acceptable. Quality > quantity. If fires drop to < 2/day for 3 days, raise the cheap threshold.
- **Phases 3-7 each have small expected $ deltas individually** (~+$10-20/day each). The combined uplift matters more than any single one.
- **Phase 1 mining may surface that BB_PURE itself underperforms** vs a simpler heuristic. If so, this plan is wrong and we pivot. Better to discover that on read-only data than after another live session.

---

## Success metrics

- **30-day P&L from $189.90 floor** (post-withdrawal baseline) ≥ +$200 net **with engine doing > 50% of the work**, not user manual trades carrying.
- **Catastrophic loss rate** (single-trade loss > $30): < 1 per 50 trades.
- **Engine-vs-manual P&L attribution**: engine attribution > 70% of total. Today was 0%.
- **Architecture metrics**: zero `OVERSELL-DETECTED`, zero `STUCK-RESIDUAL`, zero `REENTRY-BLOCK` logs across any 7-day window.

---

## Decision rules going forward

1. **Don't ship a phase without Phase 1 data backing it** — even if it sounds right.
2. **Don't fix-on-fix** — if a phase has a bug, roll it back via flag, then fix from clean state.
3. **Don't bundle phases** — one feature, one commit, one verification cycle.
4. **Don't trust truth-monitor labels** — when in doubt, query Kalshi positions API directly.
5. **Don't restart engine after a loss without a written hypothesis** for what changed.
6. **The engine doesn't ship today**. Period.

---

## Today's open items (carried forward)

| Item | Phase |
|---|---|
| `_entered_tickers_this_window` lock has known edge cases | Phase 0 (PositionManager handles this) |
| Centralized order management — "half-day surgery" | Phase 0 |
| Maker bid+1 entry option | Defer to Phase 8 (post-validation) |
| MFE-aware trail | Phase 3 |
| Cheap-side primary | Phase 2 |
| Same-ticker re-entry block | Phase 0 (PositionManager handles this) |
| Truth monitor false-alarms | Communication discipline, not a code fix |
---

## Codex implementation notes - 2026-05-01

This section converts the reflection into execution constraints for the next coding session.

### Confirmed local module map

- `bb_pure.py` exists and is the correct surface for BB_PURE entry math and sizing rules.
- `polymarket_copy_engine.py` remains the live orchestrator and still directly references `_open_position` in many places.
- `user_config.py` remains the correct feature-flag surface.
- `BTCBiasEngine` was confirmed stopped before this plan was reviewed.

### Critical correction to sequencing

Phase 1 is read-only and can run before or during Phase 0. The live engine must stay stopped, but data mining should not wait on the refactor. Recommended next-session order:

1. Run Phase 1 mining first to establish the entry/exit truth table.
2. Keep engine stopped while Phase 0 is implemented.
3. Use the Phase 1 output to decide whether Phase 2 and later are worth implementing.
4. Do not restart live until Phase 0 tests pass and the live config is explicitly reviewed.

This avoids doing a large order-management refactor while still guessing about the alpha filters.

### Minimum safe Phase 0 scope

Phase 0 should not attempt to rewrite every engine path in one pass. The minimum viable safety boundary is:

1. Add one authoritative inventory snapshot object for the active ticker/side.
2. Route all live sell placement through a single helper, even if `_open_position` is still present internally.
3. The helper must cap sell quantity to verified available inventory minus already-resting sell quantity.
4. No protective order replacement may place a new sell before cancel status is confirmed, unless the old order is proven filled or expired.
5. Manual fills must update the same inventory snapshot before any automatic TP/protective order is placed.

If the full `PositionManager` takes longer than expected, this boundary is the fallback. It targets the actual oversell failure mode first.

### Required tests before restart

- Cancel failure followed by replacement attempt: assert no second live sell is placed.
- Manual buy increases position while engine already has a protective sell: assert one resized exit order, not two independent exits.
- Manual sell reduces position while protective sell is resting: assert sell cap shrinks and stale order is cancelled or left harmless.
- Async fill after cancel request: assert state transitions to filled/closed, not cancelled/none.
- Shutdown with resting protective order: assert the preserved order is included in resting sell quantity on next boot.

### Implementation stop conditions

Stop and ask for review if any of these appear:

- More than two live order paths still bypass the centralized sell helper.
- A test requires mocking away the Kalshi order status response instead of modeling it.
- A fix depends on sleeping/retrying rather than an explicit state transition.
- The refactor touches strategy entry logic and order-management logic in the same commit.
- Any live restart is needed to verify correctness.

### Data-mining priority

The first script should answer only three questions:

1. Which entry price buckets have positive expectancy after fees?
2. Which session-minute buckets produce catastrophic losses?
3. What percentage of MFE does the current exit logic capture?

Do not build the full 7-dimensional alpha table first if it delays these answers. The first useful output is a compact table that can reject bad entry zones and calibrate the MFE trail.
