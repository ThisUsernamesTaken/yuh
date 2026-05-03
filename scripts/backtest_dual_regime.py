"""Backtest harness for BB dual-regime strategy (2026-05-03).

Compares 3 configurations head-to-head against `data/trades.db`
window_snapshots + settlement_ledger:

    A. MEANREV_ONLY    — current production (BB_PURE near-strike only)
    B. TREND_ONLY      — only fires in trend zone (>0.15% from strike,
                          direction match, no strong reversal)
    C. DUAL_REGIME     — fires in either zone

For each ticker:
    - Walk window_snapshots in t_offset order
    - Determine regime per snapshot (need btc, strike, btc_5m_move)
    - At each snapshot, evaluate bb_pure if regime qualifies
    - Take FIRST qualifying signal per (ticker, config) — matches engine's
      MAX_TRADES_PER_SESSION_TICKER=1 rule
    - Look up settlement, compute net P&L

Caveats:
    - btc_5m_move proxy: derived from btc_price_cents at t_offset vs t-300s
    - Strike: parsed from ticker name suffix (rough — uses 2-min-prior btc
      price as approximation since strike isn't stored directly)
    - Assumes maker-bid+1 fill at the YES bid+1 (or 2c slippage on cap miss)

Usage:
    python scripts/backtest_dual_regime.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bb_pure import evaluate  # noqa: E402


def kalshi_fee_cents(price_cents: int, contracts: int) -> float:
    p = float(price_cents) / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return per_contract * contracts


def classify_regime(
    btc_price: float, strike: float, side: str, btc_5m_move: float,
    *, max_meanrev_pct: float = 0.0004,
    min_trend_pct: float = 0.0015,
    max_reversal_dollars: float = 50.0,
) -> str:
    if btc_price <= 0 or strike <= 0:
        return "BLOCK"
    dist_pct = abs(btc_price - strike) / btc_price
    if dist_pct <= max_meanrev_pct:
        return "MEAN_REVERSION"
    if dist_pct >= min_trend_pct:
        btc_above = btc_price > strike
        signal_dir_yes = (side == "yes")
        if btc_above != signal_dir_yes:
            return "BLOCK"
        if btc_above and btc_5m_move <= -max_reversal_dollars:
            return "BLOCK"
        if (not btc_above) and btc_5m_move >= max_reversal_dollars:
            return "BLOCK"
        return "TREND"
    return "BLOCK"


def load_settlements(con: sqlite3.Connection) -> dict[str, str]:
    cur = con.cursor()
    cur.execute("SELECT ticker, market_result FROM settlement_ledger")
    return {row[0]: row[1].lower() for row in cur.fetchall() if row[0] and row[1]}


def load_snapshots(con: sqlite3.Connection):
    """Yield (ticker, list of snapshot dicts in t_offset order)."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT ticker, t_offset_sec, ts_ms, btc_price_cents,
               kalshi_mid_cents, model_prob_yes
        FROM window_snapshots
        WHERE kalshi_mid_cents IS NOT NULL
          AND model_prob_yes IS NOT NULL
          AND btc_price_cents IS NOT NULL
        ORDER BY ticker, t_offset_sec
        """
    )
    current_ticker = None
    bucket: list[dict] = []
    for row in cur.fetchall():
        ticker, t_off, ts, btc_c, mid, prob_yes = row
        if ticker != current_ticker:
            if current_ticker is not None:
                yield current_ticker, bucket
            current_ticker = ticker
            bucket = []
        bucket.append({
            "t_offset_sec": int(t_off),
            "ts_ms": int(ts),
            "btc": float(btc_c) / 100.0,  # cents → dollars
            "mid": int(mid),
            "fair": int(round(float(prob_yes) * 100.0)),
        })
    if current_ticker is not None:
        yield current_ticker, bucket


def estimate_strike(snapshots: list[dict]) -> float:
    """Estimate strike as the BTC price at t_offset = 0 (window open)."""
    if not snapshots:
        return 0.0
    # Find earliest snapshot
    earliest = min(snapshots, key=lambda s: s["t_offset_sec"])
    return earliest["btc"]


def compute_btc_5m_move(snapshots: list[dict], idx: int) -> float:
    """BTC price delta over last 300 seconds vs current snapshot."""
    if idx < 0 or idx >= len(snapshots):
        return 0.0
    cur_t = snapshots[idx]["t_offset_sec"]
    cur_btc = snapshots[idx]["btc"]
    target_t = cur_t - 300
    if target_t < 0:
        return 0.0
    # Find snapshot at or just after target_t
    for j in range(idx, -1, -1):
        if snapshots[j]["t_offset_sec"] <= target_t:
            return cur_btc - snapshots[j]["btc"]
    return 0.0


def build_config(min_edge_pp: float, max_entry: int) -> dict:
    return {
        "min_edge_pp": min_edge_pp,
        "max_entry_cents": max_entry,
        "min_entry_cents": 5,
        "min_time_remaining_s": 60.0,
        "kelly_fraction": 0.25,
        "kelly_max_frac": 0.05,
        "max_contracts": 8,
        "fee_aware_edge_enabled": False,
        "fee_aware_edge_mult": 2.0,
        "fee_aware_edge_floor_pp": 4.0,
    }


def simulate_outcome(side: str, entry_cents: int, contracts: int,
                     market_result: str | None) -> tuple[float, str]:
    if market_result is None:
        return 0.0, "UNSETTLED"
    won = (side == "yes" and market_result == "yes") or \
          (side == "no" and market_result == "no")
    payoff = 100.0 * contracts if won else 0.0
    cost = float(entry_cents) * contracts
    fees = kalshi_fee_cents(entry_cents, contracts)
    net = payoff - cost - fees
    return net, "WIN" if won else "LOSS"


def run_config(snapshots_iter, settlements: dict[str, str],
               cfg: dict, balance: float,
               regime_filter: set[str]) -> list[dict]:
    """Run backtest. regime_filter = {'MEAN_REVERSION', 'TREND'} or subset."""
    fires = []
    for ticker, snapshots in snapshots_iter:
        if not snapshots:
            continue
        strike = estimate_strike(snapshots)
        if strike <= 0:
            continue
        result = settlements.get(ticker)
        first_sig = None
        for idx, snap in enumerate(snapshots):
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
            if sig is None:
                continue
            btc_5m_move = compute_btc_5m_move(snapshots, idx)
            regime = classify_regime(
                btc_price=snap["btc"], strike=strike, side=sig.side,
                btc_5m_move=btc_5m_move,
            )
            if regime not in regime_filter:
                continue
            # Apply regime-specific entry-cap and min
            if regime == "TREND":
                cap, regime_min = 75, 60
            else:
                cap, regime_min = 55, 5
            if sig.suggested_entry_cents > cap:
                continue
            if sig.suggested_entry_cents < regime_min:
                continue
            first_sig = (snap, sig, regime, btc_5m_move, strike)
            break
        if first_sig is None:
            continue
        snap, sig, regime, b5, strike = first_sig
        net, outcome = simulate_outcome(
            side=sig.side, entry_cents=sig.suggested_entry_cents,
            contracts=sig.contracts, market_result=result,
        )
        fires.append({
            "ticker": ticker, "t_offset": snap["t_offset_sec"],
            "side": sig.side, "regime": regime,
            "edge_pp": sig.edge_pp, "fair": sig.fair_yes_cents,
            "entry": sig.suggested_entry_cents, "contracts": sig.contracts,
            "outcome": outcome, "net_pnl_cents": net,
            "result": result,
            "btc": snap["btc"], "strike": strike, "btc_5m": b5,
        })
    return fires


def aggregate(fires: list[dict]) -> dict:
    if not fires:
        return {
            "fires": 0, "settled": 0, "wins": 0, "losses": 0,
            "hit_rate": 0.0, "total_pnl_cents": 0.0,
            "avg_pnl_cents": 0.0, "by_regime": {}, "by_entry_bucket": {},
        }
    settled = [f for f in fires if f["outcome"] != "UNSETTLED"]
    wins = sum(1 for f in settled if f["outcome"] == "WIN")
    losses = sum(1 for f in settled if f["outcome"] == "LOSS")
    total_pnl = sum(f["net_pnl_cents"] for f in settled)

    by_regime: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in settled:
        b = by_regime[f["regime"]]
        b["n"] += 1
        if f["outcome"] == "WIN":
            b["wins"] += 1
        b["pnl"] += f["net_pnl_cents"]

    by_entry_bucket: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in settled:
        bk = f"{(f['entry'] // 10) * 10:02d}-{(f['entry'] // 10) * 10 + 9:02d}c"
        bb = by_entry_bucket[bk]
        bb["n"] += 1
        if f["outcome"] == "WIN":
            bb["wins"] += 1
        bb["pnl"] += f["net_pnl_cents"]

    return {
        "fires": len(fires), "settled": len(settled),
        "wins": wins, "losses": losses,
        "hit_rate": wins / len(settled) if settled else 0.0,
        "total_pnl_cents": total_pnl,
        "avg_pnl_cents": total_pnl / len(settled) if settled else 0.0,
        "by_regime": dict(by_regime),
        "by_entry_bucket": dict(by_entry_bucket),
    }


def print_result(label: str, stats: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  fires        : {stats['fires']}")
    print(f"  settled      : {stats.get('settled', 0)}")
    print(f"  wins/losses  : {stats['wins']} / {stats['losses']}")
    print(f"  hit rate     : {stats['hit_rate']*100:.1f}%")
    print(f"  total P&L    : ${stats['total_pnl_cents']/100:.2f}")
    print(f"  avg P&L/trade: ${stats['avg_pnl_cents']/100:.2f}")
    if stats.get("by_regime"):
        print("  by regime:")
        for k in sorted(stats["by_regime"].keys()):
            v = stats["by_regime"][k]
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k:14s}: n={v['n']:3d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+7.2f}")
    if stats.get("by_entry_bucket"):
        print("  by entry bucket:")
        for k in sorted(stats["by_entry_bucket"].keys()):
            v = stats["by_entry_bucket"][k]
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k}: n={v['n']:3d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--balance", type=float, default=100.0)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    settlements = load_settlements(con)
    print(f"Loaded {len(settlements)} settlements from {args.db}")
    snap_count_cur = con.cursor()
    snap_count_cur.execute("SELECT COUNT(DISTINCT ticker) FROM window_snapshots")
    print(f"Tickers in window_snapshots: {snap_count_cur.fetchone()[0]}\n")

    cfg = build_config(min_edge_pp=8.0, max_entry=75)

    print("Running MEANREV_ONLY...")
    a = run_config(load_snapshots(con), settlements, cfg, args.balance,
                   {"MEAN_REVERSION"})
    print("Running TREND_ONLY...")
    b = run_config(load_snapshots(con), settlements, cfg, args.balance,
                   {"TREND"})
    print("Running DUAL_REGIME...")
    d = run_config(load_snapshots(con), settlements, cfg, args.balance,
                   {"MEAN_REVERSION", "TREND"})

    print_result("MEANREV_ONLY (current production)", aggregate(a))
    print_result("TREND_ONLY", aggregate(b))
    print_result("DUAL_REGIME (proposed)", aggregate(d))

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
