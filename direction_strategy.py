"""Direction-following strategy: pure module.

Backtest 2026-05-05 (197 settled markets, scripts/backtest_direction.py):
  Sign-aligned (BTC past strike + 5-min momentum agrees), no entry-price
  cap, entry before minute 10:
    - 132 trades, 68.9% win rate, +$2.07 mean P&L, +$273.58 corpus total
    - $50 BR -> $323.58 with flat 10ct, NO drawdown below $50
  With dist > 0.10% from strike (the sweet spot):
    - 84 trades, 90.5% win rate, +$3.94 mean P&L, +$330 corpus total

Thesis (the user's framing): "buy whichever direction BTC is moving."
Once BTC is meaningfully past the strike with momentum agreeing, it tends
to stay there for the remaining ~14 minutes. The market mid roughly
prices in the right probability, but the realized rate exceeds market-
implied rate (settlement persistence > momentum-decay assumption baked
into the mid). We pay the ask and hold to settlement.

Decision logic:
  1. Compute distance: dist_pct = (btc_price - strike) / strike
  2. Require |dist_pct| >= dist_threshold_pct (default 0.0010 = 0.10%)
  3. Require sign agreement: dist > 0 AND momentum >= +threshold (YES)
                          OR dist < 0 AND momentum <= -threshold (NO)
  4. If both conditions hold, return DirectionSignal with chosen side.
  5. Otherwise return None (skip this evaluation).

Pure module — no engine deps, only stdlib. Decision math is fully
unit-testable in isolation. Engine integration adds: live book
read for ask price, position-size calc, place_order with IOC taker,
per-window ticker lock, daily-loss halt.

Entry execution (engine layer):
  - place_order(action="buy", side=signal.side, price=ask,
                count=DIRECTION_CONTRACTS, post_only=False,
                time_in_force="immediate_or_cancel")
  - IOC ensures we either fill at the ask immediately or cancel — no
    stale resting orders, no maker-fill bag-holds.
  - On fill: add to ticker lock, log, do NOT track post-entry state.
    Position settles at expiry; Kalshi auto-credits revenue to balance.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── Defaults (overridable from user_config) ────────────────────────────


DEFAULT_DIST_THRESHOLD_PCT: float = 0.0010      # 0.10%
DEFAULT_MOMENTUM_THRESHOLD: float = 10.0        # $10 over 5 min
DEFAULT_MAX_OFFSET_S: int = 600                 # before minute 10
DEFAULT_CONTRACTS: int = 5                      # flat sizing
DEFAULT_DAILY_LOSS_HALT_FRAC: float = 0.20      # halt at -20% bankroll/day
DEFAULT_MIN_BANKROLL_X_COST: float = 1.5        # need 1.5×cost in BAL


# ── Result dataclass ───────────────────────────────────────────────────


@dataclass
class DirectionSignal:
    side: str            # "yes" or "no"
    btc_price: float     # BTC price at decision (cents-based USD)
    strike: float        # ticker strike (USD)
    dist_pct: float      # signed (btc - strike) / strike
    btc_5m_move: float   # signed dollar move in last 5 min
    reason: str          # short tag for logging


# ── Decision function ──────────────────────────────────────────────────


def evaluate(
    *,
    btc_price: float,
    strike: float,
    btc_5m_move: float,
    dist_threshold_pct: float = DEFAULT_DIST_THRESHOLD_PCT,
    momentum_threshold: float = DEFAULT_MOMENTUM_THRESHOLD,
) -> DirectionSignal | None:
    """Return a DirectionSignal if conditions match, else None.

    Conditions (sign-aligned with distance gate):
      YES: dist_pct >= +dist_threshold_pct AND btc_5m_move >= +momentum_threshold
      NO : dist_pct <= -dist_threshold_pct AND btc_5m_move <= -momentum_threshold
      else: None (skip)
    """
    if strike <= 0 or btc_price <= 0:
        return None
    dist_pct = (btc_price - strike) / strike

    # YES: BTC well above strike AND moving further up
    if dist_pct >= dist_threshold_pct and btc_5m_move >= momentum_threshold:
        return DirectionSignal(
            side="yes",
            btc_price=btc_price,
            strike=strike,
            dist_pct=dist_pct,
            btc_5m_move=btc_5m_move,
            reason=(f"YES: dist=+{dist_pct*100:.3f}% mom=+${btc_5m_move:.0f} "
                    f"(thr={dist_threshold_pct*100:.3f}%/${momentum_threshold:.0f})"),
        )

    # NO: BTC well below strike AND moving further down
    if dist_pct <= -dist_threshold_pct and btc_5m_move <= -momentum_threshold:
        return DirectionSignal(
            side="no",
            btc_price=btc_price,
            strike=strike,
            dist_pct=dist_pct,
            btc_5m_move=btc_5m_move,
            reason=(f"NO: dist={dist_pct*100:.3f}% mom=${btc_5m_move:.0f} "
                    f"(thr={dist_threshold_pct*100:.3f}%/${momentum_threshold:.0f})"),
        )

    return None


# ── Sizing helpers ─────────────────────────────────────────────────────


def compute_contracts(
    balance_cents: int,
    entry_price_c: int,
    *,
    flat_contracts: int = DEFAULT_CONTRACTS,
    min_bankroll_x_cost: float = DEFAULT_MIN_BANKROLL_X_COST,
) -> int:
    """Return contract count, or 0 if bankroll insufficient.

    Flat sizing — no Kelly, no tier fraction. The strategy's edge comes
    from win rate not asymmetry; aggressive sizing blows up the account
    on the inevitable losing streak.

    Refuses if: bal <= 0, entry_c <= 0, or bal < min_x_cost × cost.
    """
    if balance_cents <= 0 or entry_price_c <= 0 or flat_contracts <= 0:
        return 0
    cost_cents = entry_price_c * flat_contracts
    if balance_cents < int(cost_cents * min_bankroll_x_cost):
        return 0
    return flat_contracts


def daily_loss_halted(
    current_balance_cents: int,
    day_start_balance_cents: int,
    halt_frac: float = DEFAULT_DAILY_LOSS_HALT_FRAC,
) -> bool:
    """Return True if today's drawdown ≥ halt_frac × day-start balance."""
    if day_start_balance_cents <= 0 or halt_frac <= 0:
        return False
    floor_cents = day_start_balance_cents * (1.0 - halt_frac)
    return current_balance_cents < floor_cents
