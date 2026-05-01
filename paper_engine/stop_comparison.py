"""Compare three stop strategies on the same 134-session sample.

Question: given the Brownian Bridge math is the real edge, do stops help or hurt?
Variants tested:
  - stop_5c: current live (entry-5c)
  - stop_10c: wider (entry-10c) — gives noise more room
  - no_stop: hold to expiry, full value loss on opposite settle
"""
import io
import sys
from collections import defaultdict
from .fill_simulator import FillSimulator
from .log_replay import parse_log
from .strategies import CleanWithStopVariant


def run_stop_comparison(hours_back: int = 72):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sessions = parse_log(hours_back=hours_back)
    eligible = [s for s in sessions if s.settled_result and s.signals]
    print(f"Loaded {len(eligible)} eligible sessions")
    print()

    sim = FillSimulator()

    strategies = [
        CleanWithStopVariant(sim, "stop_5c (current live)", stop_cents=5),
        CleanWithStopVariant(sim, "stop_10c (wide)", stop_cents=10),
        CleanWithStopVariant(sim, "no_stop (hold to expiry)", stop_cents=None),
    ]

    print("=" * 95)
    print("STOP STRATEGY COMPARISON")
    print("=" * 95)
    print(f"  {'Strategy':<30}  {'Entries':>8}  {'WR':>5}  {'Avg win':>9}  {'Avg loss':>9}  {'Total':>10}  {'$/trade':>8}")
    print("-" * 95)

    results_by_strat = {}
    for strat in strategies:
        trades = [strat.evaluate_signal(s.signals[0], s) for s in eligible]
        entered = [t for t in trades if t and t.entered]
        won = [t for t in entered if t.pnl > 0]
        lost = [t for t in entered if t.pnl < 0]
        wr = len(won) / max(len(entered), 1) * 100
        avg_win = sum(t.pnl for t in won) / max(len(won), 1)
        avg_loss = sum(t.pnl for t in lost) / max(len(lost), 1)
        total = sum(t.pnl for t in entered)
        per = total / max(len(entered), 1)
        worst = min(t.pnl for t in entered) if entered else 0
        best = max(t.pnl for t in entered) if entered else 0
        print(f"  {strat.name:<30}  {len(entered):>8}  {wr:>4.0f}%  ${avg_win:>+7.2f}  ${avg_loss:>+7.2f}  ${total:>+8.2f}  ${per:>+6.3f}")
        results_by_strat[strat.name] = {
            "entered": entered,
            "total": total,
            "best": best,
            "worst": worst,
            "wr": wr,
        }

    print()
    print(f"  {'Strategy':<30}  {'Worst':>9}  {'Best':>9}  {'Range':>9}")
    for name, data in results_by_strat.items():
        rng = data["best"] - data["worst"]
        print(f"  {name:<30}  ${data['worst']:>+7.2f}  ${data['best']:>+7.2f}  ${rng:>7.2f}")
    print()

    # Drawdown analysis: worst 10 trades for each strategy
    print("=" * 95)
    print("WORST 10 TRADES BY STRATEGY (variance / drawdown profile)")
    print("=" * 95)
    for name, data in results_by_strat.items():
        worst10 = sorted(data["entered"], key=lambda t: t.pnl)[:10]
        worst_sum = sum(t.pnl for t in worst10)
        print(f"  {name:<30}  10 worst sum: ${worst_sum:>+7.2f}  individual losses: ", end="")
        print(", ".join(f"${t.pnl:+.1f}" for t in worst10))
    print()

    # Sharpe-like: total / stdev
    import statistics
    print(f"  {'Strategy':<30}  {'Total':>9}  {'StdDev':>9}  {'Sharpe-ish':>10}")
    for name, data in results_by_strat.items():
        pnls = [t.pnl for t in data["entered"]]
        if len(pnls) > 1:
            stdev = statistics.stdev(pnls)
            sharpe = sum(pnls) / max(stdev, 0.01) / len(pnls) ** 0.5  # rough
        else:
            stdev = 0
            sharpe = 0
        print(f"  {name:<30}  ${data['total']:>+7.2f}  ${stdev:>+7.2f}  {sharpe:>+9.3f}")


if __name__ == '__main__':
    run_stop_comparison(hours_back=72)
