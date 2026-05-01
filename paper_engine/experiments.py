"""Aggressive strategy experiments.

Tests:
1. FVG threshold sweep (find optimal entry threshold)
2. Pressure-direction-AGREE (stricter than veto-only)
3. Edge-only entries (prob > 65 or < 35)
4. Spread-quality filter (top-of-book tightness)
5. Persistent-FVG (must hold for N cycles)
6. Loser profile (what do paper losses share?)

Each strategy reuses thesis-anchored TP and -5c stop.
"""
import io
import sys
from collections import defaultdict
from typing import Optional
from .fill_simulator import FillSimulator
from .log_replay import parse_log, MarketState, SessionTimeline
from .strategies import PaperStrategy, PaperTrade


class ParametricStrategy(PaperStrategy):
    """Single configurable strategy class — parameters drive entry rules."""
    def __init__(self, sim: FillSimulator, name: str,
                 fvg_min: int = 8,
                 require_pressure_agree: bool = False,
                 prob_min: Optional[int] = None,    # require prob ≥ this for our side
                 spread_max: Optional[int] = None,  # require spread ≤ this
                 vel_supportive: bool = False,      # require vel sign matches side
                 hour_only: Optional[set] = None):  # restrict to certain UTC hours
        super().__init__(sim)
        self.name = name
        self.fvg_min = fvg_min
        self.require_pressure_agree = require_pressure_agree
        self.prob_min = prob_min
        self.spread_max = spread_max
        self.vel_supportive = vel_supportive
        self.hour_only = hour_only

    def evaluate_signal(self, state: MarketState, session: SessionTimeline) -> Optional[PaperTrade]:
        # Gates
        if abs(state.fvg) < self.fvg_min:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason=f"fvg<{self.fvg_min}")
        if self.require_pressure_agree:
            sign = 1 if state.side == "yes" else -1
            if state.score * sign <= 0:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason="pressure_not_agreeing")
        if self.prob_min is not None:
            prob_our = state.prob if state.side == "yes" else (100 - state.prob)
            if prob_our < self.prob_min:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason=f"prob_below_{self.prob_min}")
        if self.spread_max is not None and state.spread > self.spread_max:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason=f"spread>{self.spread_max}")
        if self.vel_supportive:
            if state.side == "yes" and state.vel <= 0:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason="vel_not_supportive")
            if state.side == "no" and state.vel >= 0:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason="vel_not_supportive")
        if self.hour_only is not None:
            hr = state.ts.hour
            if hr not in self.hour_only:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason=f"hour_{hr}_not_in_set")

        if not state.actual_entry_price:
            return PaperTrade(self.name, session.ticker, state.side, False,
                              skip_reason="no_ask")

        # Execute with thesis-anchored TP, -5c stop
        entry_ct = state.actual_ct or 10
        fill = self.sim.simulate_market_entry(entry_ct, state.actual_entry_price)
        entry_px = fill.avg_fill_price

        if not session.settled_result:
            return PaperTrade(self.name, session.ticker, state.side, True,
                              entry_price=entry_px, entry_ct=entry_ct,
                              exit_type="unknown")

        settled_our_side = session.settled_result == state.side
        prob_our = state.prob if state.side == "yes" else (100 - state.prob)
        tp_px = max(entry_px + 5, min(prob_our, entry_px + 20))

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


