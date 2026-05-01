"""Backtest late dominant-side strategies from Kalshi market candles.

This intentionally does not rely on local ``window_snapshots``. It pulls
KXBTC15M market metadata and 1-minute Kalshi candle data through the official
Kalshi API, caches the response in SQLite, and evaluates dominant-side entry
rules at a selected minute.

Entry model:
  - Dominance is measured by YES bid/ask midpoint at the minute checkpoint.
  - Buys use the executable ask for the chosen side:
      YES ask = candle yes_ask.close
      NO ask  = 1 - candle yes_bid.close
  - Hold to settlement.

Examples:
  python scripts/kalshi_minute11_dom_backtest.py --days 7 --refresh
  python scripts/kalshi_minute11_dom_backtest.py --days 14 --minute 11 --min-dom 65
  python scripts/kalshi_minute11_dom_backtest.py --days 30 --search
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CACHE_DB = ROOT / "data" / "kalshi_external_backtest.db"
ENV_PATH = ROOT / "credentials" / "kalshi.env"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiAPIError, KalshiClient, _parse_iso_to_ms  # noqa: E402


@dataclass(frozen=True)
class CandlePoint:
    ticker: str
    close_ts: int
    result: str
    yes_mid_c: int
    yes_bid_c: int
    yes_ask_c: int
    side: str
    entry_c: int
    won: bool


def _load_env() -> tuple[str, str]:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
    key_id = os.environ["KALSHI_API_KEY"]
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if pem_path:
        private_key = Path(pem_path).read_text()
    else:
        private_key = os.environ["KALSHI_PRIVATE_KEY"]
    return key_id, private_key


def _db() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY,
            open_ts INTEGER NOT NULL,
            close_ts INTEGER NOT NULL,
            result TEXT NOT NULL,
            status TEXT,
            raw_json TEXT,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            ticker TEXT NOT NULL,
            end_period_ts INTEGER NOT NULL,
            price_close REAL,
            yes_bid_close REAL,
            yes_ask_close REAL,
            volume_fp REAL,
            raw_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (ticker, end_period_ts)
        )
    """)
    return conn


def _dollars_to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(100, round(float(value) * 100)))
    except (TypeError, ValueError):
        return None


