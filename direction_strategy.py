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


def conviction_multiplier(
    *,
    dist_pct_abs: float,
    btc_5m_move_abs: float,
    base_dist_threshold: float = DEFAULT_DIST_THRESHOLD_PCT,
    base_momentum_threshold: float = DEFAULT_MOMENTUM_THRESHOLD,
) -> float:
    """Return sizing multiplier in [0.7, 1.5] based on signal conviction.

    Inputs are ABSOLUTE values (caller should pass abs(dist_pct) and
    abs(btc_5m_move)). evaluate() filters everything below threshold so
    callers can assume both are >= the respective threshold; we still
    defensively floor at 0.7x for inputs that drift below.

    Composition (additive on top of 1.0 base):
      + 0.5 × min(1.0, (dist_pct - 0.0010) / 0.0010)
        # +0 at threshold (0.10%), +0.5 at 0.20% or higher
      + 0.5 × min(1.0, (btc_5m_move - 10) / 30)
        # +0 at threshold ($10), +0.5 at $40 or higher

    Caps:
      Floor 0.7x — never undersize a qualified signal below 70% of base
      Cap   1.5x — 2026-05-07 PT: REDUCED from 2.0x. Empirical evidence
                   shows that effective 6ct orders (3 base × 2.0x) couldn't
                   fill on Kalshi 15m even at +3c slip. With base reduced
                   to 2ct and cap at 1.5x, effective max is 3ct which fits
                   typical 15m offer-side depth. Top-end signal extrapolation
                   beyond 1.5x was unsupported by the n=35 OOS sample anyway
                   (94% win at 0.20%/$40+ is already saturated).

    Backtest evidence (scripts/backtest_direction.py, n=197 settled):
      dist=0.01% & mom>$10:  67% win
      dist=0.05% & mom>$10:  77% win
      dist=0.10% & mom>$10:  90% win   ← threshold
      dist=0.20% & mom>$10:  94% win   ← multiplier saturates here

    Examples (with base=2ct after 2026-05-07 reduction):
      threshold (0.10%, $10):   1.0x  (2 → 2ct)
      sweet spot (0.15%, $25):  1.25x (2 → 2ct)
      strong (0.18%, $35):      ~1.40x (2 → 2ct)
      screaming (0.20%, $40+):  1.5x  (2 → 3ct)
    """
    if dist_pct_abs < base_dist_threshold or btc_5m_move_abs < base_momentum_threshold:
        return 0.7  # defensive — caller should have filtered
    dist_excess = dist_pct_abs - base_dist_threshold
    mom_excess = btc_5m_move_abs - base_momentum_threshold
    dist_bonus = 0.5 * min(1.0, dist_excess / 0.0010)
    mom_bonus = 0.5 * min(1.0, mom_excess / 30.0)
    mult = 1.0 + dist_bonus + mom_bonus
    return max(0.7, min(1.5, mult))


def compute_contracts(
    balance_cents: int,
    entry_price_c: int,
    *,
    flat_contracts: int = DEFAULT_CONTRACTS,
    min_bankroll_x_cost: float = DEFAULT_MIN_BANKROLL_X_COST,
    multiplier: float = 1.0,
) -> int:
    """Return contract count, or 0 if bankroll insufficient.

    Sizing path:
      1. Apply ``multiplier`` (typically from ``conviction_multiplier``)
         to ``flat_contracts``: scaled = floor(flat × multiplier)
      2. Clamp to >= 1
      3. Compute cost = scaled × entry_c
      4. Refuse if bankroll < min_x_cost × cost (avoid insufficient_balance)

    Backwards-compatible: multiplier=1.0 (default) gives flat sizing.
    """
    if balance_cents <= 0 or entry_price_c <= 0 or flat_contracts <= 0:
        return 0
    scaled = max(1, int(flat_contracts * multiplier))
    cost_cents = entry_price_c * scaled
    if balance_cents < int(cost_cents * min_bankroll_x_cost):
        return 0
    return scaled


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
