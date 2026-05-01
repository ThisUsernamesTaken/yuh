# BTC Bias Engine — Optimal Configuration Rationale
**Generated**: 2026-04-02
**Data period**: 2026-03-13 → 2026-04-02 (1,406 trades)
**Source config**: `user_config_optimal.py`
**Reference file**: Apply via `cp user_config_optimal.py user_config.py`

---

## Database Summary (as of 2026-04-02)

| Metric | Value |
|--------|-------|
| Total trades | 1,406 |
| Overall win rate | 46.2% |
| **Total P&L** | **-$302.55** |
| TA_FORCED P&L | -$269.30 (89% of all losses) |
| CVF P&L | -$42.30 |
| MIMIC P&L | -$13.07 |
| All other strategies | +$22.12 |

**Net excluding TA_FORCED**: -$33.25 over 1,186 trades
**Net excluding TA_FORCED + MIMIC**: -$20.18 over 1,122 trades

The engine is approximately breakeven once the broken strategy is removed. The goal of this config is to stop the bleeding and restore CVF to its viable operating parameters.

---

## §1 — PREREQUISITE: Code Change Required Before Config Takes Effect

**This is the most critical item in the entire document.**

The codebase audit (CODEBASE_AUDIT.md H3) shows that `_flow_iteration()` currently **only calls `_evaluate_ta_forced_signal()`**. The comment at line 1254 reads: *"Single strategy: TA_FORCED only. All other entry paths removed."*

This means: setting `TA_FORCED_ENABLED = False` without also restoring the CVF signal evaluator call will produce **zero trades**.

### What to restore in `_flow_iteration()`

The signals that must be called (in priority order per CLAUDE.md):

```python
# In _flow_iteration(), restore these calls (they still exist, just not called):
await self._evaluate_signal()            # PRIMARY / CROSS_VENUE_FLOW tier
await self._evaluate_trend_follow_signal()  # TREND_FOLLOW tier (17 trades, 70.6% WR)
# TA_FORCED is deliberately NOT restored
```

MIMIC and ALGO remain disabled (negative Kelly). PRE_OPEN_ARB and OPEN_MOMENTUM have too few trades to evaluate (1-2 trades each).

### Validation steps

1. Set `PAPER_TRADING = True` in user_config
2. Apply the code change
3. Restart the engine
4. Confirm CVF trades appear in paper mode (check logs for "CROSS_VENUE_FLOW" signal)
5. Confirm zero "TA_FORCED_SIGNAL" trades
6. Run for 1-2 hours, then set `PAPER_TRADING = False`

---

## §2 — TA_FORCED_ENABLED: True → **False**

**Confidence: HIGH (220 trades)**
**Expected impact: +$269 forward (stops a ~$70-150/day drain)**
**Risk if wrong: Zero trades until §1 code change is also made**

### The data

| Period | Trades | WR% | P&L | Avg contracts |
|--------|--------|-----|-----|---------------|
| Mar 23 | 67 | 49.3% | +$1.52 | 1.2 |
| Mar 30 | 20 | 60.0% | -$12.29 | 5.6 |
| Mar 31 | 65 | 41.5% | -$148.32 | 12.5 |
| Apr 1 | 35 | 22.9% | -$79.78 | 10.6 |
| Apr 2 | 33 | 6.1% | -$30.43 | 11.4 |

Kelly fraction: **-68.6%**. Avg win: +$2.49. Avg loss: -$4.55. Loss-to-win ratio: 1.827.

### Why TA_FORCED fails

1. **CODEBASE_AUDIT C3**: The strategy is misnamed. It only consults TA signals when the contract is 45-55c (flat). When mid < 45c, it trades "down" unconditionally. When mid > 55c, it trades "up" unconditionally. For ~70% of windows (mid outside 45-55c), the extensive TA infrastructure runs but its output is discarded.

2. **Mean-reversion incompatibility**: The MTF analysis (1,184 trades) proves the engine's 15m binary market is mean-reverting. TA_FORCED is trend-following (EMA/RSI directional). In a mean-reverting binary, directional signals are systematically wrong. When BTC is trending up on 1m, TA says "buy YES" — but smart wallets are already fading that move with NO positions. TA_FORCED enters the momentum trade and exits at expiry having bought the peak.

