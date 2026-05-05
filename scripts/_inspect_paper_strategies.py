"""Inspect aggregate stats for ALL paper/shadow strategies that have
historical data on disk in data/trades.db.

These strategies have been quietly logging shadow trades for weeks. We
have settlement-confirmed P&L per row in most cases — that's a
ready-made backtest result.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "trades.db"


def cols_for(cur, table: str) -> list[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def show_table_summary(cur, table: str, *, pnl_col_candidates: list[str]):
    cols = cols_for(cur, table)
    if not cols:
        print(f"  {table}: (missing)")
        return
    pnl_col = next((c for c in pnl_col_candidates if c in cols), None)
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    n_total = cur.fetchone()[0]
    print(f"\n=== {table} ({n_total:,} rows) ===")
    print(f"  cols: {cols}")
    if not pnl_col:
        print(f"  (no pnl column found in {pnl_col_candidates})")
        return
    cur.execute(
        f"SELECT COUNT(*), "
        f"SUM(CASE WHEN {pnl_col} > 0 THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {pnl_col} < 0 THEN 1 ELSE 0 END), "
        f"SUM({pnl_col}), AVG({pnl_col}), "
        f"MIN({pnl_col}), MAX({pnl_col}) "
        f"FROM {table} WHERE {pnl_col} IS NOT NULL"
    )
    n, w, l, total, avg, mn, mx = cur.fetchone()
    if not n:
        print(f"  (no rows)")
        return
    settled = (w or 0) + (l or 0)
    hit = (w / settled * 100) if settled else 0
    unit = "c" if avg and abs(avg) > 5 else "$"
    total_str = f"{total:.2f}{unit}" if total is not None else "?"
    avg_str = f"{avg:.3f}{unit}" if avg is not None else "?"
    print(
        f"  {pnl_col}: n={n} wins={w or 0} losses={l or 0} hit%={hit:.1f}  "
        f"total={total_str}  avg={avg_str}  min={mn}  max={mx}"
    )


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1
    con = sqlite3.connect(str(DB))
    cur = con.cursor()

    paper_tables = [
        ("paper_fvg_trades",         ["pnl", "pnl_cents", "pnl_dollars"]),
        ("paper_level_entries",      ["pnl_if_entered_cents", "pnl",
                                       "pnl_cents", "pnl_dollars"]),
        ("paper_entries",            ["live_pnl_cents", "vwap_pnl_cents",
                                       "pnl", "pnl_cents", "pnl_dollars"]),
        ("paper_bounce_cycles",      ["pnl", "pnl_cents", "pnl_dollars"]),
        ("atm_reversion_paper_trades", ["pnl", "pnl_cents", "pnl_dollars",
                                         "pnl_net_c", "pnl_gross_c"]),
        ("atm_reversion_live_fills", ["pnl", "pnl_cents", "pnl_dollars"]),
        ("atm_reversion_entry_shadow", ["pnl", "pnl_cents",
                                          "filled_simulated"]),
        ("codex_paper_snapback_trades", ["pnl", "pnl_cents", "pnl_dollars"]),
        ("sr_fade_excursions",       ["pnl", "pnl_cents", "max_excursion_c"]),
        ("trade_decisions",          ["pnl", "pnl_cents", "pnl_dollars"]),
        ("tp_ghost_crossings",       ["pnl", "pnl_cents", "pnl_dollars"]),
    ]

    for table, pnl_candidates in paper_tables:
        show_table_summary(cur, table, pnl_col_candidates=pnl_candidates)

    print()
    print("=== paper_fvg_trades — by exit_reason ===")
    cols = cols_for(cur, "paper_fvg_trades")
    if "exit_reason" in cols and "pnl_cents" in cols:
        cur.execute(
            "SELECT exit_reason, COUNT(*), "
            "SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END), "
            "SUM(pnl_cents), AVG(pnl_cents), MIN(pnl_cents), MAX(pnl_cents) "
            "FROM paper_fvg_trades WHERE pnl_cents IS NOT NULL "
            "GROUP BY exit_reason ORDER BY COUNT(*) DESC"
        )
        print(f"  {'exit_reason':<22} {'n':>5} {'wins':>5} "
              f"{'total $':>9} {'avg $':>8} {'min':>7} {'max':>7}")
        for r in cur.fetchall():
            er, n, w, t, a, mn, mx = r
            print(f"  {(er or '?'):<22} {n:>5} {w or 0:>5} "
                  f"{(t or 0)/100:>+8.2f} {(a or 0)/100:>+7.3f} "
                  f"{(mn or 0)/100:>+6.2f} {(mx or 0)/100:>+6.2f}")

    # legacy code from before — keep for compatibility
    if False and "paper_fvg_trades" in [t[0] for t in paper_tables]:
        cols = cols_for(cur, "paper_fvg_trades")
        if "reason" in cols and "pnl" in cols:
            cur.execute(
                "SELECT reason, COUNT(*), "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), "
                "SUM(pnl), AVG(pnl) "
                "FROM paper_fvg_trades WHERE pnl IS NOT NULL "
                "GROUP BY reason ORDER BY COUNT(*) DESC"
            )
            print(f"  {'reason':<24} {'n':>5} {'wins':>5} {'total':>9} {'avg':>8}")
            for r in cur.fetchall():
                reason, n, w, t, a = r
                u = "$" if a and abs(a) <= 5 else "c"
                print(f"  {(reason or '?'):<24} {n:>5} {w or 0:>5} "
                      f"{t:>+8.2f}{u} {a:>+7.3f}{u}")

    print()
    print("=== paper_level_entries — by setup_type ===")
    cols = cols_for(cur, "paper_level_entries")
    pnl_col = "pnl_if_entered_cents" if "pnl_if_entered_cents" in cols else None
    if "setup_type" in cols and pnl_col:
        cur.execute(
            f"SELECT setup_type, COUNT(*), "
            f"SUM(CASE WHEN {pnl_col} > 0 THEN 1 ELSE 0 END), "
            f"SUM({pnl_col}), AVG({pnl_col}) "
            f"FROM paper_level_entries WHERE {pnl_col} IS NOT NULL "
            f"GROUP BY setup_type ORDER BY COUNT(*) DESC"
        )
        print(f"  {'setup_type':<22} {'n':>5} {'wins':>5} {'total $':>9} {'avg $':>8}")
        for r in cur.fetchall():
            st, n, w, t, a = r
            print(f"  {(st or '?'):<22} {n:>5} {w or 0:>5} "
                  f"{(t or 0)/100:>+8.2f} {(a or 0)/100:>+7.3f}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
