# RESEARCH — Momentum/Reversion Covariance Using Contract Pricing — Do Not Implement

**Date:** 2026-05-04
**Scope:** New exit-management layer derived from Kalshi contract price behavior, not BTC indicators
**Targets:**
- New module: `contract_momentum.py` (proposed, not yet created)
- Integration: `polymarket_copy_engine.py` — `_manage_position()` (line 18523) and protective-order TP path
- Replaces: legacy session-level regime classifier at `polymarket_copy_engine.py:5487–5522`
- Compatible with: `bb_pure.py` (entry logic untouched), `protective_math.py` (exit math augmented), `RESEARCH_OPTIMAL_ENTRY_EXIT_V2.md` NO-side TP reduction (this layer multiplies on top)

> **Status flag:** This is a research/design document. It contains math, code skeletons, and exact integration coordinates. **Do not implement any of it yet.** The validation plan in §7 must run in shadow mode for ≥200 fills before any of the dynamic-TP behavior is wired into live execution.

---

## 0. TL;DR

Today's engine treats the take-profit target as a static function of `prob_engine.fair_value` and the side-asymmetric premium table from V2 research. After fill, the only signal it watches is **the current contract bid** vs the TP/SL rails — it does not look at *how* the contract is moving.

The **contract mid is itself the market's instantaneous probability estimate**. Its first derivative is the market's conviction delta. Its second moment (variance around a short-window SMA) is the market's uncertainty. The covariance of those two — momentum (drift) against reversion (variance) — over a short rolling window classifies the active session in real time without any reference to BTC.

This document defines:

1. Five primitive indicators computed from the Kalshi mid stream alone (CMS, CRI, MRC, Path Signature, Settlement Convergence Rate).
2. A `ContractMomentumAnalyzer` class that produces a single output: `recommended_tp_adjustment ∈ [0.7, 1.3]` plus an early-exit override on strong reversion patterns.
3. Exact integration points: where to construct it (per-window, after `prob_engine.calibrate_strike`), where to feed it (every book poll inside `_manage_position`), and where to read it (inside the protective-maintain TP recompute and the legacy TP ladder).
4. A shadow-mode validation plan: log everything, act on nothing, correlate MRC at entry with realized MFE/MAE/PnL.

The proposal is **purely about exits**, not entries. Entry remains BB_PURE. The MRC layer dynamically adjusts the resting TP and (on strong M-top / W-bottom signatures) forces an early flatten when the contract's own pricing says the move is over.

---

## 1. Conceptual Framework

### 1.1 What a Kalshi 15-minute binary actually is

A `KXBTC15M-*` contract pays $1 if BTC's CME-settled price at the end of the 15-minute window is on the YES side of the strike, $0 otherwise. As `t → 0`, the mid price converges to either 0c or 100c. Between session open (t = 900s) and expiry (t = 0s), the mid is the market's *Bayesian posterior* on the binary outcome conditional on all observable BTC information.

That has two consequences this document hangs on:

- **Mid price = implied probability.** A mid of 62c is the consensus saying "62% chance YES wins." The Brownian Bridge `prob_engine.fair_value` (`price_feed.py:346–505`) is the *model's* posterior; the **mid is the market's posterior**. The gap between them is `mispricing`. The current engine acts on that gap at entry. After entry, it stops listening to the market's posterior — that is the gap this document closes.
- **Mid evolution = belief update path.** A mid that walks 50 → 55 → 60 → 65 over four minutes is a market gaining conviction monotonically. A mid that bounces 50 → 58 → 51 → 57 → 53 is a market that does not know — equal-weight bear and bull arguments arriving in alternation. The *path* the contract took to its current price encodes information that the *current price* does not.

### 1.2 Two regimes in mid-price evolution

**Momentum regime.** The mid moves persistently in one direction. Successive 10-second mid changes share sign. Variance around a short SMA is low because the SMA is trending with the mid. In this regime, the current mid undershoots the *terminal* mid — the market is still pricing in news, not yet at equilibrium.

> Trading consequence: on the favored side of a momentum regime, **hold and widen TP**. On the wrong side, **stop early** — momentum will not reverse into your favor.

**Mean-reversion regime.** The mid oscillates around a level. Successive mid changes alternate sign. Variance around a short SMA is high relative to drift. The market is uncertain, and the contract is "pinned" pending news.

> Trading consequence: **TP at extremes, accept smaller profit, exit early** because oscillations end at expiry — a 50c entry that bounces 47–53 for 12 minutes settles at 0 or 100 with no edge to your direction.

### 1.3 The covariance term

The killer insight: momentum and reversion are not opposites. They can both be **weak** (a flat dead market — common in the 90-second baseline period), both **strong** (a chopping trend — drifting up but with violent retraces), or one strong and one weak.

The **covariance** between the rolling momentum series and the rolling reversion series classifies *which of the four cells* you are in:

| | Reversion strong | Reversion weak |
|---|---|---|
| **Momentum strong** | **Choppy trend** — TP early, dangerous | **Clean trend** — hold to expiry |
| **Momentum weak** | **Range-bound** — fade extremes | **Dead market** — TP at fair, no widening |

A positive MRC (momentum and variance rising together) is the **choppy trend** cell — the dangerous one. A negative MRC (momentum rising while variance falling) is the **clean trend** cell — the safe one. Near-zero MRC means the two signals are independent and should be acted on separately.

Today's engine has none of this. It exits on a single fixed TP relative to entry, computed once at fill time.

---

## 2. Contract Price as Probability Signal — Properties to Exploit

