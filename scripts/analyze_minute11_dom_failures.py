"""Analyze minute-11 dominant-side failures.

Uses the external Kalshi candle cache created by
``scripts/kalshi_minute11_dom_backtest.py`` and joins it with Binance BTCUSDT
1-minute candles. The goal is to understand the rare cases where the minute-11
dominant side still loses, and whether a contrarian filter/entry is plausible.

Example:
  python scripts/analyze_minute11_dom_failures.py --days 30 --minute 11 --min-dom 90
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kalshi_external_backtest.db"
BINANCE = "https://api.binance.us/api/v3/klines"


@dataclass(frozen=True)
class Point:
    ticker: str
    open_ts: int
    close_ts: int
    result: str
    side: str
    entry_c: int
    dom_price: int
    yes_bid: int
    yes_ask: int
    spread: int
    won: bool


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS btc_1m (
            open_ts INTEGER PRIMARY KEY,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL
        )
    """)
    return conn


def _cents(v: object) -> int | None:
    if v is None:
        return None
    try:
        return max(0, min(100, round(float(v) * 100)))
    except (TypeError, ValueError):
        return None


def _fetch_binance_chunk(start_ts: int, end_ts: int) -> list[list]:
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": start_ts * 1000,
        "endTime": end_ts * 1000,
        "limit": 1000,
    }
    url = BINANCE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode())


def ensure_btc(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> None:
    cur = start_ts - (start_ts % 60)
    end = end_ts - (end_ts % 60)
    while cur <= end:
        chunk_end = min(end, cur + 999 * 60)
        have = conn.execute(
            "SELECT COUNT(*) FROM btc_1m WHERE open_ts BETWEEN ? AND ?",
            (cur, chunk_end),
        ).fetchone()[0]
        expected = int((chunk_end - cur) / 60) + 1
        if have < expected * 0.95:
            rows = _fetch_binance_chunk(cur, chunk_end + 59)
            for r in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO btc_1m
                        (open_ts, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(r[0]) // 1000,
                        float(r[1]),
                        float(r[2]),
                        float(r[3]),
                        float(r[4]),
                        float(r[5]),
                    ),
                )
            conn.commit()
            time.sleep(0.08)
        cur = chunk_end + 60


def _btc_close(conn: sqlite3.Connection, ts: int) -> float | None:
    ts = ts - (ts % 60)
    r = conn.execute(
        "SELECT close FROM btc_1m WHERE open_ts=?",
        (ts,),
    ).fetchone()
    return float(r["close"]) if r else None


def _btc_open(conn: sqlite3.Connection, ts: int) -> float | None:
    ts = ts - (ts % 60)
    r = conn.execute(
        "SELECT open FROM btc_1m WHERE open_ts=?",
        (ts,),
    ).fetchone()
    return float(r["open"]) if r else None


