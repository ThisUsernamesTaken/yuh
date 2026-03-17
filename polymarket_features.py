# polymarket_features.py — Feature extraction from live Polymarket 5m state
#
# Maintains a rolling history of (timestamp, up_mid) observations and computes
# probability change windows, velocity, acceleration, imbalance, and quality
# metrics. Called on every HFT evaluation tick.
#
# All methods are synchronous — designed to be called from the HFT engine's
# asyncio tick without awaiting.

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from polymarket_stream import Poly5mState
from config import (
    POLY_STALE_SIGNAL_MS, POLY_ENDGAME_SECONDS,
    POLY_MIN_TOP_QTY, POLY_MAX_SPREAD,
)

import logging
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Feature model
# ─────────────────────────────────────────────

@dataclass
class PolyFeatures:
    """Derived features from one Polymarket 5m state snapshot."""
    # Core probabilities (0.0–1.0)
    up_mid: float
    down_mid: float
    spread: float               # up_ask - up_bid in probability space

    # Probability change over rolling windows (positive = bullish for UP)
    prob_change_1s: float
    prob_change_3s: float
    prob_change_10s: float

    # Momentum
    velocity: float             # dp/dt (probability units per second), positive = UP accelerating
    acceleration: float         # d²p/dt²

    # Book shape
    top_imbalance: float        # (up_top_qty - down_top_qty) / total; +1 = all UP demand

    # Quality scores (0.0–1.0, higher = better quality)
    noise_score: float          # 0 = clean, 1 = very noisy (high flip rate)
    feed_health: float          # 0 = stale/unhealthy, 1 = fresh and valid

    # Time
    seconds_to_expiry: float
    is_endgame: bool            # True when inside POLY_ENDGAME_SECONDS of close

    # Meta
    computed_at: float          # time.time() when features were computed


# ─────────────────────────────────────────────
# Feature computer
# ─────────────────────────────────────────────

