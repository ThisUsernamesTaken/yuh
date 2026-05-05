"""How well do the scalp engines actually EXIT their trades?

User reframe (2026-05-04): expiry betting is one game. Scalp-style
in-window exits are another — pennies per trade × high frequency could
be more profitable than dollar binary bets if the exit fill rate is high
enough.

For each of the three scalp strategies (BB_PURE, FVG, ATM):
  1. Find every entry signal in the dataset.
  2. Walk the candle stream from entry to close.
  3. Track:
       - TP fill rate at multiple TP targets (+3, +5, +8, +12, +20, +30)
       - Time to fill (seconds from entry to TP touch)
       - MFE / MAE distributions
       - Net P&L per TP setting (after fees, with/without SL=8c)
  4. Compute "effective trades/day" potential at each TP.

The intuition: a +5c TP that fills in 90 seconds with 80% reliability
beats a +30c TP that fills in 600 seconds with 60% reliability if both
are net-positive after fees, because the former enables multiple cycles
per window.

Output: side-by-side comparison across the three scalp strategies + a
"daily P&L estimate" assuming K cycles per window are achievable.
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


# ── Math (lifted) ──────────────────────────────────────────────────────


def _ndtr(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bb_fair_yes_cents(btc, strike, secs_left, vps):
    if secs_left <= 0:
        return 100 if btc >= strike else 0
    if vps <= 0:
        return 50
    sigma = vps * math.sqrt(secs_left)
    return int(round(100.0 * _ndtr((btc - strike) / sigma)))


def vol_per_sec(btc_index, around_ts, lookback_min=60):
    closes = []
    for k in range(lookback_min):
        ts = ((around_ts - 60 * (k + 1)) // 60) * 60
        c = btc_index.get(ts)
        if c and c > 0:
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


def yes_mid_from_row(yb, ya, pc):
    if yb is not None and ya is not None and yb > 0 and ya > 0:
        return int(round((float(yb) + float(ya)) * 50))
    if pc is not None and pc > 0:
        return int(round(float(pc) * 100))
    return None


def side_bid_from_row(side, yb, ya, pc):
    if yb is not None and ya is not None and yb > 0 and ya > 0:
        if side == "yes":
            return int(round(float(yb) * 100))
        return int(round((1.0 - float(ya)) * 100))
    if pc is not None and pc > 0:
        if side == "yes":
            return int(round(float(pc) * 100))
        return int(round((1.0 - float(pc)) * 100))
    return None


def kalshi_yes_at(candles, target_ts):
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
        mid = bid = ask = int(round(float(pc) * 100))
    else:
        return None
    if mid <= 0 or mid >= 100:
        return None
    return mid, bid, ask


def kalshi_fee(price_c, taker=False):
    p = max(0.0, min(1.0, price_c / 100.0))
    f = 0.07 * p * (1.0 - p) * 100.0
    return f * 1.4 if taker else f


# ── Loaders ───────────────────────────────────────────────────────────


def load_markets(con):
    cur = con.cursor()
    cur.execute("SELECT ticker, open_ts, close_ts, result, raw_json FROM markets "
                "WHERE status='finalized' AND result IN ('yes','no')")
    out = []
    for ticker, ots, cts, res, raw in cur.fetchall():
        try:
            j = json.loads(raw)
            strike = float(j.get("floor_strike") or 0)
        except Exception:
            continue
        if strike <= 0:
            continue
        out.append({"ticker": ticker, "open_ts": int(ots), "close_ts": int(cts),
                    "result": (res or "").lower(), "strike": strike})
    return out


def build_btc_index(con):
    cur = con.cursor()
    cur.execute("SELECT open_ts, close FROM btc_1m WHERE close > 0")
    return {int(ts): float(c) for ts, c in cur.fetchall()}


def build_candle_index(con):
    cur = con.cursor()
    cur.execute("SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close, "
                "price_close FROM candles ORDER BY ticker, end_period_ts")
    idx = defaultdict(list)
    for tkr, ts, yb, ya, pc in cur.fetchall():
        idx[tkr].append((int(ts), yb, ya, pc))
    return idx


# ── Signal finders (one per strategy) ─────────────────────────────────


@dataclass
class ScalpSignal:
    strategy: str
    ticker: str
    entry_ts: int
    side: str
    entry_c: int
    fair_for_side: int
    spread_c: int
    btc_dist_pct: float


def find_bb_pure_signal(market, btc_index, candle_idx, *, entry_offset_s=300):
    """BB_PURE_MEANREV: near-strike, BB-edge, alignment-aware."""
    open_ts = market["open_ts"]
    entry_ts = open_ts + entry_offset_s
    if entry_ts >= market["close_ts"]:
        return None
    btc_min = (entry_ts // 60) * 60
    btc = btc_index.get(btc_min)
    if not btc or btc <= 0:
        return None
    dist = abs(btc - market["strike"]) / btc
    if dist > 0.0004:
        return None
    candles = candle_idx.get(market["ticker"]) or []
    kp = kalshi_yes_at(candles, entry_ts)
    if not kp:
        return None
    mid, bid, ask = kp
    secs_left = float(market["close_ts"] - entry_ts)
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    edge = fair - mid
    if abs(edge) < 8:
        return None
    if edge > 0:
        side, entry_c = "yes", mid
        fair_for_side = fair
    else:
        side, entry_c = "no", 100 - mid
        fair_for_side = 100 - fair
    if entry_c < 5 or entry_c > 55:
        return None
    # A1 alignment gate
    delta = btc_index.get(btc_min) - (btc_index.get(btc_min - 300) or btc)
    if abs(delta) > 5:
        aligned = "yes" if delta > 0 else "no"
        if side != aligned:
            return None
    return ScalpSignal(
        strategy="BB_PURE",
        ticker=market["ticker"], entry_ts=entry_ts, side=side, entry_c=entry_c,
        fair_for_side=fair_for_side, spread_c=abs(fair - mid),
        btc_dist_pct=dist * 100.0,
    )


def find_atm_signal(market, btc_index, candle_idx, *, entry_offset_s=300):
    """ATM discount entry: near-strike, side-ask <= 35c, fair=50c assumption."""
    entry_ts = market["open_ts"] + entry_offset_s
    if entry_ts >= market["close_ts"]:
        return None
    btc_min = (entry_ts // 60) * 60
    btc = btc_index.get(btc_min)
    if not btc or btc <= 0:
        return None
    dist = abs(btc - market["strike"]) / btc
    if dist > 0.0003:
        return None
    candles = candle_idx.get(market["ticker"]) or []
    kp = kalshi_yes_at(candles, entry_ts)
    if not kp:
        return None
    mid, bid, ask = kp
    no_ask = 100 - bid
    if no_ask <= 0:
        return None
    yes_disc = ask <= 35 and (50 - ask) >= 8
    no_disc = no_ask <= 35 and (50 - no_ask) >= 8
    if yes_disc and (not no_disc or (50 - ask) >= (50 - no_ask)):
        return ScalpSignal(
            strategy="ATM",
            ticker=market["ticker"], entry_ts=entry_ts, side="yes",
            entry_c=ask, fair_for_side=50, spread_c=50 - ask,
            btc_dist_pct=dist * 100.0,
        )
    if no_disc:
        return ScalpSignal(
            strategy="ATM",
            ticker=market["ticker"], entry_ts=entry_ts, side="no",
            entry_c=no_ask, fair_for_side=50, spread_c=50 - no_ask,
            btc_dist_pct=dist * 100.0,
        )
    return None


def find_fvg_signal(market, btc_index, candle_idx):
    """FVG state machine — first signal in window."""
    candles = candle_idx.get(market["ticker"]) or []
    if not candles:
        return None
    open_ts = market["open_ts"]
    close_ts = market["close_ts"]
    strike = market["strike"]
    baseline_mids = []
    for ts, yb, ya, pc in candles:
        if ts < open_ts:
            continue
        if ts > open_ts + 120:
            break
        m = yes_mid_from_row(yb, ya, pc)
        if m is not None:
            baseline_mids.append(m)
    if not baseline_mids:
        return None
    baseline = int(sum(baseline_mids) / len(baseline_mids))
    for ts, yb, ya, pc in candles:
        if ts < open_ts + 90:
            continue
        if ts >= close_ts:
            break
        secs_remaining = close_ts - ts
        if secs_remaining < 120:
            return None
        session_age = ts - open_ts
        thresh = 5 if session_age < 180 else (8 if session_age < 420 else 12)
        mid = yes_mid_from_row(yb, ya, pc)
        if mid is None:
            continue
        vps = vol_per_sec(btc_index, ts, 60)
        if vps <= 0:
            continue
        btc_min = (ts // 60) * 60
        btc = btc_index.get(btc_min)
        if not btc or btc <= 0:
            continue
        fair = bb_fair_yes_cents(btc, strike, float(secs_remaining), vps)
        gap = fair - baseline
        if abs(gap) < thresh:
            continue
        btc_dist_pct = abs(btc - strike) / strike * 100.0
        if btc_dist_pct < 0.01 or btc_dist_pct > 0.15:
            continue
        if gap > 0:
            side = "yes"
            entry_c = min(mid, baseline)
            fair_for_side = fair
        else:
            side = "no"
            entry_c = min(100 - mid, 100 - baseline)
            fair_for_side = 100 - fair
        if entry_c < 5 or entry_c > 95:
            continue
        return ScalpSignal(
            strategy="FVG",
            ticker=market["ticker"], entry_ts=ts, side=side, entry_c=entry_c,
            fair_for_side=fair_for_side, spread_c=abs(fair - mid),
            btc_dist_pct=btc_dist_pct,
        )
    return None


# ── Per-signal walker: TP fill, MFE, time-to-fill ─────────────────────


@dataclass
class WalkOutcome:
    sig: ScalpSignal
    mfe_c: int
    mae_c: int
    mfe_at_s: int
    final_bid_c: int
    settled_yes: bool


def walk(sig, candles, close_ts, settle_yes):
    mfe = sig.entry_c
    mae = sig.entry_c
    mfe_at_s = 0
    last = sig.entry_c
    for ts, yb, ya, pc in candles:
        if ts < sig.entry_ts:
            continue
        if ts >= close_ts:
            break
        b = side_bid_from_row(sig.side, yb, ya, pc)
        if b is None:
            continue
        last = b
        elapsed = ts - sig.entry_ts
        if b > mfe:
            mfe, mfe_at_s = b, elapsed
        if b < mae:
            mae = b
    return WalkOutcome(sig, mfe, mae, mfe_at_s, last, settle_yes)


# ── TP/SL evaluator ──────────────────────────────────────────────────


@dataclass
class TpExit:
    reason: str           # TP / SL / PRE_EXP / SETTLE_W / SETTLE_L
    exit_c: int
    fill_at_s: int        # seconds from entry to fill
    net_c: float


def eval_tp_sl(sig, candles, close_ts, settle_yes,
               tp_offset, sl_offset=8) -> TpExit:
    tp = sig.entry_c + tp_offset
    sl = sig.entry_c - sl_offset if sl_offset else None
    pre_exp = close_ts - 90
    for ts, yb, ya, pc in candles:
        if ts < sig.entry_ts:
            continue
        if ts >= close_ts:
            break
        b = side_bid_from_row(sig.side, yb, ya, pc)
        if b is None:
            continue
        elapsed = ts - sig.entry_ts
        if b >= tp:
            gross = tp - sig.entry_c
            fee = kalshi_fee(sig.entry_c) + kalshi_fee(tp)
            return TpExit("TP", tp, elapsed, gross - fee)
        if sl is not None and b <= sl:
            x = max(1, b - 1)
            gross = x - sig.entry_c
            fee = kalshi_fee(sig.entry_c) + kalshi_fee(x, taker=True)
            return TpExit("SL", x, elapsed, gross - fee)
        if ts >= pre_exp:
            x = max(1, b)
            gross = x - sig.entry_c
            fee = kalshi_fee(sig.entry_c) + kalshi_fee(x, taker=True)
            return TpExit("PRE_EXP", x, elapsed, gross - fee)
    won = (sig.side == "yes" and settle_yes) or (sig.side == "no" and not settle_yes)
    x = 100 if won else 0
    gross = x - sig.entry_c
    fee = kalshi_fee(sig.entry_c)
    return TpExit("SETTLE_W" if won else "SETTLE_L", x,
                  close_ts - sig.entry_ts, gross - fee)


# ── Main ───────────────────────────────────────────────────────────────


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
    print(f"  {len(markets):,} markets")
    print()

    # Find signals for each scalp strategy
    finders = {
        "BB_PURE":   lambda m: find_bb_pure_signal(m, btc_index, candle_idx),
        "ATM":       lambda m: find_atm_signal(m, btc_index, candle_idx),
        "FVG":       lambda m: find_fvg_signal(m, btc_index, candle_idx),
    }
    all_sigs: dict[str, list[WalkOutcome]] = {}
    for name, fn in finders.items():
        outs: list[WalkOutcome] = []
        for m in markets:
            sig = fn(m)
            if not sig:
                continue
            candles = candle_idx.get(m["ticker"]) or []
            outs.append(walk(sig, candles, m["close_ts"],
                             m["result"] == "yes"))
        all_sigs[name] = outs
        print(f"  {name}: {len(outs):,} signals found")
    print()

    # ── Headline: TP fill rate + time-to-fill at multiple TP levels ────
    print("=" * 110)
    print("SCALP-EXIT FILL RATE & TIMING — TP target sweep with SL=8c")
    print("=" * 110)
    print(f"  {'strategy':<10} {'TP':>4} {'fires':>5} {'TP%':>5} "
          f"{'avg_fill_s':>10} {'med_fill_s':>10} "
          f"{'avg gross':>9} {'avg fee':>8} {'avg net':>9} "
          f"{'tot net $':>10} {'max DD $':>9}")
    print(f"  {'-'*10} {'-'*4} {'-'*5} {'-'*5} {'-'*10} {'-'*10} "
          f"{'-'*9} {'-'*8} {'-'*9} {'-'*10} {'-'*9}")

    summary_rows: list[tuple] = []
    tp_levels = [3, 5, 8, 12, 20, 30]

    for strat in ("BB_PURE", "ATM", "FVG"):
        outs = all_sigs[strat]
        if not outs:
            continue
        for tp in tp_levels:
            results = []
            for o in outs:
                cands = candle_idx.get(o.sig.ticker) or []
                m_idx = next((i for i, m in enumerate(markets)
                              if m["ticker"] == o.sig.ticker), None)
                if m_idx is None:
                    continue
                close_ts = markets[m_idx]["close_ts"]
                results.append(eval_tp_sl(o.sig, cands, close_ts, o.settled_yes,
                                          tp_offset=tp, sl_offset=8))
            if not results:
                continue
            n = len(results)
            tp_fills = [r for r in results if r.reason == "TP"]
            tp_pct = len(tp_fills) / n * 100
            avg_fill = sum(r.fill_at_s for r in tp_fills) / len(tp_fills) if tp_fills else 0
            sorted_fills = sorted(r.fill_at_s for r in tp_fills)
            med_fill = sorted_fills[len(sorted_fills) // 2] if sorted_fills else 0
            grosses = [(r.exit_c - o.sig.entry_c) for r, o in zip(results, outs)]
            fees = [kalshi_fee(o.sig.entry_c) + kalshi_fee(r.exit_c, taker=(r.reason in ("SL", "PRE_EXP"))) for r, o in zip(results, outs)]
            avg_gross = sum(grosses) / n
            avg_fee = sum(fees) / n
            avg_net = sum(r.net_c for r in results) / n
            tot_net = sum(r.net_c for r in results) / 100.0
            # max DD
            eq = peak = 0.0
            dd = 0.0
            for r in results:
                eq += r.net_c
                peak = max(peak, eq)
                dd = min(dd, eq - peak)
            print(f"  {strat:<10} +{tp:<3}c {n:>5} {tp_pct:>4.1f}% "
                  f"{avg_fill:>9.0f}s {med_fill:>9.0f}s "
                  f"{avg_gross:>+8.2f}c {avg_fee:>7.2f}c {avg_net:>+8.2f}c "
                  f"{tot_net:>+9.2f} {dd / 100:>+8.2f}")
            summary_rows.append((strat, tp, n, tp_pct, avg_fill, med_fill,
                                 avg_net, tot_net, dd / 100))
        print()

    # ── Effective trades/day + EV @ best config per strategy ───────────
    print("=" * 90)
    print("EFFECTIVE DAILY P&L ESTIMATE")
    print("=" * 90)
    print("  Assumes ~96 windows/day. With per-signal native eval, NO multi-cycle.")
    print(f"  {'strategy':<10} {'TP':>4} {'fires/day':>10} {'TP fill%':>9} "
          f"{'avg fill':>9} {'avg net $':>10} {'daily $/ct':>11} {'risk':>8}")
    # Rough days in dataset: 9.6 (from earlier)
    days = 9.6
    summary_rows.sort(key=lambda r: r[7], reverse=True)  # by total net $
    seen_strats: set[str] = set()
    for r in summary_rows:
        strat, tp, n, tp_pct, avg_fill, med_fill, avg_net, tot_net, dd = r
        if strat in seen_strats:
            continue
        seen_strats.add(strat)
        fires_per_day = n / days
        avg_fill_s = avg_fill if avg_fill > 0 else float("nan")
        # daily expected: avg_net (cents) × fires/day / 100 = $
        daily_dollar = avg_net / 100.0 * fires_per_day
        print(f"  {strat:<10} +{tp:<3}c {fires_per_day:>9.1f} {tp_pct:>8.1f}% "
              f"{avg_fill_s:>8.0f}s {avg_net / 100:>+9.4f} "
              f"{daily_dollar:>+10.4f} {dd:>+7.2f}")
    print()

    # ── MFE distribution per strategy (no TP) ─────────────────────────
    print("=== MFE distribution per strategy (peak favorable excursion above entry) ===")
    for strat in ("BB_PURE", "ATM", "FVG"):
        outs = all_sigs[strat]
        if not outs:
            continue
        n = len(outs)
        print(f"\n  {strat} (n={n})")
        buckets = [(-50, 0), (0, 3), (3, 5), (5, 8), (8, 12), (12, 20),
                   (20, 30), (30, 50), (50, 100)]
        for lo, hi in buckets:
            sub = [o for o in outs if lo <= (o.mfe_c - o.sig.entry_c) < hi]
            pct = len(sub) / n * 100 if n else 0
            print(f"    {lo:>+3}c .. {hi:>+3}c:  n={len(sub):>5} ({pct:>5.1f}%)")
        avg_mfe = sum(o.mfe_c - o.sig.entry_c for o in outs) / n if n else 0
        avg_mae = sum(o.mae_c - o.sig.entry_c for o in outs) / n if n else 0
        avg_t = sum(o.mfe_at_s for o in outs) / n if n else 0
        print(f"    avg MFE: +{avg_mfe:.2f}c  avg MAE: {avg_mae:.2f}c  "
              f"avg time-to-MFE: {avg_t:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