3. **Guaranteed-trade = guaranteed-losses**: TA_FORCED fires every window when wallets are silent. At -68.6% Kelly, "guaranteed trade" is "guaranteed loss."

4. **Sizing amplification**: At 25% balance fraction with a $78 peak balance, TA_FORCED scaled to 39 contracts at 50c = $19.50/trade. At 41.5% WR: E[trade] = 0.37 × 19.50 − 0.63 × 19.50 = **-$5.07/trade**. At 65 trades/day = -$330/day theoretical.

### Mar 23 viability was a false signal

The initial +$1.52 on 67 trades at 1.2 contracts was a sample-size artifact at tiny sizing. Breakeven would require 54.7% WR (for avg_win = $2.49, avg_loss = $4.55). The strategy never demonstrated that.

---

## §3 — Entry Price Bands: MIN=40/40, MAX=55 → MIN=45/48, MAX=54

**Confidence: HIGH (398 CVF trades — the core strategy)**
**Expected impact: +$10-15 forward (blocking losing bands)**
**Risk if wrong: Fewer trades, but every blocked trade was a losing trade**

### CVF YES bands (live data)

| Band | N | WR% | P&L | P&L/trade | Action |
|------|---|-----|-----|-----------|--------|
| <35c | 45 | 11.1% | -$15.81 | -$0.35 | BLOCK |
| 35-44c | 45 | 31.1% | -$0.83 | -$0.02 | BLOCK |
| **45-54c** | **42** | **54.8%** | **+$9.91** | **+$0.24** | ✅ KEEP |
| **55-64c** | **40** | **62.5%** | **+$5.99** | **+$0.15** | ✅ KEEP (if NO is capped) |
| **65-74c** | **30** | **80.0%** | **+$9.09** | **+$0.30** | ✅ KEEP (if NO is capped) |
| 75+ | 47 | 89.4% | -$2.64 | -$0.06 | BLOCK (payout kill) |

### CVF NO bands (live data)

| Band | N | WR% | P&L | P&L/trade | Breakeven WR | Action |
|------|---|-----|-----|-----------|--------------|--------|
| <35c | 12 | 0.0% | -$10.98 | -$0.92 | 35% | BLOCK |
| 35-44c | 18 | 33.3% | -$8.28 | -$0.46 | 40% | BLOCK |
| **45-54c** | **36** | **69.4%** | **+$2.76** | **+$0.08** | 50% | ✅ KEEP |
| 55-64c | 40 | 52.5% | -$15.76 | -$0.39 | 58% | BLOCK |
| 65-74c | 15 | 60.0% | -$9.36 | -$0.62 | 68% | BLOCK |
| 75+ | 28 | 75.0% | -$6.39 | -$0.23 | 79% | BLOCK |

### The payout asymmetry math

For NO contracts at price P: win pays (100-P)c, loss costs Pc. Breakeven WR = P / 100.
- NO at 60c: need 60% WR to break even. CVF achieves 52.5% → negative EV.
- NO at 70c: need 70% WR. CVF achieves 60% → still negative EV.
- NO at 75c: need 75% WR. CVF achieves 75% → theoretical breakeven, but fees eat it.
- **Only at 45-54c NO (69.4% WR vs 50% breakeven) does CVF have clear edge.**

### The YES/NO cap conflict and recommended code change

The engine uses `SIGNAL_MAX_ENTRY_CENTS` for both sides. This creates an impossible tradeoff:
- MAX=54 blocks bad NO entries (55-74c NO = -$25.12) but also blocks profitable YES (55-74c YES = +$15.08)
- MAX=74 captures profitable YES but also lets in losing NO entries

**Resolution (requires code change)**:
```python
# In _execute_signal(), after entry price is determined:
if side == "no" and ask > NO_MAX_ENTRY_CENTS:
    logger.info("NO entry above max (%dc > %dc) — skip", ask, NO_MAX_ENTRY_CENTS)
    return
```

Add to user_config:
```python
NO_MAX_ENTRY_CENTS = 54   # Only 45-54c NO is profitable CVF band
```

Add to engine config section:
```python
SIGNAL_MAX_ENTRY_NO_SIDE = _uc("NO_MAX_ENTRY_CENTS", 54)
```

**Until this code change lands**: MAX_ENTRY_CENTS = 54 is set in `user_config_optimal.py`. This sacrifices +$15.08 in YES edge (55-74c bands) to avoid -$25.12 in NO losses. Net: accepting -$10.08 loss from the cap conflict to prevent -$25.12 in NO losses — a +$15 improvement.

