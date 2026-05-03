"""Backtest for bb_momentum (2026-05-03).

Simulates the momentum-rider strategy against `data/trades.db`
window_snapshots:

  - For each ticker, walk snapshots in t_offset order.
  - At each snapshot, compute btc_move_300s and btc_move_30s.
  - Call bb_momentum.evaluate_entry. If signal fires:
      - Record entry (side, entry price, contracts, entry velocity)
      - Walk forward checking evaluate_stall each tick
      - On stall/reversal: exit at current opposite-side bid
      - Apply cooldown N seconds, then watch for next entry
      - Cap at MAX_CYCLES_PER_WINDOW
  - Track per-window cycle P&L and settle remaining position at window
    close (using settlement_ledger).

Outputs aggregate stats: fires, hit rate, total P&L, by entry-bucket,
by cycle-number-within-window.

Usage:
    python scripts/backtest_momentum.py
    python scripts/backtest_momentum.py --max-cycles 3 --cooldown 30
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bb_momentum import evaluate_entry, evaluate_stall  # noqa: E402


def kalshi_fee_cents(price_cents: int, contracts: int) -> float:
    p = float(price_cents) / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return per_contract * contracts


def load_settlements(con: sqlite3.Connection) -> dict[str, str]:
    cur = con.cursor()
    cur.execute("SELECT ticker, market_result FROM settlement_ledger")
    return {row[0]: row[1].lower() for row in cur.fetchall() if row[0] and row[1]}


def load_snapshots(con: sqlite3.Connection):
    """Yield (ticker, list of snapshot dicts in t_offset order)."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT ticker, t_offset_sec, btc_price_cents, kalshi_mid_cents
        FROM window_snapshots
        WHERE kalshi_mid_cents IS NOT NULL AND btc_price_cents IS NOT NULL
        ORDER BY ticker, t_offset_sec
        """
    )
    current_ticker = None
    bucket: list[dict] = []
    for ticker, t_off, btc_c, mid in cur.fetchall():
        if ticker != current_ticker:
            if current_ticker is not None:
                yield current_ticker, bucket
            current_ticker = ticker
            bucket = []
        bucket.append({
            "t_offset_sec": int(t_off),
            "btc": float(btc_c) / 100.0,
            "yes_mid": int(mid),
        })
    if current_ticker is not None:
        yield current_ticker, bucket


def btc_move_at(snapshots: list[dict], idx: int, lookback_s: int) -> float:
    """Compute BTC dollar move over lookback_s seconds ending at snapshots[idx]."""
    if idx < 0 or idx >= len(snapshots):
        return 0.0
    cur = snapshots[idx]
    target_t = cur["t_offset_sec"] - lookback_s
    if target_t < 0:
        return 0.0
    for j in range(idx, -1, -1):
        if snapshots[j]["t_offset_sec"] <= target_t:
            return cur["btc"] - snapshots[j]["btc"]
    return 0.0


def simulate_ticker(
    ticker: str, snapshots: list[dict],
    settlement: str | None,
    entry_cfg: dict, stall_cfg: dict,
    *,
    cooldown_s: int = 30,
    max_cycles: int = 3,
    balance: float = 100.0,
) -> list[dict]:
    """Simulate momentum-cycle trading on a single ticker.
    Returns list of cycle records (one dict per fire/exit pair).
    """
    cycles = []
    next_eligible_t = 0
    cycle_count = 0
    in_position = None  # dict with entry info or None

    for idx, snap in enumerate(snapshots):
        t = snap["t_offset_sec"]
        secs_to_exp = max(0.0, 900.0 - t)
        if secs_to_exp <= 0:
            break

        if in_position is None:
            # Cooldown gate
            if t < next_eligible_t:
                continue
            if cycle_count >= max_cycles:
                break

            mv_300 = btc_move_at(snapshots, idx, 300)
            mv_30 = btc_move_at(snapshots, idx, 30)

            sig = evaluate_entry(
                btc_move_30s=mv_30, btc_move_300s=mv_300,
                yes_mid_cents=snap["yes_mid"],
                seconds_to_expiry=secs_to_exp,
                balance_dollars=balance,
                config=entry_cfg,
            )
            if sig is None:
                continue

            in_position = {
                "side": sig.side,
                "entry_t": t,
                "entry_cents": sig.suggested_entry_cents,
                "contracts": sig.contracts,
                "entry_velocity_30s": sig.velocity_30s,
                "cycle_idx": cycle_count,
            }
            cycle_count += 1
            continue

        # In position — check for stall
        mv_30_now = btc_move_at(snapshots, idx, 30)
        verdict = evaluate_stall(
            entry_velocity_30s=in_position["entry_velocity_30s"],
            current_velocity_30s=mv_30_now,
            config=stall_cfg,
        )
        if not verdict.should_exit:
            continue

        # Exit at current opposite-side bid (approximated as our-side mid)
        # For YES position: exit at our_yes_bid; we use our-side mid as proxy.
        if in_position["side"] == "yes":
            exit_cents = snap["yes_mid"]
        else:
            exit_cents = 100 - snap["yes_mid"]

        # P&L on this cycle (mid-trade exit, NOT settlement)
        gross_pnl_cents = (exit_cents - in_position["entry_cents"]) * \
            in_position["contracts"]
        fees = (
            kalshi_fee_cents(in_position["entry_cents"], in_position["contracts"])
            + kalshi_fee_cents(exit_cents, in_position["contracts"])
        )
        net_pnl = gross_pnl_cents - fees

        cycles.append({
            "ticker": ticker,
            "cycle_idx": in_position["cycle_idx"],
            "side": in_position["side"],
            "entry_t": in_position["entry_t"],
            "exit_t": t,
            "entry_cents": in_position["entry_cents"],
            "exit_cents": exit_cents,
            "contracts": in_position["contracts"],
            "stall_reason": verdict.reason,
            "net_pnl_cents": net_pnl,
            "settled_held": False,
            "outcome": "WIN" if net_pnl > 0 else "LOSS",
        })

        next_eligible_t = t + cooldown_s
        in_position = None

    # If still in position at window end, settle to 100/0 based on settlement
    if in_position is not None and settlement is not None:
        won = (in_position["side"] == "yes" and settlement == "yes") or \
              (in_position["side"] == "no" and settlement == "no")
        payoff_per_contract = 100.0 if won else 0.0
        gross = (payoff_per_contract - in_position["entry_cents"]) \
            * in_position["contracts"]
        fees = kalshi_fee_cents(in_position["entry_cents"],
                                 in_position["contracts"])
        net_pnl = gross - fees
        cycles.append({
            "ticker": ticker,
            "cycle_idx": in_position["cycle_idx"],
            "side": in_position["side"],
            "entry_t": in_position["entry_t"],
            "exit_t": 900,
            "entry_cents": in_position["entry_cents"],
            "exit_cents": int(payoff_per_contract),
            "contracts": in_position["contracts"],
            "stall_reason": "WINDOW_END",
            "net_pnl_cents": net_pnl,
            "settled_held": True,
            "outcome": "WIN" if won else "LOSS",
        })

    return cycles


def aggregate(cycles: list[dict]) -> dict:
    if not cycles:
        return {
            "cycles": 0, "wins": 0, "losses": 0,
            "hit_rate": 0.0, "total_pnl_cents": 0.0,
            "avg_pnl_cents": 0.0, "by_entry_bucket": {},
            "by_cycle_idx": {}, "by_stall_reason": {},
        }
    wins = sum(1 for f in cycles if f["outcome"] == "WIN")
    losses = sum(1 for f in cycles if f["outcome"] == "LOSS")
    total_pnl = sum(f["net_pnl_cents"] for f in cycles)

    by_bucket: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in cycles:
        b = (f["entry_cents"] // 10) * 10
        key = f"{b:02d}-{b+9:02d}c"
        bb = by_bucket[key]
        bb["n"] += 1
        if f["outcome"] == "WIN":
            bb["wins"] += 1
        bb["pnl"] += f["net_pnl_cents"]

    by_cycle_idx: dict[int, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in cycles:
        cb = by_cycle_idx[f["cycle_idx"]]
        cb["n"] += 1
        if f["outcome"] == "WIN":
            cb["wins"] += 1
        cb["pnl"] += f["net_pnl_cents"]

    by_stall: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for f in cycles:
        # Strip detail off stall_reason
        key = f["stall_reason"].split()[0] if f["stall_reason"] else "NONE"
        sb = by_stall[key]
        sb["n"] += 1
        if f["outcome"] == "WIN":
            sb["wins"] += 1
        sb["pnl"] += f["net_pnl_cents"]

    return {
        "cycles": len(cycles), "wins": wins, "losses": losses,
        "hit_rate": wins / len(cycles) if cycles else 0.0,
        "total_pnl_cents": total_pnl,
        "avg_pnl_cents": total_pnl / len(cycles) if cycles else 0.0,
        "by_entry_bucket": dict(by_bucket),
        "by_cycle_idx": dict(by_cycle_idx),
        "by_stall_reason": dict(by_stall),
    }


def print_result(label: str, stats: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  cycles       : {stats['cycles']}")
    print(f"  wins/losses  : {stats['wins']} / {stats['losses']}")
    print(f"  hit rate     : {stats['hit_rate']*100:.1f}%")
    print(f"  total P&L    : ${stats['total_pnl_cents']/100:.2f}")
    print(f"  avg P&L/cycle: ${stats['avg_pnl_cents']/100:.2f}")
    if stats.get("by_entry_bucket"):
        print("  by entry bucket:")
        for k in sorted(stats["by_entry_bucket"].keys()):
            v = stats["by_entry_bucket"][k]
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k}: n={v['n']:4d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+8.2f}")
    if stats.get("by_cycle_idx"):
        print("  by cycle position within window:")
        for k in sorted(stats["by_cycle_idx"].keys()):
            v = stats["by_cycle_idx"][k]
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    cycle#{k}: n={v['n']:4d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+8.2f}")
    if stats.get("by_stall_reason"):
        print("  by exit reason:")
        for k, v in sorted(stats["by_stall_reason"].items()):
            hit = v["wins"] / v["n"] if v["n"] > 0 else 0
            print(f"    {k:12s}: n={v['n']:4d} hit={hit*100:5.1f}% pnl=${v['pnl']/100:+8.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--balance", type=float, default=100.0)
    ap.add_argument("--cooldown", type=int, default=30)
    ap.add_argument("--max-cycles", type=int, default=3)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    settlements = load_settlements(con)
    print(f"Loaded {len(settlements)} settlements\n")

    # Try a few configurations head-to-head
    base_entry = {
        "min_btc_move_300s_dollars": 30.0,
        "min_btc_move_30s_dollars": 10.0,
        "require_same_direction": True,
        "max_entry_cents": 50,
        "min_entry_cents": 5,
        "min_time_remaining_s": 90.0,
        "kelly_fraction": 0.20,
        "kelly_max_frac": 0.05,
        "max_contracts": 8,
    }
    base_stall = {"stall_decay_ratio": 0.5, "stall_min_entry_velocity": 5.0}

    configs = [
        ("DEFAULT (300s>$30, 30s>$10, decay=0.5)",
         dict(base_entry), dict(base_stall)),
        ("HOLD TO SETTLEMENT (decay=0 disables stall)",
         dict(base_entry),
         {**base_stall, "stall_decay_ratio": 0.0,
          "stall_min_entry_velocity": 99999.0}),  # never triggers
        ("CHEAP-ONLY entry 30-39c (max_entry=39)",
         {**base_entry, "max_entry_cents": 39},
         dict(base_stall)),
        ("CHEAP+HOLD (max_entry=39, hold to settle)",
         {**base_entry, "max_entry_cents": 39},
         {**base_stall, "stall_decay_ratio": 0.0,
          "stall_min_entry_velocity": 99999.0}),
    ]

    for label, entry_cfg, stall_cfg in configs:
        all_cycles = []
        for ticker, snaps in load_snapshots(con):
            if not snaps:
                continue
            cycles = simulate_ticker(
                ticker, snaps, settlements.get(ticker),
                entry_cfg, stall_cfg,
                cooldown_s=args.cooldown,
                max_cycles=args.max_cycles,
                balance=args.balance,
            )
            all_cycles.extend(cycles)
        print_result(label, aggregate(all_cycles))

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
