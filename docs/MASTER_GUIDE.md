# BTC Bias Engine — Master Guide
## For AI Consultation & Improvement

**Last updated:** 2026-03-14
**Engine version:** Phase 3 + Strategy Index + After-Hours TF Weighting
**Live contract:** KXBTC15M (Kalshi 15-minute BTC up/down binary)
**Base directory:** `F:/Trading/btc-bias-engine/`

---

## Table of Contents

1. [System Purpose & Contract Mechanics](#1-system-purpose--contract-mechanics)
2. [Backtesting — Foundation & Validation](#2-backtesting--foundation--validation)
3. [Signal Generation Pipeline](#3-signal-generation-pipeline)
4. [Kalshi Contract Ordering — End to End](#4-kalshi-contract-ordering--end-to-end)
5. [Strategy Index — Regime × Session Routing](#5-strategy-index--regime--session-routing)
6. [Risk Management & Kill Switch](#6-risk-management--kill-switch)
7. [Configuration Reference](#7-configuration-reference)
8. [Database Schema & Logging](#8-database-schema--logging)
9. [Deployment & Operations](#9-deployment--operations)
10. [Known Gaps & Improvement Opportunities](#10-known-gaps--improvement-opportunities)

---

## 1. System Purpose & Contract Mechanics

### What the system does

The engine predicts whether Bitcoin's price will be **higher or lower** at the close of a 15-minute window versus its open, then bets on that prediction via Kalshi binary contracts.

### KXBTC15M Contract Mechanics

```
Contract: KXBTC15M-26MAR131930-30
          ^^^^^^^  ^^^^^^^ ^^^^^
          series   date    expiry (HH:MM UTC)

YES resolves if: BTC close >= BTC open (for that 15m window)
NO  resolves if: BTC close <  BTC open

Max payout per contract: $1.00
Cost of YES at 60¢ ask: $0.60 → wins $0.40, loses $0.60
Cost of NO  at 40¢ ask: $0.40 → wins $0.60, loses $0.40

CALL signal → buy YES (bet BTC goes up)
PUT  signal → buy NO  (bet BTC goes down)
```

**Edge formula:**
`edge = (expected_win_rate / 100) - (contract_ask_cents / 100)`
Minimum edge required: **5%** (configurable via `min_edge` in `PositionManager`)

**Breakeven:** At 60¢ ask, you need to win ≥ 60% of the time to profit.
**Key constraint:** Kalshi is a limit-order exchange — orders rest in the book and may not fill.

---

## 2. Backtesting — Foundation & Validation

### Data

- **File:** `data/btc_1m_90d.csv` (~128,000 rows, 7.3 MB)
- **Source:** Binance.US REST API via `fetch_history.py`
- **Period:** 90 days of 1-minute BTCUSDT OHLCV
- **Columns:** timestamp (Unix ms), open, high, low, close, volume

To refresh data:
```bash
python fetch_history.py
```

### Running Backtests

```bash
# Multi-strategy comparison (the primary backtest)
python backtest_strategies.py

# Single-engine validation vs Pine Script
python backtest.py

# Regime × time-of-day analysis
python analyze_regime_tod.py
```

### How `backtest_strategies.py` Works

1. **Load candles** from CSV (`_load_candles`)
2. **Replay** through the full indicator pipeline (`_replay_candles`):
   - Each 1m candle feeds all five `BiasEngine` instances (1m direct, 3m/5m/15m/1h via `CandleAggregator`)
   - `RegimeDetector` updates on every completed 5m candle
   - At each 15m boundary, record the best signal seen in the window and the window outcome (CALL/PUT)
3. **Evaluate** each of 5 strategies against the recorded windows (`_evaluate_strategies`)
4. **Output** per-strategy stats and a comparison table

### `WindowRecord` — the core backtest unit

```python
@dataclass
class WindowRecord:
    window_ts:    int           # Unix ms of window open
    window_open:  float         # BTC price at window open
    window_close: float         # BTC price at window close
    outcome:      str           # "CALL" if close>=open, "PUT" if close<open, "FLAT" if equal
    best_signal:  ConsensusSignal   # Highest-confidence signal seen in window
    last_signal:  ConsensusSignal   # Most recent signal before window boundary
    best_regime:  Regime        # Most favorable regime seen in window
```

### Five Strategies Tested

| Strategy | Filter | Uses | 90-Day WR |
|----------|--------|------|-----------|
| A (Current Live) | conf≥50, aligned≥3, regime_mult≥0.5 | Best signal | ~58% |
| B (Direction Only) | None | Last signal | ~52% |
| C (Fourier≥35) | fourier≥35 | Best signal | ~55% |
| D (Relaxed) | conf≥30, aligned≥2 | Best signal | ~56% |
| **E (TF Majority)** | **aligned≥3** | **Last signal** | **63.5%** ✓ |

**Strategy E is deployed live.** It requires only that ≥3 of 5 timeframes agree on direction (positive/negative score), no confidence floor. It uses the *last* signal before the 15m boundary, not the highest-confidence one, because the most recent signal reflects the freshest price action.

### Regime × Session Analysis (`analyze_regime_tod.py`)

Runs Strategy E signals through a cross-tabulation of:
- **Regimes:** TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, UNKNOWN
- **Sessions:** Asia (00-08 UTC), London (08-13), NY-Open (13-17), NY-Prime (17-21), After-Hrs (21-24)

**Key empirical findings from 90-day backtest:**

```
BEST CELLS (≥65% WR, ≥50 trades):
  RANGING × NY-Prime:  71.3% WR  ← best cell, position_scale=1.5x
  RANGING × NY-Open:   66.8% WR
  RANGING × London:    67.5% WR
  RANGING × After-Hrs: 66.2% WR

WORST CELLS (blocked):
  VOLATILE × London:   46.0% WR  ← only losing cell, BLOCKED
  VOLATILE × After-Hrs: 53.3% WR ← BLOCKED

SURPRISING FINDING:
  RANGING is the BEST regime (not TRENDING).
  The confidence_multiplier for RANGING is 1.0 (not discounted).
  RANGING is included in is_favorable=True.

BAD HOURS (UTC): 9, 12, 15, 21
BEST HOUR (UTC): 16 (68.5% WR — was incorrectly blocked, now allowed)
```

### How to Validate a Code Change via Backtest

Before deploying any change to signal generation, consensus weighting, or filtering:

```bash
# 1. Make your change
# 2. Run the strategy comparison
python backtest_strategies.py

# 3. Check Strategy E win rate hasn't degraded from 63.5%
# 4. Run regime analysis to see if any cells shifted significantly
python analyze_regime_tod.py

# 5. Run unit tests
python -m pytest tests/ -q
```

**Critical:** Changes to `bias_engine.py`, `consensus.py`, `indicators.py`, or `config.py` weights/lengths will affect backtested WR. Always re-run `backtest_strategies.py` after touching these files.

---

## 3. Signal Generation Pipeline

### Overview

```
Binance WS (1m candles)
  │
  ├─→ BiasEngine["1m"].update(candle)              → BiasResult
  ├─→ CandleAggregator["3m"].update(candle)  ─────→ (when complete) BiasEngine["3m"].update()
  ├─→ CandleAggregator["5m"].update(candle)  ─────→ BiasEngine["5m"] + RegimeDetector.update()
  ├─→ CandleAggregator["15m"].update(candle) ─────→ BiasEngine["15m"]
  └─→ CandleAggregator["1h"].update(candle)  ─────→ BiasEngine["1h"]
                    │
                    ▼
            ConsensusLayer.compute(results, timestamp, weights)
                    │
                    ▼
            ConsensusSignal (direction, confidence, fourier_score, aligned_count)
```

### BiasEngine — Single TF Scoring

Each `BiasEngine` instance runs for one timeframe. It tracks 15-minute *cycle boundaries* regardless of its own TF (a 1m engine still knows where 15m windows are).

**Five scoring components** (all normalized to a signed score):

| Component | Config Key | Weight | Formula |
|-----------|------------|--------|---------|
| cycleReturnPct | `CYCLE_RETURN_W` | 120 | `(close - cycle_open) / cycle_open * 100` |
| emaSpreadPct | `EMA_SPREAD_W` | 200 | `(fast_ema - slow_ema) / close * 100` |
| rsiBias | `RSI_BIAS_W` | 25 | `(rsi - 50) / 50 * 100` |
| candlePressure | `CANDLE_PRESSURE_W` | 15 | `(close - low) / (high - low + ε) * 100 - 50` |
| relVol | `REL_VOL_W` | 10 | `clamp(volume / avg_volume, 0, 3) * 100 / 3` |

```
raw_score = Σ(component_value × weight) / Σ(weights)
score     = EMA(raw_score, period=SCORE_SMOOTH_LEN=2)
bull_conf = clamp(score, 0, 100)    # positive values only
bear_conf = clamp(-score, 0, 100)   # negative values only
```

**EMA lengths:** fast=5, slow=13, RSI=7, vol_avg=20, score_smooth=2 — all in `config.py`

### CandleAggregator

Converts 1m candles into higher TF candles by accumulating OHLCV until the period boundary (3m, 5m, 15m, or 60m from Unix epoch). Returns `None` until a candle completes, then returns the completed `Candle`.

### ConsensusLayer — Fourier Weighting

```python
signed_score[tf] = bull_conf[tf] - bear_conf[tf]   # range: -100 to +100
weighted_sum      = Σ(signed_score[tf] × weight[tf])
fourier_score     = weighted_sum / Σ(weights)
confidence        = abs(fourier_score)              # range: 0-100
direction         = "CALL" if fourier_score > 0 else "PUT"
aligned_count     = count of TFs whose sign matches consensus direction
```

**Standard weights (UTC 08-21):**

| TF | Weight | Rationale |
|----|--------|-----------|
| 1m | 0.10 | Highest noise |
| 3m | 0.15 | |
| 5m | 0.20 | |
| 15m | 0.30 | Structural momentum |
| 1h | 0.25 | Longer trend context |

**After-Hours weights (UTC 21-08):** `AFTER_HRS_TF_WEIGHTS`

| TF | Weight | Change |
|----|--------|--------|
| 1m | 0.10 | same |
| 3m | 0.15 | same |
| 5m | 0.28 | +0.08 |
| 15m | 0.37 | +0.07 |
| 1h | 0.10 | **-0.15** |

**Why:** The 1h TF updates only once per hour. During after-hours, it can lock in a stale directional bias (e.g., -53 from a prior hour's drop) that overwhelms strong short-term momentum signals. Reducing its weight lets 5m/15m momentum be decisive.

**Effect:** The 23:17 UTC PUT signal (wrong direction, would have traded) dropped from 19.5% → 13.3% confidence under after-hours weights, blocked by the 15% floor. A hypothetical strong 4/5 TF CALL against a stale 1h DOWN goes from 3.5% (blocked) → 20.1% (approved).

### RegimeDetector

Runs on every completed **5m candle** (set by `REGIME_TIMEFRAME` in `config_phase3.py`).

**Three voting metrics:**

1. **DER (Directional Efficiency Ratio):** `net_displacement / total_path` over 20 candles
   - DER ≥ 0.35 AND EMA fan ≥ 0.3 → TRENDING
   - DER < 0.15 AND tangled EMAs → RANGING

2. **ATR Expansion:** `short_atr / long_atr`
   - Ratio ≥ 1.5 with no sustained direction → VOLATILE

3. **EMA Fan:** Alignment of fast(8)/mid(21)/slow(55) EMAs
   - Perfect bullish stack (fast>mid>slow) → +1.0
   - Perfect bearish stack → -1.0

**Regime → confidence multiplier:**
```
TRENDING_UP/DOWN: 0.8 + (trending_strength / 100) × 0.2
RANGING:          1.0  ← best regime, no discount
VOLATILE:         0.4-0.7
UNKNOWN:          0.5
```

**Warmup:** Needs ~55+ 5m candles before meaningful classification. Warm start pre-loads 99 historical 1m candles, giving ~19 5m candles — enough to initialize but not always enough for stable regime. In practice, the engine reaches stable regime within 2-3 minutes of live WebSocket data.

### SignalFilter — Approval Gates

Checks applied **in order** (first failure = rejection, no further checks):

```
1. BLOCKED_CELL      — strategy_index says this regime×session is blocked
2. LOW_CONFIDENCE    — raw signal confidence < MIN_RAW_SIGNAL_CONFIDENCE (15%)  ← hard floor
3. LOW_CONFIDENCE    — raw signal confidence < strategy.min_confidence (0% for Strategy E)
4. BAD_HOUR          — utc_hour in {9, 12, 15, 21}
5. UNFAVORABLE_REGIME — VOLATILE × London (UTC 08-13)
6. UNFAVORABLE_REGIME — VOLATILE with trending_strength < 40
7. INSUFFICIENT_ALIGNMENT — aligned_count < strategy.min_alignment (3)
8. SEVERE_DIVERGENCE — higher TFs (15m, 1h) oppose consensus direction
9. MODEL_DEGRADED    — live win rate > 10% below backtest baseline for this bucket
```

**After passing all gates:**
```python
adjusted_conf = signal.confidence × regime.confidence_multiplier
cell_wr       = strategy.effective_wr   # from strategy_index backtest table
adjusted_conf = max(cell_wr, adjusted_conf)  # floor: never below empirical WR
```

The `adjusted_confidence` floor ensures the position manager's edge check uses the real historical win rate (e.g., 66.2% for RANGING×After-Hrs) rather than a potentially discounted raw signal score.

---

## 4. Kalshi Contract Ordering — End to End

### Contract Discovery

```python
contracts = await client.find_btc_contracts(
    window_minutes=15,
    min_minutes_remaining=2.0,   # inner param; PositionManager applies 4.0 min threshold
)
```

**What `find_btc_contracts` does:**
1. Calls `GET /markets?series_ticker=KXBTC15M&limit=100`
2. Filters to markets where `open_time <= now < close_time`
3. Parses `yes_ask_dollars`, `yes_bid_dollars`, `no_ask_dollars`, `no_bid_dollars`, `volume_fp`, `close_time`
4. Sorts ascending by expiry, returns only tradeable contracts (`status=open/active`, `minutes_to_expiry > 0.5`)

In practice there is almost always exactly **one** open KXBTC15M contract at a time.

### Position Manager Filters (applied in `size_trade`)

The position manager runs these checks before sizing any order:

```
1. Kill switch active? → abort
2. decision.approved?  → abort if not
3. minutes_to_expiry < 4.0 → abort (MIN_MINUTES_REMAINING)
4. spread_cents > 20 → abort (MAX_SPREAD_CENTS)
   spread = (ask - bid) × 100 for the relevant side
5. volume < 25 → abort (MIN_CONTRACT_VOLUME)
6. edge < 0.05 → abort
   effective_conf = decision.expected_wr / 100   (strategy backtest WR)
   breakeven      = limit_price / 100
   edge           = effective_conf - breakeven
```

### Order Direction Logic

```python
side = "yes" if signal.direction == "CALL" else "no"

# For YES side:
bid = contract.yes_bid
ask = contract.yes_ask

# For NO side:
bid = contract.no_bid
ask = contract.no_ask

# Limit price = ask (aggressive fill — never mid-market)
limit_price = max(1, min(99, round(ask * 100)))
```

**Why ask price:** Mid-market orders rest and may expire unfilled. Using the ask guarantees an immediate match if a seller exists at that price.

### Stake Sizing Formula

```python
cost_per_contract = limit_price / 100.0

conf_frac      = decision.adjusted_confidence / 100.0
position_scale = decision.position_scale               # from strategy_index (0.25 – 2.0)
base_risk      = equity × (MAX_PCT_EQUITY / 100) × conf_frac
scaled_risk    = base_risk × position_scale
target_risk    = min(scaled_risk, STAKE_CAP)           # hard cap at $100

count          = max(1, int(target_risk / cost_per_contract))
dollar_risk    = count × cost_per_contract
```

**Current values:** `MAX_PCT_EQUITY=2.0`, `equity≈$23`, so at 100% confidence and 1x scale:
`base_risk = 23 × 0.02 × 1.0 = $0.46` → 1 contract at most prices.

**To increase sizing:** Raise `MAX_PCT_EQUITY` in `config.py`. At 5% with $23 equity:
`base_risk = $1.15` → still likely 1-2 contracts, but scales properly when equity grows.

### Order Placement

```python
order = await client.place_order(
    ticker=contract.ticker,
    side=side,              # "yes" or "no"
    count=count,
    price=limit_price,      # cents (1-99)
    order_type="limit",
)
```

**Kalshi API call:** `POST /portfolio/orders`
**Payload:**
```json
{
  "ticker": "KXBTC15M-26MAR131930-30",
  "action": "buy",
  "side": "no",
  "count": 1,
  "type": "limit",
  "no_price": 38
}
```
Note: `yes_price` vs `no_price` key depends on `side`.

**Auth:** Every request signed with RSA-PSS.
Signature covers: `"{timestamp_ms}{METHOD}{/trade-api/v2/full/path}"` (no query string).
Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`

### Order Statuses

| Status | Meaning |
|--------|---------|
| `resting` | In order book, waiting for counterparty |
| `executed` | Order accepted (does NOT mean filled — check `filled_count`) |
| `canceled` | Cancelled (engine auto-cancels resting orders with < 2 min remaining) |

**Kalshi uses limit-order matching.** `status=executed` only means the API accepted the request, not that it was filled. Always check `filled_count` to determine if contracts were actually traded.

### Outcome Tracking (`_outcome_poller`)

Runs every **60 seconds** in the background. Two sub-tasks:

**1. Cancel stale orders (`_check_and_cancel_stale_orders`)**
```
For each open_order:
  if (contract.expiry_ts - now) < 2 min:
    order = await client.get_order(order_id)
    if order.status == "resting":
      await client.cancel_order(order_id)
      record_outcome(order_id, is_win=False, pnl=0.0)
      log_kalshi_outcome(..., result="cancelled")
```

**2. Settle expired contracts (`_settle_expired_orders`)**
```
For each open_order:
  if now < contract.expiry_ts + 90s:
    continue   # grace period for Kalshi settlement

  contract = await client.get_contract(ticker)
  if contract.result is None:
    continue   # not yet settled

  order = await client.get_order(order_id)
  filled = order.filled_count

  if filled == 0:
    → record as unfilled, $0 PnL
  else:
    is_win = (contract.result == trade.intent.side)
    if is_win:  pnl = filled × (1.0 - fill_price / 100)
    else:       pnl = -filled × (fill_price / 100)
    → update PositionManager equity
    → update WinRateTracker
    → log_kalshi_outcome to DB
```

**Contract result field:** `"yes"` or `"no"` (string), set by Kalshi after settlement. `None` while pending.

**Win determination:** `contract.result == side` where `side` is `"yes"` or `"no"`.

### Startup Recovery (on every restart)

**1. Balance sync:**
```python
bal = await client.get_balance()
position_mgr._equity = bal.balance / 100.0
```
Kalshi returns balance in cents. Ensures equity tracker matches real account balance regardless of prior session outcomes.

**2. Dedup recovery:**
```python
# Reads most recent pending order from DB
# Sets _last_signal_ts to that window's cycle boundary
# Prevents re-entering the same 15m window after restart
```

**3. Pending order scrub (`_scrub_pending_orders`):**
```python
# Queries all rows with status='pending' in kalshi_trades
# For each: fetch contract result + order filled_count from Kalshi
# Updates DB with correct status/pnl
# (equity already synced via get_balance)
```
Handles orders placed by previous engine instances that the outcome poller never tracked.

### Unfill Rate Analysis

In live trading (2026-03-13), **all 6 orders were unfilled** (0 fills, 0 losses, 0 wins). The engine uses ask price but still gets unfills because:

1. **Thin after-hours liquidity** — fewer participants willing to sell at the ask
2. **Rapid price movement** — ask changes between contract fetch and order placement
3. **Order book mechanics** — even at ask, Kalshi may batch-match and miss

**Implication for improvement:** Consider whether the current `EXECUTE_TRADES=true` period (after-hours, ASIA session) is appropriate. London and NY sessions have higher volume and better fills. Alternatively, investigate if `order_type="market"` would improve fills (Kalshi supports market orders).

---

## 5. Strategy Index — Regime × Session Routing

### Architecture

```
signal arrives
  ↓
regime = regime_detector.state.regime
utc_hour = candle.timestamp → UTC hour
  ↓
strategy = strategy_index.select(regime, utc_hour)
  ↓
if strategy.blocked → FilterReason.BLOCKED_CELL
else → use strategy.min_alignment, strategy.expected_wr, strategy.position_scale
```

### `StrategyConfig` Fields

```python
@dataclass
class StrategyConfig:
    name:           str            # e.g. "E-RANGING-NYPRIME"
    min_alignment:  int   = 3      # TFs required (Strategy E: always 3)
    min_confidence: float = 0.0   # raw confidence floor (Strategy E: 0)
    expected_wr:    float = 63.5  # empirical WR from backtest table
    live_wr:        Optional[float] = None  # future: adaptive override
    position_scale: float = 1.0   # sizing multiplier (0.25–2.0)
    blocked:        bool  = False  # completely block this cell
    notes:          str   = ""

    @property
    def effective_wr(self):
        return self.live_wr if self.live_wr is not None else self.expected_wr
```

### Full Strategy Table (from `strategy_index.py`)

| Regime | Session | Expected WR | Scale | Blocked? |
|--------|---------|-------------|-------|----------|
| TRENDING_UP | Asia | 64.2% | 0.75 | No |
| TRENDING_UP | London | 66.4% | 1.0 | No |
| TRENDING_UP | NY-Open | 63.3% | 0.75 | No |
| TRENDING_UP | NY-Prime | 60.3% | 0.5 | No |
| TRENDING_UP | After-Hrs | 60.1% | 0.5 | No |
| TRENDING_DOWN | Asia | 59.9% | 0.5 | No |
| TRENDING_DOWN | London | 59.5% | 0.5 | No |
| TRENDING_DOWN | NY-Open | 67.4% | 1.0 | No |
| TRENDING_DOWN | NY-Prime | 58.0% | 0.5 | No |
| TRENDING_DOWN | After-Hrs | 58.9% | 0.5 | No |
| **RANGING** | **Asia** | **63.5%** | **0.75** | No |
| **RANGING** | **London** | **67.5%** | **1.0** | No |
| **RANGING** | **NY-Open** | **66.8%** | **1.25** | No |
| **RANGING** | **NY-Prime** | **71.3%** | **1.5** | **No (BEST CELL)** |
| **RANGING** | **After-Hrs** | **66.2%** | **1.0** | No |
| VOLATILE | Asia | 63.5% | 0.75 | No |
| VOLATILE | London | 46.0% | 0.0 | **BLOCKED** |
| VOLATILE | NY-Open | 59.1% | 0.5 | No |
| VOLATILE | NY-Prime | 63.5% | 0.75 | No |
| VOLATILE | After-Hrs | 53.3% | 0.0 | **BLOCKED** |
| UNKNOWN | (all) | 63.5% | 0.75 | No |

**Bad UTC hours** (blocked globally regardless of regime): 9, 12, 15, 21
**Low-sample blocking:** Cells with < 30 historical trades are auto-blocked when `BLOCK_LOW_SAMPLE_CELLS=True`

### Session Boundaries (UTC)

```
ASIA:     00:00 – 07:59
LONDON:   08:00 – 12:59
NY_OPEN:  13:00 – 16:59
NY_PRIME: 17:00 – 20:59
AFTER_HRS: 21:00 – 23:59
```

### Future: Adaptive Live WR

`StrategyConfig.live_wr` exists for future adaptive updating. Once 100+ trades accumulate per cell, `WinRateTracker` data could populate `live_wr`, overriding the backtest `expected_wr`. The `effective_wr` property already routes to `live_wr` when set.

---

## 6. Risk Management & Kill Switch

### Daily Loss Limit

```python
DAILY_LOSS_LIMIT = 500.0  # dollars

# Checked after every outcome:
if daily_pnl <= -daily_loss_limit:
    daily.killed = True
    → no more trades today
```

Resets at UTC midnight. State is in-memory only (resets on restart).

### Equity Tracking

- **At startup:** synced from `client.get_balance()` (live Kalshi balance)
- **After each win/loss:** `equity += pnl`
- **Does not track:** open position value (only realized P&L)

Current balance (2026-03-13): **$23.29** (started at $25.81 — $2.52 loss from prior activity before outcome tracking was implemented)

### Position Sizing Caps

```
Per-trade cap:  STAKE_CAP = $100   (regardless of confidence or equity)
Max per-trade:  MAX_PCT_EQUITY = 2% of equity at 100% confidence
Min contracts:  1 always (never 0)
Scale bounds:   MIN_POSITION_SCALE=0.25, MAX_POSITION_SCALE=2.0
```

### PID Lock File

Location: `data/engine.pid`
Uses `psutil` to verify the PID is still alive and is a Python process.
On startup: aborts if another live instance detected. Cleans up on exit.

---

## 7. Configuration Reference

### `config.py` — Primary Settings

```python
# Indicator lengths (match Pine Script exactly)
FAST_EMA_LEN = 5       # Fast EMA for emaSpread component
SLOW_EMA_LEN = 13      # Slow EMA for emaSpread component
RSI_LEN = 7            # RSI period
VOL_AVG_LEN = 20       # Volume average period
SCORE_SMOOTH_LEN = 2   # EMA smoothing on raw score

# Scoring weights (match Pine Script exactly)
CYCLE_RETURN_W = 120.0
EMA_SPREAD_W = 200.0   # Dominant component
RSI_BIAS_W = 25.0
CANDLE_PRESSURE_W = 15.0
REL_VOL_W = 10.0

# Fourier weights
TF_WEIGHTS = {1m:0.10, 3m:0.15, 5m:0.20, 15m:0.30, 1h:0.25}
AFTER_HRS_TF_WEIGHTS = {1m:0.10, 3m:0.15, 5m:0.28, 15m:0.37, 1h:0.10}

# Kalshi execution
MIN_MINUTES_REMAINING = 4.0   # Skip contracts expiring in < 4 min
MAX_SPREAD_CENTS = 20.0       # Skip if bid-ask spread > 20¢
MIN_CONTRACT_VOLUME = 25      # Skip thin markets

# Risk
DAILY_LOSS_LIMIT = 500.0
STARTING_EQUITY = 24.13       # Overridden at runtime by Kalshi balance sync
MAX_PCT_EQUITY = 2.0          # % of equity at 100% confidence
STAKE_CAP = 100.0             # Hard per-trade dollar cap

# Signal quality floor (prevents WR floor from rescuing noise signals)
MIN_RAW_SIGNAL_CONFIDENCE = 15.0

# Strategy index
STRATEGY_INDEX_ENABLED = True
BLOCK_LOW_SAMPLE_CELLS = True
LOW_SAMPLE_THRESHOLD = 30
MIN_POSITION_SCALE = 0.25
MAX_POSITION_SCALE = 2.0
```

### `config_phase3.py` — Phase 3 Filters

```python
REGIME_TIMEFRAME = "5m"         # Which TF feeds regime detector
MIN_TF_ALIGNMENT = 3            # Min TFs aligned (overridden by strategy.min_alignment)
MIN_SIGNAL_CONFIDENCE = 0.0     # Strategy E uses alignment, not confidence
BAD_UTC_HOURS = {9, 12, 15, 21}
REQUIRE_FAVORABLE_REGIME = True

# Velocity filters (in BiasEngine)
SCORE_VELOCITY_CAP = 30.0
REQUIRE_VEL_ALIGN = True
LOCK_ON_FIRST_TOUCH = True
CONFIRM_BARS = 1

# Win rate tracking
CHECK_MODEL_DEGRADATION = True
WIN_RATE_WINDOW = 100
DEGRADATION_THRESHOLD_PCT = 10.0
```

### Environment Variables (NSSM service)

```
KALSHI_API_KEY           UUID from Kalshi account settings
KALSHI_PRIVATE_KEY_PATH  Path to RSA PEM file (e.g. C:\Users\..\.kalshi\kalshi_key.pem)
EXECUTE_TRADES           "true" to place real orders, "false" for signal-only
KALSHI_DEMO              "false" for live, "true" for demo API
DAILY_LOSS_LIMIT         Dollar amount (default 500)
PYTHONUNBUFFERED         1  (required for real-time log flushing via NSSM)
```

---

## 8. Database Schema & Logging

### `data/signals.db` — Every consensus signal

```sql
CREATE TABLE signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     INTEGER NOT NULL,        -- Unix ms (candle close time)
    direction     TEXT NOT NULL,           -- "CALL" / "PUT"
    confidence    REAL NOT NULL,           -- raw Fourier score magnitude (0-100)
    bucket        INTEGER NOT NULL,        -- 0-3 (25% bands)
    fourier_score REAL NOT NULL,           -- signed (-100 to +100)
    aligned_count INTEGER NOT NULL,        -- TFs agreeing with direction
    total_tfs     INTEGER NOT NULL,        -- always 5
    tf_scores     TEXT,                    -- JSON dict {tf: signed_score}
    created_at    INTEGER NOT NULL
)
```

**Note:** The signals table stores raw signal data regardless of whether the signal was approved or traded. Regime, strategy name, and approval reason are NOT stored here — only in the engine log.

### `data/trades.db` — All trade records

**`kalshi_trades` table — primary ledger:**
```sql
CREATE TABLE kalshi_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT NOT NULL UNIQUE,
    placed_at     TEXT NOT NULL,           -- "2026-03-13 23:18:00" UTC
    ticker        TEXT NOT NULL,           -- "KXBTC15M-26MAR131930-30"
    side          TEXT NOT NULL,           -- "yes" or "no"
    count         INTEGER NOT NULL,        -- number of contracts
    limit_price   INTEGER NOT NULL,        -- cents (e.g. 38)
    dollar_risk   REAL NOT NULL,           -- count × (limit_price/100)
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending/won/lost/unfilled/cancelled
    pnl           REAL,                    -- NULL until settled
    filled_count  INTEGER,                 -- NULL until settled
    result        TEXT,                    -- "yes"/"no" (Kalshi contract result)
    strategy_name TEXT,                    -- e.g. "E-RANGING-AFTERHRS"
    expected_wr   REAL,                    -- from strategy_index at time of trade
    created_at    INTEGER NOT NULL
)
```

**Status lifecycle:**
```
placed → "pending"
settled (filled=0) → "unfilled"
settled (filled>0, win) → "won"
settled (filled>0, loss) → "lost"
cancelled (stale) → see result="cancelled"
```

**`trades` table — legacy backtest/simulation records** (not used for live Kalshi trades)

### Viewing the Ledger

```bash
python ledger.py
```

Shows last 100 kalshi_trades (most recent first) + last 20 signals.

---

## 9. Deployment & Operations

### Service Management (Windows / NSSM)

```powershell
# Start/stop (use these, not nssm restart — which silently fails)
Stop-Service BTCBiasEngine -Force
Start-Service BTCBiasEngine

# Check status
Get-Service BTCBiasEngine

# Check for multiple instances (MUST be only 2 PIDs — NSSM parent + Python child)
Get-WmiObject Win32_Process | Where-Object Name -like 'python*' | Select ProcessId, CommandLine

# NSSM executable
C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe

# Log file
F:\Trading\btc-bias-engine\data\engine.log

# View live log (tail equivalent)
Get-Content 'F:\Trading\btc-bias-engine\data\engine.log' -Tail 30
```

### Startup Sequence (what happens on `Start-Service`)

1. `_acquire_pid_lock()` — write PID file, abort if another instance running
2. `SignalLogger.initialize()` — create DB tables if not exist
3. `_warm_start()` — fetch 99 closed 1m candles from Binance.US REST, feed through pipeline
4. `KalshiClient.__aenter__()` — open aiohttp session
5. `get_balance()` — sync real Kalshi balance to `PositionManager._equity`
6. `_restore_last_signal_ts()` — recover 15m dedup state from most recent pending order
7. `_scrub_pending_orders()` — settle any orders stuck as "pending" from prior sessions
8. `_outcome_poller()` — start background settlement task
9. `BinanceWSClient.start()` — connect to WebSocket, begin processing live candles

### Warm Start

Fetches from `https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=100`
Drops last candle (still-open bar), feeds the rest through `_on_candle(execute=False)`.
After 99 candles: all 5 TF BiasEngines have at least one result, RegimeDetector has ~19 5m candles of state.

### Key Log Patterns to Monitor

```
# Good — normal operation
BTC Bias Engine starting - mode=LIVE
Warm start complete: fed 99 historical candles. TFs ready: ['1m', '3m', '5m', '15m', '1h']
Balance synced from Kalshi: $23.29
Restored last_signal_ts from pending order at ...
Scrub: [order_id] unfilled (result=yes)
WebSocket connected.
Filter: APPROVED | regime=RANGING | align=4/5 | adj_conf=66.2
Strategy: E-RANGING-NYPRIME | WR=71.3% | Scale=1.5x
Order placed: id=... status=executed

# Warning signs
Warm start fetch failed: HTTP 429    ← Binance rate limit, retries fine
Order placement failed: ...          ← Kalshi API error
Trade rejected: insufficient edge    ← edge below 5%, normal behavior
Kill switch active                   ← daily loss limit hit
Another engine instance running      ← duplicate process, abort

# Outcome tracking
Outcome: WIN  | ticker | filled=1 @ 38¢ | pnl=+0.62 | equity=23.91
Outcome: LOSS | ticker | filled=1 @ 62¢ | pnl=-0.62 | equity=22.67
Order [id] unfilled - no P&L impact
```

### Tests

```bash
# Full suite (65 tests, ~0.3s)
python -m pytest tests/ -q

# Specific modules
python -m pytest tests/test_strategy_index.py -v
python -m pytest tests/test_phase3.py -v
```

---

## 10. Known Gaps & Improvement Opportunities

This section is the primary resource for an AI consultant to identify high-value changes. Ordered by expected impact.

### Tier 1 — High Impact, Well-Understood

#### 1.1 Position Sizing Scale-Up
**Problem:** `MAX_PCT_EQUITY=2%` of $23 forces every trade to 1 contract ($0.23-$0.51 risk). Strategy E has a proven 63.5%+ WR. The engine is under-betting by an order of magnitude.
**Fix:** Raise `MAX_PCT_EQUITY` to 5-10% after confirming live WR with real fills. At 5%: `$23 × 0.05 = $1.15` → 2-3 contracts typical.
**Risk:** Verify fill rates are acceptable at higher quantities before scaling. 1-contract orders at 38¢ are still unfilling, so check if market depth supports 2-3 contracts.
**File:** `config.py` → `MAX_PCT_EQUITY`

#### 1.2 Fill Rate Investigation
**Problem:** 6/6 orders placed so far have been unfilled. The engine uses ask price to guarantee fills, but still gets unfills.
**Hypotheses:**
- a) After-hours liquidity is genuinely thin — wait for London/NY sessions
- b) Market orders (`order_type="market"`) would guarantee fills at the cost of slippage
- c) Kalshi's order book requires matching at exact price — even ask orders rest if no seller
**Diagnosis:** Check if orders during London/NY prime (08-21 UTC) fill at higher rates.
**File:** `kalshi_client.py` → `place_order()` (consider `order_type="market"` option)

#### 1.3 Regime Persistence Across Restart
**Problem:** `RegimeDetector` state is fully in-memory. On restart with 99 warm-start candles, only ~19 5m candles exist — less than the 55-bar warmup minimum. First 2-3 minutes post-restart produce UNKNOWN regime signals.
**Fix:** Persist last-known regime to a JSON file on every regime change. Load and restore it on startup before the warm start.
**Benefit:** Reduces UNKNOWN regime trades immediately post-restart.
**File:** `regime_detector.py` + `main.py`

#### 1.4 Adaptive Strategy WR (Live Win Rate Integration)
**Infrastructure exists:** `StrategyConfig.live_wr` and `StrategyConfig.effective_wr` already route to live WR when set. `WinRateTracker` accumulates outcomes.
**Missing:** Wiring between `WinRateTracker` and `StrategyConfig.live_wr` per cell.
**When meaningful:** Each cell needs ≥100 filled trades before live WR is statistically reliable.
**Files:** `strategy_index.py`, `signal_intelligence.py`, `main.py`

### Tier 2 — Medium Impact, Some Unknowns

#### 2.1 Contract Staleness Detection
**Problem:** The contract's bid/ask is fetched when a signal fires. If the signal fires at :01 into the window and the order isn't placed until :02, the price may have moved.
**Fix:** After placing the order, immediately re-fetch the contract and compare current ask to fill price. If ask has moved >5¢, consider whether to cancel.
**Files:** `main.py._execute_signal()`

#### 2.2 Multi-Contract Window
**Problem:** The engine only trades the current open contract (nearest expiry). Kalshi often lists 2-3 contracts at different expiries. A signal with 12 minutes remaining could also be expressed with a contract expiring in 27 minutes (next window).
**Consideration:** Later contracts have more time value priced in, different probability distribution. This would require fundamental rethinking of signal timing.

#### 2.3 Signal Frequency Optimization
**Problem:** The engine generates 2 signals/minute (one from the 1m engine, another from the higher-TF engine that triggered). The deduplication takes only the first approved signal in a 15m window.
**Opportunity:** The best signal in a window (highest conviction) is usually not the first. Consider trading 2-3 minutes after a strong signal confirms, rather than on the first qualifying bar.
**Backtest finding:** Strategy E uses the *last* signal (63.5% WR) vs best signal (Strategy A: lower WR). This paradox — earlier signals being better — may be worth re-examining with the new filters.

#### 2.4 1h Score Normalization (Score Clipping)
**Problem:** `signed_score = bull_conf - bear_conf` can reach ±100. The 1h at -53 or -69 has a raw magnitude 2-3× the short TFs. After-hours weighting (1h→0.10) reduces but doesn't eliminate this.
**Alternative fix:** Clip each TF score to ±35 before weighting. This prevents any single TF from having disproportionate influence at extreme values while preserving direction information.
**Impact:** Changes signal magnitudes — re-backtest required.
**File:** `consensus.py` → `compute()` — apply `clamp(signed_score, -35, 35)` before weighting.

#### 2.5 Regime Detection Sensitivity
**Current:** DER threshold at 0.35/0.15, ATR threshold at 1.5. These were set heuristically.
**Opportunity:** Grid-search optimal thresholds via `analyze_regime_tod.py` — tune to maximize the win-rate difference between RANGING (best) and VOLATILE (worst).
**Files:** `regime_detector.py` init params, `config_phase3.py`

### Tier 3 — Infrastructure / Quality

#### 3.1 Signals Table — Add Regime and Filter Decision
**Problem:** The signals DB stores raw signal data but not the regime, strategy, or filter decision. Post-hoc analysis requires cross-referencing with engine logs.
**Fix:** Add `regime TEXT`, `strategy_name TEXT`, `filter_reason TEXT`, `approved INTEGER` columns to the signals table.
**Files:** `signal_logger.py`, `signal_intelligence.py`, `main.py`

#### 3.2 Daily Statistics Persistence
**Problem:** `DailyState` (trades_placed, trades_won, realized_pnl) is in-memory only. On restart, daily stats reset to zero even mid-day.
**Fix:** Write daily state to a small SQLite table or JSON file. Load on startup if trade_date matches today.
**File:** `position_manager.py`, `main.py`

#### 3.3 Alert System
**Problem:** No notifications for: kill switch triggered, model degraded, WebSocket disconnect > 5 min, unusual P&L.
**Fix:** Simple HTTP webhook (Pushover, Discord, or IFTTT) triggered from key events in `main.py`.
**Low effort:** Add `_send_alert(message)` helper that POSTs to a webhook URL stored in env var.

#### 3.4 Health Endpoint
**Problem:** No way to check engine health from outside the machine without reading log files.
**Fix:** Add a simple aiohttp server (single GET `/health` endpoint) returning current state as JSON: equity, daily_pnl, trades_today, open_orders, regime, last_signal_ts, ws_connected.

---

## File Quick Reference

```
F:/Trading/btc-bias-engine/
│
├── main.py              ← Orchestrator — start here for any live trading change
├── config.py            ← Primary constants — weights, risk params, execution filters
├── config_phase3.py     ← Phase 3 filter settings — bad hours, velocity, alignment
│
├── bias_engine.py       ← Single-TF scoring engine (matches Pine Script)
├── aggregator.py        ← 1m → higher TF resampler
├── consensus.py         ← Fourier-weighted multi-TF aggregation
├── regime_detector.py   ← DER + ATR + EMA fan → TRENDING/RANGING/VOLATILE
├── signal_intelligence.py ← Filter gates, WinRateTracker, regime×session WR table
├── strategy_index.py    ← Regime×session → StrategyConfig routing
├── position_manager.py  ← Edge check, stake sizing, kill switch
├── kalshi_client.py     ← Kalshi REST API client (RSA-PSS auth)
├── ws_client.py         ← Binance WebSocket consumer
├── signal_logger.py     ← SQLite async writer
│
├── backtest_strategies.py  ← Primary backtest — 5 strategies on 90-day data
├── backtest.py             ← Single-engine backtest + Pine Script validation
├── analyze_regime_tod.py   ← Regime × time-of-day win-rate analysis
├── fetch_history.py        ← Download 90 days of 1m candles from Binance.US
│
├── models.py            ← Candle, BiasResult, ConsensusSignal, TradeRecord
├── indicators.py        ← EMACalc, RSICalc, SMACalc (incremental, no look-ahead)
├── ledger.py            ← CLI ledger viewer (python ledger.py)
│
├── data/
│   ├── btc_1m_90d.csv   ← 90-day 1m candle history (backtest input)
│   ├── signals.db       ← All consensus signals (every minute)
│   ├── trades.db        ← kalshi_trades + trades tables
│   ├── engine.log       ← NSSM stdout/stderr log
│   └── engine.pid       ← PID lock file
│
├── tests/
│   ├── test_bias_engine.py
│   ├── test_consensus.py
│   ├── test_indicators.py
│   ├── test_phase3.py
│   └── test_strategy_index.py
│
└── docs/
    ├── MASTER_GUIDE.md  ← This file
    └── NEXT_SESSION.md  ← Session-specific context (may be stale)
```

---

## Critical Invariants — Do Not Break

1. **Pine Script parity in `bias_engine.py` and `indicators.py`:** EMA uses `k = 2/(n+1)`. RSI uses Wilder's smoothing after warmup. Changing these invalidates all backtests.

2. **`aligned_count` is the signal for Strategy E:** The whole system is gated on ≥3 TFs agreeing. This is what gives 63.5% WR. The confidence value is almost irrelevant for approval (MIN_SIGNAL_CONFIDENCE=0.0); only the 15% raw floor and alignment matter.

3. **`adjusted_confidence` floor = `expected_wr`:** The position manager's edge check uses `expected_wr` from the strategy, not raw signal confidence. This means the edge check reflects historical accuracy, not discounted signal score. Do not remove this floor without understanding the edge math.

4. **Kalshi order side vs signal direction:**
   `CALL → side="yes"` (YES pays if BTC close >= open)
   `PUT  → side="no"` (NO pays if BTC close < open)
   Swapping this = systematically trading the wrong direction.

5. **Ask price for limit orders:** Using mid-market price risks orders resting unfilled. Always use `ask` for the side being purchased.

6. **Outcome tracking relies on `contract.result` field:** Kalshi returns `"yes"` or `"no"` (lowercase string), not a boolean. Win check: `contract.result == trade.intent.side`.

7. **Equity syncs from Kalshi balance, not DB P&L:** The DB `pnl` column is informational. Ground truth is `get_balance()`. Do not compute equity from DB trade records.