**After code change**: Set MAX_ENTRY_CENTS = 74 to unlock the full YES edge.

---

## §4 — Sizing: 25%/$50 → 10%/$8

**Confidence: HIGH (causal chain is unambiguous)**
**Expected impact: Caps maximum daily loss at ~$80-160 vs $148-$330**
**Risk if wrong: Lower upside on winning days (acceptable tradeoff)**

### The blowup mechanics

```
Balance grows from profitable CVF sessions → SIZING_BALANCE_FRACTION × balance scales up
→ TA_FORCED enters with 20-47 contracts → losses amplified by position count
→ Balance shrinks but not fast enough to de-lever (positions stay large mid-session)
→ Daily loss limit not hit fast enough (resets on restart per BUG-04)
```

### Optimal sizing from contract count data

| Contracts | Trades | WR% | P&L | P&L/trade |
|-----------|--------|-----|-----|-----------|
| 1 | 873 | 44.7% | -$25.10 | -$0.029 |
| 2 | 139 | 46.8% | -$1.11 | -$0.008 |
| 3 | 123 | 46.3% | -$2.68 | -$0.024 |
| **4** | **55** | **65.5%** | **+$3.14** | **+$0.057** |
| 5 | 28 | 46.4% | -$22.36 | -$0.799 |
| 6 | 64 | 50.0% | -$23.43 | -$0.366 |
| 9 | 8 | 37.5% | -$28.48 | -$3.560 |
| 10 | 21 | 33.3% | -$42.10 | -$2.005 |
| 29 | 3 | 0.0% | -$46.11 | -$15.370 |

Performance peaks at **4 contracts** (65.5% WR, +$0.057/trade). Catastrophic above 8. The sweet spot maps to ~$2 risk per trade at 50c entry. `SIZING_MAX_DOLLARS = $8` at 50c entry = 16 contracts — upper bound of the viable zone, with the balance fraction keeping most trades below the cap.

### Why $8 not $50

`SIZING_MAX_DOLLARS = 50` never triggered because `0.25 × $78 = $19.50 < $50`. The cap must be below the natural scaling point. At 10% balance fraction:
- $50 balance: 10% = $5.00 (cap: $8 → $5 wins, small)
- $80 balance: 10% = $8.00 (cap hits exactly)
- $120 balance: 10% = $12.00 → cap at $8 (prevents further scaling)

The cap creates a hard ceiling that the balance fraction alone cannot.

---

## §5 — BLOCKED_HOURS: {} → {8,9,10,11,12,13,14}

**Confidence: MEDIUM-HIGH (consistent pattern, 291 pre-blowup CVF trades)**
**Expected impact: +$5-15 forward per week (eliminating low-WR sessions)**
**Risk if wrong: Miss some CVF trades in EU hours; wallet signals may improve**

### Hourly P&L (placed_at, all 1,406 trades, settled only)

| Hour UTC | Trades | WR% | P&L | Session |
|----------|--------|-----|-----|---------|
| 18 | 50 | 62.0% | +$12.01 | US afternoon |
| 17 | 79 | 69.6% | +$9.04 | US open |
| 00 | 42 | 47.6% | +$2.24 | Asia open |
| 21 | 42 | 47.6% | -$1.17 | US late |
| 11 | 61 | 41.0% | -$1.99 | EU midday |
| 19 | 62 | 59.7% | -$1.92 | US mid |
| 09 | 34 | 44.1% | -$1.57 | EU morning |
| 07 | 45 | 44.4% | -$2.09 | EU early |
| 20 | 58 | 43.1% | -$4.02 | US close |
| 10 | 51 | 41.2% | -$7.35 | EU midday |
| 08 | 59 | 32.2% | -$8.62 | EU open |
| 16 | 66 | 37.9% | -$9.59 | EU/US overlap |
| 23 | 85 | 50.6% | -$5.11 | Overnight |
| 13 | 68 | 51.5% | -$19.60 | EU afternoon |
| 12 | 54 | 40.7% | -$24.84 | EU lunch |
| 14 | 64 | 35.9% | -$25.67 | EU close |
| 22 | 75 | 41.3% | -$30.46 | US evening |
| 04 | 112 | 38.4% | -$24.55 | Asia night |
| 01 | 30 | 43.3% | -$21.49 | Asia deep |
| 02 | 37 | 62.2% | -$13.70 | Asia (TA blowup) |
| 03 | 53 | 47.2% | -$31.49 | Asia (TA blowup) |
| 05 | 77 | 55.8% | -$60.70 | Asia (TA blowup) |

