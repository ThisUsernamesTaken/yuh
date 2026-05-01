# File Interaction Audit — Files Modified After 2026-04-05

_Generated: 2026-04-15_

## Live Engine Entry Point

- `run_copy_engine.py` (run by NSSM service **BTCBiasEngine**)
  - Application: `C:\Trading\btc-bias-engine\venv\Scripts\python.exe`
  - AppParameters: `C:\Trading\btc-bias-engine\run_copy_engine.py`
  - Note: Despite the CLAUDE.md reference to `polymarket_copy_engine.py` as entry point, NSSM actually invokes `run_copy_engine.py`, which bootstraps credentials and then imports `PolymarketCopyEngine` from `polymarket_copy_engine.py`.

---

## ACTIVE — In the Live Trade Chain

Files directly imported and executed by the running engine (import chain: `run_copy_engine.py` → `polymarket_copy_engine.py` → ...).

| File | Last Modified | Description | Imported By |
|------|---------------|-------------|-------------|
| `run_copy_engine.py` | 2026-04-11 | NSSM service entry point; bootstraps credentials, spawns `WhaleMonitor`, creates `KalshiClient`, instantiates `PolymarketCopyEngine` | NSSM (top-level) |
| `polymarket_copy_engine.py` | 2026-04-14 | Core engine (~3900 lines): signal cascade (PRE_OPEN_ARB, PRIMARY, TREND_FOLLOW, MIMIC, ALGO, TA_FORCED, FLIP_INVERT), order execution, position management, TP/stop logic, wallet scorer, Polymarket + Kalshi REST polling | `run_copy_engine.py` |
| `kalshi_client.py` | 2026-04-09 | Kalshi REST API client: RSA-PSS auth, order placement/cancellation, orderbook, positions, contract lookup | `run_copy_engine.py`, `polymarket_copy_engine.py` (indirect via run_copy_engine) |
| `ta_module.py` | 2026-04-06 | TA_FORCED tier: 1m EMA/RSI/volume scorer, `TAScorer`, `TASignalResult`, `fetch_binance_1m_candles`; TA_FORCED is the active fallback tier | `polymarket_copy_engine.py` (top-level import) |
| `whale_monitor.py` | pre-2026-04-05 (not modified in window) | Mempool.space WebSocket whale BTC flow detection; provides `WhaleFlowState` | `run_copy_engine.py`, `polymarket_copy_engine.py` |
| `microstructure.py` | 2026-04-12 | Market pressure scoring (`MarketPressure`, `PressureScore`): orderbook imbalance, tape flow, momentum — used for entry validation in TA_FORCED | `polymarket_copy_engine.py` (top-level import) |
| `signal_logger.py` | 2026-04-11 | SQLite trade/signal logger (aiosqlite); logs all trades, outcomes, brier scores to `data/trades.db` | `run_copy_engine.py` |
| `user_config.py` | 2026-04-14 | Runtime config overrides loaded via `importlib` at startup: sizing, stops, entry bands, SNIPER_ENABLED=False, TA_FORCED settings, etc. | `polymarket_copy_engine.py` (dynamic import via importlib) |
| `kalshi_ws.py` | 2026-04-05 | Kalshi WebSocket client (`KalshiWebSocket`): real-time orderbook and tape feed; imported inside try/except at engine startup | `polymarket_copy_engine.py` (lazy import at line 696) |
| `indicators.py` | 2026-04-08 | EMA/RSI/SMA/KST/ADX primitives used by `ta_module.py` | `ta_module.py` |
| `models.py` | pre-2026-04-05 (not in window) | Shared dataclasses: `Candle`, `ConsensusSignal`, `TradeRecord` | `signal_logger.py`, `ta_module.py`, `price_feed.py`, `mtf_scorer.py` |

### Conditionally Active (imported inside `try/except ImportError` — engine continues without them)

