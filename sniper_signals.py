# sniper_signals.py — Signal layer for the sniper
#
# Produces direction score (-1 to +1) and confidence (0 to 1) from:
#   - BTC momentum (Binance kline close prices via tick tracker)
#   - Settling contract mid (Kalshi tape)
#   - Kalshi orderbook imbalance (WS trade flow)
#   - Acceleration (momentum derivative)
#   - Chop filter (noise detector)
#
# Also handles post-activation micro-confirmation for moderate signals.

import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    direction: float       # -1.0 (NO) to +1.0 (YES)
    confidence: float      # 0.0 to 1.0
    regime: str            # "strong", "moderate", "skip"
    tp_offset: int         # Adaptive TP: 6c for strong, 4c for moderate
    components: dict       # Individual scores for logging


@dataclass
class ConfirmationResult:
    confirmed: bool
    reason: str
    mid_delta: int         # Kalshi mid change during confirmation
    btc_delta: float       # BTC price change during confirmation


# ── Tunable parameters ─────────────────────────────────────────────────

# For a millisecond scalp, only one thing matters:
# is BTC ticking up or down RIGHT NOW?
W_MOMENTUM = 0.95           # The ONLY signal. Is BTC ticking up or down right now?
W_ACCELERATION = 0.00       # Removed
W_ORDERBOOK = 0.05          # Tiny tiebreaker if momentum is ambiguous
W_SETTLING = 0.00           # REMOVED — previous window doesn't predict next

# Regime thresholds — lowered for millisecond scalp
# Any detectable BTC tick momentum is enough to pick a side
STRONG_DIRECTION = 0.30      # Clear momentum → immediate entry, TP +6c
STRONG_CONFIDENCE = 0.20
MODERATE_DIRECTION = 0.15    # Slight momentum → confirm 1.5s, TP +4c
MODERATE_CONFIDENCE = 0.10

# Chop filter
CHOP_KILL_THRESHOLD = 0.70   # chop_ratio > 0.70 → halve confidence

# Momentum scaling (normalize raw values to -1..+1)
MOM_60S_SCALE = 50.0         # $50 BTC move in 60s = max score
MOM_15S_SCALE = 25.0         # $25 in 15s = max
MOM_5S_SCALE = 10.0          # $10 in 5s = max

# Settling mid scaling
SETTLING_MID_CENTER = 50     # 50c = neutral
SETTLING_MID_SCALE = 50.0    # 0c or 100c = max signal

# Confirmation — quick 0.5s check at session open
CONFIRM_WAIT_S = 0.5         # Just enough to see first print direction
CONFIRM_MID_THRESHOLD = 1    # 1c move confirms at session open (contract just left 50c)
CONFIRM_BTC_AGREE = False    # Don't check BTC — the contract mid IS the signal at open


