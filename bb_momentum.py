"""Pure momentum-rider strategy (2026-05-03).

Conceptually different from BB_PURE. Where BB_PURE fades model-vs-market
gaps near strike, this module rides sustained directional BTC moves:

    DETECTION:  btc_move_300s in same direction AND btc_move_30s confirming
    ENTRY:      buy with the trend (YES if BTC up, NO if BTC down)
                only when our-side mid is cheap enough to have appreciation room
    STALL:      btc_move_30s magnitude drops below HALF entry-time magnitude
                OR sign-flips → momentum dead → exit
    RE-ENTRY:   cooldown N seconds, watch for new directional confirmation,
                cycle up to MAX_CYCLES_PER_WINDOW

Pure functions, no engine state. Trivially unit-testable.

Output:
    MomentumSignal | None
    or
    StallVerdict   (separate function for the exit decision)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MomentumSignal:
    side: str                       # "yes" or "no"
    suggested_entry_cents: int      # current ask side we'd hit (or bid+1 maker)
    direction_dollars: float        # signed btc_move_300s at entry
    velocity_30s: float             # signed btc_move_30s at entry (entry-time
                                    # velocity used as the "stall" baseline)
    contracts: int                  # rounded position size
    kelly_fraction: float           # signed fraction of bankroll
    seconds_to_expiry: float
    reason: str


@dataclass
class StallVerdict:
    should_exit: bool
    reason: str                     # "STALL", "REVERSAL", "NONE"


def evaluate_entry(
    *,
    btc_move_30s: float,              # signed $ over last 30s
    btc_move_300s: float,             # signed $ over last 300s
    yes_mid_cents: int,               # current YES mid
    seconds_to_expiry: float,
    balance_dollars: float,
    config: dict,
) -> Optional[MomentumSignal]:
    """Return a MomentumSignal if directionality is sustained, else None.

    Knobs read from `config`:
        min_btc_move_300s_dollars:  required 5-min trend magnitude (default 30.0)
        min_btc_move_30s_dollars:   required 30s confirmation magnitude (default 10.0)
        require_same_direction:     if True, both 30s and 300s same sign (default True)
        max_entry_cents:            don't enter if our-side mid above this (default 50)
        min_entry_cents:            don't fire on dust (default 5)
        min_time_remaining_s:       don't fire too close to expiry (default 90)
        kelly_fraction:             base Kelly (default 0.20 — smaller than
                                    BB_PURE's 0.25 because trend can reverse)
        kelly_max_frac:             hard ceiling on bankroll % (default 0.05)
        max_contracts:              contract count cap (default 200)
    """
    # ── Input validation ──────────────────────────────────────────────
    if yes_mid_cents <= 0 or yes_mid_cents >= 100:
        return None
    if seconds_to_expiry <= 0:
        return None

    min_300 = float(config.get("min_btc_move_300s_dollars", 30.0))
    min_30 = float(config.get("min_btc_move_30s_dollars", 10.0))
    require_same = bool(config.get("require_same_direction", True))
    max_entry = int(config.get("max_entry_cents", 50))
    min_entry = int(config.get("min_entry_cents", 5))
    min_time = float(config.get("min_time_remaining_s", 90.0))
    kelly_frac = float(config.get("kelly_fraction", 0.20))
    kelly_max = float(config.get("kelly_max_frac", 0.05))
    max_contracts = int(config.get("max_contracts", 200))

    # ── Time gate ─────────────────────────────────────────────────────
    if seconds_to_expiry < min_time:
        return None

    # ── Direction confirmation ────────────────────────────────────────
    if abs(btc_move_300s) < min_300:
        return None
    if abs(btc_move_30s) < min_30:
        return None
    if require_same:
        # Both must be same sign (no recent reversal)
        if (btc_move_300s > 0) != (btc_move_30s > 0):
            return None

    # ── Pick side: buy WITH the trend ─────────────────────────────────
    btc_up = btc_move_300s > 0
    if btc_up:
        side = "yes"
        # Buy YES when BTC is up — YES contract is appreciating
        entry_cents = int(yes_mid_cents)
        win_p = yes_mid_cents / 100.0  # market's implied probability
    else:
        side = "no"
        entry_cents = int(100 - yes_mid_cents)  # NO mid
        win_p = 1.0 - (yes_mid_cents / 100.0)

    # ── Entry-price gates ─────────────────────────────────────────────
    if entry_cents > max_entry:
        return None  # too expensive — limited appreciation room
    if entry_cents < min_entry:
        return None  # dust price — likely no liquidity / settled

    # ── Sizing: simple fractional ─────────────────────────────────────
    # Momentum doesn't have a clean Kelly framework (no separate model
    # prob — the bet is on the trend continuing, not on a known edge
    # vs market mid). Use fixed fractional sizing instead:
    #   bet = kelly_fraction × balance, capped by kelly_max_frac.
    # Trend conviction enters via the velocity gates above; sizing is
    # symmetric and conservative.
    if entry_cents >= 99:
        return None
    if balance_dollars <= 0:
        return None
    fractional_kelly = max(0.0, min(kelly_frac, kelly_max))
    bet_dollars = fractional_kelly * balance_dollars
    contracts = int(round(bet_dollars / max(entry_cents / 100.0, 0.01)))
    contracts = max(1, min(contracts, max_contracts))

    return MomentumSignal(
        side=side,
        suggested_entry_cents=entry_cents,
        direction_dollars=btc_move_300s,
        velocity_30s=btc_move_30s,
        contracts=contracts,
        kelly_fraction=fractional_kelly,
        seconds_to_expiry=seconds_to_expiry,
        reason=(
            f"trend={btc_move_300s:+.1f}/5m vel30={btc_move_30s:+.1f} "
            f"side={side} entry={entry_cents}c p_market={win_p:.3f} "
            f"kelly={fractional_kelly:.4f} contracts={contracts}"
        ),
    )


def evaluate_stall(
    *,
    entry_velocity_30s: float,        # signed velocity at entry time
    current_velocity_30s: float,      # signed velocity now
    config: dict,
) -> StallVerdict:
    """Return whether to exit based on momentum decay.

    Two stall conditions:
      1. REVERSAL: sign-flip in 30s velocity (momentum reversed)
      2. STALL:   |current| < HALF |entry|   (momentum dying)

    Knobs from `config`:
        stall_decay_ratio:   threshold ratio (default 0.5 — half)
        stall_min_entry_velocity:  ignore if entry vel was tiny (default 5.0)
    """
    decay_ratio = float(config.get("stall_decay_ratio", 0.5))
    min_entry_vel = float(config.get("stall_min_entry_velocity", 5.0))

    if abs(entry_velocity_30s) < min_entry_vel:
        # Entered on a tiny velocity — can't meaningfully detect stall.
        # Don't trigger exit on this basis.
        return StallVerdict(should_exit=False, reason="NONE")

    # Reversal: sign flipped
    if (entry_velocity_30s > 0) != (current_velocity_30s > 0):
        if abs(current_velocity_30s) >= min_entry_vel:
            # Genuinely flipped (not just noise around zero)
            return StallVerdict(
                should_exit=True,
                reason=(
                    f"REVERSAL entry_vel={entry_velocity_30s:+.1f} "
                    f"current_vel={current_velocity_30s:+.1f}"
                ),
            )

    # Stall: magnitude has decayed
    if abs(current_velocity_30s) < decay_ratio * abs(entry_velocity_30s):
        return StallVerdict(
            should_exit=True,
            reason=(
                f"STALL entry_vel={entry_velocity_30s:+.1f} "
                f"current_vel={current_velocity_30s:+.1f} "
                f"ratio={abs(current_velocity_30s)/abs(entry_velocity_30s):.2f} "
                f"< {decay_ratio:.2f}"
            ),
        )

    return StallVerdict(should_exit=False, reason="NONE")
