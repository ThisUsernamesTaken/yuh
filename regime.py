# regime.py — environment classification + drawdown state for trade attribution
# ============================================================================
# INSTRUMENTATION ONLY (Phase 1, 2026-04-21)
# ----------------------------------------------------------------------------
# Purpose: tag every entry with {regime, drawdown_state} so we can later
# answer "where is PnL actually coming from" without guessing.
#
# Explicit non-goals for this phase:
#   * Does NOT gate entries.
#   * Does NOT influence sizing.
#   * Does NOT trigger exits.
#   * Does NOT contain RSI, FVG, or other signal-layer inputs — those belong
#     to the SIGNAL, not the ENVIRONMENT. Mixing them here defeats the point.
#
# The thresholds below are intentionally simple priors. After 72h of tagged
# data we'll re-bucket post-hoc from the logged raw values; we are NOT going
# to patch thresholds during the observation window.
# ============================================================================
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Deque, Optional, Tuple


class Regime(str, Enum):
    STRUCTURED = "structured"   # steady BTC drift, persistent pressure, low noise
    CHOP       = "chop"         # mixed signals / oscillation — the default bucket
    CHAOTIC    = "chaotic"      # large realized vol — environment is hostile


class DrawdownState(str, Enum):
    FLAT    = "flat"       # near the session high-water mark
    SOFT_DD = "soft_dd"    # down 1.0%–3.0% from HWM on the day
    HARD_DD = "hard_dd"    # down >3.0% from HWM on the day


# ──────────────────────────────────────────────────────────────────────────
# Regime classifier
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class RegimeState:
    """Everything we log on every cycle + at every entry."""
    regime: Regime
    realized_vol_bps: float       # stdev of 1-tick log returns over vol_window_s, in bps
    btc_5m_move_abs: float        # absolute $ move over last 5 min
    pressure_consistency: float   # 0..1 — fraction of same-sign pressure scores in window
    sample_count: int             # ticks used for vol calc (<=5 = not ready)


@dataclass
class RegimeClassifier:
    """Stateful classifier. Call `update()` each engine cycle with the fresh
    BTC price, the 5-min BTC move, and the current pressure score. Returns
    a RegimeState. Internal buffers are bounded.

    Thresholds are deliberately coarse. The whole point is to observe, not
    to be clever. If the first week of data shows all trades in CHOP, that
    IS information — the current entry logic lives there.
    """
    # Window lengths
    vol_window_s: int = 30                 # realized-vol measurement window
    pressure_window_n: int = 40            # ~12s at 0.3s cadence

    # Classification thresholds (intentionally simple)
    vol_chaotic_bps: float = 35.0          # >= this → CHAOTIC regardless of others
    vol_structured_max_bps: float = 20.0   # structured requires calm tape
    btc_5m_structured_usd: float = 25.0    # structured requires real drift
    pressure_structured_min: float = 0.70  # structured requires persistent pressure
    pressure_sign_eps: float = 0.03        # |score| below this is "no sign"

    # Internal state (not part of the public config)
    _btc_ticks: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=500)
    )
    _pressure_signs: Deque[int] = field(
        default_factory=lambda: deque(maxlen=40)
    )

    def update(
        self,
        now: float,
        btc_price: float,
        btc_5m_move: float,
        pressure_score: float,
    ) -> RegimeState:
        # Maintain rolling BTC tick window
        if btc_price > 0:
            self._btc_ticks.append((now, btc_price))
        cutoff = now - self.vol_window_s
        while self._btc_ticks and self._btc_ticks[0][0] < cutoff:
            self._btc_ticks.popleft()

        # Realized vol: stdev of consecutive log returns in bps
        prices = [p for _, p in self._btc_ticks if p > 0]
        if len(prices) >= 5:
            rets_bps = [
                (b - a) / a * 10000.0
                for a, b in zip(prices, prices[1:])
                if a > 0
            ]
            realized_vol_bps = statistics.pstdev(rets_bps) if len(rets_bps) >= 2 else 0.0
        else:
            realized_vol_bps = 0.0

        # Pressure sign consistency: |sum(signs)| / count over window
        sign = 0
        if pressure_score > self.pressure_sign_eps:
            sign = 1
        elif pressure_score < -self.pressure_sign_eps:
            sign = -1
        self._pressure_signs.append(sign)
        if self._pressure_signs:
            consistency = abs(sum(self._pressure_signs)) / len(self._pressure_signs)
        else:
            consistency = 0.0

        btc_5m_abs = abs(btc_5m_move)

        # Classification — priority order, no AND-maze:
        #   CHAOTIC first (volatility spike dominates everything)
        #   STRUCTURED requires low vol + real drift + persistent pressure
        #   Everything else is CHOP (the default bucket — most of the tape)
        if realized_vol_bps >= self.vol_chaotic_bps:
            regime = Regime.CHAOTIC
        elif (realized_vol_bps <= self.vol_structured_max_bps
              and btc_5m_abs >= self.btc_5m_structured_usd
              and consistency >= self.pressure_structured_min):
            regime = Regime.STRUCTURED
        else:
            regime = Regime.CHOP

        return RegimeState(
            regime=regime,
            realized_vol_bps=round(realized_vol_bps, 3),
            btc_5m_move_abs=round(btc_5m_abs, 2),
            pressure_consistency=round(consistency, 3),
            sample_count=len(prices),
        )


