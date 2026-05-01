"""Codex Snapback paper engine.

This is intentionally separate from the live engine. It tests one thesis:
BTC 15m windows often mean-revert through the opening price, but the trade is
only valuable if the Kalshi contract offers a harvestable snapback bid.

Design:
  - Detect BTC impulse away from window open.
  - Wait for stall/deceleration instead of catching the falling knife.
  - Buy the losing side only in the mid band where settlement-truth losses
    were less severe than cheap convexity.
  - Exit on the first executable-style +4c snapback, not settlement.

Data sources are local SQLite tables:
  - window_snapshots for independent hypothetical signals.
  - settlement_ledger for expiry fallback truth.

Usage:
  python -m paper_engine.codex_snapback --hours 24
  python -m paper_engine.codex_snapback --hours 24 --write-db
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional


DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trades.db"


@dataclass(frozen=True)
class Snap:
    ticker: str
    t_offset_sec: int
    ts_ms: int
    btc_price: float
    yes_mid: int
    pressure_score: float
    pressure_confidence: float
    regime: str


@dataclass
class PaperTrade:
    ticker: str
    side: str
    entry_ts_ms: int
    entry_offset_s: int
    entry_cents: int
    contracts: int
    exit_ts_ms: int
    exit_offset_s: int
    exit_cents: int
    exit_reason: str
    pnl: float
    mfe_cents: int
    mae_cents: int
    btc_move_entry: float
    pressure_score: float
    regime: str


@dataclass(frozen=True)
class Params:
    impulse_usd: float = 25.0
    entry_min_s: int = 60
    entry_max_s: int = 420
    min_entry_cents: int = 36
    max_entry_cents: int = 55
    max_contract_60s_adverse_cents: int = 3
    min_btc_stall_ratio: float = 0.65
    pressure_disagree_block: float = 0.08
    target_cents: int = 4
    stop_loss_cents: int = 0
    hard_exit_s: int = 600
    contracts: int = 10
    exit_slippage_cents: int = 1


def _side_price(side: str, yes_mid: int) -> int:
    return yes_mid if side == "yes" else 100 - yes_mid


def _pressure_oriented(side: str, pressure_score: float) -> float:
    return pressure_score if side == "yes" else -pressure_score


def _load_windows(conn: sqlite3.Connection, hours: int) -> dict[str, list[Snap]]:
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    rows = conn.execute(
        """
        SELECT ticker, t_offset_sec, ts_ms, btc_price_cents, kalshi_mid_cents,
               COALESCE(pressure_score, 0.0) AS pressure_score,
               COALESCE(pressure_confidence, 0.0) AS pressure_confidence,
               COALESCE(regime, '') AS regime
        FROM window_snapshots
        WHERE ts_ms >= ?
          AND btc_price_cents IS NOT NULL
          AND kalshi_mid_cents IS NOT NULL
          AND kalshi_mid_cents BETWEEN 1 AND 99
        ORDER BY ticker, t_offset_sec ASC
        """,
        (cutoff_ms,),
    ).fetchall()
    windows: dict[str, list[Snap]] = {}
    for r in rows:
        s = Snap(
            ticker=r["ticker"],
            t_offset_sec=int(r["t_offset_sec"]),
            ts_ms=int(r["ts_ms"]),
            btc_price=float(r["btc_price_cents"]) / 100.0,
            yes_mid=int(r["kalshi_mid_cents"]),
            pressure_score=float(r["pressure_score"] or 0.0),
            pressure_confidence=float(r["pressure_confidence"] or 0.0),
            regime=str(r["regime"] or "").lower(),
        )
        windows.setdefault(s.ticker, []).append(s)
    return windows


def _settlement_result(conn: sqlite3.Connection, ticker: str) -> Optional[str]:
    row = conn.execute(
        "SELECT market_result FROM settlement_ledger WHERE ticker=?",
        (ticker,),
    ).fetchone()
    return str(row["market_result"]).lower() if row and row["market_result"] else None


def _choose_entry(snaps: list[Snap], p: Params) -> Optional[tuple[int, str, int, float]]:
    if len(snaps) < 4:
        return None
    btc_open = snaps[0].btc_price
    by_t = {s.t_offset_sec: i for i, s in enumerate(snaps)}
    for i, s in enumerate(snaps):
        if s.t_offset_sec < p.entry_min_s or s.t_offset_sec > p.entry_max_s:
            continue
        move = s.btc_price - btc_open
        if abs(move) < p.impulse_usd:
            continue
        side = "no" if move > 0 else "yes"
        entry = _side_price(side, s.yes_mid)
        if not (p.min_entry_cents <= entry <= p.max_entry_cents):
            continue
        oriented_pressure = _pressure_oriented(side, s.pressure_score)
        if oriented_pressure < -p.pressure_disagree_block:
            continue

        prev_i = by_t.get(s.t_offset_sec - 60)
        prev2_i = by_t.get(s.t_offset_sec - 120)
        if prev_i is None or prev2_i is None:
            continue
        prev = snaps[prev_i]
        prev2 = snaps[prev2_i]

        # BTC stall: latest one-minute extension should be materially smaller
        # than the prior one-minute impulse in the same direction.
        d_recent = (s.btc_price - prev.btc_price) * (1 if move > 0 else -1)
        d_prior = (prev.btc_price - prev2.btc_price) * (1 if move > 0 else -1)
        if d_recent > max(3.0, d_prior * p.min_btc_stall_ratio):
            continue

        # Contract stall: our side should not still be puking.
        prev_side = _side_price(side, prev.yes_mid)
        if entry - prev_side < -p.max_contract_60s_adverse_cents:
            continue
        return i, side, entry, move
    return None


def _simulate_exit(
    conn: sqlite3.Connection,
    snaps: list[Snap],
    entry_i: int,
    side: str,
    entry: int,
    p: Params,
) -> tuple[int, int, str, int, int]:
    peak_profit = -99
    trough_profit = 99
    best_exit_i = entry_i
    for j in range(entry_i + 1, len(snaps)):
        s = snaps[j]
        age = s.t_offset_sec - snaps[entry_i].t_offset_sec
        side_mid = _side_price(side, s.yes_mid)
        executable_exit = max(1, min(99, side_mid - p.exit_slippage_cents))
        profit = executable_exit - entry
        peak_profit = max(peak_profit, profit)
        trough_profit = min(trough_profit, profit)
        best_exit_i = j
        if p.stop_loss_cents > 0 and profit <= -p.stop_loss_cents:
            return j, entry - p.stop_loss_cents, "stop_loss", peak_profit, trough_profit
        if profit >= p.target_cents:
            # Conservative harvest assumption: once the executable bid crosses
            # target, assume we sell at target, not at the full sampled spike.
            return j, entry + p.target_cents, "target_capture", peak_profit, trough_profit
        if age >= p.hard_exit_s:
            return j, executable_exit, "time_exit", peak_profit, trough_profit

    result = _settlement_result(conn, snaps[entry_i].ticker)
    if result in ("yes", "no"):
        settle_px = 100 if result == side else 0
        profit = settle_px - entry
        peak_profit = max(peak_profit, profit)
        trough_profit = min(trough_profit, profit)
        return best_exit_i, settle_px, "settlement", peak_profit, trough_profit
    if best_exit_i > entry_i:
        side_mid = _side_price(side, snaps[best_exit_i].yes_mid)
        return best_exit_i, max(1, min(99, side_mid - p.exit_slippage_cents)), "last_snapshot", peak_profit, trough_profit
    return entry_i, entry, "no_exit_data", 0, 0


def run_backtest(hours: int, params: Params, write_db: bool = False,
                 db_path: Path = DB_PATH) -> list[PaperTrade]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    windows = _load_windows(conn, hours)
    trades: list[PaperTrade] = []
    for snaps in windows.values():
        choice = _choose_entry(snaps, params)
        if choice is None:
            continue
        entry_i, side, entry, btc_move = choice
        exit_i, exit_px, reason, mfe, mae = _simulate_exit(
            conn, snaps, entry_i, side, entry, params,
        )
        pnl = (exit_px - entry) * params.contracts / 100.0
        ent = snaps[entry_i]
        ex = snaps[exit_i]
        trades.append(PaperTrade(
            ticker=ent.ticker,
            side=side,
            entry_ts_ms=ent.ts_ms,
            entry_offset_s=ent.t_offset_sec,
            entry_cents=entry,
            contracts=params.contracts,
            exit_ts_ms=ex.ts_ms,
            exit_offset_s=ex.t_offset_sec,
            exit_cents=exit_px,
            exit_reason=reason,
            pnl=round(pnl, 4),
            mfe_cents=mfe,
            mae_cents=mae,
            btc_move_entry=round(btc_move, 2),
            pressure_score=round(ent.pressure_score, 4),
            regime=ent.regime,
        ))
    if write_db:
        _write_trades(conn, trades, params)
    conn.close()
    return trades


def _write_trades(conn: sqlite3.Connection, trades: Iterable[PaperTrade],
                  p: Params) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS codex_paper_snapback_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts_ms INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL,
            entry_offset_s INTEGER NOT NULL,
            entry_cents INTEGER NOT NULL,
            contracts INTEGER NOT NULL,
            exit_ts_ms INTEGER NOT NULL,
            exit_offset_s INTEGER NOT NULL,
            exit_cents INTEGER NOT NULL,
            exit_reason TEXT NOT NULL,
            pnl REAL NOT NULL,
            mfe_cents INTEGER NOT NULL,
            mae_cents INTEGER NOT NULL,
            btc_move_entry REAL NOT NULL,
            pressure_score REAL NOT NULL,
            regime TEXT,
            params_json TEXT NOT NULL
        )
    """)
    import json
    run_ts_ms = int(time.time() * 1000)
    params_json = json.dumps(p.__dict__, sort_keys=True)
    conn.executemany("""
        INSERT INTO codex_paper_snapback_trades
            (run_ts_ms, strategy, ticker, side, entry_ts_ms, entry_offset_s,
             entry_cents, contracts, exit_ts_ms, exit_offset_s, exit_cents,
             exit_reason, pnl, mfe_cents, mae_cents, btc_move_entry,
             pressure_score, regime, params_json)
        VALUES (?, 'codex_snapback_v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            run_ts_ms, t.ticker, t.side, t.entry_ts_ms, t.entry_offset_s,
            t.entry_cents, t.contracts, t.exit_ts_ms, t.exit_offset_s,
            t.exit_cents, t.exit_reason, t.pnl, t.mfe_cents, t.mae_cents,
            t.btc_move_entry, t.pressure_score, t.regime, params_json,
        )
        for t in trades
    ])
    conn.commit()


def _print_summary(trades: list[PaperTrade]) -> None:
    print("Codex Snapback paper engine")
    print(f"Trades: {len(trades)}")
    if not trades:
        return
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    total = sum(t.pnl for t in trades)
    print(f"P&L: ${total:+.2f}  WR={len(wins)/len(trades)*100:.1f}%  $/trade=${total/len(trades):+.3f}")
    print(f"Avg win=${mean([t.pnl for t in wins]) if wins else 0:+.2f}  "
          f"Avg loss=${mean([t.pnl for t in losses]) if losses else 0:+.2f}")
    by_reason: dict[str, list[PaperTrade]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t)
    print("\nBy exit reason:")
    for reason, rows in sorted(by_reason.items()):
        subtotal = sum(t.pnl for t in rows)
        wr = sum(1 for t in rows if t.pnl > 0) / len(rows) * 100
        print(f"  {reason:<18} n={len(rows):>3} pnl=${subtotal:+7.2f} wr={wr:5.1f}%")
    print("\nRecent trades:")
    for t in trades[-12:]:
        print(
            f"  {t.ticker[-18:]:<18} {t.side.upper():<3} "
            f"@{t.entry_cents:>2} -> {t.exit_cents:>3} "
            f"{t.exit_reason:<17} pnl=${t.pnl:+5.2f} "
            f"mfe={t.mfe_cents:+3} mae={t.mae_cents:+3} "
            f"btc={t.btc_move_entry:+6.1f} regime={t.regime}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--write-db", action="store_true")
    ap.add_argument("--target", type=int, default=4)
    ap.add_argument("--stop-loss", type=int, default=0,
                    help="Optional hard stop in cents; 0 disables.")
    ap.add_argument("--impulse", type=float, default=25.0)
    ap.add_argument("--contracts", type=int, default=10)
    args = ap.parse_args()
    params = Params(
        impulse_usd=args.impulse,
        target_cents=args.target,
        stop_loss_cents=args.stop_loss,
        contracts=args.contracts,
    )
    trades = run_backtest(args.hours, params, write_db=args.write_db)
    _print_summary(trades)
    if args.write_db:
        print("\nWrote rows to codex_paper_snapback_trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
