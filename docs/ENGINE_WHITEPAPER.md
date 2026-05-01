# BTC Bias Engine — Technical Whitepaper

## 1. System Overview

The BTC Bias Engine is an automated trading system for Kalshi KXBTC15M contracts — binary options that settle based on whether BTC's price at the end of a 15-minute window is above or below its price at the start. Contracts settle at $1.00 (correct) or $0.00 (incorrect). The engine operates 24/7 across 96 sessions per day, each lasting exactly 15 minutes.

The system synthesizes five independent data streams — Brownian Bridge probability modeling, Polymarket smart wallet flow, Kalshi order book dynamics, BTC tick-level microstructure, and mempool whale detection — into a unified entry signal. It enters via the Kalshi REST API, manages positions through dynamic take-profit ratcheting, and holds to settlement or exits on thesis collapse.

---

## 2. Architecture

### 2.1 Event Loop

The engine runs a single Python asyncio event loop with concurrent tasks:

| Task | Cadence | Purpose |
|---|---|---|
| Flow iteration | 0.3s | Core signal evaluation + position management |
| Wallet scorer | Hourly | Rescore Polymarket wallets from settled markets |
| Settlement reconciler | 5 min | Reconcile P&L against Kalshi settlement API |
| Whale monitor | Real-time | WebSocket subscription to mempool.space |
| Price feed | Real-time | Coinbase/Binance WebSocket for BTC spot + candles |

The flow iteration splits into a **fast path** (every 0.3s cycle: signal evaluation, position management using cached WebSocket data) and a **slow path** (every 3rd cycle: REST API polls for Polymarket trades, Kalshi tape, BTC spot fallback, TA candle updates). This separation ensures signal evaluation isn't blocked by API latency.

### 2.2 Data Flow

```
Coinbase WS ─────> BTC Spot Price ──────────────────────────┐
Binance WS ──────> 5s/1m/5m/15m Candles ──> Indicators ─────┤
                                              |              |
                                        TA Scorer        Prob Engine
                                         (1m dir)      (fair value)
                                              |              |
Kalshi WS ───────> Orderbook/Mid ─────────────┤──────────────┤
Kalshi REST ─────> Trade Tape ────────────────┤              |
                                              |              |
Polymarket REST ─> Smart Wallet Trades ───────┤        FVG Baseline
                                              |         (divergence)
Mempool.space WS > Whale BTC Flows ───────────┤              |
                                              |              |
                                        Signal Cascade ◄─────┘
                                              |
                                        Entry Decision
                                              |
                                        Kalshi REST (order)
                                              |
                                        Position Management
                                              |
                                  TP / Settlement / Exit
```

---

## 3. Probability Engine (Brownian Bridge Model)

### 3.1 Theoretical Basis

The engine models BTC's 15-minute price path as a geometric Brownian motion. Given current price S, strike K (session open), volatility sigma, and time remaining T, the probability of BTC finishing above strike is:

```
P(BTC > K at expiry) = CDF( ln(S/K) / (sigma * sqrt(T)) )
```

Where CDF is the standard Normal cumulative distribution function, implemented via the Abramowitz & Stegun erf approximation (max error 1.5e-7).

**Fair value** = P * 100 cents. **Mispricing** = fair_value - contract_mid.

### 3.2 Volatility Estimation

Realized volatility is computed from a rolling buffer of 180 five-second candles (15 minutes of data). Log-returns between consecutive 5s closes are annualized using BTC's 24/7 trading calendar:

```
periods_per_year = 365 * 24 * 3600 / 5.0
sigma_annual = stdev(log_returns) * sqrt(periods_per_year)
```