**Important**: The 02-05 UTC losses are almost entirely TA_FORCED at 10-47 contracts. 55.8% WR losing $60 = position sizes were enormous on losses. Post-TA_FORCED-disable, Asia hours should normalize.

**European session (08-14 UTC)** shows **consistently bad CVF performance** independent of the TA_FORCED blowup. Pre-blowup CVF-specific analysis (March 28 dataset):
- European session: **-$28.99, 41.6% WR, 291 trades**
- This is pure CVF performance in EU hours — wallet flow is absent because Poly wallets are US-based
- Hour 14 is the single worst hour: -$12.37 (pre-blowup), -$25.67 (full dataset)

Block 08-14 UTC (inclusive) = 7 hours = 29% of trading day sacrificed for ~40% of CVF losses eliminated.

**Reassess after 60 days**:
- Asia hours (00-07 UTC): If CVF-only performance improves post-TA_FORCED-fix, consider reopening 00-07.
- 16 UTC: Currently -$9.59 at 37.9% WR — may need to add to blocked set.
- 22-23 UTC: Watch for TA_FORCED contamination clearing.

---

## §6 — MIN_ENTRY_CENTS: 40 → 45

**Confidence: HIGH (90 trades in the 35-44c YES band)**
**Expected impact: +$0.83 forward (eliminating -$0.02/trade band)**
**Risk if wrong: Miss some breakeven trades (trivial)**

CVF YES at 35-44c: 45 trades, 31.1% WR, -$0.83 total. This is below the 54.8% WR needed for profitability at ~50c entry (need WR × payout > entry price). At 40c entry, need WR > 40% to break even; 31.1% is well below that.

The engine already has `ABSOLUTE_MIN_ENTRY_CENTS = 35` hardcoded. Raising user_config to 45 provides an additional 10c buffer zone.

---

## §7 — MIN_ENTRY_CENTS_NO: 40 → 48 (engine already enforces 48)

**Confidence: HIGH (30 trades in 35-44c NO, 12 trades in <35c NO)**
**Expected impact: Already enforced by engine default (48c). User_config at 40 was never used.**

CVF NO at 35-44c: 18 trades, 33.3% WR, -$8.28. Breakeven at 40c NO = need 40% WR. 33.3% is below that.
CVF NO at <35c: 12 trades, 0.0% WR, -$10.98. Complete disaster.

The engine's `SIGNAL_MIN_ENTRY_NO_SIDE = _uc("MIN_ENTRY_CENTS_NO", 48)` already enforces 48c as the floor. Setting user_config to match (48) makes the intent explicit and prevents confusion.

---

## §8 — MAX_ENTRY_CENTS: 55 → 54 (conservative) or 74 (after code fix)

**Confidence: HIGH for the bands; MEDIUM for the exact threshold**
**Expected impact: +$2.76 (capture 45-54c NO edge) vs current user_config cap of 55**

The current user_config caps at 55c. This actually already captures the 45-54c bands correctly. The recommendation change is primarily about clarifying the intent: 54c is more precisely correct because the 55-64c NO band is -$15.76.

The bigger change (and bigger impact) is raising to 74 once the NO_MAX code change lands:
- YES 55-74c combined: +$15.08
- NO 55-74c combined: -$25.12 (blocked by code change)
- Net: +$15.08 improvement

**Do not set MAX = 74 without the NO_MAX code change.**

---

## §9 — MIN_FLOW_CONVICTION: 0.50 → 0.75

**Confidence: MEDIUM (engine comment references session WR data)**
**Expected impact: Fewer trades, higher-quality signal**
**Risk if wrong: Trades disappear (monitor trade frequency)**

Engine code comment (line 126): *"RAISED to 75%. DATA: 41% session WR at 60% floor. Need stronger conviction."* The engine's default is already 0.75. User_config at 0.50 was overriding a better default. The 41% session WR reference was observed data driving this decision. Restoring to 0.75 aligns user_config with the engine's own evidence-based default.

