"""Pure Brownian-Bridge-anchored signal + sizing.

Founding philosophy implementation, isolated from engine state. Pure math.
No async, no I/O, no globals. Trivially unit-testable.

Inputs the caller supplies:
    market_mid_cents:   current YES mid in cents (= market-implied YES %)
    fair_yes_cents:     BB model's fair YES price in cents (already factors
                        in time-to-expiry, distance-to-strike, volatility)
    seconds_to_expiry:  time remaining in window (s)
    balance_dollars:    current account balance for sizing
    config:             dict of knobs (see CONFIG section below)

Output:
    BBSignal | None — None means no qualifying mispricing this cycle.

The BB model's `fair_yes_cents` already encodes the founding doc's math
(Normal CDF via Abramowitz & Stegun erf). This module's job is just:
    1. Measure mispricing magnitude (fair vs market)
    2. Pick the underpriced side
    3. Filter on edge / entry-price / time-to-expiry
    4. Size by Kelly on the implied edge
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class BBSignal:
    side: str                       # "yes" or "no"
    edge_pp: float                  # mispricing magnitude (pp = percentage points)
    fair_yes_cents: int             # cached for protective-order use
    market_mid_cents: int
    suggested_entry_cents: int      # cheap side's price (= entry target)
    win_probability: float          # model probability we win, 0.0-1.0
    kelly_fraction: float           # signed fraction of bankroll
    contracts: int                  # rounded position size
    seconds_to_expiry: float
    reason: str                     # diagnostic for logs / DB
    conviction_tier: int = 1        # 1=clean, 2=high, 3=extreme
    # 2026-05-03 Option C: alignment classification of the BB cheap side
    # vs BTC 5-min trend direction. Set by the caller after bb_evaluate
    # returns; pure module doesn't see BTC trend. Values:
    #   "aligned"      — cheap side matches BTC trend (with-trend bet)
    #   "contrarian"   — cheap side opposes BTC trend (counter-trend bet)
    #   "neutral"      — BTC trend within dead zone, no clear direction
    #   "aligned_fallback" — synthesized by the alignment-fallback tier
    #                        when BB had no contrarian setup
    alignment: str = "neutral"


def classify_alignment(
    side: str,
    btc_5m_change_usd: float,
    dead_zone_usd: float = 20.0,
) -> str:
    """Classify a BB cheap-side signal as aligned/contrarian/neutral.

    Pure helper, no I/O. Used by the engine's alignment gate (option B)
    and by the alignment-fallback tier (option A) shipped 2026-05-03.

    Args:
        side: "yes" or "no" — the side BB_PURE wants to buy (cheap side).
        btc_5m_change_usd: BTC's 5-minute price change in $.
            Positive = BTC rising = YES is the with-trend side.
            Negative = BTC falling = NO is the with-trend side.
        dead_zone_usd: |change| ≤ this is treated as no clear trend.
            Default $20, mirrors `BB_PURE_TAPE_BTC_DEAD_ZONE_USD`.

    Returns:
        "aligned" if `side` matches BTC trend direction.
        "contrarian" if `side` opposes BTC trend direction.
        "neutral" if |btc_5m_change_usd| ≤ dead_zone_usd.
    """
    if abs(float(btc_5m_change_usd)) <= float(dead_zone_usd):
        return "neutral"
    s = (side or "").lower()
    if btc_5m_change_usd > 0:  # BTC up — YES is with-trend
        return "aligned" if s == "yes" else "contrarian"
    else:  # BTC down — NO is with-trend
        return "aligned" if s == "no" else "contrarian"


def aligned_side_from_btc(
    btc_5m_change_usd: float,
    dead_zone_usd: float = 20.0,
) -> Optional[str]:
    """Return the trend-aligned side ("yes"/"no") or None if neutral.

    Companion helper to `classify_alignment` for the alignment-fallback
    tier: when BB_PURE has no contrarian setup, the fallback fires on
    the with-trend side returned here.
    """
    if abs(float(btc_5m_change_usd)) <= float(dead_zone_usd):
        return None
    return "yes" if btc_5m_change_usd > 0 else "no"


def build_alignment_fallback_signal(
    side: str,
    market_mid_cents: int,
    seconds_to_expiry: float,
    balance_dollars: float,
    config: dict,
) -> Optional[BBSignal]:
    """Synthesize a BBSignal for the alignment-fallback tier.

    Used when BB_PURE found no contrarian mispricing but the user wants
    to fire WITH the BTC trend anyway (option A). No fair-value math —
    this tier bets that directional drift continues. Pure module so it
    can be unit-tested without engine state.

    Args:
        side: "yes" or "no" — pre-computed trend-aligned side.
        market_mid_cents: current YES mid in cents.
        seconds_to_expiry: window time remaining in seconds.
        balance_dollars: account balance for sizing.
        config: dict with keys
            "max_entry_cents", "min_entry_cents", "min_time_remaining_s",
            "kelly_fraction", "kelly_max_frac", "max_contracts".

    Returns:
        A BBSignal with `alignment="aligned_fallback"`, `conviction_tier=1`,
        and a small fixed-fraction bet sized off entry price; or None if
        gates fail (price out of band, time too short, etc.).
    """
    s = (side or "").lower()
    if s not in ("yes", "no"):
        return None
    if (market_mid_cents <= 0 or market_mid_cents >= 100):
        return None
    if seconds_to_expiry <= 0 or balance_dollars <= 0:
        return None
    min_time = float(config.get("min_time_remaining_s", 60.0))
    if seconds_to_expiry < min_time:
        return None
    max_entry = int(config.get("max_entry_cents", 55))
    min_entry = int(config.get("min_entry_cents", 5))
    if s == "yes":
        entry_cents = int(market_mid_cents)
    else:
        entry_cents = int(100 - market_mid_cents)
    if entry_cents < min_entry or entry_cents > max_entry:
        return None
    if entry_cents >= 99:
        return None
    # Sizing: fixed fraction of balance scaled by entry price (no Kelly —
    # there is no edge to compute against). Conservative by design: this
    # is a "no contrarian setup, follow trend" fallback, not a thesis bet.
    kelly_frac = float(config.get("kelly_fraction", 0.10))
    kelly_max = float(config.get("kelly_max_frac", 0.05))
    bet_frac = max(0.0, min(kelly_frac, kelly_max))
    if bet_frac <= 0:
        return None
    bet_dollars = bet_frac * float(balance_dollars)
    max_contracts = int(config.get("max_contracts", 200))
    contracts = int(round(bet_dollars / max(entry_cents / 100.0, 0.01)))
    contracts = max(1, min(contracts, max_contracts))
    # Win probability proxied by entry price (= market-implied) — the
    # protective layer needs a value but won't use this for math since
    # `alignment="aligned_fallback"` flags this as a non-edge bet.
    win_p = float(entry_cents) / 100.0
    return BBSignal(
        side=s,
        edge_pp=0.0,
        fair_yes_cents=int(entry_cents if s == "yes" else 100 - entry_cents),
        market_mid_cents=int(market_mid_cents),
        suggested_entry_cents=entry_cents,
        win_probability=win_p,
        kelly_fraction=bet_frac,
        contracts=contracts,
        seconds_to_expiry=float(seconds_to_expiry),
        reason=(
            f"alignment_fallback side={s} entry={entry_cents}c "
            f"frac={bet_frac:.4f} contracts={contracts}"
        ),
        conviction_tier=1,
        alignment="aligned_fallback",
    )


def evaluate(
    market_mid_cents: int,
    fair_yes_cents: int,
    seconds_to_expiry: float,
    balance_dollars: float,
    config: dict,
) -> Optional[BBSignal]:
    """Return a BBSignal if mispricing exceeds threshold, else None.

    Knobs read from `config`:
        min_edge_pp:        minimum pp mispricing to fire (default 8.0)
        max_entry_cents:    don't enter above this price (default 70)
        min_entry_cents:    don't fire on dust prices (default 5)
        min_time_remaining_s:  don't fire too close to expiry (default 60)
        kelly_fraction:     base fraction of full Kelly to use (default 0.25)
        kelly_max_frac:     hard ceiling on bankroll % per position (default 0.15)
        max_contracts:      contract-count cap (default 200)
    """
    # ── Input validation ──────────────────────────────────────────────
    if (market_mid_cents <= 0 or market_mid_cents >= 100
            or fair_yes_cents <= 0 or fair_yes_cents >= 100):
        return None
    if seconds_to_expiry <= 0:
        return None

    min_edge_pp = float(config.get("min_edge_pp", 8.0))
    max_entry = int(config.get("max_entry_cents", 70))
    min_entry = int(config.get("min_entry_cents", 5))
    min_time = float(config.get("min_time_remaining_s", 60.0))
    kelly_frac = float(config.get("kelly_fraction", 0.25))
    kelly_max = float(config.get("kelly_max_frac", 0.15))
    max_contracts = int(config.get("max_contracts", 200))
    # Fee-aware dynamic edge threshold (2026-05-03)
    # Kalshi fee ≈ 0.07 × P × (1−P) × 100¢ per contract (bell-shaped, peaks at 50c).
    # Round-trip breakeven edge_pp = 2 × fee = 0.14 × P × (1−P).
    # We require K × breakeven margin of safety, floored at FLOOR_PP.
    # When disabled, falls back to the static `min_edge_pp` floor.
    fee_aware_enabled = bool(config.get("fee_aware_edge_enabled", False))
    fee_aware_mult = float(config.get("fee_aware_edge_mult", 2.0))
    fee_aware_floor = float(config.get("fee_aware_edge_floor_pp", 4.0))

    # Conviction-tier sizing thresholds (2026-05-01 INVERTED late PM)
    # Original framing: extreme-confidence trades (fair >= 95c) are the
    # most reliable, so size up. Reality from 2026-05-01 trading data:
    # extreme-fair trades are the engine's WORST performers.
    #   Fair >= 95c or <= 5c: 4 trades → -$275 net (1 win, 3 losses,
    #     with -$252, -$83 catastrophes)
    #   Fair 85-94c or 6-15c: 5 trades → -$522 net, 0% hit rate
    #   Fair 50-84c moderate: 75% hit rate, small consistent wins
    # The Brownian-Bridge model overshoots after fast BTC moves; its
    # "fair=95c" is actually "BTC just rallied hard, model expects
    # continuation". But fast moves are exactly when reversals happen.
    # NEW: at extreme fair, SHRINK the Kelly cap rather than expand.
    # Mild fair (model uncertain) gets the largest size because that's
    # where the model is most calibrated.
    t2_min_edge = float(config.get("kelly_tier2_min_edge_pp", 25.0))
    t2_min_extreme = float(config.get("kelly_tier2_min_fair_extreme", 85.0))
    t2_kelly_max = float(config.get("kelly_tier2_max_frac", 0.10))
    t3_min_edge = float(config.get("kelly_tier3_min_edge_pp", 40.0))
    t3_min_extreme = float(config.get("kelly_tier3_min_fair_extreme", 95.0))
    t3_kelly_max = float(config.get("kelly_tier3_max_frac", 0.05))

    # ── Time gate ─────────────────────────────────────────────────────
    if seconds_to_expiry < min_time:
        return None

    # ── Mispricing direction ──────────────────────────────────────────
    # market_mid is the market-implied YES probability in pp.
    # fair_yes_cents is the model-implied YES probability in pp.
    # If fair > market: YES is underpriced (model says it's more likely
    # than the market thinks) → BUY YES.
    # If fair < market: NO is underpriced (model says YES is less likely
    # than market) → BUY NO.
    edge_yes_pp = float(fair_yes_cents) - float(market_mid_cents)
    # Static-threshold pre-gate: cheap rejection of obvious zero-edge cases.
    # Even with fee-aware enabled, we keep a small fixed floor (1pp) so
    # numerical noise around fair=mid doesn't trigger eval. The real gate
    # is computed below after entry_cents is known.
    if abs(edge_yes_pp) < min(1.0, min_edge_pp):
        return None

    if edge_yes_pp > 0:
        side = "yes"
        # Buying YES at market_mid; win probability = fair_yes / 100
        entry_cents = int(market_mid_cents)
        win_p = fair_yes_cents / 100.0
    else:
        side = "no"
        # Buying NO at (100 - market_mid); win probability = 1 - fair_yes / 100
        entry_cents = int(100 - market_mid_cents)
        win_p = 1.0 - (fair_yes_cents / 100.0)

    # ── Entry-price gates ─────────────────────────────────────────────
    if entry_cents > max_entry:
        return None
    if entry_cents < min_entry:
        return None

    # ── Edge gate (static or fee-aware dynamic) ───────────────────────
    if fee_aware_enabled:
        # Kalshi-fee-derived breakeven edge per round-trip.
        # fee_per_contract = 0.07 × P × (1−P) × 100¢   (P in [0,1])
        # round_trip_breakeven_pp = 2 × fee = 0.14 × p × (100−p)/100
        p = float(entry_cents)
        breakeven_pp = 0.14 * p * (100.0 - p) / 100.0
        effective_edge_floor = max(fee_aware_floor, fee_aware_mult * breakeven_pp)
    else:
        effective_edge_floor = min_edge_pp
    if abs(edge_yes_pp) < effective_edge_floor:
        return None

    # ── Kelly sizing ──────────────────────────────────────────────────
    # Pay entry_cents to win 100c gross.
    # Net profit per unit invested if win: (100 - entry) / entry = b
    # Full Kelly = (p * b - q) / b  where q = 1 - p
    #            = p - q / b
    #            = p - (1 - p) * entry / (100 - entry)
    if entry_cents >= 99:
        return None  # no payoff
    b = (100.0 - entry_cents) / entry_cents
    q = 1.0 - win_p
    full_kelly = (win_p * b - q) / b if b > 0 else 0.0
    if full_kelly <= 0:
        # Shouldn't happen if mispricing is on the right side, but guard
        return None
    fractional_kelly = full_kelly * kelly_frac

    # ── Conviction-tier resolution ─────────────────────────────────────
    # `fair_extremity` = how far from 50/50 the model's fair price is.
    # Tier 2: edge >= 25pp AND fair >= 85c (or fair <= 15c on NO side)
    # Tier 3: edge >= 40pp AND fair >= 95c (or fair <= 5c on NO side)
    # Side already chose the cheap side; fair_extremity uses the
    # higher of (fair_yes, 100 - fair_yes).
    fair_extremity = max(float(fair_yes_cents), 100.0 - float(fair_yes_cents))
    abs_edge = abs(edge_yes_pp)
    if abs_edge >= t3_min_edge and fair_extremity >= t3_min_extreme:
        conviction_tier = 3
        active_kelly_max = t3_kelly_max
    elif abs_edge >= t2_min_edge and fair_extremity >= t2_min_extreme:
        conviction_tier = 2
        active_kelly_max = t2_kelly_max
    else:
        conviction_tier = 1
        active_kelly_max = kelly_max
    fractional_kelly = max(0.0, min(fractional_kelly, active_kelly_max))

    # ── Contract count ────────────────────────────────────────────────
    if balance_dollars <= 0:
        return None
    bet_dollars = fractional_kelly * balance_dollars
    contracts = int(round(bet_dollars / max(entry_cents / 100.0, 0.01)))
    contracts = max(1, min(contracts, max_contracts))

    return BBSignal(
        side=side,
        edge_pp=abs(edge_yes_pp),
        fair_yes_cents=int(fair_yes_cents),
        market_mid_cents=int(market_mid_cents),
        suggested_entry_cents=entry_cents,
        win_probability=win_p,
        kelly_fraction=fractional_kelly,
        contracts=contracts,
        seconds_to_expiry=seconds_to_expiry,
        reason=(
            f"edge={abs(edge_yes_pp):.1f}pp fair={fair_yes_cents}c "
            f"market={market_mid_cents}c side={side} entry={entry_cents}c "
            f"p_win={win_p:.3f} kelly={fractional_kelly:.4f} "
            f"contracts={contracts} tier={conviction_tier}"
        ),
        conviction_tier=conviction_tier,
    )
