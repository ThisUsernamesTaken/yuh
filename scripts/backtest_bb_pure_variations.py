"""Multi-variation backtest for BB_PURE on Kalshi 15m BTC contracts.

Baseline source: ``data/kalshi_external_backtest.db``
  - ``markets``  : ticker, open_ts (s), close_ts (s), result ('yes'/'no'),
                   raw_json (contains floor_strike + expiration_value)
  - ``btc_1m``   : open_ts (s), open, high, low, close — BTC OHLCV per minute
  - ``candles``  : ticker, end_period_ts (s), price_close, yes_bid_close,
                   yes_ask_close, volume_fp — Kalshi 1m candles

Methodology (per the user's spec — 2026-05-04):
  For each finalized market in ``markets``:
    1. Pull strike from raw_json.floor_strike
    2. Pull settlement outcome from `result` ('yes' = BTC settled at/above
       strike, 'no' = below)
    3. At entry-time (default: 60s past window open), grab:
         - BTC mid from ``btc_1m``
         - Kalshi YES mid from ``candles`` ((yes_bid + yes_ask)/2 at the
           closest candle ≤ entry-time, fallback to price_close)
    4. Run BB_PURE math (bb_pure.evaluate) — gives fair_yes_cents and edge
    5. Apply each VARIATION's gates (min_edge, max_dist, entry buckets,
       etc.). If the variation says "fire", record:
         - Side (yes/no, whichever is underpriced)
         - Entry price (yes_mid for buy-yes; 100-yes_mid for buy-no)
         - Settlement P&L: +($1 - entry/100) if win, -(entry/100) if loss
    6. Apply 7% maker fee on the winning leg only (Kalshi fee schedule)

Variations tested (configurable list — edit ``VARIATIONS`` below):
  CURRENT         baseline (min_edge=8 max_dist=0.0004 max_entry=55)
  NO_MIDDLE       baseline minus 30-49c entry bucket
  CHEAP_ONLY      only 20-29c
  STRIKE_ONLY     only 50-59c
  EXTREME_EDGES   20-29c OR 50-59c (winners-only buckets from prior backtest)
  TIGHTER_EDGE    min_edge=12pp
  MUCH_TIGHTER    min_edge=16pp
  TIGHTER_STRIKE  max_dist=0.0002
  COMBINED        extreme_edges + min_edge=10pp + tighter_strike

Outputs:
  - Side-by-side comparison table (n, hit%, avg_$, total_$, max_dd_$)
  - By-bucket breakdown for the best variation
  - CSV dump per variation in scripts/_backtest_results/
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
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"
OUT_DIR = REPO / "scripts" / "_backtest_results"


# ── BB Brownian-Bridge model (lifted from bb_pure.py for self-contained
#    backtest; matches the engine math) ──────────────────────────────────


def _ndtr(z: float) -> float:
    """Normal CDF via erf. Identical to scipy.stats.norm.cdf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bb_fair_yes_cents(
    btc: float, strike: float, secs_left: float, vol_per_sec: float
) -> int:
    """Return BB-bridge fair YES probability in cents (0..100).

    Matches the production formula: fair = N((BTC - strike) / sigma_T),
    where sigma_T = vol_per_sec * sqrt(secs_left).
    """
    if secs_left <= 0:
        return 100 if btc >= strike else 0
    if vol_per_sec <= 0:
        return 50
    sigma = vol_per_sec * math.sqrt(secs_left)
    if sigma <= 0:
        return 100 if btc >= strike else 0
    z = (btc - strike) / sigma
    return int(round(100.0 * _ndtr(z)))


# ── Variation config ────────────────────────────────────────────────────


@dataclass
class Variation:
    name: str
    min_edge_pp: float = 8.0
    min_entry_c: int = 5
    max_entry_c: int = 55
    max_strike_dist_pct: float = 0.0004
    # If set, only fire when entry price is in one of these (lo, hi)
    # inclusive ranges. Empty = no bucket filter.
    allowed_buckets: list[tuple[int, int]] = field(default_factory=list)
    # When True, require the cheap side to ALIGN with the BTC 5-min trend
    # direction (mirrors the production A1 alignment gate). Block
    # contrarian fires.
    require_alignment: bool = False
    # Alignment fallback (mirrors A2): if no aligned signal exists but BTC
    # has a clear trend, fire on the with-trend side at min_edge_pp/2.
    enable_fallback: bool = False
    # Entry timing: how many seconds into the 15-min window to evaluate.
    entry_offset_s: int = 60
    description: str = ""


