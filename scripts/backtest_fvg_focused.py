"""FVG-focused backtest: does the gap close within the 15-min window?

The user's clarification (2026-05-04): FVG is NOT predicting expiry. It's
predicting that the contract's price will move toward the BB-fair value
(closing the value gap). Settlement is irrelevant; what matters is:

  1. Within the 15-min window, does the bid on our side EVER reach the
     fair-value target? (full gap fill)
  2. If not, does it at least move halfway? (partial fill)
  3. Does it move in the correct direction at all? (directional accuracy)

Methodology:
  - For each finalized market, evaluate FVG signal at first qualifying
    moment (mirrors PAPER_FVG state machine: baseline 1st 90s, then look
    for |fair - baseline| >= threshold).
  - Walk per-minute candle stream from entry to close.
  - Track MFE (max favorable excursion = best bid on our side) and MAE
    (max adverse = worst bid).
  - Report:
      * % of trades where MFE >= fair_target (full gap fill)
      * % of trades where MFE >= entry + 8c (small TP fill)
      * % of trades where MFE > entry (any directional movement)
      * Average MFE, MAE per trade
      * Per-trade gross @ TP=fair, TP=entry+8c (no SL), with fees.

Output: a table comparing TP-target levels to find the best
gap-closure threshold, plus a distribution of MFE.
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


# ── Math (reused) ───────────────────────────────────────────────────────


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


def kalshi_fee_cents(price_c: int, contracts: int = 1, taker: bool = False) -> float:
    p = max(0.0, min(1.0, float(price_c) / 100.0))
    per_c = 0.07 * p * (1.0 - p) * 100.0
    if taker:
        per_c *= 1.4
    return per_c * contracts


def yes_mid_from_row(yb_d, ya_d, pc_d) -> int | None:
    if yb_d is not None and ya_d is not None and yb_d > 0 and ya_d > 0:
        bid = int(round(float(yb_d) * 100))
        ask = int(round(float(ya_d) * 100))
        return (bid + ask) // 2
    if pc_d is not None and pc_d > 0:
        return int(round(float(pc_d) * 100))
    return None


def side_bid_from_row(side: str, yb_d, ya_d, pc_d) -> int | None:
    """The bid we'd receive if we sold this side."""
    if yb_d is not None and ya_d is not None and yb_d > 0 and ya_d > 0:
        if side == "yes":
            return int(round(float(yb_d) * 100))
        return int(round((1.0 - float(ya_d)) * 100))
    if pc_d is not None and pc_d > 0:
        if side == "yes":
            return int(round(float(pc_d) * 100))
        return int(round((1.0 - float(pc_d)) * 100))
    return None


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
        out.append({"ticker": ticker, "open_ts": int(open_ts),
                    "close_ts": int(close_ts), "strike": strike,
                    "result": (result or "").lower()})
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


# ── FVG signal ──────────────────────────────────────────────────────────


@dataclass
class FvgSignal:
    ticker: str
    entry_ts: int
    side: str
    entry_c: int
    fair_c: int           # fair_yes (or 100-fair if NO) — the "target gap close"
    fair_for_side: int    # fair value on our side (gap_fill target)
    spread_c: int
    baseline_c: int
    btc_at_entry: float
    strike: float
    btc_dist_pct: float
    seconds_left: int


