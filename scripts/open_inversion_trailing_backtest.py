"""Backtest session-open price/volume inversion with trailing TP.

Hypothesis:
  - The first N minutes define the session-open condition.
  - If BTC price action later inverts that condition with elevated volume,
    buy the newly favored Kalshi side.
  - Manage with a trailing take-profit on executable same-side bid.

Data:
  - Kalshi 1-minute market candles from data/kalshi_external_backtest.db
    created by scripts/kalshi_minute11_dom_backtest.py.
  - Binance BTCUSDT 1-minute candles cached in the same DB by
    scripts/analyze_minute11_dom_failures.py.

This is intentionally conservative:
  - Entry uses the Kalshi ask for the chosen side.
  - Exit uses same-side bid.
  - No fill queue advantage is assumed.
  - 1-minute candles are coarse, so trailing exits are approximate.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kalshi_external_backtest.db"


@dataclass(frozen=True)
class Trade:
    ticker: str
    entry_minute: int
    side: str
    entry_c: int
    exit_c: int
    exit_reason: str
    result: str
    pnl_c: int
    max_profit_c: int
    open_dir: str
    signal_move_usd: float
    vol_ratio: float


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def _btc(conn: sqlite3.Connection, ts: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM btc_1m WHERE open_ts=?",
        (ts - ts % 60,),
    ).fetchone()


def _kalshi_candle(conn: sqlite3.Connection, ticker: str, target_ts: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM candles
        WHERE ticker=?
          AND end_period_ts BETWEEN ? AND ?
        ORDER BY ABS(end_period_ts - ?), end_period_ts DESC
        LIMIT 1
        """,
        (ticker, target_ts - 45, target_ts + 45, target_ts),
    ).fetchone()


def _cents(v: object) -> int | None:
    if v is None:
        return None
    try:
        return max(0, min(100, round(float(v) * 100)))
    except (TypeError, ValueError):
        return None


def _ask_bid_for(candle: sqlite3.Row, side: str) -> tuple[int, int] | None:
    yes_bid = _cents(candle["yes_bid_close"])
    yes_ask = _cents(candle["yes_ask_close"])
    if yes_bid is None or yes_ask is None or yes_ask < yes_bid:
        return None
    if side == "yes":
        return yes_ask, yes_bid
    return 100 - yes_bid, 100 - yes_ask


def _settle_exit(result: str, side: str) -> int:
    return 100 if result == side else 0


def _score_exit(
    conn: sqlite3.Connection,
    ticker: str,
    side: str,
    result: str,
    entry_c: int,
    entry_minute: int,
    arm_c: int,
    giveback_c: int,
    max_hold_min: int,
) -> tuple[int, str, int]:
    peak_profit = -entry_c
    armed = False
    for minute in range(entry_minute + 1, min(15, entry_minute + max_hold_min + 1)):
        m = conn.execute("SELECT open_ts FROM markets WHERE ticker=?", (ticker,)).fetchone()
        if not m:
            break
        c = _kalshi_candle(conn, ticker, int(m["open_ts"]) + minute * 60)
        if c is None:
            continue
        prices = _ask_bid_for(c, side)
        if prices is None:
            continue
        _ask, bid = prices
        profit = bid - entry_c
        if profit > peak_profit:
            peak_profit = profit
        if not armed and profit >= arm_c:
            armed = True
        if armed and peak_profit - profit >= giveback_c:
            return bid, "trail", peak_profit
    settle = _settle_exit(result, side)
    return settle, "settlement", max(peak_profit, settle - entry_c)


def run(
    days: int,
    open_minutes: int,
    min_open_move_usd: float,
    min_signal_move_usd: float,
    vol_mult: float,
    min_entry_minute: int,
    max_entry_minute: int,
    min_entry_c: int,
    max_entry_c: int,
    arm_c: int,
    giveback_c: int,
    max_hold_min: int,
) -> list[Trade]:
    conn = _conn()
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - days * 86400
    trades: list[Trade] = []
    for m in conn.execute(
        """
        SELECT ticker, open_ts, close_ts, result, raw_json
        FROM markets
        WHERE close_ts BETWEEN ? AND ?
          AND result IN ('yes', 'no')
        ORDER BY close_ts
        """,
        (start_ts, end_ts),
    ):
        open_ts = int(m["open_ts"])
        b0 = _btc(conn, open_ts)
        b_open_end = _btc(conn, open_ts + open_minutes * 60)
        if b0 is None or b_open_end is None:
            continue
        open_start = float(b0["open"])
        open_end = float(b_open_end["close"])
        open_move = open_end - open_start
        if abs(open_move) < min_open_move_usd:
            continue
        open_dir = "up" if open_move > 0 else "down"
        open_vols = []
        for minute in range(open_minutes + 1):
            b = _btc(conn, open_ts + minute * 60)
            if b is not None:
                open_vols.append(float(b["volume"]))
        if not open_vols:
            continue
        open_vol = median(open_vols)

        signal = None
        for minute in range(min_entry_minute, max_entry_minute + 1):
            b_prev = _btc(conn, open_ts + (minute - 1) * 60)
            b = _btc(conn, open_ts + minute * 60)
            if b_prev is None or b is None:
                continue
            px = float(b["close"])
            prev_px = float(b_prev["close"])
            move_from_open_end = px - open_end
            one_min_move = px - prev_px
            inverted = (
                open_dir == "up"
                and move_from_open_end <= -min_signal_move_usd
                and one_min_move < 0
            ) or (
                open_dir == "down"
                and move_from_open_end >= min_signal_move_usd
                and one_min_move > 0
            )
            vol_ratio = float(b["volume"]) / max(open_vol, 1e-9)
            if inverted and vol_ratio >= vol_mult:
                side = "no" if open_dir == "up" else "yes"
                c = _kalshi_candle(conn, str(m["ticker"]), open_ts + minute * 60)
                if c is None:
                    continue
                prices = _ask_bid_for(c, side)
                if prices is None:
                    continue
                ask, _bid = prices
                if min_entry_c <= ask <= max_entry_c:
                    signal = (minute, side, ask, move_from_open_end, vol_ratio)
                    break
        if signal is None:
            continue
        minute, side, entry_c, signal_move, vol_ratio = signal
        exit_c, reason, max_profit = _score_exit(
            conn, str(m["ticker"]), side, str(m["result"]).lower(),
            entry_c, minute, arm_c, giveback_c, max_hold_min,
        )
        trades.append(Trade(
            ticker=str(m["ticker"]),
            entry_minute=minute,
            side=side,
            entry_c=entry_c,
            exit_c=exit_c,
            exit_reason=reason,
            result=str(m["result"]).lower(),
            pnl_c=exit_c - entry_c,
            max_profit_c=max_profit,
            open_dir=open_dir,
            signal_move_usd=signal_move,
            vol_ratio=vol_ratio,
        ))
    conn.close()
    return trades


