# Sniper Signal Layer -- Strategy & Implementation Guide

**Target system**: `sniper.py` signal upgrade for KXBTC15M session-open momentum capture
**Upstream data**: Binance kline WS (`price_feed.py`), Kalshi WS orderbook/trade flow (`kalshi_ws.py`)
**Execution layer**: Existing sniper state machine (PROBING -> DEPLOYING -> RESTING -> FILLED)

---

## 1. Executive Overview

The sniper targets the first seconds after a KXBTC15M contract activates. The contract resolves YES if BTC closes the 15-minute window at or above its open price. At the moment of activation, the market has not yet priced the direction -- there is a brief window where BTC momentum from the prior seconds predicts which side gets immediate follow-through.

The system's job is to answer one question before the contract goes live: **does BTC have enough directional momentum right now that one side will get bought aggressively in the first 2-3 seconds?**

Design principles:

- **Selective participation**. Most sessions have no clear pre-open momentum. Forcing a trade every session destroys edge because you are buying into noise at 50/50 odds minus the spread. The system skips weak sessions entirely.
- **Speed once conditions align**. When the signal is strong, the constraint is latency to fill, not analysis depth. Pre-compute direction and size during T-30s to T-3s. At activation, validate and fire.
- **The prediction target is not "where will BTC be in 15 minutes."** It is "which side gets immediate follow-through in the first seconds after activation." A 5-cent move in the contract mid is the target. BTC can reverse later -- we will already be out at TP.

---

## 2. Signal Design

The signal layer produces two outputs: a **direction score** (-1.0 to +1.0) and a **confidence score** (0.0 to 1.0). Direction says which side. Confidence says whether to trade at all.

Five components feed the scoring system. Each produces a raw sub-score that gets weighted and summed.

### A. Short-Horizon BTC Momentum

**What to measure**: BTC/USDT price change over three lookback windows from the Binance kline feed.

| Window | Calculation | Weight | Rationale |
|--------|-------------|--------|-----------|
| 60s | `(price_now - price_60s_ago) / price_60s_ago` | 0.20 | Establishes the trend context. A move that has been building for 60s has more inertia than a spike. |
| 15s | `(price_now - price_15s_ago) / price_15s_ago` | 0.35 | Primary signal window. Most predictive of immediate continuation. |
| 5s | `(price_now - price_5s_ago) / price_5s_ago` | 0.25 | Captures whether momentum is fresh and accelerating into the open. |

**How to interpret**: Normalize each return to a -1.0 to +1.0 range using empirical scaling factors. A 15s move of +0.05% in BTC is a moderate signal; +0.10% is strong. Scale linearly and clamp at +/-1.0.

**Scaling (suggested starting points)**:
- 60s: divide by 0.0015 (0.15% = max score)
- 15s: divide by 0.0008 (0.08% = max score)
- 5s: divide by 0.0004 (0.04% = max score)

**Scoring contribution**: Weighted sum of the three normalized returns. Positive = YES bias, negative = NO bias. Combined weight in the final score: **0.40** (sum of 0.20 + 0.35 + 0.25, rescaled so all five components sum to 1.0 after global normalization).

**Data source**: `BTCTickTracker` in `price_feed.py` provides `last_price` and tick history. For lookback returns, sample `PriceFeedTask.state.buffers["1m"]` for 60s context and maintain a dedicated ring buffer of (timestamp, price) tuples captured from the aggTrade stream at ~100ms resolution.

### B. Acceleration

**What to measure**: Is the move speeding up or fading? Compare the rate of change over the most recent 5s against the rate of change over the preceding 10s.

```
velocity_recent = (price_now - price_5s_ago) / 5.0
velocity_prior  = (price_5s_ago - price_15s_ago) / 10.0
acceleration    = velocity_recent - velocity_prior
```

**How to interpret**:
- `acceleration > 0` with positive momentum: move is accelerating (bullish continuation likely)
- `acceleration < 0` with positive momentum: move is fading (bullish conviction drops)
- `acceleration > 0` with negative momentum: bounce forming (bearish conviction drops)
- `acceleration < 0` with negative momentum: move is accelerating down (bearish continuation likely)

**Scoring contribution**: Normalize acceleration by dividing by the absolute value of `velocity_prior` (or a floor of 0.01 $/s to avoid division by zero). Clamp to -1.0 to +1.0. Sign must agree with momentum direction -- if momentum is positive and acceleration is negative, the acceleration score is negative (penalizes the signal). Weight: **0.15**.

### C. Order Book Imbalance

**What to measure**: Kalshi WS bid/ask depth from `LocalOrderBook` in `kalshi_ws.py`, plus trade flow from `TradeFlowTracker`.

Two sub-components:

**C1. Depth imbalance** -- Compare total resting quantity within 5 cents of the mid on each side.

```
yes_depth = sum(qty for price, qty in orderbook.yes_bids.items() if price >= mid - 5)
no_depth  = sum(qty for price, qty in orderbook.no_bids.items() if price >= (100 - mid) - 5)
imbalance = (yes_depth - no_depth) / max(yes_depth + no_depth, 1)
```

Positive imbalance = more resting YES bids = YES side is better supported.

**C2. Trade flow direction** -- From `TradeFlowTracker`:

```
flow_score = tracker.buy_pressure  # 0.0-1.0, >0.5 = net YES buying
flow_directional = (flow_score - 0.5) * 2  # rescale to -1.0 to +1.0
```

Only use if `tracker.is_active` (at least 3 trades in window). Otherwise score = 0.

**Combined orderbook score**: `0.6 * imbalance + 0.4 * flow_directional`

**Scoring contribution**: Weight: **0.20**.

### D. Chop Filter

**What to measure**: How noisy is recent BTC price action? Two metrics:

**D1. Sign flips**: Count direction changes in the last 12 five-second returns.

```
returns = [price_at(t) - price_at(t-5) for t in range(-60, 0, 5)]
signs = [1 if r > 0 else -1 for r in returns if abs(r) > 0.50]  # $0.50 noise floor
flips = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i-1])
```

6+ flips out of 11 possible = choppy. This is a confidence penalty, not a direction signal.

**D2. Net vs gross move ratio**: Compare the net displacement to the total path traveled.

```
net_move   = abs(price_now - price_60s_ago)
gross_move = sum(abs(price_at(t) - price_at(t-5)) for t in range(-55, 5, 5))
efficiency = net_move / max(gross_move, 0.01)
```

Efficiency near 1.0 = clean trend. Near 0.0 = pure chop.

**Scoring contribution**: Chop does not affect direction. It reduces confidence.

```
chop_penalty = 1.0 - (flips / 11.0) * 0.5 - (1.0 - efficiency) * 0.5
confidence_multiplier = max(0.1, chop_penalty)
```

This multiplier is applied to the final confidence score (Section 3).

### E. Cross-Venue Confirmation (Settling Contract Mid)

**What to measure**: The mid-price of the currently settling (about-to-expire) KXBTC15M contract. This is the contract that is still live as the new one is about to open.

**How to interpret**: The settling contract mid reflects the market's real-time assessment of BTC direction for the current window. If the settling mid is 65+ (strong YES) or 35- (strong NO), the market has already decided on the direction. If BTC momentum aligns with this -- both saying the same direction -- that is confirmation.

```
settling_mid_signal = (settling_mid - 50) / 50.0  # -1.0 to +1.0
```

If the settling mid disagrees with BTC momentum (e.g., settling YES at 70 but BTC is falling), this creates a conflict that should reduce confidence, not flip direction.

**Scoring contribution**: Weight: **0.25**. This is the same signal the current sniper uses (the `_YES_THRESHOLD`/`_NO_THRESHOLD` check), but now it is one component among five rather than the sole signal.

### Summary of Weights

| Component | Direction Weight | Confidence Effect |
|-----------|-----------------|-------------------|
| A. BTC Momentum (60s/15s/5s) | 0.40 | -- |
| B. Acceleration | 0.15 | -- |
| C. Orderbook Imbalance + Flow | 0.20 | -- |
| D. Chop Filter | 0.00 | Multiplicative penalty |
| E. Settling Contract Mid | 0.25 | -- |
| **Total** | **1.00** | |

---

## 3. Decision Logic

### Direction Score: -1.0 to +1.0

```
raw_direction = (
    0.40 * momentum_score +
    0.15 * acceleration_score +
    0.20 * orderbook_score +
    0.25 * settling_mid_score
)
direction = clamp(raw_direction, -1.0, +1.0)
```

Positive direction = YES. Negative direction = NO. The magnitude indicates conviction.

### Confidence Score: 0.0 to 1.0

```
raw_confidence = abs(direction)
confidence = raw_confidence * chop_multiplier
```

Confidence is the absolute magnitude of the direction score, penalized by chop. A perfectly clean +1.0 direction with no chop yields confidence 1.0. A +0.3 direction in choppy conditions might yield confidence 0.15.

### Three Regimes

| Regime | Direction Threshold | Confidence Threshold | Action |
|--------|--------------------|--------------------|--------|
| **Strong signal** | abs(direction) >= 0.50 | confidence >= 0.40 | Full size, immediate entry at activation |
| **Moderate signal** | abs(direction) >= 0.30 | confidence >= 0.25 | Half size, tighter TP (+4c instead of +5c) |
| **Weak / skip** | below moderate thresholds | -- | No entry. Log and wait for next session. |

### Why Forcing Every Session Destroys Edge

The contract spread at open is typically 3-6 cents. A random 50/50 entry loses the spread every time. The edge comes entirely from selectivity -- entering only when momentum predicts follow-through. Historical data from the existing engine shows that sessions with a clear pre-open BTC direction (>0.05% move in the 15s before open) have significantly higher follow-through rates than sessions where BTC is flat or choppy. Entering every session dilutes the high-confidence wins with low-confidence coin flips that bleed the spread.

The system should trade roughly 30-50% of sessions. If it is trading 80%+, the thresholds are too loose. If it is trading <15%, the thresholds are too tight.

---

## 4. Execution Logic

### Pre-Open Analysis: T-30s to T-3s

Starting 30 seconds before the next boundary, the signal layer begins continuous scoring. This runs alongside the sniper's existing IDLE phase.

1. **T-30s**: Begin sampling BTC price at 100ms intervals into the tick ring buffer.
2. **T-15s**: First full score computation (60s lookback now has 45s of data, 15s lookback is complete). Log the preliminary direction/confidence.
3. **T-10s**: Score stabilizes. If confidence is already below 0.15, early-skip. Log reason.
4. **T-5s**: Final acceleration measurement is valid. Lock the pre-open score.
5. **T-3s**: **Decision point**. Commit to LOCKED with side + size, or commit to SKIP.

Between T-3s and T-0s, no new scoring -- the sniper transitions to PROBING as soon as the boundary passes.

### Activation Detection Loop

Unchanged from current sniper. The PROBING phase sends 1-contract 50c probe orders every 250-750ms (adaptive backoff) until Kalshi accepts one, indicating the contract is live.

### Signal Validation at Activation

When the probe returns accepted (contract is live), before deploying the ladder, run one final signal check:

```
time_since_lock = now - lock_time
if time_since_lock > STALE_SIGNAL_TIMEOUT_S:
    # Signal was computed too long ago. Activation took too long.
    skip("stale signal: locked %.1fs ago" % time_since_lock)
    return

# Recompute 5s momentum only (fast check)
current_5s_return = (btc_price_now - btc_price_5s_ago) / btc_price_5s_ago
if locked_side == "yes" and current_5s_return < -REVERSAL_THRESHOLD:
    skip("momentum reversed since lock")
    return
if locked_side == "no" and current_5s_return > REVERSAL_THRESHOLD:
    skip("momentum reversed since lock")
    return
```

If validation passes, fall through to DEPLOYING.

### Immediate Market Entry on the Favored Side

The existing ladder logic in `_build_ladder()` and `_deploy_ladder()` handles the placement. The signal layer determines `side` and `bias_strength` (which controls aggressive vs passive allocation across ladder rungs).

### Take Profit at +5c

Unchanged. On fill, post a sell order at `fill_price + 5` cents. The `_SELL_OFFSET_CENTS = 5` constant governs this.

### Stale Signal Rejection

If the time between LOCKED and activation exceeds `STALE_SIGNAL_TIMEOUT_S` (default 10s), cancel and skip. The market conditions that justified the entry may no longer hold after 10+ seconds of waiting for the contract to go live.

---

## 5. Risk Controls

### Max Position Size Per Session

Existing logic: `_BALANCE_FRACTION = 0.50`, `_MAX_DOLLARS = 50.0`, `_MAX_CONTRACTS = 100`. The signal layer can further reduce size based on confidence:

```
if regime == "moderate":
    total_count = total_count // 2
```

### Daily Loss Cap

Integrate with the engine's existing `DAILY_LOSS_LIMIT = 15.00`. Before locking a new sniper trade, check:

```
daily_pnl = engine.get_daily_pnl()
if daily_pnl <= -DAILY_LOSS_LIMIT:
    skip("daily loss cap hit: $%.2f" % daily_pnl)
```

### Consecutive Loss Pause

Track sequential losses in the sniper. After 3 consecutive losses, pause for 2 sessions (skip the next 2 boundaries regardless of signal quality). Reset counter on any win.

```
if sniper_consecutive_losses >= 3:
    skip("3 consecutive losses, cooling off (%d sessions remaining)" % cooloff_remaining)
```

### Activation Timeout (45s)

Existing: `_ACTIVATION_TIMEOUT_S = 45.0`. If the contract does not activate within 45 seconds of the boundary, cancel. This is already implemented in the PROBING phase.

### Stale Signal Timeout (10s)

New. Separate from activation timeout. If activation occurs but the signal was locked more than 10 seconds ago, the pre-open analysis is stale. Skip rather than deploy on old information.

### Spread Check at Activation

At the moment of activation, read the order book spread:

```
spread = orderbook.best_yes_ask - orderbook.best_yes_bid
if spread > MAX_SPREAD_AT_ACTIVATION:
    skip("spread too wide: %dc" % spread)
```

Default `MAX_SPREAD_AT_ACTIVATION = 4` cents. A 5c+ spread at open means the TP of +5c is almost entirely consumed by the entry cost.

### Order Book Instability Skip

If the order book has fewer than 3 levels on the entry side within 5 cents of the mid, the book is too thin to trust. Skip.

```
levels_near_mid = sum(1 for p in orderbook.yes_bids if abs(p - mid) <= 5)
if levels_near_mid < 3:
    skip("thin book: %d levels near mid" % levels_near_mid)
```

### State Inconsistency Flatten

If the sniper detects contradictory state (e.g., fill recorded but no order ID, or phase is FILLED but engine already has a position), cancel everything and reset:

```
if state.phase == _Phase.FILLED and engine._open_position is not None:
    await _cancel_unfilled(engine._client, state)
    state.reset()
    log("state inconsistency: engine already has position, sniper resetting")
```

---

## 6. Full Pseudocode