The vol buffer persists across session boundaries (BTC volatility doesn't reset on a 15-minute clock), giving the engine instant readiness at session open.

### 3.3 Strike Calibration

Kalshi settles contracts against CF Benchmarks' Bitcoin Real-Time Index (BRTI) — a composite of 60 price sources averaged over the final 60 seconds. The engine uses Coinbase spot as its BTC feed, which can diverge from BRTI by ~$50-100.

At session open, the engine calibrates the strike by inverting the Brownian Bridge formula using the Kalshi contract's opening mid-price:

```
d = inverse_CDF(mid / 100)
K = S / exp(d * sigma * sqrt(T))
```

Where S is Coinbase spot and mid is the Kalshi opening mid. This aligns the probability engine's output with the market's implied pricing.

### 3.4 Indicators Suite

A parallel set of 5-second indicators runs alongside the probability engine:

| Indicator | Parameters | Purpose |
|---|---|---|
| Bollinger Bands | 20-period, 2 sigma | Overbought/oversold detection for tick confirmation |
| MACD | 12/26/9 | Momentum direction and histogram strength |
| MA50 / MA100 | Simple moving averages | Trend support/resistance levels |
| Tick Velocity | Rolling 10-tick window | BTC price change rate ($/second) |

These feed the entry confirmation gate: the FVG signal identifies WHAT to trade, tick data confirms WHEN (momentum moving with the position before committing capital).

---

## 4. FVG Baseline System

### 4.1 Baseline Construction

During the first 90 seconds of each 15-minute session, the engine collects all Kalshi mid-price readings. At 90 seconds (or when at least 5 samples exist), the arithmetic mean becomes the session baseline:

```
baseline = mean(mid_prices[0:90s])
```

This baseline represents the market's initial consensus pricing before directional moves develop.

### 4.2 Fair Value Gap (FVG)

The FVG is the divergence between the probability engine's computed fair value and the session baseline:

```
FVG = fair_value - baseline
```

A positive FVG means the prob engine thinks YES is underpriced relative to where the session started. A negative FVG means NO is underpriced. The magnitude indicates conviction strength.

### 4.3 Time-Weighted Entry Thresholds

The FVG must exceed a threshold that increases with session age:

| Session Age | Threshold | Rationale |
|---|---|---|
| 0-3 minutes | 5c | Early divergence — BTC has time to continue moving |
| 3-7 minutes | 8c | Standard — moderate conviction required |
| 7-15 minutes | 12c | Late entry — needs strong divergence to justify remaining time premium |

### 4.4 Tick Confirmation

When FVG crosses the threshold, the engine waits for tick-level momentum to confirm before entering:

- **YES entry**: Tick velocity > $2/s rising, OR BTC at/below BB mid (oversold bounce)
- **NO entry**: Tick velocity < -$2/s falling, OR BTC at/above BB mid (overbought rejection)
- **Fallback**: If 5s data is unavailable, enter immediately (don't block on missing data)

---

## 5. Signal Cascade

### 5.1 Tier Architecture

The engine evaluates signal tiers in descending priority. Each tier represents a different data source and conviction level. Currently, only TA_FORCED (the FVG baseline system) is active:

| Tier | Status | Trigger | Historical WR |
|---|---|---|---|
| PRIMARY | Disabled | Polymarket smart flow aligns with Kalshi mid | 58% (wallet override reduced accuracy) |
| TREND_FOLLOW | Disabled | 95%+ wallet consensus, 3+ wallets | N/A (rarely triggered) |
| MIMIC | Disabled | Single elite wallet activity | 44% (-$13.07 lifetime) |
| ALGO | Disabled | High-volume algo bot detection | N/A (architecture change) |
| **TA_FORCED** | **Active** | FVG baseline threshold crossing | **55.6% (471 Kalshi sessions)** |

### 5.2 Entry Guards

Before any signal can execute, it must pass:

1. **Daily loss limit**: Cumulative daily P&L > -$15.00
2. **Window lock**: No existing position closed this window (unless re-entry conditions met)
3. **Open position check**: No active position
4. **Trade cap**: <99 fills this window
5. **Re-entry gate**: Same direction as first entry, prob has 5c+ edge, 5+ min remaining

---

## 6. Entry Mechanics

### 6.1 Price Band

All entries must fall within 40-75c. This range is derived from 471 sessions of Kalshi settlement data:

- **Below 40c**: 33% directional accuracy — worse than a coin flip
- **40-64c**: 52-58% accuracy — the profitable core
- **65-75c**: 63% accuracy — high conviction entries
- **Above 75c**: Insufficient sample size for reliable inference

### 6.2 Sizing

Contract count scales with the probability engine's mispricing (edge):

| Edge | Contracts | Rationale |
|---|---|---|
| < 10c | 4 | Small edge — minimum viable position |
| 10-14c | 6 | Moderate edge — confident entry |
| 15c+ | 8 | Strong edge — max comfortable position |

Hard cap: 10 contracts. Data shows 13+ contract positions have 48% directional accuracy and near-zero average P&L.

### 6.3 Order Execution

The engine enters at market (take the ask) within the price band. The FVG threshold crossing is the signal — passive bidding below fair value caused 60%+ miss rates in historical testing. The price band (40-75c) provides sufficient discipline without price-level optimization.

Orders are placed via the Kalshi REST API with RSA-PSS authentication. If the order doesn't fill within 1 second (ask moved), it's cancelled and the signal re-evaluates on the next cycle.

---

## 7. Position Management

### 7.1 Take Profit (TP)

On fill, the engine places a resting limit sell order at the probability engine's current fair value:

```
TP_price = fair_value (for YES) or 100 - fair_value (for NO)
```

If fair value is 65c and we entered YES at 50c, TP rests at 65c (+15c profit).

### 7.2 Dynamic TP Ratcheting

Every 10 seconds, the engine recomputes fair value. If the new fair value exceeds the current TP by 2c+, the TP order is cancelled and re-placed higher. TPs only ratchet UP (never down), locking in higher potential profit as the position runs.

### 7.3 Sweep Sell

When a TP partially fills (e.g., 8 of 10 contracts fill), the engine immediately:

1. Cancels all remaining TP orders
2. Queries Kalshi for the actual remaining position (handles fractional artifacts)
3. Market sells all remaining contracts at the current bid

This prevents orphan contracts that would otherwise be caught by the profit trail at worse prices.

### 7.4 DCA (Dollar Cost Averaging)

The DCA system allows scaling into a position if:

- BB momentum confirms direction
- Prob engine still agrees with the trade side
- Total exposure stays under 15% of the entry balance
- Maximum 3 DCA tiers

Each DCA tier adds 1.5x the original position size at a 2.5c lower price. DCA is conservative — it requires BB momentum and prob edge confirmation before adding.

---

## 8. Exit Mechanics

### 8.1 Settlement (Primary Exit)

The dominant exit path. 15-minute binary contracts settle at $1.00 (correct) or $0.00 (incorrect). With 55.6% directional accuracy and entries in the 40-75c band, expected value is positive on settlement:

```
EV = 0.556 * (100 - entry) - 0.444 * entry
```

At 55c entry: EV = 0.556 * 45 - 0.444 * 55 = 25.0 - 24.4 = +0.6c per contract.

### 8.2 Take Profit Fill

Resting limit orders fill when the contract reaches the target price. TP exits capture profit before settlement, avoiding the risk of a late reversal. The dynamic ratcheting ensures TPs stay aligned with current fair value.

### 8.3 Thesis Invalidation Exit

Fires when the FVG that justified entry has **fully reversed** (flipped sign) AND the position is 10c+ underwater:

```
thesis_dead = (fvg_for_our_side < 0) AND (bid < entry - 10)
```

This only cuts positions where the original directional thesis is completely gone — not positions experiencing normal oscillation.

### 8.4 Reversal Exit (Soft Only)

Tracks the probability shift from entry. When 60%+ of the original edge has evaporated AND the position is still profitable, it exits to lock in gains:

```
edge_at_entry = |entry_prob - 0.50|
edge_lost = |current_prob - entry_prob|
reversal_fraction = edge_lost / edge_at_entry

if reversal_fraction >= 0.60 AND bid > entry:
    market sell (lock in profit before reversal completes)
```

Hard reversal (cutting losses when prob crosses 50%) is disabled. At 55.6% accuracy, cutting losses also cuts positions that would have recovered at settlement.

### 8.5 Profit Trail

Arms when the position gains +10c from entry. Fires when the bid drops 8c from the high-water mark, provided the BTC thesis has broken:

```
arm: hwm - entry >= 10c
fire: bid <= hwm - 8c AND bid > entry AND btc_against_us
```

The BTC thesis gate checks the probability engine — the trail only fires when BTC is fundamentally moving against the position (prob shifted past 40% for YES, or past 60% for NO). Contract price noise alone won't trigger it.

### 8.6 Mandatory 3-Minute Exit

Safety exit: with 3 minutes remaining, if a position is still open and no TP has filled, it's market sold. This prevents holding into the final settlement window where liquidity dries up.

---

## 9. Regime Classification

The regime classifier categorizes each session based on BTC's recent behavior:

| Regime | Condition | Impact |
|---|---|---|
| TRENDING | Vol < 23%, trend_score >= 0.4 | BTC moving decisively — larger FVG signals expected |
| MEAN_REVERTING | Vol < 46%, trend_score < 0.4 | BTC oscillating — FVG signals may fade before settlement |
| EXPLOSIVE | Vol >= 46% | High volatility — rapid moves, uncertain direction |

Trend score is derived from 5-second MACD histogram strength, MA50 vs MA100 divergence, and Bollinger Band width compression.

The regime is computed at each window change using data from the prior session and logged for monitoring, but does not currently gate entry decisions.

---

## 10. Smart Wallet Flow (Polymarket)

### 10.1 Wallet Scoring

Hourly, the engine queries recently settled Polymarket BTC 15-minute markets and computes per-wallet performance:

- **Elite tier** (top 15): Composite score weighting PnL efficiency, win rate, hold rate, and sample size. Wallets with 100% WR are penalized (late-entry gaming).
- **Algo tier** (top 30): Volume-weighted WR above 65%, minimum 30 trades across 10+ markets.
- **Blacklisted**: 4 wallets with confirmed -EV patterns, permanently excluded.

### 10.2 Real-Time Flow

Every 0.9 seconds, the engine polls Polymarket for new trades on the active BTC 15-minute market. For each smart wallet trade:

```
weighted_volume = trade_size * wallet_win_rate
```

The conviction metric is the fraction of weighted volume on the majority side:

```
flow_conviction = max(up_volume, down_volume) / total_volume
```

Flow data is published to the signal cascade but currently only used for logging (wallet-based tiers are disabled due to accuracy degradation when wallet signals override contract mid direction).

---

## 11. Whale Monitor (Mempool.space)

The whale monitor subscribes to mempool.space's WebSocket API, tracking 14 known exchange addresses across 6 exchanges (Binance, Coinbase, Kraken, Bitfinex, Gemini, OKX).

- **Inflow** (BTC sent TO exchange): Bearish signal — likely preparation to sell
- **Outflow** (BTC sent FROM exchange): Bullish signal — removing from exchange (holding)
- **Threshold**: Only transfers >= 5 BTC register as whale activity

The whale bias (-1.0 to +1.0) is available to the signal cascade but currently serves as advisory data for the TA inversion logic: if whale flow strongly opposes the TA direction, the engine may flip the trade.

---

## 12. Settlement Reconciliation

Every 5 minutes, the engine queries the Kalshi `/portfolio/settlements` API and cross-references against its internal trade log:

1. Match settled tickers against `kalshi_trades` DB entries
2. Compute actual P&L from settlement revenue vs. entry cost
3. Correct any discrepancies (the internal log estimates P&L from fill prices, which may differ from settlement accounting)
4. Update the `balance_snapshots` table with authoritative balance data

This ensures the engine's performance tracking reflects actual Kalshi account activity, not estimated fill prices.

---

## 13. Monitoring

### 13.1 Dashboard JSON State

Every 2 seconds, the engine writes a JSON snapshot of all decision factors to `data/dashboard_state.json`:

- Session: ticker, time remaining, baseline, regime
- Probability engine: fair value, probability, mispricing, volatility, strike
- FVG: divergence vs baseline, threshold, crossed status
- Tape: contract mid, taker imbalance, VWAP
- BTC: spot price, session open, distance from strike
- Position: side, count, entry, TP, HWM, MAE, unrealized P&L, reversal %
- Account: balance, daily P&L

### 13.2 Terminal Monitor

A standalone Python script (`monitor.py`) reads the JSON state file, tails the engine log, and queries the trade database to render a live terminal dashboard. It includes visual gauges for probability, FVG divergence, regime, and contract mid, plus a decision log showing recent engine actions.

The monitor fetches the live Kalshi balance directly via API every 30 seconds, independent of the engine's cached balance.

---

## 14. Risk Controls

| Control | Threshold | Action |
|---|---|---|
| Daily loss limit | -$15.00 | Halt all trading for remainder of UTC day |
| Position size cap | 10 contracts | Hard maximum regardless of edge |
| Entry price band | 40-75c | Reject entries outside proven profitable range |
| Mandatory exit | 3 min remaining | Market sell any open position |
| Same-direction re-entry | Must match first entry side | Prevent mid-session direction flips |
| Window lock | After position close | One trade cycle per session (re-entry requires prob edge) |

---

## 15. Performance Characteristics

Based on 471 sessions of verified Kalshi settlement data:

- **Directional accuracy**: 55.6% (first trade per session)
- **Profitable entry band**: 50-64c (58% WR, largest sample)
- **Profitable sizing**: 4-12 contracts (+$83.60 avg per session)
- **Unprofitable sizing**: 13+ contracts (48% WR, +$7.51 avg)
- **Single vs multi-trade**: Single entry sessions average +$9.13; multi-trade sessions average +$56-156 (re-entry captures continuation)

The engine's edge is narrow (~5.6% above coin flip) but positive. Profitability depends on maintaining entry discipline (40-75c band), controlled sizing (4-10 contracts), and minimal interference with settlement outcomes (thesis/reversal exits only fire on confirmed collapses, not noise).
