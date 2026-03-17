# Next Session Synopsis
**Last updated:** 2026-03-14 (end of Session 07)
**Engine status:** LIVE, trading, service running as BTCBiasEngine (NSSM)

---

## What Is Running Right Now

**Service:** `BTCBiasEngine` Windows Service via NSSM — starts on boot, auto-restarts.
**Python:** `F:\Trading\btc-bias-engine\venv\Scripts\python.exe`
**Log:** `F:\Trading\btc-bias-engine\data\engine.log`
**Ledger:** `cd F:\Trading\btc-bias-engine && python ledger.py`

**Current balance:** ~$36.53 (synced from Kalshi at last startup)

---

## What Changed in Session 07

### 1. Dynamic Position Sizing (Rolling WR Tiers)
`MAX_PCT_EQUITY` now auto-scales based on last N settled trades.
Config tiers in `config.py`:
```python
DYNAMIC_SIZING_TIERS = [
    (30, 0.65, 8.0),   # 30+ trades, WR >= 65% → 8%
    (30, 0.58, 5.0),   # 30+ trades, WR >= 58% → 5%
    (20, 0.55, 3.5),   # 20+ trades, WR >= 55% → 3.5%
]
DYNAMIC_SIZING_FLOOR_PCT = 2.0   # fallback
DYNAMIC_SIZING_LOOKBACK  = 20    # last N settled trades evaluated
```
Implementation: `position_manager.py:_compute_dynamic_max_pct()` — synchronous sqlite3 query.
Logs WARNING on tier change.

### 2. KalshiOrderBook + get_orderbook()
Full bid/ask depth stacks in `kalshi_client.py`:
```python
book = await client.get_orderbook(ticker)
book.best_yes_ask, book.best_no_ask   # best prices
book.spread_cents                      # bid-ask spread
book.liquidity_within("yes", 5)        # total contracts within 5¢ of best
book.fill_cost("yes", 3)               # cost in cents to fill 3 contracts at market
```
Handles Kalshi API v2 quirks: `orderbook_fp` key, `yes_dollars`/`no_dollars` (dollar floats).

### 3. HFT Engine (hft_engine.py) — Strategy 1: YES+NO Arbitrage
When `yes_ask + no_ask < 100¢`, buy both sides. Guaranteed $1.00 payout regardless of BTC direction.
- Config: `HFT_ARB_MIN_EDGE_CENTS = 2.0`, `HFT_ARB_MAX_CONTRACTS = 5`
- Logs both legs to `hft_log` before placing. Updates with fill/pnl data.

### 4. HFT Engine — Strategy 2: Directional Scalping
Uses `decision.expected_wr` (calibrated regime×session WR) as probability — NOT raw `signal.confidence`.
Edge model:
```
gross_edge = expected_wr_cents - market_ask_cents
net_edge   = gross_edge - spread_cents   (conservative friction proxy)
```
Gates: regime filter, bad hour filter, submit lock, no-oppose rule, additive cap.
- Config: `HFT_SCALP_GROSS_EDGE_FLOOR = 5.0`, `HFT_SCALP_NET_EDGE_FLOOR = 2.0`
- `HFT_SCALP_TARGET_CENTS = 8.0`, `HFT_SCALP_STOP_CENTS = 6.0`
- `HFT_SCALP_ENTRY_TIMEOUT = 30.0`, `HFT_SCALP_MAX_PER_WINDOW = 6`

### 5. HFT Telemetry (hft_log table)
New table `hft_log` in `trades.db` — 24 columns. Captures every evaluation: entered or rejected.
- Every rejection has `reject_reason` (e.g. `regime_unfavorable`, `gross_edge_insufficient`, `opposes_main_position`)
- Fill and exit data updated in-place by `log_row_id`
- Methods: `signal_logger.log_hft_eval(**fields) -> int`, `update_hft_row(row_id, **fields)`

### 6. Main/HFT Coordination
Three rules, all using asyncio shared refs (single-threaded, no locks needed):
1. **Submit lock**: `_hft_submit_lock = {"active": bool, "ticker": str}` — HFT skips entry while main is mid-`place_order`
2. **No-oppose**: `HFT_ALLOW_OPPOSE_MAIN = False` — HFT won't take opposite side of main's open position
3. **Additive cap**: `HFT_MAX_ADDITIVE_CONTRACTS = 2` — total contracts in same direction (main + HFT combined)

### 7. ENGINE_OVERVIEW.md
Created at project root. Holistic reference: architecture layers, performance data, backtest WR matrix, full config reference, file inventory, known gotchas.

---

## Immediate Next Steps (HIGHEST PRIORITY)

### A. Run Live Validation Queries
Once market opens and `hft_log` accumulates rows. **Run in this order — each query informs whether the next matters.**

**Step 1 — Where is scalp flow dying?**
```sql
SELECT reject_reason, COUNT(*) AS n
FROM hft_log
WHERE strategy = 'SCALP' AND decision = 'rejected'
GROUP BY reject_reason
ORDER BY n DESC;
```

**Step 2 — What gross-edge is the engine actually seeing?**
```sql
SELECT
  ROUND(MIN(gross_edge_cents), 2) AS min_gross,
  ROUND(AVG(gross_edge_cents), 2) AS avg_gross,
  ROUND(MAX(gross_edge_cents), 2) AS max_gross,
  COUNT(*) AS n
FROM hft_log
WHERE strategy = 'SCALP'
  AND gross_edge_cents IS NOT NULL;
```

