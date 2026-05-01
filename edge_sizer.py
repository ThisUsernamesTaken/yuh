# =============================================================================
# edge_sizer.py — Edge-tier rapid-fire sizing (resurrection of Apr 15 engine)
# =============================================================================
# Reverse-engineered from 2026-04-15 engine_history.log which logged 4,140
# SIZING events, producing 352 Kalshi fills and $1,252 of buy volume in one
# day. That session made $1k+ profit. The sizing logic that produced it was
# removed/replaced some time between Apr 15 and Apr 17; this module restores
# the behaviour without regressing the stops/staircase/FVG work that landed
# today.
#
# Contract: callers pass raw market state (edge, vol, spread, velocity) and
# get back an EdgeSizing with a final `contracts` count + metadata. NO data
# is read from the local DB — every input is live Kalshi/Coinbase.
#
# Log-line format matches the Apr 15 legacy exactly so grep/forensics still
# work across the archaeology boundary:
#   "CopyEngine SIZING: HIGH_EDGE_NIGHT_BOOST2.2x path | 9 contracts | ..."
# =============================================================================

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# ── Night / weekend sizing regime ──────────────────────────────────────────
# 19:00-05:00 UTC on weekdays AND ALL of Saturday/Sunday are "SCALP" mode:
# HALF the base contracts. User direction 2026-04-17:
#   "Historically, weekends are entirely for retail, and overnight and
#    weekends should be when we're testing our scalping system. Not doing
#    the entire account into a trade. But still learning how to accurately
#    model volatility."
# Weekday 05:00-19:00 UTC = "FULL" mode: institutional hours, full boosts.
NIGHT_START_UTC_H = 19
NIGHT_END_UTC_H = 5   # exclusive: hour < 5 counts as night

# ── Edge tier thresholds (reverse-engineered from Apr 15 logs) ─────────────
# edge_cents = absolute Kalshi mid vs probability-fair-value spread in cents.
# Evidence: BASE logs showed edge 0-10c, MED edge 10-14c, HIGH edge ≥15c.
MIN_EDGE_CENTS_TO_FIRE = 5
MED_EDGE_MIN = 10
HIGH_EDGE_MIN = 15

# Base contracts per tier (from Apr 15 log: contracts = base × multiplier).
# HIGH day base = 8 (11@1.4x, 13@1.7x, 15@1.8x, 17@2.2x, 22@2.7x, 28@3.5x)
# HIGH night   = 4 (half day). MED day = 6, night = 3. BASE day = 4, night = 2.
BASE_CONTRACTS_DAY = {"HIGH": 8, "MED": 6, "BASE": 4}
BASE_CONTRACTS_NIGHT = {"HIGH": 4, "MED": 3, "BASE": 2}

# ── Boost multipliers (observed on Apr 15) ─────────────────────────────────
# 1.4 / 1.7 / 1.8 / 2.2 / 2.7 / 3.5 — driven by spread primarily, with an
# edge-extreme kicker at edge ≥ 50c that forces 3.5x regardless of spread.
# The original code isn't recoverable (April-15 deploy was a compiled .exe),
# so this is best-fit inference from observed (edge, spread, multiplier)
# samples. Rationale: spread is the direct profit-potential proxy — wider
# spread = more room for the staircase TPs to fill before expiry.


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_scalp_mode(ts_utc: Optional[datetime] = None) -> tuple[bool, bool, bool]:
    """Return (is_scalp_mode, is_night_hours, is_weekend).

    Scalp mode = night_hours OR weekend. User confirmed 2026-04-17:
    weekends stay in scalp mode (no institutional flow, retail-only).
    """
    ts = ts_utc or _now_utc()
    hr = ts.hour
    wd = ts.weekday()  # 0=Mon .. 6=Sun
    is_weekend = wd >= 5
    is_night = hr >= NIGHT_START_UTC_H or hr < NIGHT_END_UTC_H
    return (is_night or is_weekend), is_night, is_weekend


def classify_edge_tier(edge_cents: float) -> str:
    """Return 'HIGH' / 'MED' / 'BASE' / 'NONE'."""
    e = abs(int(round(float(edge_cents))))
    if e >= HIGH_EDGE_MIN:
        return "HIGH"
    if e >= MED_EDGE_MIN:
        return "MED"
    if e >= MIN_EDGE_CENTS_TO_FIRE:
        return "BASE"
    return "NONE"


