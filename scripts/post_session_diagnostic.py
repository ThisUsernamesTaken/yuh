"""Post-session diagnostic — run after any active trading session.

Parses engine_history.log for the new instrumented exit fields:
  - slippage, fill_mode, elapsed_ms (Phase B)
  - MFE, MAE (Phase B addendum)
  - exit_reason (reversal, take_profit, bid_collapse, tp_timeout, tp_erosion, tp_ceiling)

Reports:
  1. TP slippage by fill_mode
  2. MAE/MFE vs realized P&L
  3. Bid-collapse exit P&L
  4. Main-engine fill count + edge rejection rate
  5. Exit efficiency summary

Usage:
    python scripts/post_session_diagnostic.py [--since YYYY-MM-DD]
"""

import re
import sys
from collections import defaultdict
from datetime import datetime

LOG_FILE = "data/engine_history.log"


def parse_args():
    since = None
    for i, arg in enumerate(sys.argv):
        if arg == "--since" and i + 1 < len(sys.argv):
            since = sys.argv[i + 1]
    return since


def main():
    since_str = parse_args()
    since_dt = datetime.fromisoformat(since_str) if since_str else None

    # ── Patterns ──────────────────────────────────────────────────────
    # TP hold FILLED
    tp_filled_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"TP hold FILLED @ (\d+).*entry=(\d+\.?\d*).*pnl=([+-]?\d+\.\d+).*"
        r"MFE=\+(\d+\.?\d*).*MAE=-(\d+\.?\d*).*"
        r"slippage=([+-]?\d+).*trigger_bid=(\d+).*elapsed=(\d+)ms.*fill_mode=(\w+)"
    )
    # TP hold force-exit
    tp_force_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"TP hold force-exit \[(\w+)\].*entry=(\d+\.?\d*).*sell=(\d+\.?\d*).*pnl=([+-]?\d+\.\d+).*"
        r"MFE=\+(\d+\.?\d*).*MAE=-(\d+\.?\d*).*"
        r"slippage=([+-]?\d+).*trigger_bid=(\d+).*elapsed=(\d+)ms.*fill_mode=(\w+)"
    )
    # Early exit (non-TP)
    early_exit_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"Early exit \[(\w+)\].*entry=(\d+\.?\d*)c sell=(\d+\.?\d*)c.*pnl=([+-]?\d+\.\d+).*"
        r"MFE=\+(\d+\.?\d*)c MAE=-(\d+\.?\d*)c"
    )
    # Natural expiry outcome
    outcome_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
        r"Outcome: (WIN|LOSS).*pnl=([+-]?\d+\.\d+).*"
        r"MFE=\+(\d+\.?\d*).*MAE=-(\d+\.?\d*)"
    )
    # Edge rejection
    edge_reject_re = re.compile(r"Trade rejected: insufficient edge")
    # Trade intent (new fill)
    trade_intent_re = re.compile(r"Trade intent:")
    # TP hold posted
    tp_posted_re = re.compile(r"TP hold: limit sell.*fill_mode=(\w+)")

    # ── Accumulators ──────────────────────────────────────────────────
    exits = []  # All exit events
    tp_fill_mode_stats = defaultdict(lambda: {"count": 0, "slippage": [], "elapsed_ms": [], "pnl": []})
    exit_reason_stats = defaultdict(lambda: {"count": 0, "pnl": [], "mfe": [], "mae": []})
    edge_rejects = 0
    trade_intents = 0
    tp_posted_modes = defaultdict(int)

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Date filter
                if since_dt:
                    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        line_dt = datetime.fromisoformat(m.group(1))
                        if line_dt < since_dt:
                            continue

                # TP hold FILLED
                m = tp_filled_re.search(line)
                if m:
                    ts, fill_price, entry, pnl, mfe, mae, slippage, trigger_bid, elapsed, fill_mode = m.groups()
                    rec = {
                        "ts": ts, "exit_reason": "tp_filled", "fill_mode": fill_mode,
                        "entry": float(entry), "exit": float(fill_price), "pnl": float(pnl),
                        "mfe": float(mfe), "mae": float(mae),
                        "slippage": int(slippage), "trigger_bid": int(trigger_bid),
                        "elapsed_ms": int(elapsed),
                    }
                    exits.append(rec)
                    tp_fill_mode_stats[fill_mode]["count"] += 1
                    tp_fill_mode_stats[fill_mode]["slippage"].append(rec["slippage"])
                    tp_fill_mode_stats[fill_mode]["elapsed_ms"].append(rec["elapsed_ms"])
                    tp_fill_mode_stats[fill_mode]["pnl"].append(rec["pnl"])
                    exit_reason_stats["tp_filled"]["count"] += 1
                    exit_reason_stats["tp_filled"]["pnl"].append(rec["pnl"])
                    exit_reason_stats["tp_filled"]["mfe"].append(rec["mfe"])
                    exit_reason_stats["tp_filled"]["mae"].append(rec["mae"])
                    continue

                # TP hold force-exit
                m = tp_force_re.search(line)
                if m:
                    ts, reason, entry, sell, pnl, mfe, mae, slippage, trigger_bid, elapsed, fill_mode = m.groups()
                    rec = {
                        "ts": ts, "exit_reason": f"tp_{reason}", "fill_mode": fill_mode,
                        "entry": float(entry), "exit": float(sell), "pnl": float(pnl),
                        "mfe": float(mfe), "mae": float(mae),
                        "slippage": int(slippage), "trigger_bid": int(trigger_bid),
                        "elapsed_ms": int(elapsed),
                    }
                    exits.append(rec)
                    tp_fill_mode_stats[fill_mode]["count"] += 1
                    tp_fill_mode_stats[fill_mode]["slippage"].append(rec["slippage"])
                    tp_fill_mode_stats[fill_mode]["elapsed_ms"].append(rec["elapsed_ms"])
                    tp_fill_mode_stats[fill_mode]["pnl"].append(rec["pnl"])
                    exit_reason_stats[rec["exit_reason"]]["count"] += 1
                    exit_reason_stats[rec["exit_reason"]]["pnl"].append(rec["pnl"])
                    exit_reason_stats[rec["exit_reason"]]["mfe"].append(rec["mfe"])
                    exit_reason_stats[rec["exit_reason"]]["mae"].append(rec["mae"])
                    continue

                # Early exit (reversal, bid_collapse, take_profit without TP hold)
                m = early_exit_re.search(line)
                if m:
                    ts, reason, entry, sell, pnl, mfe, mae = m.groups()
                    rec = {
                        "ts": ts, "exit_reason": reason,
                        "entry": float(entry), "exit": float(sell), "pnl": float(pnl),
                        "mfe": float(mfe), "mae": float(mae),
                    }
                    exits.append(rec)
                    exit_reason_stats[reason]["count"] += 1
                    exit_reason_stats[reason]["pnl"].append(rec["pnl"])
                    exit_reason_stats[reason]["mfe"].append(rec["mfe"])
                    exit_reason_stats[reason]["mae"].append(rec["mae"])
                    continue

                # Natural expiry
                m = outcome_re.search(line)
                if m:
                    ts, result, pnl, mfe, mae = m.groups()
                    rec = {
                        "ts": ts, "exit_reason": f"expiry_{result.lower()}",
                        "pnl": float(pnl), "mfe": float(mfe), "mae": float(mae),
                    }
                    exits.append(rec)
                    exit_reason_stats[rec["exit_reason"]]["count"] += 1
                    exit_reason_stats[rec["exit_reason"]]["pnl"].append(rec["pnl"])
                    exit_reason_stats[rec["exit_reason"]]["mfe"].append(rec["mfe"])
                    exit_reason_stats[rec["exit_reason"]]["mae"].append(rec["mae"])
                    continue

                # Counters
                if edge_reject_re.search(line):
                    edge_rejects += 1
                if trade_intent_re.search(line):
                    trade_intents += 1
                m = tp_posted_re.search(line)
                if m:
                    tp_posted_modes[m.group(1)] += 1

    except FileNotFoundError:
        print(f"Log file not found: {LOG_FILE}")
        return

    # ── Report ────────────────────────────────────────────────────────
    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    def median(lst):
        if not lst:
            return 0
        s = sorted(lst)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    print("=" * 70)
    print("POST-SESSION DIAGNOSTIC")
    print("=" * 70)

    # 1. TP slippage by fill_mode
    print("\n-- 1. TP Slippage by fill_mode --")
    print(f"{'fill_mode':<15} {'count':>5} {'avg_slip':>9} {'med_slip':>9} {'avg_ms':>8} {'avg_pnl':>8}")
    print("-" * 60)
    for mode in sorted(tp_fill_mode_stats.keys()):
        s = tp_fill_mode_stats[mode]
        print(f"{mode:<15} {s['count']:>5} {avg(s['slippage']):>+8.1f}c {median(s['slippage']):>+8.1f}c "
              f"{avg(s['elapsed_ms']):>7.0f}ms ${avg(s['pnl']):>+6.3f}")

    # 2. Exit reason summary
    print("\n-- 2. Exit Reason Summary --")
    print(f"{'reason':<20} {'count':>5} {'avg_pnl':>8} {'sum_pnl':>8} {'avg_MFE':>8} {'avg_MAE':>8}")
    print("-" * 65)
    for reason in sorted(exit_reason_stats.keys()):
        s = exit_reason_stats[reason]
        print(f"{reason:<20} {s['count']:>5} ${avg(s['pnl']):>+6.3f} ${sum(s['pnl']):>+6.2f} "
              f"{avg(s['mfe']):>+7.1f}c {avg(s['mae']):>+7.1f}c")

    # 3. Main engine fill count + edge rejections
    print("\n-- 3. Main Engine Activity --")
    print(f"  Trade intents submitted:  {trade_intents}")
    print(f"  Edge rejections:          {edge_rejects}")
    print(f"  TP orders posted:         {sum(tp_posted_modes.values())}")
    for mode, count in sorted(tp_posted_modes.items()):
        print(f"    {mode}: {count}")

    # 4. Overall summary
    total_exits = len(exits)
    total_pnl = sum(e["pnl"] for e in exits)
    wins = sum(1 for e in exits if e["pnl"] > 0)
    losses = sum(1 for e in exits if e["pnl"] <= 0)
    print(f"\n-- 4. Overall --")
    if total_exits:
        print(f"  Total exits:   {total_exits}")
        print(f"  Wins/Losses:   {wins}W / {losses}L ({100*wins/total_exits:.1f}% WR)")
        print(f"  Total P&L:     ${total_pnl:+.2f}")
        all_mfe = [e.get("mfe", 0) for e in exits if "mfe" in e]
        all_mae = [e.get("mae", 0) for e in exits if "mae" in e]
        print(f"  Avg MFE:       +{avg(all_mfe):.1f}c")
        print(f"  Avg MAE:       -{avg(all_mae):.1f}c")
    else:
        print("  No exits found in log")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
