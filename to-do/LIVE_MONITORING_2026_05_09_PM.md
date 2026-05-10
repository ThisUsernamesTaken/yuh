# Live Monitoring — 2026-05-09 PM session

**Started**: 2026-05-09 13:30 PT (16:30 EST, US equity close)
**Architecture**: Single-tier MOMENTUM_SCALP with Fixes A–Q live
**Equity at start**: $71.86 (per BALANCE SNAPSHOT 13:29:55)

## Active fixes under observation

| Fix | What it does | Live evidence so far |
|---|---|---|
| **typo** | `count_filled` → `filled_count` (commit 13a9b8f) | All SCALP MARKET FILL logs since restart show real fill counts |
| **dyn-trail** | Strike-distance scaled BTC trail | Log shows `trail=$X` derived from strike distance |
| **A** | Tier-aware STARTUP RECOVERY | Boot-only — n/a this session |
| **B** | Window-flip lock preservation | Implicit — no boot collisions observed |
| **C** | SCALP MARKET collision check | Implicit — no double-stack fills |
| **D** | PROTECTIVE 404 terminal | No paralysis-loop spam in current log |
| **E** | Ghost-desync skip on boot | Boot-only — n/a this session |
| **F** | Loss-side asymmetric trail (8c) | Log will show `(loss-tight)` suffix if fired |
| **G** | LOSS-CUT INVERSE eligible | Log will show INVERSE after LOSS-CUT |
| **H** | STALE exit (5min, no profit) | Log will show `SCALP STALE` events |
| **I** | Profit-locked trail (5c) | Log will show `(profit-locked)` suffix |
| **J** | Keep direction-active on exit | Implicit — no Fix-K-style hijacks observed |
| **K** | Phantom-sell SCALP re-arm | **CONFIRMED 13:24:17** — NO 5x +36c |
| **L** | Pre-INVERSE truth check | Log will show `INVERSE ABORT (phantom-sell detected)` if fires |
| **N** | Re-arm cost-basis preservation | **CONFIRMED 13:24:17** — cost_basis=58c, bid=94c (correct) |
| **O** | Strike-crossing flip gate | Log will show `FLIP-SKIP (insufficient BTC reversal)` if fires |
| **P** | Re-arm cap (3 max) | **CONFIRMED 13:24:17** — `[1/3]` counter visible |
| **Q** | IOC SCALP exits | Log will show `EXIT-NOFILL` if IOC fails |

## Per-window trade ledger

### Window `-26MAY091630-30` (EST 16:30, opened ~13:15 PT)

- **13:15ish** entry: NO 5x @ 58c (cost basis from API check)
- **Mid-window**: bid spiked to 94c (+36c paper profit)
- **13:24:17**: SCALP RE-ARM `[1/3]`: phantom-sell on prior exit. cost_basis=58c, bid=94c, hwm=94c (+36c/ct).
  - **Inference**: a TRAIL or BTC-TRAIL fired earlier. The IOC sell phantomed (Kalshi cache lag at 94c near-the-top). Fix K re-armed correctly with real cost basis.
- **Outcome**: Likely settled NO won (BTC below strike at expiry). Position rode to settlement via NEAR-CERTAIN HOLD (bid 94c ≥ 90c threshold).
- **P&L estimate**: +$2.10 (5×($1 settlement − $0.58 entry) − fees)

### Window `-26MAY091645-45` (EST 16:45, opened 13:30 PT) ✅ PROFITABLE EXIT

- **13:29:55** BALANCE SNAPSHOT $71.86 (window flip)
- **13:30:34** SCALP MARKET FILL: NO 5x @ 53c IOC. Book: yes_ask=48, no_ask=53. Picked NO (favorite, no_ask > yes_ask). Cost ~$2.65 + slip.
- **(intra-window)** bid ran to 88c peak (+$1.75 paper profit)
- **13:34:11** SCALP TRAIL: hwm=88c bid=82c trail=**5c (profit-locked)** → exit side=NO entry=53c (+29c/contract)
  - **Fix I confirmed live**: profit-locked trail kept us tight at 5c instead of giving back full 15c phase trail
  - Without Fix I: would've held until bid hit 88-15=73c, capturing only +20c
  - With Fix I: held until bid retraced 5c from peak (88→82c), captured +29c