```python
# ── Data Collection ──────────────────────────────────────────────────

def get_recent_btc_data(tick_buffer, now):
    """Extract BTC prices at specific lookback points from the ring buffer.

    tick_buffer: deque of (timestamp, price) tuples, ~100ms resolution
    Returns dict with price snapshots or None if data insufficient.
    """
    prices = list(tick_buffer)
    if len(prices) < 50:  # Need at least 5s of data at 100ms
        return None

    price_now = prices[-1][1]
    ts_now = prices[-1][0]

    def price_at_offset(seconds_ago):
        target_ts = ts_now - seconds_ago
        # Binary search or linear scan for nearest timestamp
        best = None
        for ts, p in reversed(prices):
            if ts <= target_ts:
                best = p
                break
        return best

    p5  = price_at_offset(5)
    p15 = price_at_offset(15)
    p60 = price_at_offset(60)

    if p5 is None or p15 is None:
        return None  # Not enough history

    return {
        "price_now": price_now,
        "price_5s": p5,
        "price_15s": p15,
        "price_60s": p60,  # May be None if < 60s of data
        "ts_now": ts_now,
    }


def compute_momentum_features(btc_data):
    """Compute normalized momentum scores for 60s/15s/5s windows.

    Returns dict of raw returns and normalized scores.
    """
    p = btc_data["price_now"]
    ret_5s  = (p - btc_data["price_5s"]) / btc_data["price_5s"]
    ret_15s = (p - btc_data["price_15s"]) / btc_data["price_15s"]
    ret_60s = ((p - btc_data["price_60s"]) / btc_data["price_60s"]
               if btc_data["price_60s"] else 0.0)

    # Normalize to -1..+1 using empirical scaling
    score_5s  = clamp(ret_5s  / 0.0004, -1.0, 1.0)
    score_15s = clamp(ret_15s / 0.0008, -1.0, 1.0)
    score_60s = clamp(ret_60s / 0.0015, -1.0, 1.0)

    # Weighted combination
    momentum = 0.25 * score_5s + 0.35 * score_15s + 0.20 * score_60s
    # Rescale so momentum component is in -1..+1
    momentum = clamp(momentum / 0.80, -1.0, 1.0)

    return {
        "ret_5s": ret_5s, "ret_15s": ret_15s, "ret_60s": ret_60s,
        "score_5s": score_5s, "score_15s": score_15s, "score_60s": score_60s,
        "momentum": momentum,
    }


def compute_acceleration(btc_data):
    """Is the move speeding up or fading?

    Compares velocity over last 5s vs preceding 10s.
    Returns score in -1.0..+1.0 where sign must agree with momentum.
    """
    p_now = btc_data["price_now"]
    p_5s  = btc_data["price_5s"]
    p_15s = btc_data["price_15s"]

    velocity_recent = (p_now - p_5s) / 5.0
    velocity_prior  = (p_5s - p_15s) / 10.0

    accel = velocity_recent - velocity_prior

    # Normalize by prior velocity magnitude (floor to avoid div/0)
    divisor = max(abs(velocity_prior), 0.01)
    raw_score = accel / divisor
    return clamp(raw_score, -1.0, 1.0)


def compute_orderbook_features(orderbook, trade_flow_tracker):
    """Compute order book imbalance and trade flow direction.

    orderbook: LocalOrderBook instance from kalshi_ws.py
    trade_flow_tracker: TradeFlowTracker instance from kalshi_ws.py
    Returns score in -1.0..+1.0 (positive = YES bias).
    """
    mid = orderbook.mid_price_cents

    # Depth imbalance: resting qty within 5c of mid on each side
    yes_depth = sum(
        qty for price, qty in orderbook.yes_bids.items()
        if price >= mid - 5
    )
    no_depth = sum(
        qty for price, qty in orderbook.no_bids.items()
        if price >= (100 - mid) - 5
    )
    total_depth = yes_depth + no_depth
    depth_imbalance = (yes_depth - no_depth) / max(total_depth, 1)

    # Trade flow direction
    if trade_flow_tracker.is_active:
        flow_directional = (trade_flow_tracker.buy_pressure - 0.5) * 2
    else:
        flow_directional = 0.0

    # Combine
    score = 0.6 * depth_imbalance + 0.4 * flow_directional
    return clamp(score, -1.0, 1.0)


def compute_chop_features(tick_buffer, now):
    """Compute chop penalty from sign flips and move efficiency.

    Returns confidence multiplier in 0.1..1.0 (1.0 = clean, 0.1 = maximum chop).
    """
    prices = list(tick_buffer)
    if len(prices) < 120:  # Need ~60s of data
        return 0.5  # Uncertain, penalize moderately

    ts_now = prices[-1][0]

    # Sample prices at 5s intervals over last 60s
    samples = []
    for offset in range(0, 65, 5):  # 0s, 5s, 10s, ..., 60s ago
        target_ts = ts_now - offset
        closest = min(prices, key=lambda x: abs(x[0] - target_ts))
        samples.append(closest[1])
    samples.reverse()  # oldest first

    # Compute 5s returns
    returns = [samples[i+1] - samples[i] for i in range(len(samples)-1)]

    # D1: Sign flips (ignore returns smaller than $0.50 noise floor)
    significant = [1 if r > 0.50 else (-1 if r < -0.50 else 0)
                   for r in returns]
    significant = [s for s in significant if s != 0]

    if len(significant) < 3:
        flips = 0
        max_flips = 1
    else:
        flips = sum(1 for i in range(1, len(significant))
                    if significant[i] != significant[i-1])
        max_flips = len(significant) - 1

    flip_ratio = flips / max(max_flips, 1)

    # D2: Net vs gross efficiency
    net_move = abs(samples[-1] - samples[0])
    gross_move = sum(abs(r) for r in returns)
    efficiency = net_move / max(gross_move, 0.01)

    # Combine into confidence multiplier
    chop_penalty = 1.0 - flip_ratio * 0.5 - (1.0 - efficiency) * 0.5
    return max(0.1, min(1.0, chop_penalty))


# ── Scoring ──────────────────────────────────────────────────────────

def score_direction(momentum_features, acceleration_score,
                    orderbook_score, settling_mid_cents):
    """Compute composite direction score: -1.0 (strong NO) to +1.0 (strong YES).

    Weights:
      Momentum:     0.40
      Acceleration: 0.15
      Orderbook:    0.20
      Settling mid: 0.25
    """
    settling_score = (settling_mid_cents - 50) / 50.0
    settling_score = clamp(settling_score, -1.0, 1.0)

    direction = (
        0.40 * momentum_features["momentum"] +
        0.15 * acceleration_score +
        0.20 * orderbook_score +
        0.25 * settling_score
    )
    return clamp(direction, -1.0, 1.0)


def score_confidence(direction, chop_multiplier):
    """Compute confidence: 0.0 (no confidence) to 1.0 (maximum confidence).

    Confidence = |direction| * chop_multiplier.
    """
    return abs(direction) * chop_multiplier


# ── Session Skip Logic ───────────────────────────────────────────────

def should_skip_session(confidence, direction, engine, sniper_stats):
    """Determine whether to skip this session.

    Returns (should_skip: bool, reason: str).
    """
    # Daily loss cap
    daily_pnl = engine.get_daily_pnl()
    if daily_pnl <= -engine.DAILY_LOSS_LIMIT:
        return True, "daily loss cap: $%.2f" % daily_pnl

    # Consecutive loss cooloff
    if sniper_stats.consecutive_losses >= 3:
        if sniper_stats.cooloff_remaining > 0:
            return True, ("cooling off after %d consecutive losses (%d skips left)"
                          % (sniper_stats.consecutive_losses,
                             sniper_stats.cooloff_remaining))

    # Existing engine position
    if engine._open_position is not None:
        return True, "engine already has open position"

    # Window already traded
    if getattr(engine, '_window_locked', False):
        return True, "window already locked"

    # Signal too weak
    if confidence < CONFIDENCE_THRESHOLD_MODERATE:
        return True, "confidence %.3f below threshold %.3f" % (
            confidence, CONFIDENCE_THRESHOLD_MODERATE)

    if abs(direction) < DIRECTION_THRESHOLD_MODERATE:
        return True, "direction %.3f below threshold %.3f" % (
            abs(direction), DIRECTION_THRESHOLD_MODERATE)

    return False, ""


# ── Activation and Entry ─────────────────────────────────────────────

async def wait_for_contract_activation(client, ticker, side, boundary_ts):
    """Probe for contract activation using 1-lot orders.

    Mirrors existing _probe_activation logic in sniper.py.
    Returns (activated: bool, elapsed_s: float).
    """
    start = time.time()
    probe_count = 0

    while True:
        elapsed = time.time() - boundary_ts
        if elapsed > ACTIVATION_TIMEOUT_S:
            return False, elapsed

        # Adaptive interval: fast first 5s, then slow
        interval = 0.250 if elapsed < 5.0 else 0.750
        await asyncio.sleep(interval)

        probe_count += 1
        try:
            order = await client.place_order(
                ticker=ticker, side=side, price=50, count=1
            )
        except KalshiAPIError:
            continue

        if order is None:
            continue

        # Accepted -- contract is live. Cancel the probe.
        if order.order_id:
            try:
                await client.cancel_order(order.order_id)
            except Exception:
                pass
        return True, elapsed


def validate_signal_at_activation(locked_side, lock_time, tick_buffer, now):
    """Fast validation that the signal has not reversed since lock.

    Returns (valid: bool, reason: str).
    """
    # Stale signal check
    time_since_lock = now - lock_time
    if time_since_lock > STALE_SIGNAL_TIMEOUT_S:
        return False, "stale signal: %.1fs since lock" % time_since_lock

    # Quick 5s momentum reversal check
    btc_data = get_recent_btc_data(tick_buffer, now)
    if btc_data is None:
        return False, "no BTC data at activation"

    ret_5s = (btc_data["price_now"] - btc_data["price_5s"]) / btc_data["price_5s"]
    reversal_threshold = 0.0002  # 0.02% reversal kills the signal

    if locked_side == "yes" and ret_5s < -reversal_threshold:
        return False, "momentum reversed: 5s return %.5f" % ret_5s
    if locked_side == "no" and ret_5s > reversal_threshold:
        return False, "momentum reversed: 5s return %.5f" % ret_5s

    return True, "ok"


async def place_market_entry(client, state):
    """Deploy the ladder. Wraps existing _deploy_ladder.

    Ladder rungs are pre-built during LOCKED phase via _build_ladder.
    """
    return await _deploy_ladder(client, state)


async def place_take_profit(client, state):
    """Post TP sell at fill_price + SELL_OFFSET_CENTS. Wraps existing logic."""
    tp_price = min(state.fill_price + SELL_OFFSET_CENTS, 99)
    tp_order = await client.place_order(
        ticker=state.ticker,
        side=state.side,
        price=tp_price,
        count=state.fill_count,
        action="sell",
    )
    return tp_order


async def manage_open_trade(engine, state):
    """Post-entry management. Monitors fill, posts TP, registers position.

    This wraps the existing RESTING -> FILLED -> EXIT_POSTED flow.
    No changes to the existing state machine here.
    """
    # Poll fills
    if state.phase == _Phase.RESTING:
        got_fill = await _poll_fills(engine._client, state)
        if got_fill:
            for rung in state.rungs:
                if rung.filled > 0:
                    state.fill_count = rung.filled
                    state.fill_price = rung.fill_price
                    state.fill_order_id = rung.order_id
                    break
            await _cancel_unfilled(engine._client, state,
                                   except_id=state.fill_order_id)
            state.phase = _Phase.FILLED

    # Post TP and register position
    if state.phase == _Phase.FILLED:
        tp_order = await place_take_profit(engine._client, state)
        if tp_order and tp_order.order_id:
            state.tp_order_id = tp_order.order_id
        _set_position(engine, state)
        state.phase = _Phase.DONE


# ── Main Loop ────────────────────────────────────────────────────────

async def sniper_signal_loop(engine):
    """Complete sniper main loop with signal layer.

    Replaces the IDLE->LOCKED transition in sniper_check() with
    full multi-factor scoring.
    """
    state = _SniperState()
    stats = SniperStats()  # Tracks consecutive losses, daily count
    tick_buffer = deque(maxlen=6000)  # 600s at 100ms resolution

    while True:
        now = time.time()
        boundary = _next_boundary(now)
        ttb = boundary - now

        # ── Continuous tick capture (always running) ──────────────
        btc_price = engine._price_feed.tick_tracker.last_price
        if btc_price > 0:
            tick_buffer.append((now, btc_price))

        # ── Phase: IDLE ──────────────────────────────────────────
        if state.phase == _Phase.IDLE:

            # Not in the pre-open window yet
            if ttb > 30.0 or ttb < 1.0:
                await asyncio.sleep(0.5)
                continue

            # ── Pre-open scoring: T-30s to T-3s ──────────────────
            btc_data = get_recent_btc_data(tick_buffer, now)
            if btc_data is None:
                await asyncio.sleep(0.5)
                continue

            # Compute all features
            momentum = compute_momentum_features(btc_data)
            acceleration = compute_acceleration(btc_data)
            orderbook = engine._kalshi_tape  # LocalOrderBook
            trade_flow = engine._trade_flow_tracker
            ob_score = compute_orderbook_features(orderbook, trade_flow)
            chop_mult = compute_chop_features(tick_buffer, now)

            # Settling contract mid
            tape = getattr(engine, '_kalshi_tape', None)
            settling_mid = tape.mid_price_cents if tape and tape.updated_at > 0 else 50

            # Score
            direction = score_direction(momentum, acceleration, ob_score, settling_mid)
            confidence = score_confidence(direction, chop_mult)

            # Log continuously during pre-open
            if int(ttb) % 5 == 0:  # Log every ~5s
                log("SNIPER PRE-OPEN T-%.0fs: dir=%.3f conf=%.3f "
                    "mom=%.3f accel=%.3f ob=%.3f mid=%dc chop=%.2f",
                    ttb, direction, confidence,
                    momentum["momentum"], acceleration, ob_score,
                    settling_mid, chop_mult)

            # ── Decision point: T-3s ─────────────────────────────
            if ttb <= 3.0:
                skip, reason = should_skip_session(
                    confidence, direction, engine, stats)

                if skip:
                    log("SNIPER SKIP: %s", reason)
                    state.phase = _Phase.DONE
                    state.boundary = boundary
                    await asyncio.sleep(ttb + 2)
                    state.reset()
                    continue

                # Determine side and regime
                side = "yes" if direction > 0 else "no"
                regime = ("strong" if confidence >= CONFIDENCE_THRESHOLD_STRONG
                          and abs(direction) >= DIRECTION_THRESHOLD_STRONG
                          else "moderate")

                # Compute size
                total = await _compute_size(engine._client)
                if regime == "moderate":
                    total = max(1, total // 2)

                bias = min(abs(direction), 1.0)
                ticker = compute_next_ticker(now)
                rungs = _build_ladder(side, total, bias)

                # Lock
                state.phase = _Phase.LOCKED
                state.boundary = boundary
                state.side = side
                state.total_count = total
                state.ticker = ticker
                state.rungs = rungs
                state.bias_strength = bias
                state.lock_time = now

                log("SNIPER LOCKED [%s]: %s %s dir=%.3f conf=%.3f "
                    "regime=%s size=%d | %s",
                    ticker, side.upper(), regime, direction, confidence,
                    regime, total,
                    " + ".join("%dx@%dc(%s)" % (r.count, r.price, r.label)
                               for r in rungs))

            await asyncio.sleep(0.25)  # 250ms scoring resolution
            continue

        # ── Phase: LOCKED -> wait for boundary ───────────────────
        if state.phase == _Phase.LOCKED:
            if ttb > 0.1:
                await asyncio.sleep(min(ttb - 0.05, 0.1))
                continue
            state.phase = _Phase.PROBING
            log("SNIPER: boundary reached, probing %s", state.ticker)

        # ── Phase: PROBING ───────────────────────────────────────
        if state.phase == _Phase.PROBING:
            activated, elapsed = await wait_for_contract_activation(
                engine._client, state.ticker, state.side, state.boundary
            )

            if not activated:
                log("SNIPER: activation timeout after %.1fs", elapsed)
                state.phase = _Phase.DONE
                state.reset()
                continue

            state.activation_ts = time.time()

            # ── Signal validation at activation ──────────────────
            valid, reason = validate_signal_at_activation(
                state.side, state.lock_time, tick_buffer, time.time()
            )

            if not valid:
                log("SNIPER: signal invalid at activation: %s", reason)
                state.phase = _Phase.DONE
                state.reset()
                continue

            # ── Spread check ─────────────────────────────────────
            if orderbook and orderbook.is_ready:
                spread = orderbook.best_yes_ask - orderbook.best_yes_bid
                if spread > MAX_SPREAD_AT_ACTIVATION:
                    log("SNIPER: spread %dc too wide at activation", spread)
                    state.phase = _Phase.DONE
                    state.reset()
                    continue

            # ── Book thickness check ─────────────────────────────
            if orderbook and orderbook.is_ready:
                mid = orderbook.mid_price_cents
                entry_book = (orderbook.yes_bids if state.side == "yes"
                              else orderbook.no_bids)
                levels = sum(1 for p in entry_book if abs(p - mid) <= 5)
                if levels < MIN_BOOK_LEVELS:
                    log("SNIPER: thin book (%d levels), skipping", levels)
                    state.phase = _Phase.DONE
                    state.reset()
                    continue

            log("SNIPER ACTIVATED: %.2fs after boundary, deploying", elapsed)
            state.phase = _Phase.DEPLOYING

        # ── Phase: DEPLOYING ─────────────────────────────────────
        if state.phase == _Phase.DEPLOYING:
            await place_market_entry(engine._client, state)

            # Check for instant fills
            for rung in state.rungs:
                if rung.filled > 0:
                    state.fill_count = rung.filled
                    state.fill_price = rung.fill_price
                    state.fill_order_id = rung.order_id
                    await _cancel_unfilled(engine._client, state,
                                           except_id=rung.order_id)
                    state.phase = _Phase.FILLED
                    break

            if state.phase == _Phase.DEPLOYING:
                state.phase = _Phase.RESTING
                state.last_poll_ts = time.time()

        # ── Phase: RESTING ───────────────────────────────────────
        if state.phase == _Phase.RESTING:
            elapsed = time.time() - state.activation_ts
            if elapsed > FILL_TIMEOUT_S:
                await _cancel_unfilled(engine._client, state)
                log("SNIPER: fill timeout %.1fs, cancelling", elapsed)
                state.phase = _Phase.DONE
                state.reset()
                continue

            await asyncio.sleep(FILL_POLL_INTERVAL_S)
            await manage_open_trade(engine, state)
            continue

        # ── Phase: FILLED ────────────────────────────────────────
        if state.phase == _Phase.FILLED:
            await manage_open_trade(engine, state)
            stats.on_trade(state)
            state.reset()
            continue

        # ── Phase: DONE ──────────────────────────────────────────
        if state.phase == _Phase.DONE:
            # Wait until next boundary is far enough away to reset
            if now > state.boundary + ACTIVATION_TIMEOUT_S + 10:
                state.reset()
            await asyncio.sleep(1.0)
            continue

        await asyncio.sleep(0.25)
```

