# Research Paper Upgrades — 2026-03-16

**Source:** `to-do/kalshi_limit_order_arbitrage_research_paper.md`
**Prepared by:** OpenAI ChatGPT
**Implemented by:** Claude (claude-sonnet-4-6)

---

## Overview

The research paper outlined a microstructure-first philosophy for Kalshi limit-order trading. The existing HFT engine was already well-architected but lacked several execution-quality controls the paper treats as mandatory: stale-book protection, depth validation, imbalance-aware filtering, reprice loop limits, and post-fill drift logging. All improvements were made to the existing codebase without structural rewrites.

---

## Files Modified

| File | Nature of Change |
|------|-----------------|
| `config.py` | 7 new constants |
| `kalshi_client.py` | 4 new properties on `KalshiOrderBook` |
| `signal_logger.py` | 5 new DB column migrations |
| `hft_engine.py` | 6 behavioral improvements |

---

## Change 1 — Stale Book Gate

**Paper reference:** §10.1 — "If any threshold is exceeded, the order should be blocked or canceled."

**Problem:** The HFT engine polled the order book via REST every 4 seconds. Between polls, the book snapshot aged in memory. If an opportunity was evaluated at second 3.9 of a poll cycle, it was acting on a 3.9-second-old snapshot — far outside the paper's 1.5s threshold.

**Approach:** Added `book_age_ms` as a computed property on `KalshiOrderBook` that returns `now_ms - fetched_at_ms`. Both `_check_arb` and `_check_scalp_entry` now check this value before any order logic. If the book is older than `HFT_MAX_BOOK_AGE_MS = 1500ms`, the evaluation is immediately rejected with reason code `stale_book`.

**Files touched:**
- `kalshi_client.py` — `book_age_ms` property on `KalshiOrderBook`
- `config.py` — `HFT_MAX_BOOK_AGE_MS: int = 1500`
- `hft_engine.py` — gate added at top of `_check_arb` and `_check_scalp_entry`

---

## Change 2 — Opposite-Side Depth Gate

**Paper reference:** §8 — "displayed opposite-side depth is above `min_depth`"

**Problem:** The engine could enter positions on contracts with very thin opposite-side books. A thin book means the liquidity providing our entry fill can vanish or reprice before execution, resulting in missed fills or worse adverse selection.

**Approach:** Added `top_yes_qty` and `top_no_qty` properties to `KalshiOrderBook` (the quantity at the single best level). Then in `_check_scalp_entry`, used the existing `liquidity_within(side, 2)` helper — which aggregates depth within 2 cents of the best price on the relevant side — and compared against `HFT_MIN_DEPTH_CONTRACTS = 10`. For a YES buy, the relevant depth is the NO-bid stack (those are our sellers). For a NO buy, the YES-bid stack.

**Files touched:**
- `kalshi_client.py` — `top_yes_qty`, `top_no_qty` properties
- `config.py` — `HFT_MIN_DEPTH_CONTRACTS: int = 10`
- `hft_engine.py` — Gate 4 block in `_check_scalp_entry`, reject reason `insufficient_depth`

---

## Change 3 — Imbalance Gate

**Paper reference:** §8 — "imbalance supports the chosen side"; §7.1 — "microprice from depth imbalance"

**Problem:** The engine entered on directional signals (CALL/PUT from the main candle engine) without checking whether the live order book's supply/demand balance supported the direction. A strong imbalance opposing the trade is an early adverse selection signal.

**Approach:** Added `imbalance` as a computed property on `KalshiOrderBook`:

```
imbalance = (top_yes_qty - top_no_qty) / (top_yes_qty + top_no_qty)
```

Range is −1 to +1. Positive = YES-demand dominant (bullish). Negative = NO-demand dominant (bearish). The gate in `_check_scalp_entry` rejects a YES entry if `imbalance < HFT_IMBALANCE_OPPOSE_THRESHOLD` (default −0.60), meaning NO buyers are dominant by roughly an 80/20 ratio. The threshold is flipped symmetrically for NO entries. The imbalance value is also written to `hft_log` on every entered trade for future calibration.

**Files touched:**
- `kalshi_client.py` — `imbalance` property
- `config.py` — `HFT_USE_IMBALANCE_GATE: bool = True`, `HFT_IMBALANCE_OPPOSE_THRESHOLD: float = -0.60`
- `hft_engine.py` — Gate 5 block in `_check_scalp_entry`, reject reason `imbalance_opposes_side`

---

## Change 4 — Microprice Computation

**Paper reference:** §7.1 — "microprice from depth imbalance"; §5.2 — "compute depth imbalance"

**Problem:** The engine had no microstructure-based fair value estimate. The standard midprice treats both sides symmetrically, which ignores the information in order book imbalance.

**Approach:** Added `microprice_cents` as a computed property on `KalshiOrderBook` using the standard imbalance-weighted formula:

```
microprice = (best_yes_ask * top_yes_qty + best_yes_bid * top_no_qty) / (top_yes_qty + top_no_qty)
```

Interpretation: when YES demand (top_yes_qty) is high relative to NO demand, the microprice sits closer to the ask — reflecting upward price pressure. When NO demand dominates, it sits closer to the bid. This is a better point estimate of fair value than the simple mid. Currently the microprice is logged on every entered scalp (`microprice_cents` column in `hft_log`) but is not yet used for limit price selection — the existing mid-spread limit logic is preserved. The logged values will enable calibration of whether microprice-based entries improve fill quality.

**Files touched:**
- `kalshi_client.py` — `microprice_cents` property
- `hft_engine.py` — passed to `log_hft_eval` on entry
- `signal_logger.py` — `microprice_cents REAL` migration added to `hft_log`

---

