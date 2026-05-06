"""Deeper analysis of the cheap-buy + trailing-TP strategy.

Builds on backtest_cheap_trail.py with:
  - Time-distribution of entries (when in the window do we enter?)
  - Entry-price distribution
  - Exit-reason economics (trail vs settle_win vs settle_loss)
  - Per-day equity curve simulation (flat sizing)
  - Compounding sim (proportional sizing)
  - Sensitivity to entry-time cap

Goal: understand whether this is a strategy worth shipping live, vs
Tier-aware FVG. If +EV per trade exists at low entry prices, the
question becomes execution quality (can we actually fill at those
cheap prices on Kalshi).
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

TRADES_DB = "data/trades.db"
SETTLE_DB = "data/kalshi_external_backtest.db"


def taker_fee_cents(price_c: int, count: int) -> float:
    p = price_c / 100.0
    maker = 0.07 * p * (1 - p) * count * 100
    return maker * 1.4


@dataclass
class Trade:
    ticker: str
    side: str
    entry_offset_s: int
    entry_c: int
    exit_offset_s: int
    exit_c: int
    exit_reason: str
    contracts: int
    pnl_cents: float
    fees_cents: float


def simulate(snaps, market_result, *, contracts, trail_c,
             max_entry_c, max_entry_offset_s):
    entry = None
    for t_off, yes_bid, yes_ask, _ in snaps:
        if t_off >= max_entry_offset_s:
            break
        if not yes_bid or not yes_ask or yes_bid >= 100 or yes_ask >= 100:
            continue
        no_bid = 100 - yes_ask
        no_ask = 100 - yes_bid
        if no_bid <= 0 or no_ask <= 0:
            continue
        if yes_ask <= no_ask:
            side, entry_c = "yes", yes_ask
        else:
            side, entry_c = "no", no_ask
        if entry_c > max_entry_c:
            continue
        entry = (t_off, side, entry_c)
        break
    if entry is None:
        return None
    entry_off_s, side, entry_c = entry
    fees = taker_fee_cents(entry_c, contracts)
    hwm = entry_c
    trail_fired = False
    exit_off_s = entry_off_s
    exit_c = entry_c
    for t_off, yes_bid, yes_ask, _ in snaps:
        if t_off <= entry_off_s:
            continue
        if not yes_bid or not yes_ask:
            continue
        our_bid = yes_bid if side == "yes" else 100 - yes_ask
        if our_bid <= 0 or our_bid >= 100:
            continue
        if our_bid > hwm:
            hwm = our_bid
        if hwm - our_bid >= trail_c:
            trail_fired = True
            exit_off_s = t_off
            exit_c = our_bid
            break
    if trail_fired:
        reason = "trail"
        fees += taker_fee_cents(exit_c, contracts)
    else:
        if (side == "yes" and market_result == "yes") or \
           (side == "no" and market_result == "no"):
            exit_c = 100
            reason = "settle_win"
        else:
            exit_c = 0
            reason = "settle_loss"
    pnl = (exit_c - entry_c) * contracts - fees
    return Trade("", side, entry_off_s, entry_c, exit_off_s, exit_c,
                 reason, contracts, pnl, fees)


def load_data():
    conn = sqlite3.connect(SETTLE_DB)
    settled = dict(conn.execute(
        "SELECT ticker, result FROM markets "
        "WHERE status='finalized' AND result IN ('yes','no')"
    ).fetchall())
    conn.close()
    conn = sqlite3.connect(TRADES_DB)
    rows = conn.execute(
        "SELECT ticker, t_offset_sec, kalshi_bid_cents, "
        "kalshi_ask_cents, kalshi_mid_cents FROM window_snapshots "
        "ORDER BY ticker, t_offset_sec"
    ).fetchall()
    conn.close()
    snaps_by_ticker = defaultdict(list)
    for ticker, t, bid, ask, mid in rows:
        snaps_by_ticker[ticker].append((t, bid, ask, mid))
    return settled, snaps_by_ticker


def run_corpus(settled, snaps_by_ticker, *, trail_c, max_entry_c,
               max_entry_offset_s, contracts):
    trades: list[Trade] = []
    for ticker, market_result in settled.items():
        snaps = snaps_by_ticker.get(ticker)
        if not snaps or len(snaps) < 2:
            continue
        rec = simulate(snaps, market_result,
                       contracts=contracts, trail_c=trail_c,
                       max_entry_c=max_entry_c,
                       max_entry_offset_s=max_entry_offset_s)
        if rec is None:
            continue
        rec.ticker = ticker
        trades.append(rec)
    return trades


def analyze(trades: list[Trade], label: str):
    if not trades:
        print(f"\n{label}: no trades")
        return
    pnls = [t.pnl_cents for t in trades]
    n = len(trades)
    wins = [t for t in trades if t.pnl_cents > 0]
    losses = [t for t in trades if t.pnl_cents <= 0]
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason].append(t)
    by_entry_bucket = defaultdict(list)
    for t in trades:
        bucket = (t.entry_c // 5) * 5
        by_entry_bucket[bucket].append(t)

    print(f"\n=== {label} ===")
    print(f"n={n}  win_rate={len(wins)/n*100:.1f}%  "
          f"mean_pnl={statistics.mean(pnls):+.1f}c  "
          f"total=${sum(pnls)/100:+.2f}")
    print(f"avg_win={statistics.mean(t.pnl_cents for t in wins):+.1f}c "
          f"({len(wins)})  "
          f"avg_loss={statistics.mean(t.pnl_cents for t in losses):+.1f}c "
          f"({len(losses)})")
    print(f"max_win={max(pnls):+.0f}c  max_loss={min(pnls):+.0f}c")
    print(f"by exit reason:")
    for r, ts in sorted(by_reason.items()):
        ps = [t.pnl_cents for t in ts]
        print(f"  {r:>14}: n={len(ts):>4}  "
              f"mean={statistics.mean(ps):+7.1f}c  "
              f"total=${sum(ps)/100:+7.2f}")
    print(f"entry-price buckets (cents, in 5c bins):")
    for bucket in sorted(by_entry_bucket):
        ts = by_entry_bucket[bucket]
        ps = [t.pnl_cents for t in ts]
        win_count = sum(1 for t in ts if t.pnl_cents > 0)
        print(f"  {bucket:>2}-{bucket+4:<2}: n={len(ts):>4}  "
              f"win={win_count/len(ts)*100:>5.1f}%  "
              f"mean_pnl={statistics.mean(ps):+7.1f}c  "
              f"total=${sum(ps)/100:+7.2f}")


def equity_curve(trades, *, starting_bal_dollars=50.0,
                 contracts_per_trade=10, sort_by_time=True):
    """Flat-sizing P&L curve with realistic going-broke check."""
    if sort_by_time:
        trades = sorted(trades, key=lambda t: t.entry_offset_s)
    bal = starting_bal_dollars * 100
    curve = [bal]
    busted = False
    for t in trades:
        cost = t.entry_c * t.contracts
        if cost > bal:  # can't afford
            curve.append(bal)
            continue
        bal += t.pnl_cents
        curve.append(bal)
        if bal < 100:  # below $1 — bust
            busted = True
            break
    return curve, busted


def compounding_curve(trades, *, starting_bal_dollars=50.0,
                      bet_frac=0.20, max_contracts_cap=200,
                      sort_by_time=True):
    """Compounding sim: bet `bet_frac` of bankroll per trade."""
    if sort_by_time:
        trades = sorted(trades, key=lambda t: t.entry_offset_s)
    bal = starting_bal_dollars * 100
    curve = [bal]
    for t in trades:
        if t.entry_c <= 0:
            continue
        notional = bal * bet_frac
        contracts = int(notional / t.entry_c)
        contracts = min(contracts, max_contracts_cap)
        if contracts < 1 or contracts * t.entry_c > bal:
            curve.append(bal)
            continue
        # Re-derive PnL at our (different) contracts count.
        fees = taker_fee_cents(t.entry_c, contracts)
        if t.exit_reason == "trail":
            fees += taker_fee_cents(t.exit_c, contracts)
        pnl = (t.exit_c - t.entry_c) * contracts - fees
        bal += pnl
        curve.append(bal)
        if bal < 100:
            break
    return curve


def main():
    print("Loading...")
    settled, snaps = load_data()
    common = set(settled) & set(snaps)
    print(f"  {len(common)} joinable tickers (settled + snapshots)")
    print()

    # --- Anchor config: max_entry=25c, trail=2c, entry before min 10 ---
    print("=" * 70)
    print("ANCHOR: cheap_max=25c, trail=2c, entry before min 10")
    print("=" * 70)
    trades = run_corpus(settled, snaps,
                        trail_c=2, max_entry_c=25,
                        max_entry_offset_s=600, contracts=10)
    analyze(trades, "10ct flat / 25c cap / trail=2c / pre-min-10")

    print("\n--- Equity curve ($50 BR, 10ct flat) ---")
    curve, busted = equity_curve(trades, starting_bal_dollars=50)
    print(f"  start: ${curve[0]/100:.2f}  end: ${curve[-1]/100:+.2f}  "
          f"trades_taken={len(curve)-1}  bust={busted}")
    print(f"  peak: ${max(curve)/100:.2f}  trough: ${min(curve)/100:.2f}")

    print("\n--- Compounding curve (20% bet) ---")
    cc = compounding_curve(trades, starting_bal_dollars=50, bet_frac=0.20)
    print(f"  start: ${cc[0]/100:.2f}  end: ${cc[-1]/100:.2f}  "
          f"trades_taken={len(cc)-1}")
    print(f"  peak: ${max(cc)/100:.2f}  trough: ${min(cc)/100:.2f}")

    print("\n--- Compounding curve (10% bet) ---")
    cc10 = compounding_curve(trades, starting_bal_dollars=50, bet_frac=0.10)
    print(f"  start: ${cc10[0]/100:.2f}  end: ${cc10[-1]/100:.2f}  "
          f"trades_taken={len(cc10)-1}")
    print(f"  peak: ${max(cc10)/100:.2f}  trough: ${min(cc10)/100:.2f}")

    # --- Tighter cap variant ---
    print("\n" + "=" * 70)
    print("TIGHTER: cheap_max=20c (extreme cheap only)")
    print("=" * 70)
    trades = run_corpus(settled, snaps,
                        trail_c=2, max_entry_c=20,
                        max_entry_offset_s=600, contracts=10)
    analyze(trades, "10ct flat / 20c cap / trail=2c / pre-min-10")

    # --- Earlier entry-time cut ---
    print("\n" + "=" * 70)
    print("EARLIER: entry only before minute 5 (max_entry=25c)")
    print("=" * 70)
    trades = run_corpus(settled, snaps,
                        trail_c=2, max_entry_c=25,
                        max_entry_offset_s=300, contracts=10)
    analyze(trades, "10ct flat / 25c cap / trail=2c / pre-min-5")

    # --- Trail variants on 25c cap ---
    print("\n" + "=" * 70)
    print("TRAIL VARIANTS (max_entry=25c, pre-min-10)")
    print("=" * 70)
    print(f"\n{'trail_c':>8} {'n':>5} {'win%':>6} {'mean_c':>8} "
          f"{'total_$':>8} {'trail_n':>8}")
    for trail_c in (2, 3, 5, 8, 12, 20):
        trades = run_corpus(settled, snaps,
                            trail_c=trail_c, max_entry_c=25,
                            max_entry_offset_s=600, contracts=10)
        n = len(trades)
        wins = sum(1 for t in trades if t.pnl_cents > 0)
        pnls = [t.pnl_cents for t in trades]
        trail_n = sum(1 for t in trades if t.exit_reason == "trail")
        print(f"{trail_c:>8} {n:>5} {wins/n*100:>5.1f}% "
              f"{statistics.mean(pnls):>+7.1f} "
              f"${sum(pnls)/100:>+7.2f} {trail_n:>8}")


if __name__ == "__main__":
    main()
