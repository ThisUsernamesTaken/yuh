"""Best-effort BB_PURE trade reconstruction from engine_history.log.

This is read-only. It does not call Kalshi or import the live engine. The log
does not always include ticker on exits, so tickerless exits are assigned to
the active BB_PURE position in log order and the report surfaces anomalies.
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "engine_history.log"

TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
FIRE_RE = re.compile(
    r"BB_PURE FIRE: (?P<side>YES|NO) (?P<ticker>\S+) (?P<count>\d+)x @ "
    r"(?P<entry>\d+)c .*?edge=(?P<edge>[-.\d]+)pp fair=(?P<fair>\d+)c "
    r"market=(?P<market>\d+)c kelly=(?P<kelly>[-.\d]+)(?: tier=(?P<tier>\d+))?"
)
FILL_RE = re.compile(
    r"BB_PURE FILL: (?P<side>YES|NO) (?P<ticker>\S+) (?P<count>\d+)x @ "
    r"(?P<entry>\d+)c .*?order=(?P<order>\S+)"
)
SELL_TIER_RE = re.compile(r"SELL TIER FILLED: (?P<count>\d+)x @ (?P<price>\d+)c \(\+(?P<delta>-?\d+)c\)")
TRAIL_RE = re.compile(
    r"TRAIL FIRED: (?P<count>\d+)x @ ~(?P<price>\d+)c "
    r"\(entry=(?P<entry>\d+)c floor=(?P<floor>\d+)c hwm=(?P<hwm>\d+)c\)"
)
RESIDUAL_RE = re.compile(r"RESIDUAL-CLEAN: (?P<ticker>\S+) \((?P<state>[^,]+), reason=(?P<reason>[^)]+)\)")
MANUAL_TP_RE = re.compile(r"MANUAL TP PLACED: (?P<ticker>\S+) (?P<count>\d+)x (?P<side>YES|NO) @ (?P<price>\d+)c")
ANOMALY_PATTERNS = ("OVERSELL", "STUCK-RESIDUAL", "FILL OVERRUN", "SYNC RECONCILE", "SYNC RECLAIM")


@dataclass
class Event:
    ts: datetime
    kind: str
    ticker: str = ""
    side: str = ""
    count: int = 0
    price: int = 0
    entry: int = 0
    edge: float | None = None
    fair: int | None = None
    market: int | None = None
    kelly: float | None = None
    tier: int | None = None
    reason: str = ""
    raw: str = ""


@dataclass
class Trade:
    entry_ts: datetime
    ticker: str
    side: str
    entry_count: int
    entry_price: int
    order_id: str = ""
    edge: float | None = None
    fair: int | None = None
    market: int | None = None
    kelly: float | None = None
    tier: int | None = None
    exit_count: int = 0
    exit_value_c: int = 0
    exit_ts: datetime | None = None
    max_exit_price: int = 0
    hwm: int | None = None
    exit_reasons: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.exit_count >= self.entry_count

    @property
    def avg_exit_price(self) -> float | None:
        if self.exit_count <= 0:
            return None
        return self.exit_value_c / self.exit_count

    @property
    def pnl_cents(self) -> int:
        sold = min(self.exit_count, self.entry_count)
        realized = self.exit_value_c
        basis = sold * self.entry_price
        return int(realized - basis)

    @property
    def mfe_cents(self) -> int | None:
        if self.hwm is not None:
            return max(0, self.hwm - self.entry_price)
        if self.max_exit_price:
            return max(0, self.max_exit_price - self.entry_price)
        return None

    @property
    def capture_ratio(self) -> float | None:
        mfe = self.mfe_cents
        avg = self.avg_exit_price
        if not mfe or mfe <= 0 or avg is None:
            return None
        return max(0.0, min((avg - self.entry_price) / mfe, 2.0))


def parse_ts(line: str) -> datetime | None:
    match = TS_RE.search(line)
    if not match:
        return None
    return datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")


def parse_events(lines: Iterable[str], since: datetime | None = None) -> list[Event]:
    events: list[Event] = []
    last_fire_by_ticker: dict[str, Event] = {}
    for raw in lines:
        ts = parse_ts(raw)
        if ts is None or (since and ts < since):
            continue
        if "BB_PURE FIRE:" in raw:
            m = FIRE_RE.search(raw)
            if m:
                ev = Event(
                    ts=ts, kind="fire", ticker=m.group("ticker"), side=m.group("side").lower(),
                    count=int(m.group("count")), entry=int(m.group("entry")),
                    edge=float(m.group("edge")), fair=int(m.group("fair")),
                    market=int(m.group("market")), kelly=float(m.group("kelly")),
                    tier=int(m.group("tier")) if m.group("tier") else None, raw=raw.strip(),
                )
                last_fire_by_ticker[ev.ticker] = ev
                events.append(ev)
            continue
        if "BB_PURE FILL:" in raw:
            m = FILL_RE.search(raw)
            if m:
                ticker = m.group("ticker")
                fire = last_fire_by_ticker.get(ticker)
                ev = Event(
                    ts=ts, kind="fill", ticker=ticker, side=m.group("side").lower(),
                    count=int(m.group("count")), entry=int(m.group("entry")),
                    reason=m.group("order"), raw=raw.strip(),
                )
                if fire:
                    ev.edge, ev.fair, ev.market, ev.kelly, ev.tier = fire.edge, fire.fair, fire.market, fire.kelly, fire.tier
                events.append(ev)
            continue
        if "SELL TIER FILLED:" in raw:
            m = SELL_TIER_RE.search(raw)
            if m:
                events.append(Event(ts=ts, kind="exit", count=int(m.group("count")), price=int(m.group("price")), reason="sell_tier", raw=raw.strip()))
            continue
        if "TRAIL FIRED:" in raw:
            m = TRAIL_RE.search(raw)
            if m:
                events.append(Event(ts=ts, kind="exit", count=int(m.group("count")), price=int(m.group("price")), entry=int(m.group("entry")), reason="trail", fair=int(m.group("floor")), market=int(m.group("hwm")), raw=raw.strip()))
            continue
        if "RESIDUAL-CLEAN:" in raw:
            m = RESIDUAL_RE.search(raw)
            if m:
                events.append(Event(ts=ts, kind="residual", ticker=m.group("ticker"), reason=m.group("reason"), raw=raw.strip()))
            continue
        if "MANUAL TP PLACED:" in raw:
            m = MANUAL_TP_RE.search(raw)
            if m:
                events.append(Event(ts=ts, kind="manual_tp", ticker=m.group("ticker"), side=m.group("side").lower(), count=int(m.group("count")), price=int(m.group("price")), reason="manual_tp", raw=raw.strip()))
            continue
        if "BB_PURE" in raw or any(p in raw for p in ANOMALY_PATTERNS):
            if any(p in raw for p in ANOMALY_PATTERNS):
                events.append(Event(ts=ts, kind="anomaly", raw=raw.strip()))
    return events


def reconstruct_trades(events: Iterable[Event]) -> list[Trade]:
    trades: list[Trade] = []
    active: Trade | None = None
    by_ticker: dict[str, Trade] = {}

    for ev in events:
        if ev.kind == "fill":
            trade = Trade(
                entry_ts=ev.ts,
                ticker=ev.ticker,
                side=ev.side,
                entry_count=ev.count,
                entry_price=ev.entry,
                order_id=ev.reason,
                edge=ev.edge,
                fair=ev.fair,
                market=ev.market,
                kelly=ev.kelly,
                tier=ev.tier,
            )
            trades.append(trade)
            active = trade
            by_ticker[trade.ticker] = trade
            continue

        if ev.kind in ("exit", "manual_tp"):
            trade = by_ticker.get(ev.ticker) if ev.ticker else active
            if trade is None:
                continue
            qty = min(ev.count, max(0, trade.entry_count - trade.exit_count)) if ev.count else 0
            if qty <= 0:
                trade.anomalies.append(f"{ev.kind}:over_exit:{ev.count}")
                continue
            trade.exit_count += qty
            trade.exit_value_c += qty * ev.price
            trade.exit_ts = ev.ts
            trade.max_exit_price = max(trade.max_exit_price, ev.price)
            if ev.reason == "trail" and ev.market:
                trade.hwm = max(trade.hwm or 0, ev.market)
            trade.exit_reasons.append(ev.reason)
            if trade.is_closed and active is trade:
                active = None
            continue

        if ev.kind == "residual":
            trade = by_ticker.get(ev.ticker)
            if trade:
                trade.exit_ts = ev.ts
                trade.exit_reasons.append(f"residual:{ev.reason}")
                if active is trade:
                    active = None
            continue

        if ev.kind == "anomaly":
            target = active
            if target is not None:
                target.anomalies.append(ev.raw[:180])

    return trades


def summarize(trades: list[Trade]) -> str:
    closed = [t for t in trades if t.exit_count > 0]
    complete = [t for t in closed if t.is_closed]
    pnl = sum(t.pnl_cents for t in closed)
    winners = sum(1 for t in closed if t.pnl_cents > 0)
    captures = [t.capture_ratio for t in closed if t.capture_ratio is not None]
    avg_capture = sum(captures) / len(captures) if captures else None
    lines = [
        "# BB_PURE Log-Mined Trades",
        "",
        f"- Reconstructed entries: {len(trades)}",
        f"- Trades with exits: {len(closed)}",
        f"- Fully closed by parsed exits: {len(complete)}",
        f"- Parsed-exit net: ${pnl / 100.0:.2f}",
        f"- Parsed-exit WR: {(winners / len(closed) * 100.0):.1f}%" if closed else "- Parsed-exit WR: n/a",
        f"- Avg MFE capture: {(avg_capture * 100.0):.1f}%" if avg_capture is not None else "- Avg MFE capture: n/a",
        "",
        "| Entry ts | Ticker | Side | Entry | Count | Exit count | Avg exit | PnL | MFE | Capture | Reasons | Anomalies |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for t in trades:
        avg_exit = f"{t.avg_exit_price:.1f}c" if t.avg_exit_price is not None else ""
        capture = f"{t.capture_ratio * 100.0:.1f}%" if t.capture_ratio is not None else ""
        mfe = f"{t.mfe_cents}c" if t.mfe_cents is not None else ""
        lines.append(
            f"| {t.entry_ts} | {t.ticker} | {t.side.upper()} | {t.entry_price}c | "
            f"{t.entry_count} | {t.exit_count} | {avg_exit} | ${t.pnl_cents / 100.0:.2f} | "
            f"{mfe} | {capture} | {', '.join(t.exit_reasons)} | {len(t.anomalies)} |"
        )
    lines.append("")
    lines.append("Note: tickerless exits are assigned to the active BB_PURE trade in log order; use this as calibration evidence, not accounting truth.")
    return "\n".join(lines)


def write_csv(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["entry_ts", "ticker", "side", "entry_price", "entry_count", "exit_count", "avg_exit_price", "pnl_cents", "mfe_cents", "capture_ratio", "reasons", "anomaly_count"])
        for t in trades:
            writer.writerow([t.entry_ts, t.ticker, t.side, t.entry_price, t.entry_count, t.exit_count, t.avg_exit_price, t.pnl_cents, t.mfe_cents, t.capture_ratio, ";".join(t.exit_reasons), len(t.anomalies)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--since", default="2026-04-30 00:00:00")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "bb_pure_trades_2026_05_01.md")
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S") if args.since else None
    with args.log.open("r", encoding="utf-8", errors="replace") as f:
        events = parse_events(f, since=since)
    trades = reconstruct_trades(events)
    report = summarize(trades)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n", encoding="utf-8")
    if args.csv:
        write_csv(args.csv, trades)
    print(f"events={len(events)} trades={len(trades)} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