## Change 5 — Near-Expiry Edge Tightening

**Paper reference:** §11 — "ultra-near-expiry markets can reprice violently"; §9.1 — "tighter caps near expiry"

**Problem:** The engine applied the same net edge floor (`HFT_SCALP_NET_EDGE_FLOOR = 2.0¢`) regardless of how much time remained in the contract window. Near expiry, adverse selection sharpens, resolution risk increases, and the queue edge the paper describes degrades as everyone races the close. A 2¢ edge that's fine at 10 minutes is marginal at 4 minutes.

**Approach:** Added a secondary floor check inside `_check_scalp_entry` that activates when `contract.minutes_to_expiry < HFT_TIGHTEN_INVENTORY_MINUTES` (default 5 minutes). In that zone, the required net edge is raised by 2¢:

```python
tightened_floor = HFT_SCALP_NET_EDGE_FLOOR + 2.0  # 4.0¢ instead of 2.0¢
```

This is additive so both thresholds remain independently tunable. The hard entry cutoff (`HFT_MIN_MINUTES_REMAINING = 2.5`) is unchanged. Rejection reason is `near_expiry_edge_insufficient`.

**Files touched:**
- `config.py` — `HFT_TIGHTEN_INVENTORY_MINUTES: float = 5.0`
- `hft_engine.py` — tightening gate after net_edge check in `_check_scalp_entry`

---

## Change 6 — Reprice Count Limit

**Paper reference:** §12.2 — "max_reprices_per_order: 3"; §19 — "infinite cancel-replace churn"

**Problem:** The `_check_pending_fill` method could step the limit price up by 1¢ every 15 seconds until `HFT_SCALP_ENTRY_TIMEOUT` (30 seconds). This created up to 2 reprices per order, but with no explicit cap and no tracking. The paper flags infinite cancel-replace churn as a named failure mode.

**Approach:** Added `reprice_count: int = 0` to the `HFTPosition` dataclass. In `_check_pending_fill`, before the step logic, an explicit check aborts the entry if `pos.reprice_count >= HFT_MAX_REPRICES_PER_ORDER`. When a step does execute, `pos.reprice_count` is incremented. The count is visible in the step log line for diagnostics.

**Files touched:**
- `hft_engine.py` — `reprice_count` field on `HFTPosition`; count check and increment in `_check_pending_fill`
- `config.py` — `HFT_MAX_REPRICES_PER_ORDER: int = 3`

---

## Change 7 — Post-Fill Drift Logging

**Paper reference:** §10.3 — "Every fill should record the book at fill time, +1 second, +3 seconds, +10 seconds"; §18 Phase A — "Are fills followed by positive short-horizon drift or negative drift?"

**Problem:** Without post-fill drift data, it is impossible to distinguish between fills that captured a genuine microstructure edge (bid continues rising after a YES buy) and toxic fills (bid immediately falls — meaning the fill happened because informed sellers were unloading). This is the primary diagnostic for whether the scalp strategy has real edge.

**Approach:** Added `_log_post_fill_drift(log_row_id, ticker, side)` as an async method on `HFTEngine`. It is spawned as an `asyncio.create_task` immediately after fill confirmation in `_check_pending_fill`, so it never blocks the main HFT poll loop. The task sleeps sequentially (1s, then 2s more, then 7s more) and fetches the live order book at each offset, writing the best bid for the filled side into `drift_1s_cents`, `drift_3s_cents`, `drift_10s_cents` in `hft_log`.

Interpretation: positive drift (bid rises after a YES fill) = good microstructure capture. Negative drift = the fill was toxic. After enough settled fills, these columns directly answer Phase A of the research plan.

**Files touched:**
- `hft_engine.py` — `_log_post_fill_drift` method; `asyncio.create_task` call in `_check_pending_fill`
- `signal_logger.py` — migrations for `drift_1s_cents`, `drift_3s_cents`, `drift_10s_cents` in `hft_log`
- `config.py` — `HFT_POST_FILL_DRIFT_SECONDS: list = [1, 3, 10]`

---

## DB Schema Changes

All schema changes are applied via the existing migration pattern in `SignalLogger.initialize()` — `ALTER TABLE ... ADD COLUMN` is attempted and silently ignored if the column already exists. No manual migration is needed.

New columns added to `hft_log`:

| Column | Type | Populated by |
|--------|------|-------------|
| `imbalance` | REAL | Every entered scalp at decision time |
| `microprice_cents` | REAL | Every entered scalp at decision time |
| `drift_1s_cents` | REAL | Background task, 1s after fill |
| `drift_3s_cents` | REAL | Background task, 3s after fill |
| `drift_10s_cents` | REAL | Background task, 10s after fill |

---

## What Was Not Implemented

The research paper also recommends a full Kalshi WebSocket order book feed (§2.3, §5.1) to replace REST polling. This would be the highest-impact remaining improvement — it would reduce book age from ~0–4s (REST poll cycle) to near-zero. It was not implemented in this session because it requires a new `market_data.py` module, WebSocket reconnect logic, snapshot + delta application, and sequence integrity tracking. The stale-book gate (Change 1) partially mitigates the REST lag by blocking trades on stale snapshots, but the right fix is to move to the WebSocket feed.

Other paper items deferred:
- Deterministic client order IDs (§12.1)
- Order groups for burst control (§13)
- Cross-market structure arbitrage (§3.3)
- Full YAML config externalization (§15)

---

## Engine Restart Log

Stopped: `BTCBiasEngine` NSSM service (PIDs 14068, 15680 — running since 2026-03-15 17:22)
Started: 2026-03-16 17:31:19
Balance at restart: **$33.86**
Scrub on startup: 24 pending orders settled — **14W / 10L (58.3% WR)**