def compute_signal(engine) -> SignalResult:
    """Compute direction + confidence from all available data sources.
    Called at T-3s to T-1s before boundary."""

    components = {}

    # ── A. BTC Momentum ──────────────────────────────────────────
    # Read from the tick tracker (fed by kline close prices)
    pf = getattr(engine, '_price_feed', None)
    ticks = pf.tick_tracker if pf else None

    mom_score = 0.0
    if ticks and len(ticks._prices) >= 3:
        # Use timestamps to find correct lookback, not index count
        # Coinbase sends 10-50 ticks/s so index-based lookback is wrong
        all_ticks = list(ticks._prices)  # [(ts, price), ...]
        now_ts = all_ticks[-1][0]
        current_price = all_ticks[-1][1]

        def price_at_seconds_ago(secs):
            target_ts = now_ts - secs
            for ts, px in reversed(all_ticks):
                if ts <= target_ts:
                    return px
            return all_ticks[0][1]  # Oldest available

        mom_60 = current_price - price_at_seconds_ago(60)
        mom_15 = current_price - price_at_seconds_ago(15)
        mom_5 = current_price - price_at_seconds_ago(5)

        # Normalize and weight by recency (recent matters more)
        norm_60 = max(-1.0, min(1.0, mom_60 / MOM_60S_SCALE))
        norm_15 = max(-1.0, min(1.0, mom_15 / MOM_15S_SCALE))
        norm_5 = max(-1.0, min(1.0, mom_5 / MOM_5S_SCALE))

        # Weight: 5s=70%, 15s=25%, 60s=5% (we only care about right now)
        mom_score = norm_5 * 0.70 + norm_15 * 0.25 + norm_60 * 0.05

        components["mom_60"] = f"${mom_60:+.1f}"
        components["mom_15"] = f"${mom_15:+.1f}"
        components["mom_5"] = f"${mom_5:+.1f}"
        components["mom_score"] = f"{mom_score:+.3f}"

    # ── B. Settling Mid ──────────────────────────────────────────
    tape = getattr(engine, '_kalshi_tape', None)
    settling_mid = tape.mid_price_cents if tape and tape.updated_at > 0 else 50

    # Normalize: 0c → -1.0, 50c → 0.0, 100c → +1.0
    settling_score = (settling_mid - SETTLING_MID_CENTER) / SETTLING_MID_SCALE
    settling_score = max(-1.0, min(1.0, settling_score))
    components["settling_mid"] = settling_mid
    components["settling_score"] = f"{settling_score:+.3f}"

    # ── C. Orderbook Imbalance ───────────────────────────────────
    ob_score = 0.0
    ws = getattr(engine, '_kalshi_ws', None)
    if ws and ws._subscribed_tickers:
        try:
            active_ticker = list(ws._subscribed_tickers)[-1]
            flow = ws.get_flow(active_ticker)
            if flow.is_active:
                # buy_pressure: 0.0-1.0, 0.5=neutral
                ob_score = (flow.buy_pressure - 0.5) * 2.0  # -1 to +1
                ob_score = max(-1.0, min(1.0, ob_score))
                components["buy_pressure"] = f"{flow.buy_pressure:.2f}"
                components["flow_dir"] = flow.flow_direction or "neutral"
        except Exception:
            pass
    components["ob_score"] = f"{ob_score:+.3f}"

    # ── D. Acceleration ──────────────────────────────────────────
    accel_score = 0.0
    if ticks and len(ticks._prices) >= 6:
        prices = [p for _, p in ticks._prices]
        # Compare recent velocity to slightly older velocity
        recent = prices[-3:]  # Last ~4.5s
        older = prices[-6:-3]  # Previous ~4.5s

        vel_recent = recent[-1] - recent[0]
        vel_older = older[-1] - older[0] if len(older) >= 2 else 0

        # Positive accel = move strengthening, negative = fading
        accel_raw = vel_recent - vel_older
        accel_score = max(-1.0, min(1.0, accel_raw / 15.0))  # $15 accel = max
        components["accel"] = f"{accel_raw:+.1f}"
        components["accel_score"] = f"{accel_score:+.3f}"

    # ── E. Chop Filter (confidence multiplier) ───────────────────
    chop_mult = 1.0
    if ticks and len(ticks._prices) >= 8:
        # Sample 8 evenly-spaced prices over the last 10 seconds
        all_t = list(ticks._prices)
        now_t = all_t[-1][0]
        sample_times = [now_t - (10 * (7 - i) / 7) for i in range(8)]
        recent = []
        for st in sample_times:
            closest = min(all_t, key=lambda t: abs(t[0] - st))
            recent.append(closest[1])

        # Check for stale data: if all prices identical, data isn't updating
        unique_prices = len(set(f"{p:.2f}" for p in recent))
        if unique_prices <= 2:
            # Stale or near-stale tick data — don't penalize, just neutral
            chop_mult = 1.0
            components["chop_ratio"] = "stale"
            components["chop_mult"] = "1.00 (stale data)"
        else:
            returns = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
            sign_flips = sum(1 for i in range(len(returns)-1)
                             if (returns[i] > 0) != (returns[i+1] > 0))
            max_flips = len(returns) - 1

            net_move = abs(recent[-1] - recent[0])
            gross_move = sum(abs(r) for r in returns)
            chop_ratio = 1.0 - (net_move / gross_move) if gross_move > 0 else 1.0

            if chop_ratio > CHOP_KILL_THRESHOLD:
                chop_mult = 0.5
            elif chop_ratio > 0.50:
                chop_mult = 0.75

            components["sign_flips"] = f"{sign_flips}/{max_flips}"
            components["chop_ratio"] = f"{chop_ratio:.2f}"
            components["chop_mult"] = f"{chop_mult:.2f}"

    # ── Combine into direction score ─────────────────────────────
    direction = (
        mom_score * W_MOMENTUM +
        settling_score * W_SETTLING +
        ob_score * W_ORDERBOOK +
        accel_score * W_ACCELERATION
    )
    direction = max(-1.0, min(1.0, direction))

    # ── Confidence = |direction| × agreement × chop multiplier ──
    # Agreement: how many components agree on direction
    scores = [mom_score, settling_score, ob_score, accel_score]
    if direction > 0:
        agreeing = sum(1 for s in scores if s > 0.05)
    else:
        agreeing = sum(1 for s in scores if s < -0.05)
    agreement = agreeing / len(scores)  # 0.0 to 1.0

    confidence = abs(direction) * (0.5 + 0.5 * agreement) * chop_mult
    confidence = max(0.0, min(1.0, confidence))

    # ── Determine regime ─────────────────────────────────────────
    abs_dir = abs(direction)
    if abs_dir >= STRONG_DIRECTION and confidence >= STRONG_CONFIDENCE:
        regime = "strong"
        tp_offset = 6
    elif abs_dir >= MODERATE_DIRECTION and confidence >= MODERATE_CONFIDENCE:
        regime = "moderate"
        tp_offset = 4
    else:
        regime = "skip"
        tp_offset = 5

    components["direction"] = f"{direction:+.3f}"
    components["confidence"] = f"{confidence:.3f}"
    components["agreement"] = f"{agreement:.2f}"
    components["regime"] = regime

    return SignalResult(
        direction=direction,
        confidence=confidence,
        regime=regime,
        tp_offset=tp_offset,
        components=components,
    )


