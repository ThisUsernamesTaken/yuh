# RESEARCH — Kalshi KXBTC15M Strategy Archetypes — Analysis and Implementation Mapping

**Date**: 2026-05-07
**Status**: Research only. No code changes. No flag flips. No execution path edits.
**Scope**: Map eight commonly-discussed Kalshi 15-minute BTC binary strategy archetypes against the current engine. For each, identify what the engine already does, what is missing, and a concrete (but unimplemented) integration path.

---

## 0. Pre-flight correction — important framing note

The brief that motivated this document was written assuming **BB_PURE is the live primary signal**. That has not been true since 2026-05-04. Per [`btc-bias-engine/CLAUDE.md`](btc-bias-engine/CLAUDE.md) and [`btc-bias-engine/user_config.py`](btc-bias-engine/user_config.py):

| Strategy / Flag                       | State                                  |
| ------------------------------------- | -------------------------------------- |
| `DIRECTION_STRATEGY_ENABLED = True`   | **PRIMARY LIVE SIGNAL** (since 2026-05-06) |
| `BB_PURE_MODE = False`                | Killed 2026-05-04 19:55                |
| `BB_MOMENTUM_ENABLED = False`         | Killed 2026-05-05                      |
| `PAPER_FVG_LIVE_MODE = False`         | Emergency-disabled 2026-05-05 (-19% loss event) |
| `TA_FORCED_ENTRY_ENABLED = False`     | Retired                                |
| `SR_FADE_ENABLED = False`             | Retired                                |
| `WALLET_COPY_ENABLED = False`         | Retired                                |
| `SNIPER_ENABLED = False`              | Retired                                |

The **current** live model is single-strategy direction-following: when BTC is `≥ 0.10%` past strike with 5-min momentum agreeing (`≥ $10`), buy that side at ask + 3c via IOC, hold to expiry. No exit orders — Kalshi auto-credits $1.00 per winning contract at settlement. Backtest evidence (n=197): 90.5% WR / +$3.93 mean / +$331 corpus.

This pivot **inverts** several premises in the brief:

- The brief asks "which archetypes does BB_PURE map to" — but BB_PURE is retired. The relevant question is which archetypes does **DIRECTION** map to, and what role (if any) the BB_PURE codebase still serves.
- The brief says "avoid Momentum Continuation (#3) because it conflicts with the contrarian thesis" — but DIRECTION **is** Momentum Continuation, and it is the only live strategy. The contrarian (BB_PURE) thesis is exactly what was killed.
- The brief assumes "5 exit enhancements + MRC + micro-timeframes" are stacked on top of an active mean-reversion strategy. In reality MRC and the exit-enhancement research were aimed at BB_PURE's `_maintain_protective_order` path, which DIRECTION **does not use** — DIRECTION places no exit orders.
- The recent engine log shows two `ESCROW LEAK` warnings (2026-05-07 07:30 and 07:45) — balance dropping while engine reports `open_pos=None`. This is a known anomaly class and suggests the broader execution accounting still has gaps independent of strategy choice.

So this document does two things in tandem:

1. Honors the brief by analyzing all eight archetypes against the codebase (because most of the analytical infrastructure — `microstructure.py`, `contract_sr.py`, `contract_momentum.py`, `tape_pressure.py`, `regime.py`, the BB_PURE module itself — is still in the tree, and most archetypes can be re-anchored to DIRECTION as the primary).
2. Re-frames the "current engine capability" claims to reflect what the engine **does today**, not what it would do if BB_PURE were still live. Where the brief's claim is wrong-given-current-state, that is called out explicitly.

---

## 1. Eight Archetypes — Per-Archetype Analysis

### Archetype 1 — Early-Session Mispricing Scalpers

**Concept.** First 1-3 minutes after window open, retail emotional bidding skews the contract away from any defensible probability estimate. Buy the underpriced side, exit when the market reverts to fair.

**Engine capability today.**
- DIRECTION does not do this. DIRECTION fires whenever its dist+momentum conditions are met within `0-600s` of session age — it has no gate that says "wait for the early-session emotional spike to clear." Its entry timing is determined by BTC's distance from strike, not by session age.
- BB_PURE *was* this archetype's primary implementation. Brownian-Bridge fair value (`price_feed.py:346-505` → `prob_engine.fair_value`) computes a model probability; entries fired on `mispricing ≥ BB_PURE_MIN_EDGE_PP = 8.0` pp. But BB_PURE is off.
- The infrastructure to re-enable it is intact: `bb_pure.py` module is still in the tree; `prob_engine` still computes fair value every tick; `BB_PURE_MIN_EDGE_PP / BB_PURE_TP_INSIDE_FAIR_C / BB_PURE_KELLY_*` are all still defined in `user_config.py` (lines 489-973).

