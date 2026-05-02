"""Pure tape-pressure / absorption analysis (Phase 8 shadow-mode).

Encodes the user's manual-trading edge: watch the Kalshi tape for
sustained large buys on the side OPPOSITE to BTC's current direction.
That's institutional absorption — they're loading up against the
prevailing momentum, betting on a retracement.

The kalshi_tape module records `(ts_ms, taker_side, count, yes_price_cents)`
where taker_side is the AGGRESSOR side. So a tape entry with side="yes"
means a market BUY on YES at the recorded price; side="no" means BUY NO.

Dollar value:
- YES buy: count × yes_price_cents / 100
- NO  buy: count × (100 − yes_price_cents) / 100

Decision logic:
- Compute net absorption per side (BUY $ on side_X with BTC moving
  AGAINST that side).
- If absorption_dominant_side ≠ "none":
    - intended_side == absorption_dominant_side  → CONFIRM
    - intended_side == opposite                  → BLOCK
- Else                                            → NEUTRAL

Pure module; no I/O, no engine imports. Trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


TapeDecision = Literal["confirm", "block", "neutral"]


@dataclass(frozen=True)
class TapePressureSnapshot:
    yes_buy_dollars: float          # total $ of BUY-aggressor on YES in window
    no_buy_dollars: float           # total $ of BUY-aggressor on NO in window
    yes_large_count: int            # # of yes buys >= large_buy_usd
    no_large_count: int             # # of no buys >= large_buy_usd
    yes_reliable_count: int         # # of yes buys >= reliable_buy_usd
    no_reliable_count: int          # # of no buys >= reliable_buy_usd
    btc_5m_change_usd: float        # BTC change over 5min window (signed)
    inverse_trend_side: Literal["yes", "no", "none"]
    inverse_trend_dollars: float
    with_trend_dollars: float
    sample_count: int               # total trades counted in window


def trade_dollar_value(side: str, count: int, yes_price_cents: int) -> float:
    """Dollar value of a single buy-aggressor trade.

    Side is the aggressor side. yes_price_cents is the YES leg price.
    """
    side_l = (side or "").lower()
    count = max(0, int(count or 0))
    yes_px = max(0, min(100, int(yes_price_cents or 0)))
    if side_l == "yes":
        return count * yes_px / 100.0
    if side_l == "no":
        return count * (100 - yes_px) / 100.0
    return 0.0


def compute_pressure_snapshot(
    trades: Iterable[tuple],
    *,
    session_start_ms: int,
    now_ms: int,
    btc_5m_change_usd: float,
    window_min_s: float = 0.0,
    window_max_s: float = 300.0,
    large_buy_usd: float = 100.0,
    reliable_buy_usd: float = 200.0,
    btc_dead_zone_usd: float = 20.0,
) -> TapePressureSnapshot:
    """Aggregate buy-side dollar volume in a session-age window.

    `trades` is an iterable of (ts_ms, side, count, yes_price_cents).
    `session_start_ms` anchors the session-age clock.

    Only trades whose ts is in [session_start + window_min_s,
                                session_start + window_max_s] count.

    btc_5m_change_usd: signed dollar change over BTC's recent 5min.
        Positive = BTC up. Used to classify which side is "inverse-trend".
    """
    win_lo_ms = session_start_ms + int(window_min_s * 1000)
    win_hi_ms = session_start_ms + int(window_max_s * 1000)

    yes_dollars = 0.0
    no_dollars = 0.0
    yes_large = no_large = 0
    yes_reliable = no_reliable = 0
    samples = 0

    for entry in trades:
        try:
            ts_ms, side, count, yes_px = entry
        except (ValueError, TypeError):
            continue
        ts_ms = int(ts_ms or 0)
        if ts_ms < win_lo_ms or ts_ms > win_hi_ms:
            continue
        side_l = (str(side) or "").lower()
        if side_l not in ("yes", "no"):
            continue
        dollars = trade_dollar_value(side_l, count, yes_px)
        if dollars <= 0:
            continue
        samples += 1
        if side_l == "yes":
            yes_dollars += dollars
            if dollars >= large_buy_usd:
                yes_large += 1
            if dollars >= reliable_buy_usd:
                yes_reliable += 1
        else:
            no_dollars += dollars
            if dollars >= large_buy_usd:
                no_large += 1
            if dollars >= reliable_buy_usd:
                no_reliable += 1

    # Classify which side (if any) is fading the BTC trend.
    if abs(btc_5m_change_usd) <= btc_dead_zone_usd:
        inverse_side: Literal["yes", "no", "none"] = "none"
        inverse_dollars = 0.0
        with_dollars = 0.0
    elif btc_5m_change_usd < 0:
        # BTC DOWN. YES is the side fading (= absorbing the drop).
        inverse_side = "yes"
        inverse_dollars = yes_dollars
        with_dollars = no_dollars
    else:
        # BTC UP. NO is the side fading (= absorbing the rip).
        inverse_side = "no"
        inverse_dollars = no_dollars
        with_dollars = yes_dollars

    return TapePressureSnapshot(
        yes_buy_dollars=yes_dollars,
        no_buy_dollars=no_dollars,
        yes_large_count=yes_large,
        no_large_count=no_large,
        yes_reliable_count=yes_reliable,
        no_reliable_count=no_reliable,
        btc_5m_change_usd=btc_5m_change_usd,
        inverse_trend_side=inverse_side,
        inverse_trend_dollars=inverse_dollars,
        with_trend_dollars=with_dollars,
        sample_count=samples,
    )


def evaluate_tape_decision(
    snapshot: TapePressureSnapshot,
    intended_side: str,
    *,
    inverse_trend_min_usd: float = 200.0,
    dominance_ratio: float = 2.0,
    min_consistency_count: int = 3,
) -> TapeDecision:
    """Decide CONFIRM / BLOCK / NEUTRAL for a BB_PURE entry attempt.

    CONFIRM: tape shows sustained absorption on our intended side
        (= big money has been buying our side AGAINST the BTC trend, AND
         dominantly so vs the with-trend side).
    BLOCK: tape shows sustained absorption on the OPPOSITE side
        (= big money is loading the other way; we'd be fighting them).
    NEUTRAL: no clear absorption signal (BTC neutral, or no dominance).

    Three conditions must hold to flag absorption on a side:
      1. inverse_trend_dollars >= inverse_trend_min_usd  (must be sized)
      2. inverse_trend_dollars >= dominance_ratio × with_trend_dollars  (must dominate)
      3. count of large buys on inverse side >= min_consistency_count   (sustained)
    """
    side_l = (intended_side or "").lower()
    if side_l not in ("yes", "no"):
        return "neutral"
    if snapshot.inverse_trend_side == "none":
        return "neutral"

    # Is the absorption signal strong enough?
    if snapshot.inverse_trend_dollars < inverse_trend_min_usd:
        return "neutral"
    if snapshot.with_trend_dollars > 0:
        ratio = snapshot.inverse_trend_dollars / snapshot.with_trend_dollars
        if ratio < dominance_ratio:
            return "neutral"

    # Consistency: count large buys on the absorption side
    inv_large = (
        snapshot.yes_large_count
        if snapshot.inverse_trend_side == "yes"
        else snapshot.no_large_count
    )
    if inv_large < min_consistency_count:
        return "neutral"

    # Strong absorption flagged. Is our intended side aligned?
    if side_l == snapshot.inverse_trend_side:
        return "confirm"
    return "block"


# ── Exit-side detection (Phase 8b) ───────────────────────────────────────
# User: "massive volume to the opposite direction is also an indicator to
# exit." Symmetric to entry absorption but on a much shorter window. While
# entry looks at 5-min cumulative absorption, exit looks at 15-30s flow
# spikes — when smart money suddenly sells our side aggressively, the
# thesis is broken before the bid catches up.

ExitDecision = Literal["exit", "hold"]


@dataclass(frozen=True)
class ExitPressureSnapshot:
    holding_side: str               # "yes" or "no"
    opposite_side: str              # the OTHER side (where adverse flow lives)
    our_side_dollars: float         # buys on our side in window
    opposite_dollars: float         # buys on opposite side in window
    opposite_large_count: int       # # of opposite buys >= large_buy_usd
    sample_count: int


def compute_exit_pressure_snapshot(
    trades: Iterable[tuple],
    *,
    holding_side: str,
    now_ms: int,
    window_s: float = 30.0,
    large_buy_usd: float = 100.0,
) -> ExitPressureSnapshot:
    """Aggregate recent tape on each side from the holder's perspective.

    Trade format (from kalshi_tape): (ts_ms, side, count, yes_price_cents)
    where side is the BUY-aggressor side.

    For a holder of YES, the opposite side is NO; if NO buys spike, that
    means aggressors are buying NO = selling-pressure on YES = thesis-
    break signal.
    """
    side_l = (holding_side or "").lower()
    if side_l not in ("yes", "no"):
        # Defensive default — return all-zero snapshot
        return ExitPressureSnapshot(
            holding_side="", opposite_side="",
            our_side_dollars=0.0, opposite_dollars=0.0,
            opposite_large_count=0, sample_count=0,
        )
    opp_l = "no" if side_l == "yes" else "yes"
    cutoff_ms = now_ms - int(window_s * 1000)

    our_dollars = 0.0
    opp_dollars = 0.0
    opp_large = 0
    samples = 0

    for entry in trades:
        try:
            ts_ms, side, count, yes_px = entry
        except (ValueError, TypeError):
            continue
        ts_ms = int(ts_ms or 0)
        if ts_ms < cutoff_ms:
            continue
        s = (str(side) or "").lower()
        if s not in ("yes", "no"):
            continue
        dollars = trade_dollar_value(s, count, yes_px)
        if dollars <= 0:
            continue
        samples += 1
        if s == side_l:
            our_dollars += dollars
        elif s == opp_l:
            opp_dollars += dollars
            if dollars >= large_buy_usd:
                opp_large += 1

    return ExitPressureSnapshot(
        holding_side=side_l,
        opposite_side=opp_l,
        our_side_dollars=our_dollars,
        opposite_dollars=opp_dollars,
        opposite_large_count=opp_large,
        sample_count=samples,
    )


def evaluate_exit_signal(
    snapshot: ExitPressureSnapshot,
    *,
    massive_volume_usd: float = 300.0,
    dominance_ratio: float = 3.0,
    min_large_count: int = 2,
) -> ExitDecision:
    """Decide EXIT or HOLD based on opposite-side flow spike.

    Three conditions to flag exit:
      1. opposite_dollars >= massive_volume_usd  (real size, not noise)
      2. opposite_dollars >= dominance_ratio × our_side_dollars
                                                 (one-sided flow)
      3. opposite_large_count >= min_large_count (sustained, not single)

    Tighter than entry-confirm because exits are reactive — false-positives
    cost realised P&L rather than just a missed entry.
    """
    if not snapshot.holding_side or not snapshot.opposite_side:
        return "hold"
    if snapshot.opposite_dollars < massive_volume_usd:
        return "hold"
    if snapshot.opposite_large_count < min_large_count:
        return "hold"
    # Dominance check: only meaningful if our_side has *some* volume; if
    # our side is zero the ratio is infinite and the threshold check
    # above is sufficient.
    if snapshot.our_side_dollars > 0:
        ratio = snapshot.opposite_dollars / snapshot.our_side_dollars
        if ratio < dominance_ratio:
            return "hold"
    return "exit"