| File | Last Modified | Description | Imported By | Condition |
|------|---------------|-------------|-------------|-----------|
| `price_feed.py` | 2026-04-12 | `PriceFeedTask`: 5s candle aggregator with Bollinger Bands, MACD, MA50/100 for BTC price tracking | `polymarket_copy_engine.py` (lines 50-56, try/except) | `_MTF_AVAILABLE` flag |
| `mtf_scorer.py` | pre-2026-04-05 (not in window) | `MTFConfluenceScorer`: multi-timeframe confluence scoring; uses `tf_analyzer.py` | `polymarket_copy_engine.py` (lines 50-56, try/except) | `_MTF_AVAILABLE` flag |

### Conditionally Active (gated by `SNIPER_ENABLED=False` in user_config.py — code path exists but does not execute)

| File | Last Modified | Description | Gated By |
|------|---------------|-------------|----------|
| `sniper.py` | 2026-04-06 | Session-open momentum scalp: single market order at T+0, enumerates `_Phase` states | `polymarket_copy_engine.py` line 3127: `if _uc("SNIPER_ENABLED", False)` — currently **disabled** |
| `sniper_signals.py` | 2026-04-05 | Signal layer for sniper: direction score from BTC momentum, Kalshi tape, orderbook imbalance | Not imported in live chain (sniper.py itself not called) |

---

## TOOLS — Standalone Scripts (Not in Live Chain)

Scripts run on-demand for analysis, backfill, inspection, or diagnostics. None are imported by the engine.

| File | Last Modified | Description |
|------|---------------|-------------|
| `analyze_48h_holistic.py` | 2026-04-14 | Holistic 48h performance assessment from `data/trades.db` and balance snapshots |
| `analyze_account.py` | 2026-04-12 | Deep analysis of Kalshi account data; fetches live account via API, correlates entry factors with outcomes |
| `analyze_alignment_threshold.py` | 2026-04-14 | Cross-tabulates TA_FORCED alignment count at entry vs win/loss outcome (live-account only) |
| `analyze_attribution.py` | 2026-04-13 | PnL attribution + component importance + entry timing from `data/trade_export.json` |
| `analyze_convexity.py` | 2026-04-13 | Analyzes convexity of outcomes vs entry price/conviction |
| `analyze_dca.py` | 2026-04-14 | DCA/scaling analysis on trade records |
| `analyze_performance.py` | 2026-04-12 | General performance analysis by strategy/session from `data/trades.db` |
| `analyze_persistence_effect.py` | 2026-04-14 | Studies whether winning/losing streaks persist across sessions |
| `analyze_pressure_attribution.py` | 2026-04-13 | Decomposes microstructure pressure score components vs trade outcomes |
| `analyze_recent.py` | 2026-04-13 | Quick summary of recent trades (last N sessions) from trade DB |
| `analyze_regime_48h.py` | 2026-04-14 | 48h regime-of-day performance breakdown |
| `analyze_regime_tod.py` | 2026-04-08 | Time-of-day regime analysis |
| `analyze_stop_timing.py` | 2026-04-14 | Analyzes stop-loss timing and counterfactual outcome if held longer |
| `analyze_tp_timing.py` | 2026-04-13 | Analyzes take-profit timing, TP hit rates by tier |
| `analyze_true_pnl.py` | 2026-04-13 | Cross-checks DB-reported PnL against `balance_snapshots` for accuracy |
| `backfill_mfe_mae.py` | 2026-04-09 | Backfills MFE/MAE from Kalshi 1m candlestick data into `data/trades.db` (trade_excursions table) |
| `diagnose.py` | 2026-04-08 | Execution quality report: signal→execution funnel, block reasons, actual vs hypothetical PnL |
| `fetch_history.py` | 2026-04-08 | Downloads 90 days of 1m BTC/USDT candles from Binance.US to `data/btc_1m_90d.csv` |
| `inspect_orderbook.py` | 2026-04-08 | Prints live Kalshi KXBTC15M orderbook depth snapshot (one-shot or watch mode) |
| `ledger.py` | 2026-04-08 | Prints a readable trade ledger from `data/trades.db` to stdout |
| `perf_tracker.py` | 2026-04-14 | Rolling balance-delta tracker; tags config versions and compares performance across config changes |
| `validate_vs_pinescript.py` | 2026-04-08 | Compares Python engine output to Pine Script CSV export for bias engine validation |
| `scripts/fix_sniper.py` | 2026-04-05 | One-shot patch script that replaced `_sniper_check` in `polymarket_copy_engine.py` with a pre-fire hammer approach (already applied) |
| `scripts/hourly_pnl.py` | 2026-04-05 | Parses `data/engine_history.log` to compute P&L by hour of day |
| `scripts/sniper_new.py` | 2026-04-05 | Draft/prototype of updated sniper logic (inline function definition, standalone) |
| `C:/Trading/temp_ghost_research.py` | 2026-04-13 | Temporary research script to pull last 500 Kalshi orders as JSON for ghost order investigation (outside project dir) |

