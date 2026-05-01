# shadow_edge.py — Phase 3 additive edge model (LOGS ONLY, NO ORDERS)
# ============================================================================
# Runs alongside the DOMINANT gate. Combines the BB prob-engine signal with
# the microstructure pressure, MTF confluence, RSI tilt, and 5-min BTC move
# into a single signed score on a consistent [-100, +100] scale where
# positive = YES, negative = NO.
#
# Explicit non-goals for Phase 3:
#   * Does NOT gate entries (DOMINANT still decides).
#   * Does NOT size entries.
#   * Does NOT mutate any engine state.
#
# Weights are round defaults chosen for rough equivalence across components;
# calibration from Phase-1 tagged data will refine them. The log line is the
# contract — schema stays stable even as numbers change.
# ============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


def _clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


@dataclass
class ShadowEdge:
    score: float                      # signed, +ve = YES
    side: str                         # "yes" / "no" / ""  ("" only if score == 0)
    confidence: float                 # 0..1, = min(1.0, |score|/25)
    components: Dict[str, float] = field(default_factory=dict)

    def as_log_fragment(self) -> str:
        """Compact "| bb=+6.2 pres=+3.1 mtf=+1.6 rsi=+0.5 btc=+0.0" block."""
        parts = []
        for k in ("bb", "pres", "mtf", "rsi", "btc"):
            v = self.components.get(k, 0.0)
            parts.append(f"{k}={v:+.1f}")
        return " ".join(parts)


def compute_shadow_edge(
    model_yes_prob: float,
    mkt_price_cents: float,
    pressure_score: float,
    pressure_confidence: float,
    mtf_score: Optional[float],
    rsi: float,
    btc_5m_move: float,
) -> ShadowEdge:
    """Additive linear blend → ShadowEdge. All components are YES-positive.

    Inputs
    ------
    model_yes_prob   : Brownian-Bridge fair-value P(YES), 0..1
    mkt_price_cents  : current Kalshi YES price in cents (0..100)
    pressure_score   : MarketPressure.score, -1..+1 (YES +ve)
    pressure_confidence : MarketPressure.confidence, 0..1
    mtf_score        : MTF confluence, -100..+100 (None if unavailable)
    rsi              : 1-minute RSI, 0..100 (50 = neutral)
    btc_5m_move      : BTC $ move over trailing 5 min

    Returns
    -------
    ShadowEdge with .score, .side, .confidence, .components
    """
    # Guard against pre-warmup garbage. If the prob engine isn't ready,
    # model_yes_prob can be 0.0 — that would produce a massive false NO.
    if model_yes_prob <= 0.0 or mkt_price_cents <= 0.0:
        return ShadowEdge(score=0.0, side="", confidence=0.0,
                          components={"bb": 0.0, "pres": 0.0, "mtf": 0.0,
                                      "rsi": 0.0, "btc": 0.0})

    bb = _clip(model_yes_prob * 100.0 - mkt_price_cents, -20.0, 20.0)
    pres = _clip(pressure_score * pressure_confidence * 10.0, -10.0, 10.0)
    mtf = _clip((mtf_score or 0.0) / 5.0, -20.0, 20.0)
    rsi_tilt = _clip((rsi - 50.0) / 50.0 * 3.0, -3.0, 3.0)
    btc = _clip(btc_5m_move / 30.0, -1.0, 1.0) * 5.0

    score = bb + pres + mtf + rsi_tilt + btc
    if score > 0:
        side = "yes"
    elif score < 0:
        side = "no"
    else:
        side = ""
    confidence = min(1.0, abs(score) / 25.0)

    return ShadowEdge(
        score=round(score, 2),
        side=side,
        confidence=round(confidence, 3),
        components={
            "bb":   round(bb, 2),
            "pres": round(pres, 2),
            "mtf":  round(mtf, 2),
            "rsi":  round(rsi_tilt, 2),
            "btc":  round(btc, 2),
        },
    )
