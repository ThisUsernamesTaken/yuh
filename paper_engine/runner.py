"""Runner: replays all sessions through each strategy and compares results.
Also computes counterfactual analysis for skipped trades."""
import io
import sys
from collections import defaultdict
from .fill_simulator import FillSimulator
from .log_replay import parse_log
from .strategies import (BaselineStrategy, ThesisAnchoredStrategy,
                         HoldToExpiryStrategy, HighFVGStrategy,
                         DipTimedStrategy, SingleStrongSignalStrategy,
                         DipTimedCleanStrategy, PaperTrade)


def counterfactual_skips(sessions, strategy, sim):
    """For each trade that strategy SKIPPED, compute what it WOULD have made
    if we'd taken it with thesis-anchored exit rules and NO FILTERS."""
    from .strategies import PaperTrade
    cf = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for session in sessions:
        if not session.signals or not session.settled_result:
            continue
        signal = session.signals[0]
        skip_trade = strategy.evaluate_signal(signal, session)
        if skip_trade and not skip_trade.entered:
            # Bypass all filters: just simulate the trade with thesis-anchored exits
            if not signal.actual_entry_price:
                continue
            entry_ct = signal.actual_ct or 10
            fill = sim.simulate_market_entry(entry_ct, signal.actual_entry_price)
            entry_px = fill.avg_fill_price
            settled_our_side = session.settled_result == signal.side
            prob_our = signal.prob if signal.side == "yes" else (100 - signal.prob)
            tp_px = max(entry_px + 5, min(prob_our, entry_px + 20))
            if settled_our_side:
                pnl = (tp_px - entry_px) * entry_ct / 100.0
            else:
                stop_fill = sim.simulate_market_stop(entry_ct, entry_px - 5)
                pnl = (stop_fill.avg_fill_price - entry_px) * entry_ct / 100.0
            r = cf[skip_trade.skip_reason]
            r["count"] += 1
            r["pnl"] += pnl
            if pnl > 0:
                r["wins"] += 1
    return cf


def run_all_strategies(hours_back: int = 72):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sessions = parse_log(hours_back=hours_back)
    eligible = [s for s in sessions if s.settled_result and s.signals]
    print(f"Loaded {len(sessions)} sessions ({len(eligible)} with entries+outcomes)")
    print()

    sim = FillSimulator()
    strategies = [
        BaselineStrategy(sim),
        ThesisAnchoredStrategy(sim),
        HighFVGStrategy(sim),
        DipTimedStrategy(sim),
        SingleStrongSignalStrategy(sim),
        DipTimedCleanStrategy(sim),
        HoldToExpiryStrategy(sim),
    ]

    results = {s.name: [] for s in strategies}
    for session in eligible:
        signal = session.signals[0]
        for strat in strategies:
            trade = strat.evaluate_signal(signal, session)
            if trade:
                results[strat.name].append(trade)

    # Summary table
    print(f"{'Strategy':<22}  {'Entries':>8}  {'Skips':>7}  {'Wins':>5}  "
          f"{'WR':>6}  {'Avg win':>9}  {'Avg loss':>9}  {'Total P&L':>10}  {'$/trade':>9}")
    print("-" * 108)
    for strat in strategies:
        trades = results[strat.name]
        entered = [t for t in trades if t.entered]
        skipped = [t for t in trades if not t.entered]
        won = [t for t in entered if t.pnl > 0]
        lost = [t for t in entered if t.pnl < 0]
        wr = len(won) / max(len(entered), 1) * 100
        avg_win = sum(t.pnl for t in won) / max(len(won), 1)
        avg_loss = sum(t.pnl for t in lost) / max(len(lost), 1)
        total = sum(t.pnl for t in entered)
        per_trade = total / max(len(entered), 1)
        print(f"{strat.name:<22}  {len(entered):>8}  {len(skipped):>7}  {len(won):>5}  "
              f"{wr:>5.0f}%  ${avg_win:>+7.2f}  ${avg_loss:>+7.2f}  ${total:>+9.2f}  ${per_trade:>+7.3f}")

    print()

    # Counterfactual for baseline's skip reasons
    print("=" * 75)
    print("COUNTERFACTUAL: what would baseline's SKIPPED trades have made?")
    print("  (If we had entered them with thesis-anchored exit rules)")
    print("=" * 75)
    baseline = strategies[0]
    cf = counterfactual_skips(eligible, baseline, sim)
    total_cf_pnl = 0
    print(f"  {'Skip reason':<22}  {'Trades skipped':>15}  {'Would-have WR':>14}  {'Would-have P&L':>15}")
    for reason, data in sorted(cf.items(), key=lambda x: -x[1]["pnl"]):
        wr = data["wins"] / max(data["count"], 1) * 100
        total_cf_pnl += data["pnl"]
        verdict = "COST US" if data["pnl"] > 0.5 else ("SAVED US" if data["pnl"] < -0.5 else "WASH")
        print(f"  {reason:<22}  {data['count']:>15}  {wr:>13.0f}%  ${data['pnl']:>+12.2f}  [{verdict}]")
    print(f"\n  TOTAL counterfactual P&L on skipped trades: ${total_cf_pnl:+.2f}")
    print(f"  Current baseline total (kept entries): ${sum(t.pnl for t in results['baseline'] if t.entered):+.2f}")
    if total_cf_pnl > 0:
        print(f"  → Filters cost us ${total_cf_pnl:.2f} of potential P&L")
    else:
        print(f"  → Filters saved us ${-total_cf_pnl:.2f} of losses")


if __name__ == '__main__':
    run_all_strategies(hours_back=72)
