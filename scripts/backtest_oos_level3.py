"""Out-of-sample validation of Level 3 (full-Kelly) tiered FVG sizing.

Splits the dataset chronologically 70/30:
  - First 70% (FIT): observe fill rates per tier; sanity-check the
    backtest assumptions hold on this in-sample data.
  - Last 30% (VALIDATION): apply the Level 3 tier-sized strategy. Report
    actual P&L vs projection.

Level 3 sizing (the user's "more aggressive"):

  Tier 1 (99% fill: late_300+ & aligned & far_10+)         35% bankroll
  Tier 2 (93% fill: late_300+ & aligned, NOT in Tier 1)    25% bankroll
  Tier 3 (91% fill: late_300+, NOT in Tier 1/2)            18% bankroll
  Tier 4 (76% fill: late_180+, NOT in Tier 1/2/3)          10% bankroll

  Tiers are nested-exclusive: each trade gets ONLY ONE tier (the most
  specific match).

  TP targets per tier (from heatmap):
    Tier 1: +20c (best $/trade with 95%+ fill at this segment)
    Tier 2: +15c
    Tier 3: +12c
    Tier 4: +12c (slightly lower confidence so use the lower margin)

  SL: -8c on all tiers (taker exit when triggered)
  Bankroll floor: $5 (refuse to size below 1ct)

The script reports:
  - Per-tier stats: trade count, fill rate, avg net, total contribution
  - Aggregate fit-set vs validation-set comparison
  - Bankroll trajectory under FLAT sizing (start $50, hold size constant)
  - Bankroll trajectory under COMPOUNDING (each trade sized as % of CURRENT bankroll)
  - Worst single-day drawdown on validation
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "kalshi_external_backtest.db"


# -- Math (lifted) ------------------------------------------------------


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


# -- Loaders -----------------------------------------------------------


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


# -- FVG signal + per-trade record -------------------------------------


@dataclass
class FvgTrade:
    ticker: str
    entry_ts: int
    side: str
    entry_c: int
    fair_for_side: int
    spread_c: int
    btc_dist_abs_pct: float
    session_age_s: int
    btc_5m_move: float
    aligned: bool
    settle_yes: bool
    close_ts: int
    # Filled later via candle walk
    mfe_c: int = 0
    mae_c: int = 0


def find_fvg_signal(market, btc_index, candle_idx) -> FvgTrade | None:
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
        fair_for_side = fair
    else:
        side = "no"
        entry_c = min(100 - mid, 100 - baseline)
        fair_for_side = 100 - fair
    if entry_c < 5 or entry_c > 95:
        return None

    move_5m = btc_move(btc_index, ts, 300)
    aligned = (side == "yes" and move_5m > 0) or (side == "no" and move_5m < 0)

    trade = FvgTrade(
        ticker=market["ticker"], entry_ts=ts, side=side, entry_c=entry_c,
        fair_for_side=fair_for_side, spread_c=abs(fair - mid),
        btc_dist_abs_pct=btc_dist_abs, session_age_s=session_age,
        btc_5m_move=move_5m, aligned=aligned,
        settle_yes=market["result"] == "yes", close_ts=close_ts,
    )

    # Walk for MFE
    mfe = mae = entry_c
    for ts2, yb2, ya2, pc2 in candles[sig_idx:]:
        if ts2 >= close_ts:
            break
        b = side_bid_from_row(side, yb2, ya2, pc2)
        if b is None:
            continue
        if b > mfe:
            mfe = b
        if b < mae:
            mae = b
    trade.mfe_c = mfe
    trade.mae_c = mae
    return trade


# -- Tier classification ----------------------------------------------


def classify_tier(t: FvgTrade) -> int:
    """Return 1, 2, 3, 4, or 0 (refuse)."""
    if t.session_age_s < 180:
        return 0
    if not t.aligned and 30 <= t.entry_c <= 49:
        return 0
    if abs(t.btc_5m_move) < 20 and not t.aligned:
        return 0
    # Nested-exclusive: most specific tier first
    if t.session_age_s >= 300 and t.aligned and t.btc_dist_abs_pct >= 0.10:
        return 1
    if t.session_age_s >= 300 and t.aligned:
        return 2
    if t.session_age_s >= 300:
        return 3
    return 4


# -- Per-trade outcome simulator (uses MFE for fill, SL/PRE_EXP for misses) --


def simulate_outcome(t: FvgTrade, candles, *, tp_offset, sl_offset=8) -> tuple[str, int, int]:
    """Walk candles to find first exit. Return (reason, exit_c, fill_at_s)."""
    tp = t.entry_c + tp_offset
    sl = t.entry_c - sl_offset if sl_offset else None
    pre_exp = t.close_ts - 90
    for ts, yb, ya, pc in candles:
        if ts < t.entry_ts:
            continue
        if ts >= t.close_ts:
            break
        b = side_bid_from_row(t.side, yb, ya, pc)
        if b is None:
            continue
        elapsed = ts - t.entry_ts
        if b >= tp:
            return "TP", tp, elapsed
        if sl is not None and b <= sl:
            return "SL", max(1, b - 1), elapsed
        if ts >= pre_exp:
            return "PRE_EXP", max(1, b), elapsed
    won = (t.side == "yes" and t.settle_yes) or (t.side == "no" and not t.settle_yes)
    return ("SETTLE_W", 100, t.close_ts - t.entry_ts) if won else \
           ("SETTLE_L", 0, t.close_ts - t.entry_ts)


# -- Tier configuration (Level 3 — full-Kelly aggressive) ---------------


TIER_CONFIG = {
    1: {"frac": 0.35, "tp": 20, "sl": 8, "name": "T1: 99% fill late_300+ aligned far_10+"},
    2: {"frac": 0.25, "tp": 15, "sl": 8, "name": "T2: 93% fill late_300+ aligned"},
    3: {"frac": 0.18, "tp": 12, "sl": 8, "name": "T3: 91% fill late_300+ alone"},
    4: {"frac": 0.10, "tp": 12, "sl": 8, "name": "T4: 76% fill late_180+"},
}


def trade_pnl(t: FvgTrade, candles, *, tp_offset, sl_offset, contracts) -> float:
    """Return $ net P&L for this trade with the given config."""
    reason, exit_c, _ = simulate_outcome(t, candles, tp_offset=tp_offset, sl_offset=sl_offset)
    gross_per_ct = exit_c - t.entry_c
    if reason == "TP":
        fee_per_ct = kalshi_fee(t.entry_c) + kalshi_fee(exit_c)
    elif reason in ("SL", "PRE_EXP"):
        fee_per_ct = kalshi_fee(t.entry_c) + kalshi_fee(exit_c, taker=True)
    else:
        # Settle — entry leg fee only
        fee_per_ct = kalshi_fee(t.entry_c)
    net_per_ct = (gross_per_ct - fee_per_ct) / 100.0  # cents → dollars
    return net_per_ct * contracts


# -- Bankroll simulators ----------------------------------------------


def simulate_flat(trades: list[FvgTrade], candle_idx, base_bankroll=50.0,
                  ticker_cap_frac=0.40):
    """Each trade sized as fraction of FIXED initial bankroll. No compounding."""
    daily: dict[str, float] = defaultdict(float)
    total_pnl = 0.0
    eq = peak = 0.0
    max_dd = 0.0
    by_tier: dict[int, dict] = {i: {"n": 0, "tp_fills": 0, "total": 0.0,
                                     "wins": 0, "losses": 0}
                                 for i in (1, 2, 3, 4)}
    for t in trades:
        tier = classify_tier(t)
        if tier == 0:
            continue
        cfg = TIER_CONFIG[tier]
        notional = base_bankroll * cfg["frac"]
        notional = min(notional, base_bankroll * ticker_cap_frac)
        contracts = max(1, int(notional / max(0.01, t.entry_c / 100.0)))
        # Simulate exit
        candles = candle_idx.get(t.ticker) or []
        reason, exit_c, _ = simulate_outcome(t, candles,
                                              tp_offset=cfg["tp"],
                                              sl_offset=cfg["sl"])
        gross = (exit_c - t.entry_c) / 100.0 * contracts
        if reason == "TP":
            fee = (kalshi_fee(t.entry_c) + kalshi_fee(exit_c)) / 100.0 * contracts
        elif reason in ("SL", "PRE_EXP"):
            fee = (kalshi_fee(t.entry_c) + kalshi_fee(exit_c, taker=True)) / 100.0 * contracts
        else:
            fee = kalshi_fee(t.entry_c) / 100.0 * contracts
        net = gross - fee
        total_pnl += net
        # Daily P&L
        day = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] += net
        # Equity curve
        eq += net
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
        # Per-tier
        by_tier[tier]["n"] += 1
        by_tier[tier]["total"] += net
        if reason == "TP":
            by_tier[tier]["tp_fills"] += 1
            by_tier[tier]["wins"] += 1
        elif net > 0:
            by_tier[tier]["wins"] += 1
        else:
            by_tier[tier]["losses"] += 1
    return {"total": total_pnl, "max_dd": max_dd, "daily": dict(daily),
            "by_tier": by_tier}


def simulate_compound(trades: list[FvgTrade], candle_idx,
                      starting_bankroll=50.0, ticker_cap_frac=0.40,
                      ruin_threshold=5.0):
    """Each trade sized as fraction of CURRENT bankroll. Compounds.

    If bankroll falls below ruin_threshold, stop trading (ruin).
    """
    bankroll = starting_bankroll
    bankroll_history: list[tuple[str, float]] = []
    bankroll_history.append((
        datetime.fromtimestamp(trades[0].entry_ts if trades else 0,
                               tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        bankroll))
    fired = 0
    skipped = 0
    daily_eq: dict[str, float] = {}
    ruined = False
    ruined_at: str | None = None
    for t in trades:
        if bankroll < ruin_threshold:
            if not ruined:
                ruined = True
                ruined_at = datetime.fromtimestamp(
                    t.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            skipped += 1
            continue
        tier = classify_tier(t)
        if tier == 0:
            continue
        cfg = TIER_CONFIG[tier]
        notional = bankroll * cfg["frac"]
        notional = min(notional, bankroll * ticker_cap_frac)
        contracts = max(1, int(notional / max(0.01, t.entry_c / 100.0)))
        candles = candle_idx.get(t.ticker) or []
        reason, exit_c, _ = simulate_outcome(t, candles,
                                              tp_offset=cfg["tp"],
                                              sl_offset=cfg["sl"])
        gross = (exit_c - t.entry_c) / 100.0 * contracts
        if reason == "TP":
            fee = (kalshi_fee(t.entry_c) + kalshi_fee(exit_c)) / 100.0 * contracts
        elif reason in ("SL", "PRE_EXP"):
            fee = (kalshi_fee(t.entry_c) + kalshi_fee(exit_c, taker=True)) / 100.0 * contracts
        else:
            fee = kalshi_fee(t.entry_c) / 100.0 * contracts
        net = gross - fee
        bankroll += net
        fired += 1
        day = datetime.fromtimestamp(t.entry_ts,
                                      tz=timezone.utc).strftime("%Y-%m-%d")
        daily_eq[day] = bankroll
        bankroll_history.append((
            datetime.fromtimestamp(t.entry_ts,
                                    tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            bankroll))
    return {"final": bankroll, "fired": fired, "skipped": skipped,
            "ruined": ruined, "ruined_at": ruined_at,
            "history": bankroll_history, "daily": daily_eq}


def main() -> int:
    if not DB.exists():
        print(f"missing: {DB}")
        return 1

    print("Loading + walking trades...", flush=True)
    con = sqlite3.connect(str(DB))
    markets = load_markets(con)
    btc_index = build_btc_index(con)
    candle_idx = build_candle_index(con)
    con.close()

    trades: list[FvgTrade] = []
    for m in markets:
        t = find_fvg_signal(m, btc_index, candle_idx)
        if t:
            trades.append(t)
    trades.sort(key=lambda x: x.entry_ts)
    print(f"  {len(trades):,} FVG trades, sorted chronologically")

    if not trades:
        return 0
    span_s = trades[-1].entry_ts - trades[0].entry_ts
    print(f"  span: {span_s / 86400:.1f} days "
          f"({datetime.fromtimestamp(trades[0].entry_ts, tz=timezone.utc):%Y-%m-%d} "
          f"to "
          f"{datetime.fromtimestamp(trades[-1].entry_ts, tz=timezone.utc):%Y-%m-%d})")

    # 70/30 split
    split_idx = int(len(trades) * 0.7)
    fit = trades[:split_idx]
    val = trades[split_idx:]
    fit_days = (fit[-1].entry_ts - fit[0].entry_ts) / 86400 if len(fit) > 1 else 1
    val_days = (val[-1].entry_ts - val[0].entry_ts) / 86400 if len(val) > 1 else 1
    print(f"  FIT  : {len(fit):,} trades over {fit_days:.1f} days")
    print(f"  VAL  : {len(val):,} trades over {val_days:.1f} days")
    print()

    # Tier counts in each set (sanity check tiers stable)
    fit_tiers = defaultdict(int)
    val_tiers = defaultdict(int)
    for t in fit:
        fit_tiers[classify_tier(t)] += 1
    for t in val:
        val_tiers[classify_tier(t)] += 1
    print("Tier distribution (sanity check):")
    print(f"  {'tier':<8} {'FIT n':>8} {'FIT %':>8} {'VAL n':>8} {'VAL %':>8}")
    for tier in (0, 1, 2, 3, 4):
        fp = fit_tiers[tier] / len(fit) * 100
        vp = val_tiers[tier] / len(val) * 100
        print(f"  {tier:<8} {fit_tiers[tier]:>8} {fp:>7.1f}% "
              f"{val_tiers[tier]:>8} {vp:>7.1f}%")
    print()

    print("=" * 90)
    print("LEVEL 3 SIZING ON VALIDATION SET (last 30%)")
    print("=" * 90)
    print(f"  {'tier':<32} {'frac':>6} {'TP':>4} {'SL':>4}")
    for tier_id in (1, 2, 3, 4):
        cfg = TIER_CONFIG[tier_id]
        print(f"  {cfg['name']:<32} {cfg['frac']:>5.0%} +{cfg['tp']:<3}c -{cfg['sl']:<3}c")
    print()

    # -- FLAT sizing simulation on VAL --
    print("--- FLAT SIZING (constant bankroll = $50, no compounding) --")
    flat_res = simulate_flat(val, candle_idx, base_bankroll=50.0)
    print(f"  Total P&L on validation: ${flat_res['total']:+.2f}")
    print(f"  Max equity-curve DD:     ${flat_res['max_dd']:+.2f}")
    print(f"  Daily $ avg:             ${flat_res['total'] / val_days:+.2f}/day")
    print()
    print("  Per-tier breakdown:")
    print(f"    {'tier':<6} {'n':>4} {'TP fills':>9} {'TP %':>6} {'total $':>9} {'$/trade':>10}")
    for tier_id in (1, 2, 3, 4):
        bt = flat_res["by_tier"][tier_id]
        if bt["n"] == 0:
            continue
        tp_pct = bt["tp_fills"] / bt["n"] * 100
        per = bt["total"] / bt["n"]
        print(f"    T{tier_id:<5} {bt['n']:>4} {bt['tp_fills']:>9} "
              f"{tp_pct:>5.1f}% {bt['total']:>+8.2f} {per:>+9.4f}")
    print()

    # -- COMPOUNDING simulation on VAL --
    print("--- COMPOUNDING (start $50, scale contracts with current bankroll) --")
    comp_res = simulate_compound(val, candle_idx, starting_bankroll=50.0,
                                  ruin_threshold=5.0)
    days_in_val = val_days if val_days > 0 else 1
    growth_factor = comp_res["final"] / 50.0
    annualized = (growth_factor ** (365 / days_in_val) - 1) * 100 if growth_factor > 0 else -100
    print(f"  Starting bankroll:       $50.00")
    print(f"  Final bankroll:          ${comp_res['final']:.2f}")
    print(f"  Growth factor:           {growth_factor:.2f}x")
    print(f"  Trades fired:            {comp_res['fired']}")
    print(f"  Trades skipped (ruin):   {comp_res['skipped']}")
    print(f"  Ruined?                  {comp_res['ruined']} "
          f"({comp_res['ruined_at'] or '—'})")
    if growth_factor > 1:
        print(f"  Implied annualized:      {annualized:>+.1f}% (extrapolated)")
    # Bankroll trajectory at 10 evenly-spaced points
    if comp_res["history"]:
        n_points = 10
        step = max(1, len(comp_res["history"]) // n_points)
        print()
        print("  Bankroll trajectory (every Nth trade):")
        print(f"    {'time (UTC)':<20} {'bankroll':>10}")
        for i in range(0, len(comp_res["history"]), step):
            tstr, br = comp_res["history"][i]
            print(f"    {tstr:<20} ${br:>9.2f}")
    print()

    # -- FIT-set baseline for comparison --
    print("--- COMPARISON: same simulation on FIT set (in-sample) --")
    flat_fit = simulate_flat(fit, candle_idx, base_bankroll=50.0)
    print(f"  FIT total P&L:           ${flat_fit['total']:+.2f}")
    print(f"  FIT $/day:               ${flat_fit['total'] / fit_days:+.2f}/day")
    print(f"  FIT max DD:              ${flat_fit['max_dd']:+.2f}")
    val_per_day = flat_res['total'] / val_days
    fit_per_day = flat_fit['total'] / fit_days
    if fit_per_day != 0:
        retention = val_per_day / fit_per_day * 100
    else:
        retention = 0
    print()
    print(f"  IN-SAMPLE/OUT-OF-SAMPLE retention: {retention:.0f}%")
    print(f"    (100% = OOS as good as fit. <60% = signs of overfit.)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
