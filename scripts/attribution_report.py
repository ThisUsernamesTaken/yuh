#!/usr/bin/env python
"""Phase 1 attribution report — reads tagged trade data and cross-tabs P&L.

Resolves authoritative P&L through `settlement_ledger.pnl_cents`, joined to
the entry `kalshi_trades` row via ticker. Phase-1 tags (regime, drawdown,
raw_edge_pp, dominant_margin, shadow_edge) come out of
`kalshi_trades.ladder_detail` JSON.

Output is four plain-text cross-tabs:
  1. pnl_by_regime              — STRUCTURED / CHOP / CHAOTIC
  2. pnl_by_edge_bucket         — <2, 2-4, 4-6, 6-8, 8-10, >=10  (pp)
  3. pnl_by_entry_type          — strategy × regime × drawdown
  4. pnl_by_regime_counterfactual — same trades, damped qty (Phase 2 preview)

Usage:  python scripts/attribution_report.py [--days=7] [--db=data/trades.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from anywhere — fall back to project root on sys.path
ENGINE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_DIR))

from regime import Regime, DrawdownState, REGIME_FACTORS, DRAWDOWN_FACTORS  # noqa: E402


# ── Edge-pp bucketing ──────────────────────────────────────────────────────
EDGE_BUCKETS = [
    ("<2",    lambda e: e < 2),
    ("2-4",   lambda e: 2 <= e < 4),
    ("4-6",   lambda e: 4 <= e < 6),
    ("6-8",   lambda e: 6 <= e < 8),
    ("8-10",  lambda e: 8 <= e < 10),
    (">=10",  lambda e: e >= 10),
]


def edge_bucket(edge_pp: float) -> str:
    for name, pred in EDGE_BUCKETS:
        if pred(edge_pp):
            return name
    return "<2"


def fmt_money(cents: float) -> str:
    return f"${cents / 100.0:+.2f}"


def print_table(title: str, rows: list[dict], columns: list[tuple[str, str]]) -> None:
    print(f"\n-- {title} --")
    if not rows:
        print("  (no data)")
        return
    widths = [max(len(h), max(len(str(r.get(k, ""))) for r in rows)) for k, h in columns]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*[h for _, h in columns]))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[str(r.get(k, "")) for k, _ in columns]))


def fetch_tagged_trades(conn: sqlite3.Connection, cutoff_iso: str) -> list[dict]:
    """Return one dict per entry trade with tags + settled P&L attached.

    Joins entry trades to settlement P&L by ticker. Skips trades that haven't
    settled yet (can't attribute them). Uses settlement_ledger.pnl_cents as
    the authoritative P&L; kalshi_trades.pnl is stale per CLAUDE.md.
    """
    q = """
    SELECT  kt.order_id,
            kt.placed_at,
            kt.ticker,
            kt.side,
            kt.count,
            kt.filled_count,
            kt.limit_price,
            kt.strategy_name,
            kt.ladder_detail,
            sl.pnl_cents,
            sl.market_result,
            sl.settled_time
      FROM  kalshi_trades kt
      JOIN  settlement_ledger sl ON sl.ticker = kt.ticker
     WHERE  kt.placed_at >= ?
       AND  sl.pnl_cents IS NOT NULL
       AND  (kt.filled_count IS NOT NULL AND kt.filled_count > 0)
    """
    cur = conn.execute(q, (cutoff_iso,))
    cols = [c[0] for c in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        detail = {}
        try:
            detail = json.loads(d.get("ladder_detail") or "{}")
        except Exception:
            detail = {}
        d["regime"]         = detail.get("regime") or "UNKNOWN"
        d["drawdown_state"] = detail.get("drawdown_state") or "UNKNOWN"
        d["raw_edge_pp"]    = float(detail.get("raw_edge_pp") or 0.0)
        d["dominant_margin"] = float(detail.get("dominant_margin") or 0.0)
        shadow = detail.get("shadow_edge") or {}
        d["shadow_score"] = float(shadow.get("score") or 0.0) if shadow else 0.0
        rows.append(d)
    return rows


def group_pnl(trades: list[dict], key_fn) -> list[dict]:
    """Bucket trades and report count / wr_pct / gross / net / avg_size."""
    buckets: dict = defaultdict(lambda: {"n": 0, "wins": 0, "net_c": 0.0, "qty_sum": 0})
    for t in trades:
        k = key_fn(t)
        b = buckets[k]
        b["n"] += 1
        pnl = float(t.get("pnl_cents") or 0.0)
        b["net_c"] += pnl
        if pnl > 0:
            b["wins"] += 1
        b["qty_sum"] += int(t.get("filled_count") or 0)
    rows = []
    for k, b in sorted(buckets.items(), key=lambda x: str(x[0])):
        n = b["n"]
        wr = (b["wins"] / n * 100.0) if n else 0.0
        avg_size = (b["qty_sum"] / n) if n else 0.0
        rows.append({
            "key":      k,
            "n":        n,
            "wr_pct":   f"{wr:.1f}",
            "net_pnl":  fmt_money(b["net_c"]),
            "avg_size": f"{avg_size:.1f}",
        })
    return rows


def counterfactual_damper(trades: list[dict]) -> list[dict]:
    """Replay trades with qty × regime_factor × dd_factor. Assumes payoff
    scales linearly with qty: damped_pnl_c = pnl_c × (damped_qty / qty).
    Qty <= 0 or unknown regime/dd → factor 1.0 (no damping).
    """
    buckets: dict = defaultdict(lambda: {"n": 0, "net_c": 0.0, "skip": 0})
    for t in trades:
        regime = t.get("regime")
        dd = t.get("drawdown_state")
        try:
            rf = REGIME_FACTORS[Regime(regime)]
        except (ValueError, KeyError):
            rf = 1.0
        try:
            df = DRAWDOWN_FACTORS[DrawdownState(dd)]
        except (ValueError, KeyError):
            df = 1.0
        qty = int(t.get("filled_count") or 0)
        if qty <= 0:
            continue
        damped_qty = int(round(qty * rf * df))
        b = buckets[regime or "UNKNOWN"]
        b["n"] += 1
        if damped_qty <= 0:
            b["skip"] += 1
            continue
        scale = damped_qty / qty
        b["net_c"] += float(t.get("pnl_cents") or 0.0) * scale
    rows = []
    for k, b in sorted(buckets.items()):
        rows.append({
            "key":     k,
            "n":       b["n"],
            "skipped": b["skip"],
            "net_pnl": fmt_money(b["net_c"]),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=str(ENGINE_DIR / "data" / "trades.db"))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERROR] DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_iso = cutoff.isoformat()

    conn = sqlite3.connect(args.db)
    conn.row_factory = None

    trades = fetch_tagged_trades(conn, cutoff_iso)
    print(f"\n== Attribution report: last {args.days} days ==")
    print(f"   cutoff: {cutoff_iso}")
    print(f"   db:     {args.db}")
    print(f"   trades: {len(trades)} settled")

    if not trades:
        print("\nNo settled tagged trades in window. Run the engine for a few hours.")
        return

    gross_pnl_c = sum(float(t.get("pnl_cents") or 0.0) for t in trades)
    print(f"   gross:  {fmt_money(gross_pnl_c)}")

    print_table(
        "pnl_by_regime",
        group_pnl(trades, lambda t: t.get("regime") or "UNKNOWN"),
        [("key", "regime"), ("n", "trades"), ("wr_pct", "wr%"),
         ("net_pnl", "net_pnl"), ("avg_size", "avg_ct")],
    )

    print_table(
        "pnl_by_drawdown",
        group_pnl(trades, lambda t: t.get("drawdown_state") or "UNKNOWN"),
        [("key", "drawdown"), ("n", "trades"), ("wr_pct", "wr%"),
         ("net_pnl", "net_pnl"), ("avg_size", "avg_ct")],
    )

    print_table(
        "pnl_by_edge_bucket",
        group_pnl(trades, lambda t: edge_bucket(t.get("raw_edge_pp") or 0.0)),
        [("key", "edge_pp"), ("n", "trades"), ("wr_pct", "wr%"),
         ("net_pnl", "net_pnl"), ("avg_size", "avg_ct")],
    )

    print_table(
        "pnl_by_entry_type",
        group_pnl(
            trades,
            lambda t: f"{t.get('strategy_name') or 'UNK'}/"
                      f"{t.get('regime') or '?'}/"
                      f"{t.get('drawdown_state') or '?'}",
        ),
        [("key", "strat/regime/dd"), ("n", "trades"), ("wr_pct", "wr%"),
         ("net_pnl", "net_pnl"), ("avg_size", "avg_ct")],
    )

    print_table(
        "pnl_by_regime_counterfactual (Phase 2 damper replay)",
        counterfactual_damper(trades),
        [("key", "regime"), ("n", "trades"), ("skipped", "skipped"),
         ("net_pnl", "damped_net_pnl")],
    )

    print()


if __name__ == "__main__":
    main()
