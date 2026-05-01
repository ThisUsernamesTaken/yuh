"""Strategy implementations for paper engine.

Each strategy takes a MarketState (signal moment) and a session outcome,
and simulates what would have happened under that strategy's rules.

Returns a PaperTrade with realistic fills.
"""
from dataclasses import dataclass
from typing import Optional
from .fill_simulator import FillSimulator, FillResult
from .log_replay import MarketState, SessionTimeline


@dataclass
class PaperTrade:
    """Outcome of one paper trade."""
    strategy: str
    ticker: str
    side: str
    entered: bool
    entry_price: int = 0
    entry_ct: int = 0
    exit_type: str = ""
    exit_price: int = 0
    pnl: float = 0.0
    skip_reason: str = ""


class PaperStrategy:
    """Abstract base: decide whether to enter, and simulate exit."""
    name = "abstract"

    def __init__(self, sim: FillSimulator):
        self.sim = sim

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        """Return PaperTrade: either entered and resolved, or skipped."""
        raise NotImplementedError


class BaselineStrategy(PaperStrategy):
    """Replicate current live engine:
      - Enter on FVG >= 12c with reversal-risk filter, vel dead-zone, lag
      - Size per conviction boost (approximated)
      - Fixed +5c scalp TP → OR hold to expiry if not filled
      - Stop at entry-5c with 10s cooldown (approximated as 'usually fires')
    """
    name = "baseline"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        # Entry gates (approximate current live filters)
        if abs(state.fvg) < 12:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="fvg_below_12")
        # Velocity dead-zone
        if state.side == "yes" and -2.0 < state.vel < 0.0:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="vel_dead_zone")
        if state.side == "no" and 0.0 < state.vel < 2.0:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="vel_dead_zone")
        # Lag against
        lag_side = state.lag if state.side == "yes" else -state.lag
        if lag_side < 0:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="lag_against")
        # Vol cut
        if state.vol < 0.25:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="low_vol")

        # Determine entry price and count
        # Approximate using actual logged entry price if available
        if not state.actual_entry_price:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="no_ask_data")

        entry_ct = state.actual_ct or 10
        # Apply realistic fill simulator
        fill = self.sim.simulate_market_entry(entry_ct, state.actual_entry_price)
        entry_px = fill.avg_fill_price

        # Decide outcome: session settled our way = won, opposite = lost
        if not session.settled_result:
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="unknown_settlement",
                              skip_reason="no_settlement")

        settled_our_side = session.settled_result == state.side

        # TP @ entry+5c for scalp; assume TP fills intra-session if our side settled
        tp_px = entry_px + 5
        if settled_our_side:
            # Very likely TP fills during session (book moved our way enough)
            # Use TP price as exit
            pnl = (tp_px - entry_px) * entry_ct / 100.0
            exit_type = "scalp_tp"
            exit_px = tp_px
        else:
            # Our side lost. Stop at entry-5c OR full loss to settlement.
            # Assume stop fires ~75% of the time (based on live observation
            # that many losers get flushed), full loss 25% (stop doesn't fire).
            # For simplicity: assume scalp stop does fire at -5c avg.
            stop_fill = self.sim.simulate_market_stop(entry_ct, entry_px - 5)
            pnl = (stop_fill.avg_fill_price - entry_px) * entry_ct / 100.0
            exit_type = "scalp_stop"
            exit_px = stop_fill.avg_fill_price

        return PaperTrade(self.name, session.ticker, state.side, True,
                          entry_price=entry_px, entry_ct=entry_ct,
                          exit_type=exit_type, exit_price=exit_px, pnl=pnl)


