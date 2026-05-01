"""Live shadow monitor for Codex paper-engine research.

This process is intentionally read-only with respect to the live engine:
it tails data/dashboard_state.json, persists sub-minute observations, and
paper-trades the "session-open inversion" idea using conservative estimated
prices from the visible Kalshi mid.

It answers the question the live engine can answer best:
  - Did a cross of the opening impulse get accepted or rejected intraminute?
  - Did Kalshi reprice with the BTC move?
  - Would a trailing TP have captured the move?

No Kalshi API calls, no order placement, no engine restarts.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "data" / "dashboard_state.json"
DB = ROOT / "data" / "codex_shadow_monitor.db"


@dataclass
class PaperPosition:
    ticker: str
    side: str
    entry_ts_ms: int
    entry_age_s: float
    entry_c: int
    peak_profit_c: int = -100
    armed: bool = False


@dataclass
class AtmPosition:
    ticker: str
    side: str
    entry_ts_ms: int
    entry_age_s: float
    entry_c: int
    kind: str
    peak_profit_c: int = -100
    armed: bool = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            age_s REAL,
            btc_price REAL,
            session_open REAL,
            btc_from_open REAL,
            strike REAL,
            strike_dist_usd REAL,
            strike_dist_pct REAL,
            abs_strike_dist_pct REAL,
            kalshi_mid_c INTEGER,
            yes_bid_est_c INTEGER,
            yes_ask_est_c INTEGER,
            spread_est_c INTEGER,
            pressure_score REAL,
            pressure_direction TEXT,
            pressure_confidence REAL,
            btc_5s REAL,
            btc_30s REAL,
            btc_300s REAL,
            tick_velocity REAL,
            fvg_signed_c INTEGER,
            rsi_1m REAL,
            regime TEXT,
            open_dir TEXT,
            opening_move_usd REAL,
            accepted_side TEXT,
            weak_breakout INTEGER DEFAULT 0,
            momentum_change INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL,
            exit_ts_ms INTEGER,
            entry_age_s REAL,
            exit_age_s REAL,
            entry_c INTEGER NOT NULL,
            exit_c INTEGER,
            pnl_c INTEGER,
            peak_profit_c INTEGER,
            exit_reason TEXT,
            open_dir TEXT,
            opening_move_usd REAL,
            strike_entry REAL,
            strike_dist_pct_entry REAL,
            abs_strike_dist_pct_entry REAL,
            btc_from_open_entry REAL,
            pressure_score_entry REAL,
            pressure_confidence_entry REAL,
            btc_30s_entry REAL,
            tick_velocity_entry REAL,
            fvg_signed_entry INTEGER,
            created_at_ms INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atm_reversion_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            kind TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL,
            exit_ts_ms INTEGER,
            entry_age_s REAL,
            exit_age_s REAL,
            entry_c INTEGER NOT NULL,
            exit_c INTEGER,
            pnl_c INTEGER,
            peak_profit_c INTEGER,
            exit_reason TEXT,
            strike_entry REAL,
            strike_dist_pct_entry REAL,
            abs_strike_dist_pct_entry REAL,
            kalshi_mid_entry INTEGER,
            fair_yes_entry REAL,
            fvg_signed_entry INTEGER,
            rsi_1m_entry REAL,
            created_at_ms INTEGER NOT NULL
        )
    """)
    for table, columns in {
        "shadow_ticks": {
            "strike": "REAL",
            "strike_dist_usd": "REAL",
            "strike_dist_pct": "REAL",
            "abs_strike_dist_pct": "REAL",
        },
        "shadow_trades": {
            "strike_entry": "REAL",
            "strike_dist_pct_entry": "REAL",
            "abs_strike_dist_pct_entry": "REAL",
        },
    }.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()
    return conn


