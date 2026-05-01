"""Paper vs LIVE validation.

For each session in the last 72h:
  1. Paper engine predicts what each strategy WOULD have made
  2. Compare to what actually happened (balance_snapshots delta)
  3. Report discrepancies

If paper's predicted $ are consistently off from live reality, the fill model
is wrong. This is the feedback loop to calibrate paper → live.
"""
import io
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from .fill_simulator import FillSimulator
from .log_replay import parse_log
from .strategies import CleanWithStopVariant


def get_balance_pnl_per_session(hours_back: int):
    """Return {ticker: pnl_cents} from balance_snapshots (ground truth).
    For each entry snapshot, find the next window_change and compute delta."""
    conn = sqlite3.connect('data/trades.db', timeout=5)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    c.execute("""SELECT ts, balance_cents, note, open_position_side, open_position_entry,
                 open_position_count, window_ticker FROM balance_snapshots
                 WHERE ts > ? ORDER BY id ASC""", (cutoff,))
    snaps = c.fetchall()
    conn.close()

    results = {}
    for i, s in enumerate(snaps):
        if s['note'] != 'entry_TA_FORCED_SIGNAL':
            continue
        if not s['window_ticker']:
            continue
        ticker = s['window_ticker']
        entry_bal = s['balance_cents']
        # Find next window_change OR next entry on different ticker
        exit_bal = None
        for j in range(i + 1, len(snaps)):
            ns = snaps[j]
            if ns['note'] == 'window_change':
                exit_bal = ns['balance_cents']
                break
            if ns['note'] == 'entry_TA_FORCED_SIGNAL' and ns['window_ticker'] != ticker:
                exit_bal = ns['balance_cents']
                break
        if exit_bal is None:
            continue
        delta = (exit_bal - entry_bal) / 100.0  # cents → dollars
        # Filter outliers (deposits/withdrawals)
        if -50 < delta < 50:
            results[ticker] = delta
    return results


def validate():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    hours_back = 72
    sessions = parse_log(hours_back=hours_back)
    eligible = [s for s in sessions if s.settled_result and s.signals]
    live_pnls = get_balance_pnl_per_session(hours_back=hours_back)

    print(f"Paper sessions:  {len(eligible)}")
    print(f"Live balance deltas: {len(live_pnls)}")
    matched = [(s, live_pnls[s.ticker]) for s in eligible if s.ticker in live_pnls]
    print(f"Matched for comparison: {len(matched)}")
    print()

    sim = FillSimulator()
    strategy = CleanWithStopVariant(sim, "current_live", stop_cents=5)

    discrepancies = []
    matches = []
    for session, live_pnl in matched:
        paper_trade = strategy.evaluate_signal(session.signals[0], session)
        if not paper_trade or not paper_trade.entered:
            continue
        paper_pnl = paper_trade.pnl
        diff = paper_pnl - live_pnl
        discrepancies.append(diff)
        matches.append({
            "ticker": session.ticker[-18:],
            "side": paper_trade.side,
            "ct": paper_trade.entry_ct,
            "px": paper_trade.entry_price,
            "paper_pnl": paper_pnl,
            "live_pnl": live_pnl,
            "diff": diff,
            "settled": session.settled_result,
        })

    # Summary
    total_paper = sum(m["paper_pnl"] for m in matches)
    total_live = sum(m["live_pnl"] for m in matches)
    avg_diff = sum(discrepancies) / len(discrepancies) if discrepancies else 0

    print(f"{'Source':<18}  {'Total P&L':>10}  {'Avg/trade':>11}")
    print("-" * 45)
    print(f"{'PAPER (modeled)':<18}  ${total_paper:>+8.2f}  ${total_paper/max(len(matches),1):>+8.3f}")
    print(f"{'LIVE (balance)':<18}  ${total_live:>+8.2f}  ${total_live/max(len(matches),1):>+8.3f}")
    print(f"{'DIFF':<18}  ${total_paper-total_live:>+8.2f}  ${avg_diff:>+8.3f}")
    print()

    # Categorize discrepancies
    paper_overestimate = [m for m in matches if m["diff"] > 0.5]  # paper > live by 50c
    paper_underestimate = [m for m in matches if m["diff"] < -0.5]
    close_match = [m for m in matches if abs(m["diff"]) <= 0.5]

    print(f"Paper OVERESTIMATES (paper > live): {len(paper_overestimate)} trades")
    print(f"Paper UNDERESTIMATES (paper < live): {len(paper_underestimate)} trades")
    print(f"Close match (within 50c): {len(close_match)} trades")
    print()

    # Show worst overestimates (paper predicted big win, reality was bad)
    print(f"TOP 8 PAPER OVERESTIMATES (paper too optimistic):")
    print(f"  {'ticker':<20} {'side':>4} {'ct':>3} {'entry':>6}  {'paper':>8}  {'live':>8}  {'diff':>8}")
    for m in sorted(matches, key=lambda x: -x["diff"])[:8]:
        print(f"  {m['ticker']:<20} {m['side']:>4} {m['ct']:>3} {m['px']:>5}c  ${m['paper_pnl']:>+6.2f}  ${m['live_pnl']:>+6.2f}  ${m['diff']:>+6.2f}")

    print()
    print(f"TOP 8 PAPER UNDERESTIMATES (paper too pessimistic):")
    print(f"  {'ticker':<20} {'side':>4} {'ct':>3} {'entry':>6}  {'paper':>8}  {'live':>8}  {'diff':>8}")
    for m in sorted(matches, key=lambda x: x["diff"])[:8]:
        print(f"  {m['ticker']:<20} {m['side']:>4} {m['ct']:>3} {m['px']:>5}c  ${m['paper_pnl']:>+6.2f}  ${m['live_pnl']:>+6.2f}  ${m['diff']:>+6.2f}")

    print()

    # By settle_result
    print("BY SETTLEMENT OUTCOME:")
    for r in ['yes', 'no']:
        sub = [m for m in matches if m.get('settled', '') == r]
        if not sub:
            continue
        avg_diff = sum(x["diff"] for x in sub) / len(sub)
        avg_paper = sum(x["paper_pnl"] for x in sub) / len(sub)
        avg_live = sum(x["live_pnl"] for x in sub) / len(sub)
        print(f"  settled={r}: n={len(sub)}  avg_paper=${avg_paper:+.2f}  avg_live=${avg_live:+.2f}  avg_diff=${avg_diff:+.2f}")

    print()

    # Win/loss WR comparison
    paper_wins = sum(1 for m in matches if m["paper_pnl"] > 0.05)
    live_wins = sum(1 for m in matches if m["live_pnl"] > 0.05)
    print(f"WIN RATE COMPARISON:")
    print(f"  Paper predicts: {paper_wins}/{len(matches)} = {paper_wins/len(matches)*100:.0f}%")
    print(f"  Live actual:    {live_wins}/{len(matches)} = {live_wins/len(matches)*100:.0f}%")


if __name__ == '__main__':
    validate()