# ──────────────────────────────────────────────────────────────────────────
# Drawdown tracker
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class DrawdownTracker:
    """Tracks intraday PnL vs session high-water mark. Session = UTC day.

    Purpose: attribute which drawdown state each entry was opened from.
    Mechanical thresholds — no volatility smoothing, no regime interaction.

    State is self-initializing on first update: the first balance seen on
    a new UTC day becomes both the session start AND the session HWM. That
    means the very first entry of the day is always FLAT, which is correct:
    we haven't drawn down from anything yet.
    """
    soft_threshold: float = 0.01   # 1.0% below HWM → SOFT_DD
    hard_threshold: float = 0.03   # 3.0% below HWM → HARD_DD

    _current_day: Optional[str] = None
    _session_start: float = 0.0
    _session_hwm: float = 0.0

    @staticmethod
    def _utc_day(now: float) -> str:
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    def update(self, now: float, balance: float) -> DrawdownState:
        if balance <= 0:
            return DrawdownState.FLAT  # no balance signal → can't classify

        day = self._utc_day(now)
        if day != self._current_day:
            # New UTC day — reset HWM and session start to whatever balance we have
            self._current_day = day
            self._session_start = balance
            self._session_hwm = balance
            return DrawdownState.FLAT

        if balance > self._session_hwm:
            self._session_hwm = balance
            return DrawdownState.FLAT

        dd_pct = (self._session_hwm - balance) / self._session_hwm
        if dd_pct >= self.hard_threshold:
            return DrawdownState.HARD_DD
        if dd_pct >= self.soft_threshold:
            return DrawdownState.SOFT_DD
        return DrawdownState.FLAT

    def session_hwm(self) -> float:
        return self._session_hwm


