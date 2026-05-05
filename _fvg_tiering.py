"""FVG tier classification + size computation (Level 3 sizing).

Pure module — no engine dependencies. Returns tier integer + sizing
recommendation given a feature dict. All math here is unit-testable in
isolation.

Tier definitions are derived from the OOS-validated 70/30 split of the
2,485-trade FVG signal universe (see scripts/backtest_oos_level3.py
output, 2026-05-04). Validation showed:

    Tier 1 (99% backtest fill, 97.4% OOS): late_300+ & aligned & far_10+
    Tier 2 (93% backtest fill, 92.2% OOS): late_300+ & aligned
    Tier 3 (91% backtest fill, 81.2% OOS): late_300+ alone (small n)
    Tier 4 (76% backtest fill, 75.0% OOS): late_180+ alone

Refuse predicates (Tier 0):
  - session_age < 180s         (early-window noise; ~66% of raw fires)
  - counter & 30-49c entry     (worst observed loser combo)
  - flat BTC & counter         (no edge)

Sizing fractions (Level 3 — full-Kelly aggressive):
  T1: 35% bankroll
  T2: 25%
  T3: 18%
  T4: 10%

The sizing fractions assume the per-trade MAX_TICKER_EXPOSURE_FRAC has
been raised to 0.40 in user_config to allow Tier 1's 35% slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# ── Tier configuration (do not edit without re-running OOS validation) ──


TIER_FRACTIONS: dict[int, float] = {
    1: 0.35,
    2: 0.25,
    3: 0.18,
    4: 0.10,
}

TIER_TP_OFFSETS_C: dict[int, int] = {
    1: 20,
    2: 15,
    3: 12,
    4: 12,
}

TIER_SL_OFFSET_C: int = 8

# Refuse predicates
MIN_SESSION_AGE_S: int = 180
COUNTER_REFUSE_ENTRY_LOW: int = 30
COUNTER_REFUSE_ENTRY_HIGH: int = 49
FLAT_BTC_THRESHOLD_DOLLARS: float = 20.0


# ── Feature dataclass for cleaner type signatures ──────────────────────


@dataclass
class FvgFeatures:
    session_age_s: int       # seconds since the 15m window opened
    aligned: bool            # FVG side == BTC 5min trend direction
    btc_dist_abs_pct: float  # absolute distance from strike, percent
    btc_5m_move: float       # signed dollar move over last 5 min
    entry_c: int             # entry price in cents on the bought side


# ── Tier classifier ─────────────────────────────────────────────────────


def classify_tier(features: FvgFeatures | Mapping) -> int:
    """Return integer tier in {1, 2, 3, 4} or 0 (refuse).

    Tiers are nested-exclusive: each trade gets exactly one tier — the
    most specific one matching its features. Tier 1 is most selective,
    Tier 4 is least.
    """
    if isinstance(features, Mapping):
        age = int(features.get("session_age_s", 0) or 0)
        aligned = bool(features.get("aligned", False))
        dist = float(features.get("btc_dist_abs_pct", 0.0) or 0.0)
        move = float(features.get("btc_5m_move", 0.0) or 0.0)
        entry = int(features.get("entry_c", 0) or 0)
    else:
        age = int(features.session_age_s)
        aligned = bool(features.aligned)
        dist = float(features.btc_dist_abs_pct)
        move = float(features.btc_5m_move)
        entry = int(features.entry_c)

    # Refuse predicates ────────────────────────────────────────────────
    if age < MIN_SESSION_AGE_S:
        return 0  # early-window noise
    if (not aligned) and COUNTER_REFUSE_ENTRY_LOW <= entry <= COUNTER_REFUSE_ENTRY_HIGH:
        return 0  # counter-trend midprice = worst combo
    if abs(move) < FLAT_BTC_THRESHOLD_DOLLARS and (not aligned):
        return 0  # flat BTC + counter alignment = no edge

    # Tier classification (most specific first) ────────────────────────
    if age >= 300 and aligned and dist >= 0.10:
        return 1
    if age >= 300 and aligned:
        return 2
    if age >= 300:
        return 3
    return 4  # 180 <= age < 300


# ── Size + price helpers ────────────────────────────────────────────────


def compute_size_contracts(
    tier: int,
    balance_cents: int,
    entry_c: int,
    *,
    max_exposure_frac: float = 0.40,
    min_contracts: int = 1,
    max_contracts_cap: int = 200,
) -> int:
    """Return contract count for ``tier`` given ``balance_cents`` and ``entry_c``.

    Returns 0 when tier == 0 (refused) or when inputs are invalid.

    The notional sized is ``balance_cents * TIER_FRACTIONS[tier]``,
    capped at ``balance_cents * max_exposure_frac`` (defends against a
    misconfigured tier fraction). Floored at ``min_contracts`` (so we
    never round to zero on a small bankroll); capped at
    ``max_contracts_cap`` (defends against runaway sizing).
    """
    if tier == 0 or tier not in TIER_FRACTIONS:
        return 0
    if entry_c <= 0:
        return 0
    if balance_cents <= 0:
        return 0
    frac = TIER_FRACTIONS[tier]
    notional_cents = balance_cents * frac
    notional_cents = min(notional_cents, balance_cents * max_exposure_frac)
    contracts = int(notional_cents / entry_c)
    if contracts < min_contracts:
        # Only allow min_contracts floor when bankroll can actually
        # afford it. If the floor's cost > balance × max_exposure_frac,
        # the trade is genuinely too small and we should refuse.
        floor_cost_cents = min_contracts * entry_c
        if floor_cost_cents > balance_cents * max_exposure_frac:
            return 0
        contracts = min_contracts
    contracts = min(contracts, max_contracts_cap)
    return contracts


def tier_tp_price(tier: int, entry_c: int) -> int:
    """Return TP target price in cents for the given tier."""
    if tier == 0:
        return entry_c + 8  # safe fallback
    offset = TIER_TP_OFFSETS_C.get(tier, 8)
    return entry_c + offset


def tier_sl_price(tier: int, entry_c: int) -> int:
    """Return SL trigger price in cents for the given tier.

    Same SL offset across all tiers (8c). The trigger is bid <=
    sl_price; cross-spread exit fires from there.
    """
    return max(1, entry_c - TIER_SL_OFFSET_C)


# ── Convenience: full sizing decision in one call ───────────────────────


@dataclass
class TieredSizing:
    tier: int
    contracts: int
    entry_c: int
    tp_price_c: int
    sl_price_c: int
    notional_cents: int
    rejection_reason: str = ""

    @property
    def fires(self) -> bool:
        return self.tier > 0 and self.contracts > 0


def compute_tiered_sizing(
    features: FvgFeatures | Mapping,
    balance_cents: int,
    entry_c: int,
    *,
    max_exposure_frac: float = 0.40,
    min_contracts: int = 1,
    max_contracts_cap: int = 200,
) -> TieredSizing:
    """Single-call helper: classify + size + compute TP/SL."""
    tier = classify_tier(features)
    if tier == 0:
        return TieredSizing(
            tier=0, contracts=0, entry_c=entry_c,
            tp_price_c=0, sl_price_c=0, notional_cents=0,
            rejection_reason="tier-0 refuse predicate hit",
        )
    contracts = compute_size_contracts(
        tier, balance_cents, entry_c,
        max_exposure_frac=max_exposure_frac,
        min_contracts=min_contracts,
        max_contracts_cap=max_contracts_cap,
    )
    if contracts == 0:
        return TieredSizing(
            tier=tier, contracts=0, entry_c=entry_c,
            tp_price_c=0, sl_price_c=0, notional_cents=0,
            rejection_reason=(
                f"size=0 (bal_c={balance_cents} entry_c={entry_c} "
                f"frac={TIER_FRACTIONS[tier]:.2f})"
            ),
        )
    return TieredSizing(
        tier=tier, contracts=contracts, entry_c=entry_c,
        tp_price_c=tier_tp_price(tier, entry_c),
        sl_price_c=tier_sl_price(tier, entry_c),
        notional_cents=contracts * entry_c,
    )
