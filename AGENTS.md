# BTC Bias Engine — System Reference

**Last updated**: 2026-04-21
**Entry point**: `run_copy_engine.py` (NSSM service `BTCBiasEngine` on Windows)
**Primary signal**: Microstructure pressure score + FVG filter (wallet copying DISABLED)

This document is the single source of truth. If it disagrees with code, the code wins — but update this doc before the next session.

---

## What the engine does

Trades Kalshi **KXBTC15M** 15-minute BTC binary options. Every 15 minutes a new contract opens at a strike near the current BTC price. The engine looks for mispricings (FVG = fair value gap vs a Brownian Bridge probability model) AND microstructure pressure (BTC impulse, order book imbalance, taker flow, Kalshi price lag) agreeing on the same side, then enters at the bid with resting limit orders. Holds to expiry. TP limits rest at the ask. No mid-session stops.

The edge is **execution discipline + microstructure exploitation**, not a magic probability model. The prob engine is a baseline anchor; the pressure score is the trigger.

---

## Architecture

```
Coinbase BTC WS ─┐
Binance 1m/5m/15m/1h candles (REST warmup + WS) ─┐
                                                  ├─→ price_feed.py ─→ prob engine + MTF scorer + tick tracker
Kalshi WS (orderbook_delta, trade) ──────────────┘
Kalshi REST tape (3s poll) ──────────────────────┘

microstructure.py (MarketPressure)
  ├─ BTC impulse (velocity, 5s/30s/300s deltas)
  ├─ Book pressure (imbalance, microprice, depth)
  ├─ Flow momentum (taker buy_pressure, large-trade bias)
  ├─ Kalshi lag (expected vs actual repricing)
  └─→ PressureScore {score, direction, confidence, persistent, btc_move_300s, ...}

polymarket_copy_engine.py (~12K lines, orchestrator)
  ├─ _flow_iteration (0.3s cycle)
  ├─ _evaluate_ta_forced_signal (FVG trigger + pressure gate)
  ├─ _evaluate_dominant_gate (4-factor AND — see below)
  ├─ _compute_position_size (Kelly aggressive — see below)
  ├─ _place_laddered_entry (maker-only limit orders)
  └─ _manage_position (TP placement, window watchdog, hold-to-expiry)
```

---

## Signal cascade (current reality)

Only two tiers actually fire:

1. **TA_FORCED** (primary): FVG ≥ 8c AND DOMINANT-DIRECTION gate passes
2. **FLIP_INVERT** (dormant): Requires scored wallets; currently inert because wallet copy is off

Everything that used to be ahead of TA_FORCED (PRE_OPEN_ARB, PRIMARY wallet-driven, TREND_FOLLOW, MIMIC, ALGO) is either gated off or requires the wallet pool that we no longer populate.

### DOMINANT-DIRECTION gate (all four AND)

Located in `_evaluate_ta_forced_signal` and `_evaluate_dominant_gate_standalone`. Replaces the old OR-based gate that drained $50 on contrarian RSI entries.

| # | Factor | Threshold |
|---|---|---|
| 1 | BTC 5-min trend matches FVG side | YES: `btc_5m_move ≥ +$30`, NO: `≤ -$30` |
| 2 | Pressure score sign + confidence | YES: `score ≥ +0.03`, NO: `≤ -0.03`, AND `confidence ≥ 0.55` |
| 3 | RSI not contrarian | YES: `RSI ≤ 70`, NO: `RSI ≥ 30` |
| 4 | FVG non-marginal | `|FVG| ≥ 10c` |

Fail any factor → `DOMINANT-SKIP` log line, no entry.

---

## Execution: maker-only ladder

Set by `MAKER_ONLY = True` in `user_config.py`. Kalshi charges ~7% taker, 0% maker. Paying the ask on entries was burning 3-5c/trade in hidden slippage.

