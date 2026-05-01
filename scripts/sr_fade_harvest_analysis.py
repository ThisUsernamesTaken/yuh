"""Harvest analysis — Codex 2026-04-23.

Question: for sessions where BTC crossed its opening price (the 70% of
windows that DO mean-revert tonight), what was the maximum favorable
contract bid move AFTER our hypothetical entry time, and how long did
the market stay there?

If BTC reverts 70% but the contract only offers fillable +5c for 1-2
seconds, the alpha is theoretical, not harvestable by a maker-only engine.

Data granularity: window_snapshots is per-minute. Too coarse for 1-second
questions, but sufficient to answer "did the bid EVER hold the peak for
>=60 seconds" which is the relevant question for maker fills.

Uses authoritative settlement_ledger pnl (not kalshi_trades.pnl).

Usage: python scripts/sr_fade_harvest_analysis.py
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"


def _to_ts_ms(placed_at: str) -> int:
    if not placed_at:
        return 0
    s = placed_at.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _favorable_profit_c(side: str, mid: int, entry: int) -> int:
    """Profit in cents per contract if we hypothetically exited at this mid.
    For YES: profit = mid - entry. For NO: profit = (100 - mid) - entry."""
    if side.lower() == "yes":
        return int(mid) - int(entry)
    return (100 - int(mid)) - int(entry)


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Load settlement map for pnl truth
    cur.execute("SELECT ticker, market_result, fee_cents FROM settlement_ledger")
    settle_map = {r[0]: {"market_result": r[1], "fee_cents": r[2] or 0.0}
                  for r in cur.fetchall()}

    # Pull reconciled SR_FADE trades from last 8h
    cutoff_ts = (datetime.now(timezone.utc).timestamp() - 28800) * 1000
    cur.execute("""
        SELECT order_id, placed_at, ticker, side, count, filled_count,
               limit_price, status, pnl
        FROM kalshi_trades
        WHERE strategy_name='SR_FADE' AND status LIKE 'reconciled%'
        ORDER BY placed_at ASC
    """)
    trades = [dict(r) for r in cur.fetchall()]
    # Filter to last 8h
    trades = [t for t in trades if _to_ts_ms(t["placed_at"]) >= cutoff_ts]

    if not trades:
        # Fallback: show all available reconciled trades
        cur.execute("""
            SELECT order_id, placed_at, ticker, side, count, filled_count,
                   limit_price, status, pnl
            FROM kalshi_trades
            WHERE strategy_name='SR_FADE' AND status LIKE 'reconciled%'
            ORDER BY placed_at DESC LIMIT 15
        """)
        trades = [dict(r) for r in cur.fetchall()]

    print(f"Analyzing {len(trades)} SR_FADE trades\n")
    print(f"{'placed':<19} {'ticker':<12} {'side':<4} {'entry':>5} "
          f"{'btc_crossed':>11} {'max_fav_c':>9} {'dwell_min':>9} "
          f"{'pnl$':>7} {'assessment'}")
    print("-" * 130)

    summary = {
        "btc_crossed_fav_never_positive": 0,
        "btc_crossed_brief_peak_only": 0,  # peak < 2 snapshots at peak
        "btc_crossed_sustained_peak": 0,   # peak >= 2 snapshots
        "btc_did_not_cross": 0,
    }

    for t in trades:
        entry_ts_ms = _to_ts_ms(t["placed_at"])
        ticker = t["ticker"]
        side = (t["side"] or "").lower()
        entry = int(t["limit_price"] or 0)
        filled = int(t.get("filled_count") or t.get("count") or 0)

        # Settlement truth pnl
        settle = settle_map.get(ticker)
        if settle and filled > 0 and entry > 0:
            mr = (settle.get("market_result") or "").lower()
            settle_cents = 100 if side == mr else 0
            gross = (settle_cents - entry) * filled
            fee_total = float(settle.get("fee_cents", 0.0))
            cur.execute(
                "SELECT COALESCE(SUM(filled_count),0) FROM kalshi_trades WHERE ticker=?",
                (ticker,),
            )
            tot = int(cur.fetchone()[0] or 0)
            fee_share = (fee_total * filled / tot
                         if tot > 0 and fee_total > 0 else 0.0)
            pnl = round((gross - fee_share) / 100.0, 2)
        else:
            pnl = t.get("pnl") or 0.0

        # Window snapshots for this ticker
        cur.execute("""
            SELECT t_offset_sec, btc_price_cents, kalshi_mid_cents
            FROM window_snapshots
            WHERE ticker=?
            ORDER BY t_offset_sec ASC
        """, (ticker,))
        snaps = cur.fetchall()

        if len(snaps) < 2:
            print(f"{t['placed_at'][:19]:<19} {ticker[-12:]:<12} {side:<4} "
                  f"{entry:>5} {'no_snaps':>11} {'-':>9} {'-':>9} "
                  f"{pnl:>+7.2f}  insufficient window_snapshots")
            continue

        opens_btc = snaps[0]["btc_price_cents"]
        opens_mid = snaps[0]["kalshi_mid_cents"]

        # BTC crossed open?
        early_btc = snaps[min(2, len(snaps)-1)]["btc_price_cents"]
        init_up_btc = early_btc > opens_btc
        init_down_btc = early_btc < opens_btc
        if not (init_up_btc or init_down_btc):
            btc_crossed = "flat_btc"
        elif init_up_btc:
            later_below = any(s["btc_price_cents"] < opens_btc for s in snaps[2:])
            btc_crossed = "YES" if later_below else "no"
        else:
            later_above = any(s["btc_price_cents"] > opens_btc for s in snaps[2:])
            btc_crossed = "YES" if later_above else "no"

        # Post-entry max favorable profit + dwell
        post_entry_snaps = [s for s in snaps
                             if s["t_offset_sec"] is not None
                             and (s["t_offset_sec"] * 1000
                                  + _to_ts_ms("2026-04-23T00:00:00+00:00"))
                             > 0]
        # Simpler: use all snaps after min offset (entry happened during window)
        # For this analysis just compute over full window — entry time within window is approximate.
        profits = [_favorable_profit_c(side, s["kalshi_mid_cents"], entry) for s in snaps]
        max_fav = max(profits)
        # Dwell count: snapshots within 2c of max_fav
        dwell_snaps = sum(1 for p in profits if p >= max_fav - 2)
        # Rough minutes at peak (each snap ≈ 1 min cadence)
        dwell_min = dwell_snaps

        if btc_crossed != "YES":
            assess = "btc_did_not_cross"
            summary["btc_did_not_cross"] += 1
        elif max_fav <= 0:
            assess = "crossed_but_never_favorable"
            summary["btc_crossed_fav_never_positive"] += 1
        elif dwell_snaps < 2:
            assess = "brief_peak_only (1 snap)"
            summary["btc_crossed_brief_peak_only"] += 1
        else:
            assess = f"sustained_peak ({dwell_snaps} snaps)"
            summary["btc_crossed_sustained_peak"] += 1

        print(f"{t['placed_at'][:19]:<19} {ticker[-12:]:<12} {side:<4} "
              f"{entry:>5} {btc_crossed:>11} {max_fav:>+8}c "
              f"{dwell_min:>9} {pnl:>+7.2f}  {assess}")

    print()
    print("-- Summary --")
    total = sum(summary.values())
    for k, v in summary.items():
        pct = (100.0 * v / total) if total else 0
        print(f"  {k:<35} {v:>3} ({pct:>5.1f}%)")
    print()
    print("Interpretation:")
    print("  sustained_peak  = BTC crossed AND contract gave >=2 min of profit zone")
    print("                    → alpha is harvestable with maker-only")
    print("  brief_peak_only = BTC crossed but contract gave <2 min at peak")
    print("                    → alpha real but gap-fill, taker-only would work")
    print("  crossed_but_never_favorable = BTC technically crossed but the")
    print("                    contract was already priced correctly")
    print("  btc_did_not_cross = trend continued, fade thesis failed entirely")

    return 0


if __name__ == "__main__":
    sys.exit(main())
