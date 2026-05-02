"""Read-only alpha extraction report for BTC 15m Kalshi trades.

This script intentionally does not import engine modules or call Kalshi. It
only reads data/trades.db and writes a compact markdown report that can guide
BB_PURE / TA_FORCED entry and exit changes before any live code is touched.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "trades.db"


@dataclass(frozen=True)
class TradeRow:
    placed_at: str
    ticker: str
    side: str
    count: int
    entry_c: int
    pnl_dollars: float
    status: str
    result: str | None
    strategy: str | None
    ladder: dict[str, Any]
    created_ms: int | None = None
    gate_session_min: float | None = None
    gate_entry_c: int | None = None
    gate_snapshot: dict[str, Any] | None = None
    mfe_c: int | None = None
    mae_c: int | None = None

    @property
    def pnl_cents(self) -> int:
        return int(round(self.pnl_dollars * 100))

    @property
    def is_win(self) -> bool:
        return self.pnl_dollars > 0


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _fmt_money(cents: float) -> str:
    return f"${cents / 100.0:,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _bucket_entry(cents: int | None) -> str:
    if cents is None:
        return "unknown"
    if cents <= 25:
        return "00-25c"
    if cents <= 35:
        return "26-35c"
    if cents <= 50:
        return "36-50c"
    if cents <= 65:
        return "51-65c"
    if cents <= 80:
        return "66-80c"
    return "81-99c"


def _bucket_session_min(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 3:
        return "00-02m"
    if value < 6:
        return "03-05m"
    if value < 9:
        return "06-08m"
    if value < 12:
        return "09-11m"
    return "12-14m"


def _bucket_vol(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.15:
        return "<0.15"
    if value < 0.50:
        return "0.15-0.50"
    if value < 1.00:
        return "0.50-1.00"
    if value < 2.00:
        return "1.00-2.00"
    return ">=2.00"


def _bucket_edge(value: float | None) -> str:
    if value is None:
        return "unknown"
    value = abs(value)
    if value < 8:
        return "<8pp"
    if value < 15:
        return "08-15pp"
    if value < 25:
        return "15-25pp"
    if value < 40:
        return "25-40pp"
    return ">=40pp"


def _bucket_dist(value: float | None) -> str:
    if value is None:
        return "unknown"
    value = abs(value)
    if value < 25:
        return "<$25"
    if value < 50:
        return "$25-50"
    if value < 100:
        return "$50-100"
    if value < 200:
        return "$100-200"
    return ">=$200"


def _hour_bucket(placed_at: str) -> str:
    dt = _parse_dt(placed_at)
    if dt is None:
        return "unknown"
    return f"{dt.weekday()}:{dt.hour:02d}Z"


def _get_schema(cur: sqlite3.Cursor, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _load_trade_mfe(cur: sqlite3.Cursor) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    if not _get_schema(cur, "trade_decisions"):
        return {}
    out: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for ticker, side, mfe, mae in cur.execute(
        """
        SELECT ticker, entry_side, mfe_bid, mae_bid
        FROM trade_decisions
        WHERE ticker IS NOT NULL
        """
    ):
        out[(str(ticker), str(side or "").lower())] = (mfe, mae)
    return out


def _load_gate_meta(cur: sqlite3.Cursor) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not _get_schema(cur, "gate_decisions"):
        return {}
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in cur.execute(
        """
        SELECT ts_ms, ticker, side, session_min, entry_cents, snapshot_json
        FROM gate_decisions
        WHERE ticker IS NOT NULL
          AND gate IN ('bb_pure_signal', 'entry_filter')
        ORDER BY ts_ms ASC
        """
    ):
        ticker, side = str(row[1] or ""), str(row[2] or "").lower()
        if not ticker:
            continue
        out[(ticker, side)].append(
            {
                "ts_ms": int(row[0] or 0),
                "session_min": _as_float(row[3]),
                "entry_cents": row[4],
                "snapshot": _parse_json(row[5]),
            }
        )
    return out


def _nearest_gate(
    meta: dict[tuple[str, str], list[dict[str, Any]]],
    ticker: str,
    side: str,
    created_ms: int | None,
) -> dict[str, Any] | None:
    rows = meta.get((ticker, side)) or meta.get((ticker, ""))
    if not rows:
        return None
    if created_ms is None:
        return rows[-1]
    # Gate rows can repeat every cycle; pick the closest one before/near the
    # trade write. A wide tolerance keeps old trades from cross-linking.
    best = min(rows, key=lambda r: abs(int(r.get("ts_ms") or 0) - created_ms))
    if abs(int(best.get("ts_ms") or 0) - created_ms) > 20 * 60 * 1000:
        return None
    return best


def load_engine_trades(db_path: Path, since: str | None) -> list[TradeRow]:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=15000")
    mfe_by_trade = _load_trade_mfe(cur)
    gate_meta = _load_gate_meta(cur)
    params: list[Any] = []
    where = "WHERE COALESCE(pnl, 0) IS NOT NULL"
    if since:
        where += " AND placed_at >= ?"
        params.append(since)
    rows = cur.execute(
        f"""
        SELECT placed_at, ticker, side, count, limit_price, pnl, status, result,
               created_at, strategy_name, ladder_detail
        FROM kalshi_trades
        {where}
        ORDER BY placed_at ASC
        """,
        params,
    ).fetchall()
    conn.close()

    trades: list[TradeRow] = []
    for row in rows:
        side = str(row["side"] or "").lower()
        ticker = str(row["ticker"] or "")
        created_ms = row["created_at"]
        if not created_ms:
            dt = _parse_dt(str(row["placed_at"] or ""))
            created_ms = int(dt.timestamp() * 1000) if dt else None
        created_ms = int(created_ms) if created_ms else None
        mfe, mae = mfe_by_trade.get((ticker, side), (None, None))
        gate = _nearest_gate(gate_meta, ticker, side, created_ms)
        trades.append(
            TradeRow(
                placed_at=str(row["placed_at"] or ""),
                ticker=ticker,
                side=side,
                count=int(row["count"] or 0),
                entry_c=int(row["limit_price"] or 0),
                pnl_dollars=float(row["pnl"] or 0.0),
                status=str(row["status"] or ""),
                result=row["result"],
                strategy=row["strategy_name"],
                ladder=_parse_json(row["ladder_detail"]),
                created_ms=created_ms,
                gate_session_min=_as_float(gate.get("session_min")) if gate else None,
                gate_entry_c=int(gate["entry_cents"]) if gate and gate.get("entry_cents") is not None else None,
                gate_snapshot=gate.get("snapshot") if gate else None,
                mfe_c=mfe,
                mae_c=mae,
            )
        )
    return trades


def load_manual_fills(db_path: Path, since_ms: int | None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=15000")
    where = "WHERE action = 'buy'"
    params: list[Any] = []
    if since_ms is not None:
        where += " AND created_at_ms >= ?"
        params.append(since_ms)
    try:
        rows = cur.execute(
            f"""
            SELECT ticker, side, action, count, price_cents, fee_cents,
                   created_at_ms, session_minute, seconds_to_expiry,
                   dist_from_strike_pct, cb_volatility, bb_fvg,
                   regime, pnl_cents, market_result
            FROM manual_fills
            {where}
            ORDER BY created_at_ms ASC
            """,
            params,
        ).fetchall()
    except sqlite3.Error:
        rows = []
    conn.close()
    return rows


def _summarize(rows: Iterable[TradeRow]) -> dict[str, float]:
    data = list(rows)
    n = len(data)
    if not n:
        return {"n": 0, "wins": 0, "wr": math.nan, "pnl_c": 0, "avg_c": math.nan}
    wins = sum(1 for r in data if r.is_win)
    pnl_c = sum(r.pnl_cents for r in data)
    return {
        "n": n,
        "wins": wins,
        "wr": wins / n,
        "pnl_c": pnl_c,
        "avg_c": pnl_c / n,
    }


def _group_table(trades: list[TradeRow], label: str, key_fn) -> list[str]:
    grouped: dict[str, list[TradeRow]] = defaultdict(list)
    for trade in trades:
        grouped[key_fn(trade)].append(trade)
    lines = [f"### {label}", "", "| Bucket | N | WR | Net | Avg/trade | Cat losses |", "|---|---:|---:|---:|---:|---:|"]
    for bucket, rows in sorted(grouped.items()):
        stats = _summarize(rows)
        cat = sum(1 for r in rows if r.pnl_cents <= -3000)
        lines.append(
            f"| {bucket} | {int(stats['n'])} | {_fmt_pct(stats['wr'])} | "
            f"{_fmt_money(stats['pnl_c'])} | {_fmt_money(stats['avg_c'])} | {cat} |"
        )
    lines.append("")
    return lines


def _rule_delta(trades: list[TradeRow], name: str, keep_fn) -> str:
    kept = [t for t in trades if keep_fn(t)]
    skipped = [t for t in trades if not keep_fn(t)]
    base = _summarize(trades)
    kept_s = _summarize(kept)
    avoided_loss_c = -sum(t.pnl_cents for t in skipped if t.pnl_cents < 0)
    skipped_gain_c = sum(t.pnl_cents for t in skipped if t.pnl_cents > 0)
    delta_c = avoided_loss_c - skipped_gain_c
    return (
        f"| {name} | {int(base['n'])} | {len(kept)} | {_fmt_money(delta_c)} | "
        f"{_fmt_pct(kept_s['wr'])} | {_fmt_money(kept_s['pnl_c'])} |"
    )


def _mfe_section(trades: list[TradeRow]) -> list[str]:
    rows = [t for t in trades if t.mfe_c is not None and t.mae_c is not None]
    lines = ["## MFE / MAE Coverage", ""]
    if not rows:
        lines += [
            "No usable `trade_decisions.mfe_bid` / `mae_bid` rows were found for the selected engine trades.",
            "Phase 1 should add or backfill excursion capture before calibrating the MFE-aware trail.",
            "",
        ]
        return lines
    captures = []
    for trade in rows:
        if trade.mfe_c and trade.mfe_c > 0 and trade.pnl_cents > 0 and trade.count > 0:
            realized_c_per_contract = trade.pnl_cents / max(trade.count, 1)
            captures.append(max(0.0, min(realized_c_per_contract / trade.mfe_c, 2.0)))
    avg_capture = sum(captures) / len(captures) if captures else math.nan
    lines += [
        f"- Trades with excursion fields: {len(rows)}",
        f"- Positive-trade average MFE capture ratio: {_fmt_pct(avg_capture)}",
        f"- Worst MAE: {min((t.mae_c or 0) for t in rows)}c",
        "",
    ]
    return lines


def build_report(trades: list[TradeRow], manual_rows: list[sqlite3.Row], since: str | None) -> str:
    strategies = defaultdict(list)
    for trade in trades:
        strategies[str(trade.strategy or "unknown")].append(trade)

    lines: list[str] = [
        "# Alpha Extraction Report",
        "",
        f"- Generated UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Since filter: `{since or 'none'}`",
        f"- Engine trades analyzed: {len(trades)}",
        f"- Manual buy fills analyzed: {len(manual_rows)}",
        "",
        "## Executive Summary",
        "",
    ]
    all_stats = _summarize(trades)
    lines += [
        f"- Engine net: {_fmt_money(all_stats['pnl_c'])} across {int(all_stats['n'])} trades, WR {_fmt_pct(all_stats['wr'])}.",
        "- Treat this as an entry/exit diagnostic, not account-truth P&L. Account truth remains Kalshi balance snapshots.",
        "- Candidate rules below show retrospective deltas only; they are filters to validate, not proof of forward edge.",
        "",
    ]

    lines += ["## Strategy Totals", "", "| Strategy | N | WR | Net | Avg/trade |", "|---|---:|---:|---:|---:|"]
    for strategy, rows in sorted(strategies.items()):
        s = _summarize(rows)
        lines.append(f"| {strategy} | {int(s['n'])} | {_fmt_pct(s['wr'])} | {_fmt_money(s['pnl_c'])} | {_fmt_money(s['avg_c'])} |")
    lines.append("")

    lines += ["## Engine Buckets", ""]
    lines += _group_table(trades, "Entry Price", lambda t: _bucket_entry(t.entry_c))
    lines += _group_table(trades, "Gate Session Minute", lambda t: _bucket_session_min(_trade_session_min(t)))
    lines += _group_table(trades, "Regime", lambda t: str(t.ladder.get("regime") or t.ladder.get("mtf_regime") or "unknown").lower())
    lines += _group_table(trades, "Regime Volatility", lambda t: _bucket_vol(_as_float(t.ladder.get("regime_vol_bps"))))
    lines += _group_table(trades, "Raw Edge", lambda t: _bucket_edge(_as_float(t.ladder.get("raw_edge_pp"))))
    lines += _group_table(trades, "BTC 5m Distance/Momentum", lambda t: _bucket_dist(_as_float(t.ladder.get("regime_btc_5m_abs"))))
    lines += _group_table(trades, "UTC Weekday:Hour", lambda t: _hour_bucket(t.placed_at))

    lines += ["## Candidate Retrospective Filters", "", "| Rule | Base N | Kept N | Retrospective Delta | Kept WR | Kept Net |", "|---|---:|---:|---:|---:|---:|"]
    lines.append(_rule_delta(trades, "Keep entry <= 35c", lambda t: t.entry_c <= 35))
    lines.append(_rule_delta(trades, "Keep entry <= 50c", lambda t: t.entry_c <= 50))
    lines.append(_rule_delta(trades, "Block entry 51-65c", lambda t: not (51 <= t.entry_c <= 65)))
    lines.append(_rule_delta(trades, "Block session minute >= 9", lambda t: (_trade_session_min(t) is None) or (_trade_session_min(t) < 9)))
    lines.append(_rule_delta(trades, "Block vol >= 1.0", lambda t: (_as_float(t.ladder.get("regime_vol_bps")) is None) or (_as_float(t.ladder.get("regime_vol_bps")) < 1.0)))
    lines.append("")

    lines += _mfe_section(trades)
    lines += _manual_section(manual_rows)
    lines += [
        "## Next Implementation Decision",
        "",
        "Use this report to decide whether Phase 2 cheap-side gating is backed by data.",
        "If MFE coverage is missing or sparse, implement/backfill excursion tracking before changing trailing exits.",
        "",
    ]
    return "\n".join(lines)


def _manual_section(rows: list[sqlite3.Row]) -> list[str]:
    lines = ["## Manual Fill Context", ""]
    if not rows:
        return lines + ["No manual buy fills found for the selected window.", ""]
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[_bucket_entry(row["price_cents"])].append(row)
    lines += ["| Entry bucket | Fills | Contracts | Avg price | Settled PnL |", "|---|---:|---:|---:|---:|"]
    for bucket, vals in sorted(grouped.items()):
        contracts = sum(int(v["count"] or 0) for v in vals)
        avg_px = sum(float(v["price_cents"] or 0) for v in vals) / len(vals)
        pnl = sum(int(v["pnl_cents"] or 0) for v in vals if v["pnl_cents"] is not None)
        lines.append(f"| {bucket} | {len(vals)} | {contracts} | {avg_px:.1f}c | {_fmt_money(pnl)} |")
    lines.append("")
    return lines


def _trade_session_min(trade: TradeRow) -> float | None:
    return (
        trade.gate_session_min
        if trade.gate_session_min is not None
        else _as_float(trade.ladder.get("session_min") or trade.ladder.get("session_minute"))
    )


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _since_to_ms(since: str | None) -> int | None:
    if not since:
        return None
    dt = _parse_dt(since)
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since", help="ISO UTC lower bound, e.g. 2026-04-29")
    parser.add_argument("--out", type=Path, help="Markdown output path")
    args = parser.parse_args()

    since = args.since
    if since and "T" not in since:
        since = since + "T00:00:00+00:00"

    trades = load_engine_trades(args.db, since)
    manual = load_manual_fills(args.db, _since_to_ms(since))
    report = build_report(trades, manual, since)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