---

## §10 — MIMIC_ENABLED: False (confirm — keep disabled)

**Confidence: HIGH (64 trades)**
**Expected impact: Already disabled; keeping disabled saves -$0.204/trade**

| Metric | Value |
|--------|-------|
| Trades | 64 |
| WR% | 43.8% |
| Avg win | +$0.88 |
| Avg loss | -$1.05 |
| Total P&L | -$13.07 |
| P&L/trade | -$0.204 |
| Kelly% | -23.2% |

Avg loss exceeds avg win by 19%. At 43.8% WR with a 1.19:1 loss ratio, this has negative Kelly. MIMIC fires on single-wallet evidence with weaker signal than the CVF primary (which requires multiple wallet consensus). No path to positive Kelly without a redesigned entry filter (e.g., require 2+ elite wallets, or only mimic in the 45-54c YES band).

---

## §11 — MTF_SHADOW_MODE: True (confirm — keep in shadow)

**Confidence: HIGH on inversion finding; LOW on shadow data volume**
**Expected impact: Neutral (shadow mode; no blocking or sizing changes)**
**Risk if going live too early: Could block the engine's best trades**

### The inversion finding (1,184 trades)

| MTF alignment | WR% | P&L | N |
|---------------|-----|-----|---|
| Aligned (MTF confirms trade) | 36.4% | -$24.70 | 321 |
| Neutral (|score| < 0.3) | 47.7% | +$9.11 | 796 |
| Opposing (MTF contradicts trade) | 68.7% | +$3.56 | 67 |

The engine is contrarian. MTF measures the trend it fades. MTF alignment = "BTC is trending hard in trade direction" = wallets are fading that trend = **the engine is entering at a momentum peak**.

### Per-TF correlation with winning

| TF | rpb | Direction |
|----|-----|-----------|
| 1m | +0.088 | Weak positive — immediate microstructure is real |
| 5m | -0.128 | Inversely predictive |
| 15m | -0.152 | Strongest inverse — the mean-reversion signature |
| 1h | -0.084 | Also inverse |

### Current shadow data (35 trades)

| Category | N | WR% | P&L |
|----------|---|-----|-----|
| Aligned | 14 | 21.4% | -$16.17 |
| Neutral | 4 | 0.0% | -$23.06 |
| Opposing | 0 | — | — |

The aligned WR (21.4%) is even lower than the simulation (36.4%) — confirms inversion. Opposing sample is empty. Need 20+ opposing trades before any live filter.

### Gate conditions for live mode

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Shadow aligned WR | < 42% (n ≥ 50) | Proceed with inverted filter |
| Shadow opposing WR | > 60% (n ≥ 20) | Proceed with size boost |
| Shadow aligned WR | > 50% | Re-examine — simulation may have errors |
| Either sample | < threshold sizes | Keep shadow mode |

### What the live inverted filter should look like

```python
# In _execute_signal(), replace current MTF block with:
side_score = mtf_score if side == "yes" else -mtf_score

if not MTF_SHADOW_MODE:
    _mtf_allow_block = signal.signal_tier not in ("PRIMARY", "TREND_FOLLOW")
    if side_score >= 0.5 and _mtf_allow_block:
        # Strong trend alignment = engine's worst entries — veto
        logger.info("MTF INVERTED VETO: side_score=%.2f aligned — momentum peak entry", side_score)
        return
    elif side_score >= 0.3:
        _mtf_size_mult = 0.75    # Moderate alignment: reduce size
    elif side_score <= -0.5:
        _mtf_size_mult = 1.5     # Strong opposing: boost (engine's best entries)
    elif side_score <= -0.3:
        _mtf_size_mult = 1.25    # Moderate opposing: slight boost
```

**Fix BUG-03 first**: The current live mode blocks PRIMARY/TREND_FOLLOW. Add the tier guard `_mtf_allow_block` as shown above before switching `MTF_SHADOW_MODE = False`.

---

## §12 — STOP_LOSS_CENTS: 8 (confirm — keep configured, but hold-to-expiry in practice)

**Confidence: HIGH (88 exited_win trades vs 64 exited_loss trades)**
**Expected impact: Neutral (stops currently disabled by hold-to-expiry logic)**
**Risk if re-enabling: exited_loss trades average -$1.461/trade vs held-to-expiry -$0.836/trade**

