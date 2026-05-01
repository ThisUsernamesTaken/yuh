"""Print ATM Reversion paper-trading status.

Read-only helper for the operator:

    python scripts/atm_paper_status.py
    python scripts/atm_paper_status.py --watch 30
    python scripts/atm_paper_status.py --limit 20

Shows:
  - ATM paper PnL and win rate
  - recent paper trades
  - exit reason breakdown
  - maker-entry shadow statistics
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trades.db"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fmt_usd(cents: float | int | None) -> str:
    if cents is None:
        return "$0.00"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


def _fmt_c(cents: float | int | None) -> str:
    if cents is None:
        return "0.00c"
    return f"{cents:+.2f}c"


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _print_rows(rows: list[tuple], headers: list[str]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
    fmt = "  " + "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(x) for x in row]))


def _paper_where(*, clean: bool) -> str:
    base = "COALESCE(entry_c, 0) > 0"
    if not clean:
        return base
    return (
        base
        + " AND COALESCE(entry_yes_bid, 0) > 0"
        + " AND COALESCE(entry_yes_ask, 0) > 0"
        + " AND entry_yes_bid < entry_yes_ask"
    )


def _shadow_where(*, clean: bool) -> str:
    if not clean:
        return "1=1"
    return (
        "COALESCE(signal_side_bid, 0) > 0"
        " AND COALESCE(signal_side_ask, 0) > 0"
        " AND signal_side_bid < signal_side_ask"
    )


def _load_paper_summary(cur: sqlite3.Cursor, *, clean: bool) -> tuple:
    cur.execute(
        f"""
        SELECT COUNT(*),
               SUM(CASE WHEN net_cents > 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(net_cents), 0),
               COALESCE(SUM(gross_cents), 0),
               COALESCE(AVG(net_cents * 1.0 / NULLIF(contracts, 0)), 0),
               COALESCE(AVG(gross_cents * 1.0 / NULLIF(contracts, 0)), 0),
               COALESCE(SUM(contracts), 0)
        FROM atm_reversion_paper_trades
        WHERE {_paper_where(clean=clean)}
        """
    )
    return cur.fetchone()


def _print_paper_summary(label: str, summary: tuple) -> None:
    n, wins, net, gross, avg_net_c, avg_gross_c, total_contracts = summary
    wins = wins or 0
    wr = (wins / n * 100.0) if n else 0.0
    print(
        f"  {label}: trades={n}  wins={wins}  WR={wr:.1f}%  "
        f"net={_fmt_usd(net)}  gross={_fmt_usd(gross)}"
    )
    print(
        f"  {label}: avg_net={_fmt_c(avg_net_c)}/contract  "
        f"avg_gross={_fmt_c(avg_gross_c)}/contract  "
        f"contracts={total_contracts}"
    )


async def _load_live_btc_positions() -> tuple[str, list[dict]]:
    """Return live Kalshi balance text + non-flat KXBTC15M positions.

    This is intentionally opt-in because it uses live credentials and hits
    Kalshi REST. It does not place or cancel orders.
    """
    env_file = ROOT / "credentials" / "kalshi.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

    key_id = os.environ.get("KALSHI_API_KEY", "")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_id or not pem_path or not Path(pem_path).exists():
        return ("credentials unavailable", [])

    from kalshi_client import KalshiClient

    pem = Path(pem_path).read_text(encoding="utf-8")
    demo = os.environ.get("KALSHI_DEMO", "false").lower() == "true"
    async with KalshiClient(key_id, pem, demo=demo) as client:
        bal = await client.get_balance()
        positions = await client.get_positions()

    open_btc = []
    for p in positions:
        ticker = str(p.get("ticker") or p.get("market_ticker") or "")
        if not ticker.startswith("KXBTC15M"):
            continue
        try:
            qty = float(p.get("position_fp") or p.get("position") or 0)
        except Exception:
            qty = 0.0
        resting = int(p.get("resting_orders_count") or 0)
        if abs(qty) > 0.0001 or resting > 0:
            open_btc.append(p)

    balance_text = (
        f"cash={_fmt_usd(int(bal.balance))} "
        f"portfolio={_fmt_usd(int(getattr(bal, 'portfolio_value', 0) or 0))}"
    )
    return (balance_text, open_btc)


def render(limit: int, *, live_check: bool = False) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("=" * 78)
    print(f"ATM Reversion Paper Status | {now}")
    print(f"DB: {DB}")
    if not DB.exists():
        print("trades.db not found yet.")
        return

    if live_check:
        print("\nLive Kalshi BTC Position Check")
        try:
            balance_text, open_btc = asyncio.run(_load_live_btc_positions())
            print(f"  {balance_text}")
            if not open_btc:
                print("  no open KXBTC15M positions or resting KXBTC15M orders")
            else:
                rows = []
                for p in open_btc:
                    rows.append((
                        str(p.get("ticker", ""))[-18:],
                        p.get("position_fp", p.get("position", "")),
                        p.get("market_exposure_dollars", ""),
                        p.get("resting_orders_count", 0),
                        p.get("last_updated_ts", ""),
                    ))
                _print_rows(rows, ["ticker", "position", "exposure", "resting", "updated"])
        except Exception as e:
            print(f"  live check failed: {e}")

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=15000")
    cur = conn.cursor()

    print("\nPaper Trades")
    if not _table_exists(cur, "atm_reversion_paper_trades"):
        print("  atm_reversion_paper_trades has not been created yet.")
    else:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM atm_reversion_paper_trades
            WHERE COALESCE(entry_yes_bid, 0) >= COALESCE(entry_yes_ask, 100)
            """
        )
        crossed_n = cur.fetchone()[0]
        if crossed_n:
            print(
                f"  note: {crossed_n} crossed/complementary book row(s); "
                "treat as execution-verification candidates"
            )
        cur.execute(
            """
            SELECT COUNT(*)
            FROM atm_reversion_paper_trades
            WHERE COALESCE(entry_c, 0) <= 0
            """
        )
        invalid_n = cur.fetchone()[0]
        if invalid_n:
            print(
                f"  note: {invalid_n} invalid zero-entry row(s) excluded "
                "from performance totals"
            )

        _print_paper_summary("all-valid", _load_paper_summary(cur, clean=False))
        clean_summary = _load_paper_summary(cur, clean=True)
        _print_paper_summary("clean-book", clean_summary)
        if clean_summary[0] == 0:
            print(
                "  clean-book: no rows yet. Current ATM paper edge is entirely "
                "crossed/complementary-book behavior, not a normal bid<ask book."
            )

        print("\nBy Side")
        cur.execute(
            """
            SELECT UPPER(side),
                   COUNT(*),
                   SUM(CASE WHEN net_cents > 0 THEN 1 ELSE 0 END),
                   ROUND(AVG(net_cents * 1.0 / NULLIF(contracts, 0)), 2),
                   SUM(net_cents)
            FROM atm_reversion_paper_trades
            WHERE """ + _paper_where(clean=False) + """
            GROUP BY side
            ORDER BY side
            """
        )
        rows = []
        for side, n, wins, avg_net, total_net in cur.fetchall():
            wr = wins / n * 100.0 if n else 0.0
            rows.append((side, n, f"{wr:.1f}%", _fmt_c(avg_net), _fmt_usd(total_net)))
        _print_rows(rows, ["side", "n", "WR", "avg_net", "total_net"])

        print("\nBy Exit")
        cur.execute(
            """
            SELECT exit_reason,
                   COUNT(*),
                   SUM(CASE WHEN net_cents > 0 THEN 1 ELSE 0 END),
                   ROUND(AVG(net_cents * 1.0 / NULLIF(contracts, 0)), 2),
                   SUM(net_cents)
            FROM atm_reversion_paper_trades
            WHERE """ + _paper_where(clean=False) + """
            GROUP BY exit_reason
            ORDER BY COUNT(*) DESC
            """
        )
        rows = []
        for reason, n, wins, avg_net, total_net in cur.fetchall():
            wr = wins / n * 100.0 if n else 0.0
            rows.append((reason, n, f"{wr:.1f}%", _fmt_c(avg_net), _fmt_usd(total_net)))
        _print_rows(rows, ["exit", "n", "WR", "avg_net", "total_net"])

        print("\nClean-Book By Side")
        cur.execute(
            """
            SELECT UPPER(side),
                   COUNT(*),
                   SUM(CASE WHEN net_cents > 0 THEN 1 ELSE 0 END),
                   ROUND(AVG(net_cents * 1.0 / NULLIF(contracts, 0)), 2),
                   SUM(net_cents)
            FROM atm_reversion_paper_trades
            WHERE """ + _paper_where(clean=True) + """
            GROUP BY side
            ORDER BY side
            """
        )
        clean_rows = []
        for side, n, wins, avg_net, total_net in cur.fetchall():
            wr = wins / n * 100.0 if n else 0.0
            clean_rows.append((side, n, f"{wr:.1f}%", _fmt_c(avg_net), _fmt_usd(total_net)))
        _print_rows(clean_rows, ["side", "n", "WR", "avg_net", "total_net"])

        print(f"\nRecent {limit}")
        cur.execute(
            """
            SELECT ticker, UPPER(side), entry_c, exit_c, exit_reason,
                   contracts, gross_cents, net_cents, created_at
            FROM atm_reversion_paper_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for ticker, side, entry, exit_c, reason, contracts, gross, net, created in cur.fetchall():
            rows.append((
                ticker[-18:], side, f"{contracts}ct", f"{entry}c",
                f"{exit_c}c", reason, _fmt_usd(net), created,
            ))
        _print_rows(rows, ["ticker", "side", "size", "entry", "exit", "reason", "net", "created"])

        if crossed_n:
            print(f"\nCrossed/Complementary Book Rows {min(limit, crossed_n)}")
            cur.execute(
                """
                SELECT ticker, UPPER(side), entry_yes_bid, entry_yes_ask,
                       entry_c, exit_c, net_cents, created_at
                FROM atm_reversion_paper_trades
                WHERE COALESCE(entry_yes_bid, 0) >= COALESCE(entry_yes_ask, 100)
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            bad_rows = []
            for ticker, side, ybid, yask, entry, exit_c, net, created in cur.fetchall():
                bad_rows.append((
                    ticker[-18:], side, f"{ybid}c", f"{yask}c",
                    f"{entry}c", f"{exit_c}c", _fmt_usd(net), created,
                ))
            _print_rows(
                bad_rows,
                ["ticker", "side", "yes_bid", "yes_ask", "entry", "exit", "net", "created"],
            )

    print("\nEntry Shadow")
    if not _table_exists(cur, "atm_reversion_entry_shadow"):
        print("  atm_reversion_entry_shadow has not been created yet.")
    else:
        cur.execute(
            """
            SELECT entry_mode,
                   COUNT(*),
                   COALESCE(SUM(filled_simulated), 0),
                   ROUND(AVG(sim_fill_delay_s), 1),
                   ROUND(AVG(signal_side_ask - min_same_side_ask_60s), 2),
                   ROUND(AVG(signal_side_ask - min_same_side_ask_180s), 2)
            FROM atm_reversion_entry_shadow
            GROUP BY entry_mode
            """
        )
        rows = []
        for mode, signals, fills, delay, drop60, drop180 in cur.fetchall():
            fill_rate = fills / signals * 100.0 if signals else 0.0
            rows.append((
                mode, signals, fills, f"{fill_rate:.1f}%",
                f"{delay if delay is not None else 0}s",
                _fmt_c(drop60 or 0), _fmt_c(drop180 or 0),
            ))
        _print_rows(rows, ["mode", "signals", "fills", "fill_rate", "avg_delay", "drop60", "drop180"])

        cur.execute(
            """
            SELECT entry_mode,
                   COUNT(*),
                   COALESCE(SUM(filled_simulated), 0),
                   ROUND(AVG(sim_fill_delay_s), 1),
                   ROUND(AVG(signal_side_ask - min_same_side_ask_60s), 2),
                   ROUND(AVG(signal_side_ask - min_same_side_ask_180s), 2)
            FROM atm_reversion_entry_shadow
            WHERE """ + _shadow_where(clean=True) + """
            GROUP BY entry_mode
            """
        )
        clean_shadow_rows = []
        for mode, signals, fills, delay, drop60, drop180 in cur.fetchall():
            fill_rate = fills / signals * 100.0 if signals else 0.0
            clean_shadow_rows.append((
                mode, signals, fills, f"{fill_rate:.1f}%",
                f"{delay if delay is not None else 0}s",
                _fmt_c(drop60 or 0), _fmt_c(drop180 or 0),
            ))
        print("\nClean-Book Entry Shadow")
        _print_rows(
            clean_shadow_rows,
            ["mode", "signals", "fills", "fill_rate", "avg_delay", "drop60", "drop180"],
        )
        if not clean_shadow_rows:
            print(
                "  clean-book shadow has no rows yet; current shadow fills are "
                "crossed/complementary-book signals."
            )

        cur.execute(
            """
            SELECT ticker, UPPER(side), signal_side_bid, signal_side_ask,
                   intended_entry_c, filled_simulated, sim_fill_delay_s,
                   min_same_side_ask_60s, final_reason, created_at
            FROM atm_reversion_entry_shadow
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        shadow_rows = []
        for row in cur.fetchall():
            ticker, side, bid, ask, intended, filled, delay, min60, reason, created = row
            shadow_rows.append((
                ticker[-18:], side, f"{bid}c", f"{ask}c", f"{intended}c",
                "yes" if filled else "no", f"{delay if delay is not None else ''}",
                f"{min60 if min60 is not None else ''}c", reason, created,
            ))
        if shadow_rows:
            print(f"\nRecent Shadow {limit}")
            _print_rows(
                shadow_rows,
                ["ticker", "side", "bid", "ask", "intend", "fill", "delay", "min60", "reason", "created"],
            )

    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="Recent rows to show")
    ap.add_argument("--watch", type=int, default=0, help="Refresh every N seconds")
    ap.add_argument("--live-check", action="store_true",
                    help="Also query Kalshi REST for live KXBTC15M positions")
    args = ap.parse_args()

    while True:
        if args.watch:
            os.system("cls" if os.name == "nt" else "clear")
        render(args.limit, live_check=args.live_check)
        if not args.watch:
            break
        time.sleep(max(1, args.watch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