def _jsonish(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


async def _fetch_markets(
    client: KalshiClient,
    conn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    max_markets: int | None,
) -> list[str]:
    tickers: list[str] = []
    cursor = ""
    fetched_at = datetime.now(timezone.utc).isoformat()
    while True:
        params: dict[str, Any] = {
            "series_ticker": "KXBTC15M",
            "status": "settled",
            "min_close_ts": start_ts,
            "max_close_ts": end_ts,
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        data = await client._request("GET", "/markets", params=params)
        markets = data.get("markets", [])
        for m in markets:
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            open_ts_ms = _parse_iso_to_ms(m.get("open_time", ""))
            close_ts_ms = _parse_iso_to_ms(m.get("close_time", ""))
            if not open_ts_ms or not close_ts_ms:
                continue
            ticker = m["ticker"]
            conn.execute(
                """
                INSERT OR REPLACE INTO markets
                    (ticker, open_ts, close_ts, result, status, raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    open_ts_ms // 1000,
                    close_ts_ms // 1000,
                    result,
                    m.get("status", ""),
                    _jsonish(m),
                    fetched_at,
                ),
            )
            tickers.append(ticker)
            if max_markets and len(tickers) >= max_markets:
                conn.commit()
                return tickers
        conn.commit()
        cursor = data.get("cursor") or ""
        if not cursor or not markets:
            break
    return tickers


async def _fetch_candles_for(
    client: KalshiClient,
    conn: sqlite3.Connection,
    ticker: str,
    open_ts: int,
    close_ts: int,
    refresh: bool,
) -> bool:
    if not refresh:
        existing = conn.execute(
            "SELECT COUNT(*) FROM candles WHERE ticker=?",
            (ticker,),
        ).fetchone()[0]
        if existing:
            return True

    params = {
        "period_interval": 1,
        "start_ts": open_ts - 120,
        "end_ts": close_ts + 120,
    }
    try:
        data = await client._request(
            "GET",
            f"/series/KXBTC15M/markets/{ticker}/candlesticks",
            params=params,
        )
    except KalshiAPIError as exc:
        # Markets older than the live partition need the historical endpoint.
        if exc.status != 404:
            raise
        data = await client._request(
            "GET",
            f"/historical/markets/{ticker}/candlesticks",
            params=params,
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    for c in data.get("candlesticks", []):
        price = c.get("price") or {}
        yes_bid = c.get("yes_bid") or {}
        yes_ask = c.get("yes_ask") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO candles
                (ticker, end_period_ts, price_close, yes_bid_close, yes_ask_close,
                 volume_fp, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                int(c["end_period_ts"]),
                float(price.get("close_dollars", 0) or 0),
                float(yes_bid.get("close_dollars", 0) or 0),
                float(yes_ask.get("close_dollars", 0) or 0),
                float(c.get("volume_fp", 0) or 0),
                _jsonish(c),
                fetched_at,
            ),
        )
    conn.commit()
    return True


async def refresh_cache(days: int, max_markets: int | None, refresh: bool) -> None:
    key_id, private_key = _load_env()
    conn = _db()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    async with KalshiClient(key_id, private_key, demo=False) as client:
        tickers = await _fetch_markets(
            client,
            conn,
            int(start_dt.timestamp()),
            int(end_dt.timestamp()),
            max_markets,
        )
        rows = conn.execute(
            """
            SELECT ticker, open_ts, close_ts
            FROM markets
            WHERE close_ts BETWEEN ? AND ?
            ORDER BY close_ts
            """,
            (int(start_dt.timestamp()), int(end_dt.timestamp())),
        ).fetchall()
        if max_markets:
            rows = rows[-max_markets:]
        print(f"Fetched metadata for {len(tickers)} markets; caching candles for {len(rows)} markets")
        for i, r in enumerate(rows, 1):
            await _fetch_candles_for(
                client,
                conn,
                r["ticker"],
                int(r["open_ts"]),
                int(r["close_ts"]),
                refresh=refresh,
            )
            if i % 50 == 0:
                print(f"  candles cached: {i}/{len(rows)}")
            await asyncio.sleep(0.03)
    conn.close()


def _load_points(days: int, minute: int) -> list[CandlePoint]:
    conn = _db()
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - days * 86400
    points: list[CandlePoint] = []
    for m in conn.execute(
        """
        SELECT ticker, open_ts, close_ts, result
        FROM markets
        WHERE close_ts BETWEEN ? AND ?
          AND result IN ('yes', 'no')
        ORDER BY close_ts
        """,
        (start_ts, end_ts),
    ):
        target_ts = int(m["open_ts"]) + minute * 60
        c = conn.execute(
            """
            SELECT *
            FROM candles
            WHERE ticker=?
              AND end_period_ts BETWEEN ? AND ?
            ORDER BY ABS(end_period_ts - ?), end_period_ts DESC
            LIMIT 1
            """,
            (m["ticker"], target_ts - 45, target_ts + 45, target_ts),
        ).fetchone()
        if c is None:
            continue
        yes_bid = _dollars_to_cents(c["yes_bid_close"])
        yes_ask = _dollars_to_cents(c["yes_ask_close"])
        if yes_bid is None or yes_ask is None:
            continue
        if yes_bid <= 0 and yes_ask <= 0:
            continue
        if yes_ask < yes_bid:
            continue
        yes_mid = round((yes_bid + yes_ask) / 2)
        side = "yes" if yes_mid >= 50 else "no"
        entry_c = yes_ask if side == "yes" else 100 - yes_bid
        if entry_c <= 0 or entry_c >= 100:
            continue
        result = str(m["result"]).lower()
        points.append(CandlePoint(
            ticker=m["ticker"],
            close_ts=int(m["close_ts"]),
            result=result,
            yes_mid_c=yes_mid,
            yes_bid_c=yes_bid,
            yes_ask_c=yes_ask,
            side=side,
            entry_c=entry_c,
            won=(result == side),
        ))
    conn.close()
    return points


def evaluate(
    points: list[CandlePoint],
    min_dom: int,
    min_entry: int,
    max_entry: int,
    start_balance: float,
) -> dict[str, Any]:
    selected: list[CandlePoint] = []
    for p in points:
        dom_price = p.yes_mid_c if p.side == "yes" else 100 - p.yes_mid_c
        if dom_price < min_dom:
            continue
        if p.entry_c < min_entry or p.entry_c > max_entry:
            continue
        selected.append(p)

    fixed_pnl = sum((100 - p.entry_c) if p.won else -p.entry_c for p in selected)
    fixed_cost = sum(p.entry_c for p in selected)
    bal = start_balance
    min_bal = bal
    max_bal = bal
    full_trades = 0
    for p in selected:
        contracts = int((bal * 100) // p.entry_c)
        if contracts <= 0:
            break
        full_trades += 1
        bal = (bal - contracts * p.entry_c / 100.0) + (contracts if p.won else 0.0)
        min_bal = min(min_bal, bal)
        max_bal = max(max_bal, bal)
        if bal <= 0:
            break
    wins = sum(1 for p in selected if p.won)
    return {
        "n": len(selected),
        "wins": wins,
        "wr": wins / len(selected) * 100 if selected else 0.0,
        "fixed_pnl_100ct": fixed_pnl,
        "avg_pnl_100ct": fixed_pnl / len(selected) if selected else 0.0,
        "roi_cost": fixed_pnl / fixed_cost * 100 if fixed_cost else 0.0,
        "full_final": bal,
        "full_min": min_bal,
        "full_max": max_bal,
        "full_trades": full_trades,
        "selected": selected,
    }


def print_eval(points: list[CandlePoint], args: argparse.Namespace) -> None:
    res = evaluate(points, args.min_dom, args.min_entry, args.max_entry, args.start)
    print(f"\nKalshi external candle dominant-side backtest")
    print(f"days={args.days} minute={args.minute} min_dom={args.min_dom} entry={args.min_entry}-{args.max_entry}c")
    print(f"source_points={len(points)} trades={res['n']}")
    if not res["n"]:
        return
    print(
        f"WR={res['wins']}/{res['n']} {res['wr']:.1f}% | "
        f"fixed100=${res['fixed_pnl_100ct']:+.2f} "
        f"avg=${res['avg_pnl_100ct']:+.2f}/trade ROIcost={res['roi_cost']:+.1f}%"
    )
    print(
        f"full-port ${args.start:.2f} -> ${res['full_final']:.2f} "
        f"range ${res['full_min']:.2f}-${res['full_max']:.2f} "
        f"executed={res['full_trades']}/{res['n']}"
    )
    print("\nRecent selected trades:")
    for p in res["selected"][-12:]:
        ts = datetime.fromtimestamp(p.close_ts, timezone.utc).strftime("%m-%d %H:%M")
        print(
            f"  {ts} {p.ticker:<25} {p.side.upper():<3}@{p.entry_c:>2} "
            f"mid={p.yes_mid_c:>2} bid/ask={p.yes_bid_c:>2}/{p.yes_ask_c:<2} "
            f"settle={p.result.upper():<3} {'WIN' if p.won else 'LOSS'}"
        )


def print_search(points: list[CandlePoint], args: argparse.Namespace) -> None:
    rows = []
    for min_dom in (50, 55, 60, 65, 70, 75, 80, 85, 90):
        for min_entry, max_entry in (
            (1, 99), (35, 99), (45, 99), (50, 99), (60, 99),
            (1, 60), (35, 75), (45, 80), (55, 90),
        ):
            r = evaluate(points, min_dom, min_entry, max_entry, args.start)
            if r["n"] >= args.min_trades:
                rows.append((min_dom, min_entry, max_entry, r))
    rows.sort(key=lambda x: (x[3]["avg_pnl_100ct"], x[3]["n"]), reverse=True)
    print(f"\nMinute {args.minute} search on {len(points)} external Kalshi candle points")
    print(f"Requires >= {args.min_trades} trades; PnL is fixed 100ct dollars, entry uses ask.")
    print(f"{'rank':>4} {'dom>=':>5} {'entry':>9} {'n':>4} {'WR':>6} {'fixed100':>9} {'$/tr':>7} {'ROIcost':>8} {'fullPort':>10}")
    for i, (min_dom, min_entry, max_entry, r) in enumerate(rows[:25], 1):
        print(
            f"{i:>4} {min_dom:>5} {min_entry:>2}-{max_entry:<2} "
            f"{r['n']:>4} {r['wr']:>5.1f}% {r['fixed_pnl_100ct']:>+9.2f} "
            f"{r['avg_pnl_100ct']:>+7.2f} {r['roi_cost']:>+7.1f}% "
            f"${r['full_final']:>9.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--minute", type=int, default=11)
    ap.add_argument("--min-dom", type=int, default=65)
    ap.add_argument("--min-entry", type=int, default=1)
    ap.add_argument("--max-entry", type=int, default=99)
    ap.add_argument("--start", type=float, default=100.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--min-trades", type=int, default=20)
    args = ap.parse_args()

    if not args.no_fetch:
        asyncio.run(refresh_cache(args.days, args.max_markets, args.refresh))
    points = _load_points(args.days, args.minute)
    if args.search:
        print_search(points, args)
    else:
        print_eval(points, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