def _load_dashboard() -> dict | None:
    try:
        return json.loads(DASHBOARD.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _clip_c(price: float) -> int:
    return max(1, min(99, int(round(price))))


def _estimated_prices(mid_c: float, spread_c: int, side: str) -> tuple[int, int]:
    """Return conservative (ask, bid) for the requested side."""
    yes_bid = _clip_c(mid_c - spread_c / 2)
    yes_ask = _clip_c(mid_c + spread_c / 2)
    if side == "yes":
        return yes_ask, yes_bid
    return 100 - yes_bid, 100 - yes_ask


def _tick_features(state: dict, spread_c: int) -> dict | None:
    session = state.get("session") or {}
    btc = state.get("btc") or {}
    tape = state.get("tape") or {}
    pressure = state.get("pressure") or {}
    five_sec = state.get("five_sec") or {}
    fvg = state.get("fvg") or {}
    ta = state.get("ta") or {}
    prob = state.get("prob") or {}
    ticker = session.get("ticker") or ""
    if not ticker:
        return None
    mid = tape.get("mid_cents")
    price = btc.get("price")
    session_open = btc.get("session_open")
    if mid is None or price is None or session_open is None:
        return None
    strike = float(prob.get("strike") or 0.0)
    strike_dist_usd = float(price) - strike if strike > 0 else 0.0
    strike_dist_pct = strike_dist_usd / strike * 100.0 if strike > 0 else 0.0
    age_s = 900.0 - float(session.get("seconds_remaining") or 0.0)
    yes_ask, yes_bid = _estimated_prices(float(mid), spread_c, "yes")
    return {
        "ts_ms": int(float(state.get("ts") or time.time()) * 1000),
        "ticker": str(ticker),
        "age_s": age_s,
        "btc_price": float(price),
        "session_open": float(session_open),
        "btc_from_open": float(price) - float(session_open),
        "strike": strike,
        "strike_dist_usd": strike_dist_usd,
        "strike_dist_pct": strike_dist_pct,
        "abs_strike_dist_pct": abs(strike_dist_pct),
        "kalshi_mid_c": int(round(float(mid))),
        "yes_bid_est_c": yes_bid,
        "yes_ask_est_c": yes_ask,
        "spread_est_c": spread_c,
        "pressure_score": float(pressure.get("score") or 0.0),
        "pressure_direction": str(pressure.get("direction") or ""),
        "pressure_confidence": float(pressure.get("confidence") or 0.0),
        "btc_5s": float(pressure.get("btc_move_5s") or 0.0),
        "btc_30s": float(pressure.get("btc_move_30s") or 0.0),
        "btc_300s": float(pressure.get("btc_move_300s") or 0.0),
        "tick_velocity": float(five_sec.get("tick_velocity") or 0.0),
        "fvg_signed_c": int(round(float(fvg.get("fvg_vs_baseline") or 0.0))),
        "rsi_1m": float(ta.get("rsi") or 50.0),
        "regime": str(session.get("regime") or ""),
    }


def _insert_tick(conn: sqlite3.Connection, row: dict) -> None:
    keys = list(row.keys())
    conn.execute(
        f"INSERT INTO shadow_ticks ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
        [row[k] for k in keys],
    )


def _classify_acceptance(
    history: list[dict],
    row: dict,
    open_dir: str,
    opening_move: float,
    min_inversion_usd: float,
    hold_s: float,
) -> tuple[str, bool, bool]:
    """Return accepted side plus weak/momentum flags.

    The acceptance test is deliberately simple and inspectable:
      - cross must move at least min_inversion_usd beyond the first-minute anchor
      - hold requires recent ticks to remain beyond that anchor
      - pressure and 30s BTC move must agree with the new side
    """
    if not open_dir:
        return "", False, False
    anchor = opening_move
    move = float(row["btc_from_open"])
    if open_dir == "up":
        crossed = move <= anchor - min_inversion_usd
        side = "no"
        pressure_ok = row["pressure_direction"] == "no" and row["pressure_confidence"] >= 0.55
        btc_ok = row["btc_30s"] < 0 and row["tick_velocity"] <= 0
        held = all(float(t["btc_from_open"]) <= anchor - min_inversion_usd for t in history if row["ts_ms"] - t["ts_ms"] <= hold_s * 1000)
    else:
        crossed = move >= anchor + min_inversion_usd
        side = "yes"
        pressure_ok = row["pressure_direction"] == "yes" and row["pressure_confidence"] >= 0.55
        btc_ok = row["btc_30s"] > 0 and row["tick_velocity"] >= 0
        held = all(float(t["btc_from_open"]) >= anchor + min_inversion_usd for t in history if row["ts_ms"] - t["ts_ms"] <= hold_s * 1000)
    if not crossed:
        return "", False, False
    recent = [t for t in history if row["ts_ms"] - t["ts_ms"] <= hold_s * 1000]
    enough_hold_samples = len(recent) >= max(3, int(hold_s // 2))
    momentum = bool(enough_hold_samples and held and pressure_ok and btc_ok)
    weak = bool(not momentum)
    return side if momentum else "", weak, momentum


def _open_trade(conn: sqlite3.Connection, pos: PaperPosition, row: dict, meta: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO shadow_trades
            (ticker, side, entry_ts_ms, entry_age_s, entry_c, peak_profit_c,
             open_dir, opening_move_usd, strike_entry, strike_dist_pct_entry,
             abs_strike_dist_pct_entry, btc_from_open_entry, pressure_score_entry,
             pressure_confidence_entry, btc_30s_entry, tick_velocity_entry,
             fvg_signed_entry, created_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pos.ticker, pos.side, pos.entry_ts_ms, pos.entry_age_s, pos.entry_c,
            pos.peak_profit_c, meta.get("open_dir"), meta.get("opening_move"),
            row["strike"], row["strike_dist_pct"], row["abs_strike_dist_pct"],
            row["btc_from_open"], row["pressure_score"], row["pressure_confidence"],
            row["btc_30s"], row["tick_velocity"], row["fvg_signed_c"],
            int(time.time() * 1000),
        ),
    )
    return int(cur.lastrowid)


def _close_trade(conn: sqlite3.Connection, trade_id: int, row: dict, exit_c: int, reason: str, pos: PaperPosition) -> None:
    conn.execute(
        """
        UPDATE shadow_trades
        SET exit_ts_ms=?, exit_age_s=?, exit_c=?, pnl_c=?, peak_profit_c=?, exit_reason=?
        WHERE id=?
        """,
        (
            row["ts_ms"], row["age_s"], exit_c, exit_c - pos.entry_c,
            pos.peak_profit_c, reason, trade_id,
        ),
    )


def _open_atm_trade(conn: sqlite3.Connection, pos: AtmPosition, row: dict, fair_yes: float) -> int:
    cur = conn.execute(
        """
        INSERT INTO atm_reversion_trades
            (ticker, kind, side, entry_ts_ms, entry_age_s, entry_c, peak_profit_c,
             strike_entry, strike_dist_pct_entry, abs_strike_dist_pct_entry,
             kalshi_mid_entry, fair_yes_entry, fvg_signed_entry, rsi_1m_entry,
             created_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pos.ticker, pos.kind, pos.side, pos.entry_ts_ms, pos.entry_age_s,
            pos.entry_c, pos.peak_profit_c, row["strike"], row["strike_dist_pct"],
            row["abs_strike_dist_pct"], row["kalshi_mid_c"], fair_yes,
            row["fvg_signed_c"], row["rsi_1m"], int(time.time() * 1000),
        ),
    )
    return int(cur.lastrowid)


def _close_atm_trade(conn: sqlite3.Connection, trade_id: int, row: dict, exit_c: int, reason: str, pos: AtmPosition) -> None:
    conn.execute(
        """
        UPDATE atm_reversion_trades
        SET exit_ts_ms=?, exit_age_s=?, exit_c=?, pnl_c=?, peak_profit_c=?, exit_reason=?
        WHERE id=?
        """,
        (
            row["ts_ms"], row["age_s"], exit_c, exit_c - pos.entry_c,
            pos.peak_profit_c, reason, trade_id,
        ),
    )


def run(args: argparse.Namespace) -> None:
    conn = _connect()
    ticker = ""
    history: list[dict] = []
    opening_move: float | None = None
    open_dir = ""
    paper_pos: PaperPosition | None = None
    paper_id: int | None = None
    atm_pos: AtmPosition | None = None
    atm_id: int | None = None
    # 2026-04-25 fix per Codex handoff: lock ATM paper to ONE entry per ticker.
    # Pre-fix the monitor was re-entering the same ticker after every instant
    # target exit (164 paper trades on a single ticker observed). Per-ticker
    # set is reset on ticker change below; ticker is added immediately on
    # ATM open (not on exit), so a fast in-out cycle still locks the window.
    entered_atm_tickers: set[str] = set()
    seen_ts: int | None = None
    last_commit = time.time()
    print(f"Codex shadow monitor writing {DB}")
    print("Read-only: dashboard_state.json -> shadow_ticks/shadow_trades")
    while True:
        state = _load_dashboard()
        if state is None:
            time.sleep(args.poll_s)
            continue
        row = _tick_features(state, args.spread_c)
        if row is None or row["ts_ms"] == seen_ts:
            time.sleep(args.poll_s)
            continue
        seen_ts = row["ts_ms"]
        if row["ticker"] != ticker:
            ticker = row["ticker"]
            history = []
            opening_move = None
            open_dir = ""
            paper_pos = None
            paper_id = None
            atm_pos = None
            atm_id = None
            entered_atm_tickers.clear()  # reset per-ticker ATM lock on new window
            print(f"\nNEW {ticker} session_open=${row['session_open']:.2f}")

        if opening_move is None and row["age_s"] <= args.impulse_window_s:
            # The "open condition" is not a fixed first-minute candle. It is
            # the first meaningful displacement from the session-open anchor
            # that forms early enough to be actionable.
            candidate_move = row["btc_from_open"]
            if abs(candidate_move) >= args.min_open_move_usd:
                opening_move = candidate_move
                open_dir = "up" if opening_move > 0 else "down"
                print(
                    f"OPEN-IMPULSE {ticker[-18:]} dir={open_dir} "
                    f"age={row['age_s']:.0f}s move=${opening_move:+.2f}"
                )
        elif opening_move is None and row["age_s"] > args.impulse_window_s:
            opening_move = 0.0
            open_dir = ""
            print(f"OPEN-IMPULSE none {ticker[-18:]} by age={row['age_s']:.0f}s")

        accepted_side, weak, momentum = _classify_acceptance(
            history, row, open_dir, opening_move or 0.0,
            args.min_inversion_usd, args.hold_s,
        )
        row["open_dir"] = open_dir
        row["opening_move_usd"] = opening_move
        row["accepted_side"] = accepted_side
        row["weak_breakout"] = int(weak)
        row["momentum_change"] = int(momentum)
        _insert_tick(conn, row)
        history.append(row)
        cutoff = row["ts_ms"] - 180_000
        history = [h for h in history if h["ts_ms"] >= cutoff]

        if paper_pos is None and accepted_side and args.min_entry_age_s <= row["age_s"] <= args.max_entry_age_s:
            ask, _bid = _estimated_prices(row["kalshi_mid_c"], args.spread_c, accepted_side)
            dist_ok = args.min_strike_dist_pct <= row["abs_strike_dist_pct"] <= args.max_strike_dist_pct
            if args.min_entry_c <= ask <= args.max_entry_c and dist_ok:
                paper_pos = PaperPosition(
                    ticker=ticker,
                    side=accepted_side,
                    entry_ts_ms=row["ts_ms"],
                    entry_age_s=row["age_s"],
                    entry_c=ask,
                    peak_profit_c=-ask,
                )
                paper_id = _open_trade(conn, paper_pos, row, {
                    "open_dir": open_dir,
                    "opening_move": opening_move,
                })
                print(
                    f"PAPER ENTRY {ticker[-18:]} {accepted_side.upper()}@{ask} "
                    f"age={row['age_s']:.0f}s btc_from_open=${row['btc_from_open']:+.2f} "
                    f"strike_dist={row['strike_dist_pct']:+.3f}% "
                    f"p30=${row['btc_30s']:+.2f} conf={row['pressure_confidence']:.2f}"
                )

        if paper_pos is not None and paper_id is not None:
            _ask, bid = _estimated_prices(row["kalshi_mid_c"], args.spread_c, paper_pos.side)
            profit = bid - paper_pos.entry_c
            paper_pos.peak_profit_c = max(paper_pos.peak_profit_c, profit)
            if not paper_pos.armed and profit >= args.arm_c:
                paper_pos.armed = True
                print(f"PAPER TRAIL ARM {ticker[-18:]} peak=+{paper_pos.peak_profit_c}c bid={bid}c")
            should_exit = False
            reason = ""
            if paper_pos.armed and paper_pos.peak_profit_c - profit >= args.giveback_c:
                should_exit = True
                reason = "trail"
            elif row["age_s"] >= args.force_exit_age_s:
                should_exit = True
                reason = "time"
            if should_exit:
                _close_trade(conn, paper_id, row, bid, reason, paper_pos)
                print(
                    f"PAPER EXIT {ticker[-18:]} {reason} bid={bid} "
                    f"pnl={bid - paper_pos.entry_c:+}c peak={paper_pos.peak_profit_c:+}c"
                )
                paper_pos = None
                paper_id = None

        if (atm_pos is None and args.atm_enabled
                and ticker not in entered_atm_tickers):
            fair_yes = max(0.0, min(100.0, float(row["kalshi_mid_c"]) + float(row["fvg_signed_c"])))
            fair_no = 100.0 - fair_yes
            yes_ask, _yes_bid = _estimated_prices(row["kalshi_mid_c"], args.spread_c, "yes")
            no_ask, _no_bid = _estimated_prices(row["kalshi_mid_c"], args.spread_c, "no")
            near_strike = row["abs_strike_dist_pct"] <= args.atm_max_strike_dist_pct
            age_ok = args.atm_min_age_s <= row["age_s"] <= args.atm_max_age_s
            spread_ok = args.spread_c <= args.atm_max_spread_c
            candidates = []
            if near_strike and age_ok and spread_ok:
                yes_edge = fair_yes - yes_ask
                no_edge = fair_no - no_ask
                if yes_ask <= args.atm_max_discount_entry_c and fair_yes >= args.atm_min_fair_c and yes_edge >= args.atm_min_edge_c:
                    candidates.append(("yes", yes_ask, yes_edge, "discount"))
                if no_ask <= args.atm_max_discount_entry_c and fair_no >= args.atm_min_fair_c and no_edge >= args.atm_min_edge_c:
                    candidates.append(("no", no_ask, no_edge, "discount"))
                if args.atm_bias_enabled:
                    if row["strike_dist_pct"] > 0 and yes_ask <= args.atm_max_bias_entry_c and fair_yes - yes_ask >= args.atm_min_bias_edge_c:
                        candidates.append(("yes", yes_ask, fair_yes - yes_ask, "bias"))
                    if row["strike_dist_pct"] < 0 and no_ask <= args.atm_max_bias_entry_c and fair_no - no_ask >= args.atm_min_bias_edge_c:
                        candidates.append(("no", no_ask, fair_no - no_ask, "bias"))
            if candidates:
                side, entry_c, edge_c, kind = max(candidates, key=lambda c: c[2])
                atm_pos = AtmPosition(
                    ticker=ticker,
                    side=side,
                    entry_ts_ms=row["ts_ms"],
                    entry_age_s=row["age_s"],
                    entry_c=entry_c,
                    kind=kind,
                    peak_profit_c=-entry_c,
                )
                atm_id = _open_atm_trade(conn, atm_pos, row, fair_yes)
                # Lock ticker IMMEDIATELY on open (not on exit) so a fast
                # round-trip to target+exit doesn't allow re-entry within
                # the same window.
                entered_atm_tickers.add(ticker)
                print(
                    f"ATM PAPER ENTRY {ticker[-18:]} {kind} {side.upper()}@{entry_c} "
                    f"edge={edge_c:+.1f}c fairY={fair_yes:.1f} "
                    f"dist={row['strike_dist_pct']:+.3f}% mid={row['kalshi_mid_c']}c"
                )

        if atm_pos is not None and atm_id is not None:
            _ask, bid = _estimated_prices(row["kalshi_mid_c"], args.spread_c, atm_pos.side)
            profit = bid - atm_pos.entry_c
            atm_pos.peak_profit_c = max(atm_pos.peak_profit_c, profit)
            if not atm_pos.armed and profit >= args.atm_arm_c:
                atm_pos.armed = True
            should_exit = False
            reason = ""
            if bid >= args.atm_target_c:
                should_exit = True
                reason = "target"
            elif atm_pos.armed and atm_pos.peak_profit_c - profit >= args.atm_giveback_c:
                should_exit = True
                reason = "trail"
            elif row["abs_strike_dist_pct"] >= args.atm_stop_strike_dist_pct:
                should_exit = True
                reason = "strike_escape"
            elif row["age_s"] >= args.atm_force_exit_age_s:
                should_exit = True
                reason = "time"
            if should_exit:
                _close_atm_trade(conn, atm_id, row, bid, reason, atm_pos)
                print(
                    f"ATM PAPER EXIT {ticker[-18:]} {reason} {atm_pos.side.upper()} "
                    f"bid={bid} pnl={bid - atm_pos.entry_c:+}c peak={atm_pos.peak_profit_c:+}c"
                )
                atm_pos = None
                atm_id = None

        if time.time() - last_commit >= args.commit_s:
            conn.commit()
            last_commit = time.time()
        time.sleep(args.poll_s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-s", type=float, default=0.5)
    ap.add_argument("--commit-s", type=float, default=2.0)
    ap.add_argument("--spread-c", type=int, default=2, help="Conservative estimated YES spread from dashboard mid.")
    ap.add_argument("--impulse-window-s", type=float, default=300.0)
    ap.add_argument("--min-open-move-usd", type=float, default=30.0)
    ap.add_argument("--min-inversion-usd", type=float, default=30.0)
    ap.add_argument("--hold-s", type=float, default=12.0)
    ap.add_argument("--min-entry-age-s", type=float, default=10.0)
    ap.add_argument("--max-entry-age-s", type=float, default=600.0)
    ap.add_argument("--min-entry-c", type=int, default=20)
    ap.add_argument("--max-entry-c", type=int, default=70)
    ap.add_argument("--min-strike-dist-pct", type=float, default=0.0)
    ap.add_argument("--max-strike-dist-pct", type=float, default=9.0)
    ap.add_argument("--arm-c", type=int, default=4)
    ap.add_argument("--giveback-c", type=int, default=3)
    ap.add_argument("--force-exit-age-s", type=float, default=840.0)
    ap.add_argument("--atm-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--atm-max-strike-dist-pct", type=float, default=0.02)
    ap.add_argument("--atm-min-age-s", type=float, default=10.0)
    ap.add_argument("--atm-max-age-s", type=float, default=720.0)
    ap.add_argument("--atm-max-spread-c", type=int, default=4)
    ap.add_argument("--atm-max-discount-entry-c", type=int, default=40)
    ap.add_argument("--atm-min-fair-c", type=float, default=47.0)
    ap.add_argument("--atm-min-edge-c", type=float, default=8.0)
    ap.add_argument("--atm-bias-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--atm-max-bias-entry-c", type=int, default=62)
    ap.add_argument("--atm-min-bias-edge-c", type=float, default=1.0)
    ap.add_argument("--atm-target-c", type=int, default=49)
    ap.add_argument("--atm-arm-c", type=int, default=4)
    ap.add_argument("--atm-giveback-c", type=int, default=3)
    ap.add_argument("--atm-stop-strike-dist-pct", type=float, default=0.06)
    ap.add_argument("--atm-force-exit-age-s", type=float, default=840.0)
    args = ap.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