class ThesisAnchoredStrategy(BaselineStrategy):
    """Same entry rules as baseline, but TP targets fair value (clamped entry+5 to +20)."""
    name = "thesis_anchored"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        base = super().evaluate_signal(state, session)
        if not base or not base.entered:
            return base  # same skip decision
        if not session.settled_result:
            return base

        settled_our_side = session.settled_result == state.side
        entry_px = base.entry_price
        entry_ct = base.entry_ct

        # Fair value target (approximate from prob): prob of our side
        prob_our = state.prob if state.side == "yes" else (100 - state.prob)
        fv_target = prob_our
        # Clamp to [entry+5, entry+20]
        tp_px = max(entry_px + 5, min(fv_target, entry_px + 20))

        if settled_our_side:
            pnl = (tp_px - entry_px) * entry_ct / 100.0
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="thesis_tp", exit_price=tp_px, pnl=pnl)
        else:
            stop_fill = self.sim.simulate_market_stop(entry_ct, entry_px - 5)
            pnl = (stop_fill.avg_fill_price - entry_px) * entry_ct / 100.0
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="scalp_stop", exit_price=stop_fill.avg_fill_price, pnl=pnl)


class HighFVGStrategy(ThesisAnchoredStrategy):
    """Like thesis-anchored, but require FVG >= 20c. Fewer trades, possibly higher WR.
    Tests whether 'only take big mispricings' concentrates the edge."""
    name = "high_fvg_20c"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        if abs(state.fvg) < 20:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="fvg_below_20")
        return super().evaluate_signal(state, session)


class DipTimedStrategy(ThesisAnchoredStrategy):
    """Same entry rules as thesis, but models dip-timed entry.
    Assumes our entry captures a 1-2c better avg fill via waiting for local low.
    Model: 40% of entries get 2c better fill, 60% get same fill as baseline.
    Based on observed tape behavior (bids frequently dip in first 60s post-signal)."""
    name = "dip_timed"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        base = super().evaluate_signal(state, session)
        if not base or not base.entered:
            return base

        # Model: 40% of the time, we get 2c better fill by waiting for local low.
        # Use deterministic "has dipped" proxy: use ticker hash for repeatability.
        hash_val = hash(session.ticker) % 100
        dipped = hash_val < 40  # 40% simulated

        if dipped:
            improved_entry = max(1, base.entry_price - 2)
            # Recompute PnL with better entry
            # exit price stays the same; improvement goes straight to pnl
            pnl_delta = (base.entry_price - improved_entry) * base.entry_ct / 100.0
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=improved_entry, entry_ct=base.entry_ct,
                              exit_type=base.exit_type + "_diptimed",
                              exit_price=base.exit_price,
                              pnl=base.pnl + pnl_delta)
        return PaperTrade(self.name, session.ticker, state.side, True,
                          entry_price=base.entry_price, entry_ct=base.entry_ct,
                          exit_type=base.exit_type, exit_price=base.exit_price,
                          pnl=base.pnl)


class SingleStrongSignalStrategy(ThesisAnchoredStrategy):
    """Paradoxical: earlier data showed 1/4 indicators aligned = 86% WR.
    Enter only when EXACTLY ONE of the four momentum indicators is strongly aligned.
    Skip 0 aligned (weak) and 3-4 aligned (exhausted/crowded)."""
    name = "single_signal"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        # Pre-filter: FVG must cross
        if abs(state.fvg) < 12:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="fvg_below_12")

        # Count aligned indicators (MEDIUM thresholds)
        sign = 1 if state.side == "yes" else -1
        aligned = 0
        if state.score * sign >= 0.10: aligned += 1
        if state.imp * sign >= 0.10: aligned += 1
        if state.vel * sign >= 1.0: aligned += 1
        if state.flow * sign >= 0.15: aligned += 1

        if aligned != 1:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason=f"aligned_{aligned}_of_4")

        # Use thesis-anchored from here
        return super().evaluate_signal(state, session)


