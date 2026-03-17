"""Print a readable trade ledger from the local databases."""
import sqlite3, os
from datetime import datetime, timezone

def show_trades():
    db = os.path.join(os.path.dirname(__file__), "data", "trades.db")
    if not os.path.exists(db):
        print("No trades database found yet.")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    trades = conn.execute(
        "SELECT * FROM kalshi_trades ORDER BY placed_at DESC LIMIT 100"
    ).fetchall()

    if not trades:
        print("No trades recorded yet.")
        return

    wins   = sum(1 for t in trades if t['status'] == 'won')
    losses = sum(1 for t in trades if t['status'] == 'lost')
    settled = wins + losses
    total_pnl = sum(t['pnl'] or 0.0 for t in trades)

    print(f"{'Time (UTC)':<22} {'Ticker':<35} {'Side':<5} {'Qty':>4} {'Price':>6} {'Risk':>7} {'Status':<10} {'P&L':>8}  {'Order ID'}")
    print("-" * 125)
    for t in trades:
        pnl_str = f"${t['pnl']:+.2f}" if t['pnl'] is not None else "  --  "
        print(
            f"{t['placed_at']:<22} "
            f"{t['ticker']:<35} "
            f"{t['side'].upper():<5} "
            f"{t['count']:>4} "
            f"{t['limit_price']:>5}c "
            f"${t['dollar_risk']:>6.2f} "
            f"{t['status']:<10} "
            f"{pnl_str:>8}  "
            f"{t['order_id']}"
        )

    print()
    print(f"Total trades shown: {len(trades)}  |  Settled: {settled}  |  W/L: {wins}/{losses}  |  P&L: ${total_pnl:+.2f}")
    conn.close()

def show_signals():
    db = os.path.join(os.path.dirname(__file__), "data", "signals.db")
    if not os.path.exists(db):
        print("No signals database found.")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 20"
    ).fetchall()

    print(f"\nLast 20 signals:")
    print(f"{'Time (UTC)':<22} {'Dir':<5} {'Conf':>6} {'Align':<8} {'Regime'}")
    print("-" * 60)
    for r in rows:
        ts = datetime.fromtimestamp(r['timestamp'] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{ts:<22} "
            f"{r['direction']:<5} "
            f"{r['confidence']:>5.1f}% "
            f"{r['aligned_count']}/{r['total_tfs']}     "
            f"{'N/A'}"
        )
    conn.close()

if __name__ == "__main__":
    show_trades()
    show_signals()
