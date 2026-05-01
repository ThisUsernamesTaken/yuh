"""Backtest late-window dominant-side expiry strategy.

Strategy:
  - At a chosen late minute (13 or 14), observe Kalshi YES mid.
  - Buy the dominant side: YES if yes_mid >= 50, else NO.
  - Full-port the account balance at that side's mid price.
  - Hold to settlement and compound.

This intentionally tests the simple research claim: late in a 15m binary,
the side currently dominant wins often enough for small compounding gains.

Usage:
  python scripts/late_dominant_backtest.py --start 100 --minute 13
  python scripts/late_dominant_backtest.py --all
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path


DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"


@dataclass
class Trade:
    ticker: str
    offset_s: int
    yes_mid: int
    side: str
    price_cents: int
    result: str
    won: bool
    contracts: int
    balance_before: float
    balance_after: float
    pnl: float


def _pick_snapshot(rows: list[sqlite3.Row], target_s: int,
                   tolerance_s: int = 45) -> sqlite3.Row | None:
    if not rows:
        return None
    eligible = [r for r in rows
                if abs(int(r["t_offset_sec"]) - target_s) <= tolerance_s]
    if not eligible:
        return None
    # Prefer latest snapshot at or before target; fallback to closest after.
    before = [r for r in eligible if int(r["t_offset_sec"]) <= target_s]
    if before:
        return max(before, key=lambda r: int(r["t_offset_sec"]))
    return min(eligible, key=lambda r: abs(int(r["t_offset_sec"]) - target_s))


def run(minute: int, start_balance: float, min_price: int = 1,
        max_price: int = 99) -> list[Trade]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tickers = cur.execute("""
        SELECT DISTINCT w.ticker
        FROM window_snapshots w
        JOIN settlement_ledger s ON s.ticker = w.ticker
        WHERE s.market_result IN ('yes', 'no')
        ORDER BY w.ticker
    """).fetchall()
    balance = float(start_balance)
    trades: list[Trade] = []
    target_s = int(minute * 60)
    for tr in tickers:
        ticker = tr["ticker"]
        rows = cur.execute("""
            SELECT t_offset_sec, kalshi_mid_cents
            FROM window_snapshots
            WHERE ticker=?
              AND kalshi_mid_cents IS NOT NULL
              AND kalshi_mid_cents BETWEEN 1 AND 99
            ORDER BY t_offset_sec ASC
        """, (ticker,)).fetchall()
        snap = _pick_snapshot(rows, target_s)
        if snap is None:
            continue
        yes_mid = int(snap["kalshi_mid_cents"])
        side = "yes" if yes_mid >= 50 else "no"
        price = yes_mid if side == "yes" else 100 - yes_mid
        if price < min_price or price > max_price:
            continue
        result = cur.execute(
            "SELECT market_result FROM settlement_ledger WHERE ticker=?",
            (ticker,),
        ).fetchone()["market_result"].lower()
        contracts = int((balance * 100) // price)
        if contracts <= 0:
            break
        cost = contracts * price / 100.0
        cash_left = balance - cost
        won = result == side
        payout = float(contracts) if won else 0.0
        new_balance = cash_left + payout
        trades.append(Trade(
            ticker=ticker,
            offset_s=int(snap["t_offset_sec"]),
            yes_mid=yes_mid,
            side=side,
            price_cents=price,
            result=result,
            won=won,
            contracts=contracts,
            balance_before=balance,
            balance_after=new_balance,
            pnl=new_balance - balance,
        ))
        balance = new_balance
        if balance <= 0:
            break
    conn.close()
    return trades


def print_summary(minute: int, trades: list[Trade], start_balance: float) -> None:
    print(f"\nLate dominant full-port @ minute {minute}")
    print(f"Start balance: ${start_balance:.2f}")
    print(f"Trades: {len(trades)}")
    if not trades:
        return
    wins = sum(1 for t in trades if t.won)
    pnl = trades[-1].balance_after - start_balance
    print(f"Final balance: ${trades[-1].balance_after:.2f}")
    print(f"P&L: ${pnl:+.2f}  return={pnl/start_balance*100:+.1f}%")
    print(f"WR: {wins}/{len(trades)} = {wins/len(trades)*100:.1f}%")
    max_bal = max(t.balance_after for t in trades)
    min_bal = min(t.balance_after for t in trades)
    print(f"Balance range: ${min_bal:.2f} - ${max_bal:.2f}")
    print("\nRecent trades:")
    for t in trades[-12:]:
        print(
            f"  {t.ticker[-18:]:<18} off={t.offset_s:>3}s "
            f"{t.side.upper():<3}@{t.price_cents:>2} mid={t.yes_mid:>2} "
            f"settle={t.result.upper():<3} {'WIN ' if t.won else 'LOSS'} "
            f"ct={t.contracts:<6} bal ${t.balance_before:>8.2f} -> ${t.balance_after:>8.2f} "
            f"pnl=${t.pnl:+.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=100.0)
    ap.add_argument("--minute", type=int, default=13, choices=[13, 14])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    minutes = [13, 14] if args.all else [args.minute]
    for minute in minutes:
        trades = run(minute=minute, start_balance=args.start)
        print_summary(minute, trades, args.start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
