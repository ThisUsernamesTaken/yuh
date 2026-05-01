"""ATM Reversion parameter sweep.

Per Codex direction 2026-04-25: sweep entry/exit param grid, rank by
fee-adjusted avg and max drawdown, report discount-only separately from
discount+bias.

The first run of atm_reversion_backtest.py at defaults showed 1098 trades,
64.3% WR, +4.27c avg with strike_escape (-15c × 305) and settled (-22c × 108)
as the main drags. Sweep targets: shrink those drags without choking the
clean profit_5/target_49 winners.

Sweep axes (per Codex):
  max_strike_dist_pct: 0.01, 0.015, 0.02, 0.025, 0.03
  stop_strike_dist_pct: 0.035, 0.045, 0.06, 0.08
  discount_min_edge_c: 6, 8, 10, 12
  discount_max_entry_c: 30, 35, 40, 45
  target_c: 45, 47, 49, 52
  profit_c: 3, 4, 5, 8

Full grid = 5*4*4*4*4*4 = 5120 combos. Each ~1s after data preload.
Initial pass: 3D sweep on (max_strike_dist, stop_strike_dist, profit_c)
keeping others at default. Then 2D refinement on the top region.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import types
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atm_reversion_backtest as bt


def _make_args(overrides: dict, bias_enabled: bool = False) -> types.SimpleNamespace:
    """Build an args namespace matching the backtest's argparse defaults."""
    defaults = {
        "max_strike_dist_pct": 0.02,
        "stop_strike_dist_pct": 0.06,
        "max_entry_c": 40,
        "min_fair_c": 47.0,
        "min_edge_c": 8.0,
        "bias_enabled": bias_enabled,
        "max_bias_entry_c": 62,
        "min_bias_edge_c": 1.0,
        "target_c": 49,
        "profit_target_c": 5,
        "stretch_profit_target_c": 8,
        "force_exit_age_s": 840,
        "use_mid_fair": False,
        "min_volume": 10.0,
        "limit_tickers": 0,
        "side_filter": "both",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _print_row(label: str, summary: dict) -> None:
    print(f"  {label:<55} n={summary['n']:>4}  WR={summary['wr']:>5.1f}%  "
          f"avg={summary['avg_c']:+6.2f}c  net={summary['avg_net_c']:+6.2f}c  "
          f"total={summary['total_c']:+6}c  maxDD={summary['max_dd_c']:>5}c")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["1d", "3d", "full"], default="3d")
    ap.add_argument("--bias", action="store_true", help="Run with bias enabled")
    ap.add_argument("--top-n", type=int, default=15)
    args = ap.parse_args()

    print("Loading backtest data once...", end=" ", flush=True)
    conn = sqlite3.connect(str(bt.DB))
    markets = bt._load_markets(conn)
    btc_map = bt._load_btc_at(conn)
    candles = bt._load_candles_per_ticker(conn)
    print(f"{len(markets)} markets, {len(btc_map)} BTC bars, "
          f"{sum(len(v) for v in candles.values())} candles")

    bias_label = "WITH-BIAS" if args.bias else "DISCOUNT-ONLY"
    print(f"\n=== ATM REVERSION PARAM SWEEP ({bias_label}) ===\n")

    if args.mode == "1d":
        # 1D sweep: vary one axis at a time
        axes = {
            "max_strike_dist_pct": [0.01, 0.015, 0.02, 0.025, 0.03],
            "stop_strike_dist_pct": [0.035, 0.045, 0.06, 0.08],
            "min_edge_c": [6, 8, 10, 12],
            "max_entry_c": [30, 35, 40, 45],
            "target_c": [45, 47, 49, 52],
            "profit_target_c": [3, 4, 5, 8],
        }
        for axis_name, values in axes.items():
            print(f"\n-- Sweep {axis_name} --")
            for v in values:
                args_obj = _make_args({axis_name: v}, bias_enabled=args.bias)
                result = bt.run_backtest(args_obj, markets, btc_map, candles)
                summary = bt.summarize(result["trades"])
                _print_row(f"{axis_name}={v}", summary)
        return 0

    if args.mode == "3d":
        # Focused 3D sweep on most-impactful axes
        results = []
        msd_vals = [0.01, 0.015, 0.02, 0.025, 0.03]
        ssd_vals = [0.035, 0.045, 0.06, 0.08]
        prof_vals = [3, 4, 5, 8]
        total = len(msd_vals) * len(ssd_vals) * len(prof_vals)
        print(f"\n-- 3D Sweep (max_strike_dist x stop_strike_dist x profit_c) "
              f"= {total} combos --")
        for i, (msd, ssd, prof) in enumerate(product(msd_vals, ssd_vals, prof_vals)):
            args_obj = _make_args({
                "max_strike_dist_pct": msd,
                "stop_strike_dist_pct": ssd,
                "profit_target_c": prof,
            }, bias_enabled=args.bias)
            result = bt.run_backtest(args_obj, markets, btc_map, candles)
            summary = bt.summarize(result["trades"])
            results.append({
                "max_strike_dist_pct": msd,
                "stop_strike_dist_pct": ssd,
                "profit_target_c": prof,
                **summary,
            })
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{total}]", flush=True)

        # Rank by fee-adjusted avg, then by total / max_dd ratio
        ranked_by_net = sorted(results, key=lambda r: -r["avg_net_c"])
        print(f"\n-- TOP {args.top_n} by avg_net_c --")
        for r in ranked_by_net[:args.top_n]:
            label = (f"msd={r['max_strike_dist_pct']:.3f}% "
                     f"ssd={r['stop_strike_dist_pct']:.3f}% "
                     f"prof={r['profit_target_c']}c")
            _print_row(label, r)

        ranked_by_total = sorted(results, key=lambda r: -r["total_c"])
        print(f"\n-- TOP {args.top_n} by total_c --")
        for r in ranked_by_total[:args.top_n]:
            label = (f"msd={r['max_strike_dist_pct']:.3f}% "
                     f"ssd={r['stop_strike_dist_pct']:.3f}% "
                     f"prof={r['profit_target_c']}c")
            _print_row(label, r)

        # Also rank by net per trade × n (proxy for risk-adjusted total)
        for r in results:
            r["net_total"] = r["avg_net_c"] * r["n"]
        ranked_by_net_total = sorted(results, key=lambda r: -r["net_total"])
        print(f"\n-- TOP {args.top_n} by net_total (avg_net_c × n) --")
        for r in ranked_by_net_total[:args.top_n]:
            label = (f"msd={r['max_strike_dist_pct']:.3f}% "
                     f"ssd={r['stop_strike_dist_pct']:.3f}% "
                     f"prof={r['profit_target_c']}c")
            _print_row(label, r)
        return 0

    # full mode — careful, ~5000 combos
    if args.mode == "full":
        results = []
        msd_vals = [0.01, 0.015, 0.02, 0.025, 0.03]
        ssd_vals = [0.035, 0.045, 0.06, 0.08]
        edge_vals = [6, 8, 10, 12]
        ent_vals = [30, 35, 40, 45]
        tgt_vals = [45, 47, 49, 52]
        prof_vals = [3, 4, 5, 8]
        total = (len(msd_vals) * len(ssd_vals) * len(edge_vals)
                 * len(ent_vals) * len(tgt_vals) * len(prof_vals))
        print(f"\n-- FULL Sweep = {total} combos. This will take ~{total*0.6:.0f}s --")
        for i, (msd, ssd, edge, ent, tgt, prof) in enumerate(
                product(msd_vals, ssd_vals, edge_vals, ent_vals, tgt_vals, prof_vals)):
            args_obj = _make_args({
                "max_strike_dist_pct": msd,
                "stop_strike_dist_pct": ssd,
                "min_edge_c": edge,
                "max_entry_c": ent,
                "target_c": tgt,
                "profit_target_c": prof,
            }, bias_enabled=args.bias)
            result = bt.run_backtest(args_obj, markets, btc_map, candles)
            summary = bt.summarize(result["trades"])
            if summary["n"] < 50:  # filter tiny samples
                continue
            results.append({
                "msd": msd, "ssd": ssd, "edge": edge, "ent": ent,
                "tgt": tgt, "prof": prof, **summary,
            })
            if (i + 1) % 200 == 0:
                print(f"  [{i+1}/{total}] kept {len(results)}", flush=True)

        ranked = sorted(results, key=lambda r: -r["avg_net_c"])
        print(f"\n-- FULL TOP {args.top_n} by avg_net_c (n>=50) --")
        for r in ranked[:args.top_n]:
            label = (f"msd={r['msd']:.3f}% ssd={r['ssd']:.3f}% edge={r['edge']}c "
                     f"ent={r['ent']}c tgt={r['tgt']}c prof={r['prof']}c")
            _print_row(label, r)

        ranked = sorted(results, key=lambda r: -(r["avg_net_c"] * r["n"]))
        print(f"\n-- FULL TOP {args.top_n} by avg_net_c × n --")
        for r in ranked[:args.top_n]:
            label = (f"msd={r['msd']:.3f}% ssd={r['ssd']:.3f}% edge={r['edge']}c "
                     f"ent={r['ent']}c tgt={r['tgt']}c prof={r['prof']}c")
            _print_row(label, r)
        return 0


if __name__ == "__main__":
    sys.exit(main())
