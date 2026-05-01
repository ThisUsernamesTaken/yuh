"""
MTF Simulation — Historical prediction accuracy + dynamic TP analysis.
Runs over March 13-28 2026 historical trades.
"""
import json
import sqlite3
import math
import datetime
import calendar
import sys
from collections import defaultdict

# ── Load candle data ──────────────────────────────────────────────────────────
with open("data/btc_ohlcv_sim.json") as f:
    raw = json.load(f)

def parse_candles(raw_list):
    result = {}
    for k in raw_list:
        ts_ms = k[0]
        result[ts_ms] = {
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]),  "close": float(k[4]),
            "volume": float(k[5]), "close_ms": k[6],
        }
    return result

candles = {tf: parse_candles(raw[tf]) for tf in raw}
ts_sorted = {tf: sorted(candles[tf].keys()) for tf in candles}

def get_candles_up_to(tf, up_to_ms, n=50):
    """Return up to n closed candles for tf whose close_ms <= up_to_ms."""
    result = []
    for ts in ts_sorted[tf]:
        c = candles[tf][ts]
        if c["close_ms"] <= up_to_ms:
            result.append(c)
        elif ts > up_to_ms:
            break
    return result[-n:] if len(result) >= n else result


# ── Lightweight indicator helpers ─────────────────────────────────────────────

def ema(prices, period):
    if not prices or len(prices) < 2:
        return prices[-1] if prices else None
    k = 2.0 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
    return e