| Property | What it tells you | How to compute |
|---|---|---|
| Mid level | Current implied P(YES) | `(best_yes_bid + best_yes_ask) / 2` |
| Mid first difference | Belief update over poll interval | `mid[t] - mid[t-1]` |
| Short EMA of mid returns | Smoothed conviction | EMA(α=0.3) over 30s of returns |
| Variance around short SMA | Indecision / oscillation | `Var(mid - SMA_2min)` over 30 obs |
| Mid VWAP vs current mid | Drift detection | `mid_now - sum(mid_i * dt_i) / sum(dt_i)` |
| Time-at-extreme | "Pinned" detection | `count(|mid - HWM| < 1c) / count(all obs)` over last 60s |
| Path shape | Reversion vs trend | classify {open, current, HWM, LWM, time_at_HWM, time_at_LWM} |

These all have **two important properties**:

1. **No external API call.** Every input is already in the `KalshiTape` rolling buffer (`kalshi_tape.py`) and the orderbook poll output that `_manage_position` already consumes. This is purely in-memory processing on data the engine has on every cycle.
2. **No dependence on BTC.** The signal is generated by the Kalshi market itself. This is critical: the existing regime classifier (`polymarket_copy_engine.py:5487–5522`) classifies on BTC indicators (`five_sec.macd_histogram`, `bb_width`) which describe spot, not the contract. Contract pricing already integrates BTC + flow + tape — using it directly skips the indirection.

### 2.1 A worked example of why path matters

Two contracts both trading at 60c with 8 minutes remaining:

- **Contract A:** opened at 50c, drifted 50 → 53 → 56 → 58 → 60 monotonically over 7 minutes. Variance around 2-min SMA: low. CMS: strongly positive. Path: STAIRCASE. The market is gaining conviction — terminal value is more likely 100 than 60.
- **Contract B:** opened at 70c, fell 70 → 65 → 55 → 50 → 55 → 60 in the same 7 minutes. Variance around SMA: high. CMS: weakly positive *now* but historically negative. Path: V_SHAPE recovering. The market lost conviction and is now reverting from a low — there is no fundamental support; this is a bounce that may not finish.

A YES holder bought at 55c is in profit on both. The current engine treats them identically: TP target = `max(int(prob.fair_value), entry + 5)`, computed once at fill (`polymarket_copy_engine.py:3342`). The MRC layer would instead:

- A: MRC strongly negative → multiply TP target by 1.3 (widen), suppress time-exit, hold.
- B: MRC near zero with M-top forming if the mid stalls between 60–62 → multiply TP by 0.7 (tighten), arm trailing stop, exit on the next 1c retrace.

---

## 3. Indicator Definitions

All indicators are computed on a **rolling buffer of (timestamp, mid_cents) tuples**. The engine already polls the orderbook every ~0.9s on average (`FLOW_POLL_INTERVAL_S = 0.3`, REST every 3rd cycle — `polymarket_copy_engine.py:139`), so a 30-observation buffer covers ~27s of contract evolution.

**Notation:**
- `m[t]`: mid price at observation `t`, in cents (float).
- `r[t] = (m[t] - m[t-1]) / max(m[t-1], 1e-6)` — single-step relative return. Use relative returns so a 1c move at a 10c mid weighs more than a 1c move at a 50c mid (conviction is harder to gain when you already "know" the answer).
- `EMA_α(x)`: exponential moving average with smoothing factor α.
- `SMA_n(x)`: simple moving average over the last n observations.

### 3.1 Contract Momentum Score (CMS) — drift indicator

```
CMS[t] = EMA_0.3(r[t])   # smoothed return
CMS_norm[t] = clip(CMS[t] / 0.005, -1.0, +1.0)
```

The 0.005 normalizer corresponds to a 0.5% per-poll relative return — at typical 0.9s polls and 50c mid, that's 22 basis points per second of drift, which is "fast" for Kalshi BTC15M. CMS_norm = +1 means the contract is moving as fast in the YES direction as the engine should ever expect.

**Interpretation:**
- `CMS_norm > +0.3`: meaningful YES momentum (price walking up).
- `CMS_norm < -0.3`: meaningful NO momentum.
- `|CMS_norm| < 0.1`: drift is below noise — treat as flat.

**Code (illustrative):**

```python
def update_momentum(self, mid_cents: float, ts: float) -> None:
    if self._prev_mid is None:
        self._prev_mid = mid_cents
        self._prev_ts = ts
        return
    dt = ts - self._prev_ts
    if dt <= 0 or self._prev_mid <= 0:
        return
    r = (mid_cents - self._prev_mid) / self._prev_mid
    if self._cms is None:
        self._cms = r
    else:
        self._cms = 0.3 * r + 0.7 * self._cms
    self._prev_mid = mid_cents
    self._prev_ts = ts
```

### 3.2 Contract Reversion Index (CRI) — variance indicator

```
SMA_2min[t] = mean(m[i] for i where ts[i] >= ts[t] - 120)
residuals[t] = m[t] - SMA_2min[t]
CRI_raw[t] = stdev(residuals over last 30 observations)
CRI_norm[t] = clip(CRI_raw[t] / 3.0, 0.0, 1.0)
```

The 3.0 cents normalizer is the empirical 95th percentile of residual stdev for KXBTC15M during a typical 15-min window with no surprise news (verify in §7). Above 3c residual stdev means the contract is oscillating violently around its short-window mean — a hallmark of a mean-reverting market.

**Interpretation:**
- `CRI_norm < 0.3`: contract is tracking its SMA closely (smooth move).
- `CRI_norm > 0.6`: contract is oscillating around the SMA.

### 3.3 Momentum-Reversion Covariance (MRC) — regime classifier

```
window: last 60 observations (~54s) of (CMS_norm[t], CRI_norm[t]) pairs
mean_cms = mean(CMS_norm over window)
mean_cri = mean(CRI_norm over window)
MRC_raw = sum((CMS_norm[t] - mean_cms) * (CRI_norm[t] - mean_cri)) / N
MRC = clip(MRC_raw / 0.05, -1.0, +1.0)
```

The 0.05 normalizer is small because both inputs are already in [-1, +1] / [0, 1] and their typical covariance is small. After normalization:

| MRC range | Regime label | Trade response |
|---|---|---|
| `MRC < -0.3` | Clean trend (momentum up, vol down) | **Widen TP × 1.3, suppress time-exit** |
| `-0.3 ≤ MRC ≤ +0.3` | Independent / mixed | **Default TP, no adjustment** |
| `MRC > +0.3` | Choppy trend (momentum + vol both rising) | **Tighten TP × 0.7, arm trail at TP-2c** |

Two notes on the math:

- **Sign of CMS does not enter the covariance directly.** What matters is whether |CMS| and CRI move together. A trend that gets cleaner over time (|CMS| up, CRI down) is the negative-MRC case regardless of side. Implement with `abs_cms_norm[t] = abs(CMS_norm[t])` in the covariance to remove the side-flip noise.
- **Window length is critical.** 60 observations × ~0.9s = 54s. Shorter windows are too noisy; longer windows lag too much for a 15-min contract. The 54s window means MRC reacts roughly 4× per minute — fast enough to catch a regime change in time to act before the contract expires.

### 3.4 Path Signature — pattern classifier

A discrete classifier over the last 120 observations (~108s, roughly 1/8 of the contract's life). Computes:

- `m_open`: mid at the buffer's oldest observation
- `m_now`: mid at the latest observation
- `m_hwm`: max mid in the buffer
- `m_lwm`: min mid in the buffer
- `t_hwm`: timestamp of HWM
- `t_lwm`: timestamp of LWM
- `range`: `m_hwm - m_lwm`

Then classify by these rules in order:

```
if range < 2.0:
    signature = FLAT                    # nothing happened

elif m_now - m_lwm > 0.7 * range and m_hwm - m_now < 1.5:
    signature = STAIRCASE_UP           # near HWM, trend up

elif m_hwm - m_now > 0.7 * range and m_now - m_lwm < 1.5:
    signature = STAIRCASE_DOWN         # near LWM, trend down

elif m_now > m_open and m_lwm < m_open - 1 and t_lwm < t_hwm and t_hwm > buffer_age * 0.6:
    signature = V_SHAPE                # dipped then recovered

elif m_now < m_open and m_hwm > m_open + 1 and t_hwm < t_lwm and t_lwm > buffer_age * 0.6:
    signature = INVERTED_V             # spiked then collapsed

elif (m_hwm - m_now) < 1.5 and (m_hwm - m_open) > 2 and count_visits(m_hwm, tol=1c) >= 2:
    signature = M_TOP                  # double-touch at HWM, possible reversal down

elif (m_now - m_lwm) < 1.5 and (m_open - m_lwm) > 2 and count_visits(m_lwm, tol=1c) >= 2:
    signature = W_BOTTOM               # double-touch at LWM, possible reversal up

else:
    signature = MIXED
```

`count_visits(level, tol)`: number of times the mid crossed within `tol` of `level` and then moved away by ≥2c. This identifies actual touches rather than noise hovering at one price.

**Trade response:**

| Signature | YES position response | NO position response |
|---|---|---|
| STAIRCASE_UP | Hold, widen TP | **Force exit** — tide is against us |
| STAIRCASE_DOWN | **Force exit** | Hold, widen TP |
| V_SHAPE (recovering up) | Hold, this is working | **Force exit** |
| INVERTED_V | **Force exit** | Hold, this is working |
| M_TOP | **Force exit** — reversion likely | Hold, widen TP |
| W_BOTTOM | Hold, widen TP | **Force exit** |
| FLAT | Default behavior | Default behavior |
| MIXED | Default behavior | Default behavior |

"Force exit" means: cancel resting TP and place a market sell at `bid - 1c` (cross-spread) on the next poll cycle. This is more aggressive than the protective layer's SL — used only when the path signature is unambiguously against the position.

### 3.5 Settlement Convergence Rate (SCR) — "is the market still updating"

```
seconds_left = prob_engine.time_to_expiry_s
distance_from_extreme = min(m_now, 100 - m_now)   # cents to nearest of 0/100
expected_remaining_movement = 50 * sqrt(seconds_left / 900)   # diffusion estimate
SCR = 1.0 - (distance_from_extreme / max(expected_remaining_movement, 1.0))
SCR = clip(SCR, 0.0, 1.0)
```

SCR ≈ 1.0 means the contract is much closer to one extreme than diffusion would suggest given remaining time — high market conviction. SCR ≈ 0.0 means the contract has lots of room left and is still meandering.

**Trade response:**
- `SCR > 0.7` and you are on the converging side → hold to settlement.
- `SCR > 0.7` and you are on the wrong side → exit immediately, the market has decided.
- `SCR < 0.3` → use MRC and Path Signature normally.

---

## 4. The `ContractMomentumAnalyzer` Class

### 4.1 Public surface

```python
# proposed: contract_momentum.py

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PathSignature(Enum):
    FLAT = "FLAT"
    STAIRCASE_UP = "STAIRCASE_UP"
    STAIRCASE_DOWN = "STAIRCASE_DOWN"
    V_SHAPE = "V_SHAPE"
    INVERTED_V = "INVERTED_V"
    M_TOP = "M_TOP"
    W_BOTTOM = "W_BOTTOM"
    MIXED = "MIXED"


@dataclass(frozen=True)
class MomentumReadout:
    momentum_score: float        # CMS_norm in [-1, +1]
    reversion_index: float       # CRI_norm in [0, 1]
    covariance: float            # MRC in [-1, +1]
    path_signature: PathSignature
    convergence_rate: float      # SCR in [0, 1]
    recommended_tp_multiplier: float    # in [0.7, 1.3]
    force_exit: bool             # True = next-poll cross-spread sell


class ContractMomentumAnalyzer:
    """Real-time momentum/reversion classifier on a single ticker's mid stream.

    Stateful. One instance per (ticker, side, fill) lifetime. Reset (or
    construct fresh) on window boundary or new fill.
    """

    BUFFER_LEN = 120           # ~108s at 0.9s polls
    COV_WINDOW = 60            # ~54s
    SMA_WINDOW_S = 120.0       # 2 minutes

    def __init__(self, side: str) -> None:
        self._side = side                          # "yes" or "no"
        self._buf: deque = deque(maxlen=self.BUFFER_LEN)   # (ts, mid)
        self._cms_history: deque = deque(maxlen=self.COV_WINDOW)   # CMS_norm at each tick
        self._cri_history: deque = deque(maxlen=self.COV_WINDOW)   # CRI_norm at each tick
        self._cms: Optional[float] = None
        self._prev_mid: Optional[float] = None
        self._prev_ts: Optional[float] = None
        self._opening_mid: Optional[float] = None

    def update(self, mid_cents: float, ts: float, seconds_left: float) -> MomentumReadout:
        """Feed a new (mid, timestamp, seconds_left) observation and return
        the current readout. Always returns a value — early ticks return
        a "neutral" readout (multiplier=1.0, force_exit=False)."""
        ...
```

### 4.2 Internal state and update sequence

On each `update()` call:

1. **Append to buffer.** `self._buf.append((ts, mid_cents))`. Set `_opening_mid` on first call.
2. **Update CMS.** Compute `r` from `_prev_mid`, smooth into `self._cms`, compute `cms_norm = clip(self._cms / 0.005, -1, +1)`. Push to `_cms_history`.
3. **Update CRI.** Compute SMA over observations within `SMA_WINDOW_S`, residuals, stdev over last 30, normalize to `cri_norm`. Push to `_cri_history`.
4. **Compute MRC.** From paired `(cms_history, cri_history)` arrays, compute covariance of `|cms_history|` and `cri_history`, normalize.
5. **Compute Path Signature.** Run the classification rules from §3.4 over the buffer.
6. **Compute SCR.** Use `seconds_left` and `mid_cents`.
7. **Compute multiplier and force_exit flag.**
8. **Return readout.**

### 4.3 The recommendation logic — single source of truth

```python
def _recommend(self, cms_norm, cri_norm, mrc, path, scr) -> tuple[float, bool]:
    """Combine indicators into a TP multiplier and force-exit flag."""

    # ── Force-exit overrides — checked first ────────────────────────
    against_yes = self._side == "yes" and (
        path == PathSignature.STAIRCASE_DOWN
        or path == PathSignature.INVERTED_V
        or path == PathSignature.M_TOP
    )
    against_no = self._side == "no" and (
        path == PathSignature.STAIRCASE_UP
        or path == PathSignature.V_SHAPE
        or path == PathSignature.W_BOTTOM
    )
    if against_yes or against_no:
        return (0.7, True)

    # ── SCR override — market has decided ───────────────────────────
    if scr > 0.7:
        # Are we on the converging side?
        going_yes = cms_norm > 0
        if (self._side == "yes" and going_yes) or (self._side == "no" and not going_yes):
            return (1.3, False)         # hold to settlement
        else:
            return (0.7, True)          # exit, wrong side of convergence

    # ── MRC-based default ───────────────────────────────────────────
    if mrc < -0.3:
        return (1.3, False)             # clean trend, hold
    elif mrc > 0.3:
        return (0.7, False)             # choppy, tighten but don't force-exit
    else:
        return (1.0, False)             # neutral
```

### 4.4 Edge cases

- **Buffer underfilled.** Until `len(self._buf) >= 30`, return `MomentumReadout(0, 0, 0, FLAT, 0, 1.0, False)` — no opinion.
- **Mid at extreme.** When `mid_cents <= 2` or `mid_cents >= 98`, MRC is meaningless. Force `multiplier = 1.0`, `force_exit = False` — let the protective layer handle these.
- **Stale poll.** If `ts - self._prev_ts > 5.0`, drop the observation as a network gap and do not update CMS (keeps EMA from spiking on stitch-back).
- **Side change.** Class is per-fill. On flip-and-rebuild (BB_PURE never does this; only legacy SCALP_DCA did, and it's killed in CLAUDE.md), construct a new instance.

---

## 5. Integration Points

All references are to the current `polymarket_copy_engine.py` head as of 2026-05-04. Line numbers will drift; use the symbolic anchors when implementing.

### 5.1 Construction — at fill time, not session boundary

Construct one analyzer per filled position. Place the construction inside the BB_PURE fill handler where `_open_position` gets populated. Search anchor: `_execute_bb_pure_signal` and where the engine writes `pos["entry_price"]` and `pos["side"]`.

```python
# Inside the fill-confirmed branch of BB_PURE execution
from contract_momentum import ContractMomentumAnalyzer

pos["_momentum_analyzer"] = ContractMomentumAnalyzer(side=pos["side"])
```

Do **not** construct it at session boundary. The session-level regime classifier at lines 5487–5522 misses the fact that the trade-relevant regime is the *post-entry* regime. Pre-entry chop is informational; post-entry chop is what kills the position.

### 5.2 Feeding — every poll inside `_manage_position`

`_manage_position` starts at line 18523. The orderbook is fetched in many places throughout the function. The cleanest insertion point is right after the protective_maintain block returns (~line 18556), before any TP/SL decisions are made downstream:

```python
# After:   _protective_owned = await self._maintain_protective_order()
# Before:  the pre-expiry flatten block at line 18567

if pos is not None:
    analyzer = pos.get("_momentum_analyzer")
    book = pos.get("_last_book") or await self._client.get_orderbook(pos["ticker"])
    if analyzer is not None and book is not None:
        mid = self._compute_mid_from_book(book, pos["side"])
        if mid is not None:
            seconds_left = max(0.0, 900.0 - (time.time() - self._window_start_time))
            readout = analyzer.update(mid, time.time(), seconds_left)
            pos["_mrc_readout"] = readout    # available to all downstream consumers
```

Do **not** call `get_orderbook` again if the protective layer already fetched it this cycle — pull it from the position state. The protective layer at `_maintain_protective_order` already has the book; storing it as `pos["_last_book"]` is a one-line change there.

### 5.3 Reading — TP recompute path inside protective_maintain

The protective_maintain layer is in `polymarket_copy_engine.py` (search anchor: `def _maintain_protective_order`). The TP recompute logic uses `prob.fair_value` directly:

```python
# Current behavior (illustrative, near where TP target is set):
new_tp = max(int(prob.fair_value), pos["entry_price"] + 5)
```

Modified:

```python
new_tp_base = max(int(prob.fair_value), pos["entry_price"] + 5)
readout = pos.get("_mrc_readout")
if readout is not None:
    # Premium = TP - entry, scaled by MRC multiplier
    premium = new_tp_base - pos["entry_price"]
    scaled_premium = max(2, int(round(premium * readout.recommended_tp_multiplier)))
    new_tp = pos["entry_price"] + scaled_premium
else:
    new_tp = new_tp_base
```

Floor of 2c on `scaled_premium` is critical — without it, a 0.7 multiplier on a 3c premium yields 2.1 → 2 → engine sells at entry+2c which is below maker fees on a 1c spread.

### 5.4 Reading — force-exit handler

A separate code path, after the analyzer feed in §5.2:

```python
if readout is not None and readout.force_exit:
    # Cancel TP, market-sell at bid - 1c
    logger.warning(
        "CopyEngine MRC FORCE-EXIT: %s side=%s path=%s mrc=%.2f scr=%.2f",
        pos["ticker"][-15:], pos["side"],
        readout.path_signature.value, readout.covariance, readout.convergence_rate,
    )
    # Use the same _cancel_tp_order + cross-spread sell pattern the
    # protective SL layer uses; do NOT bypass _place_capped_side_sell.
    await self._cancel_tp_order(pos)
    await self._place_capped_side_sell(
        ticker=pos["ticker"],
        side=pos["side"],
        price=max(1, int(self._best_bid_for_side(book, pos["side"])) - 1),
        count=pos["count"],
        reason="mrc_force_exit",
    )
```

Three rules:

1. Always cancel the resting TP first. The "place-then-pray" pattern was specifically banned in the safety stack (`bdf91cb`-era commits per CLAUDE.md).
2. Use `_place_capped_side_sell` not direct `client.place_order` — that wraps the OVERSELL-GUARD and position-zero gate.
3. Log enough to reconstruct the decision in post-mortem. Path, MRC, SCR are essential.

### 5.5 Replacing the legacy regime classifier

Lines 5487–5522 set `self._session_regime` from BTC indicators. The current architecture (BB_PURE-only) already mostly ignores it — only the legacy TA_FORCED sizing path reads it (line 12095), and TA_FORCED is disabled (`TA_FORCED_ENTRY_ENABLED = False` per CLAUDE.md). However, removing the block prematurely is a separate tracked task.

This document recommends **leaving the legacy classifier in place** for now; the MRC system runs independently and is per-fill, not per-session. When TA_FORCED is fully removed, the regime block at 5487–5522 becomes dead code and can be deleted.

### 5.6 Persistence and analyzer reset

- On engine restart with an open position (rare; SESSION-LOCK path): `_open_position` is reconstructed but `_momentum_analyzer` is not in the persisted state. The analyzer must be re-instantiated empty. The first 30s after restart will return neutral readouts. This is acceptable — restarts are rare and the protective layer covers exits regardless.
- On window boundary with a position still open (also rare; pre-expiry flatten usually closes by then): keep the same analyzer; the contract is the same. But the buffer should be cleared because the new strike resets the contract's economics. Add `analyzer.on_window_boundary()` that clears `_buf`, `_cms_history`, `_cri_history`, but preserves `_side` and `_opening_mid := mid_at_boundary`.

---

## 6. Configuration Knobs

All knobs default OFF / shadow. None of these go live in the same commit that introduces the analyzer.

```python
# user_config.py additions (proposed, do not implement)

# Master switch — when False, analyzer still runs in shadow but no execution
# changes happen. Set True only after §7 validation passes.
MRC_LIVE_ENABLED                  = False

# Subsystem switches — independent, layered
MRC_TP_ADJUST_ENABLED             = False    # use multiplier on TP premium
MRC_FORCE_EXIT_ENABLED            = False    # honor force_exit flag from analyzer
MRC_SUPPRESS_TIME_EXIT_ON_TREND   = False    # under MRC < -0.3, skip pre-expiry flatten

# Tunables — start at the defaults computed in this document
MRC_BUFFER_LEN                    = 120
MRC_COV_WINDOW                    = 60
MRC_TP_MULT_FLOOR                 = 0.7
MRC_TP_MULT_CEIL                  = 1.3
MRC_SIGNATURE_MIN_RANGE_C         = 2.0       # range threshold for FLAT detection
MRC_PATH_DOUBLE_TOUCH_TOL_C       = 1.0
MRC_SCR_HIGH_THRESHOLD            = 0.7
MRC_TP_PREMIUM_FLOOR_C            = 2         # absolute floor on scaled premium

# Shadow logging — always on once analyzer is constructed
MRC_SHADOW_LOG_ENABLED            = True      # write readout to data/trades.db column
```

The decision to launch is **one boolean flip** (`MRC_LIVE_ENABLED = True`) plus enabling whichever subsystems passed validation. The subsystem switches are independent so we can launch (e.g.) TP adjustment while keeping force-exit in shadow if validation says force-exit is too aggressive.

---

## 7. Validation Plan

### 7.1 Phase 0 — code review and unit tests

Before any data collection:

- Unit-test the analyzer against synthetic mid streams:
  - **Pure linear up trend (50 → 80 over 8 min, no noise):** expect CMS_norm ≈ 1.0, CRI_norm ≈ 0, MRC ≈ -1.0, signature = STAIRCASE_UP, multiplier = 1.3.
  - **Pure oscillation (50 ↔ 55 every 30s):** expect CMS_norm ≈ 0, CRI_norm ≈ 1, MRC ≈ 0, signature ∈ {MIXED, FLAT}, multiplier = 1.0.
  - **Choppy uptrend (50 → 80 with 4c oscillations):** expect |CMS_norm| > 0.5, CRI_norm > 0.5, MRC > 0.3, multiplier = 0.7.
  - **V-shape (50 → 40 → 55 over 6 min):** expect signature = V_SHAPE, force_exit = True for NO holder.
  - **M-top (50 → 65 → 60 → 65 → 62 over 8 min, two HWM touches):** expect signature = M_TOP, force_exit = True for YES holder.
- Verify analyzer construction has no async dependency. Should be importable in `tests/test_contract_momentum.py` without any Kalshi/Coinbase mocks.
- Verify analyzer adds <1ms to a single `_manage_position` cycle. The protective layer already runs on a 0.9s budget; adding the analyzer must not push it over.

### 7.2 Phase 1 — shadow-mode logging (no execution change)

`MRC_SHADOW_LOG_ENABLED = True`, all other MRC_* flags False. Collect for ≥7 days or ≥200 fills, whichever comes first. Persist on every readout to a new table:

```sql
CREATE TABLE mrc_readouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,             -- foreign key to kalshi_trades
    ts REAL NOT NULL,
    seconds_left REAL NOT NULL,
    mid_cents REAL NOT NULL,
    cms_norm REAL,
    cri_norm REAL,
    mrc REAL,
    path_signature TEXT,
    scr REAL,
    multiplier REAL,
    force_exit INTEGER,
    -- For correlation analysis
    entry_price INTEGER,
    side TEXT,
    final_pnl_cents REAL                   -- backfilled at close
);
CREATE INDEX idx_mrc_trade ON mrc_readouts(trade_id);
```

Write one row per `_manage_position` cycle that calls the analyzer. At trade close, backfill `final_pnl_cents` on every row for that trade — this lets us correlate any in-flight readout with eventual outcome.

### 7.3 Phase 1 analysis — what to look for before going live

For each indicator, run these queries against `mrc_readouts`:

1. **MRC vs realized PnL.** Group readouts by `mrc` bucket (`< -0.3`, `[-0.3, +0.3]`, `> +0.3`). For each bucket, compute median of `final_pnl_cents - (current_mid - entry)`, i.e., the "future PnL from this point" given entry side. If the negative-MRC bucket shows positive future-PnL (continuation pays) and the positive-MRC bucket shows negative future-PnL (chop reverses), the MRC signal is real.
2. **Path Signature vs MAE.** Group by signature, compute distribution of subsequent MAE (in the next 60s) for trades where the signature appeared mid-trade. M_TOP (for YES) and W_BOTTOM (for NO) should show large subsequent adverse moves.
3. **Force-exit hypothetical.** For every readout where `force_exit = True`, compute the PnL if the engine had exited at `mid_cents - 1` at that timestamp vs the actual realized PnL at trade close. Sum the difference: positive sum = force-exit would help, negative sum = it would hurt.
4. **Multiplier hypothetical.** For trades whose realized MFE exceeded the static TP, compute "what if TP had been multiplied by readout.multiplier from the readout immediately after fill?" Did the higher multiplier capture more of the MFE without converting wins into losses?

### 7.4 Phase 1 acceptance criteria — go-live thresholds

Each subsystem is gated independently:

| Subsystem | Acceptance criterion | Action if pass | Action if fail |
|---|---|---|---|
| TP multiplier | Hypothetical PnL improves by ≥10c per trade on average over 100+ fills | Enable `MRC_TP_ADJUST_ENABLED` | Iterate on the multiplier function; do not enable |
| Force exit | Hypothetical PnL improves by ≥5c per fill on the subset where `force_exit=True`; false-positive rate (force-exit when subsequent move was favorable) < 30% | Enable `MRC_FORCE_EXIT_ENABLED` | Tighten signature thresholds (raise double-touch tolerance, require longer time-at-extreme) |
| Suppress time-exit on negative MRC | On the subset where MRC < -0.3 within 60s of pre-expiry flatten trigger, the held-to-settle PnL is better than the flatten PnL on average | Enable `MRC_SUPPRESS_TIME_EXIT_ON_TREND` | Leave time-exit untouched |

### 7.5 Phase 2 — gradual live rollout

Order: TP multiplier first (least aggressive), then force-exit (more aggressive), then time-exit suppression (highest variance). One subsystem per week. Daily check: total P&L delta vs the prior 7-day rolling average; if any subsystem drives a 2σ negative deviation in the first 48 hours, flip the switch off and post-mortem before re-enabling.

### 7.6 Reversion of the legacy regime classifier

Once Phase 2 is stable for 4 weeks, remove `polymarket_copy_engine.py:5487–5522` (the `_session_regime` classifier) plus any reads of `_session_regime` (the only live consumer is the dead TA_FORCED sizing block at 12095, which itself should be removed when TA_FORCED is fully retired). MRC subsumes its function with better data.

---

## 8. Interaction With Existing Systems

### 8.1 Compatibility with V2 NO-side TP reduction

`RESEARCH_OPTIMAL_ENTRY_EXIT_V2.md` reduced NO-side TP premiums to 5/8/12 (from 9/15/22) because NO trades reach +8c MFE only 58.8% of the time. The MRC multiplier **stacks on top**:

- NO trade entered at 50c, V2-prescribed TP premium = 5c (slow regime), so static TP = 55c.
- MRC at the moment of TP recompute: -0.5 (clean trend down — favorable to NO).
- MRC multiplier = 1.3, so scaled premium = round(5 × 1.3) = 7c (with floor 2). Final TP = 57c.

The two layers are independent: V2 sets the **base premium** (where the contract typically peaks), MRC sets the **dynamic adjustment** (whether the current path supports widening). Both are subordinate to the entry-price floor (`entry + 2`).

### 8.2 Compatibility with BB_PURE entry

BB_PURE (`bb_pure.py`) only handles entry. It does not consume `prob_engine.fair_value` post-entry — that's the protective layer's job. MRC does not touch entry logic. The two are orthogonal.

### 8.3 Compatibility with the protective layer

The protective layer (`_maintain_protective_order` + `protective_math.py`) maintains exactly one resting Kalshi-side sell. MRC does **not** add a second order. It modifies the price of the existing TP order. The cancel-then-replace pattern the protective layer uses is the same flow MRC piggybacks on.

The MRC force-exit is the only place a *second* order shape is introduced (cross-spread sell instead of resting TP). Implement it as: cancel resting TP, place market sell. This is the same flow as the existing `PROTECTIVE [SL]` transition — the code is already there and tested.

### 8.4 Compatibility with FVG and ATM_REVERSION (both off)

These are off in the live config (`PAPER_FVG_ENABLED = False`, `ATM_REVERSION_ENABLED = False`). MRC does not interact with either. If they are ever re-enabled, they would consume `prob.fair_value` for entry and would need their own analyzer instance per fill — same pattern as BB_PURE.

### 8.5 Compatibility with pre-expiry flatten (Patches #7, #13)

`PRE_EXPIRY_FLATTEN_ENABLED = True` forces a market-sell at 1c when fewer than 180 seconds remain. MRC's "suppress time-exit" subsystem is **only** allowed to override pre-expiry flatten if `MRC < -0.3` AND `SCR > 0.7` AND `path_signature` aligns with our side. Even then, it must not suppress the final 60-second flatten — too risky. Acceptable suppression window: `60s ≤ seconds_left < 180s`.

This is a hard rule. Do not let MRC weaken the pre-expiry safety net under any other condition.

### 8.6 Database migration

One new table (`mrc_readouts`, §7.2). One nullable column on `kalshi_trades`:

```sql
ALTER TABLE kalshi_trades ADD COLUMN mrc_at_entry REAL;
ALTER TABLE kalshi_trades ADD COLUMN mrc_path_at_close TEXT;
```

Backfill is not necessary; new fields are populated forward only.

---

## 9. Implementation Skeleton — Pseudocode for Reviewer

This is illustrative, not final. Final code must include all defensive checks (None handling, division-by-zero, deque emptiness, etc.) consistent with the existing engine style.

```python
# contract_momentum.py — proposed file, ~250 lines

import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class PathSignature(Enum):
    FLAT = "FLAT"
    STAIRCASE_UP = "STAIRCASE_UP"
    STAIRCASE_DOWN = "STAIRCASE_DOWN"
    V_SHAPE = "V_SHAPE"
    INVERTED_V = "INVERTED_V"
    M_TOP = "M_TOP"
    W_BOTTOM = "W_BOTTOM"
    MIXED = "MIXED"


@dataclass(frozen=True)
class MomentumReadout:
    momentum_score: float
    reversion_index: float
    covariance: float
    path_signature: PathSignature
    convergence_rate: float
    recommended_tp_multiplier: float
    force_exit: bool


class ContractMomentumAnalyzer:
    BUFFER_LEN = 120
    COV_WINDOW = 60
    SMA_WINDOW_S = 120.0
    EMA_ALPHA = 0.3
    CMS_NORM = 0.005
    CRI_NORM = 3.0
    MRC_NORM = 0.05

    def __init__(self, side: str) -> None:
        self._side = side
        self._buf: deque = deque(maxlen=self.BUFFER_LEN)
        self._cms_history: deque = deque(maxlen=self.COV_WINDOW)
        self._cri_history: deque = deque(maxlen=self.COV_WINDOW)
        self._cms: Optional[float] = None
        self._prev_mid: Optional[float] = None
        self._prev_ts: Optional[float] = None

    def on_window_boundary(self, mid_at_boundary: float) -> None:
        self._buf.clear()
        self._cms_history.clear()
        self._cri_history.clear()
        self._cms = None
        self._prev_mid = mid_at_boundary
        self._prev_ts = time.time()

    def update(self, mid_cents: float, ts: float, seconds_left: float) -> MomentumReadout:
        # 1) buffer + return early on first observation
        self._buf.append((ts, mid_cents))
        if self._prev_mid is None:
            self._prev_mid = mid_cents
            self._prev_ts = ts
            return self._neutral_readout()

        # 2) drop stale ticks
        if self._prev_ts is not None and (ts - self._prev_ts) > 5.0:
            self._prev_mid = mid_cents
            self._prev_ts = ts
            return self._neutral_readout()

        # 3) CMS
        r = (mid_cents - self._prev_mid) / max(self._prev_mid, 1e-6)
        if self._cms is None:
            self._cms = r
        else:
            self._cms = self.EMA_ALPHA * r + (1 - self.EMA_ALPHA) * self._cms
        cms_norm = max(-1.0, min(1.0, self._cms / self.CMS_NORM))
        self._cms_history.append(cms_norm)

        # 4) CRI — variance around 2-min SMA
        cutoff = ts - self.SMA_WINDOW_S
        sma_window = [m for (t, m) in self._buf if t >= cutoff]
        if len(sma_window) >= 5:
            sma = sum(sma_window) / len(sma_window)
            recent = list(self._buf)[-30:]
            residuals = [m - sma for (_, m) in recent]
            mean_r = sum(residuals) / len(residuals)
            var = sum((x - mean_r) ** 2 for x in residuals) / len(residuals)
            cri_raw = math.sqrt(var)
            cri_norm = min(1.0, cri_raw / self.CRI_NORM)
        else:
            cri_norm = 0.0
        self._cri_history.append(cri_norm)

        # 5) MRC — covariance of |CMS_norm| and CRI_norm over the cov window
        if len(self._cms_history) >= 10:
            abs_cms = [abs(c) for c in self._cms_history]
            cri_arr = list(self._cri_history)
            mean_a = sum(abs_cms) / len(abs_cms)
            mean_c = sum(cri_arr) / len(cri_arr)
            cov = sum((abs_cms[i] - mean_a) * (cri_arr[i] - mean_c) for i in range(len(abs_cms))) / len(abs_cms)
            mrc = max(-1.0, min(1.0, cov / self.MRC_NORM))
        else:
            mrc = 0.0

        # 6) Path signature
        path = self._classify_path(ts)

        # 7) SCR
        if seconds_left > 0:
            distance = min(mid_cents, 100.0 - mid_cents)
            expected = 50.0 * math.sqrt(seconds_left / 900.0)
            scr = max(0.0, min(1.0, 1.0 - distance / max(expected, 1.0)))
        else:
            scr = 1.0

        # 8) Recommendation
        multiplier, force_exit = self._recommend(cms_norm, cri_norm, mrc, path, scr)

        # 9) Update state
        self._prev_mid = mid_cents
        self._prev_ts = ts

        return MomentumReadout(
            momentum_score=cms_norm,
            reversion_index=cri_norm,
            covariance=mrc,
            path_signature=path,
            convergence_rate=scr,
            recommended_tp_multiplier=multiplier,
            force_exit=force_exit,
        )

    def _classify_path(self, ts: float) -> PathSignature:
        if len(self._buf) < 30:
            return PathSignature.FLAT
        mids = [m for (_, m) in self._buf]
        timestamps = [t for (t, _) in self._buf]
        m_open = mids[0]
        m_now = mids[-1]
        m_hwm = max(mids)
        m_lwm = min(mids)
        i_hwm = mids.index(m_hwm)
        i_lwm = mids.index(m_lwm)
        rng = m_hwm - m_lwm
        age = timestamps[-1] - timestamps[0]
        if age <= 0:
            return PathSignature.FLAT
        if rng < 2.0:
            return PathSignature.FLAT
        # ... rules from §3.4 ...
        # (omitted in skeleton; see §3.4 for full rule set)
        return PathSignature.MIXED

    def _recommend(self, cms_norm, cri_norm, mrc, path, scr) -> Tuple[float, bool]:
        # ... rules from §4.3 ...
        return (1.0, False)

    @staticmethod
    def _neutral_readout() -> MomentumReadout:
        return MomentumReadout(
            momentum_score=0.0,
            reversion_index=0.0,
            covariance=0.0,
            path_signature=PathSignature.FLAT,
            convergence_rate=0.0,
            recommended_tp_multiplier=1.0,
            force_exit=False,
        )
```

---

## 10. Open Questions for Pre-Implementation Review

These need explicit answers before any of this is wired up. Document the answers inline in the implementation PR.

1. **Polling cadence under load.** When the engine is in a busy session (whale alert + tape spike), `_manage_position` cycles can stretch from 0.9s to ~2.5s. The MRC buffer assumes ~0.9s ticks. Should the analyzer time-decay sample weights, or should it accept variable spacing as-is? **Recommendation:** accept as-is — covariance over 60 samples is robust to variable spacing within ±2× of the mean.

2. **Mid source.** Should the analyzer feed off the orderbook mid (best_bid + best_ask)/2, or the microprice (size-weighted mid)? Microprice is less noisy on thin books but lags slightly. **Recommendation:** start with simple mid for shadow phase; switch to microprice if shadow data shows excessive false-positives on M_TOP from one-tick wiggles in the offer.

3. **Side-aware path interpretation.** The current §3.4 table interprets M_TOP as "force-exit YES." But for a NO position whose original entry was at 60c and price has fallen to 38c with M_TOP at 38c, the M_TOP is *favorable* — it suggests the price is unlikely to bounce. The classifier needs to consider whether the touch zone is *for* or *against* the position relative to entry, not just relative to side. **Recommendation:** add a `position_direction` parameter to the analyzer at construct time: `+1` for "we want the contract to go up," `-1` otherwise. Reinterpret signatures relative to that.

4. **Conflict with the protective layer's MFE-aware trail.** `protective_math.py` already implements an MFE-aware trail that re-arms TP above FVG-close as bid climbs. MRC's TP multiplier might *fight* the trail (multiplier 0.7 tightens; trail wants to widen on rising MFE). **Recommendation:** the MRC multiplier should only modify the *base* TP premium, not the trail's already-armed TP. If the trail has armed a TP higher than the MRC-multiplied target, leave the trail's TP alone.

5. **Cold-start blackout.** First 30s of a position have neutral readouts. During those 30s, force_exit cannot fire. Is that an acceptable blind spot? **Recommendation:** yes — most fills happen at the entry price and the protective SL covers catastrophic adverse moves regardless. The cold-start period is short relative to the 15-minute window.

---

## 11. Summary

**The thesis:** the contract's own pricing, when read as a probability evolution rather than a number, is the cleanest signal in the engine. The current architecture acts on it at entry (BB_PURE mispricing) and ignores it after. Adding a per-fill `ContractMomentumAnalyzer` that watches CMS, CRI, MRC, path signature, and SCR gives the exit layer the same kind of math the entry layer already has.

**The gain:** dynamic TP that adapts to the regime instead of a static premium table. Force-exit on path patterns that today's engine has no way to see. Suppression of time-exit on clean trends — the cases where the static rules currently TP us out of trades that would have settled deeper in profit.

**The risk:** every dynamic exit is a chance to talk ourselves out of a winning trade. The validation plan in §7 is structured to gate each subsystem on hypothetical PnL improvement before flipping it live. The shadow-mode logging in §7.2 is the cheap part; do that first regardless of whether any of the live behavior ever ships.

**The integration cost:** one new file (~250 lines). One database migration. ~30 lines of changes in `_manage_position`. ~10 lines of changes in `_maintain_protective_order` and the BB_PURE fill handler. No changes to `bb_pure.py`, `price_feed.py`, or the entry pipeline.

---

*End of research document. Do not implement any changes without re-reading §7 (Validation Plan) and §10 (Open Questions) in full.*