# ──────────────────────────────────────────────────────────────────────────
# DOMINANT margin — compact score of how strongly the live gate passed
# ──────────────────────────────────────────────────────────────────────────
def dominant_margin(
    btc_5m_move: float,
    fvg_side: str,
    pressure_score: float,
    pressure_confidence: float,
    rsi: float,
    fvg_signed: float,
) -> float:
    """Geometric mean of four normalized factor margins.

    Each factor is normalized to its threshold:
        btc       : |btc_5m_move| / 30              (required ≥ 1.0 to pass)
        pressure  : (|score|/0.03) * (confidence/0.55)
        rsi       : for YES, distance below 70 / 70; for NO, distance above 30 / 70
        fvg       : |fvg_signed| / 10               (required ≥ 1.0 to pass)

    Each sub-margin is clipped to [0.0, 5.0]. Geometric mean penalizes the
    weakest factor — a gate that "barely passes" has margin near 1.0 even if
    one factor is huge. A gate that is strong everywhere has margin >> 1.0.

    This is logged at every DOMINANT-passing entry so we can later ask
    "do marginal passes under-perform clear passes?"
    """
    def _clip(x: float) -> float:
        if x < 0.0:
            return 0.0
        if x > 5.0:
            return 5.0
        return x

    btc_m = _clip(abs(btc_5m_move) / 30.0)

    press_m_raw = (abs(pressure_score) / 0.03) * (pressure_confidence / 0.55)
    press_m = _clip(press_m_raw)

    # RSI margin: directional. For YES, we want RSI ≤ 70; margin = (70 - RSI)/70
    # For NO, we want RSI ≥ 30; margin = (RSI - 30)/70
    if fvg_side == "yes":
        rsi_m = _clip((70.0 - rsi) / 70.0)
    else:  # no
        rsi_m = _clip((rsi - 30.0) / 70.0)

    fvg_m = _clip(abs(fvg_signed) / 10.0)

    product = btc_m * press_m * rsi_m * fvg_m
    if product <= 0.0:
        return 0.0
    return round(product ** 0.25, 3)


# ──────────────────────────────────────────────────────────────────────────
# Raw edge helper — shadow "continuous edge" for comparison against gate
# ──────────────────────────────────────────────────────────────────────────
def raw_edge_pp(
    side: str,
    model_yes_prob: float,
    market_price_cents: float,
) -> float:
    """Edge in percentage points for OUR entry side.

    Positive = model thinks our side is underpriced on Kalshi.
    Negative = model thinks our side is overpriced.

    model_yes_prob comes from the Brownian Bridge prob engine; 0.0..1.0.
    market_price_cents is the Kalshi mid / fill price for OUR side, 0..100.

    This is the Phase-1 placeholder for the "continuous edge model". When
    Phase 3 builds the additive edge model, that replaces model_yes_prob
    here and the attribution schema doesn't change.
    """
    if model_yes_prob <= 0.0 or market_price_cents <= 0.0:
        return 0.0
    if side == "yes":
        model_pp = model_yes_prob * 100.0
    else:
        model_pp = (1.0 - model_yes_prob) * 100.0
    return round(model_pp - market_price_cents, 2)


# ──────────────────────────────────────────────────────────────────────────
# Phase 2 — sizing damper factors (live, multiplicative on raw sizing)
# ──────────────────────────────────────────────────────────────────────────
# User-approved "aggressive" ladder. Worst-case product 0.06× in
# CHAOTIC+HARD_DD; STRUCTURED+FLAT upsizes 10 %.
#
# This is load-bearing: a 0-contract damper result is meant to SKIP the
# entry, not fire a token 1-contract trade. The engine enforces that via
# REGIME_DAMPER_MIN_CONTRACTS in user_config.
REGIME_FACTORS = {
    Regime.STRUCTURED: 1.10,
    Regime.CHOP:       0.60,
    Regime.CHAOTIC:    0.25,
}
DRAWDOWN_FACTORS = {
    DrawdownState.FLAT:    1.00,
    DrawdownState.SOFT_DD: 0.55,
    DrawdownState.HARD_DD: 0.25,
}


def damp_factors(regime: Regime, dd: DrawdownState) -> Tuple[float, float]:
    """Look up (regime_factor, drawdown_factor). Defensive fallback to 1.0
    if either argument is not a member of its Enum — callers have already
    passed through `_last_regime` / `_last_drawdown`, but one bad frame
    should degrade to no-damping, not crash the engine.
    """
    rf = REGIME_FACTORS.get(regime, 1.0)
    df = DRAWDOWN_FACTORS.get(dd, 1.0)
    return rf, df