def _range(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> tuple[float, float] | None:
    r = conn.execute(
        "SELECT MIN(low) AS lo, MAX(high) AS hi FROM btc_1m WHERE open_ts BETWEEN ? AND ?",
        (start_ts - (start_ts % 60), end_ts - (end_ts % 60)),
    ).fetchone()
    if r is None or r["lo"] is None or r["hi"] is None:
        return None
    return float(r["lo"]), float(r["hi"])


def load_points(conn: sqlite3.Connection, days: int, minute: int, min_dom: int,
                min_entry: int, max_entry: int) -> list[Point]:
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - days * 86400
    points: list[Point] = []
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
        target = int(m["open_ts"]) + minute * 60
        c = conn.execute(
            """
            SELECT *
            FROM candles
            WHERE ticker=?
              AND end_period_ts BETWEEN ? AND ?
            ORDER BY ABS(end_period_ts - ?), end_period_ts DESC
            LIMIT 1
            """,
            (m["ticker"], target - 45, target + 45, target),
        ).fetchone()
        if c is None:
            continue
        yes_bid = _cents(c["yes_bid_close"])
        yes_ask = _cents(c["yes_ask_close"])
        if yes_bid is None or yes_ask is None or yes_ask < yes_bid:
            continue
        yes_mid = round((yes_bid + yes_ask) / 2)
        side = "yes" if yes_mid >= 50 else "no"
        dom_price = yes_mid if side == "yes" else 100 - yes_mid
        entry_c = yes_ask if side == "yes" else 100 - yes_bid
        if dom_price < min_dom or entry_c < min_entry or entry_c > max_entry:
            continue
        result = str(m["result"]).lower()
        points.append(Point(
            ticker=m["ticker"],
            open_ts=int(m["open_ts"]),
            close_ts=int(m["close_ts"]),
            result=result,
            side=side,
            entry_c=entry_c,
            dom_price=dom_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            spread=yes_ask - yes_bid,
            won=result == side,
        ))
    return points


def features(conn: sqlite3.Connection, p: Point, minute: int) -> dict[str, float] | None:
    op = _btc_open(conn, p.open_ts)
    m6 = _btc_close(conn, p.open_ts + 6 * 60)
    m8 = _btc_close(conn, p.open_ts + 8 * 60)
    m10 = _btc_close(conn, p.open_ts + 10 * 60)
    m11 = _btc_close(conn, p.open_ts + minute * 60)
    m12 = _btc_close(conn, p.open_ts + 12 * 60)
    m14 = _btc_close(conn, p.open_ts + 14 * 60)
    final = _btc_close(conn, p.close_ts - 60)
    rng = _range(conn, p.open_ts, p.open_ts + minute * 60)
    if None in (op, m6, m8, m10, m11, m12, m14, final) or rng is None:
        return None
    sign = 1.0 if p.side == "yes" else -1.0
    impulse = sign * (m11 - op)
    pre5 = sign * (m11 - m6)
    pre3 = sign * (m11 - m8)
    pre1 = sign * (m11 - m10)
    next1 = sign * (m12 - m11)
    late3 = sign * (m14 - m11)
    finish = sign * (final - m11)
    lo, hi = rng
    rng_w = hi - lo
    if p.side == "yes":
        excursion = (m11 - lo) / rng_w if rng_w > 0 else 0.5
    else:
        excursion = (hi - m11) / rng_w if rng_w > 0 else 0.5
    return {
        "impulse": impulse,
        "pre5": pre5,
        "pre3": pre3,
        "pre1": pre1,
        "next1": next1,
        "late3": late3,
        "finish": finish,
        "range": rng_w,
        "excursion": excursion,
        "entry_c": float(p.entry_c),
        "dom_price": float(p.dom_price),
        "spread": float(p.spread),
    }


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, round((len(s) - 1) * q)))
    return s[idx]


def summarize(name: str, rows: list[dict[str, float]]) -> None:
    print(f"\n{name}: n={len(rows)}")
    if not rows:
        return
    keys = ["entry_c", "dom_price", "spread", "impulse", "pre5", "pre3", "pre1", "next1", "late3", "finish", "range", "excursion"]
    print(f"{'feature':<10} {'med':>9} {'p10':>9} {'p90':>9}")
    for k in keys:
        vals = [r[k] for r in rows]
        print(f"{k:<10} {median(vals):>9.2f} {pct(vals,0.10):>9.2f} {pct(vals,0.90):>9.2f}")


def print_rules(rows: list[tuple[Point, dict[str, float]]]) -> None:
    print("\nSimple contrarian-veto candidates on dominant>=90 set")
    print("Rule blocks dominant, flips only if you are willing to buy the loser-side lottery.")
    candidates = [
        ("entry<=90", lambda f: f["entry_c"] <= 90),
        ("entry<=95", lambda f: f["entry_c"] <= 95),
        ("spread>=2", lambda f: f["spread"] >= 2),
        ("pre1<=0", lambda f: f["pre1"] <= 0),
        ("next1<=0", lambda f: f["next1"] <= 0),
        ("excursion>=0.95", lambda f: f["excursion"] >= 0.95),
        ("range>=120", lambda f: f["range"] >= 120),
        ("pre1<=0 and excursion>=0.90", lambda f: f["pre1"] <= 0 and f["excursion"] >= 0.90),
        ("spread>=2 and entry<=90", lambda f: f["spread"] >= 2 and f["entry_c"] <= 90),
    ]
    print(f"{'rule':<32} {'blocked':>7} {'loser_hit':>9} {'blocked_WR':>10} {'contrarian_EV':>13}")
    for label, fn in candidates:
        blocked = [(p, f) for p, f in rows if fn(f)]
        if not blocked:
            continue
        loser_hit = sum(1 for p, _f in blocked if not p.won)
        dominant_wins = len(blocked) - loser_hit
        # If flipped contrarian, win when original dominant loses.
        pnl = 0.0
        for p, _f in blocked:
            contra_entry = 100 - p.entry_c
            pnl += (100 - contra_entry) if not p.won else -contra_entry
        print(
            f"{label:<32} {len(blocked):>7} {loser_hit:>9} "
            f"{dominant_wins/len(blocked)*100:>9.1f}% {pnl/len(blocked):>+12.2f}"
        )


