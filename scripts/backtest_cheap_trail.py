"""Backtest: buy cheapest contract before minute 10, trailing TP.

Strategy
--------
For each settled 15-min binary market:
  1. Find the first snapshot with t_offset_sec < 600 (minute 10).
  2. Determine "cheap side" = side with lower ask.
  3. Enter at cheap side's ask price (taker fill).
  4. Track HWM of cheap side's bid over remaining snapshots.
  5. Exit when current_bid <= HWM - trail_distance (trailing TP fires).
  6. If trail never fires and we hold to expiry: settle at 100c (won) or 0c (lost).

Data sources
------------
- data/trades.db : window_snapshots (per-ticker bid/ask/mid time series)
- data/kalshi_external_backtest.db : markets (binary result yes/no)

We join on ticker, simulate per-tick fills, and roll up P&L.
Tests multiple trail distances + entry price filters to find the regime.

Fee model: Kalshi maker = 7% × p × (1−p), taker = 1.4× maker.
For an entry at the ASK we pay taker; trailing exits are taker too
(crossing the spread to hit the bid). Settlement is fee-free.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Iterator


TRADES_DB = "data/trades.db"
SETTLE_DB = "data/kalshi_external_backtest.db"


# ── Fee math (Kalshi) ─────────────────────────────────────────────────


def taker_fee_cents(price_c: int, count: int) -> float:
    """Round-trip-style fee per Kalshi: 7% × p × (1-p) × count, then 1.4× for taker."""
    p = price_c / 100.0
    maker = 0.07 * p * (1 - p) * count * 100  # cents
    return maker * 1.4


# ── Per-trade simulation ──────────────────────────────────────────────


@dataclass
class TradeRecord:
    ticker: str
    side: str            # "yes" or "no"
    entry_offset_s: int
    entry_c: int
    exit_offset_s: int
    exit_c: int
    exit_reason: str     # "trail" | "settle_win" | "settle_loss"
    contracts: int
    pnl_cents: float
    fees_cents: float


def simulate_ticker(
    snapshots: list[tuple],
    market_result: str,
    *,
    contracts: int,
    trail_c: int,
    max_entry_c: int,
    max_entry_offset_s: int,
) -> TradeRecord | None:
    """Simulate one full session for a single ticker.

    snapshots: list of (t_offset_sec, kalshi_bid, kalshi_ask, kalshi_mid)
               for the cheap side AT FIRST. We re-derive cheap side here
               at entry-time, then track that side throughout.
    """
    # window_snapshots stores ONE side's view (kalshi_bid / kalshi_ask
    # are the YES side per engine convention). We derive both sides:
    #   yes_bid = kalshi_bid       yes_ask = kalshi_ask
    #   no_bid  = 100 - yes_ask    no_ask  = 100 - yes_bid
    # Find entry tick: first t_offset_sec < max_entry_offset_s with valid
    # quotes on both sides.
    entry_tick = None
    for row in snapshots:
        t_off, yes_bid, yes_ask, _mid = row
        if t_off >= max_entry_offset_s:
            break
        if yes_bid is None or yes_ask is None:
            continue
        if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= 100 or yes_ask >= 100:
            continue
        no_bid = 100 - yes_ask
        no_ask = 100 - yes_bid
        if no_bid <= 0 or no_ask <= 0:
            continue
        # Cheap side = side with lower ASK (cheaper to buy).
        if yes_ask <= no_ask:
            side = "yes"
            entry_c = yes_ask
        else:
            side = "no"
            entry_c = no_ask
        if entry_c > max_entry_c:
            continue  # too expensive — wait
        entry_tick = (t_off, side, entry_c)
        break

    if entry_tick is None:
        return None

    entry_offset_s, side, entry_c = entry_tick
    fees = taker_fee_cents(entry_c, contracts)

    # Walk the rest of the session, tracking HWM of OUR-side bid.
    hwm_bid = entry_c
    last_bid = entry_c
    last_offset = entry_offset_s
    trail_fired = False
    exit_offset_s = entry_offset_s
    exit_c = entry_c

    for row in snapshots:
        t_off, yes_bid, yes_ask, _mid = row
        if t_off <= entry_offset_s:
            continue
        if yes_bid is None or yes_ask is None:
            continue
        if side == "yes":
            our_bid = yes_bid
        else:
            our_bid = 100 - yes_ask  # no_bid
        if our_bid <= 0 or our_bid >= 100:
            continue
        last_bid = our_bid
        last_offset = t_off
        if our_bid > hwm_bid:
            hwm_bid = our_bid
        if hwm_bid - our_bid >= trail_c:
            # Trailing TP fires: exit at current bid (cross-spread sell, taker).
            trail_fired = True
            exit_offset_s = t_off
            exit_c = our_bid
            break

    if trail_fired:
        exit_reason = "trail"
        # Trailing exit pays taker fee at exit price.
        fees += taker_fee_cents(exit_c, contracts)
    else:
        # Held to settlement.
        if (side == "yes" and market_result == "yes") or \
           (side == "no" and market_result == "no"):
            exit_c = 100  # settled in our favor — full $1.00
            exit_reason = "settle_win"
        else:
            exit_c = 0
            exit_reason = "settle_loss"
        # No fee on settlement (Kalshi expires positions for free).

    pnl_cents = (exit_c - entry_c) * contracts - fees

    return TradeRecord(
        ticker="",                     # filled by caller
        side=side,
        entry_offset_s=entry_offset_s,
        entry_c=entry_c,
        exit_offset_s=exit_offset_s,
        exit_c=exit_c,
        exit_reason=exit_reason,
        contracts=contracts,
        pnl_cents=pnl_cents,
        fees_cents=fees,
    )


# ── Data loading ──────────────────────────────────────────────────────


def load_settled_tickers() -> dict[str, str]:
    """Map ticker → market_result for finalized markets."""
    conn = sqlite3.connect(SETTLE_DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT ticker, result FROM markets "
        "WHERE status = 'finalized' AND result IN ('yes','no')"
    ).fetchall()
    conn.close()
    return {t: r for t, r in rows}


def load_snapshots_by_ticker() -> dict[str, list[tuple]]:
    """Map ticker → ordered list of (t_offset_sec, yes_bid, yes_ask, mid)."""
    conn = sqlite3.connect(TRADES_DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT ticker, t_offset_sec, kalshi_bid_cents, "
        "kalshi_ask_cents, kalshi_mid_cents "
        "FROM window_snapshots "
        "ORDER BY ticker, t_offset_sec"
    ).fetchall()
    conn.close()
    out: dict[str, list[tuple]] = {}
    for ticker, t_off, bid, ask, mid in rows:
        out.setdefault(ticker, []).append((t_off, bid, ask, mid))
    return out


# ── Run ────────────────────────────────────────────────────────────────


def run(*, trail_c: int, max_entry_c: int, max_entry_offset_s: int = 600,
        contracts: int = 10) -> dict:
    """Simulate the full corpus once with one parameter set."""
    settled = load_settled_tickers()
    snapshots = load_snapshots_by_ticker()
    common = set(settled) & set(snapshots)

    trades: list[TradeRecord] = []
    for ticker in common:
        market_result = settled[ticker]
        snaps = snapshots[ticker]
        if len(snaps) < 2:
            continue
        rec = simulate_ticker(
            snaps, market_result,
            contracts=contracts, trail_c=trail_c,
            max_entry_c=max_entry_c,
            max_entry_offset_s=max_entry_offset_s,
        )
        if rec is None:
            continue
        rec.ticker = ticker
        trades.append(rec)

    # Aggregate
    if not trades:
        return {"trades": 0}
    pnls = [t.pnl_cents for t in trades]
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t)
    total_pnl_dollars = sum(pnls) / 100
    win_count = sum(1 for p in pnls if p > 0)
    return {
        "trail_c": trail_c,
        "max_entry_c": max_entry_c,
        "trades": len(trades),
        "wins": win_count,
        "win_rate": win_count / len(trades),
        "mean_pnl_c": statistics.mean(pnls),
        "median_pnl_c": statistics.median(pnls),
        "total_pnl_dollars": total_pnl_dollars,
        "by_reason": {k: {"n": len(v), "mean_c": statistics.mean(t.pnl_cents for t in v)}
                      for k, v in by_reason.items()},
        "max_loss_c": min(pnls),
        "max_win_c": max(pnls),
    }


def main() -> None:
    print(f"Loaded settled-tickers + snapshots...")
    settled = load_settled_tickers()
    snapshots = load_snapshots_by_ticker()
    common = set(settled) & set(snapshots)
    print(f"  {len(settled):,} settled, {len(snapshots):,} with snapshots, "
          f"{len(common):,} joinable")
    print()
    print(f"  YES-result count: {sum(1 for r in settled.values() if r=='yes')}")
    print(f"  NO-result count:  {sum(1 for r in settled.values() if r=='no')}")
    print()

    print(
        f"{'TRAIL':>6} {'MAX_ENT':>8} {'TRADES':>7} {'WIN%':>6} "
        f"{'MEAN_C':>8} {'MEDIAN':>7} {'TOTAL_$':>9} "
        f"{'TRAIL%':>7} {'WIN%':>6} {'LOSS%':>6} "
        f"{'MAX_W':>6} {'MAX_L':>7}"
    )
    print("-" * 110)

    # Sweep parameters.
    grid = []
    for max_entry_c in (15, 25, 35, 50, 99):  # 99 = no cap
        for trail_c in (2, 3, 5, 8, 12):
            r = run(trail_c=trail_c, max_entry_c=max_entry_c, contracts=10)
            grid.append(r)
            br = r.get("by_reason", {})
            n = r["trades"]
            t_pct = (br.get("trail", {}).get("n", 0) / n * 100) if n else 0
            w_pct = (br.get("settle_win", {}).get("n", 0) / n * 100) if n else 0
            l_pct = (br.get("settle_loss", {}).get("n", 0) / n * 100) if n else 0
            print(
                f"{trail_c:>6} {max_entry_c:>8} {n:>7} {r['win_rate']*100:>5.1f}% "
                f"{r['mean_pnl_c']:>+8.1f} {r['median_pnl_c']:>+7.1f} "
                f"{r['total_pnl_dollars']:>+9.2f} "
                f"{t_pct:>6.1f}% {w_pct:>5.1f}% {l_pct:>5.1f}% "
                f"{r['max_win_c']:>+5.0f} {r['max_loss_c']:>+6.0f}"
            )
    print()

    # Best by total $
    grid_sorted = sorted(grid, key=lambda r: r.get("total_pnl_dollars", -1e9), reverse=True)
    print("Top 5 configs by total $:")
    for r in grid_sorted[:5]:
        print(f"  trail={r['trail_c']}c max_entry={r['max_entry_c']}c -> "
              f"${r['total_pnl_dollars']:+.2f} on {r['trades']} trades, "
              f"win={r['win_rate']*100:.1f}%")


if __name__ == "__main__":
    main()
