"""Kalshi Microstructure Engine — Market Pressure Scoring.

Replaces probability-based signal generation with order flow + latency exploitation.
Detects when the Kalshi binary contract market is being forced directionally and
which side is in control.

Core insight: Kalshi reprices AFTER BTC moves. The lag between BTC impulse and
contract repricing is the primary exploitable inefficiency.

Components:
    1. BTC Impulse    — exogenous driver (BTC moved, Kalshi hasn't caught up)
    2. Book Pressure  — passive liquidity bias (who's stacking bids)
    3. Flow Momentum  — aggressive intent (taker flow direction + large trades)
    4. Kalshi Lag      — inefficiency timing (expected vs actual contract move)
"""
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class PressureScore:
    """Composite microstructure signal for entry decisions."""
    # Core signal
    direction: str = ""         # "yes", "no", or "" (no signal)
    score: float = 0.0          # -1.0 (strong NO) to +1.0 (strong YES)
    confidence: float = 0.0     # 0.0-1.0 (magnitude-weighted agreement)

    # Component scores (each -1.0 to +1.0)
    btc_impulse: float = 0.0    # BTC direction + magnitude
    book_pressure: float = 0.0  # order book imbalance + microprice skew
    flow_momentum: float = 0.0  # taker flow direction + large trade bias
    kalshi_lag: float = 0.0     # how much Kalshi is lagging BTC move

    # Threshold
    entry_threshold: float = 0.30
    crossed: bool = False

    # Persistence (signal must hold for N cycles)
    persistence_count: int = 0  # consecutive cycles above threshold
    persistent: bool = False    # True when persistence_count >= required

    # Market state
    spread_cents: int = 0
    book_depth_yes: int = 0
    book_depth_no: int = 0
    btc_move_5s: float = 0.0
    btc_move_30s: float = 0.0
    btc_move_300s: float = 0.0   # 5-min BTC trend ($) — dominant-direction gate input


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class MarketPressure:
    """Real-time market pressure scorer.

    Call update() every engine cycle (0.3s) with current market data.
    Returns a PressureScore indicating direction + strength of market forcing.
    """

    # Persistence: signal must hold for this many consecutive cycles
    PERSISTENCE_CYCLES = 3  # ~1 second at 0.3s cycles

    def __init__(self):
        # Rolling BTC price history (for impulse calculation)
        self._btc_history: deque = deque(maxlen=200)  # (timestamp, price)

        # Rolling Kalshi mid history (for lag calculation)
        self._mid_history: deque = deque(maxlen=200)  # (timestamp, mid_cents)

        # Rolling pressure scores (for persistence)
        self._score_history: deque = deque(maxlen=10)

        # Empirical BTC→Kalshi mapping (rolling regression)
        self._lag_pairs: deque = deque(maxlen=100)  # (btc_change, mid_change) over 30s windows

        self._last_update: float = 0.0
        self._last_score: PressureScore = PressureScore()

    def update(
        self,
        btc_price: float,
        btc_velocity: float,
        kalshi_mid: float,
        book_imbalance: float,
        microprice: float,
        depth_yes: int,
        depth_no: int,
        spread_cents: int,
        flow_buy_pressure: float,
        flow_strength: float,
        taker_imbalance: float,
        large_trade_bias: float,
        volatility: float,
        session_age: float,
    ) -> PressureScore:
        """Compute market pressure score from all available microstructure data.

        Args:
            btc_price: Current BTC spot from Coinbase/Binance
            btc_velocity: BTC price change rate ($/sec)
            kalshi_mid: Current Kalshi contract mid (cents)
            book_imbalance: Order book imbalance -1 (NO) to +1 (YES)
            microprice: Imbalance-weighted mid (cents)
            depth_yes: Contracts within 5c of best YES bid
            depth_no: Contracts within 5c of best NO bid
            spread_cents: Current bid-ask spread
            flow_buy_pressure: YES taker fraction 0-1 (from WS TradeFlowTracker)
            flow_strength: Flow signal strength 0-1
            taker_imbalance: Tape taker imbalance -1 to +1 (from REST)
            large_trade_bias: Large trade bias -1 to +1 (from REST)
            volatility: Annualized BTC vol (from prob engine)
            session_age: Seconds into current 15-min window
        """
        now = time.time()

        # Record BTC and mid histories
        self._btc_history.append((now, btc_price))
        self._mid_history.append((now, kalshi_mid))

        # ── 1. BTC IMPULSE (weight: 0.35) ──
        # Detect BTC moves that Kalshi hasn't priced yet
        btc_5s = self._btc_change(5.0)
        btc_30s = self._btc_change(30.0)

        # Normalize: $20 move in 5s = strong (1.0), $5 = moderate
        impulse_5s = _clamp(btc_5s / 20.0, -1.0, 1.0)
        impulse_30s = _clamp(btc_30s / 50.0, -1.0, 1.0)

        # Acceleration: is the move INCREASING? (change in velocity)
        btc_2s = self._btc_change(2.0)
        acceleration = _clamp((btc_2s - btc_5s * 0.4) / 10.0, -0.5, 0.5)

        # Blend: short-term weighted higher + acceleration bonus
        btc_impulse = _clamp(
            0.6 * impulse_5s + 0.25 * impulse_30s + 0.15 * acceleration,
            -1.0, 1.0,
        )

        # ── 2. BOOK PRESSURE (weight: 0.25) ──
        # Order book reveals passive intent — who is stacking bids
        # Clamp all inputs — raw values can exceed [-1,1] on extreme books
        _book_imb = _clamp(book_imbalance, -1.0, 1.0)
        microprice_skew = _clamp((microprice - kalshi_mid) / 5.0, -1.0, 1.0)

        # Depth asymmetry
        total_depth = max(depth_yes + depth_no, 1)
        depth_ratio = _clamp((depth_yes - depth_no) / total_depth, -1.0, 1.0)

        book_pressure = _clamp(
            0.4 * _book_imb + 0.3 * microprice_skew + 0.3 * depth_ratio,
            -1.0, 1.0,
        )

        # ── 3. FLOW MOMENTUM (weight: 0.25) ──
        # Taker flow shows aggressive intent
        # WS data (fast, last 60s) + REST data (slower, last 5min)
        ws_signal = _clamp((flow_buy_pressure - 0.5) * 2 * flow_strength, -1.0, 1.0)
        rest_signal = _clamp(0.5 * taker_imbalance + 0.5 * large_trade_bias, -1.0, 1.0)

        flow_momentum = _clamp(0.6 * ws_signal + 0.4 * rest_signal, -1.0, 1.0)

        # ── 4. KALSHI LAG (weight: 0.15) ──
        # The key microstructure insight: Kalshi reprices AFTER BTC
        # Use empirical mapping, not linear assumption
        mid_30s = self._mid_change(30.0)
        expected_change = self._expected_mid_change(btc_30s, kalshi_mid)
        lag_raw = expected_change - mid_30s

        # Normalize: 10c lag = strong signal
        kalshi_lag = _clamp(lag_raw / 10.0, -1.0, 1.0)

        # FAST-PATH LAG OVERRIDE: when BTC impulse is strong but Kalshi
        # hasn't moved, the empirical mapping can't capture this yet (needs
        # paired history). Override with instant lag signal — this is the
        # HIGHEST EDGE condition (BTC shock, Kalshi stale).
        if abs(btc_impulse) > 0.5 and abs(mid_30s) < 3.0:
            kalshi_lag = _clamp(btc_impulse * 0.8, -1.0, 1.0)

        # Record pair for empirical mapping
        if abs(btc_30s) > 5:  # only record meaningful moves
            self._lag_pairs.append((btc_30s, mid_30s))

        # ── READINESS SCALING: dampen scores when data is incomplete ──
        # Prevents full-strength scores on partial history from creating
        # misleading logs, false attribution, or bypassing guards.
        _readiness = 1.0
        if len(self._btc_history) < 30 or len(self._mid_history) < 30:
            _readiness = 0.25

        # ── COMPOSITE SCORE ──
        # Regime-adaptive weights: adjust based on market state
        w_impulse = 0.35
        w_book = 0.25
        w_flow = 0.25
        w_lag = 0.15

        # High vol: impulse and lag matter more (big moves, delayed repricing)
        if volatility > 0.40:
            w_impulse = 0.40
            w_book = 0.15
            w_flow = 0.25
            w_lag = 0.20

        # Low liquidity: book pressure less reliable (can be spoofed)
        if total_depth < 20:
            w_book = 0.10
            w_impulse += 0.075
            w_flow += 0.075

        score = _clamp(
            (w_impulse * btc_impulse +
             w_book * book_pressure +
             w_flow * flow_momentum +
             w_lag * kalshi_lag) * _readiness,
            -1.0, 1.0,
        )

        # ── DIRECTION ──
        direction = "yes" if score > 0.05 else "no" if score < -0.05 else ""

        # ── CONFIDENCE: magnitude-weighted agreement ──
        components = [
            (btc_impulse, w_impulse),
            (book_pressure, w_book),
            (flow_momentum, w_flow),
            (kalshi_lag, w_lag),
        ]
        total_magnitude = sum(abs(c) * w for c, w in components)
        aligned_magnitude = sum(abs(c) * w for c, w in components
                                if (c > 0) == (score > 0))
        confidence = aligned_magnitude / total_magnitude if total_magnitude > 0 else 0

        # ── ADAPTIVE THRESHOLD ──
        vol_factor = _clamp(1.0 - (volatility / 0.50), 0.5, 1.5)
        spread_factor = _clamp(spread_cents / 5.0, 0.8, 2.0)
        time_factor = 1.0 if session_age < 480 else 1.3 if session_age < 720 else 1.6

        base_threshold = 0.25
        entry_threshold = base_threshold * vol_factor * spread_factor * time_factor
        entry_threshold = _clamp(entry_threshold, 0.20, 0.55)

        crossed = abs(score) >= entry_threshold

        # ── PERSISTENCE: signal must hold for N consecutive cycles ──
        self._score_history.append((now, score, crossed, direction))

        if crossed and direction:
            # Count consecutive cycles with same direction above threshold
            persist_count = 0
            for ts, s, c, d in reversed(list(self._score_history)):
                if c and d == direction:
                    persist_count += 1
                else:
                    break
            persistent = persist_count >= self.PERSISTENCE_CYCLES
        else:
            persist_count = 0
            persistent = False

        # 5-min BTC trend — used by Dominant-Direction gate to require
        # BTC to be moving strongly with the FVG side before entering.
        btc_300s = self._btc_change(300.0)

        result = PressureScore(
            direction=direction,
            score=round(score, 4),
            confidence=round(confidence, 3),
            btc_impulse=round(btc_impulse, 3),
            book_pressure=round(book_pressure, 3),
            flow_momentum=round(flow_momentum, 3),
            kalshi_lag=round(kalshi_lag, 3),
            entry_threshold=round(entry_threshold, 3),
            crossed=crossed,
            persistence_count=persist_count,
            persistent=persistent,
            spread_cents=spread_cents,
            book_depth_yes=depth_yes,
            book_depth_no=depth_no,
            btc_move_5s=round(btc_5s, 2),
            btc_move_30s=round(btc_30s, 2),
            btc_move_300s=round(btc_300s, 2),
        )

        self._last_score = result
        self._last_update = now
        return result

    def reset(self) -> None:
        """Reset state for new session window."""
        self._btc_history.clear()
        self._mid_history.clear()
        self._score_history.clear()
        # Keep lag_pairs — empirical mapping persists across sessions

    def _btc_change(self, lookback_s: float) -> float:
        """BTC price change over the last N seconds."""
        if len(self._btc_history) < 2:
            return 0.0
        now = self._btc_history[-1][0]
        target = now - lookback_s
        for ts, price in reversed(list(self._btc_history)):
            if ts <= target:
                return self._btc_history[-1][1] - price
        # Not enough history — use oldest available
        return self._btc_history[-1][1] - self._btc_history[0][1]

    def _mid_change(self, lookback_s: float) -> float:
        """Kalshi mid change over the last N seconds."""
        if len(self._mid_history) < 2:
            return 0.0
        now = self._mid_history[-1][0]
        target = now - lookback_s
        for ts, mid in reversed(list(self._mid_history)):
            if ts <= target:
                return self._mid_history[-1][1] - mid
        return self._mid_history[-1][1] - self._mid_history[0][1]

    def _expected_mid_change(self, btc_change: float, current_mid: float) -> float:
        """Estimate expected Kalshi mid change from BTC move.

        Uses empirical rolling regression if enough data points,
        otherwise falls back to contract-position-aware estimate.
        """
        # Empirical mapping from historical pairs
        if len(self._lag_pairs) >= 10:
            # Simple linear regression: mid_change = alpha * btc_change
            sum_xy = sum(b * m for b, m in self._lag_pairs)
            sum_xx = sum(b * b for b, _ in self._lag_pairs)
            if sum_xx > 0:
                alpha = sum_xy / sum_xx
                return alpha * btc_change

        # Fallback: position-aware estimate
        # Contract near 50c: most sensitive to BTC moves
        # Contract near 10c or 90c: less sensitive (already decided)
        sensitivity = 1.0 - abs(current_mid - 50) / 50.0  # 1.0 at 50c, 0.0 at 0/100c
        sensitivity = max(sensitivity, 0.1)

        # Rough: $15 BTC move at 50c ≈ 15c contract move
        # At 80c: $15 move ≈ 3c move (already priced in)
        return btc_change / 15.0 * sensitivity * 15.0

    @property
    def last_score(self) -> PressureScore:
        return self._last_score

    @property
    def is_ready(self) -> bool:
        """Need at least 10 seconds of data for meaningful signals."""
        return len(self._btc_history) >= 30 and len(self._mid_history) >= 30
