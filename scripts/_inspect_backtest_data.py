"""Inspect what backtest data we have available.

Reports the size and shape of:
  - data/kalshi_external_backtest.db (Kalshi window snapshots + settlements)
  - data/trades.db (live engine trades + settlements)
  - data/signals.db (signal log)
  - data/btc_1m_90d.csv (BTC OHLCV)

So we can answer "what backtest data do we have and what does it cover?"
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def hms(epoch_ms: float | int) -> str:
    if not epoch_ms:
        return "-"
    try:
        ms = int(epoch_ms)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return f"<bad {epoch_ms}>"


def inspect_db(path: Path) -> None:
    if not path.exists():
        print(f"  (missing: {path})")
        return
    print(f"  size: {path.stat().st_size / 1024 / 1024:,.1f} MB")
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    if not tables:
        print("  (no tables)")
        con.close()
        return
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            n = cur.fetchone()[0]
        except Exception as e:
            print(f"    {t:<40} ?? ({e})")
            continue
        print(f"    {t:<40} rows={n:,}")
        # Show date range if there's a sensible time column
        for col_candidate in ("ts_ms", "fill_ts_ms", "settle_ts_ms",
                              "settled_ts_ms", "snapshot_ts_ms",
                              "open_ts_ms", "close_ts_ms"):
            try:
                cur.execute(f"PRAGMA table_info({t})")
                cols = {row[1] for row in cur.fetchall()}
                if col_candidate not in cols:
                    continue
                cur.execute(
                    f"SELECT MIN({col_candidate}), MAX({col_candidate}) "
                    f"FROM {t} WHERE {col_candidate} IS NOT NULL"
                )
                lo, hi = cur.fetchone()
                if lo and hi:
                    print(f"      {col_candidate}: {hms(lo)}  →  {hms(hi)}")
                    break
            except Exception:
                pass
    con.close()


def inspect_csv(path: Path) -> None:
    if not path.exists():
        print(f"  (missing: {path})")
        return
    print(f"  size: {path.stat().st_size / 1024 / 1024:,.1f} MB")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first = f.readline().strip()
        print(f"  header: {first}")
        # Count lines + sample first/last data row
        n = 0
        last_line = ""
        for line in f:
            n += 1
            last_line = line.strip()
        print(f"  data rows: {n:,}")
        if last_line:
            print(f"  last row: {last_line}")


def main() -> int:
    print("=" * 72)
    print("BACKTEST DATA INVENTORY")
    print("=" * 72)
    print()
    print("data/kalshi_external_backtest.db  (Kalshi window snapshots)")
    inspect_db(REPO / "data" / "kalshi_external_backtest.db")
    print()
    print("data/trades.db  (live engine trades + settlements)")
    inspect_db(REPO / "data" / "trades.db")
    print()
    print("data/signals.db  (signal log)")
    inspect_db(REPO / "data" / "signals.db")
    print()
    print("data/btc_1m_90d.csv  (BTC OHLCV)")
    inspect_csv(REPO / "data" / "btc_1m_90d.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