def summarize(trades: list[Trade]) -> None:
    print(f"trades={len(trades)}")
    if not trades:
        return
    wins = sum(1 for t in trades if t.pnl_c > 0)
    pnl = sum(t.pnl_c for t in trades)
    print(f"WR={wins}/{len(trades)} {wins/len(trades)*100:.1f}%")
    print(f"fixed100 PnL=${pnl:+.2f} avg=${pnl/len(trades):+.2f}/trade")
    print(f"trail exits={sum(t.exit_reason == 'trail' for t in trades)} settlement={sum(t.exit_reason == 'settlement' for t in trades)}")
    for reason in sorted({t.exit_reason for t in trades}):
        a = [t for t in trades if t.exit_reason == reason]
        print(
            f"  {reason}: n={len(a)} WR={sum(t.pnl_c>0 for t in a)/len(a)*100:.1f}% "
            f"pnl=${sum(t.pnl_c for t in a):+.2f} avg=${sum(t.pnl_c for t in a)/len(a):+.2f}"
        )
    print("\nRecent trades:")
    for t in trades[-12:]:
        print(
            f"  {t.ticker[-18:]:<18} m{t.entry_minute:02d} "
            f"{t.side.upper()}@{t.entry_c:>2}->{t.exit_c:>3} "
            f"{t.exit_reason:<10} pnl={t.pnl_c:+4d}c "
            f"max={t.max_profit_c:+3d}c vol={t.vol_ratio:.1f}x "
            f"open={t.open_dir}"
        )


def search(args: argparse.Namespace) -> None:
    rows = []
    for open_m in (1, 2, 3):
        for min_open in (10, 20, 30, 40):
            for sig_move in (5, 10, 15, 20, 30):
                for vol_mult in (0.8, 1.0, 1.25, 1.5, 2.0):
                    for arm in (3, 4, 5, 6):
                        trades = run(
                            args.days, open_m, min_open, sig_move, vol_mult,
                            args.min_entry_minute, args.max_entry_minute,
                            args.min_entry_c, args.max_entry_c,
                            arm, args.giveback_c, args.max_hold_min,
                        )
                        if len(trades) < args.min_trades:
                            continue
                        pnl = sum(t.pnl_c for t in trades)
                        wr = sum(t.pnl_c > 0 for t in trades) / len(trades) * 100
                        rows.append((pnl / len(trades), pnl, wr, len(trades), open_m, min_open, sig_move, vol_mult, arm))
    rows.sort(reverse=True)
    print("Open-inversion trailing search")
    print("PnL is fixed 100ct dollars; entry=ask, exit=bid/settle, 1m candle approximation.")
    print(f"{'rank':>4} {'n':>4} {'WR':>6} {'pnl':>8} {'avg':>7} {'openM':>5} {'open$':>6} {'sig$':>5} {'volx':>5} {'arm':>4}")
    for i, (avg, pnl, wr, n, open_m, min_open, sig_move, vol_mult, arm) in enumerate(rows[:25], 1):
        print(f"{i:>4} {n:>4} {wr:>5.1f}% {pnl:>+8.2f} {avg:>+7.2f} {open_m:>5} {min_open:>6.0f} {sig_move:>5.0f} {vol_mult:>5.2f} {arm:>4}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--open-minutes", type=int, default=2)
    ap.add_argument("--min-open-move-usd", type=float, default=20)
    ap.add_argument("--min-signal-move-usd", type=float, default=10)
    ap.add_argument("--vol-mult", type=float, default=1.25)
    ap.add_argument("--min-entry-minute", type=int, default=3)
    ap.add_argument("--max-entry-minute", type=int, default=12)
    ap.add_argument("--min-entry-c", type=int, default=20)
    ap.add_argument("--max-entry-c", type=int, default=80)
    ap.add_argument("--arm-c", type=int, default=4)
    ap.add_argument("--giveback-c", type=int, default=3)
    ap.add_argument("--max-hold-min", type=int, default=6)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--min-trades", type=int, default=50)
    args = ap.parse_args()
    if args.search:
        search(args)
    else:
        trades = run(
            args.days, args.open_minutes, args.min_open_move_usd,
            args.min_signal_move_usd, args.vol_mult,
            args.min_entry_minute, args.max_entry_minute,
            args.min_entry_c, args.max_entry_c,
            args.arm_c, args.giveback_c, args.max_hold_min,
        )
        summarize(trades)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
