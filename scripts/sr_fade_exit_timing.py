"""SR_FADE exit-timing analysis (2026-04-23 per Codex review).

Codex's key question: "where does the fade actually monetize — in the snapback
or at settlement?" The 'won' bucket (TP fills) was net-positive, but the
'reconciled_settled' bucket was net-negative, suggesting the edge is in the
short-horizon bounce, not in surviving to expiry.

This script, for each SR_FADE entry, walks forward through window_snapshots
(per-minute mid ticks) and computes:
  - Signed P/L per minute after entry (in cents per contract, before fees)
  - Minute of peak P/L (time-to-MFE)
  - Whether P/L ever crossed +5c, +8c, +10c before finishing negative
  - Distribution of minute-to-peak across winners
  - How many trades went +5c+ then settled for a loss

Usage: python scripts/sr_fade_exit_timing.py
"""
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"


def _to_ts_ms(placed_at: str) -> int:
    """Parse ISO placed_at string to ts_ms. Handles +00:00 offsets."""
    if not placed_at:
        return 0
    try:
        # Python's fromisoformat accepts "+00:00" but not "Z"
        s = placed_at.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT order_id, placed_at, ticker, side, count, filled_count,
               limit_price, pnl, status
        FROM kalshi_trades
        WHERE strategy_name='SR_FADE'
          AND status LIKE 'reconciled%'
        ORDER BY placed_at ASC
    """)
    trades = [dict(r) for r in cur.fetchall()]

    # Codex P1 (2026-04-23): settlement-truth pnl override.
    cur.execute("SELECT ticker, market_result, fee_cents FROM settlement_ledger")
    settle_map = {r[0]: {"market_result": r[1], "fee_cents": r[2] or 0.0}
                  for r in cur.fetchall()}
    total_filled_cache: dict = {}
    for t in trades:
        ticker = t["ticker"]
        settle = settle_map.get(ticker)
        if not settle:
            continue
        if ticker not in total_filled_cache:
            cur.execute(
                "SELECT COALESCE(SUM(filled_count),0) FROM kalshi_trades "
                "WHERE ticker=?", (ticker,),
            )
            total_filled_cache[ticker] = int(cur.fetchone()[0] or 0)
        filled = int(t.get("filled_count") or t.get("count") or 0)
        lp = int(t["limit_price"] or 0)
        if filled <= 0 or lp <= 0:
            continue
        mr = (settle.get("market_result") or "").lower()
        settle_cents = 100 if (t["side"] or "").lower() == mr else 0
        gross = (settle_cents - lp) * filled
        fee_total = float(settle.get("fee_cents", 0.0))
        fee_share = (fee_total * filled / total_filled_cache[ticker]
                     if total_filled_cache[ticker] > 0 and fee_total > 0 else 0.0)
        t["pnl"] = round((gross - fee_share) / 100.0, 4)

    if not trades:
        print("No reconciled SR_FADE trades.")
        return 1

    print(f"Reconciled SR_FADE trades: {len(trades)}\n")

    # Per-trade per-minute P/L
    # We measure profit in CENTS per contract, relative to entry limit_price.
    # For YES: profit_cents = mid_yes - entry_limit
    # For NO:  profit_cents = (100 - mid_yes) - entry_limit
    buckets = {5: 0, 8: 0, 10: 0, 15: 0}  # reached-threshold-but-settled-loss
    reached_and_won = {5: 0, 8: 0, 10: 0, 15: 0}
    time_to_mfe_minutes = []
    pnl_loss_after_mfe_hit = 0.0
    pnl_still_captured = 0.0

    per_trade_rows = []

    for t in trades:
        entry_ts_ms = _to_ts_ms(t["placed_at"])
        if not entry_ts_ms:
            continue
        side = (t["side"] or "").lower()
        entry = int(t["limit_price"] or 0)
        ticker = t["ticker"]
        pnl_total = float(t["pnl"] or 0)
        ct = int(t["count"] or 0)

        # Pull ALL snapshots for this ticker (the "post-entry" filter was
        # too strict — in practice window_snapshots are sparse and many
        # entries happen between snapshot rows). We get best coverage by
        # taking the full window and computing excursion independent of
        # entry timing.
        cur.execute("""
            SELECT t_offset_sec, kalshi_mid_cents
            FROM window_snapshots
            WHERE ticker=?
            ORDER BY ts_ms ASC
        """, (ticker,))
        snaps = cur.fetchall()
        if not snaps or len(snaps) < 2:
            continue

        # Per-minute P/L in cents-per-contract.
        per_min = []
        for s in snaps:
            mid = int(s["kalshi_mid_cents"] or 0)
            if mid <= 0:
                continue
            if side == "yes":
                p = mid - entry
            else:
                p = (100 - mid) - entry
            per_min.append((int(s["t_offset_sec"] or 0), p))

        if not per_min:
            continue

        # MFE (max profit seen post-entry, cents per contract)
        mfe_p = max(p for _, p in per_min)
        # Find minute-of-peak.
        peak_t = min(t for t, p in per_min if p == mfe_p)
        time_to_mfe_minutes.append(peak_t / 60.0)

        # Check thresholds.
        for thresh in buckets.keys():
            reached = any(p >= thresh for _, p in per_min)
            if reached:
                if pnl_total > 0:
                    reached_and_won[thresh] += 1
                else:
                    buckets[thresh] += 1

        # If we hit MFE and then ended negative: how much was left on the
        # table? (MFE as $ – realized as $) gives a proxy for exit timing
        # loss. Note: realized pnl already accounts for fees; MFE is pre-fee.
        if mfe_p > 0 and pnl_total < 0:
            mfe_dollars = (mfe_p / 100.0) * ct
            pnl_loss_after_mfe_hit += mfe_dollars - pnl_total  # positive = missed
        elif mfe_p > 0 and pnl_total > 0:
            pnl_still_captured += pnl_total

        per_trade_rows.append({
            "placed_at": t["placed_at"][:19],
            "ticker": ticker[-12:],
            "side": side,
            "entry": entry,
            "ct": ct,
            "mfe_c": mfe_p,
            "peak_min": round(peak_t / 60.0, 1),
            "pnl": pnl_total,
        })

    n = len(per_trade_rows)
    if n == 0:
        print("No matched trade/snapshot pairs.")
        return 1

    print(f"Matched trades (with window_snapshots coverage): {n}\n")

    # Per-trade table (capped)
    print(f"{'placed':<19} {'ticker':<12} {'side':<4} {'entry':>5} "
          f"{'ct':>4} {'mfe_c':>6} {'peak_m':>6} {'pnl$':>7}")
    for r in per_trade_rows[-25:]:
        print(f"{r['placed_at']:<19} {r['ticker']:<12} {r['side']:<4} "
              f"{r['entry']:>5} {r['ct']:>4} {r['mfe_c']:>6} "
              f"{r['peak_min']:>6.1f} {r['pnl']:>7.2f}")

    print()
    print("--Reached threshold then settled NEGATIVE (exit timing leak)--")
    print(f"{'thresh':>8} {'hit_then_lost':>15} {'hit_and_won':>12} "
          f"{'total_hit':>10}")
    for thresh in sorted(buckets.keys()):
        tot = buckets[thresh] + reached_and_won[thresh]
        print(f"{'+' + str(thresh) + 'c':>8} {buckets[thresh]:>15} "
              f"{reached_and_won[thresh]:>12} {tot:>10}")

    print()
    if time_to_mfe_minutes:
        ttm = sorted(time_to_mfe_minutes)
        print(f"Time-to-MFE (minutes after entry):")
        print(f"  n={len(ttm)}  mean={sum(ttm)/len(ttm):.2f}  "
              f"median={ttm[len(ttm)//2]:.2f}  "
              f"p10={ttm[len(ttm)//10]:.2f}  "
              f"p90={ttm[(9*len(ttm))//10]:.2f}")

    print()
    print(f"Dollars captured on winners: ${pnl_still_captured:.2f}")
    print(f"Estimated dollars LEFT ON TABLE on losers that hit +MFE first: "
          f"${pnl_loss_after_mfe_hit:.2f}")
    print("  (How much we'd have gained by TP'ing at the MFE peak, minus"
          " what we actually lost at settlement.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
