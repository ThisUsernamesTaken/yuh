"""Pure protective-order math for the live engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# 2026-05-02 Phase 0.1.6 — asymmetric volatility gate types.
# Trend orientation labels:
#   "counter": our side opposite to BTC's prevailing direction = mean-reversion
#              fade. High-risk loser pattern; tighter vol cap.
#   "with":    our side matches BTC's prevailing direction = momentum-aligned.
#              Rare for BB_PURE; looser vol cap.
#   "neutral": |trend change| within the dead-zone. Use legacy default cap.
TrendOrientation = Literal["counter", "with", "neutral"]


def classify_trend_orientation(
    *,
    side: str,
    trend_change_usd: float,
    dead_zone_usd: float,
) -> TrendOrientation:
    """Classify whether our fade is counter-trend, with-trend, or neutral.

    Side-aware: for YES buys, a negative trend (BTC dropping) means we're
    fading the down-trend = counter. For NO buys, a positive trend means
    we're fading the up-trend = counter.
    """
    side_l = (side or "").lower()
    if abs(trend_change_usd) <= dead_zone_usd:
        return "neutral"
    btc_up = trend_change_usd > 0
    btc_down = trend_change_usd < 0
    if side_l == "yes":
        if btc_down:
            return "counter"
        if btc_up:
            return "with"
    elif side_l == "no":
        if btc_up:
            return "counter"
        if btc_down:
            return "with"
    return "neutral"


def select_vol_cap(
    *,
    orientation: TrendOrientation,
    cap_neutral: float,
    cap_counter: float,
    cap_with: float,
) -> float:
    """Pick the right vol cap for the given trend orientation."""
    if orientation == "counter":
        return cap_counter
    if orientation == "with":
        return cap_with
    return cap_neutral


@dataclass(frozen=True)
class MfeTrailDecision:
    mfe_cents: int
    trail_price: int | None
    armed: bool


def compute_mfe_trail_price(
    *,
    entry_cents: int,
    bid_cents: int,
    previous_mfe_cents: int = 0,
    min_profit_cents: int = 4,
    base_trail_cents: int = 3,
    trail_ratio: float = 0.4,
    threshold_cents: int = 8,
) -> MfeTrailDecision:
    """Return updated MFE and an optional trailing sell floor.

    Once MFE reaches the threshold, the trail gives back max(base_trail,
    ratio * MFE) from current bid while never selling below entry + min profit.
    """
    entry = int(entry_cents or 0)
    bid = int(bid_cents or 0)
    prev_mfe = max(0, int(previous_mfe_cents or 0))
    current_mfe = max(prev_mfe, max(0, bid - entry))

    threshold = max(0, int(threshold_cents or 0))
    if entry <= 0 or bid <= 0 or current_mfe < threshold:
        return MfeTrailDecision(mfe_cents=current_mfe, trail_price=None, armed=False)

    base_trail = max(0, int(base_trail_cents or 0))
    ratio_trail = int(round(float(trail_ratio or 0.0) * current_mfe))
    giveback = max(base_trail, ratio_trail)
    floor = max(entry + max(0, int(min_profit_cents or 0)), bid - giveback)
    return MfeTrailDecision(mfe_cents=current_mfe, trail_price=max(1, min(99, floor)), armed=True)
