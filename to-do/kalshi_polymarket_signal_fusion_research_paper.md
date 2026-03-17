# Kalshi 15m Limit-Order Arbitrage Research Paper
## With Polymarket 5m BTC Parsing for Reactionary Momentum Arbitration

**Prepared for:** Claude  
**Authoring mode:** Implementation-facing markdown spec  
**Date:** 2026-03-16  
**Scope:** Configure a BTC-linked prediction-market trading engine that uses **Polymarket 5-minute Bitcoin direction markets** as an external signal layer to improve **Kalshi 15-minute execution**.

---

## 1. Objective

Build a trading algorithm that:

1. **Monitors Kalshi 15-minute BTC reactionary momentum markets** as the execution venue.
2. **Parses Polymarket 5-minute BTC Up/Down markets** as an external, faster-moving information source.
3. Uses the Polymarket stream to improve:
   - direction confidence,
   - entry timing,
   - stale-move filtering,
   - momentum continuation vs exhaustion classification,
   - cancellation / reprice decisions on Kalshi.
4. Places **maker-biased Kalshi limit orders** only when the combined evidence shows positive expected value after spread, fees, queue risk, and latency risk.

This is **not** a “copy Polymarket into Kalshi” strategy. It is a **cross-venue signal fusion** strategy:

- **Polymarket 5m** = short-horizon sentiment + order-flow proxy.
- **Kalshi 15m** = target venue for execution, queue capture, and monetization.

---

## 2. Research Thesis

### 2.1 Core hypothesis

Polymarket’s 5-minute BTC markets reprice faster than Kalshi’s 15-minute BTC reactionary markets during short bursts of directional conviction. If the engine can detect:

- a rapid Polymarket odds shift,
- supporting order-book imbalance or trade flow,
- and a Kalshi book that has **not fully repriced yet**,

then the engine can rest passive Kalshi limit orders at prices that are favorable relative to the probability implied by the faster venue.

### 2.2 Why this may work

Short-duration prediction markets frequently exhibit:

- fragmented attention,
- asynchronous repricing,
- stale passive quotes,
- differing participant mixes,
- and different update speeds between venues.

Polymarket’s 5-minute BTC markets are unusually information-dense because they encode **near-immediate directional belief** over a very short horizon. Kalshi’s 15-minute markets can then be treated as a slower, wider reaction surface where that belief may diffuse with a lag.

### 2.3 What the strategy is *actually* exploiting

The edge, if present, comes from **microstructure lag and conditional repricing**, not from predicting Bitcoin direction in a vacuum.

The question is not:

> “Will BTC go up?”

It is:

> “Has Polymarket already priced in information that Kalshi has not yet fully internalized, and can we get paid for posting into that gap?”

---

## 3. Exchange Facts the Engine Must Respect

### 3.1 Kalshi market-data model

Kalshi’s order book exposes bid ladders for **YES** and **NO**. The response is wrapped in `orderbook_fp`, containing arrays like `yes_dollars` and `no_dollars`, where each entry is `[price_dollars, count_fp]`. Kalshi’s WebSocket API is intended for real-time order book changes, trade executions, market status, and fills. Order groups can impose a contracts limit over a rolling 15-second window and automatically halt/cancel grouped orders when hit. Create-order requests are submitted through the REST trading API.  

### 3.2 Polymarket market-data model

Polymarket provides a public market-data WebSocket channel for level-2 order book, price, trade, and market-event updates. Market discovery can be done by slug, tag, or events endpoints. The 5-minute BTC “Up or Down” markets expose a **Price to Beat**, current BTC price, and live Up/Down probabilities, and the sample market page states resolution is based on whether the ending BTC price is greater than or equal to the opening reference price using the Chainlink BTC/USD data stream.

### 3.3 Consequence for system design

Therefore:

- **Polymarket must be parsed continuously**, preferably over WebSocket.
- **Kalshi must be maintained as a live local order book**, preferably over WebSocket rather than REST polling.
- The engine should maintain a common probability space so that Polymarket 5-minute odds can influence Kalshi 15-minute fair value estimation.

---

## 4. Strategy Concept: 5m-to-15m Signal Fusion

### 4.1 Venue roles

#### Polymarket 5m BTC
Use as:
- a **faster sentiment meter**,
- a **trade-flow intensity proxy**,
- a **near-term directional bias source**,
- a **momentum acceleration detector**,
- a **reversal / exhaustion detector**.