def compute_multiplier(spread_cents: int, edge_cents: float, vol_pct: float) -> float:
    """Spread-driven boost multiplier with extreme-edge kicker.

    Returns one of 1.4 / 1.7 / 1.8 / 2.2 / 2.7 / 3.5 (matching Apr 15 log).
    """
    sp = abs(int(spread_cents))
    ed = abs(float(edge_cents))

    # Extreme-edge kicker: edge ≥ 50c always maxes out (observed Apr 15
    # 3.5x fires at edge 55-67c). The market is mis-pricing by a lot —
    # stack.
    if ed >= 50:
        return 3.5

    # Spread-driven tier:
    if sp >= 50:
        mult = 3.5
    elif sp >= 40:
        mult = 2.7
    elif sp >= 28:
        mult = 2.2
    elif sp >= 20:
        mult = 1.8
    elif sp >= 14:
        mult = 1.7
    else:
        mult = 1.4

    # High volatility regime (vol ≥ 60%) bumps the multiplier one notch —
    # observed: many 2.2x fires at vol 52-60%. Volatile markets swing further
    # past fair value before correcting, so bigger bets justified.
    if vol_pct >= 60.0 and mult < 3.5:
        bump_ladder = [1.4, 1.7, 1.8, 2.2, 2.7, 3.5]
        try:
            i = bump_ladder.index(mult)
            mult = bump_ladder[min(i + 1, len(bump_ladder) - 1)]
        except ValueError:
            pass

    return mult


@dataclass
class EdgeSizing:
    """Result of a sizing call. Matches Apr 15 log format for grep-ability."""
    edge_tier: str                 # HIGH / MED / BASE / NONE
    edge_cents: int
    vol_pct: float
    spread_cents: int
    velocity: float
    is_scalp_mode: bool            # night OR weekend
    is_night: bool
    is_weekend: bool
    base_contracts: int
    multiplier: float
    contracts: int                 # final contract count AFTER price-inverse taper
    contracts_pre_px: int          # before price-inverse taper (for logging)
    target_dollars: float
    px_mult: float                 # price-inverse multiplier (1.00 at cheap, 0.15 at 75c+)
    ask_cents: int
    log_tag: str                   # "HIGH_EDGE_NIGHT_BOOST2.2x" (Apr 15 format)
    skip_reason: str = ""

    def as_log_line(self) -> str:
        if self.skip_reason:
            return (f"CopyEngine SIZING SKIP: {self.skip_reason} "
                    f"(tier={self.edge_tier} edge={self.edge_cents}c "
                    f"spread={self.spread_cents}c ask={self.ask_cents}c)")
        return (
            f"CopyEngine SIZING: {self.log_tag} path | {self.contracts} contracts | "
            f"vol={self.vol_pct:.1f}% vel=${self.velocity:+.1f}/s "
            f"edge={self.edge_cents}c spread={self.spread_cents}c | target ${self.target_dollars:.2f}"
            + (f" (px_mult={self.px_mult:.2f}@{self.ask_cents}c pre_px={self.contracts_pre_px})"
               if self.px_mult < 1.0 else "")
        )


def price_inverse_multiplier(ask_cents: int) -> float:
    """Price-inverse taper: large at cheap, tiny at expensive.

    Shipped 2026-04-17 morning per user direction:
      "The cheaper, the larger we size."
    Target April 15 pattern: ~5ct @ 71c was the profitable maximum.
    """
    ask = int(ask_cents)
    if ask >= 75:
        return 0.15
    if ask >= 70:
        return 0.25
    if ask >= 65:
        return 0.50
    if ask >= 55:
        return 0.80
    return 1.00


# ── CONVICTION DIP tier (Refinement C, 2026-04-20) ────────────────────────
# Manual-clone size tier. User's 2026-04-19 manual BTC wins shared a signature:
# YES ask drops to <50c, microstructure pressure is strongly on the YES side
# (composite score >= 0.50, confidence >= 0.65), user clicks market for 50-240
# contracts. One such click captured +$100 on APR201500-00 at $0.44. The
# engine saw the same setup (SIGNAL: YES conv=85% src=microstructure) but
# sized 4-8 contracts from the standard edge-tier path. CONVICTION_DIP
# mirrors the manual sizing: ~60ct day / ~30ct scalp when the gate hits.
# The tier OVERRIDES the edge-tier base+multiplier if-and-only-if the
# resulting contract count would be larger, so HIGH_EDGE stacks still win
# when they legitimately call for more size.
CONVICTION_DIP_BASE_DAY = 60
CONVICTION_DIP_BASE_NIGHT = 30
CONVICTION_DIP_PRESSURE_MIN = 0.50          # |score| >= this
CONVICTION_DIP_CONFIDENCE_MIN = 0.65
CONVICTION_DIP_MAX_ASK = 50                  # ask < this triggers tier