def rsi_calc(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_tf_score(candle_list, tf):
    """Compute a directional score in [-1, +1] for a timeframe."""
    if len(candle_list) < 10:
        return None, "COLD"

    closes = [c["close"] for c in candle_list]
    highs  = [c["high"]  for c in candle_list]
    lows   = [c["low"]   for c in candle_list]
    vols   = [c["volume"] for c in candle_list]

    # EMA
    fast_p, slow_p = (8, 21) if tf != "1h" else (20, 50)
    ema_fast = ema(closes, fast_p)
    ema_slow = ema(closes, slow_p)
    if ema_fast is None or ema_slow is None:
        return None, "COLD"

    ema_dir = 1.0 if ema_fast > ema_slow else -1.0
    # Estimate cross freshness from recent bars
    # Count how many consecutive bars the direction has held
    cur_above = ema_fast > ema_slow
    cross_bars = 1
    for i in range(len(closes) - 1, 0, -1):
        f = ema(closes[:i], fast_p)
        s = ema(closes[:i], slow_p)
        if f is None or s is None:
            break
        if (f > s) == cur_above:
            cross_bars += 1
        else:
            break
    cross_bars = min(cross_bars, 25)
    freshness = max(0.35, 1.0 - (cross_bars - 1) * 0.04)
    ema_component = ema_dir * freshness

    # RSI
    rsi_val = rsi_calc(closes, 14)
    rsi_norm = (rsi_val - 50.0) / 50.0
    rsi_slope = 0.0
    if len(closes) >= 3:
        prev_rsi = rsi_calc(closes[:-1], 14)
        if rsi_val > prev_rsi + 0.5:
            rsi_slope = 0.1
        elif rsi_val < prev_rsi - 0.5:
            rsi_slope = -0.1
    rsi_component = max(-1.0, min(1.0, rsi_norm + rsi_slope))

    # Candle pressure
    lc = candle_list[-1]
    rng = lc["high"] - lc["low"]
    pressure = (lc["close"] - lc["open"]) / rng if rng > 0 else 0.0

    # Market structure (last 8 bars)
    market_struct = 0.0
    if len(highs) >= 8:
        h_early = sum(highs[-8:-4]) / 4
        h_late  = sum(highs[-4:]) / 4
        l_early = sum(lows[-8:-4]) / 4
        l_late  = sum(lows[-4:]) / 4
        if h_late > h_early and l_late > l_early:
            market_struct = 1.0
        elif h_late < h_early and l_late < l_early:
            market_struct = -1.0

    # Volume
    avg_vol = sum(vols[-20:]) / min(20, len(vols)) if vols else 1.0
    rel_vol = vols[-1] / avg_vol if avg_vol > 0 else 1.0
    vol_clamp = min(rel_vol, 3.0)
    if lc["close"] > lc["open"]:
        vol_comp = (vol_clamp - 1.0) / 2.0
    elif lc["close"] < lc["open"]:
        vol_comp = -(vol_clamp - 1.0) / 2.0
    else:
        vol_comp = 0.0

    W_EMA, W_RSI, W_CANDLE, W_MARKET, W_VOL = 0.30, 0.25, 0.20, 0.15, 0.10
    score = (W_EMA * ema_component + W_RSI * rsi_component +
             W_CANDLE * pressure + W_MARKET * market_struct +
             W_VOL * max(-1.0, min(1.0, vol_comp)))
    score = max(-1.0, min(1.0, score))

    if abs(score) < 0.15:
        regime = "NEUTRAL"
    elif score >= 0.6:
        regime = "IMPULSE_BULL"
    elif score >= 0.3:
        regime = "TREND_BULL"
    elif score <= -0.6:
        regime = "IMPULSE_BEAR"
    elif score <= -0.3:
        regime = "TREND_BEAR"
    else:
        regime = "MIXED"

    return score, regime


TF_WEIGHTS = {"1m": 20/90, "5m": 25/90, "15m": 30/90, "1h": 15/90}


def compute_mtf_score(entry_ms, side):
    tf_scores = {}
    for tf in ["1m", "5m", "15m", "1h"]:
        bars = get_candles_up_to(tf, entry_ms, n=50)
        if len(bars) < 10:
            tf_scores[tf] = None
            continue
        score, _regime = compute_tf_score(bars, tf)
        tf_scores[tf] = score

    weighted_sum = 0.0
    active_weight = 0.0
    for tf, w in TF_WEIGHTS.items():
        s = tf_scores.get(tf)
        if s is not None:
            weighted_sum += w * s
            active_weight += w

    if active_weight < 0.10:
        confluence = 0.0
    else:
        confluence = weighted_sum / active_weight
    confluence = max(-1.0, min(1.0, confluence))

    aligned  = (confluence >= 0.3 if side == "yes" else confluence <= -0.3)
    opposing = (confluence <= -0.3 if side == "yes" else confluence >= 0.3)

    return confluence, tf_scores, aligned, opposing


# ── Load trades ───────────────────────────────────────────────────────────────
conn = sqlite3.connect("data/trades.db")
cur = conn.cursor()
cur.execute("""
    SELECT id, placed_at, ticker, side, count, limit_price, dollar_risk,
           strategy_name, status, pnl, result, filled_count
    FROM kalshi_trades
    WHERE status NOT IN ('pending', 'unfilled')
      AND placed_at >= '2026-03-13'
      AND placed_at < '2026-03-29'
    ORDER BY placed_at
""")
trades = cur.fetchall()
conn.close()
print(f"Processing {len(trades)} trades...")


def parse_ts(s):
    if "T" in s:
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(s)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return int(calendar.timegm(dt.timetuple())) * 1000
    except Exception:
        return None


results = []
for i, t in enumerate(trades):
    (trade_id, placed_at, ticker, side, count, limit_price, dollar_risk,
     strategy_name, status, pnl, result, filled_count) = t

    entry_ms = parse_ts(placed_at)
    if entry_ms is None:
        continue

    mtf_score, tf_scores, aligned, opposing = compute_mtf_score(entry_ms, side)

    # MFE/MAE: use BTC 1m during the contract window as proxy
    entry_dt = datetime.datetime.utcfromtimestamp(entry_ms / 1000)
    minute_in_window = entry_dt.minute % 15
    window_start_dt = entry_dt - datetime.timedelta(
        minutes=minute_in_window, seconds=entry_dt.second, microseconds=entry_dt.microsecond
    )
    window_start_ms = int(calendar.timegm(window_start_dt.timetuple())) * 1000
    window_end_ms = window_start_ms + 15 * 60 * 1000

    # 1m candles within window after entry
    window_1m = [
        candles["1m"][ts]
        for ts in ts_sorted["1m"]
        if window_start_ms <= ts <= window_end_ms and ts in candles["1m"]
    ]

    mfe_btc_pct = None
    mae_btc_pct = None
    if window_1m:
        btc_at_entry = window_1m[0]["open"]
        if btc_at_entry and btc_at_entry > 0:
            max_high = max(c["high"] for c in window_1m)
            min_low  = min(c["low"]  for c in window_1m)
            if side == "yes":
                mfe_btc_pct = (max_high - btc_at_entry) / btc_at_entry * 100
                mae_btc_pct = (btc_at_entry - min_low)  / btc_at_entry * 100
            else:
                mfe_btc_pct = (btc_at_entry - min_low)  / btc_at_entry * 100
                mae_btc_pct = (max_high - btc_at_entry) / btc_at_entry * 100

    won = pnl > 0 if pnl is not None else None

    results.append({
        "trade_id": trade_id,
        "placed_at": placed_at,
        "side": side,
        "limit_price": limit_price,
        "strategy_name": strategy_name,
        "status": status,
        "pnl": pnl or 0.0,
        "result": result,
        "mtf_score": round(mtf_score, 4),
        "tf_1m":  round(tf_scores.get("1m"),  4) if tf_scores.get("1m")  is not None else None,
        "tf_5m":  round(tf_scores.get("5m"),  4) if tf_scores.get("5m")  is not None else None,
        "tf_15m": round(tf_scores.get("15m"), 4) if tf_scores.get("15m") is not None else None,
        "tf_1h":  round(tf_scores.get("1h"),  4) if tf_scores.get("1h")  is not None else None,
        "aligned":  aligned,
        "opposing": opposing,
        "neutral":  not aligned and not opposing,
        "mfe_btc_pct": round(mfe_btc_pct, 4) if mfe_btc_pct is not None else None,
        "mae_btc_pct": round(mae_btc_pct, 4) if mae_btc_pct is not None else None,
        "won": won,
    })

    if (i + 1) % 250 == 0:
        print(f"  {i+1}/{len(trades)} processed...", flush=True)

print(f"Done. {len(results)} trades scored.")

# Save raw data
with open("data/mtf_sim_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("Saved to data/mtf_sim_results.json")

# ── STATISTICS ────────────────────────────────────────────────────────────────

settled = [r for r in results if r["won"] is not None]
print(f"\nSettled trades: {len(settled)}")

# 1. Score distribution
def score_bucket_label(score):
    if score >= 0.6:  return "A:HIGH_BULL(>=0.6)"
    if score >= 0.3:  return "B:BULL(0.3-0.6)"
    if score >= -0.3: return "C:NEUTRAL(+-0.3)"
    if score >= -0.6: return "D:BEAR(-0.6to-0.3)"
    return              "E:HIGH_BEAR(<=-0.6)"

bucket_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
for r in settled:
    b = score_bucket_label(r["mtf_score"])
    bucket_stats[b]["n"] += 1
    if r["won"]: bucket_stats[b]["wins"] += 1
    bucket_stats[b]["pnl"] += r["pnl"]

print("\n--- Score Bucket Stats ---")
print(f"{'Bucket':<24} {'N':>5} {'WR%':>6} {'PnL$':>8} {'Avg$':>7}")
for b in sorted(bucket_stats):
    s = bucket_stats[b]
    wr = s["wins"] / s["n"] * 100 if s["n"] else 0
    avg = s["pnl"] / s["n"] if s["n"] else 0
    print(f"{b:<24} {s['n']:>5} {wr:>6.1f} {s['pnl']:>8.2f} {avg:>7.3f}")

# 2. Alignment
align_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
for r in settled:
    cat = "aligned" if r["aligned"] else ("opposing" if r["opposing"] else "neutral")
    align_stats[cat]["n"] += 1
    if r["won"]: align_stats[cat]["wins"] += 1
    align_stats[cat]["pnl"] += r["pnl"]

print("\n--- Alignment Analysis ---")
print(f"{'Category':<12} {'N':>5} {'WR%':>6} {'PnL$':>8} {'Avg$':>8} {'Trades_skipped%':>16}")
total_n = len(settled)
for cat in ["aligned", "neutral", "opposing"]:
    s = align_stats[cat]
    if s["n"] == 0: continue
    wr = s["wins"] / s["n"] * 100
    avg = s["pnl"] / s["n"]
    skip_pct = s["n"] / total_n * 100
    print(f"{cat:<12} {s['n']:>5} {wr:>6.1f} {s['pnl']:>8.2f} {avg:>8.3f} {skip_pct:>15.1f}%")

aligned_wr = align_stats["aligned"]["wins"] / align_stats["aligned"]["n"] * 100 if align_stats["aligned"]["n"] > 0 else 0
opposing_wr = align_stats["opposing"]["wins"] / align_stats["opposing"]["n"] * 100 if align_stats["opposing"]["n"] > 0 else 0
neutral_wr = align_stats["neutral"]["wins"] / align_stats["neutral"]["n"] * 100 if align_stats["neutral"]["n"] > 0 else 0
print(f"\nAligned vs Opposing WR delta: {aligned_wr - opposing_wr:+.1f}pp")
print(f"Aligned vs Neutral WR delta:  {aligned_wr - neutral_wr:+.1f}pp")

# 3. Per-TF signal correlation
print("\n--- Per-Timeframe Directional Predictive Value ---")
for tf, key in [("1m","tf_1m"), ("5m","tf_5m"), ("15m","tf_15m"), ("1h","tf_1h")]:
    with_tf = [(r[key], r["won"]) for r in settled if r[key] is not None]
    if not with_tf: continue
    # Correlation: does higher score -> more wins on YES, lower -> more wins on NO?
    side_adj = []
    for r in settled:
        if r[key] is None: continue
        adj_score = r[key] if r["side"] == "yes" else -r[key]
        side_adj.append((adj_score, r["won"]))

    # Split into tertiles by side-adjusted score
    side_adj.sort()
    n3 = len(side_adj) // 3
    low  = side_adj[:n3]
    mid  = side_adj[n3:2*n3]
    high = side_adj[2*n3:]
    def pct_win(lst): return sum(1 for _,w in lst if w) / len(lst) * 100 if lst else 0
    print(f"  {tf} (n={len(side_adj)}): bottom-tertile WR={pct_win(low):.1f}% | "
          f"mid-tertile WR={pct_win(mid):.1f}% | top-tertile WR={pct_win(high):.1f}%")

# 4. Strategy breakdown under alignment
print("\n--- Alignment WR by Strategy ---")
strat_align = defaultdict(lambda: defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0}))
for r in settled:
    cat = "aligned" if r["aligned"] else ("opposing" if r["opposing"] else "neutral")
    s = strat_align[r["strategy_name"]][cat]
    s["n"] += 1
    if r["won"]: s["wins"] += 1
    s["pnl"] += r["pnl"]