#### Kalshi 15m BTC
Use as:
- the **execution venue**,
- the **inventory and queue management environment**,
- the venue where mispriced passive quotes may still rest,
- the venue where the algorithm monetizes lagged repricing.

### 4.2 Time-horizon translation

A 5-minute market does **not** map 1:1 into a 15-minute market.

Instead, use it as one of several conditional predictors.

#### Interpretation examples

- If Polymarket 5m Up moves from 0.49 to 0.61 quickly, that does **not** imply Kalshi 15m fair value is 0.61.
- It may imply:
  - short-term bullish shock,
  - elevated likelihood of continued repricing for the next 1–4 minutes,
  - increased probability that Kalshi’s 15-minute contract should drift upward,
  - or increased danger of buying too late if Kalshi has already overreacted.

So the translation layer must estimate:

`Kalshi_15m_fair_value = f(Kalshi_book_state, BTC_spot_context, Polymarket_5m_state, time_to_expiry, local_momentum_regime)`

---

## 5. Required Architecture

```text
polymarket_discovery.py
polymarket_stream.py
polymarket_features.py
kalshi_market_data.py
kalshi_execution.py
signal_fusion.py
risk.py
position_manager.py
analytics.py
config.yaml
main.py
```

### 5.1 Module responsibilities

#### `polymarket_discovery.py`
- discover the active BTC 5-minute market(s),
- map slug / event / asset IDs,
- roll forward to the next active window,
- gracefully retire expired windows.

#### `polymarket_stream.py`
- subscribe to the Polymarket market WebSocket,
- maintain top-of-book and recent trade state,
- normalize all updates into a common internal schema,
- detect feed gaps and reconnect.

#### `polymarket_features.py`
Compute live features such as:
- current implied probability,
- 1s / 3s / 10s change in implied probability,
- velocity of odds change,
- acceleration of odds change,
- top-of-book imbalance,
- trade aggressor bias if inferable,
- distance between implied probability and live BTC move,
- end-of-window decay effects,
- confidence regime of the 5-minute market.

#### `kalshi_market_data.py`
- maintain local book for target 15-minute contract,
- compute bid/ask, spread, depth, imbalance, microprice,
- track book age and sequence integrity,
- expose fair-value inputs to the fusion layer.

#### `signal_fusion.py`
- combine Polymarket features with Kalshi state,
- estimate Kalshi fair value,
- generate entry / cancel / reprice / no-trade decisions,
- suppress false positives when the move is already exhausted.

#### `kalshi_execution.py`
- create, amend, cancel, and monitor Kalshi orders,
- enforce maker-only logic when possible,
- cap reprices,
- manage order groups,
- avoid crossing when edge is thin.

#### `risk.py`
- enforce inventory caps,
- expiry-sensitive risk tightening,
- kill-switch logic,
- stale-data rejection,
- drawdown and error-rate throttles.

#### `analytics.py`
- persist every decision,
- record pre-trade state, fill quality, post-fill drift,
- support research loops and threshold calibration.

---

## 6. Polymarket Parsing Specification

### 6.1 Discovery

Claude should implement active-market discovery using **slug or events-based discovery**. The goal is to continuously identify the **currently live BTC Up/Down 5-minute contract** and prepare the next one before the current window expires.

### 6.2 Internal normalized schema