def find_fvg_signal(market: dict, btc_index: dict[int, float],
                    candle_idx: dict[str, list[tuple]]) -> FvgSignal | None:
    candles = candle_idx.get(market["ticker"]) or []
    if not candles:
        return None
    open_ts = market["open_ts"]
    close_ts = market["close_ts"]
    strike = market["strike"]

    # Baseline: avg mid in first 2 minutes
    baseline_mids: list[int] = []
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

    # Walk for first FVG signal
    for ts, yb, ya, pc in candles:
        if ts < open_ts + 90:
            continue
        if ts >= close_ts:
            break
        secs_remaining = close_ts - ts
        if secs_remaining < 120:
            return None
        session_age = ts - open_ts
        if session_age < 180:
            thresh = 5
        elif session_age < 420:
            thresh = 8
        else:
            thresh = 12
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
        gap_vs_baseline = fair - baseline
        if abs(gap_vs_baseline) < thresh:
            continue
        btc_dist_pct = abs(btc - strike) / strike * 100.0
        if btc_dist_pct < 0.01 or btc_dist_pct > 0.15:
            continue

        if gap_vs_baseline > 0:
            side = "yes"
            entry_c = min(mid, baseline)
            fair_for_side = fair
        else:
            side = "no"
            entry_c = min(100 - mid, 100 - baseline)
            fair_for_side = 100 - fair
        if entry_c < 5 or entry_c > 95:
            continue
        return FvgSignal(
            ticker=market["ticker"], entry_ts=ts, side=side, entry_c=entry_c,
            fair_c=fair, fair_for_side=fair_for_side,
            spread_c=abs(fair - mid), baseline_c=baseline,
            btc_at_entry=btc, strike=strike, btc_dist_pct=btc_dist_pct,
            seconds_left=int(secs_remaining),
        )
    return None


# ── Track-the-position simulator with MFE/MAE ──────────────────────────


@dataclass
class FvgOutcome:
    sig: FvgSignal
    mfe_c: int            # max bid on our side seen post-entry
    mae_c: int            # min bid on our side seen post-entry
    mfe_at_s: int         # seconds after entry when MFE was reached
    mae_at_s: int
    final_bid_c: int      # bid at last candle before close
    settled_yes: bool

    # Boolean tags
    @property
    def closed_full_gap(self) -> bool:
        """MFE reached fair_for_side - 1 (fully closed the gap)."""
        return self.mfe_c >= self.sig.fair_for_side - 1

    @property
    def closed_half_gap(self) -> bool:
        """MFE reached entry + half(spread) — partial fill."""
        target = self.sig.entry_c + max(int(self.sig.spread_c * 0.5), 4)
        return self.mfe_c >= target

    @property
    def closed_min_gap(self) -> bool:
        """MFE reached entry + 8c — minimum FVG-close target."""
        return self.mfe_c >= self.sig.entry_c + 8

    @property
    def directionally_correct(self) -> bool:
        """MFE > entry — at any point the position was profitable."""
        return self.mfe_c > self.sig.entry_c


def simulate_fvg_track(sig: FvgSignal, candles: list[tuple],
                       close_ts: int, settle_yes: bool) -> FvgOutcome:
    mfe = sig.entry_c
    mae = sig.entry_c
    mfe_at_s = 0
    mae_at_s = 0
    last_bid = sig.entry_c
    for ts, yb, ya, pc in candles:
        if ts < sig.entry_ts:
            continue
        if ts >= close_ts:
            break
        b = side_bid_from_row(sig.side, yb, ya, pc)
        if b is None:
            continue
        last_bid = b
        elapsed = ts - sig.entry_ts
        if b > mfe:
            mfe = b
            mfe_at_s = elapsed
        if b < mae:
            mae = b
            mae_at_s = elapsed
    return FvgOutcome(
        sig=sig, mfe_c=mfe, mae_c=mae,
        mfe_at_s=mfe_at_s, mae_at_s=mae_at_s,
        final_bid_c=last_bid, settled_yes=settle_yes,
    )


# ── Per-trade P&L for several TP+SL configurations ─────────────────────


