# Dynamic Profit Protection — Implementation Spec
**Generated**: 2026-04-02
**Author**: Deep analysis of `polymarket_copy_engine.py`, `kalshi_trades` (1,406 trades), and existing docs
**Status**: Design spec — ready for implementation after Phase 5 validation is underway
**Implements**: Phase 6 of IMPLEMENTATION_GUIDE.md

---

## 1. Problem Statement

The engine's exit philosophy is **hold-to-expiry by default**. The data confirms this is correct on average: `won` trades (held to expiry) return +$0.97/trade average vs +$0.45/trade for `exited_win` (mid-session exits). Early TP exits systematically leave money on the table.

However, hold-to-expiry has one catastrophic failure mode: a position that was deeply profitable during the window, hit a high-water mark of, say, 78c, then BTC reversed sharply in the final 2 minutes and the contract expired at 0c. The engine held through the entire reversal and booked a full loss instead of a large gain. This is not a theoretical risk — binary contracts regularly exhibit sharp mean-reversions in the final 90 seconds as market makers collapse spreads and price discovery completes.

**Three specific problems the current system has no answer to:**

1. **Fixed TPs leave money on the table on strong trends.** The current tiered TPs at 63c/70c (breakeven + remainder) are set at entry time and don't adjust to mid-session momentum. A contract that runs to 88c during the window has only its 70c limit order as defense — if it reverts to 55c before that order fills, the engine holds all the way back down.

2. **The static trailing stop (disabled) doesn't account for time.** The existing trailing stop code at line 4802 (`if False and effective_stop > 0`) used a fixed 5-8c trail regardless of time remaining. A 5c trail with 12 minutes left is overly tight (noise will stop you out). A 5c trail with 90 seconds left is overly wide (a 5c gap is 10+ seconds of open interest settlement flow at window close).

3. **No distinction between "position that never moved" and "position that ran +20c then reversed."** Both are held identically to expiry. The second case is the one that needs protection.

**The specific failure mode to protect against:**

```
Entry: YES @ 52c, 20 contracts = $10.40 at risk
Minute 6: contract at 74c → HWM = 74c, unrealized profit = +$4.40
Minute 13: BTC spike reverses, contract at 40c
Expiry: YES settles NO → pnl = -$10.40
Loss: gave back $4.40 gain + took $10.40 full loss
With trail: would have exited at ~68c in minute 13 → pnl = +$3.20
```

The mechanism needed: **let winners run, but lock profit when BTC shows reversion** — specifically, when the contract drops significantly from its high-water mark in the later phases of the window.

---

## 2. Current Exit Logic Audit

### 2.1 The `_manage_position` Function

**File**: `polymarket_copy_engine.py`
**Function**: `_manage_position` starting at line 4103

The function runs every 3 seconds (the main poll loop). It operates on `self._open_position` (a dict). Key position fields relevant to this spec:

```python
pos = self._open_position
pos["entry_cents"]       # Entry price in cents (e.g., 52)
pos["side"]              # "yes" or "no"
pos["count"]             # Contracts held
pos["fill_time"]         # time.time() at fill (for grace period calculation)
pos["high_water_bid"]    # Highest bid seen since entry — already tracked!
pos["had_flow_at_entry"] # True if wallet signal confirmed entry
pos["tier"]              # "PRIMARY", "TREND_FOLLOW", "TA_FORCED", "SYNCED", etc.
```

### 2.2 Exit Waterfall (Priority Order)

The function executes these checks in order, returning early when an exit fires:

| Priority | Check | Lines | Condition | Status |
|---|---|---|---|---|
| 0 | Kalshi position sync | 4113–4191 | Every 30s, detect ghost positions | **ACTIVE** |
| 1 | Tiered TP fill check | 4200–4261 | Poll all `tp_order_ids` for fills | **ACTIVE** |
| 2 | Contract expired | 4263–4275 | `find_btc_contracts` returns nothing | **ACTIVE** |
| 3 | Hard stop (-40% of entry) | 4294 | `if False` — **NEVER FIRES** | Disabled |
| 4 | MTF Reversal Exit | 4317–4351 | `is_opposing AND score >= 0.5` | **ACTIVE** (but MTF shadow) |
| 5 | TP Decay Exit (<5min, profitable) | 4361–4383 | `minutes_left <= 5.0 AND bid > entry` | **ACTIVE** |
| 6 | TP Decay Lower (<7min, profitable) | 4386–4401 | `minutes_left <= 7.0` — lowers limits to entry+5c | **ACTIVE** |
| 7 | Mandatory Exit (<3min, underwater) | 4403–4425 | `bid <= entry AND minutes_left <= 3.0` | **ACTIVE** |
| 8 | Old time exit block | 4427–4458 | `if False` — **NEVER FIRES** | Disabled |
| 9 | Smart flow elite flip | 4474–4632 | `weighted_flip = False` — **NEVER FIRES** | Disabled |
| 10 | Hold if flow supports | 4634–4682 | `flow_supports = True` → HWM update + hold | **ACTIVE** |
| 11 | BTC deviation stop | 4691–4727 | `DEVIATION_STOP_ENABLED = False` | Disabled |
| 12 | Wallet backstop | 4744–4778 | `if False` — **NEVER FIRES** | Disabled |
| 13 | Trailing stop | 4802 | `if False and effective_stop > 0` — **NEVER FIRES** | Disabled |

