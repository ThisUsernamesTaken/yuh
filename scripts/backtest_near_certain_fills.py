"""Hunt for FVG entry conditions with near-100% TP fill rates.

Re-runs FVG signal discovery + tracks MFE per trade. Then for each
segment combination, reports TP fill rate at multiple TP levels using
the EXACT MFE — not a lower bound.

Heatmap output:
  rows: segment combinations (filter masks)
  cols: TP target (+3, +5, +8, +12, +15, +20, +25)

Each cell shows fill_rate% / avg_net$ at that (segment, TP) combo.
Cells with fill_rate >= 90/95/99% get *, **, *** markers.

Then prints "highest TP target maintaining >= 90% fill rate" per segment
+ projected daily $/contract.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"


# ── Math (reused) ──────────────────────────────────────────────────────


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


def btc_move(btc_index, at_ts, lookback_s):
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


# ── FVG signal + MFE tracker ──────────────────────────────────────────


@dataclass
class FvgRecord:
    entry_c: int
    side: str
    spread_c: int
    btc_dist_abs_pct: float
    session_age_s: int
    btc_5m_move: float
    aligned: bool
    mfe_c: int   # max favorable excursion (max bid on our side seen)


def find_fvg_signal(market, btc_index, candle_idx) -> FvgRecord | None:
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

    sig_idx = None
    for idx, (ts, yb, ya, pc) in enumerate(candles):
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
        btc_dist_abs = abs(btc - strike) / strike * 100.0
        if btc_dist_abs < 0.01 or btc_dist_abs > 0.15:
            continue
        sig_idx = idx
        break

    if sig_idx is None:
        return None

    # Reconstruct features
    ts, yb, ya, pc = candles[sig_idx]
    secs_remaining = close_ts - ts
    session_age = ts - open_ts
    mid = yes_mid_from_row(yb, ya, pc)
    vps = vol_per_sec(btc_index, ts, 60)
    btc_min = (ts // 60) * 60
    btc = btc_index.get(btc_min)
    fair = bb_fair_yes_cents(btc, strike, float(secs_remaining), vps)
    gap = fair - baseline
    if gap > 0:
        side = "yes"
        entry_c = min(mid, baseline)
    else:
        side = "no"
        entry_c = min(100 - mid, 100 - baseline)
    if entry_c < 5 or entry_c > 95:
        return None

    move_5m = btc_move(btc_index, ts, 300)
    if side == "yes":
        aligned = move_5m > 0
    else:
        aligned = move_5m < 0

    # Walk forward from sig_idx to compute MFE
    mfe = entry_c
    for ts2, yb2, ya2, pc2 in candles[sig_idx:]:
        if ts2 >= close_ts:
            break
        b = side_bid_from_row(side, yb2, ya2, pc2)
        if b is not None and b > mfe:
            mfe = b

    return FvgRecord(
        entry_c=entry_c, side=side, spread_c=abs(fair - mid),
        btc_dist_abs_pct=btc_dist_abs, session_age_s=session_age,
        btc_5m_move=move_5m, aligned=aligned, mfe_c=mfe,
    )


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1

    print("Loading + walking trades to compute MFE precisely...")
    con = sqlite3.connect(str(DB))
    markets = load_markets(con)
    btc_index = build_btc_index(con)
    candle_idx = build_candle_index(con)
    con.close()

    records: list[FvgRecord] = []
    for m in markets:
        rec = find_fvg_signal(m, btc_index, candle_idx)
        if rec:
            records.append(rec)
    print(f"  {len(records):,} FVG signals with MFE computed")
    print()

    segments = {
        "ALL_TRADES":              lambda r: True,
        "late_540+":               lambda r: r.session_age_s >= 540,
        "late_300+":               lambda r: r.session_age_s >= 300,
        "aligned":                 lambda r: r.aligned,
        "late_300+ & aligned":     lambda r: r.session_age_s >= 300 and r.aligned,
        "late_540+ & aligned":     lambda r: r.session_age_s >= 540 and r.aligned,
        "late_300+ & far_10+":     lambda r: r.session_age_s >= 300 and r.btc_dist_abs_pct >= 0.10,
        "late_540+ & far_10+":     lambda r: r.session_age_s >= 540 and r.btc_dist_abs_pct >= 0.10,
        "late_300+ & aligned & far_10+":
            lambda r: r.session_age_s >= 300 and r.aligned and r.btc_dist_abs_pct >= 0.10,
        "late_540+ & aligned & far_10+":
            lambda r: r.session_age_s >= 540 and r.aligned and r.btc_dist_abs_pct >= 0.10,
        "late_300+ & aligned & strong_btc":
            lambda r: r.session_age_s >= 300 and r.aligned and abs(r.btc_5m_move) >= 50,
        "60-69c entry & aligned":
            lambda r: 60 <= r.entry_c <= 69 and r.aligned,
        "60-69c entry & late_300+":
            lambda r: 60 <= r.entry_c <= 69 and r.session_age_s >= 300,
        "60-69c entry & late_540+":
            lambda r: 60 <= r.entry_c <= 69 and r.session_age_s >= 540,
    }
    tps = [3, 5, 8, 12, 15, 20, 25]

    # ── Heatmap ─────────────────────────────────────────────────────
    print("=" * 130)
    print("TP FILL RATE & NET $/TRADE — segment x TP target (precise from MFE)")
    print("=" * 130)
    header = f"  {'segment':<36} {'n':>5} "
    for tp in tps:
        header += f"  {'+' + str(tp) + 'c':>13}"
    print(header)
    print("  " + "-" * 36 + "  " + "-" * 5 + ("  " + "-" * 13) * len(tps))

    for sname, pred in segments.items():
        sub = [r for r in records if pred(r)]
        line = f"  {sname:<36} {len(sub):>5}"
        for tp in tps:
            if not sub:
                line += f"  {'—':>13}"
                continue
            target = tp
            fills = sum(1 for r in sub if (r.mfe_c - r.entry_c) >= target)
            fill_rate = fills / len(sub) * 100
            # Net $ per trade if we set TP at this offset.
            # Filled trades: profit = tp_offset minus fees.
            # Unfilled trades: held to ... well, we don't know. Use a
            # conservative proxy: those trades didn't hit +tp_offset, so
            # assume they hit pre-expiry or SL. Approximate per-trade loss
            # at -8c (the typical SL hit) for unfilled.
            avg_per_trade = 0.0
            for r in sub:
                if (r.mfe_c - r.entry_c) >= target:
                    tp_px = r.entry_c + target
                    gross = target
                    fee = kalshi_fee(r.entry_c) + kalshi_fee(tp_px)
                    avg_per_trade += gross - fee
                else:
                    # Conservative: assume SL@8c hit (it's our typical
                    # safety fallback). Real outcome distribution could be
                    # better OR worse; -8c entry is a reasonable midpoint.
                    sl_px = max(1, r.entry_c - 8)
                    gross = sl_px - r.entry_c
                    fee = kalshi_fee(r.entry_c) + kalshi_fee(sl_px, taker=True)
                    avg_per_trade += gross - fee
            avg_per_trade /= len(sub)
            avg_per_trade /= 100  # cents to dollars
            mark = ""
            if fill_rate >= 99:
                mark = "***"
            elif fill_rate >= 95:
                mark = "**"
            elif fill_rate >= 90:
                mark = "*"
            line += f"  {fill_rate:>4.1f}%/{avg_per_trade:+.3f}{mark:<3}"
        print(line)
    print()
    print("  ***  = >=99% fill (near-certain)")
    print("  **   = >=95% fill")
    print("  *    = >=90% fill")
    print()

    # ── Highest-TP-still-90%+ per segment ─────────────────────────────
    print("=" * 90)
    print("HIGHEST TP TARGET MAINTAINING >= 90% FILL RATE PER SEGMENT")
    print("=" * 90)
    print(f"  {'segment':<36} {'best TP':>8} {'fill%':>6} "
          f"{'avg net $':>10} {'fires/day':>10} {'$/day':>8}")
    days = 9.6
    n_total = len(records)
    for sname, pred in segments.items():
        sub = [r for r in records if pred(r)]
        if not sub:
            continue
        best_tp = None
        best_fill = 0.0
        best_avg = 0.0
        for tp in (3, 5, 8, 12, 15, 20, 25, 30, 35):
            fills = sum(1 for r in sub if (r.mfe_c - r.entry_c) >= tp)
            fill_rate = fills / len(sub) * 100
            if fill_rate >= 90:
                # Compute avg net at this TP
                avg = 0.0
                for r in sub:
                    if (r.mfe_c - r.entry_c) >= tp:
                        tp_px = r.entry_c + tp
                        gross = tp
                        fee = kalshi_fee(r.entry_c) + kalshi_fee(tp_px)
                        avg += gross - fee
                    else:
                        sl_px = max(1, r.entry_c - 8)
                        gross = sl_px - r.entry_c
                        fee = kalshi_fee(r.entry_c) + kalshi_fee(sl_px, taker=True)
                        avg += gross - fee
                avg = avg / len(sub) / 100
                best_tp = tp
                best_fill = fill_rate
                best_avg = avg
        if best_tp is None:
            continue
        fires_per_day = len(sub) / days
        daily = best_avg * fires_per_day
        print(f"  {sname:<36} {'+' + str(best_tp) + 'c':>8} "
              f"{best_fill:>5.1f}% {best_avg:>+9.4f} "
              f"{fires_per_day:>9.1f} {daily:>+7.3f}")
    print()

    # ── Pure MFE distribution by segment (where do they go?) ──────────
    print("=" * 80)
    print("AVG MFE PER SEGMENT (cents above entry that the bid reached)")
    print("=" * 80)
    print(f"  {'segment':<36} {'n':>5} {'avg MFE':>9} {'med MFE':>9} {'min':>5} {'max':>5}")
    for sname, pred in segments.items():
        sub = [r for r in records if pred(r)]
        if not sub:
            continue
        excs = sorted(r.mfe_c - r.entry_c for r in sub)
        avg_mfe = sum(excs) / len(excs)
        med_mfe = excs[len(excs) // 2]
        print(f"  {sname:<36} {len(sub):>5} +{avg_mfe:>7.1f}c "
              f"+{med_mfe:>7d}c {min(excs):>+4d}c {max(excs):>+4d}c")
    return 0


if __name__ == "__main__":
    sys.exit(main())