class PolyFeatureComputer:
    """Stateful feature extractor. Maintains rolling history of Poly5mState.

    Usage:
        computer = PolyFeatureComputer()
        # Each HFT tick:
        features = computer.update(poly_ref.get("state"))
        # features is None if state is missing, stale, or invalid
    """

    HISTORY_SECONDS = 30    # retain this many seconds of history
    MIN_OBS_FOR_VELOCITY = 3  # need at least this many obs to compute velocity

    def __init__(self) -> None:
        # Deque of (timestamp: float, up_mid: float) pairs
        self._history: deque = deque()
        self._last_market_id: str = ""

    def update(self, state: Optional[Poly5mState]) -> Optional[PolyFeatures]:
        """Ingest latest state and return computed features, or None if invalid."""
        if state is None:
            return None

        # Reset history when the market rolls over to a new window
        if state.market_id != self._last_market_id:
            self._history.clear()
            self._last_market_id = state.market_id

        # Reject stale feed
        if state.book_age_ms > POLY_STALE_SIGNAL_MS:
            logger.debug("Polymarket: stale feed (%.0fms)", state.book_age_ms)
            return None

        # Require both sides to have valid prices
        up_mid = state.up_mid
        down_mid = None
        if state.down_bid is not None and state.down_ask is not None:
            down_mid = (state.down_bid + state.down_ask) / 2.0
        elif state.down_bid or state.down_ask:
            down_mid = state.down_bid or state.down_ask

        if up_mid is None or down_mid is None:
            return None

        now = time.time()

        # Append to history and prune old entries
        self._history.append((now, up_mid))
        cutoff = now - self.HISTORY_SECONDS
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        # ── Probability change windows ─────────────────────────────────────
        prob_change_1s  = self._change_over(1.0,  up_mid)
        prob_change_3s  = self._change_over(3.0,  up_mid)
        prob_change_10s = self._change_over(10.0, up_mid)

        # ── Velocity & acceleration ────────────────────────────────────────
        velocity, acceleration = self._compute_momentum()

        # ── Top-of-book imbalance ──────────────────────────────────────────
        top_imbalance = self._compute_imbalance(state)

        # ── Spread ────────────────────────────────────────────────────────
        spread = 0.0
        if state.up_bid is not None and state.up_ask is not None:
            spread = max(0.0, state.up_ask - state.up_bid)

        # ── Noise score ────────────────────────────────────────────────────
        # High flip rate in recent history = noisy signal
        noise_score = self._compute_noise()

        # ── Feed health ────────────────────────────────────────────────────
        feed_health = self._compute_feed_health(state, spread)

        return PolyFeatures(
            up_mid=up_mid,
            down_mid=down_mid,
            spread=spread,
            prob_change_1s=prob_change_1s,
            prob_change_3s=prob_change_3s,
            prob_change_10s=prob_change_10s,
            velocity=velocity,
            acceleration=acceleration,
            top_imbalance=top_imbalance,
            noise_score=noise_score,
            feed_health=feed_health,
            seconds_to_expiry=state.seconds_to_expiry,
            is_endgame=state.is_endgame,
            computed_at=now,
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _change_over(self, seconds: float, current_mid: float) -> float:
        """Probability change over the last `seconds` seconds."""
        if len(self._history) < 2:
            return 0.0
        now = time.time()
        target_ts = now - seconds
        # Find the oldest observation within the window
        baseline = None
        for ts, mid in self._history:
            if ts >= target_ts:
                break
            baseline = mid
        # If no observation before the window start, use the oldest available
        if baseline is None:
            baseline = self._history[0][1]
        return current_mid - baseline

    def _compute_momentum(self) -> tuple[float, float]:
        """Compute velocity (dp/dt) and acceleration (d²p/dt²) via linear regression
        on recent history. Returns (velocity_per_sec, acceleration_per_sec²)."""
        obs = list(self._history)
        if len(obs) < self.MIN_OBS_FOR_VELOCITY:
            return 0.0, 0.0

        # Use only the last 10 seconds for velocity estimate
        now = time.time()
        recent = [(ts, mid) for ts, mid in obs if ts >= now - 10.0]
        if len(recent) < 2:
            return 0.0, 0.0

        # Normalize timestamps
        t0 = recent[0][0]
        xs = [t - t0 for t, _ in recent]
        ys = [mid for _, mid in recent]
        n = len(xs)

        # Linear regression slope = velocity
        sx = sum(xs)
        sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-12:
            return 0.0, 0.0
        velocity = (n * sxy - sx * sy) / denom

        # Acceleration: second derivative via quadratic regression using 3 points
        # Simplified: compare velocity in first half vs second half
        mid_idx = n // 2
        if mid_idx < 1:
            return velocity, 0.0
        early_vel = (ys[mid_idx] - ys[0]) / max(xs[mid_idx] - xs[0], 0.001)
        late_vel  = (ys[-1] - ys[mid_idx]) / max(xs[-1] - xs[mid_idx], 0.001)
        accel_window = max(xs[-1] - xs[0], 0.001)
        acceleration = (late_vel - early_vel) / accel_window

        return velocity, acceleration

    def _compute_imbalance(self, state: Poly5mState) -> float:
        """Signed top-of-book imbalance: +1 = all UP demand, -1 = all DOWN demand."""
        up_qty = state.up_top_qty or 0.0
        dn_qty = state.down_top_qty or 0.0
        total = up_qty + dn_qty
        if total < 1e-6:
            return 0.0
        return (up_qty - dn_qty) / total

    def _compute_noise(self) -> float:
        """Noise score 0–1: measures sign-flip rate in recent probability changes.
        High flip rate = noisy / mean-reverting market, low signal quality."""
        obs = list(self._history)
        if len(obs) < 4:
            return 0.0
        # Count direction changes in last 5 observations
        recent = obs[-5:]
        diffs = [recent[i+1][1] - recent[i][1] for i in range(len(recent) - 1)]
        if len(diffs) < 2:
            return 0.0
        flips = sum(
            1 for i in range(len(diffs) - 1)
            if diffs[i] * diffs[i+1] < 0  # sign change
        )
        return min(1.0, flips / max(len(diffs) - 1, 1))

    def _compute_feed_health(self, state: Poly5mState, spread: float) -> float:
        """Feed health score 0–1.
        Penalised by: stale data, excessive spread, missing quantities, endgame."""
        score = 1.0

        # Age penalty: loses 0.5 per second after 500ms
        age_ms = state.book_age_ms
        if age_ms > 500:
            score -= min(0.5, (age_ms - 500) / 1000.0)

        # Spread penalty: widen spread → reduce health
        if spread > POLY_MAX_SPREAD:
            score -= 0.3

        # Missing top-of-book quantities → less confidence
        if state.up_top_qty is None or state.down_top_qty is None:
            score -= 0.2

        # Endgame penalty
        if state.is_endgame:
            score -= 0.3

        return max(0.0, score)
