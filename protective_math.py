"""Pure protective-order math for the live engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ── TP placement decision (2026-05-05) ─────────────────────────────────


@dataclass
class TpPlacementDecision:
    place_px: int
    post_only: bool
    converted_to_taker: bool   # True if we crossed because bid >= target


def decide_tp_placement(
    *,
    target_state: str,
    target_px: int,
    bid: int,
    enable_taker_convert: bool = True,
) -> TpPlacementDecision:
    """Choose the right (price, post_only) for a protective TP placement.

    Background (2026-05-05 portfolio-mode incident):
      When the contract bid moves *past* our intended TP target before we
      can land the maker order, post_only=True at target_px would cross
      into the bid (a sell at 62c when bid is 69c is taking 7c of free
      profit from a buyer — Kalshi correctly rejects post_only crosses).

      Naive retry loops fire 8-12 "post only cross" rejections in a few
      seconds, polluting the log and tripping our monitoring rule.

    Decision rule:
      - For non-TP states (SL etc.): unchanged, caller handles those.
      - For TP state where bid >= target_px (winning fast): cross the
        spread. place_px = bid, post_only = False. We pay taker fee but
        get a guaranteed fill at a price >= our target.
      - For TP state where bid < target_px: standard maker post_only at
        target_px (our preferred path).

    Pre-conditions for safe taker-convert:
      - target_state == "tp" (no flipping behavior on SL paths)
      - bid > 0 (valid quote)
      - target_px > 0 (sane target)
      - enable_taker_convert is True (kill-switch for safety)

    Returns:
      TpPlacementDecision(place_px, post_only, converted_to_taker)
    """
    # Only TP state is eligible for taker-convert; SL/HOLD pass through.
    if target_state != "tp":
        return TpPlacementDecision(
            place_px=int(target_px), post_only=True,
            converted_to_taker=False,
        )
    # Defensive: invalid inputs → fall back to maker default.
    if int(target_px) <= 0 or int(bid) <= 0:
        return TpPlacementDecision(
            place_px=int(target_px), post_only=True,
            converted_to_taker=False,
        )
    # Kill-switch off → maker default.
    if not enable_taker_convert:
        return TpPlacementDecision(
            place_px=int(target_px), post_only=True,
            converted_to_taker=False,
        )
    # Bid has crossed our TP — take the spread.
    if int(bid) >= int(target_px):
        return TpPlacementDecision(
            place_px=int(bid), post_only=False,
            converted_to_taker=True,
        )
    # Standard maker placement at target.
    return TpPlacementDecision(
        place_px=int(target_px), post_only=True,
        converted_to_taker=False,
    )



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
