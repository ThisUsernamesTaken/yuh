# RESEARCH — Inactive Strategy Components for Dynamic Exit Management — Do Not Implement

**Date**: 2026-05-04
**Scope**: BB_PURE exit management (TP, SL, hold-to-expiry decisions in `_maintain_protective_order` and `_manage_position`)
**Status**: Research only. Findings are *not* recommendations to enable any disabled strategy. The question is whether *fragments* of disabled strategies expose data or computations that could enhance BB_PURE's exit decisions.

---

## Executive Summary

BB_PURE's current exit stack is well-engineered but **monocausal** — every exit decision derives from price/edge math (entry vs. fair, MFE trail, BTC velocity, expiry). The disabled strategies contain four classes of orthogonal information that are **already being computed** (or cheaply computable from already-running data) and could materially improve exit decisions:

| # | Component | What it adds | TP | SL | Hold | Effort | Data ready? |
|---|-----------|-------------|----|----|------|--------|-------------|
| 1 | **Contract S/R levels** (`contract_sr.py`) | Empirical resistance/support on the contract mid | **HIGH** | MED | MED | EASY | **YES** — `contract_sr.update()` runs every cycle (engine line 7454-7463) |
| 2 | **Shadow-edge composite** (`shadow_edge.py`) | Signed [-100,+100] thesis score combining BB+pressure+MTF+RSI+BTC | MED | **HIGH** | **HIGH** | EASY | YES — `SHADOW_EDGE_ENABLED=True`, data-only |
| 3 | **MarketPressure microstructure** (`microstructure.py`) | btc_impulse / book / flow / kalshi_lag composite, with persistence | MED | **HIGH** | **HIGH** | EASY | YES — `self._microstructure.last_score` available every cycle |
| 4 | **Wall consumption** (engine method `_detect_wall_consumption`) | Real-time ask-stack consumption rate per side | **HIGH** | **HIGH** | **HIGH** | EASY | YES — already a method on engine |

These four are the **highest value × lowest effort** wins. They produce *signed* signals on a consistent scale and they are already running. Wiring them into the exit path is a matter of reading values that the engine throws away today.