```python
@dataclass
class Poly5mState:
    event_id: str
    market_slug: str
    asset_id_up: str
    asset_id_down: str
    window_start_ts: float
    window_end_ts: float
    price_to_beat: float
    live_btc_price: float | None
    up_bid: float | None
    up_ask: float | None
    down_bid: float | None
    down_ask: float | None
    up_mid: float | None
    down_mid: float | None
    up_last: float | None
    down_last: float | None
    up_top_qty: float | None
    down_top_qty: float | None
    last_trade_ts: float | None
    feed_ts: float
    book_age_ms: float
```
```

### 6.3 Derived Polymarket features

Claude must compute at least the following features:

#### Price / probability features
- `poly_up_mid`
- `poly_down_mid`
- `poly_mid_spread`
- `poly_up_bid_ask_spread`
- `poly_prob_change_1s`
- `poly_prob_change_3s`
- `poly_prob_change_10s`
- `poly_prob_velocity`
- `poly_prob_acceleration`

#### Book-shape features
- `poly_top_imbalance = (up_top_qty - down_top_qty) / (up_top_qty + down_top_qty)`
- `poly_depth_near_touch`
- `poly_touch_stability`
- `poly_quote_flip_rate`

#### Market-regime features
- `poly_seconds_to_expiry`
- `poly_is_endgame` (e.g. last 45s)
- `poly_confidence_score`
- `poly_noise_score`
- `poly_feed_health_score`

#### BTC alignment features
- `btc_move_since_window_start`
- `poly_vs_spot_divergence`
- `poly_reacts_before_spot` flag if observed
- `spot_confirms_poly` flag

### 6.4 Special handling near expiry

Polymarket 5-minute markets become structurally unstable near resolution.

Claude must apply stricter treatment during the final segment of the 5-minute window:

- downweight the signal in the last **30–45 seconds**,
- reject signals if quote flip-rate explodes,
- reject if spread widens too much,
- reject if top-level quantity collapses,
- reject if signal change is entirely a final-seconds panic repricing.

This is critical. Near-expiry Polymarket motion can be informative, but it can also be a trap.

---

## 7. Kalshi 15m Execution Specification

### 7.1 Local book features

Claude must maintain at least:

- best YES bid / ask,
- best NO bid / ask,
- spread,
- depth within 1c / 2c / 3c,
- top-of-book imbalance,
- microprice,
- quote age,
- last trade time,
- minutes to expiry,
- own queue position estimate if available.

### 7.2 Entry philosophy

Only enter on Kalshi when:

1. Polymarket gives a valid directional or continuation signal.
2. Kalshi is not already fully repriced.
3. The Kalshi book is not stale.
4. Opposite-side displayed liquidity is real enough to matter.
5. Net edge exceeds all configured thresholds.
6. Inventory and order-group caps permit entry.

### 7.3 Maker-first rule

Default behavior should be:

- post a passive limit order,
- avoid aggressive crossing unless a special emergency rule is enabled,
- cancel or reprice only within strict bounds,
- never chase endlessly.

---

## 8. Signal Fusion Logic

### 8.1 Conceptual formula

Claude should estimate a fused Kalshi fair value using weighted components:

```text
kalshi_fair =
    w1 * kalshi_microprice
  + w2 * kalshi_mid
  + w3 * translated_poly_signal
  + w4 * btc_spot_context
  + w5 * regime_adjustment
```

Where `translated_poly_signal` is **not raw Polymarket price**. It is a transformed signal that accounts for:

- signal recency,
- time remaining in both venues,
- signal confidence,
- endgame instability,
- whether Kalshi has already repriced,
- whether spot BTC confirms the move.

### 8.2 Translation layer

Example transformation:

```text
translated_poly_signal =
    poly_up_mid
    + alpha * poly_prob_velocity
    + beta  * poly_top_imbalance
    - gamma * poly_noise_score
    - delta * poly_endgame_penalty
