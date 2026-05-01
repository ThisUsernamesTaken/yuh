"""Search late-window KXBTC15M open/close-state strategies.

Uses local Kalshi window snapshots + settlement truth:
  - window_snapshots: market state near minute N
  - settlement_ledger: final YES/NO result

This searches simple expiry-prediction policies:
  - dominant: buy the side priced above 50c
  - underdog: buy the side priced below 50c
  - filters: minute, min dominant price, entry price band

Ranks strategies by fixed 100-contract P&L/expectancy, then reports full-port
compounding for the best rows. This avoids selecting only by path-dependent
all-in luck.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"


@dataclass(frozen=True)
class Candidate:
    minute: int
    mode: str
    min_dom: int
    min_entry: int
    max_entry: int
    tolerance_s: int = 45


@dataclass
class Result:
    c: Candidate
    n: int
    wins: int
    pnl_100ct: float
    avg_pnl_100ct: float
    roi_on_cost: float
    full_port_final: float
    full_port_min: float
    full_port_max: float


def _load(conn: sqlite3.Connection) -> dict[str, dict]:
    data: dict[str, dict] = {}
    for r in conn.execute("""
        SELECT w.ticker, w.t_offset_sec, w.kalshi_mid_cents,
               s.market_result
        FROM window_snapshots w
        JOIN settlement_ledger s ON s.ticker = w.ticker
        WHERE w.kalshi_mid_cents BETWEEN 1 AND 99
          AND s.market_result IN ('yes', 'no')
        ORDER BY w.ticker, w.t_offset_sec
    """):
        d = data.setdefault(r["ticker"], {
            "result": r["market_result"].lower(),
            "snaps": [],
        })
        d["snaps"].append((int(r["t_offset_sec"]), int(r["kalshi_mid_cents"])))
    return data


def _pick(snaps: list[tuple[int, int]], target_s: int,
          tol: int) -> tuple[int, int] | None:
    elig = [(off, mid) for off, mid in snaps if abs(off - target_s) <= tol]
    if not elig:
        return None
    before = [(off, mid) for off, mid in elig if off <= target_s]
    if before:
        return max(before, key=lambda x: x[0])
    return min(elig, key=lambda x: abs(x[0] - target_s))


def _trade_for(mid: int, result: str, c: Candidate) -> tuple[bool, int, str] | None:
    dom_side = "yes" if mid >= 50 else "no"
    dom_price = mid if dom_side == "yes" else 100 - mid
    if dom_price < c.min_dom:
        return None
    side = dom_side if c.mode == "dominant" else ("no" if dom_side == "yes" else "yes")
    price = mid if side == "yes" else 100 - mid
    if price < c.min_entry or price > c.max_entry:
        return None
    return result == side, price, side


def eval_candidate(data: dict[str, dict], c: Candidate,
                   start_balance: float = 100.0) -> Result:
    target_s = c.minute * 60
    pnl_100ct = 0.0
    cost = 0.0
    n = 0
    wins = 0
    balance = start_balance
    min_bal = start_balance
    max_bal = start_balance
    for ticker in sorted(data):
        snap = _pick(data[ticker]["snaps"], target_s, c.tolerance_s)
        if snap is None:
            continue
        _, mid = snap
        tr = _trade_for(mid, data[ticker]["result"], c)
        if tr is None:
            continue
        won, price, _side = tr
        n += 1
        wins += 1 if won else 0
        pnl_100ct += (100 - price) if won else -price
        cost += price
        contracts = int((balance * 100) // price)
        if contracts <= 0:
            break
        cash_left = balance - contracts * price / 100.0
        balance = cash_left + (contracts if won else 0.0)
        min_bal = min(min_bal, balance)
        max_bal = max(max_bal, balance)
        if balance <= 0:
            break
    avg = pnl_100ct / n if n else 0.0
    roi = pnl_100ct / cost * 100.0 if cost else 0.0
    return Result(c, n, wins, pnl_100ct, avg, roi, balance, min_bal, max_bal)


def search() -> list[Result]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    data = _load(conn)
    conn.close()
    candidates = []
    for minute in range(10, 15):
        for mode in ("dominant", "underdog"):
            for min_dom in (50, 55, 60, 65, 70, 75, 80):
                for min_entry, max_entry in (
                    (1, 99), (35, 99), (45, 99), (50, 99),
                    (1, 60), (35, 70), (45, 75), (50, 80),
                ):
                    candidates.append(Candidate(
                        minute=minute, mode=mode, min_dom=min_dom,
                        min_entry=min_entry, max_entry=max_entry,
                    ))
    results = [eval_candidate(data, c) for c in candidates]
    return [r for r in results if r.n >= 5]


def main() -> int:
    results = search()
    results.sort(key=lambda r: (r.avg_pnl_100ct, r.n), reverse=True)
    print("Late-state strategy search (fixed 100ct ranking)")
    print("Requires >=5 trades. PnL is cents per 100ct = dollars.")
    print()
    print(f"{'rank':>4} {'mode':<9} {'min':>3} {'dom>=':>5} "
          f"{'entry':>9} {'n':>3} {'WR':>6} {'pnl100':>8} "
          f"{'$/tr':>7} {'ROIcost':>8} {'fullPort':>9}")
    for i, r in enumerate(results[:25], 1):
        c = r.c
        wr = r.wins / r.n * 100.0
        print(f"{i:>4} {c.mode:<9} {c.minute:>3} {c.min_dom:>5} "
              f"{c.min_entry:>2}-{c.max_entry:<2} {r.n:>3} "
              f"{wr:>5.1f}% {r.pnl_100ct:>+8.2f} {r.avg_pnl_100ct:>+7.2f} "
              f"{r.roi_on_cost:>+7.1f}% ${r.full_port_final:>8.2f}")
    print()
    print("Best full-port final balances among >=5 trade candidates:")
    by_full = sorted(results, key=lambda r: r.full_port_final, reverse=True)
    for i, r in enumerate(by_full[:10], 1):
        c = r.c
        wr = r.wins / r.n * 100.0
        print(f"{i:>2}. {c.mode} minute={c.minute} dom>={c.min_dom} "
              f"entry={c.min_entry}-{c.max_entry} n={r.n} wr={wr:.1f}% "
              f"fixed100=${r.pnl_100ct:+.2f} full=${r.full_port_final:.2f} "
              f"range=${r.full_port_min:.2f}-${r.full_port_max:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
