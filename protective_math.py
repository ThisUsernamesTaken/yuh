"""Pure protective-order math for the live engine."""
from __future__ import annotations

from dataclasses import dataclass


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