def compute_sizing(
    edge_cents: float,
    vol_pct: float,
    velocity: float,
    spread_cents: int,
    ask_cents: int,
    balance_dollars: float = 0.0,
    ts_utc: Optional[datetime] = None,
    pressure_score_for_side: float = 0.0,
    pressure_confidence: float = 0.0,
) -> EdgeSizing:
    """Main entry point. Returns an EdgeSizing.

    Inputs are all live-Kalshi/Coinbase derived — no DB reads.
      edge_cents:    |Kalshi_mid - fair_value_cents| from prob engine
      vol_pct:       1-minute realized vol (from price_feed.prob_engine.volatility)
      velocity:      BTC tick velocity $/s (from price_feed.tick_tracker)
      spread_cents:  Kalshi bid-ask spread (from live orderbook)
      ask_cents:     current Kalshi ask (for price-inverse taper)
      balance_dollars: for affordability cap only — NEVER read from DB
      pressure_score_for_side: PressureScore.score oriented so +ve agrees
                               with the signal side (caller flips sign for NO).
                               Feeds the CONVICTION_DIP tier only.
      pressure_confidence: PressureScore.confidence (0-1). Gates CONVICTION_DIP.
    """
    tier = classify_edge_tier(edge_cents)
    scalp, is_night, is_weekend = is_scalp_mode(ts_utc)

    if tier == "NONE":
        return EdgeSizing(
            edge_tier="NONE", edge_cents=int(abs(edge_cents)),
            vol_pct=float(vol_pct), spread_cents=int(spread_cents),
            velocity=float(velocity), is_scalp_mode=scalp,
            is_night=is_night, is_weekend=is_weekend,
            base_contracts=0, multiplier=0.0, contracts=0, contracts_pre_px=0,
            target_dollars=0.0, px_mult=1.0, ask_cents=int(ask_cents),
            log_tag="NONE",
            skip_reason=f"edge {int(abs(edge_cents))}c below min {MIN_EDGE_CENTS_TO_FIRE}c",
        )

    base_map = BASE_CONTRACTS_NIGHT if scalp else BASE_CONTRACTS_DAY
    base = base_map[tier]
    mult = compute_multiplier(spread_cents, edge_cents, vol_pct)
    raw_contracts = int(round(base * mult))

    # Price-inverse layer — shrink size at expensive asks so 70c+ entries
    # never become catastrophic when wrong.
    px_mult = price_inverse_multiplier(ask_cents)
    contracts = max(1, int(round(raw_contracts * px_mult)))

    # ── CONVICTION DIP override (Refinement C) ─────────────────────────
    # When pressure is strongly on our side AND ask has dipped below 50c,
    # upsize to the manual-clone tier. Only overrides if the dip tier
    # produces MORE contracts than the edge-tier path — HIGH_EDGE stacks
    # (e.g. 28ct at BOOST3.5x) are allowed to exceed 60ct legitimately.
    _dip_fires = (
        pressure_score_for_side >= CONVICTION_DIP_PRESSURE_MIN
        and pressure_confidence >= CONVICTION_DIP_CONFIDENCE_MIN
        and 0 < ask_cents < CONVICTION_DIP_MAX_ASK
    )
    if _dip_fires:
        _dip_base = CONVICTION_DIP_BASE_NIGHT if scalp else CONVICTION_DIP_BASE_DAY
        _dip_px = price_inverse_multiplier(ask_cents)  # 1.00 at <55c
        _dip_contracts = max(1, int(round(_dip_base * _dip_px)))
        if _dip_contracts > contracts:
            contracts = _dip_contracts
            raw_contracts = _dip_base
            base = _dip_base
            mult = 1.0
            tier = "CONVICTION_DIP"
            px_mult = _dip_px

    # Affordability cap — never commit more than the balance allows, with a
    # 50c buffer. Balance comes from live Kalshi `get_balance()`.
    if balance_dollars > 0 and ask_cents > 0:
        max_afford = int((balance_dollars - 0.50) * 100 / ask_cents)
        if max_afford < 1:
            return EdgeSizing(
                edge_tier=tier, edge_cents=int(abs(edge_cents)),
                vol_pct=float(vol_pct), spread_cents=int(spread_cents),
                velocity=float(velocity), is_scalp_mode=scalp,
                is_night=is_night, is_weekend=is_weekend,
                base_contracts=base, multiplier=mult,
                contracts=0, contracts_pre_px=raw_contracts,
                target_dollars=0.0, px_mult=px_mult, ask_cents=int(ask_cents),
                log_tag="AFFORD_ZERO",
                skip_reason=f"insufficient balance ${balance_dollars:.2f} for 1ct @ {ask_cents}c",
            )
        contracts = min(contracts, max_afford)

    target = contracts * spread_cents / 100.0

    # Log tag — Apr 15 format: TIER_EDGE_[NIGHT_]BOOST{mult}x
    # Weekend treated as NIGHT for log tag (same base+behavior).
    # CONVICTION_DIP uses its own tag format — not a BOOST multiplier,
    # it's a baseline override keyed to pressure + cheap-ask confluence.
    epoch = "NIGHT" if is_night else ("WKND" if is_weekend else "")
    if tier == "CONVICTION_DIP":
        log_tag = f"CONVICTION_DIP_{epoch + '_' if epoch else ''}{base}ct@{int(ask_cents)}c"
    else:
        log_tag = f"{tier}_EDGE_{epoch + '_' if epoch else ''}BOOST{mult}x"

    return EdgeSizing(
        edge_tier=tier, edge_cents=int(abs(edge_cents)),
        vol_pct=float(vol_pct), spread_cents=int(spread_cents),
        velocity=float(velocity), is_scalp_mode=scalp,
        is_night=is_night, is_weekend=is_weekend,
        base_contracts=base, multiplier=mult,
        contracts=contracts, contracts_pre_px=raw_contracts,
        target_dollars=target, px_mult=px_mult, ask_cents=int(ask_cents),
        log_tag=log_tag,
    )
