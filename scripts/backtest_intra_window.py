"""Intra-window-aware backtest with stop-loss / TP / pre-expiry simulation.

Walks the per-minute Kalshi candle stream for each market from entry to
close, simulating a held position with realistic exit triggers:

    TP_FILL     side_bid >= entry + tp_offset (maker fee, post_only resting)
    SL_HIT      side_bid <= entry - sl_offset (taker fee, cross-spread)
    PRE_EXPIRY  ts >= close_ts - pre_expiry_buffer_s (taker fee)
    SETTLE_*    no exit fired; result decides win/loss (no fee)

Fees: 7% × p × (1−p) per leg, charged on every fill.

Compares with-SL vs no-SL for each of the 5 strategy/timing variants we
care about, plus a fresh FVG evaluator that mirrors the in-engine
PAPER_FVG state machine.

Usage:
    python scripts/backtest_intra_window.py
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


# ── BB / vol math (lifted) ─────────────────────────────────────────────


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


# ── Fees ────────────────────────────────────────────────────────────────


def kalshi_fee_cents(price_c: int, contracts: int = 1, taker: bool = False) -> float:
    """Per-leg Kalshi fee: 7% × p × (1−p) per contract; taker pays 1.4x.

    (Per Kalshi docs Apr 2025 — taker fee bumped 40% above maker.)
    """
    p = max(0.0, min(1.0, float(price_c) / 100.0))
    per_c = 0.07 * p * (1.0 - p) * 100.0
    if taker:
        per_c *= 1.4
    return per_c * contracts


# ── Loaders ─────────────────────────────────────────────────────────────


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


# ── Side-bid extraction from a candle row ───────────────────────────────


def side_bid_from_candle(side: str, yb_d, ya_d, pc_d) -> int | None:
    """Return the bid we would receive if we sold this side at this candle."""
    if yb_d is not None and ya_d is not None and yb_d > 0 and ya_d > 0:
        if side == "yes":
            return int(round(float(yb_d) * 100))
        return int(round((1.0 - float(ya_d)) * 100))
    if pc_d is not None and pc_d > 0:
        # Fallback to price_close (mid-ish)
        if side == "yes":
            return int(round(float(pc_d) * 100))
        return int(round((1.0 - float(pc_d)) * 100))
    return None


def kalshi_yes_at(candles: list[tuple], target_ts: int) -> tuple[int, int, int] | None:
    """Return (yes_mid, yes_bid, yes_ask) at the most recent candle ≤ target_ts."""
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


# ── Intra-window position walker ────────────────────────────────────────


@dataclass
class TradeOutcome:
    exit_reason: str   # TP_FILL, SL_HIT, PRE_EXPIRY, SETTLE_WIN, SETTLE_LOSS
    exit_c: int
    gross_c: float
    fee_c: float
    net_c: float


def simulate_held_position(
    candles: list[tuple],
    entry_ts: int,
    close_ts: int,
    settle_yes: bool,
    side: str,
    entry_c: int,
    *,
    sl_offset_c: int = 8,
    tp_offset_c: int = 8,
    pre_expiry_buffer_s: int = 90,
    use_sl: bool = True,
) -> TradeOutcome:
    """Walk the candle stream from entry to close. Return realistic exit."""
    sl_trigger = entry_c - sl_offset_c
    tp_target = entry_c + tp_offset_c
    pre_expiry_ts = close_ts - pre_expiry_buffer_s

    exit_reason = None
    exit_c = 0
    taker_exit = False

    for ts, yb_d, ya_d, pc_d in candles:
        if ts < entry_ts:
            continue
        if ts >= close_ts:
            break
        side_bid = side_bid_from_candle(side, yb_d, ya_d, pc_d)
        if side_bid is None:
            continue
        # Order matters: TP fills first if multiple conditions hit on the
        # same candle (TP is a resting maker order, takes priority).
        if side_bid >= tp_target:
            exit_reason = "TP_FILL"
            exit_c = tp_target
            taker_exit = False
            break
        if use_sl and side_bid <= sl_trigger:
            exit_reason = "SL_HIT"
            # Cross-spread exit at bid - 1 (taker)
            exit_c = max(1, side_bid - 1)
            taker_exit = True
            break
        if ts >= pre_expiry_ts:
            exit_reason = "PRE_EXPIRY"
            exit_c = max(1, side_bid)
            taker_exit = True
            break

    if exit_reason is None:
        # No intra-window exit — settle at result.
        won = (side == "yes" and settle_yes) or (side == "no" and not settle_yes)
        if won:
            exit_reason = "SETTLE_WIN"
            exit_c = 100
        else:
            exit_reason = "SETTLE_LOSS"
            exit_c = 0
        # Settlement has no fee.
        gross = exit_c - entry_c
        # Entry fee (maker) is the only fee on settle.
        fee = kalshi_fee_cents(entry_c, taker=False)
        return TradeOutcome(exit_reason, exit_c, float(gross), float(fee),
                            float(gross - fee))

    gross = exit_c - entry_c
    # Entry leg always maker; exit leg maker if TP, taker if SL/PRE_EXPIRY.
    fee_entry = kalshi_fee_cents(entry_c, taker=False)
    fee_exit = kalshi_fee_cents(exit_c, taker=taker_exit)
    fee = fee_entry + fee_exit
    return TradeOutcome(exit_reason, exit_c, float(gross), float(fee),
                        float(gross - fee))


# ── Strategy evaluators (return entry side+price, OR None) ──────────────


def eval_bb_pure_meanrev(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
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
    if edge > 0:
        side, entry_c = "yes", mid
    else:
        side, entry_c = "no", 100 - mid
    if entry_c < 5 or entry_c > 55:
        return None
    delta = btc_move_dollars(btc_index, entry_ts, 300)
    if abs(delta) > 5:
        aligned = "yes" if delta > 0 else "no"
        if side != aligned:
            return None
    # TP target: max(fair-1 in our-side units, entry+8)
    fair_for_side = fair if side == "yes" else (100 - fair)
    tp_offset = max(int(fair_for_side - 1 - entry_c), 8)
    return side, entry_c, tp_offset


def eval_bb_trend(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist < 0.0015:
        return None
    btc_above = btc > market["strike"]
    move_5m = btc_move_dollars(btc_index, entry_ts, 300)
    if btc_above and move_5m <= -50:
        return None
    if (not btc_above) and move_5m >= 50:
        return None
    side = "yes" if btc_above else "no"
    entry_c = mid if side == "yes" else (100 - mid)
    vps = vol_per_sec(btc_index, entry_ts, 60)
    if vps <= 0:
        return None
    fair_yes = bb_fair_yes_cents(btc, market["strike"], secs_left, vps)
    fair_for_side = fair_yes if side == "yes" else (100 - fair_yes)
    if (fair_for_side - entry_c) < 8:
        return None
    if entry_c < 5 or entry_c > 80:
        return None
    tp_offset = max(int(fair_for_side - 1 - entry_c), 8)
    return side, entry_c, tp_offset


def eval_atm_reversion(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist > 0.0003:
        return None
    if bid <= 0 or ask <= 0 or ask >= 100:
        return None
    yes_ask = ask
    no_ask = 100 - bid
    if no_ask <= 0:
        return None
    fair = 50.0
    yes_edge = fair - yes_ask
    no_edge = fair - no_ask
    yes_disc = yes_ask <= 35 and fair >= 47.0 and yes_edge >= 8
    no_disc = no_ask <= 35 and fair >= 47.0 and no_edge >= 8
    if yes_disc and (not no_disc or yes_edge >= no_edge):
        return "yes", yes_ask, 8  # ATM: target +8c (matches in-engine ATM_PROFIT_TARGET)
    if no_disc:
        return "no", no_ask, 8
    return None


def eval_full_stack(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts):
    dist = abs(btc - market["strike"]) / btc
    if dist >= 0.0015:
        return eval_bb_trend(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts)
    if dist <= 0.0004:
        return eval_bb_pure_meanrev(market, btc, secs_left, mid, bid, ask, btc_index, entry_ts)
    return None


# ── FVG evaluator: state machine that simulates baseline collection +
#    first FVG signal in the window. Mirrors _paper_fvg_tick. ──────────


def eval_fvg_first_signal(market, btc_index, candle_idx) -> tuple[int, str, int, int] | None:
    """Walk the market from open to close, replicating the FVG state
    machine. Return (entry_ts, side, entry_c, tp_offset) at the FIRST
    qualifying signal, or None if no signal fires in the window.

    Differs from the in-engine PAPER_FVG: we do NOT model multiple cycles
    per session. We take the first valid entry only. Subsequent live-cycle
    behavior is downstream of this entry.
    """
    candles = candle_idx.get(market["ticker"]) or []
    if not candles:
        return None
    open_ts = market["open_ts"]
    close_ts = market["close_ts"]
    strike = market["strike"]

    # ── BASELINE PHASE: collect mids from t=open to t=open+120 ──────────
    # The in-engine PAPER_FVG samples WS mids every ~0.4s and requires 10
    # samples; here we only have 1m candles, so we extend the collection
    # window to the first 2 minutes and accept ≥1 sample. With 1m candles
    # the baseline is necessarily noisier — that just makes the FVG signal
    # threshold slightly less precise.
    baseline_mids: list[int] = []
    for ts, yb, ya, pc in candles:
        if ts < open_ts:
            continue
        if ts > open_ts + 120:
            break
        kp = side_bid_yes_mid_from_row(yb, ya, pc)
        if kp is not None:
            baseline_mids.append(kp)
    if not baseline_mids:
        return None
    baseline = int(sum(baseline_mids) / len(baseline_mids))

    # ── IDLE: walk forward looking for first FVG signal ───────────────
    for ts, yb, ya, pc in candles:
        if ts < open_ts + 90:
            continue
        if ts >= close_ts:
            break
        secs_remaining = close_ts - ts
        if secs_remaining < 120:
            return None  # FVG won't fire with <120s left
        session_age = ts - open_ts
        if session_age < 180:
            fvg_threshold = 5
        elif session_age < 420:
            fvg_threshold = 8
        else:
            fvg_threshold = 12

        mid = side_bid_yes_mid_from_row(yb, ya, pc)
        if mid is None:
            continue

        # BB fair value at this candle.
        vps = vol_per_sec(_BTC_INDEX_CACHE, ts, 60)
        if vps <= 0:
            continue
        # Need BTC at this minute.
        btc_min = (ts // 60) * 60
        btc = _BTC_INDEX_CACHE.get(btc_min)
        if not btc or btc <= 0:
            continue
        fair = bb_fair_yes_cents(btc, strike, float(secs_remaining), vps)
        fvg_vs_baseline = fair - baseline
        if abs(fvg_vs_baseline) < fvg_threshold:
            continue

        # BTC distance gate (matches in-engine: >0.01% but not >0.15%).
        btc_dist_pct = abs(btc - strike) / strike * 100.0
        if btc_dist_pct < 0.01 or btc_dist_pct > 0.15:
            continue

        if fvg_vs_baseline > 0:
            side = "yes"
            entry_c = min(mid, baseline)
        else:
            side = "no"
            entry_c = min(100 - mid, 100 - baseline)
        if entry_c < 5 or entry_c > 95:
            continue
        spread = abs(fair - mid)
        if spread < 5:
            spread = 5
        tp_offset = max(int(spread * 0.8), 8)
        return ts, side, entry_c, tp_offset

    return None


def side_bid_yes_mid_from_row(yb_d, ya_d, pc_d) -> int | None:
    """Return YES mid in cents from a candle row."""
    if yb_d is not None and ya_d is not None and yb_d > 0 and ya_d > 0:
        bid = int(round(float(yb_d) * 100))
        ask = int(round(float(ya_d) * 100))
        return (bid + ask) // 2
    if pc_d is not None and pc_d > 0:
        return int(round(float(pc_d) * 100))
    return None


# ── Cache for FVG (lazily set in main) ──────────────────────────────────


_BTC_INDEX_CACHE: dict[int, float] = {}


# ── Runner ──────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    label: str
    ticker: str
    side: str
    entry_c: int
    exit_reason: str
    exit_c: int
    gross_c: float
    fee_c: float
    net_c: float
    settle_yes: bool


def run_strategy(
    name: str, evaluator,
    timing_label: str, offset_s: int,
    use_sl: bool,
    markets, btc_index, candle_idx,
    *,
    sl_offset_c: int = 8,
) -> list[TradeRecord]:
    label = f"{name}@{timing_label}{'_SL' if use_sl else '_NOSL'}"
    out: list[TradeRecord] = []
    for m in markets:
        entry_ts = m["open_ts"] + offset_s
        if entry_ts >= m["close_ts"]:
            continue
        btc_min = (entry_ts // 60) * 60
        btc = btc_index.get(btc_min)
        if not btc or btc <= 0:
            continue
        secs_left = float(m["close_ts"] - entry_ts)
        candles = candle_idx.get(m["ticker"]) or []
        kp = kalshi_yes_at(candles, entry_ts)
        if not kp:
            continue
        mid, bid, ask = kp
        result = evaluator(m, btc, secs_left, mid, bid, ask, btc_index, entry_ts)
        if not result:
            continue
        side, entry_c, tp_offset = result
        outcome = simulate_held_position(
            candles, entry_ts, m["close_ts"],
            m["result"] == "yes",
            side, entry_c,
            sl_offset_c=sl_offset_c, tp_offset_c=tp_offset,
            use_sl=use_sl,
        )
        out.append(TradeRecord(
            label=label, ticker=m["ticker"], side=side, entry_c=entry_c,
            exit_reason=outcome.exit_reason, exit_c=outcome.exit_c,
            gross_c=outcome.gross_c, fee_c=outcome.fee_c, net_c=outcome.net_c,
            settle_yes=(m["result"] == "yes"),
        ))
    return out


def run_fvg(
    timing_label: str, use_sl: bool,
    markets, btc_index, candle_idx,
    *, sl_offset_c: int = 8,
) -> list[TradeRecord]:
    """FVG fires at its OWN timing — not a fixed offset. Use the first
    qualifying signal per market."""
    label = f"FVG@{timing_label}{'_SL' if use_sl else '_NOSL'}"
    out: list[TradeRecord] = []
    for m in markets:
        sig = eval_fvg_first_signal(m, btc_index, candle_idx)
        if not sig:
            continue
        entry_ts, side, entry_c, tp_offset = sig
        candles = candle_idx.get(m["ticker"]) or []
        outcome = simulate_held_position(
            candles, entry_ts, m["close_ts"],
            m["result"] == "yes",
            side, entry_c,
            sl_offset_c=sl_offset_c, tp_offset_c=tp_offset,
            use_sl=use_sl,
        )
        out.append(TradeRecord(
            label=label, ticker=m["ticker"], side=side, entry_c=entry_c,
            exit_reason=outcome.exit_reason, exit_c=outcome.exit_c,
            gross_c=outcome.gross_c, fee_c=outcome.fee_c, net_c=outcome.net_c,
            settle_yes=(m["result"] == "yes"),
        ))
    return out


def summarise(label: str, trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"label": label, "n": 0, "wins": 0, "losses": 0, "hit": 0.0,
                "total": 0.0, "avg": 0.0, "max_dd": 0.0,
                "tp": 0, "sl": 0, "preexp": 0, "settle_w": 0, "settle_l": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t.net_c > 0)
    losses = sum(1 for t in trades if t.net_c < 0)
    total_c = sum(t.net_c for t in trades)
    avg_c = total_c / n
    eq = peak = 0.0
    dd = 0.0
    for t in trades:
        eq += t.net_c
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "label": label, "n": n, "wins": wins, "losses": losses,
        "hit": (wins / n) * 100, "total": total_c / 100.0,
        "avg": avg_c / 100.0, "max_dd": dd / 100.0,
        "tp": sum(1 for t in trades if t.exit_reason == "TP_FILL"),
        "sl": sum(1 for t in trades if t.exit_reason == "SL_HIT"),
        "preexp": sum(1 for t in trades if t.exit_reason == "PRE_EXPIRY"),
        "settle_w": sum(1 for t in trades if t.exit_reason == "SETTLE_WIN"),
        "settle_l": sum(1 for t in trades if t.exit_reason == "SETTLE_LOSS"),
    }


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
    global _BTC_INDEX_CACHE
    _BTC_INDEX_CACHE = btc_index
    print(f"  {len(markets):,} markets / {len(btc_index):,} BTC mins / "
          f"{len(candle_idx):,} candle tickers")
    print()

    combos = [
        # (name, evaluator, timing_label, offset_s)
        ("BB_PURE_MEANREV", eval_bb_pure_meanrev, "T300", 300),
        ("BB_PURE_MEANREV", eval_bb_pure_meanrev, "T600", 600),
        ("BB_TREND",        eval_bb_trend,        "T060", 60),
        ("BB_TREND",        eval_bb_trend,        "T300", 300),
        ("FULL_STACK",      eval_full_stack,      "T300", 300),
        ("FULL_STACK",      eval_full_stack,      "T600", 600),
        ("ATM_REVERSION",   eval_atm_reversion,   "T600", 600),
    ]

    results: dict[str, list[TradeRecord]] = {}
    for name, ev, tl, off in combos:
        for use_sl in (False, True):
            label = f"{name}@{tl}{'_SL' if use_sl else '_NOSL'}"
            results[label] = run_strategy(
                name, ev, tl, off, use_sl, markets, btc_index, candle_idx
            )

    # FVG (own timing — first signal in window)
    for use_sl in (False, True):
        label = f"FVG@FIRST{'_SL' if use_sl else '_NOSL'}"
        results[label] = run_fvg("FIRST", use_sl, markets, btc_index, candle_idx)

    # ── Output ──────────────────────────────────────────────────────────
    print("=" * 105)
    print("INTRA-WINDOW BACKTEST  —  WITH-SL vs NO-SL  (fees included)")
    print("=" * 105)
    print(f"  {'label':<26} {'n':>5} {'hit%':>5} {'total $':>9} {'avg $':>8} "
          f"{'max DD':>8}  {'TP':>4} {'SL':>4} {'preX':>4} {'sW':>4} {'sL':>4}")
    print(f"  {'-'*26} {'-'*5} {'-'*5} {'-'*9} {'-'*8} {'-'*8}  {'-'*4} {'-'*4} {'-'*4} {'-'*4} {'-'*4}")
    summaries = {label: summarise(label, trades) for label, trades in results.items()}

    # Group by base name + timing for paired display.
    pairs: dict[str, dict] = defaultdict(dict)
    for label, s in summaries.items():
        if label.endswith("_SL"):
            pairs[label[:-3]]["sl"] = s
        elif label.endswith("_NOSL"):
            pairs[label[:-5]]["nosl"] = s
    for base in sorted(pairs.keys()):
        entry = pairs[base]
        for kind, label_suffix in (("nosl", "_NOSL"), ("sl", "_SL")):
            s = entry.get(kind)
            if not s:
                continue
            print(
                f"  {base+label_suffix:<26} {s['n']:>5} {s['hit']:>4.1f}% "
                f"{s['total']:>+8.2f} {s['avg']:>+7.4f} {s['max_dd']:>+7.2f}  "
                f"{s['tp']:>4} {s['sl']:>4} {s['preexp']:>4} {s['settle_w']:>4} "
                f"{s['settle_l']:>4}"
            )
        print()

    # CSV per combo
    for label, trades in results.items():
        if not trades:
            continue
        path = OUT_DIR / f"intra_{label}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ticker", "side", "entry_c", "exit_reason", "exit_c",
                "gross_c", "fee_c", "net_c", "settle_yes",
            ])
            for t in trades:
                w.writerow([
                    t.ticker, t.side, t.entry_c, t.exit_reason, t.exit_c,
                    f"{t.gross_c:.1f}", f"{t.fee_c:.2f}", f"{t.net_c:.2f}",
                    int(t.settle_yes),
                ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
