"""Quick schema inspection of the kalshi_external_backtest.db.

We're looking for: per-ticker strike price + settlement outcome
(15-min BTC binary: YES wins if BTC settles above strike, NO wins below).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    for table in ("markets", "btc_1m", "candles"):
        print(f"=== {table} ===")
        try:
            cur.execute(f"PRAGMA table_info({table})")
            cols = cur.fetchall()
            for c in cols:
                print(f"  {c[1]:<25} {c[2]}")
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  rows: {cur.fetchone()[0]:,}")
            cur.execute(f"SELECT * FROM {table} LIMIT 3")
            for row in cur.fetchall():
                print(f"  sample: {row}")
        except Exception as e:
            print(f"  ?? {e}")
        print()
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