```

Then compress and cap the result before mapping it into a 15-minute fair-value shift.

### 8.3 Required anti-overreaction rules

Claude must reject entries if any of the following are true:

- Kalshi already moved more than the translated Polymarket shift implies.
- Polymarket moved, but on tiny size / poor liquidity.
- Polymarket moved only in the final seconds of the 5-minute window.
- BTC spot diverges sharply against the signal.
- Kalshi spread widened so much that any signal edge is consumed by execution friction.
- The move appears to be a terminal spike rather than continuation.

---

## 9. Concrete Entry Rules

### 9.1 Example long-YES entry on Kalshi

Enter only if all are true:

- `poly_prob_change_3s >= +0.03`
- `poly_prob_velocity > configured_threshold`
- `poly_top_imbalance > +0.20`
- `poly_noise_score < max_noise`
- `poly_seconds_to_expiry > 45`
- `kalshi_book_age_ms <= 1000–1500`
- `kalshi_spread <= max_spread`
- `kalshi_depth_opposite >= min_depth`
- `translated_poly_signal - kalshi_yes_ask >= min_gross_edge`
- `net_edge_after_fees >= min_net_edge`
- `kalshi minutes_to_expiry >= minimum_safe_minutes`
- inventory limits not breached
- order-group limit not breached

Then:

- post a YES buy limit at the best price that preserves edge,
- record all state variables,
- start timeout and reprice logic.

### 9.2 Example short / NO-side entry

Mirror the above symmetrically.

---

## 10. Reprice and Cancel Logic

### 10.1 Cancel conditions

Claude must cancel a working Kalshi order if:

- Polymarket reverses materially,
- Polymarket feed becomes stale,
- Kalshi book becomes stale,
- net edge decays below threshold,
- queue becomes toxic,
- the market is too close to expiry,
- there is excessive flip-flopping in either venue.

### 10.2 Reprice policy

Rules:

- maximum reprices per order: **3**,
- minimum time between reprices: configurable,
- only reprice if edge still survives,
- each reprice should be incremental,
- abandon order when repricing would destroy maker economics.

### 10.3 No infinite churn

Claude must never create a cancel-replace loop without a hard cap.

---

## 11. Risk Controls

### 11.1 Hard controls

- max contracts per order,
- max gross open exposure,
- max exposure per side,
- max exposure per market,
- max reprices per order,
- max order lifetime,
- max stale-book age,
- max stale-signal age,
- max drawdown before pause,
- max consecutive toxic fills before pause,
- max error rate before pause.

### 11.2 Order-group controls on Kalshi

Claude should use Kalshi order groups to constrain bursts of matched contracts over the rolling 15-second window where appropriate.

### 11.3 Endgame tightening

As either market nears expiry, Claude must:

- raise minimum edge requirements,
- reduce order size,
- shorten order timeout,
- increase skepticism of cross-venue signals,
- disable entry entirely inside the final safety band if configured.

---

## 12. Analytics Requirements

Claude must log every significant event.

### 12.1 Decision log schema

For each evaluation:

- timestamp,
- Kalshi market ticker,
- Polymarket market slug,
- side,
- Kalshi yes/no book state,
- Kalshi spread,
- Kalshi imbalance,
- Kalshi microprice,
- Kalshi book age,
- Polymarket up/down prices,
- Polymarket spread,
- Polymarket imbalance,
- Polymarket probability change windows,
- translated signal,
- gross edge,
- net edge,
- rejection reason or action taken.

### 12.2 Fill-quality diagnostics

For each fill, record:

- fill price,
- decision-time fair value,
- fill-time fair value,
- post-fill Kalshi drift at +1s, +3s, +10s,
- Polymarket drift after fill,
- BTC spot drift after fill,
- whether the fill was favorable or toxic.

### 12.3 Research questions

The first live-research objective is not profit maximization. It is answering:

1. Do Polymarket 5m changes predict Kalshi 15m repricing at all?
2. Which Polymarket features matter most?
3. Are fills followed by favorable drift or adverse selection?
4. Does the signal degrade near either market’s expiry?
5. What thresholds create robust net edge after friction?

---

## 13. Configuration Schema

Claude should implement a YAML config resembling:

```yaml
venues:
  kalshi:
    use_websocket: true
    max_book_age_ms: 1500
    max_spread_cents: 3
    min_depth_contracts: 10
    max_reprices_per_order: 3
    order_timeout_seconds: 30
    use_order_groups: true
    order_group_contracts_limit: 20

  polymarket:
    use_websocket: true
    discovery_mode: slug_or_events
    stale_signal_ms: 2000
    endgame_seconds: 45
    min_top_qty: 50
    max_spread_cents: 6

fusion:
  enabled: true
  w_kalshi_microprice: 0.35
  w_kalshi_mid: 0.15
  w_poly_signal: 0.35
  w_btc_spot: 0.10
  w_regime: 0.05
  min_poly_prob_change_3s: 0.03
  min_poly_imbalance: 0.20
  max_poly_noise: 0.60
  compress_signal: true
  max_signal_shift_cents: 8

risk:
  max_contracts_per_order: 5
  max_position_contracts: 20
  max_market_exposure_dollars: 500
  tighten_inventory_minutes: 5
  disable_entry_final_minutes: 2.5
  pause_after_consecutive_toxic_fills: 4
  pause_after_drawdown_dollars: 50

analytics:
  log_all_decisions: true
  log_post_fill_drift_seconds: [1, 3, 10]
  persist_poly_features: true
  persist_spot_features: true
```
```

---

## 14. Pseudocode