**Step 3 — Edge distribution (separates "floor too tight" from "premise thin")**
```sql
SELECT
  CASE
    WHEN gross_edge_cents < 2 THEN '<2'
    WHEN gross_edge_cents < 4 THEN '2-4'
    WHEN gross_edge_cents < 6 THEN '4-6'
    WHEN gross_edge_cents < 8 THEN '6-8'
    ELSE '8+'
  END AS bucket,
  COUNT(*) AS n
FROM hft_log
WHERE strategy = 'SCALP'
  AND gross_edge_cents IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

**Step 4 — Fill slippage and P&L (only run after Step 3 shows entries are happening)**
```sql
-- Slippage vs limit price
SELECT ROUND(AVG(fill_cents - entry_limit_cents), 2) AS avg_slippage,
       COUNT(*) AS filled
FROM hft_log WHERE fill_cents IS NOT NULL AND strategy = 'SCALP';

-- P&L by strategy
SELECT strategy, COUNT(*) AS trades,
       ROUND(SUM(pnl), 4) AS total_pnl,
       ROUND(AVG(pnl), 4) AS avg_pnl
FROM hft_log WHERE pnl IS NOT NULL GROUP BY strategy;
```

**Interpretation guide:**
- `gross_edge_insufficient` dominates → check Step 2/3 before touching the floor:
  - avg gross ~3-4¢: floor of 5.0 is slightly tight → lower to 3.0-3.5 for observation
  - avg gross ~1-2¢: the scalp layer doesn't have enough raw edge; loosening gates won't help
- `regime_unfavorable` or `bad_hour` dominate → HFT inheriting too much of main's conservatism; consider `HFT_BAD_HOUR_OVERRIDE` config flag
- Arb never fires → normal; requires deep book mispricing; watch log for `ARB near-miss` entries
- **Do not optimize for more trades yet** — optimize for understanding whether trades that would pass are better than the rejected set

### B. Watch for Policy Mismatch
HFT inherits main's `FilterDecision.reason` for bad-hour and regime checks. If main is regularly rejecting
signals for `BAD_HOUR` but HFT could still scalp (different risk profile), add explicit
`HFT_BAD_HOUR_OVERRIDE = True` config flag.

---

## Second Priority: Coordination Hardening

After validation data is available:
1. **Shared exposure accounting** — total notional risk (main + HFT) per ticker per window
2. **Per-ticker HFT cooldown** — after a main fill, pause HFT on same ticker for N seconds
3. **Conditional additive rule** — allow HFT to add in same direction only when main position is profitable

---

## Third Priority: Replace Proxy Friction with Measured Friction

Current `net_edge = gross_edge - spread_cents` is conservative proxy.
After 50+ `hft_log` rows, bucket by `spread_cents`, `minutes_to_expiry`, `regime`, `side`.
Compare `entry_limit_cents`, `fill_cents`, `exit_cents`, `pnl` to build real `estimated_friction_cents` model.

---

## Key Operational Commands

```powershell
# Check service status
Get-Service BTCBiasEngine

# Restart service (run as admin)
Stop-Service BTCBiasEngine -Force; Start-Service BTCBiasEngine

# View live log
Get-Content 'F:\Trading\btc-bias-engine\data\engine.log' -Tail 30 -Wait
```

```bash
# Trade ledger
cd F:\Trading\btc-bias-engine && python ledger.py

# Inspect order book (live)
python inspect_orderbook.py --watch

# Check hft_log (sqlite3 CLI)
sqlite3 data/trades.db "SELECT strategy,decision,reject_reason,COUNT(*) FROM hft_log GROUP BY 1,2,3 ORDER BY 4 DESC"
```

---

## Key Files

```
F:\Trading\btc-bias-engine\
  main.py               — orchestrator; hft_decision_ref, hft_submit_lock, HFTEngine init
  hft_engine.py         — HFTEngine: YES+NO arb + directional scalping
  kalshi_client.py      — Kalshi API; KalshiOrderBook, get_orderbook(), place_order(action="sell")
  signal_intelligence.py — SignalFilter, FilterDecision, FilterReason, regime×session WR table
  position_manager.py   — _compute_dynamic_max_pct(), open_orders (used by HFT for coordination)
  config.py             — all tunable constants (HFT_*, DYNAMIC_SIZING_*, early exit params)
  signal_logger.py      — log_hft_eval(), update_hft_row(), hft_log table schema
  ledger.py             — human-readable trade + signal view
  ENGINE_OVERVIEW.md    — full system architecture reference
  inspect_orderbook.py  — diagnostic: live order book depth viewer

  data/engine.log       — live engine output
  data/trades.db        — kalshi_trades, hft_log, execution_log tables
  data/signals.db       — every consensus signal generated
```

---

## Known Gotchas

- **fill_count_fp**: Kalshi API v2 string field. Fixed in `kalshi_client.py:_parse_order`.
- **Kalshi orderbook**: Key is `orderbook_fp` (not `orderbook`). Prices are `yes_dollars`/`no_dollars` (dollar floats). Fixed in `get_orderbook()`.
- **HFT edge semantic**: `signal.confidence` is NOT a probability (it's Fourier momentum magnitude). Always use `decision.expected_wr` for edge calculations.
- **net_edge friction is still a proxy** — full-spread proxy may be too conservative; revisit after `hft_log` accumulates data.
- **submit_lock scope**: Set True before `place_order`, False in `finally`. If main engine crashes mid-submit, lock stays True until service restart. Acceptable for now.
- **HFT scalp exits use market order** — `place_order(action="sell", order_type="market")`. If Kalshi rejects, check log for "HFT scalp exit FAILED".
- **Between-window gap**: HFT resets state when ticker changes. Any pending order from prior window is orphaned (position cleared, order left open until Kalshi expires it).
- **NSSM restart can silently fail** — always verify with `Get-Service`.
```
