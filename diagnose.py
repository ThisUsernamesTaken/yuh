#!/usr/bin/env python3
"""
diagnose.py — Execution quality report for the BTC Bias Engine
==============================================================
Shows the signal→execution funnel, where orders are being blocked,
market condition stats, and actual vs hypothetical P&L.

Usage:
    python diagnose.py                 # last 24 hours
    python diagnose.py --days 7        # last 7 days
    python diagnose.py --all           # all time
    python diagnose.py --db path/to/trades.db
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import TRADES_DB_PATH, SIGNALS_DB_PATH


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "  n/a"
    return f"{num / den * 100:5.1f}%"


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _tte_bucket(minutes: float) -> str:
    if minutes >= 10:
        return "10m+"
    if minutes >= 7:
        return "7-10m"
    if minutes >= 4:
        return "4-7m"
    if minutes >= 2:
        return "2-4m"
    return "<2m"


def run_report(db_path: str, since_ts_ms: int) -> None:
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # ── Check table exists ──────────────────────────────────────────────
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "execution_log" not in tables:
        print("execution_log table not found — no execution data yet.")
        con.close()
        return

    rows = con.execute(
        "SELECT * FROM execution_log WHERE created_at >= ? ORDER BY created_at",
        (since_ts_ms,)
    ).fetchall()

    if not rows:
        print("No execution_log rows in this time window.")
        con.close()
        return

    # ── Gate funnel ─────────────────────────────────────────────────────
    n_approved       = len(rows)
    n_pricing        = sum(1 for r in rows if r["pricing_eligible"])
    n_liquidity      = sum(1 for r in rows if r["liquidity_eligible"])
    n_execution      = sum(1 for r in rows if r["execution_eligible"])
    n_submitted      = sum(1 for r in rows if r["submitted"])
    n_filled         = sum(1 for r in rows if r["filled"])

    # ── Rejection breakdown ─────────────────────────────────────────────
    no_contract   = sum(1 for r in rows if not r["contract_ticker"])
    time_expired  = sum(1 for r in rows if r["contract_ticker"] and not r["pricing_eligible"]
                        and r["minutes_to_expiry"] is not None and r["minutes_to_expiry"] < 4.0)
    spread_wide   = sum(1 for r in rows if r["contract_ticker"] and not r["liquidity_eligible"]
                        and r["spread_cents"] is not None and r["spread_cents"] > 20)
    low_vol       = sum(1 for r in rows if r["contract_ticker"] and not r["liquidity_eligible"]
                        and r["display_volume"] is not None and r["display_volume"] < 25)
    low_edge      = sum(1 for r in rows if r["pricing_eligible"] and r["liquidity_eligible"]
                        and not r["execution_eligible"] and r["submitted"] == 0)
    quote_stale   = sum(1 for r in rows if r["quote_age_ms"] is not None
                        and r["quote_age_ms"] > 2000)

    # ── Market condition stats (submitted rows only) ────────────────────
    submitted_rows = [r for r in rows if r["submitted"]]
    spreads        = [r["spread_cents"] for r in submitted_rows if r["spread_cents"] is not None]
    volumes        = [r["display_volume"] for r in submitted_rows if r["display_volume"] is not None]
    tte_vals       = [r["minutes_to_expiry"] for r in submitted_rows if r["minutes_to_expiry"] is not None]
    quote_ages     = [r["quote_age_ms"] for r in submitted_rows if r["quote_age_ms"] is not None]

    # ── P&L ─────────────────────────────────────────────────────────────
    actual_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)

    # Hypothetical: if all submitted orders had filled, what would P&L be?
    hypo_pnl = 0.0
    for r in submitted_rows:
        if r["expiry_outcome"] is not None and r["submit_price"] is not None:
            price_frac = r["submit_price"] / 100.0
            won = (r["side"] == r["expiry_outcome"])
            hypo_pnl += (1.0 - price_frac) if won else -price_frac

    # ── Session breakdown ───────────────────────────────────────────────
    session_counts: dict[str, dict] = {}
    for r in rows:
        sess = r["session"] or "unknown"
        if sess not in session_counts:
            session_counts[sess] = {"approved": 0, "submitted": 0, "filled": 0}
        session_counts[sess]["approved"] += 1
        if r["submitted"]:
            session_counts[sess]["submitted"] += 1
        if r["filled"]:
            session_counts[sess]["filled"] += 1

    # ── TTE bucket breakdown ────────────────────────────────────────────
    tte_buckets: dict[str, dict] = {}
    for r in submitted_rows:
        if r["minutes_to_expiry"] is None:
            continue
        bucket = _tte_bucket(r["minutes_to_expiry"])
        if bucket not in tte_buckets:
            tte_buckets[bucket] = {"submitted": 0, "filled": 0}
        tte_buckets[bucket]["submitted"] += 1
        if r["filled"]:
            tte_buckets[bucket]["filled"] += 1

    # ── Regime breakdown ────────────────────────────────────────────────
    regime_counts: dict[str, dict] = {}
    for r in submitted_rows:
        reg = r["regime"] or "unknown"
        if reg not in regime_counts:
            regime_counts[reg] = {"submitted": 0, "filled": 0}
        regime_counts[reg]["submitted"] += 1
        if r["filled"]:
            regime_counts[reg]["filled"] += 1

    con.close()

    # ── Print ────────────────────────────────────────────────────────────
    since_str = datetime.fromtimestamp(since_ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print("=" * 64)
    print("  BTC BIAS ENGINE — Execution Diagnostic Report")
    print(f"  Since: {since_str}")
    print("=" * 64)

    print("\n── Signal → Execution Funnel ─────────────────────────────────")
    print(f"  Approved signals      : {n_approved:>5d}")
    print(f"  Pricing-eligible      : {n_pricing:>5d}  ({_pct(n_pricing, n_approved)})")
    print(f"  Liquidity-eligible    : {n_liquidity:>5d}  ({_pct(n_liquidity, n_approved)})")
    print(f"  Execution-eligible    : {n_execution:>5d}  ({_pct(n_execution, n_approved)})")
    print(f"  Submitted             : {n_submitted:>5d}  ({_pct(n_submitted, n_approved)})")
    print(f"  Filled                : {n_filled:>5d}  ({_pct(n_filled, n_submitted)})")

    print("\n── Gate Rejection Breakdown ──────────────────────────────────")
    print(f"  No contract found     : {no_contract}")
    print(f"  Time-to-expiry low    : {time_expired}")
    print(f"  Spread too wide       : {spread_wide}")
    print(f"  Volume too low        : {low_vol}")
    print(f"  Edge insufficient     : {low_edge}")
    print(f"  Quote stale (>2s)     : {quote_stale}")

    print("\n── Market Conditions at Submission ───────────────────────────")
    if spreads:
        print(f"  Spread  median={_median(spreads):.0f}¢  min={min(spreads):.0f}¢  max={max(spreads):.0f}¢")
    if volumes:
        print(f"  Volume  median={_median(volumes):.0f}   min={min(volumes):.0f}   max={max(volumes):.0f}")
    if tte_vals:
        print(f"  TTE     median={_median(tte_vals):.1f}m  min={min(tte_vals):.1f}m  max={max(tte_vals):.1f}m")
    if quote_ages:
        print(f"  Quote age  median={_median(quote_ages):.0f}ms  max={max(quote_ages):.0f}ms")

    if tte_buckets:
        print("\n── Fill Rate by Time-to-Expiry Bucket ────────────────────────")
        for bucket in ["10m+", "7-10m", "4-7m", "2-4m", "<2m"]:
            d = tte_buckets.get(bucket)
            if d:
                print(f"  {bucket:>6s}  submitted={d['submitted']:>3d}  filled={d['filled']:>3d}"
                      f"  ({_pct(d['filled'], d['submitted'])})")

    if session_counts:
        print("\n── Funnel by Session ─────────────────────────────────────────")
        for sess in ["Asia", "London", "NY-Open", "NY-Prime", "After-Hrs"]:
            d = session_counts.get(sess)
            if d and d["approved"] > 0:
                print(f"  {sess:<12s}  approved={d['approved']:>3d}  submitted={d['submitted']:>3d}"
                      f"  filled={d['filled']:>3d}  fill%={_pct(d['filled'], d['submitted'])}")

    if regime_counts:
        print("\n── Fill Rate by Regime ───────────────────────────────────────")
        for reg, d in sorted(regime_counts.items()):
            print(f"  {reg:<16s}  submitted={d['submitted']:>3d}  filled={d['filled']:>3d}"
                  f"  ({_pct(d['filled'], d['submitted'])})")

    print("\n── P&L Summary ───────────────────────────────────────────────")
    print(f"  Actual P&L            : ${actual_pnl:+.2f}")
    print(f"  Hypothetical P&L      :  ${hypo_pnl:+.2f}  (if all submitted orders filled)")
    missed = hypo_pnl - actual_pnl
    print(f"  Edge left on table    : ${missed:+.2f}")

    print()
    print("=" * 64)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Bias Engine execution diagnostic")
    parser.add_argument("--days", type=float, default=1.0, help="Look-back window in days (default 1)")
    parser.add_argument("--all", action="store_true", help="Report all time")
    parser.add_argument("--db", default=TRADES_DB_PATH, help="Path to trades.db")
    args = parser.parse_args()

    if args.all:
        since_ts_ms = 0
    else:
        since_ts_ms = int(
            (datetime.now(timezone.utc).timestamp() - args.days * 86400) * 1000
        )

    run_report(args.db, since_ts_ms)


if __name__ == "__main__":
    main()