```python
def evaluate_kalshi_entry(kalshi_state, poly_state, btc_state, config):
    if kalshi_state.book_age_ms > config.kalshi.max_book_age_ms:
        return Reject("stale_kalshi_book")

    if poly_state.book_age_ms > config.polymarket.stale_signal_ms:
        return Reject("stale_poly_signal")

    if poly_state.seconds_to_expiry <= config.polymarket.endgame_seconds:
        return Reject("poly_endgame")

    poly_features = compute_poly_features(poly_state, btc_state)
    kalshi_features = compute_kalshi_features(kalshi_state)

    if poly_features.noise_score > config.fusion.max_poly_noise:
        return Reject("poly_noise_too_high")

    translated = translate_poly_to_kalshi(poly_features, kalshi_features, btc_state, config)
    fair_value = fuse_fair_value(kalshi_features, translated, btc_state, config)

    gross_edge = fair_value - kalshi_state.best_yes_ask
    net_edge = gross_edge - estimate_total_friction(kalshi_state, poly_features)

    if net_edge < required_net_edge(kalshi_state, poly_state, config):
        return Reject("edge_too_small")

    if kalshi_already_repriced(kalshi_state, translated):
        return Reject("already_repriced")

    if not depth_is_sufficient(kalshi_state, side="YES", config=config):
        return Reject("insufficient_depth")

    return PlacePassiveLimitOrder(side="YES", price=choose_limit_price(...))
```

---

## 15. Implementation Order for Claude

Claude should implement in this order:

### Phase A — Data plumbing
1. Kalshi WebSocket local book.
2. Polymarket discovery and market WebSocket.
3. Unified timestamping and health checks.
4. Durable decision/event logging.

### Phase B — Feature extraction
5. Polymarket odds change windows.
6. Polymarket imbalance / spread / noise metrics.
7. Kalshi microprice / imbalance / depth metrics.
8. BTC spot alignment metrics.

### Phase C — Safe execution
9. Maker-first entry logic.
10. Stale-data gates.
11. Reprice / cancel limits.
12. Inventory and order-group controls.

### Phase D — Fusion research
13. Translate 5m Polymarket signal into 15m Kalshi fair-value adjustment.
14. Backtest / paper trade threshold calibration.
15. Measure post-fill toxicity and continuation rates.
16. Enable live trading only after evidence of durable positive expectancy.

---

## 16. Explicit Instructions to Claude

Claude, implement this as a **research-grade trading engine**, not a hype script.

Your priorities are:

1. **Correctness of market-data handling**
2. **Deterministic logging and debuggability**
3. **Safety gates before speed**
4. **Maker-biased execution**
5. **Evidence collection before optimization**

You must:

- prefer WebSocket market data for both venues,
- maintain local normalized state objects,
- reject stale or noisy external signals,
- never map Polymarket price directly to Kalshi fair value without a translation layer,
- log every rejection reason,
- cap reprices,
- use order groups when needed,
- tighten risk near expiry,
- and treat post-fill drift analysis as mandatory.

You must **not**:

- blindly follow Polymarket prints,
- chase moves that are already exhausted,
- trade from stale books,
- allow unbounded cancel-replace churn,
- or assume that a 5-minute market cleanly predicts a 15-minute market without calibration.

The purpose of this system is to discover whether **Polymarket 5m microstructure can improve Kalshi 15m execution quality**. Build it so the answer can be measured.

---

## 17. Suggested Next Repo Artifacts

After this markdown, Claude should generate:

1. `config.yaml`
2. `models.py`
3. `polymarket_discovery.py`
4. `polymarket_stream.py`
5. `polymarket_features.py`
6. `kalshi_market_data.py`
7. `signal_fusion.py`
8. `kalshi_execution.py`
9. `risk.py`
10. `analytics.py`
11. `main.py`

---

## 18. Source Notes

The system design above is grounded in the current public exchange documentation and market pages:

- Kalshi orderbook responses: YES/NO bid arrays in `orderbook_fp`
- Kalshi WebSocket support for order book changes, trade executions, market status, and fills
- Kalshi order groups with rolling 15-second contracts limits
- Kalshi order creation via REST trading endpoint
- Polymarket market WebSocket for L2 order book, prices, trades, and market events
- Polymarket market discovery by slug / tags / events
- Polymarket 5-minute BTC market structure, including “Price to Beat” and Chainlink BTC/USD-based resolution on the cited market page

These source facts should be revisited during implementation in case endpoint details change.