VARIATIONS: list[Variation] = [
    Variation(
        "CURRENT",
        description="baseline production: min_edge=8 max_dist=0.0004 max_entry=55",
    ),
    Variation(
        "ALIGNED",
        require_alignment=True,
        description="A1 gate ON: block contrarian fires (BB cheap side vs BTC 5m trend)",
    ),
    Variation(
        "ALIGNED_FALLBACK",
        require_alignment=True,
        enable_fallback=True,
        description="A1 gate ON + A2 fallback (with-trend at half edge)",
    ),
    Variation(
        "ALIGNED_TIGHT",
        require_alignment=True,
        min_edge_pp=12.0,
        description="A1 gate + min_edge=12pp",
    ),
    Variation(
        "ALIGNED_NO_MIDDLE",
        require_alignment=True,
        allowed_buckets=[(5, 29), (50, 55)],
        description="A1 gate + skip 30-49c entries",
    ),
    Variation(
        "MUCH_TIGHTER",
        min_edge_pp=16.0,
        description="min_edge=16pp (no alignment)",
    ),
    Variation(
        "ALIGNED_LATE",
        require_alignment=True,
        entry_offset_s=300,
        description="A1 gate + enter at 5min into window (vs 1min default)",
    ),
    Variation(
        "ALIGNED_VERY_LATE",
        require_alignment=True,
        entry_offset_s=600,
        description="A1 gate + enter at 10min into window",
    ),
    Variation(
        "ALIGNED_T720",
        require_alignment=True,
        entry_offset_s=720,
        description="A1 gate + 12min entry",
    ),
    Variation(
        "ALIGNED_T780",
        require_alignment=True,
        entry_offset_s=780,
        description="A1 gate + 13min entry (2 min before close)",
    ),
    Variation(
        "ALIGNED_T840",
        require_alignment=True,
        entry_offset_s=840,
        description="A1 gate + 14min entry (1 min before close)",
    ),
    Variation(
        "PLAIN_LATE",
        entry_offset_s=600,
        description="10min entry, NO alignment gate (does timing alone explain it?)",
    ),
    Variation(
        "PLAIN_VERY_LATE",
        entry_offset_s=720,
        description="12min entry, NO alignment gate",
    ),
    Variation(
        "ALIGNED_CHEAP",
        require_alignment=True,
        allowed_buckets=[(15, 35)],
        description="A1 gate + only 15-35c entries (cheap side strong-edge)",
    ),
]


# ── Fees ────────────────────────────────────────────────────────────────


def kalshi_maker_fee_cents(price_cents: int, contracts: int) -> float:
    """Kalshi BTC15M maker fee: 7% × p × (1-p) per contract, charged on
    open AND close (so 2x for a round-trip). Conservative estimate."""
    p = float(price_cents) / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0  # in cents
    return per_contract * contracts * 2  # ×2 for round-trip


# ── Loaders ─────────────────────────────────────────────────────────────


def load_markets(con: sqlite3.Connection) -> list[dict]:
    """Yield finalized markets with parsed strike + settlement."""
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
        try:
            settle_value = float(j.get("expiration_value") or 0)
        except Exception:
            settle_value = 0.0
        if strike <= 0 or open_ts is None or close_ts is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "open_ts": int(open_ts),
                "close_ts": int(close_ts),
                "result": (result or "").lower(),
                "strike": strike,
                "settle_value": settle_value,
            }
        )
    return out


def build_btc_index(con: sqlite3.Connection) -> dict[int, float]:
    """Map open_ts (1-min boundary) → close price."""
    cur = con.cursor()
    cur.execute("SELECT open_ts, close FROM btc_1m WHERE close > 0")
    return {int(ts): float(c) for ts, c in cur.fetchall()}


