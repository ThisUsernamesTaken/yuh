"""Backtest the UnifiedScorer against the same settled-market corpus used by
backtest_strategy_comparison.py, sweeping weight presets to calibrate.

For each settled binary market:
  - Walk window_snapshots in time order, call backtest_decide() on each row
  - On first non-None decision, buy at ask, hold to settlement (Kalshi
    auto-pays $0/$1 binary). One trade per ticker (per-window lock).
  - Settle p&l with the same taker-fee model as backtest_strategy_comparison.

Weight presets tested:
  - default       : module defaults (BB 0.25, mom 0.20, lag 0.15, ...)
  - momentum      : tilt toward direction (mom 0.35, BB 0.15)
  - mispricing    : tilt toward BB fair value (BB 0.40, mom 0.10)
  - balanced      : every available signal equal at 0.125

DIRECTION 0.10%/$10 is run against the same corpus with the same fee
model and the same one-trade-per-ticker rule, so the unified scorer
numbers are directly comparable to what we already have live.

Run: cd btc-bias-engine && python scripts/backtest_unified_scorer.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# Make sibling package imports work when run from anywhere
sys.path.insert(0, ".")

import unified_scorer as us
from unified_scorer import UnifiedScorer, backtest_decide

TRADES_DB = "data/trades.db"
SETTLE_DB = "data/kalshi_external_backtest.db"
WINDOW_SECONDS = 900   # Kalshi 15m window
MAX_ENTRY_OFFSET_S = 600  # match live: no entry after minute 10
DEFAULT_BALANCE_C = 5_000  # $50 starting bankroll (matches existing backtest)
CONTRACTS_FALLBACK = 10    # for parity with backtest_strategy_comparison


def taker_fee_cents(price_c: int, count: int) -> float:
    p = price_c / 100.0
    maker = 0.07 * p * (1 - p) * count * 100
    return maker * 1.4


@dataclass
class Trade:
    ticker: str
    side: str
    entry_offset_s: int
    entry_c: int
    settle_c: int
    pnl_cents: float
    contracts: int


def load_data():
    conn = sqlite3.connect(SETTLE_DB)
    settled: Dict[str, tuple] = {}
    for ticker, raw, result in conn.execute(
        "SELECT ticker, raw_json, result FROM markets "
        "WHERE status='finalized' AND result IN ('yes','no')"
    ):
        try:
            j = json.loads(raw)
            strike = float(j.get("floor_strike") or 0)
        except Exception:
            strike = 0
        if strike <= 0:
            continue
        settled[ticker] = (result, strike)
    conn.close()

    conn = sqlite3.connect(TRADES_DB)
    rows = conn.execute(
        "SELECT ticker, t_offset_sec, btc_price_cents, "
        "kalshi_bid_cents, kalshi_ask_cents, kalshi_mid_cents, "
        "btc_5m_move, model_prob_yes "
        "FROM window_snapshots ORDER BY ticker, t_offset_sec"
    ).fetchall()
    conn.close()
    snaps_by_ticker = defaultdict(list)
    for ticker, t, btc_c, bid, ask, mid, mom, prob in rows:
        snaps_by_ticker[ticker].append((t, btc_c, bid, ask, mid, mom, prob))
    return settled, snaps_by_ticker


# ── Strategies ────────────────────────────────────────────────────────────


def direction_decide(btc, strike, mom, prob, mid, ask_yes, ask_no, seconds_left,
                     *, dist_thresh=0.001, mom_thresh=10):
    """Live DIRECTION strategy — used as the baseline."""
    if mom is None or strike <= 0:
        return None
    dist = (btc - strike) / strike
    if dist >= dist_thresh and mom >= mom_thresh:
        return ("yes", ask_yes, 1)  # contracts=1 at this layer; sized later
    if dist <= -dist_thresh and mom <= -mom_thresh and ask_no:
        return ("no", ask_no, 1)
    return None


def make_unified_decide(scorer: UnifiedScorer):
    def decide(btc, strike, mom, prob, mid, ask_yes, ask_no, seconds_left):
        sig = backtest_decide(
            btc=btc, strike=strike, mom=mom, prob=prob, mid=mid,
            ask_yes=ask_yes, ask_no=ask_no, seconds_left=seconds_left,
            balance_cents=DEFAULT_BALANCE_C, scorer=scorer,
        )
        if sig is None:
            return None
        return (sig.side, sig.ask_cents, sig.recommended_contracts)
    return decide


# ── Simulator ────────────────────────────────────────────────────────────


def simulate(snaps, market_result, strike, decide,
             *, max_offset_s=MAX_ENTRY_OFFSET_S, contracts_default=CONTRACTS_FALLBACK,
             use_kelly_size=False) -> Optional[Trade]:
    for t_off, btc_c, bid, ask, mid, mom, prob in snaps:
        if t_off >= max_offset_s:
            break
        if not bid or not ask or bid >= 100 or ask >= 100:
            continue
        btc = btc_c / 100.0
        ask_yes = ask
        ask_no = (100 - bid) if bid > 0 else None
        seconds_left = WINDOW_SECONDS - t_off
        try:
            res = decide(btc, strike, mom, prob, mid, ask_yes, ask_no,
                         seconds_left)
        except Exception:
            res = None
        if res is None:
            continue
        side, entry_c, sized = res
        if entry_c is None or entry_c <= 0 or entry_c >= 100:
            continue
        contracts = max(1, sized) if use_kelly_size else contracts_default
        if (side == "yes" and market_result == "yes") or \
           (side == "no" and market_result == "no"):
            settle_c = 100
        else:
            settle_c = 0
        fees = taker_fee_cents(entry_c, contracts)
        pnl = (settle_c - entry_c) * contracts - fees
        return Trade("", side, t_off, entry_c, settle_c, pnl, contracts)
    return None


def run_corpus(settled, snaps_by_ticker, decide, **sim_kw) -> List[Trade]:
    trades: List[Trade] = []
    for ticker, (result, strike) in settled.items():
        s = snaps_by_ticker.get(ticker)
        if not s:
            continue
        rec = simulate(s, result, strike, decide, **sim_kw)
        if rec is None:
            continue
        rec.ticker = ticker
        trades.append(rec)
    return trades


# ── Reporting ────────────────────────────────────────────────────────────


def equity_curve(trades, starting_dollars=50.0):
    bal = starting_dollars * 100
    curve = [bal]
    for t in sorted(trades, key=lambda t: t.entry_offset_s):
        cost = t.entry_c * t.contracts
        if cost > bal:
            curve.append(bal)
            continue
        bal += t.pnl_cents
        curve.append(bal)
    return curve


def summarize(trades, label):
    if not trades:
        print(f"{label:<55} n=0")
        return None
    n = len(trades)
    pnls = [t.pnl_cents for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    pnls_sorted = sorted(pnls)
    sans_top = sum(pnls_sorted[:-1]) / max(1, n - 1) if n > 1 else 0
    total = sum(pnls) / 100
    mean = statistics.mean(pnls)
    median = statistics.median(pnls)
    stdev = statistics.pstdev(pnls) if n > 1 else 0
    sharpe_like = (mean / stdev) if stdev > 0 else 0  # per-trade Sharpe-ish
    contracts_avg = statistics.mean(t.contracts for t in trades)

    curve = equity_curve(trades)
    max_dd = (max(curve) - min(curve)) / 100 if len(curve) > 1 else 0
    end_bal = curve[-1] / 100 if curve else 0
    risk_adj = total / max(max_dd, 0.01)  # total $ / max drawdown $

    print(f"{label:<55} n={n:>4} W={wins:>3} L={losses:>3} "
          f"win={wins/n*100:>5.1f}% mean=${mean/100:+5.2f} "
          f"med=${median/100:+5.2f} stdev=${stdev/100:>5.2f} "
          f"sharpe={sharpe_like:>+4.2f} ct={contracts_avg:>3.1f}")
    print(f"{'':<55}    sans_top=${sans_top/100:+5.2f} total=${total:+7.2f} "
          f"end=${end_bal:>6.2f} max_dd=${max_dd:>5.2f} "
          f"return/dd={risk_adj:>+5.2f}")
    return {
        "label": label,
        "n": n, "wins": wins, "losses": losses,
        "win_rate": wins / n,
        "mean_c": mean, "median_c": median,
        "stdev_c": stdev, "sharpe": sharpe_like,
        "total_d": total,
        "sans_top_c": sans_top,
        "end_bal_d": end_bal, "max_dd_d": max_dd,
        "return_dd_ratio": risk_adj,
        "contracts_avg": contracts_avg,
    }


# ── Weight presets ──────────────────────────────────────────────────────


WEIGHT_PRESETS = {
    "default":    None,  # use module default
    "momentum":   {  # tilt toward direction strategy
        "bb_mispricing":    0.15,
        "btc_momentum":     0.35,
        "kalshi_lag":       0.15,
        "book_imbalance":   0.10,
        "taker_flow":       0.10,
        "ta_composite":     0.08,
        "session_timing":   0.07,
        "wall_consumption": 0.00,
    },
    "mispricing": {  # tilt toward BB fair value
        "bb_mispricing":    0.40,
        "btc_momentum":     0.10,
        "kalshi_lag":       0.15,
        "book_imbalance":   0.10,
        "taker_flow":       0.10,
        "ta_composite":     0.08,
        "session_timing":   0.07,
        "wall_consumption": 0.00,
    },
    "balanced":   {  # all equal at 0.125 each
        "bb_mispricing":    0.125,
        "btc_momentum":     0.125,
        "kalshi_lag":       0.125,
        "book_imbalance":   0.125,
        "taker_flow":       0.125,
        "ta_composite":     0.125,
        "session_timing":   0.125,
        "wall_consumption": 0.125,
    },
    # heavy backtest signals only (BB + mom + timing) — what's actually
    # populated in the historical corpus
    "live-signals-only": {
        "bb_mispricing":    0.40,
        "btc_momentum":     0.40,
        "kalshi_lag":       0.0,
        "book_imbalance":   0.0,
        "taker_flow":       0.0,
        "ta_composite":     0.0,
        "session_timing":   0.20,
        "wall_consumption": 0.0,
    },
}


def with_weights(weights: Optional[Dict[str, float]]):
    """Patch module WEIGHTS in place; restore after."""
    saved = dict(us.WEIGHTS)

    class Ctx:
        def __enter__(self):
            if weights is not None:
                # normalize so sum=1
                total = sum(weights.values()) or 1.0
                for k in us.WEIGHTS:
                    us.WEIGHTS[k] = weights.get(k, 0.0) / total
            return self

        def __exit__(self, *a):
            us.WEIGHTS.clear()
            us.WEIGHTS.update(saved)

    return Ctx()


# ── Sweep configurations on knobs ──────────────────────────────────────


KNOB_SWEEPS = [
    # (label, min_ev_c, min_confidence)
    ("strict (ev>=5, conf>=0.30)", 5.0, 0.30),
    ("default (ev>=3, conf>=0.25)", 3.0, 0.25),
    ("loose (ev>=1, conf>=0.15)",  1.0, 0.15),
]


def main():
    print("Loading corpus...")
    settled, snaps = load_data()
    common = [t for t in settled if t in snaps]
    yes_n = sum(1 for t in common if settled[t][0] == "yes")
    no_n = sum(1 for t in common if settled[t][0] == "no")
    print(f"  {len(common)} joinable tickers (yes={yes_n}, no={no_n})")
    print(f"  total snapshots: {sum(len(v) for v in snaps.values())}")
    print()

    print("=" * 130)
    print("BASELINE: live DIRECTION 0.10%/$10")
    print("=" * 130)
    base_trades = run_corpus(settled, snaps, direction_decide,
                              contracts_default=CONTRACTS_FALLBACK)
    base = summarize(base_trades, "DIRECTION 0.10%/$10 (10ct flat)")
    print()

    print("=" * 130)
    print("UNIFIED SCORER — weight presets (10ct flat for fair comparison)")
    print("=" * 130)

    flat_results = []
    for preset_name, weights in WEIGHT_PRESETS.items():
        with with_weights(weights):
            scorer = UnifiedScorer(min_ev_c=us.DEFAULT_MIN_EV_C,
                                   min_confidence=us.DEFAULT_MIN_CONFIDENCE,
                                   min_seconds=us.DEFAULT_MIN_SECONDS)
            decide = make_unified_decide(scorer)
            trades = run_corpus(settled, snaps, decide,
                                 contracts_default=CONTRACTS_FALLBACK,
                                 use_kelly_size=False)
            r = summarize(trades, f"UNIFIED [{preset_name}] flat-10ct")
            if r:
                r["preset"] = preset_name
                flat_results.append(r)
        print()

    print("=" * 130)
    print("UNIFIED SCORER — Kelly-sized contracts (per-trade) using each preset")
    print("=" * 130)
    kelly_results = []
    for preset_name, weights in WEIGHT_PRESETS.items():
        with with_weights(weights):
            scorer = UnifiedScorer()
            decide = make_unified_decide(scorer)
            trades = run_corpus(settled, snaps, decide, use_kelly_size=True)
            r = summarize(trades, f"UNIFIED [{preset_name}] Kelly")
            if r:
                r["preset"] = preset_name
                kelly_results.append(r)
        print()

    print("=" * 130)
    print("KNOB SWEEP on best preset — min_ev / min_confidence")
    print("=" * 130)
    if flat_results:
        best_preset = max(flat_results, key=lambda r: r["return_dd_ratio"])
        print(f"  best preset by return/dd: {best_preset['preset']}\n")
        weights = WEIGHT_PRESETS[best_preset["preset"]]
        for label, ev_c, conf in KNOB_SWEEPS:
            with with_weights(weights):
                scorer = UnifiedScorer(min_ev_c=ev_c, min_confidence=conf)
                decide = make_unified_decide(scorer)
                trades = run_corpus(settled, snaps, decide,
                                     contracts_default=CONTRACTS_FALLBACK)
                summarize(trades, f"UNIFIED [{best_preset['preset']}] {label}")
            print()

    print("=" * 130)
    print("RANKING — flat-10ct scoring by RETURN / MAX-DD ratio (risk-adjusted)")
    print("=" * 130)
    pool = [base] + flat_results if base else flat_results
    pool.sort(key=lambda r: r["return_dd_ratio"], reverse=True)
    print(f"{'#':<3} {'STRATEGY':<55} {'N':>4} {'WIN%':>6} {'TOTAL':>9} "
          f"{'DD':>7} {'RET/DD':>8} {'SHARPE':>7}")
    print("-" * 130)
    for i, r in enumerate(pool, 1):
        print(f"{i:<3} {r['label']:<55} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['total_d']:>+7.2f} ${r['max_dd_d']:>5.2f} "
              f"{r['return_dd_ratio']:>+7.2f} {r['sharpe']:>+6.2f}")

    print()
    print("=" * 130)
    print("RANKING — flat-10ct by TOTAL $ extracted")
    print("=" * 130)
    pool2 = [base] + flat_results if base else flat_results
    pool2.sort(key=lambda r: r["total_d"], reverse=True)
    for i, r in enumerate(pool2, 1):
        print(f"{i:<3} {r['label']:<55} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['total_d']:>+7.2f} ${r['max_dd_d']:>5.2f} "
              f"{r['return_dd_ratio']:>+7.2f}")


if __name__ == "__main__":
    main()