key_strats = ["TA_FORCED_SIGNAL", "CROSS_VENUE_FLOW", "TREND_FOLLOW"]
for strat in key_strats:
    if strat not in strat_align: continue
    parts = []
    for cat in ["aligned", "neutral", "opposing"]:
        s = strat_align[strat][cat]
        if s["n"] == 0: continue
        wr = s["wins"]/s["n"]*100
        parts.append(f"{cat}:WR={wr:.0f}%(n={s['n']},${s['pnl']:.2f})")
    print(f"  {strat}: {' | '.join(parts)}")

# ── PART 2: MFE/MAE / Dynamic TP ────────────────────────────────────────────
print("\n" + "="*60)
print("PART 2: MFE/MAE + DYNAMIC TP SIMULATION")
print("="*60)

mfe_data = [r for r in settled if r["mfe_btc_pct"] is not None]
print(f"Trades with MFE data: {len(mfe_data)}")

def conf_bucket(r):
    score = r["mtf_score"]
    side  = r["side"]
    adj   = score if side == "yes" else -score
    if adj >= 0.6:   return "HIGH"
    if adj >= 0.3:   return "MEDIUM"
    if abs(score) < 0.3: return "NEUTRAL"
    return "OPPOSING"

mfe_by_conf = defaultdict(list)
mae_by_conf = defaultdict(list)
win_by_conf = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})