---

## 7. Tunable Parameters

| Parameter | Starting Value | Description | Sensitivity |
|-----------|---------------|-------------|-------------|
| `MOMENTUM_SCALE_5S` | 0.0004 | 5s return that maps to score +/-1.0 | Lower = more sensitive to small moves |
| `MOMENTUM_SCALE_15S` | 0.0008 | 15s return that maps to score +/-1.0 | Primary signal driver |
| `MOMENTUM_SCALE_60S` | 0.0015 | 60s return that maps to score +/-1.0 | Context signal |
| `MOMENTUM_WEIGHT_5S` | 0.25 | Weight of 5s momentum in momentum composite | |
| `MOMENTUM_WEIGHT_15S` | 0.35 | Weight of 15s momentum in momentum composite | Highest weight = most influential |
| `MOMENTUM_WEIGHT_60S` | 0.20 | Weight of 60s momentum in momentum composite | |
| `ACCELERATION_LOOKBACK_S` | 5 / 15 | Recent window (5s) vs prior window (15s) | Shorter = more responsive, noisier |
| `CHOP_NOISE_FLOOR` | $0.50 | Minimum BTC move to count as directional | Too low = noise counted as signal |
| `CHOP_FLIP_WEIGHT` | 0.50 | Weight of sign-flip penalty in chop multiplier | |
| `CHOP_EFFICIENCY_WEIGHT` | 0.50 | Weight of move-efficiency penalty in chop multiplier | |
| `DIRECTION_THRESHOLD_STRONG` | 0.50 | abs(direction) for full-size entry | |
| `DIRECTION_THRESHOLD_MODERATE` | 0.30 | abs(direction) for half-size entry | Lower = more trades, lower avg quality |
| `CONFIDENCE_THRESHOLD_STRONG` | 0.40 | Confidence for full-size entry | |
| `CONFIDENCE_THRESHOLD_MODERATE` | 0.25 | Confidence for half-size entry | This is the gate. Controls participation rate. |
| `MAX_SPREAD_AT_ACTIVATION` | 4 cents | Skip if bid-ask spread exceeds this | 4c keeps TP margin positive |
| `ACTIVATION_TIMEOUT_S` | 45.0 | Max seconds probing for activation | Existing value, well-calibrated |
| `STALE_SIGNAL_TIMEOUT_S` | 10.0 | Max seconds between lock and activation | >10s = market may have moved |
| `REVERSAL_THRESHOLD` | 0.0002 | 5s return reversal that kills the signal at activation | 0.02% is ~$17 on $85k BTC |
| `SELL_OFFSET_CENTS` | 5 | TP offset above fill price | Existing value. 4c for moderate regime. |
| `FILL_TIMEOUT_S` | 15.0 | Max seconds waiting for fills after deployment | Existing value |
| `CONSECUTIVE_LOSS_PAUSE` | 3 | Losses before cooloff triggers | |
| `COOLOFF_SESSIONS` | 2 | Sessions to skip after consecutive loss streak | |
| `MIN_BOOK_LEVELS` | 3 | Minimum order book levels within 5c of mid | |
| `WEIGHT_MOMENTUM` | 0.40 | Momentum composite weight in direction score | |
| `WEIGHT_ACCELERATION` | 0.15 | Acceleration weight in direction score | |
| `WEIGHT_ORDERBOOK` | 0.20 | Orderbook composite weight in direction score | |
| `WEIGHT_SETTLING_MID` | 0.25 | Settling mid weight in direction score | |