Current entry: **single maker limit at bid** with `post_only=True`. Kalshi rejects if it would cross. Rest 20s. If unfilled, cancel and re-evaluate next cycle. Ghost-fill verification still runs (status re-check after cancel).

TP rests at entry-price-based offset (see `TAKE_PROFIT_CENTS = 8` in config). No stop loss — `STOP_LOSS_CENTS` is still in config but the mid-session stop code paths are all disabled per the `_manage_position` hold-to-expiry rule.

---

## Sizing (aggressive Kelly, 2026-04-21)

User directive: "sizing on good trades is how we made $1k — throw precautions to the wind on entry sizing."

```
KELLY_ENABLED = True
KELLY_FRACTION = 0.65               # between half and three-quarter Kelly
KELLY_MIN_FRAC = 0.03               # keep marginal trades small
KELLY_MAX_FRAC = 0.80               # monster edges push 80% of balance
KELLY_MIN_EDGE_PP = 2.0             # skip sub-noise entries

SIZING_MAX_FRACTION = 0.75          # absolute ceiling per position
SIZING_HARD_CAP_CONTRACTS_DAY = 150 # was 25 — Kelly now binds, not the cap
SIZING_HARD_CAP_CONTRACTS_NIGHT = 150
MTF_SIZE_MULTIPLIER_HIGH = 2.0      # high confluence doubles base size

DAILY_LOSS_LIMIT = 100.00           # halt at -$100/day
MIN_BALANCE_TO_TRADE = 2.00
```

Formula: `size_contracts = balance * (f* * KELLY_FRACTION) / market_price` where `f* = (b*p - q) / b`, clamped to `[KELLY_MIN_FRAC, KELLY_MAX_FRAC]`.

Sample sizing by balance:
- **$30 balance**: marginal trade ~$1 / strong edge ~$7-10 / max monster ~$24
- **$100 balance**: marginal ~$3 / strong ~$30 / max ~$80
- **$300 balance**: marginal ~$9 / strong ~$90 / max ~$180 (contracts-capped)
- **$1000 balance**: marginal ~$30 / strong ~$300 / max ~$600 (contracts-capped ~$150 mid-price × 150ct = $225, so cap may bite sooner)

Contract cap of 150 means at mid-price ~50c, max position size is capped around 150 × $0.50 = $75 before the cap itself starts to bind. Raise cap if balance grows past ~$300 and monster edges are getting clipped.

---

## Wallet copying — OFF

```
WALLET_COPY_ENABLED = False
WALLET_SCORING_ENABLED = False
```

Rationale: Polymarket wallet signals are structurally lagging. Microstructure on the Kalshi book + Coinbase tick feed fires faster than Poly reprices. Wallet copying became a throttle / lagging indicator, not an edge. Disabled 2026-04-21 after analysis showed microstructure-only performance matched or exceeded wallet-gated performance.

Re-enable by flipping both flags in `user_config.py`. Code paths are preserved (`_poll_smart_trades`, `_recompute_smart_flow`, `_scorer_loop`, FLIP_INVERT).

---

## Hold-to-expiry exit

ALL mid-session stops are disabled. Data showed 10/10 stopped trades were correct on direction — stops cost $48.75 while "saving" $0.60.

Only exits:
- TP limit fills at the ask (resting on Kalshi book)
- Contract expires; Kalshi settles at 0c or 100c
- Hard safety: <1 min mandatory close

Disabled exits (present in code but gated off):
- `catastrophic_stop`, `scalp_stop`, `peak_giveback`, `deviation_stop`, `time_exit`, `wallet_flip_exit`, `trailing_stop`

---

## File map (top level — 20 .py files)

### Live / load-bearing (imported by `run_copy_engine.py` directly or transitively)

