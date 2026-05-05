"""FVG segmentation: which entry conditions dominate success vs failure?

Goal: take every FVG signal in the dataset, run through the +30c TP / SL=8c
simulator, then segment by entry features to find which conditions
correlate with success (high TP fill rate, positive avg net) vs failure.

Once we know the segment shape of the edge, we can:
  1. Size larger in high-confidence segments (Kelly × confidence_mult)
  2. Sit out below-average segments
  3. Set a baseline "best segment" config to deploy

Features tested:
  - entry price bucket (5-19c, 20-29c, ..., 80-95c)
  - side (yes vs no)
  - spread size (BB-fair − mid magnitude at entry)
  - BTC distance from strike (% past)
  - session age at entry (signal time bucket)
  - BTC 5-min move sign at entry (with-trend vs counter-trend)
  - BTC 15-min move sign at entry
  - Hour-of-day (UTC) — captures regime-of-day effects
  - aligned vs counter-aligned (FVG side vs BTC trend)

Output: ranked segments by avg net $/trade, with hit count + statistical
weight markers.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"
OUT_DIR = REPO / "scripts" / "_backtest_results"


# ── Math (lifted) ──────────────────────────────────────────────────────


def _ndtr(z): return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


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


def kalshi_fee(price_c, taker=False):
    p = max(0.0, min(1.0, price_c / 100.0))
    f = 0.07 * p * (1.0 - p) * 100.0
    return f * 1.4 if taker else f


def btc_move_dollars(btc_index, at_ts, lookback_s):
    now_min = (at_ts // 60) * 60
    p_now = btc_index.get(now_min)
    p_then = btc_index.get(now_min - lookback_s)
    if p_now is None or p_then is None:
        return 0.0
    return p_now - p_then


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


# ── FVG signal with full feature set ──────────────────────────────────


@dataclass
class FvgFeatures:
    ticker: str
    entry_ts: int
    side: str
    entry_c: int
    fair_for_side: int
    spread_c: int
    baseline_c: int
    btc_dist_pct: float          # signed: + above strike, − below
    btc_dist_abs_pct: float      # absolute distance
    session_age_s: int           # entry_ts − open_ts
    btc_5m_move: float           # $ move over last 5 min
    btc_15m_move: float          # $ move over last 15 min
    aligned_with_btc5m: bool     # FVG side matches BTC 5m direction
    hour_utc: int                # 0..23
    weekday: int                 # 0=Mon..6=Sun


def find_fvg_signal(market, btc_index, candle_idx):
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
        btc_dist_signed = (btc - strike) / strike * 100.0
        btc_dist_abs = abs(btc_dist_signed)
        if btc_dist_abs < 0.01 or btc_dist_abs > 0.15:
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
        move_5m = btc_move_dollars(btc_index, ts, 300)
        move_15m = btc_move_dollars(btc_index, ts, 900)
        # Aligned: FVG side direction agrees with BTC 5m direction
        if side == "yes":
            aligned = move_5m > 0
        else:
            aligned = move_5m < 0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return FvgFeatures(
            ticker=market["ticker"], entry_ts=ts, side=side, entry_c=entry_c,
            fair_for_side=fair_for_side, spread_c=abs(fair - mid),
            baseline_c=baseline, btc_dist_pct=btc_dist_signed,
            btc_dist_abs_pct=btc_dist_abs,
            session_age_s=session_age, btc_5m_move=move_5m,
            btc_15m_move=move_15m, aligned_with_btc5m=aligned,
            hour_utc=dt.hour, weekday=dt.weekday(),
        )
    return None


# ── Trade simulator ──────────────────────────────────────────────────


@dataclass
class TradeResult:
    feat: FvgFeatures
    exit_reason: str
    exit_c: int
    fill_at_s: int
    net_c: float


def simulate(feat, candles, close_ts, settle_yes, *,
             tp_offset=30, sl_offset=8) -> TradeResult:
    tp = feat.entry_c + tp_offset
    sl = feat.entry_c - sl_offset if sl_offset else None
    pre_exp = close_ts - 90
    for ts, yb, ya, pc in candles:
        if ts < feat.entry_ts:
            continue
        if ts >= close_ts:
            break
        b = side_bid_from_row(feat.side, yb, ya, pc)
        if b is None:
            continue
        elapsed = ts - feat.entry_ts
        if b >= tp:
            gross = tp - feat.entry_c
            fee = kalshi_fee(feat.entry_c) + kalshi_fee(tp)
            return TradeResult(feat, "TP", tp, elapsed, gross - fee)
        if sl is not None and b <= sl:
            x = max(1, b - 1)
            gross = x - feat.entry_c
            fee = kalshi_fee(feat.entry_c) + kalshi_fee(x, taker=True)
            return TradeResult(feat, "SL", x, elapsed, gross - fee)
        if ts >= pre_exp:
            x = max(1, b)
            gross = x - feat.entry_c
            fee = kalshi_fee(feat.entry_c) + kalshi_fee(x, taker=True)
            return TradeResult(feat, "PRE_EXP", x, elapsed, gross - fee)
    won = (feat.side == "yes" and settle_yes) or \
          (feat.side == "no" and not settle_yes)
    x = 100 if won else 0
    gross = x - feat.entry_c
    fee = kalshi_fee(feat.entry_c)
    return TradeResult(feat, "SETTLE_W" if won else "SETTLE_L", x,
                       close_ts - feat.entry_ts, gross - fee)


# ── Segment helpers ──────────────────────────────────────────────────


def summarise_segment(label: str, results: list[TradeResult], total_baseline: float):
    n = len(results)
    if not n:
        return None
    tp = sum(1 for r in results if r.exit_reason == "TP")
    sl = sum(1 for r in results if r.exit_reason == "SL")
    pre = sum(1 for r in results if r.exit_reason == "PRE_EXP")
    sw = sum(1 for r in results if r.exit_reason == "SETTLE_W")
    sl2 = sum(1 for r in results if r.exit_reason == "SETTLE_L")
    avg_net = sum(r.net_c for r in results) / n / 100.0
    total_net = sum(r.net_c for r in results) / 100.0
    delta = avg_net - total_baseline
    return {
        "label": label, "n": n, "tp": tp, "sl": sl, "preX": pre,
        "sW": sw, "sL": sl2, "avg_net": avg_net,
        "total_net": total_net, "delta": delta,
        "tp_pct": tp / n * 100,
    }


def print_segment_table(title: str, segments: list[dict],
                        total_baseline: float, *, sort_by: str = "avg_net"):
    if not segments:
        return
    print(f"\n  === {title} ===")
    print(f"    {'segment':<24} {'n':>5} {'TP%':>5} {'SL%':>5} "
          f"{'avg net $':>10} {'tot net $':>10} "
          f"{'vs base':>9}")
    sorted_segs = sorted(segments, key=lambda s: s[sort_by], reverse=True)
    for s in sorted_segs:
        marker = ""
        if s["delta"] > 0.01:
            marker = " ^"
        elif s["delta"] < -0.01:
            marker = " v"
        print(f"    {s['label']:<24} {s['n']:>5} "
              f"{s['tp_pct']:>4.1f}% "
              f"{s['sl'] / s['n'] * 100:>4.1f}% "
              f"{s['avg_net']:>+9.4f}  "
              f"{s['total_net']:>+9.2f} "
              f"{s['delta']:>+8.4f}{marker}")


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

    print("Running FVG @ TP+30c SL=8c with full feature extraction...", flush=True)
    results: list[TradeResult] = []
    for m in markets:
        feat = find_fvg_signal(m, btc_index, candle_idx)
        if not feat:
            continue
        candles = candle_idx.get(m["ticker"]) or []
        r = simulate(feat, candles, m["close_ts"], m["result"] == "yes",
                     tp_offset=30, sl_offset=8)
        results.append(r)
    print(f"  {len(results):,} trades simulated")
    print()

    n = len(results)
    baseline_avg = sum(r.net_c for r in results) / n / 100.0
    baseline_total = sum(r.net_c for r in results) / 100.0
    tp_baseline = sum(1 for r in results if r.exit_reason == "TP") / n * 100
    print(f"Baseline (all FVG @ +30c SL8): n={n} avg_net=${baseline_avg:+.4f} "
          f"total=${baseline_total:+.2f} TP_fill={tp_baseline:.1f}%")

    # ── Segment 1: entry price bucket ─────────────────────────────────
    bucket_segs = []
    buckets = [(5, 19), (20, 29), (30, 39), (40, 49), (50, 59),
               (60, 69), (70, 79), (80, 95)]
    for lo, hi in buckets:
        sub = [r for r in results if lo <= r.feat.entry_c <= hi]
        s = summarise_segment(f"entry {lo}-{hi}c", sub, baseline_avg)
        if s:
            bucket_segs.append(s)
    print_segment_table("Entry price bucket", bucket_segs, baseline_avg)

    # ── Segment 2: side ────────────────────────────────────────────────
    side_segs = []
    for side in ("yes", "no"):
        sub = [r for r in results if r.feat.side == side]
        s = summarise_segment(f"side={side}", sub, baseline_avg)
        if s:
            side_segs.append(s)
    print_segment_table("Side", side_segs, baseline_avg)

    # ── Segment 3: spread bucket (gap size) ───────────────────────────
    spread_segs = []
    spread_bins = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 60), (60, 100)]
    for lo, hi in spread_bins:
        sub = [r for r in results if lo <= r.feat.spread_c < hi]
        s = summarise_segment(f"spread {lo}-{hi}c", sub, baseline_avg)
        if s:
            spread_segs.append(s)
    print_segment_table("Spread (FVG gap size at entry)", spread_segs,
                        baseline_avg)

    # ── Segment 4: BTC distance from strike ───────────────────────────
    dist_segs = []
    dist_bins = [(0.01, 0.02), (0.02, 0.04), (0.04, 0.07),
                 (0.07, 0.10), (0.10, 0.15)]
    for lo, hi in dist_bins:
        sub = [r for r in results if lo <= r.feat.btc_dist_abs_pct < hi]
        s = summarise_segment(f"|dist| {lo:.2f}-{hi:.2f}%", sub, baseline_avg)
        if s:
            dist_segs.append(s)
    print_segment_table("BTC distance from strike (absolute %)",
                        dist_segs, baseline_avg)

    # ── Segment 5: session age at entry ───────────────────────────────
    age_segs = []
    age_bins = [(90, 180), (180, 300), (300, 420), (420, 540),
                (540, 660), (660, 780)]
    for lo, hi in age_bins:
        sub = [r for r in results if lo <= r.feat.session_age_s < hi]
        s = summarise_segment(f"age {lo}-{hi}s", sub, baseline_avg)
        if s:
            age_segs.append(s)
    print_segment_table("Session age at entry (seconds into 15-min window)",
                        age_segs, baseline_avg)

    # ── Segment 6: BTC 5m move sign + magnitude ───────────────────────
    move5_segs = []
    bins = [
        ("strong_down<-50", lambda x: x < -50),
        ("down -50..-20",   lambda x: -50 <= x < -20),
        ("flat -20..+20",   lambda x: -20 <= x < 20),
        ("up +20..+50",     lambda x: 20 <= x < 50),
        ("strong_up>50",    lambda x: x >= 50),
    ]
    for label, pred in bins:
        sub = [r for r in results if pred(r.feat.btc_5m_move)]
        s = summarise_segment(label, sub, baseline_avg)
        if s:
            move5_segs.append(s)
    print_segment_table("BTC 5-min move at entry", move5_segs, baseline_avg)

    # ── Segment 7: alignment with BTC 5m trend ────────────────────────
    align_segs = []
    sub_a = [r for r in results if r.feat.aligned_with_btc5m]
    sub_n = [r for r in results if not r.feat.aligned_with_btc5m]
    s = summarise_segment("aligned (with BTC5m)", sub_a, baseline_avg)
    if s:
        align_segs.append(s)
    s = summarise_segment("counter (vs BTC5m)", sub_n, baseline_avg)
    if s:
        align_segs.append(s)
    print_segment_table("Alignment with BTC 5-min direction",
                        align_segs, baseline_avg)

    # ── Segment 8: hour-of-day (UTC) ──────────────────────────────────
    hour_segs = []
    hour_groups = [
        ("00-04 UTC (US night)",  range(0, 4)),
        ("04-08 UTC (Asia mid)",  range(4, 8)),
        ("08-12 UTC (London open)", range(8, 12)),
        ("12-16 UTC (US open)",   range(12, 16)),
        ("16-20 UTC (US afternoon)", range(16, 20)),
        ("20-24 UTC (Late US/early Asia)", range(20, 24)),
    ]
    for label, hours in hour_groups:
        sub = [r for r in results if r.feat.hour_utc in hours]
        s = summarise_segment(label, sub, baseline_avg)
        if s:
            hour_segs.append(s)
    print_segment_table("Hour of day (UTC, by trading session)",
                        hour_segs, baseline_avg)

    # ── Cross-segment: entry × alignment ───────────────────────────────
    cross_segs = []
    for lo, hi in buckets:
        for is_aligned in (True, False):
            label_align = "aligned" if is_aligned else "counter"
            sub = [r for r in results
                   if lo <= r.feat.entry_c <= hi
                   and r.feat.aligned_with_btc5m == is_aligned]
            s = summarise_segment(f"{lo}-{hi}c × {label_align}", sub, baseline_avg)
            if s and s["n"] >= 30:
                cross_segs.append(s)
    print_segment_table("Cross: entry-bucket × alignment (n>=30 only)",
                        cross_segs, baseline_avg)

    # ═══════════════════════════════════════════════════════════════════
    # PART 2: Pairwise correlation across segment indicators
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PART 2: SEGMENT INDICATOR CORRELATIONS")
    print("=" * 70)
    print("  P(B|A) = probability of B given A. Diagonal = base rate P(A).")
    print("  If P(B|A) ~= P(B), independent. If P(B|A) >> P(B), correlated.")
    print()

    # Define indicator predicates
    indicators = {
        "late_540+":     lambda r: r.feat.session_age_s >= 540,
        "late_300+":     lambda r: r.feat.session_age_s >= 300,
        "aligned":       lambda r: r.feat.aligned_with_btc5m,
        "far_dist_10+":  lambda r: r.feat.btc_dist_abs_pct >= 0.10,
        "strong_btc_50+": lambda r: abs(r.feat.btc_5m_move) >= 50,
        "entry_60-69c":  lambda r: 60 <= r.feat.entry_c <= 69,
        "entry_30-49c":  lambda r: 30 <= r.feat.entry_c <= 49,
    }
    n_total = len(results)
    base_rates = {name: sum(1 for r in results if pred(r)) / n_total
                  for name, pred in indicators.items()}

    print(f"  {'indicator':<18} {'base rate':>10}")
    for name, p in base_rates.items():
        print(f"  {name:<18} {p:>9.1%}")
    print()

    # Pairwise conditional probabilities
    names = list(indicators.keys())
    print(f"  P(B | A)  rows=A, cols=B  (cells where A=True)")
    print(f"  {'A \\ B':<18} ", end="")
    for b in names:
        print(f"{b[:9]:>10}", end="")
    print()
    for a in names:
        a_set = [r for r in results if indicators[a](r)]
        if not a_set:
            continue
        print(f"  {a:<18} ", end="")
        for b in names:
            cond = sum(1 for r in a_set if indicators[b](r)) / len(a_set)
            marker = ""
            if a != b:
                base = base_rates[b]
                if cond > base * 1.4:
                    marker = "+"
                elif cond < base * 0.6:
                    marker = "-"
            print(f"{cond:>9.1%}{marker:<1}", end="")
        print()
    print()

    # ═══════════════════════════════════════════════════════════════════
    # PART 3: Filter+multiplier scheme — projected vs actual
    # ═══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("PART 3: FILTER + MULTIPLIER SCHEME VALIDATION")
    print("=" * 70)

    def compute_size_mult(r) -> tuple[float, str]:
        """Return (size_multiplier, reason). 0 = refuse fire."""
        f = r.feat
        # Hard filters
        if f.session_age_s < 180:
            return 0.0, "REFUSE: session_age < 180s"
        if not f.aligned_with_btc5m and 30 <= f.entry_c <= 49:
            return 0.0, "REFUSE: counter+midprice"
        if abs(f.btc_5m_move) < 20 and not f.aligned_with_btc5m:
            return 0.0, "REFUSE: flat+counter"
        # Multipliers
        m = 1.0
        if f.session_age_s >= 540:
            m *= 1.5
        elif f.session_age_s >= 300:
            m *= 1.25
        if f.aligned_with_btc5m:
            m *= 1.2
        if f.btc_dist_abs_pct >= 0.10:
            m *= 1.2
        if abs(f.btc_5m_move) >= 50:
            m *= 1.15
        return min(m, 2.5), "FIRE"

    schemes = [
        ("BASELINE (no filter, mult=1)", lambda r: (1.0, "")),
        ("Filter only (no multipliers)",
         lambda r: (1.0 if compute_size_mult(r)[0] > 0 else 0.0, "")),
        ("Filter + multipliers (proposed)", compute_size_mult),
        ("Hard-late only (age >= 540, aligned)",
         lambda r: (
             (1.5 if r.feat.session_age_s >= 540
              and r.feat.aligned_with_btc5m else 0.0),
             "")),
    ]

    print(f"  {'scheme':<40} {'fires':>5} {'tot ct':>7} "
          f"{'tot $':>9} {'avg $/ct':>10} {'avg $/fire':>11}")
    for name, fn in schemes:
        fires = 0
        tot_contracts = 0.0
        tot_dollars = 0.0
        for r in results:
            mult, _ = fn(r)
            if mult <= 0:
                continue
            fires += 1
            tot_contracts += mult
            tot_dollars += r.net_c * mult / 100.0
        if fires == 0:
            print(f"  {name:<40} {0:>5} {0:>7.1f} {0:>9.2f}")
            continue
        avg_per_ct = tot_dollars / tot_contracts if tot_contracts else 0
        avg_per_fire = tot_dollars / fires
        print(f"  {name:<40} {fires:>5} {tot_contracts:>7.1f} "
              f"{tot_dollars:>+8.2f} {avg_per_ct:>+9.4f} "
              f"{avg_per_fire:>+10.4f}")
    print()

    # Daily P&L projection
    days = 9.6
    print("  Daily P&L projection (per 1ct base size):")
    for name, fn in schemes:
        fires = sum(1 for r in results if fn(r)[0] > 0)
        tot_contracts = sum(fn(r)[0] for r in results if fn(r)[0] > 0)
        tot_dollars = sum(r.net_c * fn(r)[0] / 100.0
                          for r in results if fn(r)[0] > 0)
        if not fires:
            continue
        fires_per_day = fires / days
        contracts_per_day = tot_contracts / days
        dollars_per_day = tot_dollars / days
        # Equity-curve max DD with size weighting
        eq = peak = 0.0
        dd = 0.0
        for r in results:
            mult, _ = fn(r)
            if mult <= 0:
                continue
            eq += r.net_c * mult / 100.0
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        print(f"    {name:<40} fires/day={fires_per_day:>5.1f} "
              f"$/day={dollars_per_day:>+6.3f} max DD=${dd:>+6.2f}")
    print()

    # ── Save full per-trade CSV with feature columns ─────────────────
    csv_path = OUT_DIR / "fvg_segmented_trades.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "entry_ts", "side", "entry_c", "fair_for_side",
            "spread_c", "baseline_c", "btc_dist_signed_pct",
            "btc_dist_abs_pct", "session_age_s", "btc_5m_move",
            "btc_15m_move", "aligned_with_btc5m", "hour_utc",
            "exit_reason", "exit_c", "fill_at_s", "net_c",
        ])
        for r in results:
            w.writerow([
                r.feat.ticker, r.feat.entry_ts, r.feat.side, r.feat.entry_c,
                r.feat.fair_for_side, r.feat.spread_c, r.feat.baseline_c,
                f"{r.feat.btc_dist_pct:.4f}",
                f"{r.feat.btc_dist_abs_pct:.4f}", r.feat.session_age_s,
                f"{r.feat.btc_5m_move:.2f}", f"{r.feat.btc_15m_move:.2f}",
                int(r.feat.aligned_with_btc5m), r.feat.hour_utc,
                r.exit_reason, r.exit_c, r.fill_at_s, f"{r.net_c:.2f}",
            ])
    print(f"\n  per-trade CSV: {csv_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