for r in mfe_data:
    cb = conf_bucket(r)
    mfe_by_conf[cb].append(r["mfe_btc_pct"])
    mae_by_conf[cb].append(r["mae_btc_pct"])
    win_by_conf[cb]["n"] += 1
    if r["won"]: win_by_conf[cb]["wins"] += 1
    win_by_conf[cb]["pnl"] += r["pnl"]

def median(lst):
    s = sorted(lst)
    n = len(s)
    return s[n//2] if n else 0.0

def mean(lst):
    return sum(lst)/len(lst) if lst else 0.0

print(f"\n{'Conf':<12} {'N':>5} {'WR%':>6} {'MedMFE%':>9} {'AvgMFE%':>9} {'MedMAE%':>9} {'AvgMAE%':>9} {'PnL$':>7}")
print("-"*66)
for b in ["HIGH", "MEDIUM", "NEUTRAL", "OPPOSING"]:
    mfe = mfe_by_conf[b]
    mae = mae_by_conf[b]
    ws  = win_by_conf[b]
    if not mfe: continue
    wr = ws["wins"]/ws["n"]*100 if ws["n"] else 0
    print(f"{b:<12} {ws['n']:>5} {wr:>6.1f} {median(mfe):>9.3f} {mean(mfe):>9.3f} {median(mae):>9.3f} {mean(mae):>9.3f} {ws['pnl']:>7.2f}")

# Dynamic TP backtest
print("\n--- Dynamic TP Backtest ---")
print("Approximating TP in Kalshi cents from BTC% move (1% BTC ≈ 2c Kalshi mid)")

SCALE = 2.0  # 1% BTC move ~ 2c Kalshi price move (rough)

def sim_pnl(r, tp_cents, trail_activation=15.0, trail_distance=5.0):
    """Simulate P&L with a given TP strategy."""
    entry = r["limit_price"]
    mfe_c = (r["mfe_btc_pct"] or 0.0) * SCALE
    mae_c = (r["mae_btc_pct"] or 0.0) * SCALE
    count = 1  # normalized to 1 contract

    # Simplified: if MAE exceeds trail_distance and we're in trail, stop
    # Otherwise if MFE reaches tp_cents, capture TP
    # Hold to expiry for unstopped positions

    if mfe_c >= tp_cents:
        # TP fires — capture tp_cents
        return tp_cents
    else:
        # Didn't reach TP — hold to expiry outcome
        if r["won"]:
            return 100.0 - entry  # settled at 100
        else:
            return -entry  # settled at 0

def sim_pnl_tiered(r, tier1_cents, tier2_cents, trail_activation=None, trail_dist=5.0):
    """Tiered TP: half at tier1, half at tier2, rest trails."""
    entry = r["limit_price"]
    mfe_c = (r["mfe_btc_pct"] or 0.0) * SCALE
    won = r["won"]

    half1_pnl = 0.0
    half2_pnl = 0.0

    # Half 1
    if mfe_c >= tier1_cents:
        half1_pnl = tier1_cents * 0.5
    else:
        if won:
            half1_pnl = (100.0 - entry) * 0.5
        else:
            half1_pnl = -entry * 0.5

    # Half 2
    if mfe_c >= tier2_cents:
        half2_pnl = tier2_cents * 0.5
    else:
        if won:
            half2_pnl = (100.0 - entry) * 0.5
        else:
            half2_pnl = -entry * 0.5

    return half1_pnl + half2_pnl

# Current TP: entry*1.15 (15% profit), entry*1.20 (20% profit)
# For a 50c entry: tier1 = 7.5c, tier2 = 10c
def current_tp_pnl(r):
    entry = r["limit_price"]
    tier1 = entry * 0.15
    tier2 = entry * 0.20
    return sim_pnl_tiered(r, tier1, tier2)

# Proposed dynamic TPs
TP_PRESETS = {
    "CURRENT_FIXED": lambda r: current_tp_pnl(r),
    "DYN_LOW(4c/7c)":   lambda r: sim_pnl_tiered(r, 4.0, 7.0),
    "DYN_MED(6c/10c)":  lambda r: sim_pnl_tiered(r, 6.0, 10.0),
    "DYN_HIGH(8c/14c)": lambda r: sim_pnl_tiered(r, 8.0, 14.0),
    "HOLD_EXPIRY":      lambda r: (100 - r["limit_price"]) * r["won"] - r["limit_price"] * (not r["won"]),
}

print(f"\n{'Strategy':<22} {'Total_PnL':>12} {'Avg/Trade':>10} {'Wins':>6} {'Losses':>7} {'MaxWin':>8}")
print("-"*70)
for name, fn in TP_PRESETS.items():
    pnls = [fn(r) for r in mfe_data]
    total = sum(pnls)
    avg = total / len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    max_win = max(pnls) if pnls else 0
    print(f"{name:<22} {total:>12.2f} {avg:>10.3f} {wins:>6} {losses:>7} {max_win:>8.2f}")

# Dynamic TP by confidence level (the key hypothesis)
print("\n--- Dynamic TP Per Confidence Level ---")
for b in ["HIGH", "MEDIUM", "NEUTRAL", "OPPOSING"]:
    grp = [r for r in mfe_data if conf_bucket(r) == b]
    if not grp: continue
    cur = [current_tp_pnl(r) for r in grp]
    # Confidence-specific TP
    if b == "HIGH":
        dyn = [sim_pnl_tiered(r, 8.0, 14.0) for r in grp]
    elif b == "MEDIUM":
        dyn = [sim_pnl_tiered(r, 6.0, 10.0) for r in grp]
    else:
        dyn = [sim_pnl_tiered(r, 4.0, 7.0) for r in grp]
    delta = sum(dyn) - sum(cur)
    print(f"  {b:<10}: current=${sum(cur):.2f} | dynamic=${sum(dyn):.2f} | delta={delta:+.2f}")

# P&L if we only trade aligned
print("\n--- P&L if only trading when aligned ---")
aligned_trades   = [r for r in settled if r["aligned"]]
opposing_trades  = [r for r in settled if r["opposing"]]
neutral_trades   = [r for r in settled if r["neutral"]]
base_pnl = sum(r["pnl"] for r in settled)
aligned_pnl = sum(r["pnl"] for r in aligned_trades)
saved_from_opposing = -sum(r["pnl"] for r in opposing_trades if r["pnl"] < 0)
saved_from_neutral  = -sum(r["pnl"] for r in neutral_trades  if r["pnl"] < 0)
print(f"  Base P&L (all trades):        ${base_pnl:.2f}")
print(f"  Aligned-only P&L:             ${aligned_pnl:.2f}")
print(f"  Opposing trades avoided:       {len(opposing_trades)} trades, ${sum(r['pnl'] for r in opposing_trades):.2f} saved by skipping")
print(f"  Neutral trades avoided:        {len(neutral_trades)} trades, ${sum(r['pnl'] for r in neutral_trades):.2f} saved by skipping")
total_opposing_loss = sum(r["pnl"] for r in opposing_trades)
total_neutral_loss  = sum(r["pnl"] for r in neutral_trades)
improved_pnl = base_pnl - total_opposing_loss - total_neutral_loss
print(f"  P&L if skip opposing+neutral: ${improved_pnl:.2f} ({improved_pnl-base_pnl:+.2f} delta)")

print("\n[Done] All analysis complete.")
