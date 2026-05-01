"""ATM Reversion / Strike-Pin Reversion strategy.

Per Codex handoff 2026-04-25 + sweep-validated config:

  When BTC is within 0.030% of the contract strike, Kalshi prices can briefly
  dislocate away from a 50/50 fair value. Buy the side that's significantly
  under fair (discount mode), harvest repricing via limit exit at +8c above
  entry or 49c absolute.

Sweep results (30 days, 1202 trades):
  WR=59.9%, +5.61c avg gross, +3.21c net after Kalshi taker fees both legs.
  MaxDD ~$611 at 100ct sizing.

Bias-mode entries (slight off-strike YES/NO bias) are PROVEN BREAKEVEN
after fees in the same backtest. Disabled by default.

This module is decision-only. The engine wires it into the live cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


STRATEGY_LABEL = "ATM_REVERSION_DISCOUNT"


@dataclass
class AtmContext:
    """Inputs the strategy needs at signal-evaluation time."""
    btc_price: float
    strike: float
    yes_bid_c: int
    yes_ask_c: int
    fair_yes_c: float = 50.0  # ATM default


@dataclass
class AtmEntry:
    """Entry decision returned by evaluate()."""
    side: str          # "yes" or "no"
    entry_c: int
    edge_c: float
    kind: str          # "discount" (only one supported by default)
    strike_dist_pct: float


@dataclass
class AtmExitDecision:
    should_exit: bool
    reason: str = ""


def kalshi_taker_fee_c(price_c: float) -> float:
    """Kalshi taker fee: 7 * p * (1-p) cents per contract."""
    p = max(0.0, min(1.0, price_c / 100.0))
    return 7.0 * p * (1.0 - p)


def evaluate(
    ctx: AtmContext,
    *,
    max_strike_dist_pct: float,
    max_entry_c: int,
    min_fair_c: float,
    min_edge_c: float,
    bias_enabled: bool = False,
    max_bias_entry_c: int = 62,
    min_bias_edge_c: float = 1.0,
) -> Optional[AtmEntry]:
    """Return AtmEntry if a discount setup is live, else None.

    Discount: side ASK <= max_entry_c AND fair >= min_fair_c AND edge >= min_edge_c.
    Bias (optional): off-strike side under fair by min_bias_edge_c.
    """
    if ctx.strike <= 0 or ctx.btc_price <= 0:
        return None
    strike_dist_pct = (ctx.btc_price - ctx.strike) / ctx.strike * 100.0
    if abs(strike_dist_pct) > max_strike_dist_pct:
        return None

    yes_bid = ctx.yes_bid_c
    yes_ask = ctx.yes_ask_c
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask >= 100:
        return None
    # Note: Kalshi's WS book exposes YES ask as 100 - best NO bid. During
    # fast strike-pin dislocations, YES bid can exceed this derived YES ask.
    # That is a crossed/complementary market state, and it is exactly the
    # high-delta scalp pattern we are trying to observe. Do not reject it
    # here; status/reporting must flag it separately until live execution
    # verifies that both legs are practically fillable.

    no_ask = 100 - yes_bid
    if no_ask <= 0:
        return None
    fair_yes = ctx.fair_yes_c
    fair_no = 100.0 - fair_yes

    yes_edge = fair_yes - yes_ask
    no_edge = fair_no - no_ask

    # Discount priority — strongest edge wins
    yes_disc = (yes_ask <= max_entry_c
                and fair_yes >= min_fair_c
                and yes_edge >= min_edge_c)
    no_disc = (no_ask <= max_entry_c
               and fair_no >= min_fair_c
               and no_edge >= min_edge_c)

    if yes_disc and (not no_disc or yes_edge >= no_edge):
        return AtmEntry(side="yes", entry_c=yes_ask, edge_c=yes_edge,
                        kind="discount", strike_dist_pct=strike_dist_pct)
    if no_disc:
        return AtmEntry(side="no", entry_c=no_ask, edge_c=no_edge,
                        kind="discount", strike_dist_pct=strike_dist_pct)

    if not bias_enabled:
        return None
    # Bias mode (paper-only by default, breakeven after fees)
    if strike_dist_pct > 0:
        edge = fair_yes - yes_ask
        if yes_ask <= max_bias_entry_c and edge >= min_bias_edge_c:
            return AtmEntry(side="yes", entry_c=yes_ask, edge_c=edge,
                            kind="bias", strike_dist_pct=strike_dist_pct)
    elif strike_dist_pct < 0:
        edge = fair_no - no_ask
        if no_ask <= max_bias_entry_c and edge >= min_bias_edge_c:
            return AtmEntry(side="no", entry_c=no_ask, edge_c=edge,
                            kind="bias", strike_dist_pct=strike_dist_pct)
    return None


def evaluate_exit(
    *,
    side: str,
    entry_c: int,
    current_yes_bid_c: int,
    current_yes_ask_c: int,
    btc_price: float,
    strike: float,
    age_s: float,
    target_c: int,
    profit_target_c: int,
    stop_strike_dist_pct: float,
    force_exit_age_s: float,
) -> AtmExitDecision:
    """Decide whether to exit a paper ATM position.

    Priority (first match wins):
      1. Same-side bid >= target_c (49c absolute)
      2. Same-side bid >= entry_c + profit_target_c (+8c)
      3. |strike_dist| >= stop_strike_dist_pct (BTC escaped)
      4. age_s >= force_exit_age_s
    Returns AtmExitDecision with should_exit + reason.
    """
    if side == "yes":
        side_bid = current_yes_bid_c
    else:
        side_bid = 100 - current_yes_ask_c
    if side_bid >= target_c:
        return AtmExitDecision(True, f"target_{target_c}")
    if side_bid >= entry_c + profit_target_c:
        return AtmExitDecision(True, f"profit_{profit_target_c}")
    if strike > 0 and btc_price > 0:
        strike_dist_pct = (btc_price - strike) / strike * 100.0
        if abs(strike_dist_pct) >= stop_strike_dist_pct:
            return AtmExitDecision(True, "strike_escape")
    if age_s >= force_exit_age_s:
        return AtmExitDecision(True, "time")
    return AtmExitDecision(False)
