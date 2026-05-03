"""Backtest harness for fee-aware dynamic edge threshold (2026-05-03).

Compares 3 configurations head-to-head against `data/trades.db`
window_snapshots + settlement_ledger:

    A. STATIC      — current production: BB_PURE_MIN_EDGE_PP=8.0 floor
    B. FEE_AWARE_K2 — proposed: K=2 × breakeven, floor=4pp
    C. FEE_AWARE_K3 — conservative: K=3 × breakeven, floor=4pp

For each ticker:
    - Walk window_snapshots in t_offset order
    - At each snapshot, evaluate bb_pure with each config
    - Take FIRST signal per (ticker, config) — matches engine's
      MAX_TRADES_PER_SESSION_TICKER=1 rule
    - Look up settlement from settlement_ledger
    - Compute net P&L = payoff − entry_cost − round_trip_fees

Usage:
    python scripts/backtest_fee_aware_edge.py
    python scripts/backtest_fee_aware_edge.py --min-time 60 --balance 100
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Make repo root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bb_pure import evaluate  # noqa: E402


def kalshi_fee_cents(price_cents: int, contracts: int) -> float:
    """Kalshi per-contract fee formula: 0.07 × P × (1−P) × 100¢ × N."""
    p = float(price_cents) / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return per_contract * contracts


def load_settlements(con: sqlite3.Connection) -> dict[str, str]:
    """Map ticker → 'yes' | 'no' from settlement_ledger."""
    cur = con.cursor()
    cur.execute("SELECT ticker, market_result FROM settlement_ledger")
    return {row[0]: row[1].lower() for row in cur.fetchall() if row[0] and row[1]}


def load_snapshots(con: sqlite3.Connection):
    """Yield (ticker, list_of_snapshot_dicts_in_t_offset_order)."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT ticker, t_offset_sec, kalshi_mid_cents, kalshi_ask_cents,
               kalshi_bid_cents, model_prob_yes
        FROM window_snapshots
        WHERE kalshi_mid_cents IS NOT NULL
          AND model_prob_yes IS NOT NULL
        ORDER BY ticker, t_offset_sec
        """
    )
    current_ticker = None
    bucket: list[dict] = []
    for row in cur.fetchall():
        ticker, t_off, mid, ask, bid, prob_yes = row
        if ticker != current_ticker:
            if current_ticker is not None:
                yield current_ticker, bucket
            current_ticker = ticker
            bucket = []
        bucket.append({
            "t_offset_sec": int(t_off),
            "mid": int(mid),
            "ask": int(ask) if ask is not None else int(mid),
            "bid": int(bid) if bid is not None else int(mid),
            "fair": int(round(float(prob_yes) * 100.0)),
        })
    if current_ticker is not None:
        yield current_ticker, bucket


def build_config(min_edge_pp: float,
                 fee_aware: bool,
                 fee_mult: float,
                 fee_floor: float,
                 max_entry: int = 55,
                 min_entry: int = 5,
                 min_time: float = 60.0) -> dict:
    return {
        "min_edge_pp": min_edge_pp,
        "max_entry_cents": max_entry,
        "min_entry_cents": min_entry,
        "min_time_remaining_s": min_time,
        "kelly_fraction": 0.25,
        "kelly_max_frac": 0.05,
        "max_contracts": 8,
        "fee_aware_edge_enabled": fee_aware,
        "fee_aware_edge_mult": fee_mult,
        "fee_aware_edge_floor_pp": fee_floor,
    }


def simulate_outcome(side: str, entry_cents: int, contracts: int,
                     market_result: str | None) -> tuple[float, str]:
    """Return (net_pnl_cents, outcome) where outcome ∈ {WIN, LOSS, UNSETTLED}."""
    if market_result is None:
        return 0.0, "UNSETTLED"
    won = (side == "yes" and market_result == "yes") or \
          (side == "no" and market_result == "no")
    payoff = 100.0 * contracts if won else 0.0
    cost = float(entry_cents) * contracts
    # Round-trip fees: entry side at entry_cents,
    # exit at 100 if won, 0 if lost — which has fee=0.
    # For losers, we don't pay exit fee (settles to 0).
    # For winners, settlement to 100 also has no fee on exit.
    # Actual Kalshi: settlement is fee-free, only fills incur fee.
    fees = kalshi_fee_cents(entry_cents, contracts)
    net = payoff - cost - fees
    return net, "WIN" if won else "LOSS"


def run_config(snapshots_iter, settlements: dict[str, str],
               cfg: dict, balance: float) -> dict:
    """Run one config across all tickers, returning aggregate stats."""
    fires = []
    for ticker, snapshots in snapshots_iter:
        result = settlements.get(ticker)
        first_sig = None
        for snap in snapshots:
            secs_to_exp = max(0.0, 900.0 - snap["t_offset_sec"])
            if secs_to_exp <= 0:
                break
            sig = evaluate(
                market_mid_cents=snap["mid"],
                fair_yes_cents=snap["fair"],
                seconds_to_expiry=secs_to_exp,
                balance_dollars=balance,
                config=cfg,
            )
            if sig is not None:
                first_sig = (snap, sig)
                break
        if first_sig is None:
            continue
        snap, sig = first_sig
        net_pnl, outcome = simulate_outcome(
            side=sig.side,
            entry_cents=sig.suggested_entry_cents,
            contracts=sig.contracts,
            market_result=result,
        )
        fires.append({
            "ticker": ticker,
            "t_offset": snap["t_offset_sec"],
            "side": sig.side,
            "edge_pp": sig.edge_pp,
            "fair": sig.fair_yes_cents,
            "entry": sig.suggested_entry_cents,
            "contracts": sig.contracts,
            "outcome": outcome,
            "net_pnl_cents": net_pnl,
            "result": result,
        })
    return _aggregate(fires)


def _aggregate(fires: list[dict]) -> dict:
    if not fires:
        return {
            "fires": 0, "wins": 0, "losses": 0, "unsettled": 0,
            "hit_rate": 0.0, "total_pnl_cents": 0.0, "avg_pnl_cents": 0.0,
            "by_entry_bucket": {}, "by_side": {},
        }
    settled = [f for f in fires if f["outcome"] != "UNSETTLED"]
    wins = sum(1 for f in settled if f["outcome"] == "WIN")
    losses = sum(1 for f in settled if f["outcome"] == "LOSS")
    total_pnl = sum(f["net_pnl_cents"] for f in settled)

    # Bucket by entry price decile
    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in settled:
        b = f["entry"] // 10 * 10
        key = f"{b:02d}-{b+9:02d}c"
        buckets[key]["n"] += 1
        if f["outcome"] == "WIN":
            buckets[key]["wins"] += 1
        buckets[key]["pnl"] += f["net_pnl_cents"]

    by_side: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in settled:
        by_side[f["side"]]["n"] += 1
        if f["outcome"] == "WIN":
            by_side[f["side"]]["wins"] += 1
        by_side[f["side"]]["pnl"] += f["net_pnl_cents"]

    return {
        "fires": len(fires),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "unsettled": len(fires) - len(settled),
        "hit_rate": wins / len(settled) if settled else 0.0,
        "total_pnl_cents": total_pnl,
        "avg_pnl_cents": total_pnl / len(settled) if settled else 0.0,
        "by_entry_bucket": dict(buckets),
        "by_side": dict(by_side),
    }


def print_result(label: str, stats: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  fires        : {stats['fires']}")
    print(f"  settled      : {stats.get('settled', 0)}")
    print(f"  wins/losses  : {stats['wins']} / {stats['losses']}")
    print(f"  unsettled    : {stats.get('unsettled', 0)}")
    print(f"  hit rate     : {stats['hit_rate']*100:.1f}%")
    print(f"  total P&L    : ${stats['total_pnl_cents']/100:.2f}")
    print(f"  avg P&L/trade: ${stats['avg_pnl_cents']/100:.2f}")
    if stats.get("by_entry_bucket"):
        print("  by entry bucket:")
        for k in sorted(stats["by_entry_bucket"].keys()):
            v = stats["by_entry_bucket"][k]
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k}: n={v['n']:3d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+7.2f}")
    if stats.get("by_side"):
        print("  by side:")
        for k, v in sorted(stats["by_side"].items()):
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k:>3}: n={v['n']:3d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--balance", type=float, default=100.0)
    ap.add_argument("--max-entry", type=int, default=55)
    ap.add_argument("--min-entry", type=int, default=5)
    ap.add_argument("--min-time", type=float, default=60.0)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    settlements = load_settlements(con)
    print(f"Loaded {len(settlements)} settlements from {args.db}")
    snap_count_cur = con.cursor()
    snap_count_cur.execute("SELECT COUNT(DISTINCT ticker) FROM window_snapshots")
    n_tickers = snap_count_cur.fetchone()[0]
    print(f"Tickers in window_snapshots: {n_tickers}")
    coverage = sum(1 for t, _ in load_snapshots(con) if t in settlements)
    print(f"Tickers with both snapshot + settlement: {coverage}\n")

    configs = [
        ("STATIC 8pp (current prod)",
         build_config(8.0, False, 2.0, 4.0, args.max_entry, args.min_entry, args.min_time)),
        ("FEE_AWARE K=2 floor=4 (proposed)",
         build_config(8.0, True, 2.0, 4.0, args.max_entry, args.min_entry, args.min_time)),
        ("FEE_AWARE K=3 floor=4 (conservative)",
         build_config(8.0, True, 3.0, 4.0, args.max_entry, args.min_entry, args.min_time)),
        ("FEE_AWARE K=2 floor=3 (aggressive)",
         build_config(8.0, True, 2.0, 3.0, args.max_entry, args.min_entry, args.min_time)),
    ]

    for label, cfg in configs:
        # Re-iterate snapshots fresh per config (generator)
        stats = run_config(load_snapshots(con), settlements, cfg, args.balance)
        print_result(label, stats)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