A second tier (LATE_DOMINANT confidence composite, MRC's already-exposed `path_signature`/`should_force_exit`/`micro_alignment`, Phase 8b tape-exit-pressure) provides additional alpha but either requires light code lift or is already wired (Phase 8b — just flip a flag).

A third tier (TA inversion, MTF confluence, atm_reversion exit logic, sniper, wallet-flow tiers, whale_monitor) are either redundant with already-active checks or carry significant integration cost relative to their incremental signal.

---

## 1. Inventory of Disabled / Inactive Components

### 1.1 Strategy evaluators (entry-side only — disabled per `user_config.py` and `CLAUDE.md`)

| Strategy | Flag | State per CLAUDE.md | Exit-side data exposed |
|----------|------|---------------------|------------------------|
| `_evaluate_sr_fade_signal` | `SR_FADE_ENABLED` | False | Uses `contract_sr.nearest_support/resistance` + velocity-decel gate |
| `_evaluate_late_dominant_signal` | `LATE_DOMINANT_ENABLED` | True (rare fires) | Confidence composite (distance / cum_share / aggressor / poc / vwap / velocity / wall_consumption) |
| `_evaluate_ta_forced_signal` | `TA_FORCED_ENABLED` | False | TA scorer (EMA / RSI / candle pressure / cycle return), TA_INVERSION direction flip |
| `_evaluate_atm_session_probe_entry` | `ATM_REVERSION_ENABLED` | False | `atm_reversion.evaluate_exit()` — target_c, profit_target_c, strike_escape, force_exit_age_s |
| `_evaluate_price_action_signal` | (per-call gate) | inactive | `_read_price_action()` — mid_move, mid_rate, flow_dir, flow_strength, taker_imbalance, vote tally, phase |
| `_evaluate_pre_open_arb_signal` | wallet-flow gated | inactive | Smart wallet elite count, conviction, weighted vol |
| `_evaluate_open_burst` / `_evaluate_open_momentum_signal` | inactive | — | First-seconds momentum readings (no exit value) |
| `_evaluate_mimic_signal` | `MIMIC_ENABLED=True` (gated by wallet poll) | inactive | `SmartFlowState` — wallet directional bias |
| `_evaluate_algo_signal` / `_evaluate_trend_follow_signal` | wallet-pool dependent | inactive | Same wallet-pool inputs |
| `sniper.sniper_check` | `SNIPER_ENABLED=False` (hard `return` at top) | retired | None — pure entry strategy with fixed +5c TP |
| `_arb_detector_loop` | `ARB_DETECTOR_ENABLED=False` | inactive | `yes_ask + no_ask` summation (no exit value) |
| `MICRO_PULLBACK_ENABLED` (entry-arming logic) | True | inactive in BB_PURE path | `_pullback_armed_state` — ref_mid, ref_fvg_mag |

### 1.2 Pure modules / helpers that are *always running* (regardless of which strategy is enabled)

| Module | Class / Function | Always runs? | Currently consumed by |
|--------|------------------|--------------|------------------------|
| [contract_sr.py](btc-bias-engine/contract_sr.py) | `ContractSRState`, `level_strength`, `nearest_support/resistance` | YES — fed every cycle for the active ticker (engine 7454-7463) | SR_FADE evaluator only |
| [shadow_edge.py](btc-bias-engine/shadow_edge.py) | `compute_shadow_edge()` — bb+pres+mtf+rsi+btc → signed score | YES (when `SHADOW_EDGE_ENABLED=True`, default True) | Logging only |
| [microstructure.py](btc-bias-engine/microstructure.py) | `MarketPressure.update()` → btc_impulse/book/flow/kalshi_lag/persistence | YES — `self._microstructure.update()` at signal eval time | TA_FORCED entry path; `self._last_pressure` |
| [contract_momentum.py](btc-bias-engine/contract_momentum.py) | `ContractMomentumAnalyzer` (MRC) | YES (when `MRC_ENABLED=True`) | **BB_PURE TP modulation already** (line 15378-15405) |
| [tape_pressure.py](btc-bias-engine/tape_pressure.py) | `compute_exit_pressure_snapshot` + `evaluate_exit_signal` | YES (when `BB_PURE_TAPE_EXIT_SHADOW_ENABLED=True`) | Phase 8b shadow log only — gate is False |
| [protective_math.py](btc-bias-engine/protective_math.py) | `compute_mfe_trail_price`, `classify_trend_orientation`, `select_vol_cap` | called every protective cycle | **BB_PURE MFE-trail already wired** |
| [regime.py](btc-bias-engine/regime.py) | `RegimeClassifier` → `Regime.STRUCTURED/CHOP/CHAOTIC` + `DrawdownState` | YES — updates each cycle alongside pressure (engine 7629-7634) | Logging / SR_FADE entry gate only |
| [mtf_scorer.py](btc-bias-engine/mtf_scorer.py) | `MTFConfluenceScorer.evaluate()` | depends on TF candle ingestion | Logging only |
| `_detect_wall_consumption` (engine line 16465) | per-side ask-stack consumption rate | called on demand from `_book_depth_history` ring buffer | LATE_DOMINANT entry confidence; `_wallet_status_router` |
| [whale_monitor.py](btc-bias-engine/whale_monitor.py) | mempool BTC inflow/outflow | YES (background task) | Logging only |

This second table is the load-bearing observation: **most of the alpha-bearing computations don't care whether the strategy that *originally* asked for them is enabled.** The data is already on the engine as state. The exit path just doesn't consult it.

---

## 2. Per-Component Deep Dive

### 2.1 Contract S/R levels — `contract_sr.py` ★ HIGHEST VALUE FOR TP

**What it computes**

A pure-Python detector that watches the contract's YES mid tick stream and maintains per-cent dwell evidence. For each cent (1-99) it records:
- `dwells` — distinct visits (entered then left)
- `samples_at` — total ticks within ±1c of this price
- `last_bounced_from_above/below` — defended returns
- `last_seen_sample` — recency

The composite `level_strength()` is `0.4·dwell + 0.2·depth + 0.2·recency + 0.2·bounce_quality` ∈ [0,1]. `nearest_support(state, mid)` and `nearest_resistance(state, mid)` return the strongest level within `max_distance` (default 20c) on the relevant side.

**Currently called every cycle for the active ticker** at line 7454-7463 of `polymarket_copy_engine.py:7454`:
```python
contract_sr.update(_sr_cs, int(mid))
```
The state is in `self._sr_state[ticker]`. SR_FADE consumed it for entries; nobody consumes it for exits.

**How it could inform exits**

- **TP cap**: if a 0.5+ strength resistance sits between current bid and the FVG-close target, the FVG TP is structurally aspirational. Tighten TP to `resistance - 1c`. This is the single highest-impact use — BB_PURE today happily places a TP at `entry+12c` even when an empirically defended level sits at `entry+4c`. Maker fills are bid-priced, so a level the contract has bounced off twice is a real fill blocker.
- **SL floor**: a strong support a few cents below current bid means the structural floor is real; if our SL trigger is between entry and that support, it should jump *under* the support (a break through a level matters; a wick to it does not).
- **Hold-to-expiry**: if our side has a strong support 1-3c below current bid AND time-of-window is past minute 12, the position is structurally protected — defer exit.

**Data availability**: YES. `self._sr_state[ticker]` is already maintained each cycle.

**Effort**: EASY. A 10-line check in the protective TP-target block at line 15346-15370 to consult `nearest_resistance(state, our_mid)` and clamp `tp_target` to `min(tp_target, level_px - 1)`.

**Risk**: SR levels in the first ~60s of a window are noise (`samples_seen < ~30`); gate the consult on `sr.samples_seen >= 30`. The SR_FADE evaluator already requires `samples_seen >= 5` — match or exceed that.

---

### 2.2 Shadow-edge composite — `shadow_edge.py` ★ HIGHEST VALUE FOR THESIS-BREAK

**What it computes**

Linear blend of five components, all YES-positive on a consistent [-100, +100] scale:
```
bb        = clip(model_yes_prob*100 - mkt_price_cents, -20, 20)    # weight ~80%
pres      = clip(pressure_score * pressure_confidence * 10, -10, 10)
mtf       = clip(mtf_score / 5, -20, 20)
rsi_tilt  = clip((rsi - 50) / 50 * 3, -3, 3)
btc_5m    = clip(btc_5m_move / 30, -1, 1) * 5
score     = bb + pres + mtf + rsi_tilt + btc_5m
side      = "yes" if score > 0 else "no"
confidence = min(1.0, |score| / 25)
```

`SHADOW_EDGE_ENABLED=True` (line 7954) means it logs every cycle but never gates anything.

**How it could inform exits**

The five components encode the entire current alpha thesis (BB-fair, microstructure, MTF, RSI, BTC trend). When they collectively *flip against* our position, the thesis is broken. Specifically:

- **SL escalation**: `edge.side != our_side AND edge.confidence > 0.6` is a thesis-broken event. Force `target_state = "sl"` immediately rather than waiting for bid to drop 8c.
- **Hold past TP**: `edge.side == our_side AND edge.confidence > 0.7` — let the MFE trail run wider; do not tighten TP on micro pullbacks.
- **TP modulation**: scale TP target by `1.0 + 0.5·max(0, edge.confidence)` when aligned, by `0.7` when neutral/opposing.

This is the single best "global thesis still intact?" check available because it is *already a vote*: the BB component contributes the bulk, and if BB still says we're right, `bb` stays positive even as price wiggles. Conversely, if BB has moved against us *and* pressure agrees, we're holding a stale thesis.

**Data availability**: YES. The compute function is pure and the inputs (`prob_engine.probability`, `MarketPressure.score/confidence`, MTF `confluence_score`, RSI from `_ta_scorer.last_result`, `btc_5m_move`) all exist on the engine. Whether `compute_shadow_edge()` is *called and stored* on every cycle depends on whether the existing logging path stores the result; if not, the marginal cost is one function call per protective tick.

**Effort**: EASY. Persist last `ShadowEdge` to `self._last_shadow_edge` and read it in the protective layer.

**Risk**: The bb component caps at ±20pp, so a screaming-edge BB_PURE entry (edge=15-20pp) starts fully loaded into shadow_edge. A normal post-entry decay where bid catches up to fair will *naturally* reduce |score| toward zero — that's the trade *working*, not breaking. Don't trigger SL on score decay alone; require `edge.side != our_side` AND `confidence > threshold` to cross from "edge fading" to "edge flipped".

---

### 2.3 MarketPressure (microstructure) — `microstructure.py` ★ HIGHEST VALUE FOR LIVE THESIS

**What it computes**

`PressureScore`: signed `score` ∈ [-1, +1] from four weighted components:
- `btc_impulse` (35%): BTC 5s/30s/2s moves blended (with acceleration term)
- `book_pressure` (25%): book imbalance + microprice skew + depth ratio
- `flow_momentum` (25%): WS taker flow + REST taker imbalance + large-trade bias
- `kalshi_lag` (15%): empirically-fit BTC→mid regression vs. observed mid move; flags when Kalshi is stale relative to BTC

Plus `persistence_count` / `persistent` flag (held above threshold for ≥3 cycles ≈ 1s).

**Currently** stored as `self._last_pressure` in the TA_FORCED entry path. Updated every cycle that signal eval runs. Not consulted for exits.

**How it could inform exits**

- **`kalshi_lag`** is the single most actionable signal for a maker-bid trade: when `kalshi_lag` is on our side and `btc_impulse` agrees, Kalshi will reprice in our favor in seconds. **Hold past current TP** until `kalshi_lag` decays below threshold. This converts BB_PURE from "exit when bid hits fair" to "exit when the lag closes."
- **`persistent` against us with `confidence > 0.5`**: thesis-broken at the microstructure level, even if bid hasn't moved yet. Escalate to SL.
- **`persistent` aligned with us**: widen MFE-trail giveback ratio (let it run).

**Data availability**: YES. Already computed each cycle. Already stored on `self._last_pressure`.

**Effort**: EASY-MEDIUM. The `_last_pressure` may be stale if the protective tick runs faster than the signal-eval tick — verify by reading `self._microstructure.last_score` directly (always last computed) or by fetching `last_update` and gating on freshness.

**Risk**: MarketPressure is currently designed for *entry* gating — its threshold and persistence rules favor opening trades, not maintaining them. For exit use, drop the `entry_threshold` check and consume the raw signed score with side-alignment.

---

### 2.4 Wall consumption — `_detect_wall_consumption` (engine line 16465) ★ MOST DIRECT EXIT SIGNAL

**What it computes**

Inspects `self._book_depth_history[ticker]` ring buffer; for a given side, returns:
- `consumed_ct` (depth shrinkage over the lookback)
- `rate_per_s` (contracts/sec consumed from `top3` of that side's ask)
- `verdict`: `AGGRESSIVE_BUY` (≥30 ct/s), `BUYING` (≥10), `STABLE`, `REFILLING` (≤-10)

Already used by the LATE_DOMINANT confidence composite (line 7246).

**How it could inform exits**

This is the most physically direct exit-management signal in the entire codebase. It tells you *whether somebody is right now lifting offers on a particular side*. Two readings per protective cycle (one per side, lookback ~5-10s):

- **Our-side `AGGRESSIVE_BUY`**: buyers are lifting offers on our side. Holding past current TP is +EV. Re-arm TP +2c.
- **Opposing-side `AGGRESSIVE_BUY`**: buyers are aggressively buying *against* us. Bid is about to drop. Pre-emptive SL is +EV, even before bid moves.
- **Our-side `REFILLING` while opp-side `AGGRESSIVE_BUY`**: classic distribution into our side. Fastest possible exit signal.

**Data availability**: YES. Method is already on the engine.

**Effort**: EASY. Two `_detect_wall_consumption()` calls per protective tick.

**Risk**: Top-3 depth on a thin Kalshi market can swing on a single 50ct order. Use a 5-second lookback minimum and require `samples ≥ 3`. False positives in the first ~10s of a new ticker before the depth ring fills.

---

### 2.5 Phase 8b tape-exit-pressure — `tape_pressure.py` ★ ALREADY WIRED, JUST NEEDS FLAG

**What it computes**

Symmetric to entry-side absorption but on a 30s window. From `compute_exit_pressure_snapshot`: aggregates BUY-aggressor dollar volume on our side vs. opposite side over a short window. `evaluate_exit_signal` flags exit when:
1. `opposite_dollars >= massive_volume_usd` (default $300)
2. `opposite_dollars >= dominance_ratio × our_side_dollars` (default 3×)
3. `opposite_large_count >= min_large_count` (default 2)

Today this code is **already invoked inside `_maintain_protective_order`** (line 15500-15541, `BB_PURE_TAPE_EXIT_SHADOW_ENABLED=True`) but in shadow mode — when it fires `"GATE-LIVE: forcing SL"` is logged with the suffix `"shadow only, no action"` because `BB_PURE_TAPE_EXIT_GATE_ENABLED=False`.

**How it could inform exits**

It already does — flip the gate flag. The thresholds have already been calibrated per the user's "massive volume to the opposite direction is also an indicator to exit" instruction.

**Data availability**: YES.
**Effort**: TRIVIAL — flip `BB_PURE_TAPE_EXIT_GATE_ENABLED=True` after reviewing shadow-mode log frequency to validate that false positives aren't endemic.
**Risk**: Tape data is the lagging signal in this set — wall consumption is faster, but tape filters out spoofing in a way wall-consumption doesn't.

---

### 2.6 LATE_DOMINANT confidence composite — `_evaluate_late_dominant_signal` ★ HOLD-TO-EXPIRY

**What it computes**

Late-session momentum confidence ∈ [0, 1] from seven weighted components:
- `c_dist` (distance past 0.05% strike-distance floor)
- `c_cum` (fraction of recent CB volume on our side of strike)
- `c_agg` (signed aggressor imbalance aligned with side)
- `c_poc` (point-of-control on our side?)
- `c_vwap` (VWAP on our side?)
- `c_vel` (BTC tick velocity in our direction)
- `c_wall` (wall consumption verdict on our side)

The composite synthesizes "is BTC structurally on our side and is volume confirming?" — exactly the question that determines whether holding to expiry beats taking TP.

**How it could inform exits**

The mechanism is at heart a "hold-to-expiry" scorecard. When `secs_remaining < 180` (last 3 minutes) and we already have a profitable BB_PURE position:
- `confidence > 0.65` aligned with our side → cancel resting TP, hold to settlement
- `confidence < 0.4` against our side → take TP NOW (or escalate if losing)
- in between → keep current behavior

**Data availability**: YES — `volume_tracker`, `tick_tracker`, `_btc_last_price`, `prob.strike` all exist. The confidence composite is currently only computed inside `_evaluate_late_dominant_signal`. Refactoring it into a pure helper `compute_late_dominance_score(side)` is a small lift.

**Effort**: MEDIUM. Refactor 60 lines of the evaluator into a pure function, then read it from the protective layer in the `secs_remaining < 180` branch.

**Risk**: `volume_tracker.is_warm` (≥30 prints) gates the read. In low-volume sessions the composite returns junk. Fail closed → no late-session adjustment.

---

### 2.7 MRC `path_signature` and micro-alignment — `contract_momentum.py` ★ ALREADY EXPOSED, UNDER-USED

**Status**: BB_PURE *already* uses MRC's `recommended_tp_multiplier` (line 15378-15405). But the analyzer exposes much more than the multiplier:

- `path_signature` ∈ {FLAT, STAIRCASE_UP, STAIRCASE_DOWN, V_SHAPE, INVERTED_V, M_TOP, W_BOTTOM, MIXED}
- `should_force_exit` (already True when path is structurally against side)
- `micro_momentum_5s`, `micro_momentum_25s`, `micro_alignment` (5s flip vs. 25s trend)
- `convergence_rate` (settlement-convergence rate ∈ [0, 1])

**How more of MRC could inform exits**

- **`should_force_exit`**: today only feeds the `_tp_multiplier` clamp. Reading it directly in the protective tick gives a binary force-exit signal that the current code path silently absorbs into the TP scaling. **HIGH** value SL signal.
- **`path_signature == "M_TOP"` while holding YES** (or `"W_BOTTOM"` while holding NO): structural distribution signature. Take TP NOW.
- **`convergence_rate > 0.7` aligned with our side**: market has decided in our favor — hold to expiry.
- **`micro_alignment < -0.5`**: 5s flipped against the 25s trend = early reversion warning.

**Data availability**: YES — analyzer is already attached to the position dict at `pos["_momentum_analyzer"]` (line 10408) and updated each tick (engine 18688-18756 in `_manage_position`).

**Effort**: EASY. Read additional fields in the same block that already reads `recommended_tp_multiplier`.

**Risk**: The analyzer needs `MIN_OBS=30` samples. `is_warm` already gates the TP multiplier consumption — extend the same gate.

---

### 2.8 MTF Confluence Scorer — `mtf_scorer.py`

**What it computes**

Per-timeframe directional signals (1m / 5m / 15m / 1h) blended with weights `0.222 / 0.278 / 0.333 / 0.167`. Returns `ConfluenceResult` with `confluence_score` ∈ [-1, +1] and `is_aligned` / `is_opposing` boolean helpers. `_NORMAL_THRESHOLD = 0.30`, `_HIGH_CONFIDENCE_THRESHOLD = 0.60`.

**How it could inform exits**

- **`is_opposing`** post-entry → structural reversal across timeframes → escalate SL.
- **`is_aligned` with `confluence_score > 0.6`** → strong cross-TF support → hold past TP.

**Data availability**: depends on whether `tf_analyzer` is fed candle closes. Per `CLAUDE.md`, modules `mtf_scorer`, `tf_analyzer`, `ta_module` are "used by retired TA_FORCED tier; some helpers still imported." If the candle-feed loop is gone, MTF is cold.

**Effort**: MEDIUM-HARD. Need to verify the candle ingestion is still running; if not, restoring it adds 5m/15m/1h candle pulls.

**Risk**: Higher-TF signals lag intra-window moves; useful for the "should we hold to settlement" question but weak for tactical 5-30s decisions where wall consumption and pressure dominate.

---

### 2.9 TA scorer + TA_INVERSION — `ta_module.py`

**What it computes**

EMA spread, RSI, volume, cycle return, candle pressure → composite TA score with confidence bins (STRONG / MEDIUM / WEAK). `TA_INVERSION_ENABLED=True` flips entry direction when ≥2 of 3 broader signals oppose TA.

**How it could inform exits**

- **TA flip post-entry** = mid-trade momentum reversal at the 1-minute timescale. Useful as an SL-confirmation signal layered on top of pressure / wall consumption.
- TA confidence is already partially captured in shadow-edge's `rsi_tilt` component, so the marginal information beyond shadow-edge is small.

**Data availability**: YES, `self._ta_scorer.last_result` always available *if* the candle feed is running.

**Effort**: MEDIUM (if candle feed is intact); HARD (if not).
**Risk**: Redundant with shadow-edge's RSI input; marginal alpha probably modest.

---

### 2.10 ATM_REVERSION's `evaluate_exit` — `atm_reversion.py`

**What it computes**

Pure exit decision for ATM scalps:
1. `side_bid >= target_c` (49c absolute target — basically "bid says we won the bet")
2. `side_bid >= entry_c + profit_target_c` (+8c)
3. `|strike_dist_pct| >= stop_strike_dist_pct` (BTC escaped the strike zone)
4. `age_s >= force_exit_age_s` (time stop)

**How it could inform exits**

- The **strike-escape stop** is interesting because BB_PURE positions all sit *near* strike (the entry gate is `|BTC - strike| / BTC < 0.0004`). When BTC moves past, say, 0.10% from strike, the binary becomes structurally one-sided — winning side hold-to-expiry, losing side bail.
- The other three rules are subsumed by BB_PURE's existing TP/SL/pre-expiry layers.

**Data availability**: YES.
**Effort**: EASY (a 5-line strike-distance check in protective layer).
**Risk**: The "side that's *currently* winning" identification is nontrivial — strike was set at entry; intervening BTC moves redefine which side is winning. Can't naively use entry-time strike.

---

### 2.11 Wallet flow tiers (MIMIC / PRE_OPEN_ARB / TREND_FOLLOW / ALGO / WALLET_COPY)

**Current state**: per `CLAUDE.md`, "WALLET_COPY_* = False (all four)". The `_smart_flow` state and `SmartFlowState` would only be populated if the wallet poll loops are running.

**Potential**: high if running — elite wallet directional bias is a strong leading signal. **Effort**: HARD (requires re-enabling the wallet pool, which is explicitly retired).
**Verdict**: Out of scope. Anything wallet-flow-derived adds operational complexity (Polymarket API dependency, scoring DB) that the strategic reset deliberately removed.

---

### 2.12 Sniper, ARB_DETECTOR, OPEN_BURST, OPEN_MOMENTUM

Pure entry strategies. No exit logic worth porting.

---

### 2.13 Whale monitor — `whale_monitor.py`

Mempool BTC inflow/outflow. Lead time 10-60s before BTC spot impact. Mid-trade BTC velocity SL already catches BTC moves faster than mempool publication latency in the worst case. Marginal value for exit decisions is low.

---

### 2.14 Regime classifier — `regime.py`

`Regime ∈ {STRUCTURED, CHOP, CHAOTIC}` based on realized vol + 5m BTC drift + pressure consistency. Already updated each cycle. Currently consumed only by SR_FADE entry gate.

**Exit value**: MEDIUM. `Regime == CHAOTIC` should tighten exit aggression (faster MFE-trail giveback, lower-bar SL). `Regime == STRUCTURED` aligned with our side should widen tolerance.

**Effort**: EASY.
**Risk**: low — regime is calibrated coarse, won't oscillate on noise.

---

## 3. Per-Component Scorecard

| # | Component | TP enhance | SL enhance | Hold-to-expiry | Effort | Data ready? |
|---|-----------|----|----|----|----|----|
| 1 | Contract S/R levels | **HIGH** | MED | MED | **EASY** | **YES** |
| 2 | Shadow-edge composite | MED | **HIGH** | **HIGH** | **EASY** | YES (re-store) |
| 3 | MarketPressure (esp. `kalshi_lag`) | MED | **HIGH** | **HIGH** | **EASY** | YES |
| 4 | Wall consumption | **HIGH** | **HIGH** | **HIGH** | **EASY** | YES |
| 5 | Phase 8b tape-exit (already wired) | LOW | **HIGH** | LOW | **TRIVIAL** | YES — just flip flag |
| 6 | MRC `path_signature` / `should_force_exit` / `micro_alignment` | MED | **HIGH** | **HIGH** | **EASY** | YES |
| 7 | LATE_DOMINANT confidence composite | LOW | LOW | **HIGH** | MEDIUM | YES (refactor) |
| 8 | Regime classifier | LOW | MED | MED | **EASY** | YES |
| 9 | MTF confluence | LOW | MED | MED | MEDIUM-HARD | depends |
| 10 | TA scorer flip | LOW | MED | LOW | MEDIUM | depends |
| 11 | ATM strike-escape exit | LOW | LOW | MED | EASY | YES |
| 12 | Wallet flow / smart wallets | MED | **HIGH** | **HIGH** | HARD | NO (retired) |
| 13 | Whale monitor | LOW | LOW | LOW | EASY | YES |
| 14 | Sniper / ARB_DETECTOR / OPEN_* | — | — | — | — | irrelevant |

---

## 4. Proposed Exit Management Architecture (informational; do not implement)

The current `_maintain_protective_order` makes exit decisions in roughly this order:

```
1. Shutdown / pre-expiry consolidation gate
2. BB_MOMENTUM-only post-entry exit (strike-cross + MFE trail)
3. Kalshi truth count + flat-confirmed clearance
4. Pre-expiry force flatten (last 60s)
5. Compute tp_target (BB_PURE: fair − inside, clamped by tier)
6. MRC TP modulation (multiplier on premium)
7. BB_PURE MFE-trail re-arm
8. Three-state machine: TP / SL / HOLD on bid vs. entry vs. SL trigger
9. Mid-trade BTC velocity SL (sustained N polls of adverse vel)
10. Phase 8b tape exit-pressure (shadow only, gate=False)
11. Resting-order preflight + cancel-and-verify replan
```

A four-tier extension that consumes the disabled-strategy data without changing the existing safety stack:

```
TIER 0 (HARD STOP, beats everything else)
  a. Existing pre-expiry / shutdown / flat-confirmed
  b. NEW: Wall consumption — opposing-side AGGRESSIVE_BUY @ rate ≥ 30 ct/s
          → force SL immediately (lowest-latency thesis-break signal)
  c. NEW: MRC.should_force_exit == True (already computed; binary flag)

TIER 1 (THESIS GATE, modulates state machine)
  a. Shadow-edge: edge.side != our_side AND edge.confidence > 0.6
        → escalate target_state from HOLD to SL (skip the bid wait)
  b. MarketPressure: persistent && direction != our_side && conf > 0.5
        → same as above
  c. MRC: path_signature ∈ {against-side patterns} && obs_count ≥ MIN_OBS
        → escalate

TIER 2 (TP TARGETING — refines tp_target)
  a. Contract S/R: clamp tp_target to nearest_resistance(state, our_mid) - 1
       when level_strength ≥ 0.4 AND samples_seen ≥ 30
  b. Shadow-edge confidence aligned > 0.7 → widen tp_target by 1-2c
       (let it run, trail handles the giveback)
  c. Wall consumption — our-side AGGRESSIVE_BUY for ≥ 5s
       → re-arm TP higher (+2c above current tp_target)

TIER 3 (HOLD-TO-EXPIRY DECISION — last 3 minutes)
  a. compute LATE_DOMINANT confidence (refactored from the evaluator)
       conf > 0.65 aligned with side → cancel resting TP, hold to expiry
  b. Regime classifier == STRUCTURED + Shadow-edge aligned conf > 0.6
       → same
  c. MRC.convergence_rate > 0.7 + cms_norm aligned
       → same (MRC's own signal that the market has decided)
```

Layer interaction notes:
- **Tier 0 always wins.** A wall-consumption AGGRESSIVE_BUY against us beats any composite "thesis still intact" score.
- **Tier 1 escalates, never dampens.** It can flip HOLD → SL, but never SL → HOLD. The existing bid-driven SL trigger remains the floor.
- **Tier 2 only modifies tp_target.** It never overrides the SL trigger or the MFE-trail floor.
- **Tier 3 is gated on time remaining.** Outside the last 3 minutes, hold-to-expiry signals are noise — the contract still has too much time to decay.

The existing MRC TP modulation (`recommended_tp_multiplier`) and BB_PURE MFE-trail logic compose cleanly under this scheme — they sit at the end of Tier 2.

---

## 5. Priority Ranking (highest expected impact first)

### Quick wins (data already computed, hours of integration)

1. **Wall consumption opposite-side AGGRESSIVE_BUY → SL** — fastest exit signal in the codebase, already a method on the engine. (#4)
2. **Contract S/R `nearest_resistance` → TP cap** — fixes the "TP set above a defended level" failure mode that BB_PURE has today. (#1)
3. **MRC `should_force_exit` direct read in protective tick** — zero new compute, just consume an already-published binary signal. (#6)
4. **Phase 8b tape-exit-pressure gate flip** — one config change, framework already wired in protective layer. (#5)
5. **Shadow-edge persisted to `self._last_shadow_edge`** + thesis-break SL escalation. (#2)

### Medium-effort wins

6. **MarketPressure `kalshi_lag` aligned → hold past TP** — exploits the BB_PURE entry thesis (Kalshi is slow to reprice) on the exit side. (#3)
7. **LATE_DOMINANT confidence pulled into `compute_late_dominance_score`** for hold-to-expiry decisioning in last 3 minutes. (#7)
8. **Regime classifier consumed for exit-aggression scaling** (CHAOTIC → tighter MFE giveback). (#8)

### Lower priority / further out

9. **MRC `path_signature` pattern-match** for early reversal detection. (#6 deeper)
10. **MRC `convergence_rate > 0.7` for hold-to-settle** — partially redundant with LATE_DOMINANT but cheaper to compute.
11. **MTF / TA flip detection** — only if the candle feed is verified to still be running.
12. **ATM strike-escape exit logic** — minor late-window enhancement.
13. **Whale monitor** — too high latency.
14. **Wallet-flow tiers** — out of scope per strategic reset.

---

## 6. Quick Wins Checklist (where the data is *already* computed)

For an implementer wanting a single morning of work, these are the four quickest plumbing changes — every input already exists on `self`:

| # | Change | Lines touched | Reads | Writes |
|---|--------|---------------|-------|--------|
| 1 | Read `nearest_resistance(self._sr_state[ticker], our_mid)` after `tp_target` is computed; clamp `tp_target = min(tp_target, level_px - 1)` when `level_strength ≥ 0.4`. | ~10 in `_maintain_protective_order` after line 15370 | `self._sr_state[ticker]` | `tp_target` |
| 2 | Two `_detect_wall_consumption(ticker, side, 5.0)` calls; if `opp_verdict == "AGGRESSIVE_BUY"` AND `target_state != "sl"`, set `target_state = "sl"` with `place_px = bid - 1`. | ~15 lines after line 15490 | `self._book_depth_history[ticker]` | `target_state`, `place_px` |
| 3 | Read `pos["_momentum_analyzer"].should_force_exit`; when True AND `is_warm` AND not already SL-state, escalate. | ~5 lines after MRC TP block (line 15405) | `pos["_momentum_analyzer"]` | `target_state` |
| 4 | Flip `BB_PURE_TAPE_EXIT_GATE_ENABLED=True` after reviewing 24h of shadow-mode logs to confirm fire rate ≤ 2-3 per session. | 1 line in `user_config.py` | — | gate value |

After these four are validated live, work toward the deeper Tier 2/3 integration.

---

## 7. Open questions for follow-up

- **Is the candle feed still running?** Determines whether MTF / TA-flip signals are practically available. Check `tf_analyzer` ingestion path.
- **Is `compute_shadow_edge()` actually called every cycle, or only when a strategy that consumes it is enabled?** If the latter, it has to be moved into the always-on path.
- **What is the false-positive rate of the Phase 8b shadow log?** Without this number, flipping the gate live has unknown blast radius.
- **Does `_book_depth_history` ring buffer get populated on the active ticker even when no entry-side strategy is pulling depth?** If WS handles populate it unconditionally, wall-consumption is free; if a strategy-specific code path triggers the population, fixing the population is a prereq.
- **Does `_sr_state[ticker]` decay on ticker rotation?** Need to confirm samples accumulate across consecutive tickers vs. resetting per window. Per-window reset is fine for the proposed use; cross-window persistence is a bonus.

---

*End of research document. No code changes made.*