- **13:34:12** SCALP EXIT (TRAIL): sold 5x NO @ 82c filled cleanly. oid=2f4b0fca-59e.
  - **Fix Q confirmed live**: IOC filled immediately, no phantom, no retry
- **P&L**: +$1.45 realized (+29c × 5ct), 83% of the $1.75 paper peak captured
- **13:34:12** SCALP REVERSAL-SCORE: time=0.5 btc=0.0 book=0.0 velocity=0.4 → **total=0.24 → SAME**
  - Reversal score correctly identified bid retrace as intra-trend noise, not directional reversal
  - btc=0.0 component: BTC hadn't reversed against NO position
  - Threshold 0.50 not met → no flip
  - Fix O strike-cross gate not invoked (REVERSAL-SCORE killed it upstream)
- **13:34:12** SCALP MARKET SKIP: already entered this window (pre-IOC lock) → no re-entry attempt
- **Net outcome**: clean +$1.45 trade, single-leg, no flip into chop. Discipline observed.

### Window `-26MAY091700-00` (EST 17:00, opened 13:45 PT)

- **13:45:19** SCALP MARKET FILL: NO 5x @ 61c IOC. Book: yes_ask=40, no_ask=61.
  - **Strong NO favorite skew (21c)** — much wider than the 13:30 fill (5c skew)
  - Cost ~$3.05 + slip
- **(intra-window)** BTC ran down to $80,775 low-water-mark (NO deeply favored)
- **13:51:50** SCALP BTC-TRAIL: btc_lwm=$80775 btc_now=$80801 retrace=$26 >= trail=$25 → exit NO @ 63c (+2c/ct)
  - BTC reversed up $26 from low; dynamic trail (Fix dyn-trail) fired correctly
- **13:51:50** **SCALP EXIT-NOFILL (BTC-TRAIL): IOC 5x NO @ 63c returned 0 fills (book too thin)**
  - **Fix Q confirmed live**: IOC auto-cancelled when no match, no stale resting order, no false "sold" claim
  - Pre-Fix-Q this would have been a GTC limit at 63c rotting on the book as bid dropped
  - Will retry at new bid after 3s cooldown (Fix Q + P logic)