**Gap (brief's claim).** "Engine enters too early — entries after minute 3 had 60.5% WR vs 36.5% before." This claim is for BB_PURE, not DIRECTION. For DIRECTION, the analogous claim has not been validated; DIRECTION's 90.5% WR backtest does not stratify by minute-of-window in the docs available.

**What's actually missing.**
- No `MIN_SESSION_ELAPSED_S` gate exists on BB_PURE (or DIRECTION). Both can fire from second 0.
- BB_PURE's only time-related entry gate is `BB_PURE_HARD_MIN_TIME_S = 420.0` (minimum **time-to-expiry**, i.e. fires only in the first half of the window) — the inverse of what the brief asks for.

**Implementation path (if BB_PURE is ever re-armed).**
1. Add `BB_PURE_MIN_SESSION_ELAPSED_S = 180` in `user_config.py`.
2. In `_evaluate_bb_pure_signal` (anchor: search for `BB_PURE_HARD_MIN_TIME_S` in `polymarket_copy_engine.py`), add a parallel check: skip if `(900 - seconds_to_expiry) < MIN_SESSION_ELAPSED_S`.
3. Allow the signal to *evaluate* and *log* before the gate (for shadow-mode validation), but only allow it to *fire* after minute 3.
4. The MRC analyzer (`contract_momentum.py`) provides exactly the confirmation signal the brief mentions: `micro_momentum_5s` reverting against `micro_momentum_25s` is the "initial spike is reverting" signature. Wire MRC's `path_signature == V_SHAPE` (for YES) / `INVERTED_V` (for NO) as a confirmation overlay.

**For DIRECTION specifically.** The mispricing-scalper thesis is opposite of DIRECTION's thesis (DIRECTION buys the side that just moved; mispricing-scalper buys the side that overshot in the wrong direction during early panic). They cannot coexist in the same window without a per-window mode selector.

---

### Archetype 2 — Time Decay / Theta Extraction

**Concept.** Late-window, when BTC is stalled near strike, the contract behaves like a struck option with collapsing extrinsic value. Sell inflated sides; buy "dead" contracts at sub-10c that have a non-trivial probability of one final wick.

**Engine capability today.**
- DIRECTION explicitly **opts out** of this — `DIRECTION_MAX_OFFSET_S = 600` blocks entries after minute 10. The last 5 minutes are deliberately non-participated.
- BB_PURE's Brownian-Bridge fair value implicitly captures theta (probability evolves with `time_to_expiry_s`), but BB_PURE never actively *bought cheap* — `BB_PURE_MIN_ENTRY_CENTS = 5` and `BB_PURE_MAX_ENTRY_CENTS = 55` were the entry band. It would buy a 6c contract if mispriced, but had no specific late-window theta-harvest mode.
- The MRC analyzer's `convergence_rate (SCR)` signal in `contract_momentum.py:240-258` is conceptually related: `SCR > 0.7` means "the market has decided" (one side near 0c or 100c with little time to reverse). That is the dual signal — it identifies the inflated side rather than the dead side.

**What's missing.**
- No "late window + flat BTC + cheap contract" entry tier. None of the existing strategies fire in the `seconds_to_expiry < 300` window.
- No realized-vs-implied-vol comparison that would tell you a 7c contract is actually fairly priced at 4c (sell) vs underpriced at 4c (buy).

**Implementation path.**
1. Add `THETA_HARVEST_MODE_ENABLED = False` (off by default) plus `THETA_HARVEST_MIN_ELAPSED_S = 420` (activate after minute 7), `THETA_HARVEST_MAX_PRICE_C = 10`, `THETA_HARVEST_BTC_FLAT_USD = 30` (only when BTC range over last 60s ≤ $30).
2. New evaluator `_evaluate_theta_harvest_signal`. Reads: `seconds_to_expiry`, `mid_cents`, BTC 60s range from `tape_pressure.btc_move_window(60)`, MRC `convergence_rate`. Buys the side whose realized-vol-weighted probability exceeds its mid by ≥ 2c. Tiny size (e.g. 1-2 contracts) — the bet is statistical, won most of the time but pays only on rare wicks.
3. Risk gate: must coexist with DIRECTION's per-window ticker lock. Either lock-and-share (theta only fires if DIRECTION never fired this window) or drop the lock for theta and accept a second exposure per ticker. The existing `MAX_TRADES_PER_SESSION_TICKER = 1` is *inviolable* per CLAUDE.md, so the implementation must respect the lock.

**Honest assessment.** This is a low-priority addition. Sub-10c contracts have terrible bid/ask spreads on Kalshi (often 1c bid / 4c ask), so the realized economics often don't match the theoretical edge. Probably worth a shadow study before any code.

---

### Archetype 3 — Momentum Continuation

**Concept.** BTC trends decisively after open; ride the dominant side, hold to expiry.

**Engine capability today.** **THIS IS DIRECTION.** The brief's premise — "this conflicts with the contrarian thesis, avoid it" — was correct *when BB_PURE was live*, but the engine has since pivoted entirely to this archetype. DIRECTION is pure momentum continuation:
- `DIRECTION_DIST_THRESHOLD_PCT = 0.0010` — must be 0.10% past strike (the move has already happened).
- `DIRECTION_MOMENTUM_THRESHOLD_DOLLARS = 10` — 5-min momentum must agree.
- IOC at `ask + 3c` slippage, hold to expiry, no exit orders.
- Backtest n=197: 90.5% WR / +$3.93 / trade.

**What works.**
- Held-to-expiry eliminates exit-management failure modes that plagued the prior contrarian strategies (FVG-tier had stale-cache premature close, BB_PURE had VWAP_EXIT hijacks).
- IOC eliminates maker bag-holds (which are adverse-selection traps in a momentum strategy — the only fills come from the market reversing into your bid).
- Conviction-sized: `DIRECTION_CONTRACTS = 3 × multiplier(0.7-2.0)`, scales up on high-distance, high-momentum entries.

**Gaps.**
- `btc_5m_move` is currently a session-open proxy, not a true rolling 5-min tracker. Per CLAUDE.md known-gaps: "Real 5-min rolling tracker needs wiring to the `tick_tracker` module."
- DIRECTION has no regime gate — it will fire equally hard in choppy, low-conviction conditions as in clean trends. The `regime.py` classifier (`STRUCTURED / CHOP / CHAOTIC`) is computed every cycle but DIRECTION does not consult it.
- DIRECTION has no microstructure confirmation — it fires on BTC distance/momentum alone, ignoring whether Kalshi's book actually agrees (which is the heart of archetype 4).

**Implementation path (refinements, not replacements).**
1. Wire `regime.py` into DIRECTION's pre-fire gate: skip if `regime == CHAOTIC` (false-momentum environment). One line in `_direction_tick`.
2. Wire the rolling 5-min tracker per the CLAUDE.md gap — replace the session-open proxy with `tick_tracker`'s actual 5-min window. This already-flagged improvement directly affects entry quality.
3. Stratify the n=197 backtest by minute-of-window and by realized-vol regime to find the cells where the 90.5% WR comes from vs cells where DIRECTION is essentially random. If "clean trend cell" is 95% WR and "choppy cell" is 60% WR, gate harder on regime.

---

### Archetype 4 — Microstructure / Orderbook Traders

**Concept.** Kalshi reprices 1-3 seconds behind BTC spot. Read the book imbalance, depth ratio, taker flow, large-trade aggressor side, wall consumption. Enter when Kalshi is misaligned and microstructure agrees.

**Engine capability today.** **STRONG infrastructure, dormant utilization.**
- [`microstructure.py`](btc-bias-engine/microstructure.py) — `MarketPressure` class computes a signed score from `btc_impulse (35%) + book_pressure (25%) + flow_momentum (25%) + kalshi_lag (15%)` plus persistence. Updated every signal-eval cycle. **The `kalshi_lag` component is exactly the BTC→Kalshi lag the archetype targets.**
- [`tape_pressure.py`](btc-bias-engine/tape_pressure.py) — exit-pressure absorption / dominance ratio over 30s windows. `compute_exit_pressure_snapshot` + `evaluate_exit_signal` are the wired primitives.
- [`contract_sr.py`](btc-bias-engine/contract_sr.py) — empirical resistance/support detection on the contract mid, with composite `level_strength = 0.4·dwell + 0.2·depth + 0.2·recency + 0.2·bounce_quality`.
- `_detect_wall_consumption` on the engine — per-side ask-stack consumption rate from the `_book_depth_history` ring buffer. Returns `AGGRESSIVE_BUY (≥30 ct/s) / BUYING (≥10) / STABLE / REFILLING (≤-10)`.

Per [`RESEARCH_INACTIVE_COMPONENTS_FOR_EXIT_MANAGEMENT.md`](btc-bias-engine/RESEARCH_INACTIVE_COMPONENTS_FOR_EXIT_MANAGEMENT.md), all four of these run every cycle but **nothing currently consumes their output for entries.** They were originally entry inputs to TA_FORCED / SR_FADE / LATE_DOMINANT (all retired) and are now logged or attached to the engine for would-be future consumers.

**Gap.** DIRECTION's only input is BTC distance + 5-min momentum. The microstructure layer is invisible to it. A DIRECTION fire can happen at the exact moment Kalshi has filled and re-priced — i.e., when the easy alpha is already gone — and DIRECTION wouldn't know.

**Implementation path.**

1. **Confirmation-only first.** Add a `DIRECTION_REQUIRE_MICROSTRUCTURE_AGREE` flag (off by default). When on, gate DIRECTION fires on `self._microstructure.last_score` having matching sign. Shadow-log the would-be-skips for 7 days; only enforce after confirming microstructure agreement increases hit rate without thinning fires too much.

2. **Standalone microstructure entry tier (later).** A second tier that fires on `kalshi_lag > X AND book_pressure > Y AND wall_consumption == AGGRESSIVE_BUY` regardless of DIRECTION's distance threshold. This is the brief's "activate microstructure scoring as an entry signal" recommendation. The fire path would mirror DIRECTION's IOC-and-hold model.

3. **Wall-consumption pre-emptive exit (DIRECTION's only exit trigger).** Today DIRECTION places no exit orders and holds to settlement. The single highest-value exit addition would be: if **opposing-side `AGGRESSIVE_BUY` at ≥ 30 ct/s sustained for ≥ 5 seconds** while we hold a DIRECTION position, market-sell at `bid - 1`. This is the lowest-latency thesis-break signal in the codebase. (See exit-research doc §2.4.)

**The user is "naturally oriented toward this layer."** Per memory and prior session feedback, microstructure is the most tractable next step.

---

### Archetype 5 — Volatility Regime Traders

**Concept.** Trade *vol*, not direction. If realized vol > implied (the contract's mid implies less movement than BTC actually shows), buy whichever side is cheap — the higher-than-priced volatility will resolve one side to $1. If realized < implied, fade the extreme side.

**Engine capability today.** **Almost nothing direct.**
- `regime.py` returns `STRUCTURED / CHOP / CHAOTIC` based on realized vol + 5m drift + pressure consistency. This is a classifier, not a vol estimate.
- BB_PURE used `BB_PURE_VOLATILITY_LOOKBACK = 15` to estimate vol for its Brownian-Bridge fair-value calculation. But BB_PURE is off, and the vol estimate fed *into* fair value rather than being traded directly.
- `BinaryProbabilityEngine` in `price_feed.py` does compute a vol-aware probability — but again, this is consumed as a "fair price" rather than a tradeable vol signal.
- DIRECTION ignores vol entirely.

**Known calibration issue.** Per the brief and prior research notes, the vol estimator likely uses an equity-hours annualization constant (e.g. 252 trading days × 6.5 hours) where it should use 24/7 (365 × 24). On a 15-minute BTC contract, this miscalibration matters quantitatively — the model thinks BTC is more volatile per-unit-time than it actually is, biasing fair-value estimates toward 50c and inflating perceived mispricings on the wings. **No code change here, but flagging it as a real bug worth a separate investigation.**

**Implementation path (would be a new strategy, not an addition to DIRECTION).**

1. **Realized vs implied vol tracker.** New module `vol_regime.py`:
   - Realized: stdev of BTC log-returns over a rolling 60-300s window, annualized correctly (24/7).
   - Implied: derive from contract mid using the inverse of the Brownian-Bridge probability function. A 60c YES mid with strike 5c above BTC and 8 minutes left implies a specific σ.
   - Output: `vol_premium = realized_vol - implied_vol`.

2. **STRADDLE entry tier.** When `|vol_premium| > threshold` AND `time_to_expiry > 300s`:
   - `vol_premium > 0` (realized higher than implied): buy the cheaper of YES/NO at the mid + 1c. Tiny size.
   - `vol_premium < 0`: do nothing for now (fading the extreme is the harder direction; needs more validation before sizing).

3. **Bootstrapping.** STRADDLE would need ≥ 200 shadow-mode fires before live to validate the realized/implied gap is a real edge and not a vol-of-vol-driven illusion.

**Honest assessment.** Lowest-priority of the eight. KXBTC15M's `BTC + 5c` strike grid means YES + NO usually sums to 95-100c (no real arbitrage room), and the vol surface for 15-min BTC binaries is unlikely to be cleanly tradeable without exchange microstructure access we don't have.

---

### Archetype 6 — Statistical Mean-Reversion Systems

**Concept.** Regression model estimates fair probability from BTC + time + vol; enter when market deviates by ≥ N pp; exit when the gap closes.

**Engine capability today.** **THIS IS BB_PURE.** Brownian-Bridge fair value (`price_feed.py:346-505`) + edge gate (`BB_PURE_MIN_EDGE_PP = 8.0`) + Kelly sizing (`BB_PURE_KELLY_FRACTION = 0.25`, tier-stratified caps in `BB_PURE_KELLY_TIER2/3_*`) + `_maintain_protective_order` exits to fair value.

**Status.** Off since 2026-05-04 19:55. Not because the math was wrong, but because the live realization had a chain of failures:
- `_uc()` config-cache bug (config edits don't take effect without restart) made parameter tuning treacherous.
- Multi-fire-per-window experiments (`BB_PURE_MAX_FIRES_PER_WINDOW = 99`) interacted badly with the residual reconciler.
- Exit-path hijacks (VWAP_EXIT, MRC_FORCE_EXIT, DOMINANT_UPGRADE) all gated on `_open_position is not None`, applying wrong exit logic across strategies.
- The strategic reset to DIRECTION removed all of these failure surfaces by going single-strategy + hold-to-expiry.

**The brief's claim** that "predicted probability accuracy at <40% bucket was 16.2% (well-calibrated)" is consistent with BB_PURE's track record on extreme entries. The mid-range was poorer because mid-range BB_PURE entries fight the most randomness per pp of edge.

**What it would take to revive.**
1. Fix the equity-hours annualization in the vol estimator (see archetype 5).
2. Wire MRC `recommended_tp_multiplier` into the TP recompute (this *was* wired; verify still intact).
3. Decide on exit policy: held-to-expiry like DIRECTION (eliminates exit hijacks but caps profit at $1 - entry), or restored protective-order path with the four-tier exit overlay from `RESEARCH_INACTIVE_COMPONENTS_FOR_EXIT_MANAGEMENT.md`.
4. Validate against current backtest corpus side-by-side with DIRECTION. The brief notes "most profitable long-term systems probably live here" — but DIRECTION's measured 90.5% WR on n=197 is a high bar, and the comparison should be empirical, not theoretical.

**The infrastructure is intact** — `bb_pure.py` is in the tree, the config flags are still valid, the supporting modules (`prob_engine`, `protective_math.py`, `sell_safety.py`) are all in place. Re-arming is one flag flip plus regression testing.

---

### Archetype 7 — Cross-Market Arbitrage

**Concept.** Compare Kalshi vs Polymarket vs Deribit implied vol vs perpetual funding. Trade the gap.

**Engine capability today.**
- `WALLET_COPY_ENABLED = False` — the prior CROSS_VENUE_FLOW (Polymarket smart-wallet scoring → Kalshi execution) is fully retired. Per CLAUDE.md, "the strategic reset deliberately removed it" along with Polymarket API dependency and the wallet-scoring DB.
- The wallet-scoring code is still in the tree (`WALLET_COPY_*` configs at `user_config.py:305-393` are extensive) but the polling loops are not running.
- No Deribit / perpetual-funding ingestion exists.

**Gap.** All cross-market signals are dark. The brief's claim that "CVF could re-enable as a confirmation signal" is technically true but operationally significant — Polymarket-API dependency adds an external failure mode the strategic reset deliberately avoided.

**Implementation path.**

1. **Lowest-cost re-arming.** Re-enable Polymarket /trades polling in shadow mode only — log smart-wallet directional bias to a new SQLite table without using it for any decision. Validate that the wallet pool is still useful (wallets tracked in 2026-04 may have changed behavior or churned out).

2. **Confirmation overlay (later).** Once shadow data confirms wallets still lead Kalshi by a useful margin, gate DIRECTION fires on `wallet_directional_bias` agreeing. **Critical:** don't make DIRECTION *require* wallet agreement (would thin fires too much) — make it *upgrade conviction* (e.g. multiplier × 1.3 when wallets agree, × 1.0 otherwise).

3. **Standalone CVF tier (much later).** Only after confirmation overlay proves wallet signal is stable. The retired CVF code is the template, but should be re-implemented inside DIRECTION-style architecture (IOC + hold to expiry), not the original protective-order architecture.

**Honest assessment.** The brief's framing is right that wallet flow is information-rich, but operational complexity (Polymarket auth, scoring DB, wallet-pool maintenance) is real. Probably worth doing only once the microstructure additions (archetype 4) have been validated and the engine is confidently running multi-tier.

---

### Archetype 8 — Penny Contract Reversion Hunters

**Concept.** Buy 2-5c contracts. Most expire worthless; one violent BTC wick prints 20-50× per win. Statistical coverage strategy.

**Engine capability today.**
- `MIN_ENTRY_CENTS = 35` (line 140 of `user_config.py`). This **explicitly excludes** the penny-contract zone. The current engine cannot enter sub-35c contracts on the DIRECTION path.
- BB_PURE had `BB_PURE_MIN_ENTRY_CENTS = 5` (line 604) — would enter as low as 5c if mispriced. So the BB_PURE-era infrastructure could buy pennies, but BB_PURE is off.
- DIRECTION's IOC + hold-to-expiry pattern is *naturally suited* to penny mode (no exit logic needed, payoff is structurally $0 or $1) but the entry filter is wrong — DIRECTION fires only when BTC is past strike, which makes that side expensive, not cheap.

**Gap.** No "buy the side that BTC just moved *away from*" mode. Penny mode requires inverse logic to DIRECTION: fire on the *losing* side near expiry on the bet that BTC reverses.

**Implementation path (this is the brief's "highest-value addition" candidate).**

1. **`PENNY_MODE` flag, off by default.** Activates only when:
   - `mid_cents <= 8`
   - `seconds_to_expiry > 60` (need time for a wick) AND `seconds_to_expiry < 360` (any earlier and the contract isn't truly "left for dead")
   - BTC is within 0.05% of strike OR the *opposite* side of strike (the contract is genuinely underdog)
   - Daily-fire counter < N (statistical strategy needs many small bets, but engine should still cap exposure)

2. **Sizing.** Smaller per-fire dollar risk than DIRECTION because hit rate is structurally low (5-15%). E.g. `PENNY_MODE_MAX_DOLLARS_PER_FIRE = 1.0`. With 5c contracts that's 20 contracts; with 3c contracts, 33. Multiple fires per day across many windows for statistical coverage.

3. **No exits.** Hold to expiry by construction. The contract is either $1 (huge win) or $0 (fully expected loss).

4. **Coexists with DIRECTION.** The per-window ticker lock (`MAX_TRADES_PER_SESSION_TICKER = 1`) currently allows one entry per ticker. Either:
   - Lift the lock for PENNY_MODE specifically (allows DIRECTION + PENNY in the same window), with a separate per-strategy ticker tracker. Adds reentry surface.
   - Keep the lock global, accept that PENNY_MODE only fires when DIRECTION skipped (which is most of the time, since DIRECTION needs 0.10% BTC distance — penny opportunities are precisely when BTC didn't make that move).

   The second option is much safer and probably right. PENNY_MODE *naturally* fills the gaps DIRECTION leaves.

5. **Validation.** Backtest before live: how often does a 5c contract settle at $1? Need a realistic hit rate to justify Kelly sizing.

**Honest assessment.** This is a credible addition because the strategy *complements* DIRECTION (operates in opposite microstructure regimes) and inherits DIRECTION's no-exit-orders simplicity. It also doesn't compromise the DIRECTION thesis — they don't fight each other. **This is probably the cleanest single-strategy addition the engine could make next.**

---

## 2. Synthesis

### Which archetypes does the engine already do well?

**Archetype 3 (Momentum Continuation) — done, validated, live.** DIRECTION at 90.5% WR / +$3.93 mean / +$331 corpus on n=197 is the engine's load-bearing strategy. The brief's "avoid #3" advice was correct under BB_PURE's contrarian thesis but is precisely backwards under the current architecture.

**Archetype 4 infrastructure — built but unused for entries.** `microstructure.py`, `tape_pressure.py`, `contract_sr.py`, `_detect_wall_consumption`, `regime.py` all run every cycle and are not consumed by any active strategy. This is the highest-leverage code-already-present opportunity.

**Archetype 6 infrastructure — intact, retired.** BB_PURE's full stack is in the tree and could be re-armed in a single config flip. Whether it *should* be re-armed is a separate question (DIRECTION's hold-to-expiry simplicity is part of why it works).

### Highest-value additions

In priority order:

1. **Microstructure-confirmation overlay on DIRECTION (archetype 4, confirmation tier).** Lowest-risk addition. Uses already-running `MarketPressure.last_score` to gate DIRECTION fires on microstructure agreement. Shadow-log first; can only thin fires, never add them — so worst-case impact is bounded. **Single-line guard in `_direction_tick`.**

2. **Wall-consumption pre-emptive exit on DIRECTION (archetype 4, exit tier).** Today DIRECTION's only "exit" is settlement. Adding "if opposing-side `AGGRESSIVE_BUY` sustained 5s, market-sell at `bid - 1`" could materially improve drawdown control without compromising the held-to-expiry thesis on the 80%+ of trades where this signal never fires.

3. **PENNY_MODE (archetype 8).** Asymmetric statistical hedge. Operates in DIRECTION's blind spot (close-to-strike, no-momentum windows). Inherits DIRECTION's no-exit simplicity. Uncorrelated win timing.

4. **Rolling 5-min tracker (DIRECTION refinement).** Already on the known-gaps list in CLAUDE.md. Replaces session-open proxy with proper `tick_tracker` integration. Improves entry quality on a strategy that's already +EV.

5. **Regime gate on DIRECTION.** Skip fires when `regime == CHAOTIC`. One-line consultation of an already-computed signal.

### What to avoid

The brief's "avoid archetype 3" advice is wrong-given-current-state. **The actual archetype to avoid right now is #6 (Statistical Mean-Reversion / re-arming BB_PURE)** — not because the math is wrong, but because:

- DIRECTION is working and shipping +EV. Re-introducing a contrarian strategy alongside reintroduces the exit-path-hijack risk that motivated DIRECTION's `_direction_position` separation.
- BB_PURE's previous failure modes (config cache, residual reconciler, multi-strategy exit gates) are not architectural fixes — they are workarounds. Bringing BB_PURE back will resurface them.
- If BB_PURE is ever re-armed, it should run *strictly in shadow mode* (logging signals to a new table, taking no live actions) for ≥ 30 days while DIRECTION operates as primary, before any live activation.

Archetype 5 (volatility-regime trading) is also probably not worth pursuing at this scale — KXBTC15M's strike-grid economics and the lack of clean implied-vol observability make it the lowest expected-value of the eight.

### How exit research / MRC / micro-timeframes support each archetype

The brief assumes "5 exit enhancements + MRC + micro-timeframes" sit on top of an active mean-reversion engine. **Most of that does not apply to the current engine** because DIRECTION places no exit orders.

Re-mapping per current state:

| Component | DIRECTION (live) | PENNY_MODE (proposed) | BB_PURE (if re-armed) | Microstructure tier (proposed) |
|---|---|---|---|---|
| `contract_sr` resistance levels | irrelevant (no TP) | irrelevant (no TP) | TP cap (high value) | possible TP for active microstructure entries |
| `shadow_edge` composite | thesis-break exit (medium value) | irrelevant | thesis-break SL escalation (high value) | thesis-break exit |
| `MarketPressure` / `kalshi_lag` | confirmation gate + thesis-break exit | irrelevant | hold-past-TP signal (high value) | core entry input |
| `_detect_wall_consumption` | **highest-value DIRECTION exit add** | irrelevant | exit signal (high value) | core entry + exit input |
| MRC `path_signature / should_force_exit` | held-to-expiry; only `convergence_rate` matters (informational) | irrelevant | full TP/SL modulation (already partially wired) | exit overlay |
| MRC `micro_momentum_5s/25s` | irrelevant | irrelevant | early-reversal warning | confirmation |
| Phase 8b tape-exit gate | possible DIRECTION exit add (lower priority than wall consumption) | irrelevant | already wired in shadow; flip to live | exit |
| LATE_DOMINANT confidence | irrelevant (DIRECTION blocks entries after minute 10) | possibly an entry filter | hold-to-expiry decision in last 3 min | hold decision |
| Regime classifier | **proposed entry gate** | maybe (avoid PENNY in CHAOTIC) | exit-aggression scaling | entry gate |
| MTF / TA scorer | requires verifying candle feed still runs | irrelevant | thesis confirmation | entry confirmation |

The **single highest-value cross-archetype addition** is wall-consumption as a DIRECTION exit signal. It is built, it runs, it is the lowest-latency thesis-break in the codebase, and DIRECTION today has no exit signal at all.

### Proposed multi-strategy architecture

Honest, conservative target architecture (all changes gated on shadow-mode validation):

```
┌─────────────────────────────────────────────────────────────┐
│ DIRECTION (PRIMARY, live)                                   │
│   Entry: dist ≥ 0.10% + mom ≥ $10, IOC at ask + 3c          │
│   Confirmation: microstructure.last_score same sign         │
│   Regime gate: skip if regime == CHAOTIC                    │
│   Sizing: 3 contracts × conviction_multiplier(0.7-2.0)      │
│   Exit: hold to expiry, BUT cancel + market-sell on         │
│         opposing-side AGGRESSIVE_BUY ≥ 5s sustained         │
└─────────────────────────────────────────────────────────────┘
            │
            ↓ (if DIRECTION skipped this window AND mid ≤ 8c)
┌─────────────────────────────────────────────────────────────┐
│ PENNY_MODE (SECONDARY, asymmetric hedge)                    │
│   Entry: cheap underdog side near strike, mid ≤ 8c          │
│   Sizing: $1 max per fire, 20-30 contracts                  │
│   Exit: hold to expiry (always)                             │
└─────────────────────────────────────────────────────────────┘

(Microstructure-tier standalone entries: deferred until
 confirmation-overlay validation completes.)
(BB_PURE / mean-reversion: NOT re-armed.)
(STRADDLE / vol-regime: NOT pursued at this stage.)
(CVF / cross-market: shadow-only Polymarket polling deferred.)
```

The point of this architecture is that **DIRECTION + PENNY_MODE are non-overlapping in entry logic** (one needs BTC distance + momentum, the other needs proximity to strike + cheap mid + low momentum) and **share the same exit primitive** (hold to expiry). Adding PENNY does not put new exit-path complexity in the engine, which is the failure mode that killed BB_PURE.

---

## 3. Survival Framework — what separates survivors

Reframed against the current engine state.

**Execution timing.** DIRECTION's IOC at `ask + 3c` walks 1-2 depth tiers and either fills immediately or auto-cancels. This is the right execution model for momentum strategies — the maker-mode experiment on 2026-05-06 18:00-18:25 PT confirmed that `post_only=True` only fills on adverse selection (the market reversing into your bid). The "TP taker-convert fix" the brief alludes to was aimed at BB_PURE's protective layer; it does not apply to DIRECTION's no-exit architecture.

**Position sizing.** `DIRECTION_CONTRACTS = 3` base × `0.7-2.0×` conviction multiplier = 2-6 contracts per fire. This is intentionally small — Kalshi 15-min ask depth at typical entry prices (14c-70c) is consistently below 5-10 contracts. The 156× incident from prior memory is what motivated the conservative base size; the multiplier saturates at 2.0× even at extreme conviction. Kelly sizing exists in the BB_PURE config but is not wired to DIRECTION (DIRECTION uses a flat base × conviction model).

**Regime filtering.** `regime.py` classifier is computed but **not consulted by DIRECTION**. This is the most credible single addition — one-line gate to skip `CHAOTIC` regimes. MRC analyzer is per-position (constructed at fill) and is a *post-entry* tool; it cannot prevent a bad entry, only manage one already taken.

**Overtrading.** `MAX_TRADES_PER_SESSION_TICKER = 1` is **inviolable** per CLAUDE.md. The "1 trade per window" rule is the structural defense against re-entry compulsion that drained the account on 2026-04-22 (per memory). DIRECTION respects it; any future strategy must too.

**Liquidity conditions.** Wall-consumption detection exists. It is the engine's single sharpest read on actual microstructure but is currently unused for both entries and exits. Bringing it into DIRECTION's exit path is the highest-leverage liquidity-aware change available.

**Discipline.** Automation eliminates emotional drift. But automation has its own failure modes:
- The 2026-05-07 `ESCROW LEAK` warnings (balance dropping while engine reports `open_pos=None`) suggest accounting drift between engine state and Kalshi-side reality. This is exactly the "RECONCILE BACKFILL bug" class the brief references — not a strategy problem, an execution-accounting problem.
- The `_uc()` config-cache bug means parameter tuning requires service restart. Operators editing `user_config.py` while the engine runs see no effect — and easily mistake "no apparent change" for "change applied successfully."
- The four exit-path hijacks of `_open_position` (resolved by the `_direction_position` refactor in commit `cc07690`) showed how multi-strategy code can break in non-obvious ways when a new strategy reuses a state slot another strategy's exit logic gates on.

**The discipline failure mode that matters now is not human, it's architectural.** Adding a second active strategy (PENNY_MODE, microstructure-tier, anything) requires either replicating the `_direction_position` separation pattern (each strategy owns its own state slot, legacy exit paths cannot see it) or accepting cross-strategy interaction surface that the engine's history shows is unsafe.

---

## 4. Open follow-ups (research, not implementation)

1. Stratify DIRECTION's n=197 backtest by minute-of-window and regime to identify weak cells.
2. Quantify the `ESCROW LEAK` warnings — is balance drift a Kalshi-side fee accounting issue, an unattributed off-engine sell, or a residual from a prior failed clean-up cycle?
3. Validate that `_book_depth_history` ring buffer is populated unconditionally (regardless of which strategy is active) — prerequisite for wall-consumption being usable as a DIRECTION exit signal.
4. Verify `tf_analyzer` candle feed is still running. Determines whether MTF / TA-flip signals are practically available.
5. Decide whether the equity-hours annualization in `BinaryProbabilityEngine`'s vol estimate is real and material — affects any future revival of BB_PURE or any new vol-aware strategy.
6. Empirically measure the realized hit rate of 5c-10c KXBTC15M contracts settling at $1 across 30 days of historical data, to size PENNY_MODE realistically before any live test.

---

*End of research document. No code changes made. No flags flipped. No execution paths modified.*