def _contra_entry_at(conn: sqlite3.Connection, p: Point, ts: int) -> int | None:
    c = conn.execute(
        """
        SELECT yes_bid_close, yes_ask_close
        FROM candles
        WHERE ticker=?
          AND end_period_ts BETWEEN ? AND ?
        ORDER BY ABS(end_period_ts - ?), end_period_ts DESC
        LIMIT 1
        """,
        (p.ticker, ts - 45, ts + 45, ts),
    ).fetchone()
    if c is None:
        return None
    yes_bid = _cents(c["yes_bid_close"])
    yes_ask = _cents(c["yes_ask_close"])
    if yes_bid is None or yes_ask is None or yes_ask < yes_bid:
        return None
    contra_side = "no" if p.side == "yes" else "yes"
    return yes_ask if contra_side == "yes" else 100 - yes_bid


def print_delayed_flips(
    conn: sqlite3.Connection,
    rows: list[tuple[Point, dict[str, float]]],
    minute: int,
) -> None:
    print("\nDelayed contrarian flip test")
    print("Trigger: minute-11 dominant>=filter, then BTC moves against that side by threshold during next minute.")
    print("Entry: buy opposite side at minute-12 Kalshi ask, hold to settlement.")
    print(f"{'next1 trigger':<14} {'n':>5} {'WR':>6} {'fixed100':>9} {'$/tr':>7} {'avg_entry':>9}")
    for threshold in (-20, -30, -40, -50, -60, -75, -100):
        selected: list[tuple[Point, int]] = []
        for p, f in rows:
            if f["next1"] > threshold:
                continue
            entry = _contra_entry_at(conn, p, p.open_ts + (minute + 1) * 60)
            if entry is None or entry <= 0 or entry >= 100:
                continue
            selected.append((p, entry))
        if not selected:
            continue
        wins = 0
        pnl = 0.0
        cost = 0.0
        for p, entry in selected:
            # Opposite side wins exactly when the original dominant side loses.
            won = not p.won
            wins += 1 if won else 0
            pnl += (100 - entry) if won else -entry
            cost += entry
        print(
            f"next1<={threshold:<5} {len(selected):>5} "
            f"{wins/len(selected)*100:>5.1f}% {pnl:>+9.2f} "
            f"{pnl/len(selected):>+7.2f} {cost/len(selected):>9.1f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--minute", type=int, default=11)
    ap.add_argument("--min-dom", type=int, default=90)
    ap.add_argument("--min-entry", type=int, default=1)
    ap.add_argument("--max-entry", type=int, default=99)
    args = ap.parse_args()

    conn = _db()
    points = load_points(conn, args.days, args.minute, args.min_dom, args.min_entry, args.max_entry)
    if not points:
        print("No matching points. Run kalshi_minute11_dom_backtest.py --refresh first.")
        return 1
    ensure_btc(conn, min(p.open_ts for p in points) - 600, max(p.close_ts for p in points) + 600)

    joined: list[tuple[Point, dict[str, float]]] = []
    for p in points:
        f = features(conn, p, args.minute)
        if f is not None:
            joined.append((p, f))
    winners = [f for p, f in joined if p.won]
    losers = [f for p, f in joined if not p.won]

    print(f"Minute-{args.minute} dominant failure analysis")
    print(f"Filter: dom>={args.min_dom}, entry={args.min_entry}-{args.max_entry}c, days={args.days}")
    print(f"Joined points: {len(joined)} | winners={len(winners)} losers={len(losers)}")
    summarize("Winners", winners)
    summarize("Losers", losers)

    print("\nLoser tape")
    for p, f in joined:
        if p.won:
            continue
        ts = datetime.fromtimestamp(p.close_ts, timezone.utc).strftime("%m-%d %H:%M")
        print(
            f"  {ts} {p.ticker:<25} dom={p.side.upper():<3}@{p.entry_c:>2} "
            f"settle={p.result.upper():<3} impulse=${f['impulse']:+.0f} "
            f"pre1=${f['pre1']:+.0f} next1=${f['next1']:+.0f} "
            f"finish=${f['finish']:+.0f} range=${f['range']:.0f} "
            f"spread={p.spread}"
        )

    print_rules(joined)
    print_delayed_flips(conn, joined, args.minute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
