"""On-demand ATM live graduation check.

Run any time to see the per-contract net + verdict against the
ATM_LIVE_GRADUATION_HEALTHY_C / _MARGINAL_C thresholds.

Engine-side equivalent (`_atm_live_graduation_check`) emits a one-shot log
line when fills cross ATM_LIVE_GRADUATION_FILL_COUNT, but this script
works at ANY count and breaks down by exit_reason / side / row.

Usage:
    python scripts/atm_live_graduation_check.py
    python scripts/atm_live_graduation_check.py --limit 50
    python scripts/atm_live_graduation_check.py --since 2026-04-26

Per Claude 2026-04-26 graduation plan. Verdict thresholds match the
engine-side helper so log emit and standalone script agree.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trades.db"

# Thresholds (mirror user_config defaults; user_config import is optional so
# the script runs from a clean venv).
DEFAULT_HEALTHY_C = 2.5
DEFAULT_MARGINAL_C = 1.0
DEFAULT_FILL_COUNT = 25


def _load_thresholds() -> tuple[float, float, int]:
    sys.path.insert(0, str(ROOT))
    try:
        import user_config
        return (
            float(getattr(user_config, "ATM_LIVE_GRADUATION_HEALTHY_C", DEFAULT_HEALTHY_C)),
            float(getattr(user_config, "ATM_LIVE_GRADUATION_MARGINAL_C", DEFAULT_MARGINAL_C)),
            int(getattr(user_config, "ATM_LIVE_GRADUATION_FILL_COUNT", DEFAULT_FILL_COUNT)),
        )
    except Exception:
        return DEFAULT_HEALTHY_C, DEFAULT_MARGINAL_C, DEFAULT_FILL_COUNT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Limit to last N close rows (0 = all)")
    ap.add_argument("--since", type=str, default="",
                    help="Only count closes created at-or-after this ISO date "
                         "(e.g. 2026-04-26)")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: {DB} not found.")
        return 1

    healthy_c, marginal_c, milestone = _load_thresholds()

    where = "row_kind = 'close' AND contracts > 0"
    params: list = []
    if args.since:
        where += " AND created_at >= ?"
        params.append(args.since)

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    if args.limit > 0:
        cur.execute(
            f"SELECT side, contracts, entry_price_c, exit_price_c, "
            f"       exit_reason, gross_cents, net_cents, ticker, created_at "
            f"FROM atm_reversion_live_fills WHERE {where} "
            f"ORDER BY id DESC LIMIT ?",
            (*params, args.limit),
        )
    else:
        cur.execute(
            f"SELECT side, contracts, entry_price_c, exit_price_c, "
            f"       exit_reason, gross_cents, net_cents, ticker, created_at "
            f"FROM atm_reversion_live_fills WHERE {where} "
            f"ORDER BY id ASC",
            params,
        )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No close rows in atm_reversion_live_fills.")
        return 0

    n = len(rows)
    wins = sum(1 for r in rows if (r[6] or 0) > 0)
    total_net = sum(r[6] or 0 for r in rows)
    avg_net_per_ct = sum((r[6] or 0) / max(1, r[1] or 1) for r in rows) / n
    avg_entry = sum((r[2] or 0) for r in rows) / n
    avg_exit = sum((r[3] or 0) for r in rows) / n

    print(f"=== ATM live graduation check ===")
    print(f"  fills            : {n}")
    print(f"  win rate         : {wins/n*100:.1f}% ({wins}/{n})")
    print(f"  avg entry        : {avg_entry:.1f}c")
    print(f"  avg exit         : {avg_exit:.1f}c")
    print(f"  avg net/contract : {avg_net_per_ct:+.2f}c")
    print(f"  total net        : {total_net:+}c (${total_net/100:+.2f})")
    print()

    # Verdict
    if avg_net_per_ct >= healthy_c:
        verdict = f"HEALTHY (>={healthy_c:+.1f}c/ct — keep ATM as primary)"
    elif avg_net_per_ct >= marginal_c:
        verdict = f"MARGINAL ({marginal_c:+.1f}c..{healthy_c:+.1f}c/ct — review)"
    else:
        verdict = (f"UNDERPERFORMING (<{marginal_c:+.1f}c/ct — recommend reverting "
                   f"to LEGACY or running BOTH)")
    print(f"  verdict          : {verdict}")
    if n < milestone:
        print(f"  (note: < {milestone} fills, verdict is preliminary)")
    print()

    # Breakdowns
    print("-- by side --")
    for side in ("yes", "no"):
        sub = [r for r in rows if r[0] == side]
        if not sub:
            continue
        sub_n = len(sub)
        sub_wins = sum(1 for r in sub if (r[6] or 0) > 0)
        sub_avg_per_ct = sum((r[6] or 0) / max(1, r[1] or 1) for r in sub) / sub_n
        sub_total = sum(r[6] or 0 for r in sub)
        print(f"  {side.upper():<3}  n={sub_n:>3}  WR={sub_wins/sub_n*100:>5.1f}%  "
              f"avg_net/ct={sub_avg_per_ct:+5.2f}c  total={sub_total:+}c")

    print()
    print("-- by exit reason --")
    by_reason: dict = {}
    for r in rows:
        reason = r[4] or "?"
        by_reason.setdefault(reason, []).append(r)
    for reason in sorted(by_reason, key=lambda k: -len(by_reason[k])):
        sub = by_reason[reason]
        sub_n = len(sub)
        sub_avg_per_ct = sum((r[6] or 0) / max(1, r[1] or 1) for r in sub) / sub_n
        sub_total = sum(r[6] or 0 for r in sub)
        print(f"  {reason:<28} n={sub_n:>3}  avg_net/ct={sub_avg_per_ct:+5.2f}c  "
              f"total={sub_total:+}c")

    return 0


if __name__ == "__main__":
    sys.exit(main())
