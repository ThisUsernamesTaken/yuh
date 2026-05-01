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
    if abs(edge_yes_pp) < min_edge_pp:
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
    fractional_kelly = max(0.0, min(fractional_kelly, kelly_max))

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
            f"contracts={contracts}"
        ),
    )