### Tuning Process

1. **Shadow mode first**. Run the scoring system alongside the existing sniper for 200+ sessions. Log direction, confidence, regime, and whether the session would have been traded. Compare against actual outcomes.
2. **Calibrate scaling factors**. Plot the distribution of 5s/15s/60s BTC returns in the 30s before contract opens. Set scaling factors so that +/-1.0 corresponds to the 95th percentile of observed moves.
3. **Set thresholds by participation rate**. Adjust `CONFIDENCE_THRESHOLD_MODERATE` until the system trades 35-45% of sessions. If the win rate on those sessions is above 55% (net of spread), the thresholds are well-set.
4. **Validate chop filter**. Sessions where chop_multiplier < 0.3 should have observably worse outcomes than sessions where chop_multiplier > 0.7. If not, reduce chop weight.

---

## 8. Failure Modes

### False Continuation

**What happens**: BTC has a 15s move that looks like momentum, but it is the tail end of a larger move that is about to mean-revert. The sniper enters on the wrong side of the reversal.

**Defense**: The acceleration component (Section 2B) detects fading moves. If the 5s velocity is slower than the 10s velocity, the signal is penalized. Additionally, the chop filter catches oscillating price action that precedes reversals.

### Opening Chop

**What happens**: The contract opens into a 2-3 second period of random oscillation. Both sides get hit, spreads widen, and fills happen at poor prices.