| File | Role |
|---|---|
| `run_copy_engine.py` | Entry point. Loads creds, starts PolymarketCopyEngine. |
| `polymarket_copy_engine.py` (~12K lines) | Orchestrator: signals, execution, position mgmt. |
| `kalshi_client.py` | Kalshi REST: RSA-PSS auth, orders, orderbook, positions, settlements. |
| `kalshi_ws.py` | Kalshi WebSocket: `LocalOrderBook`, `TradeFlowTracker`. |
| `price_feed.py` | Coinbase WS + Binance candles + prob engine + tick tracker. |
| `microstructure.py` | `MarketPressure`, `PressureScore` — the primary entry signal. |
| `edge_sizer.py` | Kelly sizing + scalp-mode detection. |
| `ta_module.py` | 1m TA scorer (EMA/RSI/volume) — feeds DOMINANT-DIRECTION gate. |
| `mtf_scorer.py` | Multi-timeframe confluence scoring → `MTF_SIZE_MULTIPLIER_HIGH` boost. |
| `tf_analyzer.py` | Per-TF signal extraction (used by MTF scorer). |
| `indicators.py` | EMA/RSI/SMA/KST/ADX primitives. |
| `whale_monitor.py` | Mempool.space whale-flow WS (informational; no longer gates entries). |
| `signal_logger.py` | SQLite logging: `signals.db`, `trades.db`. |
| `models.py` | Shared dataclasses (`Candle`, `ConsensusSignal`, `TradeRecord`). |
| `user_config.py` | Runtime config — all the dials. |
| `user_config.example.py` | Template for first-run setup. |
| `monitor.py` | Terminal monitor (reads `data/dashboard_state.json`). |

### Lazy-imported (behind feature flag — keep at top level so flag-flip doesn't crash)

| File | Flag |
|---|---|
| `paper_trader.py` | `PAPER_TRADING=True` |
| `sniper.py` + `sniper_signals.py` | `SNIPER_ENABLED=True` (currently OFF — drained account $172→$8 when accidentally left armed) |

### GUI / operator tools (top level, Windows .pyw)

| File | Role |
|---|---|
| `launcher.pyw` | Unified menu: engine / monitor / setup. |
| `app.pyw`, `app_local.pyw` | Rich desktop UI. |
| `setup_panel.pyw` | First-run credential setup wizard. |

### Subfolders

