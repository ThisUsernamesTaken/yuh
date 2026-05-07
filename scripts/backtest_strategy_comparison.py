"""Comprehensive strategy comparison: pick the highest-alpha entry rule.

Backtests N candidate strategies against the SAME settled-market corpus
(197 finalized binary markets with full snapshot data). Each strategy
gets one shot per ticker — first qualifying signal before some time cap,
buy at ask (conservative taker), hold to settlement (Kalshi auto-pays
$0/$1 binary).

Reports per-strategy:
  - n trades fired (signal coverage)
  - win rate
  - mean P&L per trade (alpha density)
  - total $ across corpus
  - max drawdown (worst-case sequence)
  - robustness check (excl. top win — is the edge real or single-trade
    skewed?)
  - entry-price distribution (fragile to bucket?)

Then ranks strategies and identifies the winner.

Strategies tested:
  A. DIRECTION (current): dist>=0.10% AND |mom|>=$10
  B. DIRECTION-strict: dist>=0.20% AND |mom|>=$30 (highest backtest win rate)
  C. DIRECTION-loose: dist>=0.05% AND |mom|>=$5 (more fires)
  D. CHEAP-UNDERDOG: cheap side <= 25c, any time pre-min-10
  E. MARKET-IMPLIED-TRUST: buy mid>50 → YES, mid<50 → NO (sanity)
  F. BB-MODEL-EDGE: |model_prob_yes - mkt_yes_prob| >= 15pp
  G. MOMENTUM-ONLY: buy whichever side momentum points (no dist filter)
  H. HYBRID: DIRECTION + cheap-side filter (entry <= 35c)
  I. CONVICTION-WEIGHTED: DIRECTION but only fire when both dist AND mom
     are well past thresholds (defensive variant of B)
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

TRADES_DB = "data/trades.db"
SETTLE_DB = "data/kalshi_external_backtest.db"


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
    settled = {}
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
        "btc_5m_move, model_prob_yes, regime "
        "FROM window_snapshots ORDER BY ticker, t_offset_sec"
    ).fetchall()
    conn.close()
    snaps_by_ticker = defaultdict(list)
    for ticker, t, btc_c, bid, ask, mid, mom, prob, regime in rows:
        snaps_by_ticker[ticker].append((t, btc_c, bid, ask, mid, mom, prob, regime))
    return settled, snaps_by_ticker


# ── Strategy decision functions ────────────────────────────────────────


def strat_direction(btc, strike, mom, prob, mid, *, dist_thresh, mom_thresh):
    if mom is None or strike <= 0:
        return None
    dist = (btc - strike) / strike
    if dist >= dist_thresh and mom >= mom_thresh:
        return "yes"
    if dist <= -dist_thresh and mom <= -mom_thresh:
        return "no"
    return None


def strat_cheap_underdog(btc, strike, mom, prob, mid, ask_yes=None, ask_no=None,
                          *, max_entry_c=25):
    if ask_yes is None or ask_no is None:
        return None
    if ask_yes <= ask_no and ask_yes <= max_entry_c:
        return "yes"
    if ask_no < ask_yes and ask_no <= max_entry_c:
        return "no"
    return None


def strat_market_implied(btc, strike, mom, prob, mid):
    if mid is None:
        return None
    if mid > 50:
        return "yes"
    if mid < 50:
        return "no"
    return None


def strat_bb_model_edge(btc, strike, mom, prob, mid, *, edge_pp=15):
    """Buy side where model says mispriced by edge_pp+."""
    if prob is None or mid is None:
        return None
    mkt_yes = mid / 100.0
    edge_yes = (prob - mkt_yes) * 100  # +pp means YES underpriced
    if edge_yes >= edge_pp:
        return "yes"
    if edge_yes <= -edge_pp:
        return "no"
    return None


def strat_momentum_only(btc, strike, mom, prob, mid, *, mom_thresh=10):
    if mom is None:
        return None
    if mom >= mom_thresh:
        return "yes"
    if mom <= -mom_thresh:
        return "no"
    return None


def strat_hybrid(btc, strike, mom, prob, mid, ask_yes, ask_no,
                 *, dist_thresh=0.001, mom_thresh=10, max_entry_c=35):
    """DIRECTION + cheap-side filter."""
    side = strat_direction(btc, strike, mom, prob, mid,
                            dist_thresh=dist_thresh, mom_thresh=mom_thresh)
    if side is None:
        return None
    if side == "yes" and ask_yes is not None and ask_yes <= max_entry_c:
        return "yes"
    if side == "no" and ask_no is not None and ask_no <= max_entry_c:
        return "no"
    return None


# ── Simulator ──────────────────────────────────────────────────────────


def simulate(snaps, market_result, strike, decide, *,
             max_offset_s=600, max_entry_c=99, contracts=10,
             needs_both_asks=False):
    for t_off, btc_c, bid, ask, mid, mom, prob, regime in snaps:
        if t_off >= max_offset_s:
            break
        if not bid or not ask or bid >= 100 or ask >= 100:
            continue
        btc = btc_c / 100.0
        ask_yes = ask
        ask_no = 100 - bid if bid > 0 else None
        try:
            if needs_both_asks:
                side = decide(btc, strike, mom, prob, mid, ask_yes, ask_no)
            else:
                side = decide(btc, strike, mom, prob, mid)
        except TypeError:
            try:
                side = decide(btc, strike, mom, prob, mid, ask_yes=ask_yes,
                               ask_no=ask_no)
            except Exception:
                side = None
        if side is None:
            continue
        if side == "yes":
            entry_c = ask_yes
        else:
            entry_c = ask_no if ask_no else 99
        if entry_c <= 0 or entry_c > max_entry_c:
            continue
        if (side == "yes" and market_result == "yes") or \
           (side == "no" and market_result == "no"):
            settle_c = 100
        else:
            settle_c = 0
        fees = taker_fee_cents(entry_c, contracts)
        pnl = (settle_c - entry_c) * contracts - fees
        return Trade("", side, t_off, entry_c, settle_c, pnl, contracts)
    return None


def run_corpus(settled, snaps_by_ticker, decide, **sim_kw):
    trades = []
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


# ── Reporting ──────────────────────────────────────────────────────────


def equity_curve(trades, starting_dollars=50.0, contracts=10):
    bal = starting_dollars * 100
    curve = [bal]
    for t in sorted(trades, key=lambda t: t.entry_offset_s):
        cost = t.entry_c * contracts
        if cost > bal:
            curve.append(bal)
            continue
        bal += t.pnl_cents
        curve.append(bal)
    return curve


def summarize(trades, label, contracts=10):
    if not trades:
        print(f"{label:<45} n=0")
        return None
    n = len(trades)
    pnls = [t.pnl_cents for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    pnls_sorted = sorted(pnls)
    sans_top = sum(pnls_sorted[:-1]) / max(1, n - 1) if n > 1 else 0
    total = sum(pnls) / 100
    mean = statistics.mean(pnls)
    curve = equity_curve(trades, contracts=contracts)
    max_dd = (max(curve) - min(curve)) / 100 if len(curve) > 1 else 0
    end_bal = curve[-1] / 100 if curve else 0
    print(f"{label:<45} n={n:>4} win={wins/n*100:>5.1f}% "
          f"mean=${mean/100:+5.2f} sans_top=${sans_top/100:+5.2f} "
          f"total=${total:+7.2f} end=${end_bal:>6.2f} dd=${max_dd:>5.2f}")
    return {
        "label": label,
        "n": n, "win_rate": wins/n,
        "mean_c": mean, "total_d": total,
        "sans_top_c": sans_top, "end_bal_d": end_bal,
        "max_dd_d": max_dd,
    }


def main():
    print("Loading...")
    settled, snaps = load_data()
    common = [t for t in settled if t in snaps]
    print(f"  {len(common)} joinable tickers (yes={sum(1 for t in common if settled[t][0]=='yes')}, "
          f"no={sum(1 for t in common if settled[t][0]=='no')})")
    print()
    print("=" * 110)
    print(f"STRATEGY COMPARISON — entry pre-min-10, hold to settle, 10ct flat, $50 BR")
    print("=" * 110)
    print(f"{'STRATEGY':<45} {'N':>4} {'WIN%':>6} {'MEAN':>9} {'SANS_TOP':>9} "
          f"{'TOTAL':>8} {'END':>7} {'DD':>6}")
    print("-" * 110)

    results = []

    # A. DIRECTION (current live thresholds)
    f = lambda b,s,m,p,mi: strat_direction(b,s,m,p,mi, dist_thresh=0.001, mom_thresh=10)
    results.append(summarize(run_corpus(settled, snaps, f), "A. DIRECTION 0.10%/$10 (current)"))

    # B. DIRECTION-strict
    f = lambda b,s,m,p,mi: strat_direction(b,s,m,p,mi, dist_thresh=0.002, mom_thresh=30)
    results.append(summarize(run_corpus(settled, snaps, f), "B. DIRECTION 0.20%/$30 (strict)"))

    # B2. DIRECTION 0.15%/$20 (middle)
    f = lambda b,s,m,p,mi: strat_direction(b,s,m,p,mi, dist_thresh=0.0015, mom_thresh=20)
    results.append(summarize(run_corpus(settled, snaps, f), "B2. DIRECTION 0.15%/$20 (middle)"))

    # C. DIRECTION-loose
    f = lambda b,s,m,p,mi: strat_direction(b,s,m,p,mi, dist_thresh=0.0005, mom_thresh=5)
    results.append(summarize(run_corpus(settled, snaps, f), "C. DIRECTION 0.05%/$5 (loose)"))

    # D. CHEAP-UNDERDOG
    def f_cheap(b,s,m,p,mi, ay=None, an=None):
        return strat_cheap_underdog(b,s,m,p,mi, ask_yes=ay, ask_no=an, max_entry_c=25)
    results.append(summarize(run_corpus(settled, snaps, f_cheap, needs_both_asks=True),
                              "D. CHEAP <= 25c"))

    # D2. CHEAP <= 20c
    def f_cheap20(b,s,m,p,mi, ay=None, an=None):
        return strat_cheap_underdog(b,s,m,p,mi, ask_yes=ay, ask_no=an, max_entry_c=20)
    results.append(summarize(run_corpus(settled, snaps, f_cheap20, needs_both_asks=True),
                              "D2. CHEAP <= 20c"))

    # E. MARKET-IMPLIED-TRUST (sanity)
    results.append(summarize(run_corpus(settled, snaps, strat_market_implied),
                              "E. MARKET-IMPLIED (mid>50 -> YES)"))

    # F. BB-MODEL-EDGE
    f = lambda b,s,m,p,mi: strat_bb_model_edge(b,s,m,p,mi, edge_pp=15)
    results.append(summarize(run_corpus(settled, snaps, f), "F. BB MODEL EDGE 15pp"))

    f = lambda b,s,m,p,mi: strat_bb_model_edge(b,s,m,p,mi, edge_pp=20)
    results.append(summarize(run_corpus(settled, snaps, f), "F2. BB MODEL EDGE 20pp"))

    # G. MOMENTUM-ONLY
    f = lambda b,s,m,p,mi: strat_momentum_only(b,s,m,p,mi, mom_thresh=10)
    results.append(summarize(run_corpus(settled, snaps, f), "G. MOMENTUM only ($10)"))

    f = lambda b,s,m,p,mi: strat_momentum_only(b,s,m,p,mi, mom_thresh=30)
    results.append(summarize(run_corpus(settled, snaps, f), "G2. MOMENTUM only ($30)"))

    # H. HYBRID: DIRECTION + cheap entry
    def f_hybrid(b,s,m,p,mi, ay=None, an=None):
        return strat_hybrid(b,s,m,p,mi, ay, an,
                             dist_thresh=0.001, mom_thresh=10, max_entry_c=35)
    results.append(summarize(run_corpus(settled, snaps, f_hybrid, needs_both_asks=True),
                              "H. DIRECTION + entry <= 35c"))

    def f_hybrid25(b,s,m,p,mi, ay=None, an=None):
        return strat_hybrid(b,s,m,p,mi, ay, an,
                             dist_thresh=0.001, mom_thresh=10, max_entry_c=25)
    results.append(summarize(run_corpus(settled, snaps, f_hybrid25, needs_both_asks=True),
                              "H2. DIRECTION + entry <= 25c"))

    # I. CONVICTION-WEIGHTED: very strict
    f = lambda b,s,m,p,mi: strat_direction(b,s,m,p,mi, dist_thresh=0.0025, mom_thresh=50)
    results.append(summarize(run_corpus(settled, snaps, f), "I. DIRECTION 0.25%/$50 (extreme)"))

    print()
    print("=" * 110)
    print("RANKING by MEAN per-trade alpha (filtered by n>=20, sans_top >= -50c — i.e. not single-trade-skewed)")
    print("=" * 110)
    valid = [r for r in results if r and r["n"] >= 20 and r["sans_top_c"] >= -50]
    valid.sort(key=lambda r: r["mean_c"], reverse=True)
    print(f"{'#':<3} {'STRATEGY':<45} {'N':>4} {'WIN%':>6} {'MEAN':>9} {'TOTAL':>8} {'END':>7}")
    for i, r in enumerate(valid[:8], 1):
        print(f"{i:<3} {r['label']:<45} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['mean_c']/100:>+5.2f}    ${r['total_d']:>+7.2f} ${r['end_bal_d']:>6.2f}")
    print()
    print("=" * 110)
    print("RANKING by TOTAL $ across full corpus (raw alpha extracted)")
    print("=" * 110)
    valid_total = [r for r in results if r and r["n"] >= 20]
    valid_total.sort(key=lambda r: r["total_d"], reverse=True)
    for i, r in enumerate(valid_total[:8], 1):
        print(f"{i:<3} {r['label']:<45} {r['n']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['mean_c']/100:>+5.2f}    ${r['total_d']:>+7.2f} ${r['end_bal_d']:>6.2f}")
    print()
    print("=" * 110)
    print("ROBUSTNESS — sans_top is mean P&L EXCLUDING the single largest win")
    print("            (filters out single-trade-skewed 'edges')")
    print("=" * 110)
    for r in valid:
        if r is None:
            continue
        skew_pct = (r["mean_c"] - r["sans_top_c"]) / max(abs(r["mean_c"]), 1) * 100
        print(f"  {r['label']:<45} mean=${r['mean_c']/100:+5.2f} sans_top=${r['sans_top_c']/100:+5.2f} "
              f"(top win = {skew_pct:>5.1f}% of edge)")


if __name__ == "__main__":
    main()