def build_candle_index(con: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Map ticker → sorted list of (end_period_ts, yes_bid, yes_ask, price_close)."""
    cur = con.cursor()
    cur.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close, "
        "price_close FROM candles ORDER BY ticker, end_period_ts"
    )
    idx: dict[str, list[tuple]] = defaultdict(list)
    for tkr, ts, yb, ya, pc in cur.fetchall():
        idx[tkr].append((int(ts), yb, ya, pc))
    return idx


def vol_per_sec_from_btc(btc_index: dict[int, float], around_ts: int, lookback_min: int = 60) -> float:
    """Estimate per-second BTC vol (std of 1m returns) over a lookback
    window ending at ``around_ts``. Returns vol in dollars per sqrt(second)."""
    closes: list[float] = []
    for k in range(lookback_min):
        ts = around_ts - 60 * (k + 1)
        # snap to minute boundary
        ts = (ts // 60) * 60
        c = btc_index.get(ts)
        if c is not None and c > 0:
            closes.append(c)
    if len(closes) < 5:
        return 0.0
    closes.reverse()
    rets: list[float] = []
    for i in range(1, len(closes)):
        rets.append(closes[i] - closes[i - 1])  # dollar deltas
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    sd_per_min = math.sqrt(var)
    # Convert per-min sd to per-sqrt(second): sigma_T = sigma_per_min * sqrt(T_min)
    # so per-sec = per-min / sqrt(60)
    return sd_per_min / math.sqrt(60.0)


# ── Per-market evaluator ────────────────────────────────────────────────


@dataclass
class TradeRecord:
    ticker: str
    side: str          # 'yes' or 'no'
    entry_c: int       # entry price in cents
    fair_yes_c: int
    edge_pp: float
    btc_at_entry: float
    strike: float
    dist_pct: float
    settle_yes: bool   # True if YES settled
    won: bool
    gross_pnl_c: float
    fee_c: float
    net_pnl_c: float


def kalshi_mid_at(
    candles: list[tuple], target_ts: int, btc_index: dict[int, float]
) -> tuple[int, int] | None:
    """Return (yes_mid_cents, side_mid_used) for the closest candle at or
    before ``target_ts``. Each candle entry is
    ``(end_period_ts, yes_bid, yes_ask, price_close)`` with prices in
    dollars. Returns None when no usable candle exists.
    """
    best = None
    for ts, yb, ya, pc in candles:
        if ts > target_ts:
            break
        best = (ts, yb, ya, pc)
    if best is None:
        return None
    _ts, yb, ya, pc = best
    # Prefer mid of bid/ask
    if yb is not None and ya is not None and yb > 0 and ya > 0:
        mid_d = (float(yb) + float(ya)) / 2.0
    elif pc is not None and pc > 0:
        mid_d = float(pc)
    else:
        return None
    mid_c = int(round(mid_d * 100))
    if mid_c <= 0 or mid_c >= 100:
        return None
    return mid_c, 1


def btc_5m_trend(btc_index: dict[int, float], at_ts: int) -> int:
    """Return BTC 5-minute trend sign at ``at_ts``: +1 up, -1 down, 0 flat.
    Mirrors the production A1 gate's btc5m direction calculation."""
    now_min = (at_ts // 60) * 60
    p_now = btc_index.get(now_min)
    p_5m = btc_index.get(now_min - 300)
    if p_now is None or p_5m is None:
        return 0
    delta = p_now - p_5m
    # Production threshold ~$5; below = "flat".
    if delta > 5:
        return 1
    if delta < -5:
        return -1
    return 0


def evaluate_market(
    market: dict,
    btc_index: dict[int, float],
    candle_idx: dict[str, list[tuple]],
    variation: Variation,
) -> TradeRecord | None:
    """Run BB_PURE math + variation gates on a single market. Return a
    TradeRecord if the engine would have entered, else None."""
    open_ts = market["open_ts"]
    close_ts = market["close_ts"]
    strike = market["strike"]
    settle_yes = market["result"] == "yes"

    # Entry timestamp: per-variation offset past window open.
    entry_ts = open_ts + variation.entry_offset_s
    if entry_ts >= close_ts:
        return None

    # Snap to minute boundary for BTC lookup.
    btc_minute = (entry_ts // 60) * 60
    btc = btc_index.get(btc_minute)
    if btc is None or btc <= 0:
        return None

    # Strike-distance gate.
    dist = abs(btc - strike) / btc
    if dist > variation.max_strike_dist_pct:
        return None

    # Vol estimate (1h lookback).
    vps = vol_per_sec_from_btc(btc_index, entry_ts, lookback_min=60)
    if vps <= 0:
        return None

    secs_left = max(0, close_ts - entry_ts)
    fair_yes_c = bb_fair_yes_cents(btc, strike, secs_left, vps)

    # Get current Kalshi YES mid from the per-ticker candle stream.
    candles = candle_idx.get(market["ticker"]) or []
    mid_pair = kalshi_mid_at(candles, entry_ts, btc_index)
    if not mid_pair:
        return None
    yes_mid_c = mid_pair[0]

    # Edge: positive = YES underpriced (fire YES), negative = NO underpriced.
    edge_yes = fair_yes_c - yes_mid_c
    primary_side: str | None = None
    primary_entry_c: int = 0
    primary_edge_pp: float = 0.0
    if abs(edge_yes) >= variation.min_edge_pp:
        if edge_yes > 0:
            primary_side = "yes"
            primary_entry_c = yes_mid_c
        else:
            primary_side = "no"
            primary_entry_c = 100 - yes_mid_c
        primary_edge_pp = abs(edge_yes)

    # ── Alignment classifier (mirror of production A1 gate) ─────────
    # Block contrarian fires: if BB cheap side opposes BTC 5min trend
    # direction, the cheap side is contrarian — refuse the fire.
    btc_trend = btc_5m_trend(btc_index, entry_ts)
    aligned_side: str | None = None
    if btc_trend > 0:
        aligned_side = "yes"  # BTC up → buying YES is with-trend
    elif btc_trend < 0:
        aligned_side = "no"

    side: str | None = None
    entry_c: int = 0
    edge_pp: float = 0.0
    if primary_side is not None:
        if variation.require_alignment and aligned_side is not None:
            if primary_side == aligned_side:
                side, entry_c, edge_pp = primary_side, primary_entry_c, primary_edge_pp
            # else: contrarian fire, blocked
        else:
            side, entry_c, edge_pp = primary_side, primary_entry_c, primary_edge_pp

    # ── A2 fallback: if no aligned primary, synthesize a fire on the
    #    with-trend side using half the edge requirement ──────────────
    if side is None and variation.enable_fallback and aligned_side is not None:
        # Compute the with-trend side's edge.
        if aligned_side == "yes":
            fb_edge = fair_yes_c - yes_mid_c   # signed, want positive
            fb_entry = yes_mid_c
        else:
            fb_edge = -(fair_yes_c - yes_mid_c)
            fb_entry = 100 - yes_mid_c
        if fb_edge >= variation.min_edge_pp / 2.0:
            side, entry_c, edge_pp = aligned_side, fb_entry, fb_edge

    if side is None:
        return None

    # Entry-price gates.
    if entry_c < variation.min_entry_c or entry_c > variation.max_entry_c:
        return None

    # Bucket filter (allowed_buckets is empty = no filter).
    if variation.allowed_buckets:
        if not any(lo <= entry_c <= hi for lo, hi in variation.allowed_buckets):
            return None

    # Settlement P&L for our side (1 contract).
    won = (side == "yes" and settle_yes) or (side == "no" and not settle_yes)
    if won:
        gross = (100 - entry_c)  # we paid entry_c, get $1 = 100c
    else:
        gross = -entry_c

    fee = kalshi_maker_fee_cents(entry_c, 1)
    net = gross - fee

    return TradeRecord(
        ticker=market["ticker"],
        side=side,
        entry_c=entry_c,
        fair_yes_c=fair_yes_c,
        edge_pp=edge_pp,
        btc_at_entry=btc,
        strike=strike,
        dist_pct=dist,
        settle_yes=settle_yes,
        won=won,
        gross_pnl_c=float(gross),
        fee_c=float(fee),
        net_pnl_c=float(net),
    )


# ── Aggregator ──────────────────────────────────────────────────────────


def summarise(name: str, trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"name": name, "n": 0, "hit": 0.0, "total": 0.0, "avg": 0.0, "max_dd": 0.0}
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    total_c = sum(t.net_pnl_c for t in trades)
    avg_c = total_c / n
    # Max-drawdown over the trade sequence (assumes 1-contract sizing).
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for t in trades:
        eq += t.net_pnl_c
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "name": name,
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "hit": (wins / n) * 100,
        "total": total_c / 100.0,
        "avg": avg_c / 100.0,
        "max_dd": dd / 100.0,
    }


def bucket_breakdown(trades: list[TradeRecord]) -> list[dict]:
    buckets = [(5, 19), (20, 29), (30, 39), (40, 49), (50, 55), (56, 65),
               (66, 79), (80, 95)]
    out = []
    for lo, hi in buckets:
        sub = [t for t in trades if lo <= t.entry_c <= hi]
        if not sub:
            continue
        wins = sum(1 for t in sub if t.won)
        total = sum(t.net_pnl_c for t in sub)
        out.append(
            {
                "bucket": f"{lo}-{hi}c",
                "n": len(sub),
                "hit": wins / len(sub) * 100,
                "total": total / 100.0,
                "avg": total / len(sub) / 100.0,
            }
        )
    return out


# ── Main ────────────────────────────────────────────────────────────────


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
    print(f"  {len(markets):,} finalized markets")
    print(f"  {len(btc_index):,} BTC minutes")
    print(f"  {len(candle_idx):,} tickers with candle data")
    print()

    results: dict[str, list[TradeRecord]] = {v.name: [] for v in VARIATIONS}
    for m in markets:
        for v in VARIATIONS:
            tr = evaluate_market(m, btc_index, candle_idx, v)
            if tr is not None:
                results[v.name].append(tr)

    # ── Comparison table ────────────────────────────────────────────────
    print("=" * 95)
    print("BB_PURE BACKTEST — VARIATION COMPARISON")
    print("=" * 95)
    print(f"  {'name':<16} {'n':>5} {'wins':>5} {'loss':>5} {'hit%':>6} "
          f"{'total $':>9} {'avg $':>7} {'max_dd $':>9}  description")
    print(f"  {'-'*16} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*9} {'-'*7} "
          f"{'-'*9}  {'-'*40}")
    summaries = []
    for v in VARIATIONS:
        s = summarise(v.name, results[v.name])
        summaries.append(s)
        print(
            f"  {s['name']:<16} {s['n']:>5} "
            f"{s.get('wins', 0):>5} {s.get('losses', 0):>5} "
            f"{s['hit']:>5.1f}% {s['total']:>+8.2f} {s['avg']:>+7.3f} "
            f"{s['max_dd']:>+8.2f}  {v.description}"
        )
    print()

    # Best by total P&L (require n >= 30 to be meaningful).
    qualifying = [s for s in summaries if s["n"] >= 30]
    qualifying.sort(key=lambda s: s["total"], reverse=True)
    if qualifying:
        best = qualifying[0]
        print(f"Best (n>=30) by total $: {best['name']}  "
              f"(n={best['n']} hit={best['hit']:.1f}% "
              f"total=${best['total']:+.2f} avg=${best['avg']:+.3f})")
        print()
        print(f"=== {best['name']} entry-bucket breakdown ===")
        bd = bucket_breakdown(results[best["name"]])
        print(f"  {'bucket':<10} {'n':>5} {'hit%':>6} {'total $':>9} {'avg $':>7}")
        for b in bd:
            print(
                f"  {b['bucket']:<10} {b['n']:>5} {b['hit']:>5.1f}% "
                f"{b['total']:>+8.2f} {b['avg']:>+7.3f}"
            )
        print()

    # ── CSV dump per variation ──────────────────────────────────────────
    for name, trades in results.items():
        if not trades:
            continue
        path = OUT_DIR / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ticker", "side", "entry_c", "fair_yes_c", "edge_pp",
                "btc", "strike", "dist_pct", "settle_yes", "won",
                "gross_c", "fee_c", "net_c",
            ])
            for t in trades:
                w.writerow([
                    t.ticker, t.side, t.entry_c, t.fair_yes_c,
                    f"{t.edge_pp:.1f}", f"{t.btc_at_entry:.2f}",
                    f"{t.strike:.2f}", f"{t.dist_pct:.5f}",
                    int(t.settle_yes), int(t.won),
                    f"{t.gross_pnl_c:.1f}", f"{t.fee_c:.2f}",
                    f"{t.net_pnl_c:.2f}",
                ])
        print(f"  wrote {path.relative_to(REPO)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