async def confirm_at_activation(engine, locked_side: str, wait_s: float = CONFIRM_WAIT_S) -> ConfirmationResult:
    """Wait 1-2s after activation, observe first prints, confirm or reject.
    Only called for moderate signals — strong signals skip this."""

    import asyncio

    # Snapshot current state
    tape = getattr(engine, '_kalshi_tape', None)
    start_mid = tape.mid_price_cents if tape and tape.updated_at > 0 else 50

    pf = getattr(engine, '_price_feed', None)
    ticks = pf.tick_tracker if pf else None
    start_btc = ticks.last_price if ticks else 0

    # Wait for first prints
    await asyncio.sleep(wait_s)

    # Read post-wait state
    end_mid = tape.mid_price_cents if tape and tape.updated_at > 0 else 50
    end_btc = ticks.last_price if ticks else 0

    mid_delta = end_mid - start_mid
    btc_delta = end_btc - start_btc

    # Evaluate confirmation
    if locked_side == "yes":
        # For YES: mid should have gone up, BTC should have gone up
        mid_confirms = mid_delta >= CONFIRM_MID_THRESHOLD
        btc_confirms = btc_delta >= 0 if CONFIRM_BTC_AGREE else True
        spread_ok = end_mid > start_mid - 3  # Mid didn't collapse against us
    else:
        # For NO: mid should have gone down, BTC should have gone down
        mid_confirms = mid_delta <= -CONFIRM_MID_THRESHOLD
        btc_confirms = btc_delta <= 0 if CONFIRM_BTC_AGREE else True
        spread_ok = end_mid < start_mid + 3

    if mid_confirms and btc_confirms and spread_ok:
        return ConfirmationResult(True, "confirmed", mid_delta, btc_delta)
    elif not mid_confirms:
        return ConfirmationResult(False, f"mid_delta={mid_delta}c (need {'+' if locked_side=='yes' else '-'}{CONFIRM_MID_THRESHOLD}c)", mid_delta, btc_delta)
    elif not btc_confirms:
        return ConfirmationResult(False, f"btc_reversed ({btc_delta:+.1f})", mid_delta, btc_delta)
    else:
        return ConfirmationResult(False, f"spread_unstable (mid {start_mid}->{end_mid})", mid_delta, btc_delta)