---

## TESTS — Only Run by pytest

| File | Last Modified | Description |
|------|---------------|-------------|
| `test_microstructure.py` | 2026-04-12 | Stress tests for `microstructure.py`: tests empty state, various pressure scenarios |

_(Note: `tests/` subdirectory files were last modified 2026-03-12 to 2026-04-01 — none modified after 2026-04-05.)_

---

## UI — Separate Process

GUI/terminal dashboards launched independently of the NSSM service.

| File | Last Modified | Description |
|------|---------------|-------------|
| `app.pyw` | 2026-04-11 | Tkinter GUI dashboard: double-click launcher, credential setup UI, live P&L display, spawns `run_copy_engine.py` as subprocess |
| `app_local.pyw` | 2026-04-06 | Earlier version of Tkinter GUI launcher (local/dev variant) |
| `launcher.pyw` | 2026-04-13 | Unified launcher: `--engine` / `--monitor` / `--setup` modes; can open terminal monitor or run engine headless |
| `setup_panel.pyw` | 2026-04-06 | Tkinter control panel: service management, credential config, settings knobs |
| `deploy/setup.pyw` | 2026-04-06 | Control panel copy bundled for deploy distribution |
| `dashboard.py` | 2026-04-11 | Tkinter real-time dashboard widget (runs in separate thread alongside engine — separate launch) |
| `monitor.py` | 2026-04-13 | Full-screen terminal (ANSI) monitor dashboard: reads `data/dashboard_state.json` and `data/trades.db`; separate process |

---

## DORMANT — Modified But Not Used By Live Engine

| File | Last Modified | Description | Why Dormant |
|------|---------------|-------------|-------------|
| `aggregator.py` | 2026-04-08 | `CandleAggregator`: resamples 1m candles into higher TFs | Imported only by `main.py` (disabled consensus engine); not used by copy engine |
| `polymarket_stream.py` | 2026-04-08 | Polymarket WebSocket stream for 5m markets (`PolymarketStream`) | Only imported by disabled `main.py` and `polymarket_features.py` (which is also dormant) |
| `polymarket_features.py` | 2026-04-08 | Polymarket 5m feature computer (`PolyFeatureComputer`) | Only used by disabled `hft_engine.py` and `signal_fusion.py` |
| `ws_client.py` | 2026-04-08 | Binance WebSocket client with auto-reconnect | Only used by disabled `main.py` |
| `config.py` | 2026-04-08 | Constants for consensus/bias engine (EMA lengths, scoring weights, etc.) | Used by `aggregator.py`, `ws_client.py`, `signal_logger.py` (via try/except fallback only) — none active |
| `paper_engine/__init__.py` | 2026-04-14 | Package init for paper trading engine | Run on-demand via `runner.py` only |
| `paper_engine/experiments.py` | 2026-04-14 | Paper trade experiments: backtest strategy variants on log replay data | Standalone; not imported by engine |
| `paper_engine/fill_simulator.py` | 2026-04-14 | Fill model with realistic slippage simulation | Used by `paper_engine/runner.py` only |
| `paper_engine/log_replay.py` | 2026-04-14 | Parses `data/engine_history.log` into session objects for paper replay | Used by `paper_engine/runner.py` and `experiments.py` only |
| `paper_engine/runner.py` | 2026-04-14 | Runs all strategies through log replay, computes counterfactual analysis | Standalone |
| `paper_engine/stop_comparison.py` | 2026-04-14 | Compares stop-loss variants in paper simulation | Standalone |
| `paper_engine/strategies.py` | 2026-04-14 | Strategy variants for paper testing (BaselineStrategy, HoldToExpiryStrategy, etc.) | Used by `paper_engine/runner.py` only |
| `paper_engine/validate_against_live.py` | 2026-04-14 | Compares paper engine predictions vs actual live balance delta | Standalone |

