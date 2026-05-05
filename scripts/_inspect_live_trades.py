"""Inspect live engine trade history.

Pulls aggregate stats from data/trades.db:
  - kalshi_trades (live engine fills + closes)
  - settlement_ledger (window settlement outcomes)
  - balance_snapshots (BAL trajectory)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "trades.db"


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1
    con = sqlite3.connect(str(DB))
    cur = con.cursor()

    print("=== kalshi_trades schema ===")
    cur.execute("PRAGMA table_info(kalshi_trades)")
    for row in cur.fetchall():
        print(f"  {row[1]:<25} {row[2]}")

    print()
    print("=== kalshi_trades summary ===")
    cur.execute("SELECT COUNT(*) FROM kalshi_trades")
    print(f"  total rows: {cur.fetchone()[0]}")

    from datetime import datetime, timezone

    cur.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM kalshi_trades "
        "WHERE created_at IS NOT NULL"
    )
    lo, hi = cur.fetchone()
    if lo and hi:
        d_lo = datetime.fromtimestamp(lo / 1000, tz=timezone.utc)
        d_hi = datetime.fromtimestamp(hi / 1000, tz=timezone.utc)
        print(f"  date range: {d_lo} -> {d_hi}")
        days = (hi - lo) / 1000 / 86400
        print(f"  span: {days:.1f} days")

    print()
    print("=== Live P&L by strategy (all-time, kalshi_trades.pnl is in $) ===")
    cur.execute(
        """
        SELECT COALESCE(strategy_name, '?') AS strat, COUNT(*) as n,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN pnl IS NULL THEN 1 ELSE 0 END) as open_or_unsettled,
               COALESCE(SUM(pnl), 0) as total_pnl,
               AVG(pnl) as avg_pnl
        FROM kalshi_trades
        GROUP BY strategy_name
        ORDER BY total_pnl DESC
        """
    )
    rows = cur.fetchall()
    if rows:
        print(
            f"  {'strategy':<22} {'n':>4} {'wins':>4} {'loss':>4} {'open':>5} "
            f"{'hit%':>6}  total $    avg $"
        )
        for s, n, w, l, op, t, a in rows:
            settled = (w or 0) + (l or 0)
            hit = (w / settled * 100) if settled else 0.0
            avg = a if a is not None else 0.0
            print(
                f"  {s:<22} {n:>4d} {w or 0:>4d} {l or 0:>4d} {op or 0:>5d} "
                f"{hit:>5.1f}% {t:>+8.2f} {avg:>+7.2f}"
            )
    else:
        print("  (no rows)")

    print()
    print("=== Latest 15 trades ===")
    cur.execute(
        """
        SELECT created_at, ticker, side, strategy_name, count, limit_price,
               status, result, pnl
        FROM kalshi_trades
        ORDER BY created_at DESC
        LIMIT 15
        """
    )
    rows = cur.fetchall()
    for r in rows:
        ts, tkr, side, strat, cnt, lp, st, res, pnl = r
        when = "?"
        if ts:
            when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        ticker_short = (tkr or "")[-15:]
        pnl_str = f"{pnl:+.2f}" if pnl is not None else "(open)"
        print(
            f"  {when} {ticker_short:<15} {(side or '?'):<3} "
            f"{(strat or '?'):<14} {cnt or 0:>3}ct @ {lp or 0:>3}c "
            f"st={st or '?':<10} res={res or '?':<6} {pnl_str}"
        )

    print()
    print("=== Bankroll trajectory (last 20 snapshots) ===")
    cur.execute("PRAGMA table_info(balance_snapshots)")
    cols = [r[1] for r in cur.fetchall()]
    # Try several candidate column shapes
    ts_col = next((c for c in ("ts", "snapshot_ts_ms", "ts_ms", "created_at") if c in cols), None)
    bal_col = next((c for c in ("balance_cents", "balance", "bal_cents") if c in cols), None)
    src_col = next((c for c in ("note", "source", "reason", "tag") if c in cols), None)
    if not ts_col or not bal_col:
        print(f"  (skipped: balance_snapshots cols={cols})")
    else:
        sel = f"{ts_col}, {bal_col}" + (f", {src_col}" if src_col else "")
        cur.execute(
            f"SELECT {sel} FROM balance_snapshots ORDER BY {ts_col} DESC LIMIT 20"
        )
        rows = cur.fetchall()
        for r in rows:
            ts = r[0]
            bal = r[1]
            src = r[2] if len(r) > 2 else ""
            when = "?"
            try:
                if isinstance(ts, str):
                    when = ts[:16]  # ISO-ish
                elif ts:
                    when = datetime.fromtimestamp(
                        ts / 1000, tz=timezone.utc
                    ).strftime("%m-%d %H:%M")
            except Exception:
                pass
            # heuristic: if bal looks like cents (>1000) divide; else assume $
            try:
                b = float(bal or 0)
            except Exception:
                b = 0.0
            bal_d = b / 100 if b > 1000 else b
            print(f"  {when:<19}  ${bal_d:>7.2f}  {(src or '')[:40]}")

    print()
    print("=== Settlement ledger (last 20 by ticker) ===")
    cur.execute("PRAGMA table_info(settlement_ledger)")
    sl_cols = [r[1] for r in cur.fetchall()]
    print(f"  cols: {sl_cols}")
    if "market_result" in sl_cols and "ticker" in sl_cols:
        ts_c = next((c for c in ("settled_ts_ms", "ts_ms", "settle_ts_ms", "created_at") if c in sl_cols), None)
        sel = f"ticker, market_result" + (f", {ts_c}" if ts_c else "")
        order = f"ORDER BY {ts_c} DESC" if ts_c else ""
        cur.execute(f"SELECT {sel} FROM settlement_ledger {order} LIMIT 20")
        for row in cur.fetchall():
            tkr = row[0]
            res = row[1]
            ts = row[2] if len(row) > 2 else None
            when = (
                datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
                if ts else "?"
            )
            print(f"  {when} {(tkr or '')[-20:]:<20} {res or '?'}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