**Defense**: The spread check at activation (Section 5) rejects entries when the spread exceeds 4 cents. The book thickness check rejects thin books. The chop filter during pre-open scoring catches sessions where BTC itself is oscillating.

### Exchange Activation Lag

**What happens**: Kalshi takes 10-30 seconds to activate the contract after the boundary. By the time the contract is live, the pre-open signal is stale.

**Defense**: The stale signal timeout (Section 5) rejects entries when the lock-to-activation gap exceeds 10 seconds. The 5s momentum reversal check at activation catches signals that have reversed during the wait.

### Spread Shock

**What happens**: The spread is narrow during pre-open analysis but widens to 6-8 cents at the moment of activation, consuming the entire TP margin.

**Defense**: The spread check runs at activation, not during pre-open. If the spread at activation exceeds `MAX_SPREAD_AT_ACTIVATION`, skip.

### Thin Liquidity Fills

**What happens**: The ladder places orders but only the aggressive rung fills, at a price worse than expected. The TP at +5c is effectively only +2-3c of real profit.

**Defense**: The ladder already uses tiered pricing (53c/51c/50c). The aggressive rung is sized at only 25% of total contracts. If only the aggressive rung fills, the dollar exposure is limited. Consider reducing the aggressive rung to 15% for moderate-regime entries.