---

## LEGACY — Disabled or Pre-Replaced

Files that are code-complete but not invoked by any active path. Per CLAUDE.md: "Main consensus engine: DISABLED", "HFT engine: DISABLED".

| File | Last Modified | Description | Why Legacy |
|------|---------------|-------------|------------|
| `main.py` | 2026-04-08 | Consensus engine orchestrator: imports `BiasEngine`, `CandleAggregator`, `PolymarketStream`, etc. | Disabled — 0% WR day 1, anti-correlated signals; not invoked by NSSM service |
| `hft_engine.py` | 2026-04-08 | Order book scalping engine (~1800 lines); imports `PolyFeatureComputer` | Disabled — phantom edge, -$7.62 lifetime |
| `bias_engine.py` | 2026-04-08 | Multi-TF momentum scoring engine | Part of disabled consensus chain |
| `consensus.py` | 2026-04-08 | Fourier-weighted TF fusion layer | Part of disabled consensus chain |
| `signal_intelligence.py` | 2026-04-08 | Regime filter, WR tracker | Not used by copy engine |
| `signal_fusion.py` | 2026-04-08 | Polymarket→Kalshi fair value bridge | HFT-only; HFT disabled |
| `regime_detector.py` | 2026-04-08 | Market regime classifier (trending/mean-reverting/explosive) | Not used by copy engine |
| `strategy_index.py` | 2026-04-08 | Session strategy lookup table | Not used by copy engine |
| `position_manager.py` | 2026-04-08 | Legacy position sizing / management | Copy engine has own internal position management |
| `config_phase3.py` | 2026-04-08 | Phase 3 regime thresholds | Not used by copy engine |
| `backtest.py` | 2026-04-08 | Offline backtester against Pine Script CSV | Superseded by newer analysis scripts |
| `backtest_strategies.py` | 2026-04-08 | Multi-strategy backtest on 90-day 1m BTC candles | Replays old consensus engine strategies (now disabled) |

---

## Summary

| Category | Count | Notes |
|----------|-------|-------|
| ACTIVE (confirmed live chain) | 9 core + 2 conditional (MTF) + 2 gated (sniper, disabled) | Core live set: run_copy_engine, polymarket_copy_engine, kalshi_client, ta_module, whale_monitor, microstructure, signal_logger, user_config, kalshi_ws |
| TOOLS | 24 | All analyze_*, backfill, diagnose, ledger, perf_tracker, scripts/ |
| TESTS | 1 (in window) | test_microstructure.py |
| UI | 7 | app.pyw, app_local.pyw, launcher.pyw, setup_panel.pyw, deploy/setup.pyw, dashboard.py, monitor.py |
| DORMANT | 13 | Includes entire paper_engine/ package (all 2026-04-14) |
| LEGACY | 12 | Main consensus + HFT engine and supporting modules |

**Key finding**: The entire `paper_engine/` package (7 files, all modified 2026-04-14) is the most recently active non-engine work — it is a standalone paper-trading/backtesting harness that does NOT connect to the live engine. `perf_tracker.py` (2026-04-14) is also a standalone tool.
