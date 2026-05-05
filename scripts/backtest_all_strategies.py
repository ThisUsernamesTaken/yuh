"""Cross-strategy backtest on Kalshi 15m BTC contracts (2026-05-04).

Same methodology as ``backtest_bb_pure_variations.py`` extended to compare
multiple strategy evaluators side-by-side at multiple entry-time offsets.

Strategies tested:
  - **BB_PURE_MEANREV** : BB model fair vs mid edge, near-strike (≤0.04%)
  - **BB_TREND**        : BB model fair vs mid edge, far-from-strike (≥0.15%),
                          side must be with-trend (yes if BTC above strike)
  - **BB_MOMENTUM**     : Pure momentum-rider using btc_move_300s +
                          btc_move_30s (matches ``bb_momentum.evaluate_entry``)
  - **ATM_REVERSION**   : Near-strike discount entry with fixed fair=50c
                          (matches ``atm_reversion.evaluate``)

Each strategy is evaluated at entry offsets [60s, 300s, 600s] into the
15-min window so we can see how each strategy's optimal timing differs.

Methodology mirrors the BB_PURE harness:
  - Source: ``data/kalshi_external_backtest.db`` (2,809 finalized markets)
  - At entry time T, compute BTC + Kalshi mid, run strategy gates, simulate
    1-contract buy on signaled side, settle by ``markets.result``
  - 7% × p × (1−p) maker fee × 2 (round-trip)

Outputs:
  - Single comparison table across strategy × timing
  - Per-strategy bucket breakdown for the best timing
  - CSVs per (strategy, timing) in scripts/_backtest_results/
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"
OUT_DIR = REPO / "scripts" / "_backtest_results"


# ── Math ────────────────────────────────────────────────────────────────


def _ndtr(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bb_fair_yes_cents(btc: float, strike: float, secs_left: float, vol_per_sec: float) -> int:
    if secs_left <= 0:
        return 100 if btc >= strike else 0
    if vol_per_sec <= 0:
        return 50
    sigma = vol_per_sec * math.sqrt(secs_left)
    if sigma <= 0:
        return 100 if btc >= strike else 0
    z = (btc - strike) / sigma
    return int(round(100.0 * _ndtr(z)))


def kalshi_maker_fee_cents(price_cents: int, contracts: int) -> float:
    p = float(price_cents) / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return per_contract * contracts * 2


# ── Loaders (lifted from the BB_PURE harness) ──────────────────────────


def load_markets(con: sqlite3.Connection) -> list[dict]:
    cur = con.cursor()
    cur.execute(
        "SELECT ticker, open_ts, close_ts, result, raw_json FROM markets "
        "WHERE status='finalized' AND result IN ('yes','no')"
    )
    out: list[dict] = []
    for ticker, open_ts, close_ts, result, raw in cur.fetchall():
        try:
            j = json.loads(raw)
        except Exception:
            continue
        try:
            strike = float(j.get("floor_strike") or 0)
        except Exception:
            strike = 0.0
        if strike <= 0 or open_ts is None or close_ts is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "open_ts": int(open_ts),
                "close_ts": int(close_ts),
                "result": (result or "").lower(),
                "strike": strike,
            }
        )
    return out


def build_btc_index(con: sqlite3.Connection) -> dict[int, float]:
    cur = con.cursor()
    cur.execute("SELECT open_ts, close FROM btc_1m WHERE close > 0")
    return {int(ts): float(c) for ts, c in cur.fetchall()}


def build_candle_index(con: sqlite3.Connection) -> dict[str, list[tuple]]:
    cur = con.cursor()
    cur.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close, "
        "price_close FROM candles ORDER BY ticker, end_period_ts"
    )
    idx: dict[str, list[tuple]] = defaultdict(list)
    for tkr, ts, yb, ya, pc in cur.fetchall():
        idx[tkr].append((int(ts), yb, ya, pc))
    return idx


def vol_per_sec(btc_index: dict[int, float], around_ts: int, lookback_min: int = 60) -> float:
    closes: list[float] = []
    for k in range(lookback_min):
        ts = around_ts - 60 * (k + 1)
        ts = (ts // 60) * 60
        c = btc_index.get(ts)
        if c is not None and c > 0:
            closes.append(c)
    if len(closes) < 5:
        return 0.0
    closes.reverse()
    rets = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) / math.sqrt(60.0)


def kalshi_yes_at(candles: list[tuple], target_ts: int) -> tuple[int, int, int] | None:
    """Return (yes_mid_c, yes_bid_c, yes_ask_c) at the most recent
    candle ≤ target_ts."""
    best = None
    for ts, yb, ya, pc in candles:
        if ts > target_ts:
            break
        best = (ts, yb, ya, pc)
    if best is None:
        return None
    _ts, yb, ya, pc = best
    if yb is not None and ya is not None and yb > 0 and ya > 0:
        bid = int(round(float(yb) * 100))
        ask = int(round(float(ya) * 100))
        mid = (bid + ask) // 2
    elif pc is not None and pc > 0:
        mid = int(round(float(pc) * 100))
        bid = mid
        ask = mid
    else:
        return None
    if mid <= 0 or mid >= 100:
        return None
    return mid, bid, ask


def btc_move_dollars(btc_index: dict[int, float], at_ts: int, lookback_s: int) -> float:
    now_min = (at_ts // 60) * 60
    p_now = btc_index.get(now_min)
    p_then = btc_index.get(now_min - lookback_s)
    if p_now is None or p_then is None:
        return 0.0
    return p_now - p_then


# ── TradeRecord ─────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    strategy: str
    timing_label: str
    ticker: str
    side: str
    entry_c: int
    btc: float
    strike: float
    settle_yes: bool
    won: bool
    gross_c: float
    fee_c: float
    net_c: float
    extra: dict = field(default_factory=dict)


def settle_trade(side: str, entry_c: int, settle_yes: bool, *, strategy: str, timing: str,
                 ticker: str, btc: float, strike: float, extra: dict | None = None) -> TradeRecord:
    won = (side == "yes" and settle_yes) or (side == "no" and not settle_yes)
    gross = (100 - entry_c) if won else -entry_c
    fee = kalshi_maker_fee_cents(entry_c, 1)
    net = gross - fee
    return TradeRecord(
        strategy=strategy, timing_label=timing,
        ticker=ticker, side=side, entry_c=entry_c, btc=btc, strike=strike,
        settle_yes=settle_yes, won=won,
        gross_c=float(gross), fee_c=float(fee), net_c=float(net),
        extra=extra or {},
    )


# ── Strategy evaluators ─────────────────────────────────────────────────


def eval_bb_pure_meanrev(
    market: dict, btc: float, secs_left: float, mid_c: int, _bid: int, _ask: int,
    btc_index: dict[int, float], entry_ts: int, *,
    min_edge_pp: int = 8, max_dist_pct: float = 0.0004,
    min_entry: int = 5, max_entry: int = 55,
    require_alignment: bool = True,
) -> tuple[str, int] | None:
    dist = abs(btc - market["strike"]) / btc
    if dist > max_dist_pct:
        return None
    vps = vol_per_sec(btc_index, entry_ts, lookback_min=60)
    if vps <= 0:
        return None
    fair = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    edge_yes = fair - mid_c
    if abs(edge_yes) < min_edge_pp:
        return None
    if edge_yes > 0:
        side = "yes"
        entry_c = mid_c
    else:
        side = "no"
        entry_c = 100 - mid_c
    if entry_c < min_entry or entry_c > max_entry:
        return None
    if require_alignment:
        # Block contrarian: cheap side must align with BTC 5min trend.
        delta = btc_move_dollars(btc_index, entry_ts, 300)
        if abs(delta) > 5:
            aligned = "yes" if delta > 0 else "no"
            if side != aligned:
                return None
    return side, entry_c


def eval_bb_trend(
    market: dict, btc: float, secs_left: float, mid_c: int, _bid: int, _ask: int,
    btc_index: dict[int, float], entry_ts: int, *,
    min_edge_pp: int = 8, min_dist_pct: float = 0.0015,
    min_entry: int = 5, max_entry: int = 80,
    block_strong_reversal_dollars: float = 50.0,
) -> tuple[str, int] | None:
    """BB_TREND: fire on the with-trend side when BTC is firmly past strike."""
    dist = abs(btc - market["strike"]) / btc
    if dist < min_dist_pct:
        return None
    btc_above = btc > market["strike"]
    # Reversal block: if BTC is above strike but moved down sharply in last 5m,
    # don't fire YES (the trend is reversing).
    move_5m = btc_move_dollars(btc_index, entry_ts, 300)
    if btc_above and move_5m <= -block_strong_reversal_dollars:
        return None
    if (not btc_above) and move_5m >= block_strong_reversal_dollars:
        return None
    side = "yes" if btc_above else "no"
    entry_c = mid_c if side == "yes" else (100 - mid_c)
    # BB-edge requirement on with-trend side: fair vs market.
    vps = vol_per_sec(btc_index, entry_ts, lookback_min=60)
    if vps <= 0:
        return None
    fair_yes = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    fair_for_side = fair_yes if side == "yes" else (100 - fair_yes)
    edge = fair_for_side - entry_c
    if edge < min_edge_pp:
        return None
    if entry_c < min_entry or entry_c > max_entry:
        return None
    return side, entry_c


def eval_bb_momentum(
    market: dict, btc: float, secs_left: float, mid_c: int, _bid: int, _ask: int,
    btc_index: dict[int, float], entry_ts: int, *,
    min_300s_dollars: float = 15.0, min_30s_dollars: float = 5.0,
    min_entry: int = 5, max_entry: int = 50,
    min_time_remaining_s: float = 90.0,
) -> tuple[str, int] | None:
    """Momentum: sustained directional BTC dollar-velocity, buy with trend."""
    if secs_left < min_time_remaining_s:
        return None
    move_300s = btc_move_dollars(btc_index, entry_ts, 300)
    move_30s = btc_move_dollars(btc_index, entry_ts, 30)
    if abs(move_300s) < min_300s_dollars:
        return None
    if abs(move_30s) < min_30s_dollars:
        return None
    if (move_300s > 0) != (move_30s > 0):
        return None
    side = "yes" if move_300s > 0 else "no"
    entry_c = mid_c if side == "yes" else (100 - mid_c)
    if entry_c < min_entry or entry_c > max_entry:
        return None
    return side, entry_c


def eval_full_stack(
    market: dict, btc: float, secs_left: float, mid_c: int, bid: int, ask: int,
    btc_index: dict[int, float], entry_ts: int,
) -> tuple[str, int] | None:
    """Composite: BB_TREND if firmly past strike, else BB_PURE_MEANREV.

    The two strategies are designed for complementary regimes — TREND for
    >=0.15% past strike, MEANREV for <=0.04%. The 0.04-0.15 no-man's-land
    gives no signal.
    """
    dist = abs(btc - market["strike"]) / btc
    if dist >= 0.0015:
        return eval_bb_trend(market, btc, secs_left, mid_c, bid, ask, btc_index, entry_ts)
    if dist <= 0.0004:
        return eval_bb_pure_meanrev(market, btc, secs_left, mid_c, bid, ask, btc_index, entry_ts)
    return None


def eval_atm_reversion(
    market: dict, btc: float, secs_left: float, mid_c: int, bid: int, ask: int,
    btc_index: dict[int, float], entry_ts: int, *,
    max_dist_pct: float = 0.0003, max_entry_c: int = 35,
    min_fair_c: float = 47.0, min_edge_c: float = 8.0,
) -> tuple[str, int] | None:
    """ATM_REVERSION: near-strike discount entry, fair=50c (pin assumption)."""
    dist = abs(btc - market["strike"]) / btc
    if dist > max_dist_pct:
        return None
    if bid <= 0 or ask <= 0 or ask >= 100:
        return None
    yes_ask = ask
    no_ask = 100 - bid
    if no_ask <= 0:
        return None
    fair_yes = 50.0  # ATM pin assumption
    fair_no = 50.0
    yes_edge = fair_yes - yes_ask
    no_edge = fair_no - no_ask
    yes_disc = yes_ask <= max_entry_c and fair_yes >= min_fair_c and yes_edge >= min_edge_c
    no_disc = no_ask <= max_entry_c and fair_no >= min_fair_c and no_edge >= min_edge_c
    if yes_disc and (not no_disc or yes_edge >= no_edge):
        return "yes", yes_ask
    if no_disc:
        return "no", no_ask
    return None


# ── Runner ──────────────────────────────────────────────────────────────


STRATEGIES = {
    "BB_PURE_MEANREV": eval_bb_pure_meanrev,
    "BB_TREND":        eval_bb_trend,
    "BB_MOMENTUM":     eval_bb_momentum,
    "ATM_REVERSION":   eval_atm_reversion,
    "FULL_STACK":      eval_full_stack,
}

TIMINGS = [
    ("T060", 60),
    ("T300", 300),
    ("T600", 600),
]


def run_combo(
    strategy: str, evaluator, timing_label: str, offset_s: int,
    markets: list[dict],
    btc_index: dict[int, float],
    candle_idx: dict[str, list[tuple]],
) -> list[TradeRecord]:
    out: list[TradeRecord] = []
    for m in markets:
        entry_ts = m["open_ts"] + offset_s
        if entry_ts >= m["close_ts"]:
            continue
        btc_min = (entry_ts // 60) * 60
        btc = btc_index.get(btc_min)
        if not btc or btc <= 0:
            continue
        secs_left = m["close_ts"] - entry_ts
        candles = candle_idx.get(m["ticker"]) or []
        kp = kalshi_yes_at(candles, entry_ts)
        if not kp:
            continue
        mid, bid, ask = kp
        result = evaluator(
            m, btc, float(secs_left), mid, bid, ask, btc_index, entry_ts
        )
        if not result:
            continue
        side, entry_c = result
        out.append(
            settle_trade(
                side, entry_c, m["result"] == "yes",
                strategy=strategy, timing=timing_label,
                ticker=m["ticker"], btc=btc, strike=m["strike"],
            )
        )
    return out


def summarise(label: str, trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"label": label, "n": 0, "wins": 0, "hit": 0.0, "total": 0.0,
                "avg": 0.0, "max_dd": 0.0}
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    total_c = sum(t.net_c for t in trades)
    avg_c = total_c / n
    eq = peak = 0.0
    dd = 0.0
    for t in trades:
        eq += t.net_c
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "label": label, "n": n, "wins": wins, "losses": n - wins,
        "hit": (wins / n) * 100, "total": total_c / 100.0,
        "avg": avg_c / 100.0, "max_dd": dd / 100.0,
    }


def bucket_breakdown(trades: list[TradeRecord]) -> list[dict]:
    buckets = [(5, 19), (20, 29), (30, 39), (40, 49), (50, 55),
               (56, 65), (66, 79), (80, 95)]
    out = []
    for lo, hi in buckets:
        sub = [t for t in trades if lo <= t.entry_c <= hi]
        if not sub:
            continue
        wins = sum(1 for t in sub if t.won)
        total = sum(t.net_c for t in sub)
        out.append({
            "bucket": f"{lo}-{hi}c", "n": len(sub),
            "hit": wins / len(sub) * 100, "total": total / 100.0,
            "avg": total / len(sub) / 100.0,
        })
    return out


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1
    OUT_DIR.mkdir(exist_ok=True, parents=True)

    print("Loading data...", flush=True)
    con = sqlite3.connect(str(DB))
    markets = load_markets(con)
    btc_index = build_btc_index(con)
    candle_idx = build_candle_index(con)
    con.close()
    print(f"  {len(markets):,} markets / {len(btc_index):,} BTC mins / "
          f"{len(candle_idx):,} candle tickers")
    print()

    all_results: dict[str, list[TradeRecord]] = {}
    summaries: list[dict] = []

    for sname, evaluator in STRATEGIES.items():
        for tname, offset in TIMINGS:
            label = f"{sname}@{tname}"
            trades = run_combo(sname, evaluator, tname, offset,
                               markets, btc_index, candle_idx)
            all_results[label] = trades
            s = summarise(label, trades)
            summaries.append(s)

    # Sort by total $, n>=30 only.
    print("=" * 95)
    print("CROSS-STRATEGY BACKTEST — STRATEGY × TIMING")
    print("=" * 95)
    print(f"  {'label':<28} {'n':>5} {'wins':>5} {'loss':>5} {'hit%':>6} "
          f"{'total $':>9} {'avg $':>8} {'max_dd $':>9}")
    print(f"  {'-'*28} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*9} {'-'*8} {'-'*9}")
    for s in summaries:
        flag = "  *" if s["n"] >= 30 and s["total"] > 0 else ""
        print(
            f"  {s['label']:<28} {s['n']:>5} {s.get('wins', 0):>5} "
            f"{s.get('losses', 0):>5} {s['hit']:>5.1f}% "
            f"{s['total']:>+8.2f} {s['avg']:>+7.4f} "
            f"{s['max_dd']:>+8.2f}{flag}"
        )
    print()

    # Best per strategy by total $ (n>=30)
    print("=== Best timing per strategy (n>=30) ===")
    best_by_strat: dict[str, dict] = {}
    for s in summaries:
        if s["n"] < 30:
            continue
        strat = s["label"].split("@")[0]
        if strat not in best_by_strat or s["total"] > best_by_strat[strat]["total"]:
            best_by_strat[strat] = s
    for strat, s in best_by_strat.items():
        print(
            f"  {strat:<22} {s['label'].split('@')[1]}: n={s['n']} "
            f"hit={s['hit']:.1f}% total=${s['total']:+.2f} avg=${s['avg']:+.4f}"
        )
    print()

    # Per-strategy bucket breakdown for the best timing.
    print("=== Bucket breakdown for best variant per strategy ===")
    for strat, s in best_by_strat.items():
        if s["total"] <= 0:
            continue
        print(f"\n  {strat} ({s['label'].split('@')[1]}):")
        bd = bucket_breakdown(all_results[s["label"]])
        print(f"    {'bucket':<10} {'n':>4} {'hit%':>6} {'total $':>9} {'avg $':>8}")
        for b in bd:
            print(
                f"    {b['bucket']:<10} {b['n']:>4} {b['hit']:>5.1f}% "
                f"{b['total']:>+8.2f} {b['avg']:>+7.3f}"
            )

    # CSV per combo with at least 1 trade.
    print()
    for label, trades in all_results.items():
        if not trades:
            continue
        path = OUT_DIR / f"{label.replace('@', '_')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ticker", "strategy", "timing", "side", "entry_c",
                "btc", "strike", "settle_yes", "won",
                "gross_c", "fee_c", "net_c",
            ])
            for t in trades:
                w.writerow([
                    t.ticker, t.strategy, t.timing_label, t.side, t.entry_c,
                    f"{t.btc:.2f}", f"{t.strike:.2f}",
                    int(t.settle_yes), int(t.won),
                    f"{t.gross_c:.1f}", f"{t.fee_c:.2f}", f"{t.net_c:.2f}",
                ])
    print(f"  per-combo CSVs in {OUT_DIR.relative_to(REPO)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