### Signal Decay

**What happens**: The scoring system works well for 2-3 weeks, then the relationship between pre-open BTC momentum and contract follow-through weakens as other participants adapt.

**Defense**: Continuous monitoring of win rate by confidence bucket. If the strong-signal regime drops below 55% win rate over 50+ trades, the weights need recalibration. Run the tuning process (Section 7) monthly. The system is designed to be re-weighted without code changes -- all weights live in the parameter table.

### Overfitting Tiny Moves

**What happens**: The scaling factors are set so aggressively that a $5 BTC move (0.006%) generates a strong signal. These tiny moves have no predictive power.

**Defense**: The scaling factors are set so that +/-1.0 corresponds to the 95th percentile of observed pre-open moves. A $5 move on $85k BTC (0.006%) would score approximately 0.006% / 0.08% = 0.075 on the 15s momentum component -- far below any threshold. Only moves of ~$50+ (0.06%) generate meaningful scores.

### Confusing Noise for Momentum

**What happens**: Low-volume overnight sessions produce BTC price jitter that triggers the momentum signals despite no real directional conviction.

**Defense**: The chop filter (Section 2D) measures move efficiency. Pure noise has efficiency near 0.0, which drives the confidence multiplier toward 0.1. Combined with the minimum confidence threshold of 0.25, noisy sessions are reliably filtered.

---

## Integration with Existing sniper.py

### What the Current Sniper Handles

The existing `sniper.py` implements the full execution state machine:

- **Ticker computation**: `compute_next_ticker()` generates the KXBTC15M ticker string for the upcoming window.
- **Activation probing**: PROBING phase sends cheap 1-lot orders every 250-750ms to detect when Kalshi activates the contract.
- **Ladder placement**: `_build_ladder()` creates tiered rungs (aggressive/standard/passive). `_deploy_ladder()` places them all at once.
- **Fill monitoring**: RESTING phase polls order status at 200ms intervals.
- **TP placement**: FILLED phase posts a sell at `fill_price + 5c`.
- **Engine position registration**: `_set_position()` writes the fill into `engine._open_position` so the main engine's position management takes over.

### What This Document Describes

The **signal layer** that feeds into the sniper's LOCKED phase. Currently, the IDLE-to-LOCKED transition in `sniper_check()` uses a single signal:

```python
# Current signal logic (lines 307-316 of sniper.py)
tape = getattr(engine, '_kalshi_tape', None)
mid = tape.mid_price_cents if tape and tape.updated_at > 0 else 50

if mid >= _YES_THRESHOLD:       # 60
    side = "yes"
elif mid <= _NO_THRESHOLD:      # 40
    side = "no"
else:
    skip  # mid indeterminate
```

This is the settling contract mid as the only signal. It works but is coarse -- it only fires when the market is already 60/40 or more extreme, and it has no information about BTC momentum, acceleration, or noise level.

### Upgrade Path

1. **Add the tick ring buffer**. In the engine's main loop or in `PriceFeedTask`, populate a `deque(maxlen=6000)` with `(timestamp, btc_price)` tuples at ~100ms resolution from the aggTrade stream. This is the primary data source for momentum, acceleration, and chop features.

2. **Add the scoring functions**. Implement `compute_momentum_features()`, `compute_acceleration()`, `compute_orderbook_features()`, `compute_chop_features()`, `score_direction()`, and `score_confidence()` as standalone functions in a new `sniper_signals.py` module.

3. **Replace the IDLE-to-LOCKED transition**. Instead of the settling-mid check, run the full scoring system starting at T-30s. At T-3s, make the lock/skip decision based on direction and confidence scores.

4. **Add signal validation at activation**. Insert `validate_signal_at_activation()` between PROBING and DEPLOYING. This is a new check that does not exist in the current sniper.

5. **Add risk controls**. Consecutive loss tracking, spread check at activation, book thickness check. These are new guards that wrap the existing state transitions.

6. **Leave PROBING/DEPLOYING/RESTING/FILLED/EXIT_POSTED unchanged**. The execution phases are already well-tested. The signal layer only changes how the sniper decides *whether* and *which side* to trade. Once LOCKED, everything downstream is the same.

### File Structure After Integration

```
sniper.py            -- State machine (unchanged except IDLE->LOCKED transition)
sniper_signals.py    -- New: scoring functions, feature computation
price_feed.py        -- Existing: add tick ring buffer population
kalshi_ws.py         -- Existing: LocalOrderBook and TradeFlowTracker (read-only)
```

### Shadow Mode

Before going live, run the scoring system in shadow mode for 200+ sessions:

1. Compute direction and confidence scores for every session.
2. Log the scores alongside the actual contract outcome (YES or NO resolution).
3. Backtest: would the system have entered? On which side? What was the contract mid 5 seconds after activation (proxy for TP fill)?
4. Compute hypothetical win rate, average P&L, and participation rate.
5. Only switch from the settling-mid signal to the full scoring system after shadow mode confirms positive expected value.