| Path | Contents |
|---|---|
| `scripts/` | Ops scripts: `reconcile_pnl.py`, `hourly_pnl.py`, `post_session_diagnostic.py`, `reinstall_service.ps1`, `setup.ps1`, `mtf_sim.py`, `check_resources.ps1`, etc. |
| `tests/` | `pytest` suite for indicators, price_feed, mtf_scorer, tf_analyzer, microstructure. |
| `docs/` | Current docs (`MASTER_GUIDE.md`, `ENGINE_WHITEPAPER.md`, `SNIPER_STRATEGY.md`, `PAPER_ENGINE_FINDINGS.md`, `SCALP_MODE_REVERT.md`, `price-action-timeframes/`). |
| `docs/archive/` | Dated one-off research docs — don't rely on these for current behavior. |
| `paper_engine/` | Standalone paper-trading runner (separate from live engine's optional PAPER_TRADING mode). |
| `deploy/` | `bundle.ps1`, `swap-credentials.ps1`, deploy zip generator. |
| `data/` | Live runtime: `trades.db`, `signals.db`, `dashboard_state.json`, `engine_history.log`, `wallet_scores.json` (stale, unused). |
| `credentials/` | `kalshi.env` + PEM key (gitignored). |
| `_archive/legacy_engine/` | Old consensus engine + HFT + disabled plumbing. |
| `_archive/analysis/` | One-off analysis/backtest/diagnostic scripts. |
| `_archive/` (root) | Pre-cleanup archive — older copies of the same files in `legacy_engine/`. Left for history. |

---

## Database (`data/trades.db`)

`kalshi_trades` table columns of interest:
- `order_id, placed_at, ticker, side, count, filled_count, limit_price`
- `pnl, status, result` — pnl can be stale; use `settlement_ledger` for truth (see `scripts/reconcile_pnl.py`)
- `strategy_name` — `TA_FORCED`, `FLIP_INVERT`, etc.
- Pressure attribution columns written at entry: `_pressure_score`, `_pressure_components`, `_entry_spread`, `_fvg_magnitude` (stored in `ladder_detail` JSON)

`settlement_ledger` table: authoritative Kalshi settlements (market_result, yes/no counts, fee_cents, pnl_cents per ticker). `scripts/reconcile_pnl.py` can rewrite stale `kalshi_trades.pnl` from this truth.

Useful queries:
```sql
-- Daily P&L
SELECT DATE(placed_at), ROUND(SUM(pnl),2) FROM kalshi_trades
WHERE status NOT IN ('pending','unfilled') GROUP BY DATE(placed_at);

-- Balance over time
SELECT snapshot_ts, balance_usd, event FROM balance_snapshots
ORDER BY snapshot_ts DESC LIMIT 20;

-- WR by entry price band
SELECT CASE WHEN limit_price<50 THEN '<50c'
            WHEN limit_price<65 THEN '50-64c'
            WHEN limit_price<80 THEN '65-79c'
            ELSE '80c+' END AS band,
       COUNT(*), ROUND(AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0 END)*100,1) AS wr,
       ROUND(SUM(pnl),2) AS pnl
FROM kalshi_trades WHERE status LIKE 'reconciled%'
GROUP BY 1;
```

⚠️ **Never trust TP-fill counts as "wins"**. Balance snapshot is truth. The account drained $172→$8 while the engine reported 20 consecutive "wins" from TP fills masking ghost-position losses.

---

## Service management (Windows / NSSM)

```powershell
nssm status BTCBiasEngine              # Check
nssm restart BTCBiasEngine             # Restart (admin)
Get-Content data\engine_history.log -Tail 50 -Wait   # Live logs
```

NSSM env vars required:
- `KALSHI_API_KEY` (UUID)
- `KALSHI_PRIVATE_KEY_PATH` (path to PEM)
- `EXECUTE_TRADES=true`
- `KALSHI_DEMO=false`
- `PYTHONUNBUFFERED=1`

---

## Transferring between machines

1. Unzip / clone into `C:\Trading\btc-bias-engine` (hardcoded in `user_config.py::ENGINE_DIR`; change if relocating).
2. `python -m venv venv && .\venv\Scripts\activate && pip install -r requirements.txt`.
3. Copy `credentials/kalshi.env` + PEM into `credentials/` (both gitignored — never commit).
4. `deploy\swap-credentials.ps1` if rotating to a different Kalshi account.
5. `scripts\setup.ps1` to install NSSM service pointing at `run_copy_engine.py`.
6. On first start the engine will warm candle buffers and skip the in-progress window; trading begins at the next window boundary.

**Portability caveat**: `ENGINE_DIR = r"C:\Trading\btc-bias-engine"` is hardcoded in `user_config.py`. Change it there, and in any NSSM service definition, if the deploy path differs.

---

## Do / Don't for future sessions

**Do:**
- Check `data/balance_snapshots` (not TP-fill counts) to evaluate performance.
- Verify `WALLET_COPY_ENABLED` and `SNIPER_ENABLED` flags before assuming a trade's source.
- Use `scripts/reconcile_pnl.py` when `kalshi_trades.pnl` disagrees with balance moves.
- Update this AGENTS.md when you change signal logic or config defaults.

**Don't:**
- Turn SNIPER back on without reading why it was disabled (it can fire "while off" if flag handling is wrong — drained an account).
- Re-add mid-session stops without data; 10/10 historical stops hit were directionally correct.
- Treat anything in `_archive/` or `docs/archive/` as current behavior. Those are historical.
- Trust `kalshi_trades.pnl` as authoritative without cross-referencing `settlement_ledger` or balance snapshots.