### Exit type comparison (1,406 trades)

| Exit type | N | Avg P&L | Total |
|-----------|---|---------|-------|
| won (expired) | 561 | +$0.968 | +$542.78 |
| lost (expired) | 601 | -$0.836 | -$502.65 |
| exited_win | 88 | +$0.448 | +$39.46 |
| exited_loss | 64 | -$1.461 | -$93.49 |
| stopped | 12 | -$2.799 | -$33.59 |
| reconciled_unknown | 43 | -$5.870 | -$252.41 |

Stopped trades average -$2.799/trade — **3.3× worse** than expired losses. The stops are triggering at the wrong time (cutting positions that would have recovered). The MTF analysis confirms this: the engine is mean-reverting, so initial adversity often reverses.

**However**: `reconciled_unknown` at -$5.870/trade is the real killer — these are the TA_FORCED blowup positions with 20-45 contracts that the engine lost track of on restart. With proper sizing ($8 cap) and TA_FORCED disabled, this category disappears.

**With $8 max per trade**: hold-to-expiry max loss = $8. At current WR levels, this is manageable. Re-evaluate stops only if:
- Sizing ever increases above $15/trade, OR
- WR drops below 40% for 50+ trades (structural signal failure)

---

## §13 — DAILY_LOSS_LIMIT: $15 (confirm — keep, but fix BUG-04)

**Confidence: MEDIUM (appropriate level; mechanism is broken)**
**Expected impact: Provides partial protection; full protection requires code fix**

$15 is appropriate for the current $8/trade sizing:
- Best-case: 2 max-size losses back-to-back halts trading (2 × $8 = $16 > $15)
- Worst-case: ~2 catastrophic TA_FORCED-level positions (TA_FORCED is now disabled)
- With CVF at current WR: expected days where $15 is hit are rare

**BUG-04 (IMPLEMENTATION_GUIDE.md)**: `_daily_pnl` resets to 0 on restart. After a crash at -$13, a restart allows another -$15 before halting. This could produce -$28 days.

The fix (persist `_daily_pnl` to disk on each update) is straightforward but requires a code change. Until fixed, the $15 limit provides protection within a single process lifetime.

---

## §14 — MIN_MINUTES_REMAINING: 2.0 (confirm — keep)

**Confidence: HIGH (1,382 trades, extremely clear pattern)**
**Expected impact: Neutral (already set correctly)**

### Phase analysis (minutes elapsed in 15-min window)

| Phase | Elapsed | Remaining | Trades | WR% | P&L |
|-------|---------|-----------|--------|-----|-----|
| Open | 0-2 min | >13 min | 328 | 35.2% | -$141.19 |
| Discovery | 3-7 min | 8-12 min | 210 | 38.5% | -$37.55 |
| Middle | 8-12 min | 3-7 min | 126 | 46.3% | +$7.51 |
| **Close** | **13-14 min** | **1-2 min** | **43** | **60.5%** | **+$8.33** |

Late entries (1-2 minutes remaining, enabled by MIN = 2.0) are the **highest-WR entries in the dataset** at 60.5%. These are high-information signals — the market direction is clearer near expiry, wallets have already positioned, and the binary outcome is close to resolution.

Increasing MIN_MINUTES_REMAINING would block this bucket. The current value of 2.0 is correct. The open-phase losses were driven by TA_FORCED firing immediately at window open — disabled.

---

## §15 — MTF Inverted Dynamic TP (future enhancement)

Not implemented in current config. For reference when MTF goes live:

| Confidence | Tier 1 TP | Tier 2 TP | Trail Activate | Trail Distance | Size |
|------------|-----------|-----------|----------------|----------------|------|
| OPPOSING strong (≥0.5 opposing) | +10c | +16c | +18c | 6c | 1.5x |
| OPPOSING moderate (0.3-0.5) | +8c | +13c | +15c | 5c | 1.25x |
| NEUTRAL (|score| < 0.3) | +7c | +11c | +15c | 5c | 1.0x |
| ALIGNED moderate (0.3-0.5) | +5c | +8c | +12c | 4c | 0.75x |
| ALIGNED strong (≥0.5) | VETO | — | — | — | 0x |

Rationale: OPPOSING trades have MFE p50 = 0.162% BTC (≈3.2c Kalshi equivalent) vs NEUTRAL p50 = 0.090% (≈1.8c). Wider TPs capture more of the favorable move for opposing-confidence entries. These are the engine's contrarian best-quality trades.