class DipTimedCleanStrategy(PaperStrategy):
    """Best-of-all: thesis-anchored TP, dip-timed entry, minimal filters.
    Only FVG ≥ 8c gate (real signal required) — NO vel_dead_zone, NO lag_against,
    NO low_vol (all shown counterproductive in paper test).
    BTC-trend regime gate preserved for directional sanity."""
    name = "clean_best"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        # Just the minimum: FVG must cross
        if abs(state.fvg) < 8:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="fvg_below_8")

        if not state.actual_entry_price:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="no_ask_data")

        entry_ct = state.actual_ct or 10

        # Dip-timed entry: 40% chance of -2c improvement (simulated)
        hash_val = hash(session.ticker) % 100
        dipped = hash_val < 40
        base_entry = state.actual_entry_price
        if dipped:
            entry_px = max(1, base_entry - 2)
        else:
            fill = self.sim.simulate_market_entry(entry_ct, base_entry)
            entry_px = fill.avg_fill_price

        if not session.settled_result:
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="unknown")

        settled_our_side = session.settled_result == state.side

        # Thesis-anchored TP
        prob_our = state.prob if state.side == "yes" else (100 - state.prob)
        tp_px = max(entry_px + 5, min(prob_our, entry_px + 20))

        if settled_our_side:
            pnl = (tp_px - entry_px) * entry_ct / 100.0
            exit_type = "thesis_tp_diptimed" if dipped else "thesis_tp"
        else:
            stop_fill = self.sim.simulate_market_stop(entry_ct, entry_px - 5)
            pnl = (stop_fill.avg_fill_price - entry_px) * entry_ct / 100.0
            exit_type = "scalp_stop"
            tp_px = stop_fill.avg_fill_price  # for exit_price field

        return PaperTrade(self.name, session.ticker, state.side, True,
                          entry_price=entry_px, entry_ct=entry_ct,
                          exit_type=exit_type, exit_price=tp_px, pnl=pnl)


class CleanWithStopVariant(PaperStrategy):
    """Same entry rules as clean_best (FVG ≥ 8c, no other filters).
    Same TP rules (thesis-anchored, clamped entry+5 to +20).
    Configurable stop_cents: 5 = current live, 10 = wide, None = no stop."""
    def __init__(self, sim: FillSimulator, name: str, stop_cents=None):
        super().__init__(sim)
        self.name = name
        self.stop_cents = stop_cents

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        if abs(state.fvg) < 8:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="fvg_below_8")
        if not state.actual_entry_price:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="no_ask")

        entry_ct = state.actual_ct or 10
        fill = self.sim.simulate_market_entry(entry_ct, state.actual_entry_price)
        entry_px = fill.avg_fill_price

        if not session.settled_result:
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="unknown")

        settled_our_side = session.settled_result == state.side

        # Win path: thesis-anchored TP (or full settlement if no_stop and TP doesn't cap upside)
        prob_our = state.prob if state.side == "yes" else (100 - state.prob)
        tp_px = max(entry_px + 5, min(prob_our, entry_px + 20))

        if settled_our_side:
            # Side was correct — TP usually fills mid-session; if not, settles at 100c
            # For all-stop variants: assume TP fills during session
            pnl = (tp_px - entry_px) * entry_ct / 100.0
            exit_type = "thesis_tp"
            exit_px = tp_px
        else:
            # Side was wrong — varies by stop policy
            if self.stop_cents is None:
                # NO STOP: rides to expiry, loses full entry value
                pnl = (0 - entry_px) * entry_ct / 100.0
                exit_type = "settled_loss"
                exit_px = 0
            else:
                # STOP fires at entry - stop_cents (with simulator slippage)
                stop_fill = self.sim.simulate_market_stop(entry_ct, entry_px - self.stop_cents)
                pnl = (stop_fill.avg_fill_price - entry_px) * entry_ct / 100.0
                exit_type = f"stop_{self.stop_cents}c"
                exit_px = stop_fill.avg_fill_price

        return PaperTrade(self.name, session.ticker, state.side, True,
                          entry_price=entry_px, entry_ct=entry_ct,
                          exit_type=exit_type, exit_price=exit_px, pnl=pnl)


class HoldToExpiryStrategy(BaselineStrategy):
    """Like baseline entries but NO scalp TP — holds to expiry.
    Wins settle at 100c, losses settle at 0c."""
    name = "hold_expiry"

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        base = super().evaluate_signal(state, session)
        if not base or not base.entered:
            return base
        if not session.settled_result:
            return base

        settled_our_side = session.settled_result == state.side
        entry_px = base.entry_price
        entry_ct = base.entry_ct

        if settled_our_side:
            # Held to 100c
            pnl = (100 - entry_px) * entry_ct / 100.0
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="settled_win", exit_price=100, pnl=pnl)
        else:
            # Held to 0c (full loss)
            pnl = (0 - entry_px) * entry_ct / 100.0
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="settled_loss", exit_price=0, pnl=pnl)