### 2.3 High-Water Mark (HWM) — Already Partially Tracked

`high_water_bid` already exists in the position dict (lines 662, 1560, 3548, 4165, 4562). However, it is **only updated in two places**, both conditional:

- **Line 4638–4640**: Inside `if flow_supports:` block — only updated when smart flow data is present and supporting the position.
- **Line 4806–4808**: Inside the `if False and effective_stop > 0:` disabled trailing stop block — never runs.

**Gap**: When `flow_has_data = False` (wallets silent, flow stale), the HWM is never updated. The new mechanism must update HWM unconditionally at the start of each management cycle.

### 2.4 `minutes_to_expiry` — The Correct Attribute

After the 2026-03-28 bug fix (CLAUDE.md Known Issue #7): **`contract.minutes_to_expiry`** is the correct attribute (line 4278). `contract.minutes_remaining` does not exist and caused silent AttributeError for days. Any new code must use `minutes_to_expiry`.

### 2.5 Data: What Each Exit Type Produces

From `kalshi_trades` (1,406 trades, 2026-03-13 to 2026-04-02):

| Status | N | WR% | Avg P&L/trade | Total P&L | Notes |
|---|---|---|---|---|---|
| `won` | 561 | 99.8% | +$0.97 | +$542.78 | Held to expiry, settled favorably. Avg entry 54.4c |
| `lost` | 601 | 0.0% | -$0.84 | -$502.65 | Held to expiry, expired worthless. Avg entry 41c |
| `exited_win` | 88 | 73.9% | +$0.45 | +$39.46 | Mid-session exits. Avg entry 35.1c (mostly legacy low-entry) |
| `exited_loss` | 64 | 0.0% | -$1.46 | -$93.49 | Mid-session loss cuts |
| `stopped` | 12 | 0.0% | -$2.80 | -$33.59 | Hard stops (older periods) |

**Key observation**: Hold-to-expiry wins beat mid-session exits 2:1 ($0.97 vs $0.45). This confirms the core philosophy. Do NOT design a mechanism that fires frequently — it should be a last-resort lock against reversions, not a default exit path.

**The `exited_win` gap explained**: The 88 `exited_win` trades average $0.45/trade but represent legacy behavior (avg entry 35.1c, which is below current MIN_ENTRY_CENTS=40). At 35c entry, a +7c exit = 42c = only 20% of max payout. The relevant comparison for the current entry band (40-82c) is harder to measure since there are few `exited_win` trades in that band.

**CVF-only exits** (the profitable core strategy, 415 settled trades):

| Status | N | Avg P&L | Total P&L |
|---|---|---|---|
| `won` | 227 | +$0.83 | +$189.52 |
| `lost` | 158 | -$1.19 | -$187.99 |
| `exited_loss` | 16 | -$0.69 | -$11.07 |

The CVF strategy is nearly breakeven on net P&L (+$0.83 avg win, -$1.19 avg loss). The dynamic profit protection mechanism, if it converts even 10–15% of the 158 `lost` trades (where the position was profitable during the window) into small wins or smaller losses, would be transformative.

**High-entry winners that held to expiry (the preservation case):**

```
72c entry, 5 contracts: pnl = +$1.40 (contract settled at 100c)
81c entry, 7 contracts: pnl = +$1.33
74c entry, 5 contracts: pnl = +$1.30 (×2 identical trades)
73c entry, 4 contracts: pnl = +$1.08
```

These are the trades we must NOT exit early. At 72c, the contract needs to drop below ~62c before a 10c trail fires — which at minute 13 would only happen on a sharp reversal.

### 2.6 The Specific Failure Mode

The engine currently has no protection against this sequence:

```
Entry: YES @ 52c (10 contracts, $5.20 at risk)
Min 3:  bid = 58c  HWM = 58c (no trail active)
Min 7:  bid = 71c  HWM = 71c (no trail active, TP decay not yet)
Min 9:  bid = 68c  HWM = 71c (3c below HWM — normal noise)
Min 12: bid = 55c  HWM = 71c (16c below HWM — catastrophic reversal)
Min 14: bid = 35c  HWM = 71c (TP DECAY EXIT fires but at bid=35c = -$1.70)
Expiry: settled NO → pnl = -$5.20

With 5min decay exit:  exits at 35c = -$1.70 (better than -$5.20 but still bad)
With 8min trail (8c):  exits at ~63c at minute 9 = +$1.10  ← target behavior
```

---

## 3. Proposed Mechanism: Time-Scaled Trailing Profit Lock

### 3.1 Core Concept

Track the high-water mark (HWM) of the contract's bid price throughout the position lifetime. Once the HWM exceeds entry + a minimum threshold, activate a trailing stop whose distance scales with time remaining in the window:

- **Early in window (0–5 min elapsed)**: Trail effectively disabled (very wide). Moves are noise.
- **Mid-window (5–8 min elapsed)**: Wide trail. Trend still establishing.
- **Late-mid (8–12 min elapsed)**: Medium trail. Direction should be confirmed.
- **Final approach (12–14 min elapsed)**: Tight trail. Any reversal is likely real.
- **Last 60 seconds**: If profit is large enough, market sell immediately. Don't risk settlement race.

The mechanism only activates if the position was profitable enough to matter (HWM must exceed entry + threshold). Positions that never become profitable fall through to the existing hold-to-expiry logic.

### 3.2 Phase Definitions

Time is measured as **minutes elapsed since fill**, not minutes remaining. This is more robust because:
- `fill_time` is already tracked in the position dict
- It handles late-window entries correctly (e.g., entry at minute 10 puts you straight into the tight phase)
- It doesn't require a contract lookup for the phase determination (faster)

```python
fill_age_minutes = (time.time() - pos["fill_time"]) / 60.0
```

| Phase | Minutes Elapsed Since Fill | Trail Distance | Behavior |
|---|---|---|---|
| 0 (Ignore) | 0–5 min | 12c (effectively disabled) | Position just entered. Let it establish. |
| 1 (Wide) | 5–8 min | 8c from HWM | Trend developing — give it room |
| 2 (Medium) | 8–12 min | 5c from HWM | Trend established — protect half profit |
| 3 (Tight) | 12–14 min | 3c from HWM | Reversal risk high — lock most profit |
| 4 (Final) | 14+ min OR <60s remaining | immediate exit if profit > threshold | Settlement race — don't gamble |

**Why 5-minute ignore window**: Window entries happen in the first 3 seconds. The passive ladder (bid-3c / bid-5c) takes up to 20s to fill. Within the first 5 minutes, a 12c contract swing is well within normal bid-ask noise as the market discovers direction. Activating an 8c trail at minute 2 would stop out too many trades that ultimately settle favorably.

**Why 8c wide trail vs current 5c**: The existing trailing stop (disabled) used a flat 5c trail from HWM with activation at HWM >= entry + 15c. At minute 6, a 5c trail on a 60c contract is only an 8.3% band — too tight for mid-window noise. 8c (13%) is more appropriate.

**Why 3c tight trail at minute 12**: With 3 minutes remaining, a 3c drop from HWM represents a genuine shift in market expectations. At 60c, a 3c move implies the market went from 60% to 57% probability of settlement YES — meaningful signal with limited time to recover.

**Why the final 60 seconds immediate exit**: In the last 60 seconds, Kalshi market makers begin collapsing spreads rapidly. A contract that was 88c can drop to 50c in one quote update if a BTC print comes in adverse. The resting TP orders at 63c/70c may not fill if the market moves through them. A market sell at 85c guarantees profit.

### 3.3 The Near-Certain Settlement Exception

If the contract bid is above 90c, **do not trail**. At 90c bid:
- Market implies 90%+ probability of settling at 100c
- Expected value of holding: 0.90 × $0.10 + 0.10 × (-$0.90) = +$0.00 per contract at breakeven, but actual EV is positive since settlement is discrete (either 100c or 0c) and a 90c bid means 90% → 100c
- The maximum additional gain from settlement is +$0.10/contract. The maximum loss from a reversal is -$0.90/contract. At 90c+, the math says hold.

**Exception to the exception**: If the contract is at 90c+ AND there is less than 90 seconds remaining, and the HWM is ≥ 92c, trail tightens to 2c because the reversal risk concentrates entirely in the final print.

### 3.4 Configuration Parameters

These belong in `user_config.py` under a new `# ── Profit Lock ──` section:

```python
# ── Profit Lock (Time-Scaled Trailing Protection) ──────────────────────────
# Dynamic trailing stop that tightens as the window approaches expiry.
# Only activates when HWM exceeds entry by PROFIT_LOCK_MIN_PROFIT_CENTS.
# If never activated (position never profitable enough), holds to expiry.
PROFIT_LOCK_ENABLED = True

# Minimum profit in cents before trail activates.
# HWM must exceed entry by this much. Prevents trailing on marginal profits.
PROFIT_LOCK_MIN_PROFIT_CENTS = 8       # 8c min profit before trail watches

# Trail distances by elapsed-time phase (cents from HWM to trigger exit)
PROFIT_LOCK_TRAIL_PHASE_0 = 12        # Minutes 0-5: effectively disabled
PROFIT_LOCK_TRAIL_PHASE_1 = 8         # Minutes 5-8: wide trail
PROFIT_LOCK_TRAIL_PHASE_2 = 5         # Minutes 8-12: medium trail
PROFIT_LOCK_TRAIL_PHASE_3 = 3         # Minutes 12-14: tight trail

# Phase breakpoints in minutes elapsed since fill
PROFIT_LOCK_PHASE_0_END_MIN = 5.0
PROFIT_LOCK_PHASE_1_END_MIN = 8.0
PROFIT_LOCK_PHASE_2_END_MIN = 12.0
PROFIT_LOCK_PHASE_3_END_MIN = 14.0

# Final 60 seconds: market sell if unrealized profit exceeds this threshold
PROFIT_LOCK_FINAL_60S_THRESHOLD = 15  # cents of profit, relative to entry
PROFIT_LOCK_FINAL_60S_ENABLED = True

# Near-certain settlement: if bid above this, DON'T trail (let it settle)
PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD = 90  # cents
```

---

## 4. Edge Cases

### 4.1 HWM hit at minute 2, price consolidates

**Scenario**: Contract runs to 68c in minute 2 (HWM = 68c, profit = +16c), then trades flat at 62-65c until minute 12.

**Behavior**:
- Minutes 0-5: Trail = 12c. Trigger at 68c - 12c = 56c. Price at 62-65c → no exit.
- Minutes 5-8: Trail = 8c. Trigger at 68c - 8c = 60c. Price at 62-65c → no exit.
- Minutes 8-12: Trail = 5c. Trigger at 68c - 5c = 63c. Price at 62c → **exits at 62c**.

At minute 8 transition, price is 2c below the 5c trail trigger. If price ticks down one more cent to 62c, the trail fires. This is the correct behavior — consolidation without continuation is a warning sign with 4-7 minutes left.

**Improvement**: If you want to let consolidations breathe longer, raise `PROFIT_LOCK_PHASE_2_END_MIN` from 12 to 13, giving an extra minute of 5c trail.

### 4.2 Sharp V-reversal at minute 11

**Scenario**: Contract at 71c (HWM = 75c) at minute 11. BTC print drops contract to 60c in two ticks.

**Behavior**:
- Phase 2 (medium trail): trigger at 75c - 5c = 70c. Drop to 60c → 15c below HWM → **exits at 60c** (not at trigger; market sell fires on next poll when bid < trigger).
- Since polling is 3-second interval, there may be one cycle of lag. Worst case: exits at 58c instead of 60c.

**Note**: A 15c drop in two ticks is a flash crash scenario. The trail cannot protect you from gaps — it can only protect you from the next poll cycle. See Section 4.5.

### 4.3 Contract at 95c with 2 minutes left

**Scenario**: Contract bid = 95c, entry = 55c, 2 minutes remaining.

**Behavior** (`PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD = 90`):
- 95c > 90c threshold → trail is **disabled**.
- `PROFIT_LOCK_FINAL_60S_ENABLED` would fire if we're in the last 60s AND profit > 15c. At 95c with 55c entry, profit = 40c > 15c threshold.
- With 120 seconds left: NOT in the last-60s window yet → hold.
- With 50 seconds left: last-60s window → fires? Check: `bid (95c) > PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD (90c)` → override: **do not exit**. At 95c, settlement is 95% likely at 100c. The expected additional gain from holding ($0.05 × 0.95 = $0.0475/contract) outweighs the cost of a market sell vs settlement.

**Recommendation**: Add a secondary check: if `bid >= PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD`, skip the final-60s exit entirely and let Kalshi settle it.

### 4.4 Position is losing (bid < entry)

**Behavior**: `profit_cents = bid - entry < 0`. The HWM never exceeded `entry + PROFIT_LOCK_MIN_PROFIT_CENTS`. The trail is never activated. This mechanism does nothing — the existing mandatory exit at `minutes_left <= 3.0 AND bid <= entry` handles the loss case (line 4403–4425).

**Explicitly**: The profit lock is **only a winner protection mechanism**. Losers are handled by existing code. No change to loss behavior.

### 4.5 Flash crash (contract drops 20c in one 3-second poll)

**Scenario**: Between two poll cycles, a market event drops the contract 20c.

**Behavior**: On the next poll, `bid` is 20c below HWM. If the trail threshold is 5c, the trail fires at `bid` — but `bid` is now 15c below the trigger. The exit is a market sell at current bid.

This is the correct behavior. A trailing stop cannot guarantee exit at the trigger price; it guarantees an exit as soon as the condition is detected. The only mitigation is poll frequency (3s currently). Given this is an asyncio loop, the actual lag is bounded by the orderbook fetch latency (~100ms) plus the loop iteration time.

**There is no protection against gaps larger than the trail distance between polls.** This is acceptable — it's the same limitation as all trailing stops.

### 4.6 Late entry (entered at minute 11)

**Scenario**: Signal fires late. Fill happens at minute 11 (fill_age = 0).

**Behavior**: Phase 0 is active for 5 minutes. But there are only ~4 minutes left in the window. The trail never exits Phase 0. This means: for late entries, the profit lock is effectively disabled and the position holds to expiry (or the mandatory exit fires at minute 13+3 = minute 14).

**This is intentional and correct.** Late entries have the least time for reversals to compound. The risk of a harmful trail exit is highest when the trail is too tight relative to noise. For a 4-minute position, hold-to-expiry is the right default.

**Optional enhancement** (not in Phase 6 scope): For very late entries (<3 minutes remaining), the Phase 3 tight trail (3c) could apply immediately. This would protect the specific case of a late entry that runs 5c, then gets reversed in the final 30 seconds.

---

## 5. Implementation Plan

### 5.1 HWM Update — Make It Unconditional

**Current behavior**: HWM is updated only inside `if flow_supports:` at line 4638–4640. If flow data is absent (wallets quiet, stale data), HWM freezes.

**Fix**: Add HWM update immediately after `bid` is fetched at line 4280, before any conditional logic. This gives the profit lock an always-accurate HWM regardless of flow state.

**Insertion point**: After line 4283 (`profit_cents = bid - entry`), add:

```python
# ── HWM update (unconditional — profit lock needs accurate peak regardless of flow) ──
_hwm = pos.get("high_water_bid", entry)
if bid > _hwm:
    pos["high_water_bid"] = bid
    _hwm = bid
```

**Note**: The existing HWM update at line 4638–4640 (`if flow_supports:`) can stay — it's redundant but harmless.

### 5.2 Phase Determination Function

Add this as a standalone function in the file, near the other helper methods (around line 3861, near `_place_tiered_tp`):

```python
def _profit_lock_phase(self, fill_time: float) -> tuple[int, int]:
    """Return (phase_number, trail_distance_cents) for the profit lock.

    Phase 0: 0-5 min  — trail = 12c (effectively disabled)
    Phase 1: 5-8 min  — trail = 8c  (wide)
    Phase 2: 8-12 min — trail = 5c  (medium)
    Phase 3: 12+ min  — trail = 3c  (tight)

    Returns (phase, trail_cents).
    Reads config from user_config via module-level constants.
    """
    elapsed_min = (time.time() - fill_time) / 60.0

    if elapsed_min < PROFIT_LOCK_PHASE_0_END_MIN:
        return 0, PROFIT_LOCK_TRAIL_PHASE_0
    elif elapsed_min < PROFIT_LOCK_PHASE_1_END_MIN:
        return 1, PROFIT_LOCK_TRAIL_PHASE_1
    elif elapsed_min < PROFIT_LOCK_PHASE_2_END_MIN:
        return 2, PROFIT_LOCK_TRAIL_PHASE_2
    else:
        return 3, PROFIT_LOCK_TRAIL_PHASE_3
```

**Imports needed**: None — uses `time.time()` (already imported) and config constants (already loaded from `user_config`).

### 5.3 Main Check — Insertion Point in `_manage_position`

**Where**: After the TP Decay Lower block (line 4401) and before the Mandatory Exit (line 4403). This is the right point in the waterfall because:

- TPs have been checked (if they filled, we've already returned)
- The TP decay logic has already lowered limits or exited if within 5-7min and profitable
- We're now in the zone where the position is still live and profitable-but-uncertain
- The mandatory exit handles the loss case below

**Full block to insert** (replacing the comment gap between line 4401 and 4403):

```python
            # ── PROFIT LOCK: Time-scaled trailing stop for profitable positions ──
            # Only fires when HWM > entry + threshold. Protects gains without
            # cutting positions that never became meaningfully profitable.
            if (PROFIT_LOCK_ENABLED
                    and bid > 0
                    and count > 0
                    and _hwm >= entry + PROFIT_LOCK_MIN_PROFIT_CENTS):

                # Skip if near-certain settlement (contract at or above 90c)
                _near_certain = bid >= PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD

                # Final 60 seconds: immediate exit if profit is large
                if (PROFIT_LOCK_FINAL_60S_ENABLED
                        and minutes_left <= 1.0
                        and not _near_certain
                        and profit_cents >= PROFIT_LOCK_FINAL_60S_THRESHOLD):
                    logger.warning(
                        "CopyEngine PROFIT LOCK FINAL 60S: %s entry=%dc bid=%dc hwm=%dc "
                        "profit=+%dc | %.1f min left — market sell to lock profit",
                        pos["side"].upper(), entry, bid, _hwm, profit_cents, minutes_left,
                    )
                    await self._cancel_tp_order()
                    try:
                        sell_order = await self._client.place_order(
                            ticker=pos["ticker"], side=pos["side"],
                            price=bid, count=count, action="sell",
                        )
                        sold = sell_order.filled_count if sell_order else 0
                        if sold > 0:
                            _lock_pnl = profit_cents * sold / 100.0
                            await self._record_trade_outcome(
                                _lock_pnl, True, pos["side"], "won"
                            )
                            self._closed_tickers.add(pos["ticker"])
                            self._open_position = None
                            self._record_daily_pnl(_lock_pnl)
                            self._window_locked = True
                            logger.info(
                                "CopyEngine PROFIT LOCK EXIT: +$%.2f locked (%dx @ %dc) | "
                                "phase=final_60s hwm=%dc",
                                _lock_pnl, sold, bid, _hwm,
                            )
                            return
                    except Exception as e:
                        logger.error("CopyEngine PROFIT LOCK final-60s sell failed: %s", e)

                elif not _near_certain:
                    # Time-scaled trail check
                    _phase, _trail_c = self._profit_lock_phase(pos["fill_time"])
                    _trail_trigger = _hwm - _trail_c

                    if bid < _trail_trigger:
                        logger.warning(
                            "CopyEngine PROFIT LOCK TRAIL: %s entry=%dc bid=%dc hwm=%dc "
                            "trigger=%dc trail=%dc phase=%d | %.1f min left — exit",
                            pos["side"].upper(), entry, bid, _hwm,
                            _trail_trigger, _trail_c, _phase, minutes_left,
                        )
                        await self._cancel_tp_order()
                        try:
                            sell_order = await self._client.place_order(
                                ticker=pos["ticker"], side=pos["side"],
                                price=bid, count=count, action="sell",
                            )
                            sold = sell_order.filled_count if sell_order else 0
                            if sold > 0:
                                _lock_pnl = profit_cents * sold / 100.0
                                _is_win = _lock_pnl > 0
                                _status = "won" if _is_win else "exited_loss"
                                await self._record_trade_outcome(
                                    _lock_pnl, _is_win, pos["side"], _status
                                )
                                self._closed_tickers.add(pos["ticker"])
                                self._open_position = None
                                self._record_daily_pnl(_lock_pnl)
                                self._window_locked = True
                                logger.info(
                                    "CopyEngine PROFIT LOCK EXIT: $%+.2f (%dx @ %dc) | "
                                    "phase=%d trail=%dc hwm=%dc trigger=%dc",
                                    _lock_pnl, sold, bid, _phase,
                                    _trail_c, _hwm, _trail_trigger,
                                )
                                return
                        except Exception as e:
                            logger.error("CopyEngine PROFIT LOCK trail sell failed: %s", e)
```

### 5.4 Config Constants Loading

At the top of `polymarket_copy_engine.py`, where other `user_config` values are read (around the existing `STOP_LOSS_CENTS`, `HIGH_ENTRY_STOP_THRESHOLD` block), add:

```python
# Profit lock config (from user_config with safe defaults if keys absent)
PROFIT_LOCK_ENABLED            = getattr(user_config, 'PROFIT_LOCK_ENABLED', False)
PROFIT_LOCK_MIN_PROFIT_CENTS   = getattr(user_config, 'PROFIT_LOCK_MIN_PROFIT_CENTS', 8)
PROFIT_LOCK_TRAIL_PHASE_0      = getattr(user_config, 'PROFIT_LOCK_TRAIL_PHASE_0', 12)
PROFIT_LOCK_TRAIL_PHASE_1      = getattr(user_config, 'PROFIT_LOCK_TRAIL_PHASE_1', 8)
PROFIT_LOCK_TRAIL_PHASE_2      = getattr(user_config, 'PROFIT_LOCK_TRAIL_PHASE_2', 5)
PROFIT_LOCK_TRAIL_PHASE_3      = getattr(user_config, 'PROFIT_LOCK_TRAIL_PHASE_3', 3)
PROFIT_LOCK_PHASE_0_END_MIN    = getattr(user_config, 'PROFIT_LOCK_PHASE_0_END_MIN', 5.0)
PROFIT_LOCK_PHASE_1_END_MIN    = getattr(user_config, 'PROFIT_LOCK_PHASE_1_END_MIN', 8.0)
PROFIT_LOCK_PHASE_2_END_MIN    = getattr(user_config, 'PROFIT_LOCK_PHASE_2_END_MIN', 12.0)
PROFIT_LOCK_PHASE_3_END_MIN    = getattr(user_config, 'PROFIT_LOCK_PHASE_3_END_MIN', 14.0)
PROFIT_LOCK_FINAL_60S_THRESHOLD = getattr(user_config, 'PROFIT_LOCK_FINAL_60S_THRESHOLD', 15)
PROFIT_LOCK_FINAL_60S_ENABLED  = getattr(user_config, 'PROFIT_LOCK_FINAL_60S_ENABLED', True)
PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD = getattr(user_config, 'PROFIT_LOCK_NEAR_CERTAIN_THRESHOLD', 90)
```

**Using `getattr` with defaults**: This allows the engine to start without adding the config keys immediately — defaults to safe values (PROFIT_LOCK_ENABLED=False = off by default). Add the explicit keys to `user_config.py` when ready to enable.

### 5.5 DB Schema: New Columns for Analysis

Add two columns to `kalshi_trades` to enable post-hoc analysis:

```sql
ALTER TABLE kalshi_trades ADD COLUMN hwm_cents INTEGER;
ALTER TABLE kalshi_trades ADD COLUMN exit_trail_phase INTEGER;
```

**`hwm_cents`**: The high-water mark (in cents) at the time of exit. Allows querying "how much did the position peak before it was exited?"

**`exit_trail_phase`**: Which phase the profit lock was in when it fired (0/1/2/3), or NULL if the profit lock did not fire (position expired normally). Allows measuring how often each phase triggers and at what P&L.

**Logging update in `_record_trade_outcome`**: Pass `hwm_cents` and `exit_trail_phase` as optional fields when calling `signal_logger.log_kalshi_outcome()`. This requires updating `signal_logger.py` to accept and write these fields.

**Interim logging (no schema change required)**: Even before adding the columns, log the HWM at exit in the existing `PROFIT LOCK EXIT` log line for manual analysis.

### 5.6 Interaction with Existing TP Decay Logic

The new profit lock does NOT replace the existing TP decay exits. They are complementary:

| Existing exit | Fires when | Profit lock role |
|---|---|---|
| TP fill (line 4200–4261) | Limit order at 63c/70c fills | Not affected. TP fills cleanly, we never reach profit lock. |
| MTF Reversal Exit (line 4317) | MTF score strongly opposing | Not affected. Fires before profit lock. |
| TP Decay EXIT at ≤5min (line 4361) | Profitable + <5 min remaining | **Overlapping**: If the position is profitable at 5min remaining AND HWM > entry+8c, the TP decay exit fires first (market sell at bid). The profit lock would have also fired (tight trail at minute 12+ = trail of 3c). The TP decay exit wins since it's checked first. No conflict. |
| TP Decay LOWER at ≤7min (line 4386) | <7 min, lowers limits to entry+5c | **Complementary**: After lowering limits at 7min, if bid drops below HWM-5c, the profit lock fires and exits immediately rather than waiting for the lowered limit to fill. |
| Mandatory Exit at ≤3min underwater (line 4403) | `bid <= entry + <3 min` | **Non-overlapping**: Profit lock only fires when HWM >= entry+8c. If the position is underwater (bid <= entry), the mandatory exit handles it. |

**Priority rule**: Since the profit lock is inserted AFTER the TP decay lower (line 4401) and BEFORE the mandatory exit (line 4403), it fires only for positions that are still open at that point in the waterfall — meaning: TP orders are still pending, TP decay hasn't exited yet, and the position is not underwater with <3 minutes.

### 5.7 Shadow Mode First

Before enabling live profit locking, run it in shadow mode: add logging but don't actually sell.

Add a `PROFIT_LOCK_SHADOW = True` config key. When True, log `PROFIT LOCK SHADOW` events but skip the market sell. This allows validating:
1. Phase transitions fire at the correct times
2. Trail distances are appropriate (not too many phase 0/1 would-have-exits)
3. The near-certain threshold correctly suppresses exits at high prices
4. HWM tracking is accurate across the full position lifetime

**Suggested shadow mode log format**:
```
CopyEngine PROFIT LOCK SHADOW: YES entry=52c bid=63c hwm=71c trigger=66c trail=5c phase=2 | 4.2 min left | would_exit=True | would_pnl=+$1.10
```

---

## 6. Expected Impact

### 6.1 Qualifying Trades (From Historical Data)

The profit lock only matters for `lost` trades where the position was meaningfully profitable during the window. We can't directly measure this (HWM is not in the DB). However, we can estimate:

**CVF `lost` trades**: 158 trades, -$187.99 total, avg -$1.19/trade.

A conservative estimate: **30% of `lost` CVF trades** (≈47 trades) were profitable enough at some point during the window to have triggered the profit lock (HWM ≥ entry + 8c). The 70% that never reached the threshold would be unaffected — they were underwater or barely profitable the whole time.

**If the profit lock converts those 47 trades:**
- Current outcome: avg -$1.19/trade × 47 = -$55.93 total
- With profit lock (estimate exit at +$0.30/trade average): +$14.10
- Net improvement: **+$70/historical period** on CVF alone

This estimate is conservative. Some of these trades would exit near HWM (e.g., at a 5c trail from a 20c peak profit), yielding +$0.70–$1.00/trade rather than $0.30.

### 6.2 Risks / Costs

**Risk 1: Stops out winning trades early.**
If a position dips 5c at minute 9 but would have recovered and settled at 100c, the profit lock causes a premature exit. Estimated frequency: 15–20% of profit lock activations. At avg +$0.50/trade vs avg +$0.97 hold-to-expiry, each "unnecessary" exit costs $0.47/trade.

**Risk 2: Phase 2 (5c at min 8-12) is too tight for volatile sessions.**
On high-volatility days, 5c swings are common noise in mid-window. Shadow mode data will reveal if Phase 2 is the primary false-positive source.

**Mitigation**: Start with the defaults and tune from shadow data. Widening Phase 2 from 5c to 7c would reduce the false-positive risk at the cost of letting more reversions through.

### 6.3 Simulation Query (After Shadow Data Collected)

```sql
-- After adding hwm_cents and exit_trail_phase columns and running shadow mode:

-- Trades where profit lock would have helped (lost trades with significant HWM)
SELECT
    COUNT(*) AS n,
    ROUND(SUM(pnl), 2) AS actual_pnl,
    ROUND(AVG(pnl), 3) AS avg_actual_pnl,
    ROUND(AVG(hwm_cents - limit_price), 1) AS avg_hwm_profit_c
FROM kalshi_trades
WHERE status = 'lost'
  AND hwm_cents IS NOT NULL
  AND hwm_cents >= limit_price + 8  -- would have activated
  AND (strategy_name LIKE '%CROSS_VENUE%' OR strategy_name LIKE '%PRIMARY%');

-- Exit trail phase distribution (from shadow logs converted to data)
SELECT
    exit_trail_phase,
    COUNT(*) AS n,
    ROUND(AVG(pnl), 3) AS avg_pnl_at_exit,
    ROUND(AVG(hwm_cents - limit_price), 1) AS avg_peak_profit_c
FROM kalshi_trades
WHERE exit_trail_phase IS NOT NULL
GROUP BY exit_trail_phase
ORDER BY exit_trail_phase;
```

---

## 7. Interaction with Other Proposed Changes

### 7.1 Inverted MTF Filter (Phase 4)

**Strong MTF opposing** signals (side_score ≤ -0.5) are the engine's highest-quality entries. These have MFE/MAE ratio of 1.88 — they run further with less adverse movement. For these trades:

- Phase 2 and 3 trails may be too tight (position runs 10c and then consolidates 5c — the 5c trail would stop it out at a minor pullback on the engine's best trades)
- **Proposed integration**: When `side_score <= -0.5` (strong opposing = high quality), widen Phase 2 trail from 5c to 8c and Phase 3 from 3c to 5c

Implementation: store the MTF side score in the position dict at entry:

```python
pos["mtf_side_score"] = _side_score  # negative = opposing (good entry quality)
```

Then in `_profit_lock_phase`, accept an optional `mtf_side_score` and adjust:

```python
def _profit_lock_phase(self, fill_time: float, mtf_side_score: float = 0.0) -> tuple[int, int]:
    elapsed_min = (time.time() - fill_time) / 60.0
    phase, trail = self._profit_lock_phase_base(elapsed_min)

    # High-quality opposing entry: widen trail to let position run
    if mtf_side_score <= -0.5:
        trail = min(trail + 3, PROFIT_LOCK_TRAIL_PHASE_0)  # cap at Phase 0 width
    return phase, trail
```

This is a Phase 6.5 enhancement — implement only after shadow data shows Phase 2 is the main source of false positives.

### 7.2 Window Phase Entry Timing

The planned enhancement to delay entries to minute :30 or :45 within the window (based on entry timing data showing :00/:15 entries at 33-36% WR vs :55 entries at 60% WR) directly affects the profit lock:

- A **late entry (minute :30)** enters 7.5 minutes into the 15-minute window. Fill happens at ~7.5 minutes elapsed. Phase 0 (0-5 min) is never active — position goes straight to Phase 1 (wide, 8c trail).
- An **even later entry (minute :45)** enters at 11.25 minutes elapsed. Goes straight to Phase 2 (medium, 5c trail).

This is acceptable behavior. Late-window entries have less time for reversals to compound, and the trail reflects the narrowing time window. The near-certain threshold and final-60s checks still apply.

**If the entry timing filter is implemented first** (delaying entries to :30+), the profit lock's Phase 0 (ignore first 5 minutes) becomes less relevant since most positions will start in Phase 1 or Phase 2 immediately. The Phase 0 config can be left as-is without harm.

### 7.3 Hold-to-Expiry Philosophy

The profit lock is the **sanctioned exception** to hold-to-expiry — not a contradiction of it. The philosophy remains:

> Positions that never reached meaningful profit: hold to expiry. The wallets are right on direction.

> Positions that reached meaningful profit (HWM ≥ entry + 8c): protect gains when BTC shows reversion. The wallets were right — but the position can now be defended.

The existing exits (TP fills at 63c/70c, TP decay at <5min, mandatory close at <3min underwater) are preserved exactly. The profit lock adds a layer in between — after TPs haven't filled and before the mandatory close — specifically for the case where you had profit but are now watching it evaporate.

---

## 8. Implementation Checklist

### Prerequisites

- [ ] Phase 1 bugs fixed (especially BUG-05 exception logging — critical for debugging trail exits)
- [ ] MTF shadow mode accumulating data (profit lock shadow mode is independent but benefits from the same shadow discipline)

### Step-by-Step

1. **Add config to `user_config.py`** — the full block from Section 3.4, with `PROFIT_LOCK_ENABLED = False` and `PROFIT_LOCK_SHADOW = True` initially

2. **Load config constants** — add the `getattr(user_config, ...)` block to `polymarket_copy_engine.py` near the other config loading (around line 200–300, wherever `SIGNAL_STOP_CENTS` is loaded)

3. **Add `_profit_lock_phase` helper** — around line 3861, near `_place_tiered_tp`

4. **Add unconditional HWM update** — immediately after line 4283 (`profit_cents = bid - entry`), before any conditional logic

5. **Add shadow mode profit lock block** — insert after line 4401 (TP decay lower block) with `PROFIT_LOCK_SHADOW = True`, log events but don't sell

6. **Run in shadow mode for 5-7 days** — validate phase transition timing and trail distances

7. **Analyze shadow logs**:
   ```bash
   grep "PROFIT LOCK SHADOW" data/engine_history.log | \
     grep "would_exit=True" | \
     awk '{print $0}' | head -50
   ```
   Look for: phase distribution (how often each phase fires), avg `would_pnl` (should be positive for meaningful exits), false positives (phase 0 would-exits that settled at 100c)

8. **Tune trails if needed** — if Phase 2 (5c at 8-12min) shows >30% false positives in shadow, raise to 6c

9. **Add DB schema columns**:
   ```sql
   ALTER TABLE kalshi_trades ADD COLUMN hwm_cents INTEGER;
   ALTER TABLE kalshi_trades ADD COLUMN exit_trail_phase INTEGER;
   ```
   Update `signal_logger.py` to write these fields

10. **Enable live mode** — set `PROFIT_LOCK_ENABLED = True`, `PROFIT_LOCK_SHADOW = False`

11. **Monitor first 20 live exits**:
    - Confirm `PROFIT LOCK EXIT` appears in logs (not just shadow)
    - Confirm no exits at Phase 0 (trail = 12c, should almost never fire)
    - Confirm near-certain threshold suppresses exits at 90c+
    - Confirm `exited_win` count in DB is increasing (not `won` — these are mid-session exits)

12. **After 50+ profit-lock-triggered exits**: Run the simulation query from Section 6.3 to measure actual vs theoretical impact

---

## 9. Code Diff Summary

All changes relative to current `polymarket_copy_engine.py`:

| Location | Change | LOC |
|---|---|---|
| ~line 200-300 | Add 13 config constant loads from `user_config` | +14 |
| ~line 3861 | Add `_profit_lock_phase()` helper method | +20 |
| ~line 4284 | Add unconditional HWM update block | +5 |
| ~line 4402 | Add profit lock trail check + final-60s check | +70 |
| `user_config.py` | Add profit lock config section | +20 |
| `signal_logger.py` | Add `hwm_cents` and `exit_trail_phase` parameters to `log_kalshi_outcome()` | +10 |

**Total**: ~140 lines, all additive (no deletions, no modifications to existing logic).

**No existing exit path is modified.** All new code is additive, inserted in a gap in the existing waterfall. The `if False` disabled blocks are left as-is.