def run_experiments(hours_back: int = 72):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sessions = parse_log(hours_back=hours_back)
    eligible = [s for s in sessions if s.settled_result and s.signals]
    print(f"Loaded {len(eligible)} eligible sessions over last {hours_back}h")
    print()

    sim = FillSimulator()

    # ─── 1. FVG threshold sweep ───
    print("=" * 88)
    print("EXPERIMENT 1: FVG THRESHOLD SWEEP")
    print("=" * 88)
    strategies = []
    for thresh in [4, 6, 8, 10, 12, 15, 20]:
        strategies.append(ParametricStrategy(sim, f"fvg≥{thresh}", fvg_min=thresh))

    print(f"{'Strategy':<12}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)
    print()

    # ─── 2. Probability filter ───
    print("=" * 88)
    print("EXPERIMENT 2: PROBABILITY FILTER (require prob ≥ N for our side)")
    print("=" * 88)
    strategies = []
    for prob_min in [55, 60, 65, 70, 75]:
        strategies.append(ParametricStrategy(sim, f"prob≥{prob_min}",
                                              fvg_min=8, prob_min=prob_min))
    print(f"{'Strategy':<12}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)
    print()

    # ─── 3. Pressure-agree (stricter) ───
    print("=" * 88)
    print("EXPERIMENT 3: PRESSURE MUST AGREE (not just veto-only)")
    print("=" * 88)
    s1 = ParametricStrategy(sim, "fvg≥8 only", fvg_min=8)
    s2 = ParametricStrategy(sim, "+pressure_agree", fvg_min=8, require_pressure_agree=True)
    s3 = ParametricStrategy(sim, "+vel_supportive", fvg_min=8, vel_supportive=True)
    s4 = ParametricStrategy(sim, "agree+vel", fvg_min=8,
                             require_pressure_agree=True, vel_supportive=True)
    print(f"{'Strategy':<20}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in [s1, s2, s3, s4]:
        _summarize(s, eligible)
    print()

    # ─── 4. Spread quality ───
    print("=" * 88)
    print("EXPERIMENT 4: SPREAD QUALITY (require tight book)")
    print("=" * 88)
    strategies = [
        ParametricStrategy(sim, "no_spread_filter", fvg_min=8),
        ParametricStrategy(sim, "spread≤2c", fvg_min=8, spread_max=2),
        ParametricStrategy(sim, "spread≤3c", fvg_min=8, spread_max=3),
        ParametricStrategy(sim, "spread≤5c", fvg_min=8, spread_max=5),
    ]
    print(f"{'Strategy':<20}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)
    print()

    # ─── 5. Time-of-day analysis ───
    print("=" * 88)
    print("EXPERIMENT 5: TIME-OF-DAY (UTC hour buckets)")
    print("=" * 88)
    base = ParametricStrategy(sim, "all_hours", fvg_min=8)
    base_results = [base.evaluate_signal(s.signals[0], s) for s in eligible]
    base_entered = [t for t in base_results if t and t.entered]
    by_hour = defaultdict(list)
    for t in base_entered:
        # Find the matching session and signal for ts
        for s in eligible:
            if s.ticker == t.ticker:
                hr = s.signals[0].ts.hour
                by_hour[hr].append(t.pnl)
                break
    print(f"  {'UTC hour':>10}  {'n':>4}  {'wins':>5}  {'WR':>5}  {'total':>9}  {'avg':>9}")
    for hr in sorted(by_hour):
        pnls = by_hour[hr]
        wins = sum(1 for p in pnls if p > 0)
        print(f"  {hr:02d}:xx     {len(pnls):>4}  {wins:>5}  {wins/len(pnls)*100:>3.0f}%  ${sum(pnls):>+7.2f}  ${sum(pnls)/len(pnls):>+7.2f}")
    print()

    # ─── 6. Loser profile ───
    print("=" * 88)
    print("EXPERIMENT 6: LOSER PROFILE — what do paper losses share?")
    print("=" * 88)
    base_strat = ParametricStrategy(sim, "base", fvg_min=8)
    losers = []
    winners = []
    for s in eligible:
        t = base_strat.evaluate_signal(s.signals[0], s)
        if t and t.entered:
            sig = s.signals[0]
            row = {'pnl': t.pnl, 'fvg': abs(sig.fvg), 'prob': sig.prob,
                   'score': sig.score, 'imp': sig.imp, 'flow': sig.flow,
                   'lag': sig.lag, 'vel': sig.vel, 'vol': sig.vol,
                   'spread': sig.spread, 'side': sig.side, 'entry': t.entry_price}
            if t.pnl > 0:
                winners.append(row)
            elif t.pnl < 0:
                losers.append(row)

    print(f"  Wins: {len(winners)}  Losses: {len(losers)}")
    if winners and losers:
        print(f"  {'feature':>10}  {'win_avg':>10}  {'loss_avg':>10}  {'delta':>8}")
        for k in ['fvg', 'prob', 'score', 'imp', 'flow', 'lag', 'vel', 'vol', 'spread', 'entry']:
            try:
                w_avg = sum(r[k] for r in winners) / len(winners)
                l_avg = sum(r[k] for r in losers) / len(losers)
                delta = w_avg - l_avg
                marker = ' *' if abs(delta) > max(abs(l_avg), 0.01) * 0.2 else ''
                print(f"  {k:>10}  {w_avg:>+10.3f}  {l_avg:>+10.3f}  {delta:>+8.3f}{marker}")
            except:
                pass
        # Side breakdown
        w_yes = sum(1 for r in winners if r['side'] == 'yes')
        l_yes = sum(1 for r in losers if r['side'] == 'yes')
        print(f"  side: wins {w_yes}/{len(winners)} YES, losses {l_yes}/{len(losers)} YES")


def _summarize(strat, sessions):
    trades = []
    for s in sessions:
        t = strat.evaluate_signal(s.signals[0], s)
        if t:
            trades.append(t)
    entered = [t for t in trades if t.entered]
    won = [t for t in entered if t.pnl > 0]
    lost = [t for t in entered if t.pnl < 0]
    wr = len(won) / max(len(entered), 1) * 100
    avg_win = sum(t.pnl for t in won) / max(len(won), 1)
    avg_loss = sum(t.pnl for t in lost) / max(len(lost), 1)
    total = sum(t.pnl for t in entered)
    per = total / max(len(entered), 1)
    print(f"  {strat.name:<20}  {len(entered):>8}  {wr:>4.0f}%  ${avg_win:>+7.2f}  ${avg_loss:>+7.2f}  ${total:>+7.2f}  ${per:>+6.3f}")


def run_velocity_ceiling_experiment(hours_back: int = 72):
    """The loser profile showed losers had vel avg +15, winners +4. Test ceilings."""
    sessions = parse_log(hours_back=hours_back)
    eligible = [s for s in sessions if s.settled_result and s.signals]
    sim = FillSimulator()

    print("=" * 88)
    print("EXPERIMENT 7: VELOCITY CEILING — fade the extension")
    print("=" * 88)
    print("Loser profile showed losers had avg vel +15.3/s vs winners +4.2/s.")
    print("Hypothesis: late entries (high velocity = move already done) lose more.")
    print()

    class VelCeilingStrategy(ParametricStrategy):
        def __init__(self, sim, name, vel_ceiling):
            super().__init__(sim, name, fvg_min=8)
            self.vel_ceiling = vel_ceiling
        def evaluate_signal(self, state, session):
            sign = 1 if state.side == "yes" else -1
            if state.vel * sign > self.vel_ceiling:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason=f"vel_extended_{state.vel:.1f}")
            return super().evaluate_signal(state, session)

    strategies = [
        ParametricStrategy(sim, "no_ceiling", fvg_min=8),
        VelCeilingStrategy(sim, "vel_ceiling_20", 20),
        VelCeilingStrategy(sim, "vel_ceiling_15", 15),
        VelCeilingStrategy(sim, "vel_ceiling_10", 10),
        VelCeilingStrategy(sim, "vel_ceiling_5", 5),
    ]
    print(f"{'Strategy':<20}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)
    print()

    # Combined: vel ceiling + good hours
    print("=" * 88)
    print("EXPERIMENT 8: BEST HOURS ONLY (UTC 6, 14, 15, 17)")
    print("=" * 88)
    good_hours = {6, 14, 15, 17}
    strategies = [
        ParametricStrategy(sim, "all_hours", fvg_min=8),
        ParametricStrategy(sim, "best_4_hours", fvg_min=8, hour_only=good_hours),
    ]
    print(f"{'Strategy':<20}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)
    print()

    # Best combo
    print("=" * 88)
    print("EXPERIMENT 9: BEST OF ALL (vel_ceiling + skip worst hours)")
    print("=" * 88)
    worst_hours = {10, 11, 12, 16, 19, 20, 21}  # skip these

    class CombinedStrategy(ParametricStrategy):
        def __init__(self, sim):
            super().__init__(sim, "combined_best", fvg_min=8)
        def evaluate_signal(self, state, session):
            # Skip worst hours
            if state.ts.hour in worst_hours:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason="worst_hour")
            # Skip extended velocity
            sign = 1 if state.side == "yes" else -1
            if state.vel * sign > 15:
                return PaperTrade(self.name, session.ticker, state.side, False,
                                  skip_reason="vel_extended")
            return super().evaluate_signal(state, session)

    strategies = [
        ParametricStrategy(sim, "current_live", fvg_min=8),
        VelCeilingStrategy(sim, "vel_ceiling_15", 15),
        ParametricStrategy(sim, "skip_worst_hours", fvg_min=8,
                            hour_only={h for h in range(24) if h not in worst_hours}),
        CombinedStrategy(sim),
    ]
    print(f"{'Strategy':<22}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>9}  {'$/trade':>8}")
    for s in strategies:
        _summarize(s, eligible)


if __name__ == '__main__':
    run_experiments(hours_back=72)
    print("\n\n")
    run_velocity_ceiling_experiment(hours_back=72)
