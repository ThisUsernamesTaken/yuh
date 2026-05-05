"""Consensus classifier: combine all strategies as votes for expiry direction.

User reframe (2026-05-04): instead of picking the most profitable single
strategy, find the COMBINATION of strategy votes that predicts session
expiry (yes/no) with highest accuracy. Then work backwards to monetization.

Per market, every strategy that "fires" outputs a direction (yes/no).
Strategies that don't fire abstain. Consensus prediction is based on the
margin between yes-votes and no-votes:

  yes_votes - no_votes >= K  →  predict YES
  no_votes - yes_votes >= K  →  predict NO
  else                       →  no prediction (abstain)

We sweep K (consensus threshold) and report:
  - Coverage: % of markets where consensus fires
  - Accuracy: % of fired predictions that match settlement
  - Per-direction breakdown (YES-pred accuracy vs NO-pred accuracy)
  - Per-vote-shape breakdown (which exact combinations are most accurate)

If a high-K consensus shows e.g. 80%+ accuracy, that's a directional
edge we can monetize at any TP target — just buy the predicted side
near-mid and exit when the price moves in our favor.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"
OUT_DIR = REPO / "scripts" / "_backtest_results"


# ── Math (lifted) ───────────────────────────────────────────────────────


def _ndtr(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bb_fair_yes_cents(btc, strike, secs_left, vps):
    if secs_left <= 0:
        return 100 if btc >= strike else 0
    if vps <= 0:
        return 50
    sigma = vps * math.sqrt(secs_left)
    if sigma <= 0:
        return 100 if btc >= strike else 0
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


def btc_move(btc_index, at_ts, lookback_s):
    now_min = (at_ts // 60) * 60
    p_now = btc_index.get(now_min)
    p_then = btc_index.get(now_min - lookback_s)
    if p_now is None or p_then is None:
        return 0.0
    return p_now - p_then


def yes_mid(yb_d, ya_d, pc_d):
    if yb_d is not None and ya_d is not None and yb_d > 0 and ya_d > 0:
        return int(round((float(yb_d) + float(ya_d)) * 50))
    if pc_d is not None and pc_d > 0:
        return int(round(float(pc_d) * 100))
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
        mid = int(round(float(pc) * 100))
        bid = mid
        ask = mid
    else:
        return None
    if mid <= 0 or mid >= 100:
        return None
    return mid, bid, ask


# ── Loaders ─────────────────────────────────────────────────────────────


def load_markets(con):
    cur = con.cursor()
    cur.execute("SELECT ticker, open_ts, close_ts, result, raw_json FROM markets "
                "WHERE status='finalized' AND result IN ('yes','no')")
    out = []
    for ticker, ots, cts, res, raw in cur.fetchall():
        try:
            j = json.loads(raw)
        except Exception:
            continue
        try:
            strike = float(j.get("floor_strike") or 0)
        except Exception:
            strike = 0.0
        if strike <= 0 or ots is None or cts is None:
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


# ── Strategy "voters": each returns "yes" / "no" / None ─────────────────


def vote_bb_pure_meanrev(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist > 0.0004:
        return None
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    edge = fair - mid
    if abs(edge) < 8:
        return None
    return "yes" if edge > 0 else "no"


def vote_bb_trend(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist < 0.0015:
        return None
    btc_above = btc > market["strike"]
    move_5m = btc_move(btc_index, entry_ts, 300)
    if btc_above and move_5m <= -50:
        return None
    if (not btc_above) and move_5m >= 50:
        return None
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair_yes = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    side = "yes" if btc_above else "no"
    fair_for_side = fair_yes if side == "yes" else (100 - fair_yes)
    side_mid = mid if side == "yes" else (100 - mid)
    if (fair_for_side - side_mid) < 8:
        return None
    return side


def vote_btc_position(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """Trivial baseline: vote the side BTC is currently on."""
    if btc > market["strike"]:
        return "yes"
    if btc < market["strike"]:
        return "no"
    return None


def vote_btc_trend5m(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """Vote based on 5-minute BTC dollar move sign (>$5 deadband)."""
    delta = btc_move(btc_index, entry_ts, 300)
    if delta > 5:
        return "yes"
    if delta < -5:
        return "no"
    return None


def vote_btc_trend15m(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """Vote based on 15-min BTC dollar move sign (>$10 deadband)."""
    delta = btc_move(btc_index, entry_ts, 900)
    if delta > 10:
        return "yes"
    if delta < -10:
        return "no"
    return None


def vote_market_mid(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """Vote according to where the market itself thinks: mid > 50c → YES."""
    if mid >= 55:
        return "yes"
    if mid <= 45:
        return "no"
    return None


def vote_bb_fair(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """Vote according to the BB model alone: fair > 55c → YES."""
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    if fair >= 55:
        return "yes"
    if fair <= 45:
        return "no"
    return None


def vote_atm_reversion(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist > 0.0003:
        return None
    if bid <= 0 or ask <= 0:
        return None
    yes_ask = ask
    no_ask = 100 - bid
    if no_ask <= 0:
        return None
    yes_disc = yes_ask <= 35 and (50 - yes_ask) >= 8
    no_disc = no_ask <= 35 and (50 - no_ask) >= 8
    yes_edge = 50 - yes_ask
    no_edge = 50 - no_ask
    if yes_disc and (not no_disc or yes_edge >= no_edge):
        return "yes"
    if no_disc:
        return "no"
    return None


def vote_fvg(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    """FVG vote (single-tick — not the full state machine; uses BB fair vs
    current mid as a fast proxy for the gap direction)."""
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    btc_dist_pct = abs(btc - market["strike"]) / market["strike"] * 100.0
    if btc_dist_pct < 0.01 or btc_dist_pct > 0.15:
        return None
    gap = fair - mid
    if abs(gap) < 8:
        return None
    return "yes" if gap > 0 else "no"


VOTERS: dict[str, callable] = {
    "BB_PURE":      vote_bb_pure_meanrev,
    "BB_TREND":     vote_bb_trend,
    "BTC_POS":      vote_btc_position,
    "BTC_TREND5":   vote_btc_trend5m,
    "BTC_TREND15":  vote_btc_trend15m,
    "MARKET_MID":   vote_market_mid,
    "BB_FAIR":      vote_bb_fair,
    "ATM_DISC":     vote_atm_reversion,
    "FVG":          vote_fvg,
}


# ── Vote collector ──────────────────────────────────────────────────────


def collect_votes(market, btc_index, candle_idx, entry_offset_s):
    entry_ts = market["open_ts"] + entry_offset_s
    if entry_ts >= market["close_ts"]:
        return None
    btc_min = (entry_ts // 60) * 60
    btc = btc_index.get(btc_min)
    if not btc or btc <= 0:
        return None
    secs_left = float(market["close_ts"] - entry_ts)
    candles = candle_idx.get(market["ticker"]) or []
    kp = kalshi_yes_at(candles, entry_ts)
    if not kp:
        return None
    mid, bid, ask = kp
    votes = {}
    for name, fn in VOTERS.items():
        try:
            votes[name] = fn(market, btc, secs_left, mid, bid, ask,
                             btc_index, entry_ts)
        except Exception:
            votes[name] = None
    return votes


# ── Consensus prediction ───────────────────────────────────────────────


def consensus(votes: dict[str, str | None], threshold: int) -> tuple[str | None, int, int]:
    yes = sum(1 for v in votes.values() if v == "yes")
    no = sum(1 for v in votes.values() if v == "no")
    if yes - no >= threshold:
        return "yes", yes, no
    if no - yes >= threshold:
        return "no", yes, no
    return None, yes, no


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
    print(f"  {len(markets):,} markets / {len(btc_index):,} BTC mins / "
          f"{len(candle_idx):,} candle tickers")
    print()

    # ── Per-voter standalone accuracy at T300 ──────────────────────────
    print("=== Per-voter standalone accuracy @ T300 ===")
    print(f"  {'voter':<14} {'fires':>6} {'cov%':>5} {'yes_w':>5} {'no_w':>5} "
          f"{'yes_acc%':>8} {'no_acc%':>8} {'overall%':>8}")
    voter_stats = defaultdict(lambda: {"yes_total": 0, "yes_correct": 0,
                                        "no_total": 0, "no_correct": 0,
                                        "fires": 0})
    all_votes_per_market = []
    for m in markets:
        v = collect_votes(m, btc_index, candle_idx, 300)
        if v is None:
            continue
        all_votes_per_market.append((m, v))
        for name, vote in v.items():
            if vote is None:
                continue
            voter_stats[name]["fires"] += 1
            actual = m["result"]
            if vote == "yes":
                voter_stats[name]["yes_total"] += 1
                if actual == "yes":
                    voter_stats[name]["yes_correct"] += 1
            else:
                voter_stats[name]["no_total"] += 1
                if actual == "no":
                    voter_stats[name]["no_correct"] += 1
    n_eval = len(all_votes_per_market)
    for name in VOTERS:
        s = voter_stats[name]
        fires = s["fires"]
        cov = fires / n_eval * 100 if n_eval else 0
        yes_acc = s["yes_correct"] / s["yes_total"] * 100 if s["yes_total"] else 0
        no_acc = s["no_correct"] / s["no_total"] * 100 if s["no_total"] else 0
        total_correct = s["yes_correct"] + s["no_correct"]
        overall = total_correct / fires * 100 if fires else 0
        print(f"  {name:<14} {fires:>6d} {cov:>4.1f}% "
              f"{s['yes_total']:>5d} {s['no_total']:>5d} "
              f"{yes_acc:>7.1f}% {no_acc:>7.1f}% {overall:>7.1f}%")
    print()

    # ── Consensus accuracy by threshold ──────────────────────────────
    print("=== Consensus accuracy by threshold (vote margin) ===")
    print(f"  {'K':>2}  {'fires':>6} {'cov%':>5} "
          f"{'yes_pred':>8} {'no_pred':>7} "
          f"{'correct':>7} {'acc%':>6}  "
          f"{'yes_acc%':>8} {'no_acc%':>8}")
    for K in (1, 2, 3, 4, 5, 6, 7):
        stats = {"fires": 0, "correct": 0,
                 "yes_pred": 0, "yes_correct": 0,
                 "no_pred": 0, "no_correct": 0}
        for m, v in all_votes_per_market:
            pred, _, _ = consensus(v, K)
            if pred is None:
                continue
            stats["fires"] += 1
            actual = m["result"]
            if pred == "yes":
                stats["yes_pred"] += 1
                if actual == "yes":
                    stats["yes_correct"] += 1
            else:
                stats["no_pred"] += 1
                if actual == "no":
                    stats["no_correct"] += 1
            stats["correct"] = stats["yes_correct"] + stats["no_correct"]
        cov = stats["fires"] / n_eval * 100 if n_eval else 0
        acc = stats["correct"] / stats["fires"] * 100 if stats["fires"] else 0
        ya = stats["yes_correct"] / stats["yes_pred"] * 100 if stats["yes_pred"] else 0
        na = stats["no_correct"] / stats["no_pred"] * 100 if stats["no_pred"] else 0
        print(f"  {K:>2}  {stats['fires']:>6d} {cov:>4.1f}% "
              f"{stats['yes_pred']:>8d} {stats['no_pred']:>7d} "
              f"{stats['correct']:>7d} {acc:>5.1f}% "
              f"{ya:>7.1f}% {na:>7.1f}%")
    print()

    # ── Top exact vote shapes (sorted by accuracy, n>=20) ─────────────
    print("=== Top 15 vote shapes by accuracy (n>=20, sorted by acc) ===")
    shape_stats = defaultdict(lambda: {"yes_actual": 0, "no_actual": 0})
    for m, v in all_votes_per_market:
        # Compact shape: tuple of (name, vote) for fired voters only
        shape_items = tuple(sorted(
            (n, vt) for n, vt in v.items() if vt is not None
        ))
        actual = m["result"]
        shape_stats[shape_items][f"{actual}_actual"] += 1

    rows = []
    for shape, counts in shape_stats.items():
        n = counts["yes_actual"] + counts["no_actual"]
        if n < 20:
            continue
        # Predicted direction = majority of votes in shape
        yes_v = sum(1 for _, vt in shape if vt == "yes")
        no_v = sum(1 for _, vt in shape if vt == "no")
        if yes_v > no_v:
            pred = "yes"
            correct = counts["yes_actual"]
        elif no_v > yes_v:
            pred = "no"
            correct = counts["no_actual"]
        else:
            continue  # tie
        acc = correct / n * 100
        rows.append((acc, n, shape, pred, correct))
    rows.sort(reverse=True)
    print(f"  {'acc%':>5} {'n':>5} {'pred':>4}  shape")
    for acc, n, shape, pred, correct in rows[:15]:
        shape_str = " ".join(f"{n_}={v_}" for n_, v_ in shape)
        print(f"  {acc:>4.1f}% {n:>5} {pred:>4}  {shape_str}")
    print()

    # ── Consensus monetization: pick contract on consensus side ─────────
    # Premise: at K=2 or 3 with 70%+ accuracy, just buy the predicted side
    # at current mid and hold (or take a sensible TP).
    print("=== Consensus monetization simulation ===")
    print("  Buy at side mid + hold (no SL/TP, settle-only). 1 contract.")
    print(f"  {'K':>2} {'n':>6} {'acc%':>5} {'avg entry $':>11} "
          f"{'gross $':>9} {'fee $':>7} {'net $':>9} {'avg/trade $':>11}")
    for K in (1, 2, 3, 4, 5):
        n_trades = 0
        gross_c = 0.0
        fee_c = 0.0
        correct = 0
        avg_entry_c_sum = 0
        for m, v in all_votes_per_market:
            pred, _, _ = consensus(v, K)
            if pred is None:
                continue
            entry_ts = m["open_ts"] + 300
            candles = candle_idx.get(m["ticker"]) or []
            kp = kalshi_yes_at(candles, entry_ts)
            if not kp:
                continue
            mid, bid, ask = kp
            if pred == "yes":
                entry_c = mid
            else:
                entry_c = 100 - mid
            actual = m["result"]
            won = (pred == actual)
            gross = (100 - entry_c) if won else -entry_c
            # Maker entry + no-fee settle
            fee = 0.07 * (entry_c / 100.0) * (1 - entry_c / 100.0) * 100.0
            n_trades += 1
            gross_c += gross
            fee_c += fee
            avg_entry_c_sum += entry_c
            if won:
                correct += 1
        if not n_trades:
            print(f"  {K:>2} {0:>6}  (no fires)")
            continue
        net_c = gross_c - fee_c
        acc = correct / n_trades * 100
        print(f"  {K:>2} {n_trades:>6} {acc:>4.1f}% "
              f"{avg_entry_c_sum / n_trades / 100:>10.2f} "
              f"{gross_c / 100:>+8.2f} {fee_c / 100:>6.2f} "
              f"{net_c / 100:>+8.2f} {net_c / 100 / n_trades:>+10.4f}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
