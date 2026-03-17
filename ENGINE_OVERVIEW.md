# BTC Bias Engine — System Overview
**Last updated:** 2026-03-15
**Status:** Live — Windows Service `BTCBiasEngine` (NSSM, auto-start)
**Contract:** Kalshi `KXBTC15M` — Binary YES/NO: "Does BTC 15-minute close ≥ open?"

---

## Table of Contents

1. [What the Engine Does](#1-what-the-engine-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Layer-by-Layer Breakdown](#3-layer-by-layer-breakdown)
4. [Signal Pipeline Detail](#4-signal-pipeline-detail)
5. [Execution & Risk Management](#5-execution--risk-management)
6. [HFT Engine](#6-hft-engine)
7. [Infrastructure](#7-infrastructure)
8. [Configuration Reference](#8-configuration-reference)
9. [Performance](#9-performance)
10. [File Inventory](#10-file-inventory)
11. [Known Gotchas](#11-known-gotchas)

---

## 1. What the Engine Does

The engine trades Kalshi's `KXBTC15M` binary prediction markets using a
multi-timeframe momentum consensus signal. Each contract resolves YES ($1.00)
if BTC's 15-minute candle closes at or above its open price, or NO ($1.00) if
it closes below. Contracts run continuously — one 15-minute window at a time.

The engine:
- Streams live BTC/USDT 1-minute klines from Binance.US via WebSocket
- Aggregates to 5 timeframes and scores momentum on each
- Combines scores into a single directional signal with Fourier-weighted consensus
- Classifies the current BTC market regime (trending/ranging/volatile)
- Gates every trade through a regime × session filter backed by a 90-day backtest
- Sizes positions dynamically, scaling up as live win rate is confirmed
- Places and manages orders on Kalshi (limit entries, market exits)
- Runs a parallel HFT loop every 4 seconds for order-book arbitrage and scalping

---

## 2. Architecture Overview

```
Binance.US WebSocket (1m BTC/USDT klines)
    │
    ▼
CandleAggregator  ──────────────────────────────────────────────────────┐
    │  (rolls 1m into 3m / 5m / 15m / 1h)                              │
    │                                                                   │
    ▼  (one BiasEngine per TF)                                          │
BiasEngine × 5  [1m, 3m, 5m, 15m, 1h]                                 │
    │  EMA crossover, RSI, candle pressure, relative volume, Fourier    │
    │                                                                   │
    ▼                                                                   │
ConsensusLayer  (Fourier-weighted aggregation → ConsensusSignal)        │
    │                                                                   │
    ├──► HFTEngine  ◄── KalshiClient.get_orderbook() [every 4s] ◄──────┘
    │       │
    │       ├── YES+NO arbitrage check
    │       └── Directional scalp (combined confidence + book)
    │
    ▼
RegimeDetector  (5m candles → DER + ATR ratio + EMA fan → regime label)
    │
    ▼
SignalFilter  (regime gate + alignment gate + bad-hours gate)
    │
    ▼
StrategyIndex  (90-day backtest regime×session cell → StrategyConfig)
    │
    ▼
PositionManager  (sizing → limit price → kill switch → dynamic scale)
    │
    ▼
KalshiClient  (RSA-PSS signed HTTP → place_order / cancel_order / get_orderbook)
    │
    ├── Early exit monitor  (runs every 1m candle, checks open positions)
    └── Outcome poller      (asyncio task, runs every 60s, settles expired contracts)

All outcomes → SignalLogger → SQLite (trades.db, signals.db)
```

---

## 3. Layer-by-Layer Breakdown

### 3.1 Data Ingestion — `ws_client.py` + `aggregator.py`

| Component | Detail |
|---|---|
| Source | Binance.US WebSocket `wss://stream.binance.us:9443/ws/btcusdt@kline_1m` |
| Reason for .US | `binance.com` is geo-blocked for US users |
| Delivery | Closed 1m candles pushed to `Engine._on_candle()` callback |
| Warm start | Historical REST candles fetched on startup to pre-fill all TF indicators |
| Aggregation | `CandleAggregator` rolls 1m into 3m/5m/15m/1h by accumulating OHLCV |

### 3.2 Bias Engine — `bias_engine.py`

One `BiasEngine` instance per timeframe. On each closed candle it computes:

| Indicator | Weight | Role |
|---|---|---|
| EMA crossover (5/13) | 200 | Primary trend direction |
| Cycle return | 120 | Raw candle momentum |
| RSI (7-period) | 25 | Overbought/oversold bias |
| Candle pressure | 15 | Body-to-wick directional conviction |
| Relative volume | 10 | Volume confirms move |

Output: `BiasResult` with `bull_conf` and `bear_conf` (0–100 each).

### 3.3 Consensus Layer — `consensus.py`

Aggregates all 5 `BiasResult` objects into one `ConsensusSignal`:

```
fourier_score = Σ (bull_conf - bear_conf) × TF_weight  /  Σ TF_weight
confidence    = |fourier_score|   (0–100)
direction     = CALL if fourier_score > 0, PUT if < 0
aligned_count = number of TFs matching the consensus direction
```

**Timeframe weights (market hours):**

| TF | Weight | Rationale |
|---|---|---|
| 1m | 0.10 | Noise filter — minimal weight |
| 3m | 0.15 | Short-term momentum |
| 5m | 0.20 | Entry trigger timeframe (best backtest WR) |
| 15m | 0.30 | Contract window — structural anchor |
| 1h | 0.25 | Macro trend context |

**After-hours weights (UTC 21–08):** 1h reduced to 0.10 (stale overnight bias),
redistributed to 5m (+0.08) and 15m (+0.07).

### 3.4 Regime Detector — `regime_detector.py`

Fed 5m candles. Classifies regime on every bar using three independent metrics:

| Metric | Calculation | Threshold |
|---|---|---|
| **DER** (Directional Efficiency Ratio) | net displacement / total path over 20 bars | ≥0.35 = trending, ≤0.15 = ranging |
| **ATR ratio** | 14-period ATR / 50-period ATR | ≥1.5 = volatile |
| **EMA fan** | alignment & spread of EMA(8/21/55) | ≥0.3 fan score supports trending |

**Regime classifications:**

| Regime | 90-day WR range | Tradeable? |
|---|---|---|
| `TRENDING_UP` | 60.1–66.4% | Yes |
| `TRENDING_DOWN` | 58.0–67.4% | Yes |
| `RANGING` | 63.5–71.3% | Yes — our best regime |
| `VOLATILE` | 55.0–63.5% | London blocked; others conditional |
| `UNKNOWN` | — | No |

### 3.5 Signal Intelligence — `signal_intelligence.py`

`SignalFilter.evaluate()` runs 4 sequential gates:

1. **Regime gate** — rejects VOLATILE×London and UNKNOWN
2. **Alignment gate** — requires ≥3/5 TFs aligned with consensus direction
3. **Bad-hours gate** — blocks UTC hours {9, 12, 15, 21}
4. **Raw confidence floor** — rejects signals below `MIN_RAW_SIGNAL_CONFIDENCE = 15.0`

If all gates pass, returns `FilterDecision(approved=True)` with:
- `adjusted_confidence` — signal confidence × regime multiplier
- `expected_wr` — empirical WR from the regime×session lookup table
- `position_scale` — sizing multiplier from `StrategyIndex`

### 3.6 Strategy Index — `strategy_index.py`

Maps each (Regime × Session) cell to a `StrategyConfig`. Sessions:

| Session | UTC hours |
|---|---|
| Asia | 00–08 |
| London | 08–13 |
| NY-Open | 13–17 |
| NY-Prime | 17–21 |
| After-Hrs | 21–24 |

The 20-cell matrix provides `expected_wr` (backtest WR for that cell) and
`position_scale` (1.0 base, can be raised for high-confidence cells). Cells
with fewer than 30 historical samples are blocked (`BLOCK_LOW_SAMPLE_CELLS`).

---

## 4. Signal Pipeline Detail

```
1m candle arrives
    → update all 5 BiasEngines
    → build ConsensusSignal
    → update _hft_signal_ref (for HFT engine)
    → RegimeDetector.update(5m candle)
    → SignalFilter.evaluate()
        → if NOT approved: log and return
    → StrategyIndex.select(regime, utc_hour)
    → PositionManager.size_trade()
        → edge check: expected_wr/100 - ask_price ≥ 0.05
        → spread filter: bid-ask ≤ 20¢
        → volume filter: contract volume ≥ 25
        → sizing: equity × dynamic_max_pct × conf_frac × position_scale
    → KalshiClient.place_order(type="market")
    → SignalLogger.log_kalshi_trade()
```

**Early exit system** (checked on every 1m candle for open positions):

| Trigger | Condition | Action |
|---|---|---|
| Reversal exit | ≥4/5 TFs against position AND savings ≥5¢ | Sell at market bid; re-enter opposite if ≥5 min left |
| Take-profit | Contract moved ≥12¢ in our favor | Sell at market bid; no re-entry |
| Expiry force | <3 min remaining | Hold to binary resolution |

---

## 5. Execution & Risk Management

### 5.1 Position Sizing — `position_manager.py`

```
effective_max_pct = _compute_dynamic_max_pct()   # see dynamic tiers below
base_risk         = equity × (effective_max_pct / 100) × conf_frac
scaled_risk       = base_risk × position_scale
target_dollar     = min(scaled_risk, STAKE_CAP)
count             = max(1, int(target_dollar / ask_price_dollars))
```

**Dynamic sizing tiers** (rolling last 20 settled trades):

| Min settled | Min WR | max_pct_equity | Effect at $36 equity |
|---|---|---|---|
| — | — | 2.0% (floor) | count = 1 always |
| 20 | 55% | 3.5% | count = 1–2 |
| 30 | 58% | 5.0% | count = 2 |
| 30 | 65% | 8.0% | count = 2–3 |

### 5.2 Kill Switch

- `DAILY_LOSS_LIMIT = $500` — hard stop, no reset until UTC midnight
- Activated when `daily_realized_pnl ≤ -$500`
- Blocks all new entries from `PositionManager.is_killed`

### 5.3 Order Execution

- **Entry:** `place_order(type="market")` — fills at current ask price
- **Early exit:** `place_order(action="sell", type="market")` — fills at current bid
- **Auth:** RSA-PSS signature on every request (`KALSHI-ACCESS-SIGNATURE` header)

---

## 6. HFT Engine

File: `hft_engine.py` — parallel `asyncio.create_task`, polls every 4 seconds.

### 6.1 Strategy 1 — YES+NO Arbitrage

```
if yes_ask + no_ask < (100 - HFT_ARB_MIN_EDGE_CENTS):
    buy YES at yes_ask  }  via asyncio.gather() — simultaneous
    buy NO  at no_ask   }
    guaranteed_pnl = (100 - yes_ask - no_ask) × count¢
```

Both sides of the same contract settle to exactly $1.00 combined. If the
total ask falls below 98¢, the gap is risk-free profit. Occurs on thin books
when a stale resting order hasn't been cancelled after a market move.

### 6.2 Strategy 2 — Directional Scalping

```
edge = technical_confidence - market_ask_cents
if edge ≥ HFT_SCALP_MIN_EDGE_CENTS (8¢):
    limit = bid + spread/2   (inside spread, favor fill)
    post limit buy order
    on fill → open position, track target and stop
    exit when:
        bid ≥ entry + 8¢     (take-profit)
        bid ≤ entry - 6¢     (stop-loss)
        signal reverses      (signal_reversal exit)
        < 2.5 min to expiry  (forced exit)
    max 6 round-trips per 15m window
```

**Price stepping:** if limit order unfilled after 15s, cancel and re-post at
`limit + 1¢`; timeout and cancel after 30s total.

### 6.3 Order Book

`KalshiClient.get_orderbook(ticker)` → `KalshiOrderBook`:

- `yes_bids` / `no_bids` — full depth stacks `[(price_cents, qty), ...]`
- `best_yes_bid`, `best_yes_ask` — derived from YES/NO bid stacks
- `spread_cents`, `mid_cents`
- `liquidity_within(side, n_cents)` — contracts within N¢ of best price
- `fill_cost(side, count)` — simulated market impact (avg price + slippage)

Diagnostic: `python inspect_orderbook.py --watch`

---

## 7. Infrastructure

### 7.1 Runtime

| Item | Value |
|---|---|
| Language | Python 3.14 |
| Async framework | `asyncio` + `aiohttp` |
| Service manager | NSSM (Non-Sucking Service Manager) |
| Service name | `BTCBiasEngine` |
| Auto-restart | On crash, on boot |
| Python path | `F:\Trading\btc-bias-engine\venv\Scripts\python.exe` |
| Entry point | `F:\Trading\btc-bias-engine\main.py` |

### 7.2 Databases — SQLite

**`data/trades.db`**

| Table | Purpose |
|---|---|
| `kalshi_trades` | Every placed order: side, price, fill, outcome, P&L |
| `execution_log` | Full signal context at trade time (regime, session, spreads, fill timing) |
| `trades` | Legacy backtest trade records |

**`data/signals.db`**

| Table | Purpose |
|---|---|
| `signals` | Every consensus signal: direction, confidence, alignment, TF scores |

### 7.3 Kalshi API

| Item | Value |
|---|---|
| Endpoint | `https://api.elections.kalshi.com/trade-api/v2` |
| Auth | RSA-PSS per-request signature |
| Key ID env var | `KALSHI_API_KEY` |
| Key PEM env var | `KALSHI_PRIVATE_KEY_PATH` (path to `.pem`) |
| Response quirk | `fill_count_fp` (string float) instead of `filled_count` |
| Order book key | `orderbook_fp` with `yes_dollars` / `no_dollars` (dollar floats, not cents) |

### 7.4 Service Commands

```powershell
# Status
Get-Service BTCBiasEngine

# Restart (run as admin)
Stop-Service BTCBiasEngine -Force; Start-Service BTCBiasEngine

# Live log
Get-Content 'F:\Trading\btc-bias-engine\data\engine.log' -Tail 40 -Wait

# Trade ledger
cd F:\Trading\btc-bias-engine && python ledger.py

# Live order book (when market is open)
python inspect_orderbook.py --watch
```

### 7.5 Environment Variables (NSSM AppEnvironmentExtra)

```
KALSHI_API_KEY=<uuid>
KALSHI_PRIVATE_KEY_PATH=C:\Users\coleb\.kalshi\kalshi_key.pem
EXECUTE_TRADES=true
KALSHI_DEMO=false
PYTHONUNBUFFERED=1
```

---

## 8. Configuration Reference

### config.py (primary)

| Constant | Value | Description |
|---|---|---|
| `MAX_PCT_EQUITY` | 2.0% | Base position size % (overridden by dynamic tiers) |
| `STAKE_CAP` | $100.00 | Hard per-trade dollar cap |
| `ENTRY_THRESHOLD` | 10.0 | Min raw confidence to emit a signal |
| `MIN_RAW_SIGNAL_CONFIDENCE` | 15.0 | Floor before regime multiplier |
| `MIN_MINUTES_REMAINING` | 4.0 | Don't enter with < 4 min left |
| `MAX_SPREAD_CENTS` | 20¢ | Reject illiquid contracts |
| `MIN_CONTRACT_VOLUME` | 25 | Reject thin contracts |
| `MAX_QUOTE_AGE_MS` | 2000ms | Reject stale Kalshi quotes |
| `EARLY_EXIT_TF_THRESHOLD` | 4/5 | TFs reversed to trigger early exit |
| `EARLY_EXIT_MIN_SAVINGS_CENTS` | 5¢ | Don't exit if saving < 5¢ |
| `TAKE_PROFIT_CENTS` | 12¢ | Lock gains when up 12¢ |
| `ENABLE_REENTRY` | True | Re-enter opposite side after reversal |
| `REENTRY_MIN_MINUTES` | 5.0 | Min time remaining for re-entry |
| `DAILY_LOSS_LIMIT` | $500 | Kill switch threshold |

### Dynamic Sizing

| Constant | Value | Description |
|---|---|---|
| `DYNAMIC_SIZING_ENABLED` | True | Enable rolling WR-based scale-up |
| `DYNAMIC_SIZING_LOOKBACK` | 20 | Rolling window for WR calc |
| `DYNAMIC_SIZING_FLOOR_PCT` | 2.0% | Fallback when no tier matched |
| `DYNAMIC_SIZING_TIERS` | [(30,0.65,8.0),(30,0.58,5.0),(20,0.55,3.5)] | (min_n, min_wr, pct) |

### HFT Engine

| Constant | Value | Description |
|---|---|---|
| `HFT_ENABLED` | True | Enable HFT parallel task |
| `HFT_POLL_INTERVAL` | 4.0s | Order book poll frequency |
| `HFT_MIN_MINUTES_REMAINING` | 2.5 | Don't open new HFT positions this close |
| `HFT_ARB_MIN_EDGE_CENTS` | 2¢ | Min guaranteed profit for YES+NO arb |
| `HFT_ARB_MAX_CONTRACTS` | 5 | Max contracts per arb |
| `HFT_SCALP_MIN_EDGE_CENTS` | 8¢ | Min edge to enter a scalp |
| `HFT_SCALP_TARGET_CENTS` | 8¢ | Scalp take-profit |
| `HFT_SCALP_STOP_CENTS` | 6¢ | Scalp stop-loss |
| `HFT_SCALP_ENTRY_TIMEOUT` | 30s | Cancel unfilled entry after this long |
| `HFT_SCALP_MAX_PER_WINDOW` | 6 | Max scalp round-trips per contract window |

### config_phase3.py (signal filters)

| Constant | Value | Description |
|---|---|---|
| `REGIME_TIMEFRAME` | 5m | Feed regime detector 5m candles |
| `MIN_TF_ALIGNMENT` | 3 | Min TFs aligned to approve signal |
| `BAD_UTC_HOURS` | {9,12,15,21} | Blocked trading hours |
| `REQUIRE_FAVORABLE_REGIME` | True | Gate on regime classification |
| `DER_TREND_THRESH` | 0.35 | DER above this = trending |
| `ATR_VOL_THRESH` | 1.5 | ATR ratio above this = volatile |

---

## 9. Performance

### 9.1 Live Trading (as of 2026-03-15)

| Metric | Value |
|---|---|
| Starting balance | $24.13 (synced 2026-03-13) |
| Current balance | ~$36.53 (last sync) |
| Total trades placed | 17 |
| Settled | 16 |
| Pending | 1 |
| Wins (won + exited_win) | 5 |
| Losses | 12 |
| **Live WR** | **5/17 = 29.4%** |
| Total realized P&L | -$2.26 |

**By side:**

| Side | Trades | Avg entry price | Total P&L |
|---|---|---|---|
| YES (CALL) | 9 | 39.7¢ | +$0.27 |
| NO (PUT) | 8 | 46.9¢ | -$2.53 |

**By outcome:**

| Status | Count | Total P&L | Avg P&L |
|---|---|---|---|
| Won | 4 | +$2.25 | +$0.56 |
| Exited (win) | 1 | +$0.16 | +$0.16 |
| Lost | 12 | -$4.67 | -$0.39 |

**Note:** 17 live trades is statistically insufficient to evaluate the model.
The 90-day backtest baseline is 63.5% WR. The current 29.4% is likely a
combination of small sample variance and the NO side being placed during a
sustained BTC uptrend (engine correctly identified PUT signals but market
moved against them structurally). Minimum 30+ settled trades needed to
evaluate with any confidence.

### 9.2 Backtest Reference (90-day Strategy E)

| Metric | Value |
|---|---|
| Strategy | E — TF Majority (≥3/5 alignment) |
| Period | 90 days of 1m BTC data |
| Baseline WR | **63.5%** |
| Best cell | RANGING × NY-Prime: **71.3%** |
| Worst cell | VOLATILE × After-Hrs: **55.0%** |
| London × VOLATILE | **Blocked** (46% WR in backtest) |

**Regime × Session WR matrix:**

| Regime | Asia | London | NY-Open | NY-Prime | After-Hrs |
|---|---|---|---|---|---|
| TRENDING_UP | 64.2% | 66.4% | 63.3% | 60.3% | 60.1% |
| TRENDING_DOWN | 59.9% | 59.5% | 67.4% | 58.0% | 58.9% |
| RANGING | 63.5% | 67.5% | 66.8% | **71.3%** | 66.2% |
| VOLATILE | 63.5% | *blocked* | 59.1% | 63.5% | 55.0% |

### 9.3 Key Thresholds to Watch

| Threshold | Purpose |
|---|---|
| 30 settled trades | Minimum for statistical validity |
| ≥55% rolling WR (last 20) | Unlocks 3.5% position sizing |
| ≥58% rolling WR (last 20) | Unlocks 5.0% position sizing |
| ≥65% rolling WR (last 20) | Unlocks 8.0% position sizing |
| WR tracks toward 63.5% | Confirms engine is performing near backtest |

---

## 10. File Inventory

| File | Role |
|---|---|
| `main.py` | Orchestrator — async event loop, Engine class, all task coordination |
| `bias_engine.py` | Per-TF momentum scorer (EMA, RSI, volume, candle pressure) |
| `aggregator.py` | Rolls 1m candles into higher TFs |
| `consensus.py` | Fourier-weighted multi-TF aggregation → ConsensusSignal |
| `regime_detector.py` | DER + ATR + EMA fan → regime classification |
| `signal_intelligence.py` | SignalFilter, WinRateTracker, regime×session WR table |
| `strategy_index.py` | Backtest-driven regime×session strategy config lookup |
| `position_manager.py` | Sizing, dynamic scale, kill switch, order execution |
| `kalshi_client.py` | Kalshi API v2 client — RSA-PSS auth, orders, order book |
| `hft_engine.py` | Parallel HFT loop — YES+NO arb + directional scalping |
| `signal_logger.py` | Async SQLite writer for trades.db + signals.db |
| `models.py` | Shared dataclasses (Candle, BiasResult, ConsensusSignal, TradeRecord) |
| `indicators.py` | EMACalc, SMACalc, ATR primitives |
| `ws_client.py` | Binance.US WebSocket client |
| `config.py` | All primary constants and thresholds |
| `config_phase3.py` | Signal filter constants (regime, alignment, bad hours) |
| `ledger.py` | Human-readable trade ledger CLI |
| `inspect_orderbook.py` | Diagnostic: prints live order book depth |
| `data/trades.db` | SQLite — kalshi_trades, execution_log, trades |
| `data/signals.db` | SQLite — every consensus signal generated |
| `data/engine.log` | Live engine stdout |

---

## 11. Known Gotchas

| Issue | Detail |
|---|---|
| `fill_count_fp` | Kalshi API v2 returns this as a string (e.g. `"1.00"`) — not `filled_count` |
| `average_price` | Not returned directly — derived from `taker_fill_cost_dollars + maker_fill_cost_dollars` |
| `orderbook_fp` | Order book key is `orderbook_fp`, not `orderbook`; prices are dollar floats (`yes_dollars`) |
| YES+NO complement | `yes_ask = 100 - best_no_bid` — they're not independent; derived from opposite bid stack |
| NSSM silent fail | `Stop/Start-Service` can return success but process may not restart — always verify with `Get-Service` |
| Restart with pending order | `_scrub_pending_orders` skips non-expired pending orders, leaving `open_orders` empty; engine may double-enter on restart |
| Dynamic sizing DB read | `_compute_dynamic_max_pct()` makes a synchronous `sqlite3` call on every `size_trade()` — acceptable latency for 15m contracts |
| Log buffering | `engine.log` lags 10–30s due to stdout buffering — DB is always source of truth |
| HFT + main engine conflict | Both share `KalshiClient` — HFT positions tracked separately in `HFTEngine._scalp`; main engine's `open_orders` doesn't know about HFT positions |
| Pre-market books | `initialized` contracts return empty order books (`yes_dollars: []`) — normal before market opens |