def pnl_for_tp_sl(o: FvgOutcome, *, tp_offset: int | str, sl_offset: int | None,
                  candles: list[tuple], close_ts: int) -> tuple[str, float]:
    """Re-walk the candle stream with a specific TP/SL config and return
    ``(exit_reason, net_cents)``. Lets us compare configs without
    repeating the entire entry-evaluation loop.

    ``tp_offset`` can be an int (entry + N) or a string keyword:
      - "FAIR-1": TP = fair_for_side - 1 (the actual gap-close target)
      - "FAIR-3": TP = fair_for_side - 3 (a touch easier to fill)
      - "FAIR-5": TP = fair_for_side - 5
    """
    sig = o.sig
    if isinstance(tp_offset, str):
        if tp_offset == "FAIR-1":
            tp_target = max(sig.entry_c + 5, sig.fair_for_side - 1)
        elif tp_offset == "FAIR-3":
            tp_target = max(sig.entry_c + 5, sig.fair_for_side - 3)
        elif tp_offset == "FAIR-5":
            tp_target = max(sig.entry_c + 5, sig.fair_for_side - 5)
        else:
            tp_target = sig.entry_c + 8  # fallback
    else:
        tp_target = sig.entry_c + tp_offset
    sl_trigger = (sig.entry_c - sl_offset) if sl_offset is not None else None
    pre_expiry_ts = close_ts - 90

    exit_reason = None
    exit_c = 0
    taker = False
    for ts, yb, ya, pc in candles:
        if ts < sig.entry_ts:
            continue
        if ts >= close_ts:
            break
        b = side_bid_from_row(sig.side, yb, ya, pc)
        if b is None:
            continue
        if b >= tp_target:
            exit_reason = "TP"
            exit_c = tp_target
            break
        if sl_trigger is not None and b <= sl_trigger:
            exit_reason = "SL"
            exit_c = max(1, b - 1)
            taker = True
            break
        if ts >= pre_expiry_ts:
            exit_reason = "PRE_EXP"
            exit_c = max(1, b)
            taker = True
            break
    if exit_reason is None:
        # No exit fired — settle at result.
        won = (sig.side == "yes" and o.settled_yes) or \
              (sig.side == "no" and not o.settled_yes)
        exit_c = 100 if won else 0
        exit_reason = "SETTLE_W" if won else "SETTLE_L"

    gross = exit_c - sig.entry_c
    fee_in = kalshi_fee_cents(sig.entry_c, taker=False)
    if exit_reason in ("SETTLE_W", "SETTLE_L"):
        fee = fee_in  # no fee on settle
    else:
        fee = fee_in + kalshi_fee_cents(exit_c, taker=taker)
    return exit_reason, float(gross - fee)


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
    print(f"  {len(markets):,} markets / {len(candle_idx):,} candle tickers")
    print()

    outcomes: list[FvgOutcome] = []
    print("Finding FVG signals + tracking MFE/MAE...", flush=True)
    for m in markets:
        sig = find_fvg_signal(m, btc_index, candle_idx)
        if not sig:
            continue
        candles = candle_idx.get(m["ticker"]) or []
        out = simulate_fvg_track(sig, candles, m["close_ts"],
                                 m["result"] == "yes")
        outcomes.append(out)
    print(f"  found {len(outcomes):,} FVG signals across {len(markets):,} markets")
    print()

    # ── Gap closure stats ───────────────────────────────────────────────
    n = len(outcomes)
    if not n:
        print("No signals.")
        return 0
    full = sum(1 for o in outcomes if o.closed_full_gap)
    half = sum(1 for o in outcomes if o.closed_half_gap)
    minc = sum(1 for o in outcomes if o.closed_min_gap)
    direct = sum(1 for o in outcomes if o.directionally_correct)
    avg_mfe_above_entry = sum(o.mfe_c - o.sig.entry_c for o in outcomes) / n
    avg_mae_below_entry = sum(o.mae_c - o.sig.entry_c for o in outcomes) / n

    print("=== Gap-closure rates ===")
    print(f"  signals:                  {n:,}")
    print(f"  Full gap closed (MFE >= fair-1):     "
          f"{full:>5} ({full / n * 100:>5.1f}%)")
    print(f"  Half gap closed (MFE >= entry+spread/2): "
          f"{half:>5} ({half / n * 100:>5.1f}%)")
    print(f"  Min gap closed (MFE >= entry+8c):     "
          f"{minc:>5} ({minc / n * 100:>5.1f}%)")
    print(f"  Directionally correct (MFE > entry):  "
          f"{direct:>5} ({direct / n * 100:>5.1f}%)")
    print(f"  Avg MFE above entry: +{avg_mfe_above_entry:.2f}c")
    print(f"  Avg MAE below entry: {avg_mae_below_entry:.2f}c")
    print()

    # MFE distribution histogram
    print("=== MFE distribution (bid above entry) ===")
    buckets = [(-50, 0), (0, 5), (5, 10), (10, 15), (15, 25), (25, 40), (40, 100)]
    for lo, hi in buckets:
        sub = [o for o in outcomes if lo <= (o.mfe_c - o.sig.entry_c) < hi]
        pct = len(sub) / n * 100
        print(f"  {lo:>+3}c .. {hi:>+3}c:  n={len(sub):>5} ({pct:>5.1f}%)")
    print()

    # ── TP-target sweep ─────────────────────────────────────────────────
    print("=== TP-target sweep (entry + N cents, with/without SL@8c) ===")
    print(f"  {'TP=+':<7} {'mode':<6} {'n':>5} {'TP%':>5} {'SL%':>5} "
          f"{'preX%':>5} {'sW%':>5} {'sL%':>5} "
          f"{'total $':>9} {'avg $':>8} {'max DD':>8}")
    tp_levels: list = [8, 12, 15, 20, 25, 30, 35, "FAIR-5", "FAIR-3", "FAIR-1"]
    for tp_off in tp_levels:
        for sl_off in (None, 8):
            mode = "NoSL" if sl_off is None else f"SL{sl_off}"
            results: list[tuple[str, float]] = []
            for o in outcomes:
                candles = candle_idx.get(o.sig.ticker) or []
                results.append(
                    pnl_for_tp_sl(o, tp_offset=tp_off, sl_offset=sl_off,
                                  candles=candles,
                                  close_ts=markets[next(i for i, m in enumerate(markets)
                                                       if m["ticker"] == o.sig.ticker)]["close_ts"])
                )
            tp_n = sum(1 for r, _ in results if r == "TP")
            sl_n = sum(1 for r, _ in results if r == "SL")
            preX_n = sum(1 for r, _ in results if r == "PRE_EXP")
            sw_n = sum(1 for r, _ in results if r == "SETTLE_W")
            sl2_n = sum(1 for r, _ in results if r == "SETTLE_L")
            tot = sum(p for _, p in results) / 100.0
            avg = tot / len(results) if results else 0
            # Equity-curve drawdown.
            eq = peak = 0.0
            dd = 0.0
            for _, p in results:
                eq += p
                peak = max(peak, eq)
                dd = min(dd, eq - peak)
            tp_label = f"+{tp_off}c" if isinstance(tp_off, int) else str(tp_off)
            print(
                f"  {tp_label:<7} {mode:<6} {len(results):>5} "
                f"{tp_n / len(results) * 100:>4.1f}% "
                f"{sl_n / len(results) * 100:>4.1f}% "
                f"{preX_n / len(results) * 100:>4.1f}% "
                f"{sw_n / len(results) * 100:>4.1f}% "
                f"{sl2_n / len(results) * 100:>4.1f}% "
                f"{tot:>+8.2f} {avg:>+7.4f} {dd / 100:>+7.2f}"
            )
        print()

    # CSV per-trade
    path = OUT_DIR / "fvg_outcomes.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "side", "entry_c", "fair_for_side", "spread_c",
            "baseline_c", "mfe_c", "mae_c", "mfe_at_s", "mae_at_s",
            "final_bid_c", "settled_yes", "btc_dist_pct",
            "closed_full", "closed_half", "closed_min", "directional",
        ])
        for o in outcomes:
            w.writerow([
                o.sig.ticker, o.sig.side, o.sig.entry_c, o.sig.fair_for_side,
                o.sig.spread_c, o.sig.baseline_c, o.mfe_c, o.mae_c,
                o.mfe_at_s, o.mae_at_s, o.final_bid_c, int(o.settled_yes),
                f"{o.sig.btc_dist_pct:.4f}",
                int(o.closed_full_gap), int(o.closed_half_gap),
                int(o.closed_min_gap), int(o.directionally_correct),
            ])
    print(f"\n  per-trade CSV: {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