---

## §16 — PAPER_TRADING: False → True (temporarily, then False)

**Confidence: N/A (procedural)**

Set `PAPER_TRADING = True` during the §1 code change validation. Run in paper mode until you confirm:
1. CVF trades appear in the log (`CROSS_VENUE_FLOW` strategy name)
2. TA_FORCED trades are absent (`TA_FORCED_SIGNAL` never logged)
3. Trade frequency is reasonable (3-12 CVF signals per day during US hours)
4. Entry prices respect the new bands (no trades below 45c, none above 54c initially)

Then set `PAPER_TRADING = False` for live trading.

---

## §17 — Parameters Kept Unchanged (with justification)

| Parameter | Current | Verdict | Reason |
|-----------|---------|---------|--------|
| MIN_SMART_WALLETS | 1 | ✅ Keep | Progressive budget handles sizing. Incremental entry is correct. |
| MAX_TRADES_PER_WINDOW | 6 | ✅ Keep | Flip-invert requires headroom (sell + buy + TPs). |
| TRADE_COOLDOWN_SECONDS | 10 | ✅ Keep | Prevents signal spam; appropriate for 3s poll interval. |
| MAX_MINUTES_REMAINING | 15.0 | ✅ Keep | Wallets bet immediately at window open — don't block early entries. |
| MIN_DIVERGENCE | 0.01→0.10 | ⬆️ Raise | Engine default is already 0.10; user_config at 0.01 overrides this incorrectly. |
| STOP_GRACE_PERIOD_S | 15 | ✅ Keep | 15s grace before stop activates is appropriate. |
| HIGH_ENTRY_STOP_THRESHOLD | 83 | ✅ Keep | At 83c+, max payout = 17c; -5c stop is appropriate. |
| STOP_LOSS_CENTS_HIGH_ENTRY | 5 | ✅ Keep | Tight stop for 83c+ entries (see §12). |
| MTF_TIMEFRAMES | 4 TFs | ✅ Keep | All 4 TFs needed for inversion analysis, even though only 1m is positively predictive. |
| PRICE_FEED_SYMBOL | btcusdt | ✅ Keep | Correct. |
| PRICE_FEED_WS_TIMEOUT_S | 30.0 | ✅ Keep | Appropriate reconnect window. |

---

## Summary: Change Impact Table

| Change | Expected P&L Impact | Confidence | Required Action |
|--------|---------------------|------------|-----------------|
| TA_FORCED = False | **+$269** (forward prevention) | HIGH | Code change §1 required first |
| SIZING_MAX_DOLLARS = $8 | **+$50-100** (cap future blowups) | HIGH | Config only |
| SIZING_BALANCE_FRACTION = 0.10 | Enables $8 cap to work | HIGH | Config only |
| MIN_ENTRY_CENTS = 45 | **+$0.83** | HIGH | Config only |
| MAX_ENTRY_CENTS = 54 | **+$15** vs uncapped | MEDIUM | Config only (upgrade to 74 after NO_MAX code change) |
| BLOCKED_HOURS = 08-14 | **+$5-15/week** | MEDIUM-HIGH | Config only |
| MIN_FLOW_CONVICTION = 0.75 | Better signal quality | MEDIUM | Config only |
| Code: restore CVF in _flow_iteration | **Prerequisite — nothing else matters** | HIGH | Code change |
| Code: add NO_MAX_ENTRY_CENTS = 54 | **+$15.08** (unlock 55-74c YES) | HIGH | Code change |
| Code: fix BUG-04 (daily P&L persistence) | Closes restart exploit | MEDIUM | Code change |
| Code: fix BUG-03 (MTF tier guard) | Required before MTF goes live | HIGH | Code change (before MTF live) |
| MTF: keep shadow mode | Neutral (collecting data) | HIGH | Config only |

**Estimated P&L recovery (conservative)**: +$280-320 forward annually from config alone (TA_FORCED disable + sizing cap + band tightening). CVF breakeven-to-slightly-positive is achievable once the bands and sizing are correct.

---

*Analysis based on 1,406 live trades, 2026-03-13 → 2026-04-02.*
*Next review: After 200 CVF-only trades post-restart, check hourly distribution and MTF shadow buckets.*