- BTC at $80,801, strike at $80,814 → still $13 below. Position economics: NO might still win at settlement.
- If next IOC retry fires successfully: Fix O strike-cross gate will block inverse to YES (BTC hasn't crossed above $80,839)

#### 🚨 BUG IDENTIFIED LIVE — Fix R shipped 13:54 PT

The 13:51 EXIT-NOFILL repeated **30+ times** in 130 seconds (every ~3s). Fix Q's
cooldown is just the retry interval — there was NO upper bound on how many times
the trail could re-fire from the manage cycle. Fix P caps SYNC RECLAIM re-arms
but not in-cycle trail retries. Same wasteful pattern as the user-flagged
"sells at 65 when bid is 10" loop, just with IOC instead of stale GTC.

**Fix R (commit `78ce6f0`)**: per-ticker counter on consecutive EXIT-NOFILL.
After `SCALP_MAX_NOFILL_RETRIES = 3` (default), set _scalp_exit_placed=True
permanently for the window. Trail stops firing. Position rides to settlement.

Engine restarted 13:53:52, STARTUP RECOVERY at 13:54:08 routed the NO 5x @ 61c
back to _direction_position via Fix A. Now waiting to see the NEW behavior:
- Either trail re-fires and we see `EXIT-NOFILL [1/3]` → `[2/3]` → `CAPPED [3/3]`
- Or the bid develops liquidity and the IOC fills cleanly

## Equity timeline

| Time (PT) | BAL | Δ from prev | Cumulative session |
|---|---|---|---|
| ~03:38 | $62.85 | — | start of "monitored period" |
| (mid-day churn) | $79.06 | +$16.21 | peak |
| (later drawdown) | $70.04 | -$9.02 | trough |
| (recovery) | $67.01 | -$3.03 | mid |
| 13:29:55 | $71.86 | +$4.85 | post 16:30 win |
| ~13:34 | ~$73.45 | +$1.59 | after 16:45 +$1.45 trail capture |
| 13:55:11 | $70.13 | -$3.32 | after 17:00 entry $3.05 committed |
| **13:59:55** | **$72.74** | **+$2.61** | **17:00 settled (partial fill + win)** |

**Net session window-by-window since start of monitoring**: **+$0.88** ($71.86 → $72.74)
**Net since trough**: **+$2.70** ($70.04 → $72.74)

## Patterns and anomalies (live updated)

### 13:24 PT — Fix N validation
The phantom-sell re-arm correctly preserved cost basis (58c) instead of using current bid (94c). This is the *exact* scenario user complained about earlier ("re-arm at the high then immediately STALE"). With Fix N: position correctly identified as +36c profitable, NEAR-CERTAIN HOLD took over, rode to settlement.

**Lesson**: Fix N + NEAR-CERTAIN composability is a real win. A re-armed deeply-profitable position now defaults to settlement-ride.

### 13:30 PT — Window-flip favorite picker
NO favorite at no_ask=53 vs yes_ask=48 = 5c skew. Modest favorite. Entered on this signal alone (no FVG / BB confirmation). This is the architectural concern user raised — entering on weak microstructure signal without higher-conviction directional bias.

## Open questions for design

1. **BB-as-exit signal**: At 13:24, what was `prob_engine.probability` for NO? If it was already >0.85, NEAR-CERTAIN at bid 94c was redundant — BB drift would've held earlier. Need to start logging `prob_at_entry` and `prob_now` for each position.
2. **First-directional-move detection**: Could we delay the 13:30:34 entry by 60-180s to confirm direction? At 13:30:34 the favorite was NO at 53c — would BTC's first 60s have confirmed or contradicted that?
3. **FVG inventory**: Where are the unfilled 1m/5m FVGs right now? Is the NO 53c entry aligned with FVG-fulfillment direction or against?
4. **Continuous-evaluator vs one-shot decision (CRITICAL ARCHITECTURAL GAP)**:
   Identified live 13:34 PT 2026-05-09. After SCALP exit fires, REVERSAL-SCORE
   evaluates at that exact instant. If score < 0.50 → no flip, window-lock
   active for remainder of window, engine cannot re-engage even if a real
   reversal develops 60s/120s/5min later.
   - Real reversals develop OVER TIME — not visible at second 0 of move
   - Current design treats "reversal" as a binary moment-in-time decision
   - User observation: "We missed this reversal nonetheless"
   - The FVG+BB module must be a CONTINUOUS evaluator: every tick checks
     {FVG inventory, BB drift, book reaction, BTC velocity} for fresh entry
     signal — not just at window open or exit moment
   - Multiple entries per window allowed if multiple directional moves
     develop (chop sessions are common; double-reversal windows do happen)
   - Exit fires when *signal* degrades, not when *bid* retraces by N cents

## Forensic backlog (research after session)

- Pull `kalshi_trades` table for tonight's settled tickers, compute realized vs paper P&L per fix
- Compare Fix-Q IOC exits to pre-Q GTC exits — fill rate, slippage
- BB probability trajectory for the 13:24 NO position (was the +36c run BB-predicted?)
- 4 PM EST volatility regime: did realized vol spike/contract at the close?

---

*This file is updated live as events arrive via Monitor task `ble9jk23q`.*

---

## Session findings — 13:30 to 17:45 PT (4h 15m)

### Equity arc

| Time | BAL | Notes |
|---|---|---|
| 13:29 | $71.86 | Start of monitoring |
| 14:00 | $74.07 | +$2.21 (3 wins in a row, peak) |
| 14:30 | $71.96 | -$2.11 (4c-skew NO loss) |
| 15:00 | $70.13 | -$1.83 (Fix R cap saved one) |
| 15:30 | $68.48 | -$1.65 |
| 16:00 | $70.48 | +$2.00 (clean +$1.35 IOC fill on 19:00 NO) |
| 16:30 | $64.16 | **-$6.32 (catastrophe: 2 BORED-confident YES losses in trending market)** |
| 17:00 | $63.53 | -$0.63 |
| 17:30 | $62.80 | -$0.73 (Fix R cap loss) |
| 17:45 | $65.01 | +$2.21 (Fix R cap save) |
| **Net** | **-$6.85** | **Roughly -10% of starting BAL** |

### What worked

1. **Fix I (profit-locked trail)** — captured +$1.45 on the clean 16:45 trade
2. **Fix Q (IOC exits)** — eliminated stale resting orders. The third-attempt fill at 16:53 was a true execution success
3. **Fix R (cap on retry spam)** — bounded the no-liquidity loops at 3 attempts, prevented unbounded API spam
4. **bored_signal.py shadow integration** — produced real validation data within hours of shipping
5. **prob_engine.update() fix** — surfaced a long-dormant dead instrument bug; BB now actually computes probability

### What didn't

1. **BORED's BB-only conviction breaks in trending markets**
   - 16:00 YES @ 69c (38c skew, BB 0.78): LOST -$3.45 (BTC trended down hard)
   - 16:15 YES @ 54c (7c skew, BB 0.89, BORED said HOLD): LOST -$2.70 (same trend continued)
   - Two consecutive trend-violation losses on BB high-conviction holds
   - BB models calibrated for vol-scaled mean-reversion; real BTC trends override

2. **Execution friction kills good signals**
   - 4 separate windows where SCALP correctly identified BTC reversal but IOC couldn't fill at the favorable price
   - "Book too thin at exit price" is structural — when our side reverses, everyone wants to exit, no buyers at our bid
   - Fix R caps prevent infinite spam but don't rescue the position; outcome reduces to settlement variance
   - Net: trail signals are ~50% paper-correct but ~25% realized due to fill failure

3. **Weak-skew entries are noise**
   - 1c-skew NO @ 51c WON (+$2.45), but ex-ante the entry was a coin flip
   - 4c-skew NO LOST -$2.78 earlier in session
   - SCALP fires on any skew >0; should have a min-skew filter (≥10c?)

### v2 spec for the signal layer

Based on tonight's data, the right architecture is **NOT BORED-replaces-SCALP**. It's:

```
ENTRY:
  - SCALP MARKET fires only if skew >= 10c (filter weak signals)
  - AND BORED.evaluate() says action=enter (multi-source confluence + conviction floor)
  - Both must agree

EXIT:
  - SCALP's BTC-TRAIL with dynamic strike-distance scaling (existing Fix dyn-trail)
  - PLUS: hard velocity-override — if BTC moves $50+ against position in past 60s,
          force-exit regardless of BB conviction (no near-certain hold)
  - PLUS: BORED's BB-drift exit when own_prob drops > 0.20 from entry

EXECUTION:
  - Replace IOC with progressive sell ladder:
    - Place at bid for 2s → if no fill, walk down 1c → repeat 5x → market sell
    - Better fill rate at modest slippage cost
  - OR: pre-place TP at entry+8c on every fill so the market comes to us

SAFETY:
  - Keep all current Fix A-R (J/K/L/N/P/Q/R) as the safety layer
  - Vol regime gate: if realized vol > 30%, switch to "trend mode" — no near-certain holds
```

### Honest verdict on tonight's work

Built and shipped:
- 1 new module (bored_signal.py, 267 lines)
- 26 unit tests (all passing)
- Engine integration in shadow mode
- 5 surfaced bugs/issues fixed (count_filled typo earlier, prob_engine dead instrument, etc.)

Did NOT achieve:
- Net positive session (-$6.85)
- Validated edge for either signal model in isolation

Discovered:
- The signal layer alone isn't the bottleneck. Execution friction is at least as costly.
- The right design is hybrid: BORED's selectivity + SCALP's execution + new velocity-override + better exit ladder
- A "trade once if can't exit" rule (user's stated preference) is partially implemented via Fix R but doesn't extend to the scenario where the trail fires multiple times throughout a window

The bored_signal module is shipped, tested, and instrumented. The shadow data over the next 24-48 hours will give us the validation we need to design v2. Tonight is data collection complete, not deployment.

