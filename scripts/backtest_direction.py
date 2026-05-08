"""Backtest: 'buy whichever direction BTC is moving' strategies.

Tests several simple direction-following theses against actual binary
settlement on Kalshi 15m BTC contracts. The data has:
  - btc_price_cents per snapshot
  - kalshi mid/bid/ask
  - btc_5m_move (signed dollar move over last 5 min)
  - model_prob_yes (BB engine's prob estimate)
  - regime (chop / structured / etc.)
  - market settlement: "yes" or "no" from kalshi_external_backtest.markets

Strategies tested (each at multiple entry-time cutoffs):

  A. BTC vs strike (instantaneous):
       buy YES if btc > strike, NO if btc < strike

  B. BTC 5-min momentum:
       buy YES if btc_5m_move > +threshold, NO if < -threshold

  C. Sign-aligned (A AND B agree):
       buy YES if btc>strike AND momentum>0, NO if reverse

  D. Model prob threshold:
       buy YES if prob_yes > 0.6, NO if < 0.4

  E. Distance + momentum:
       buy YES if (btc-strike)/strike > 0.05% AND momentum>0

  F. Just trust the market (sanity check):
       buy YES if mid > 50, NO otherwise
       (this should be ~50/50 since market is generally efficient)

For each strategy: enter at first qualifying snapshot before time cap,
buy at ASK of chosen side, hold to settlement. P&L = (settle_value - entry)
* contracts - taker fees on entry.

Pure thesis test — no trail, no stop, no per-trade complexity. Just:
"does this directional bet, taken cheaply enough, beat fees + variance?"
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
    settle_c: int          # 100 if won, 0 if lost
    pnl_cents: float
    fees_cents: float
    btc_price: float
    strike: float
    btc_5m_move: float | None
    model_prob: float | None
    mid_c: int
    contracts: int


def load_data():
    """Load settled markets (with strike) + window snapshots."""
    conn = sqlite3.connect(SETTLE_DB)
    cur = conn.cursor()
    settled = {}
    for ticker, raw, result in cur.execute(
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


# ── Strategy decision functions ─────────────────────────────────────────


def strat_btc_vs_strike(btc, strike, mom, prob, mid):
    if btc > strike:
        return "yes"
    if btc < strike:
        return "no"
    return None


def strat_momentum(btc, strike, mom, prob, mid, *, threshold=10.0):
    if mom is None:
        return None
    if mom > threshold:
        return "yes"
    if mom < -threshold:
        return "no"
    return None


def strat_aligned(btc, strike, mom, prob, mid, *, threshold=10.0):
    """BTC above strike AND momentum positive → YES; mirror for NO."""
    if mom is None:
        return None
    if btc > strike and mom > threshold:
        return "yes"
    if btc < strike and mom < -threshold:
        return "no"
    return None


def strat_prob(btc, strike, mom, prob, mid, *, hi=0.60, lo=0.40):
    if prob is None:
        return None
    if prob > hi:
        return "yes"
    if prob < lo:
        return "no"
    return None


def strat_dist_momentum(btc, strike, mom, prob, mid, *,
                         dist_pct=0.0005, threshold=10.0):
    if mom is None or strike <= 0:
        return None
    dist = (btc - strike) / strike
    if dist > dist_pct and mom > threshold:
        return "yes"
    if dist < -dist_pct and mom < -threshold:
        return "no"
    return None


def strat_market_implied(btc, strike, mom, prob, mid):
    """Sanity check: trust the market mid. Should be ~50/50 net of fees."""
    if mid is None:
        return None
    if mid > 50:
        return "yes"
    if mid < 50:
        return "no"
    return None


# ── Simulator ──────────────────────────────────────────────────────────


def simulate(snaps, market_result, strike, decide,
             *, max_offset_s, max_entry_c, contracts,
             nofill_rate=0.0, slip_c=0, fee_buffer_c=0,
             rng=None):
    """Simulate a single ticker's trade decision + outcome.

    Realistic-fill mode (default off, all kwargs default to 0):
    - ``nofill_rate``: probability that the IOC doesn't fill (signal is
      seen but no execution happens). When set, we discard ``nofill_rate``
      fraction of signals at random. This models live-observed NOFILL
      behavior where the offer-side depth was swept between book-read and
      order arrival.
    - ``slip_c``: cents added to entry price (race-condition cost). Even
      when filled, live IOCs typically pay a few cents above book ask.
    - ``fee_buffer_c``: extra fee per contract subtracted from PnL. Models
      Kalshi taker fees that the existing taker_fee_cents() may underweight.
    - ``rng``: optional random.Random instance for reproducibility.
    """
    import random as _rand
    _r = rng if rng is not None else _rand
    for t_off, btc_c, bid, ask, mid, mom, prob, regime in snaps:
        if t_off >= max_offset_s:
            break
        if not bid or not ask or bid >= 100 or ask >= 100:
            continue
        btc = btc_c / 100.0  # was in cents
        side = decide(btc, strike, mom, prob, mid)
        if side is None:
            continue
        # Realistic NOFILL simulation: skip the trade entirely with the
        # configured probability. This is what live execution looks like
        # when offer-side depth disappears between book-read & order send.
        if nofill_rate > 0 and _r.random() < nofill_rate:
            continue
        # Determine entry price (ask of chosen side, plus slippage).
        if side == "yes":
            entry_c = ask + slip_c
        else:
            entry_c = (100 - bid) + slip_c  # no_ask + slip
        if entry_c <= 0 or entry_c > max_entry_c:
            continue
        # Bought. Now compute settlement.
        if (side == "yes" and market_result == "yes") or \
           (side == "no" and market_result == "no"):
            settle_c = 100
        else:
            settle_c = 0
        fees = taker_fee_cents(entry_c, contracts) + (fee_buffer_c * contracts)
        pnl = (settle_c - entry_c) * contracts - fees
        return Trade(
            ticker="", side=side,
            entry_offset_s=t_off, entry_c=entry_c, settle_c=settle_c,
            pnl_cents=pnl, fees_cents=fees,
            btc_price=btc, strike=strike,
            btc_5m_move=mom, model_prob=prob, mid_c=mid,
            contracts=contracts,
        )
    return None


def run(settled, snaps_by_ticker, decide, *,
        max_offset_s=600, max_entry_c=99, contracts=10,
        nofill_rate=0.0, slip_c=0, fee_buffer_c=0, seed=None):
    trades = []
    import random as _rand
    rng = _rand.Random(seed) if seed is not None else None
    for ticker, (result, strike) in settled.items():
        s = snaps_by_ticker.get(ticker)
        if not s:
            continue
        rec = simulate(s, result, strike, decide,
                       max_offset_s=max_offset_s,
                       max_entry_c=max_entry_c, contracts=contracts,
                       nofill_rate=nofill_rate, slip_c=slip_c,
                       fee_buffer_c=fee_buffer_c, rng=rng)
        if rec is None:
            continue
        rec.ticker = ticker
        trades.append(rec)
    return trades


def summarize(trades, label):
    if not trades:
        print(f"{label:<55} n=0")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_cents > 0)
    pnls = [t.pnl_cents for t in trades]
    by_side = defaultdict(list)
    for t in trades:
        by_side[t.side].append(t.pnl_cents)
    yes_n = len(by_side.get("yes", []))
    no_n = len(by_side.get("no", []))
    print(f"{label:<55} n={n:>4} win={wins/n*100:>5.1f}% "
          f"mean={statistics.mean(pnls):+7.1f}c "
          f"total=${sum(pnls)/100:>+7.2f} "
          f"yes/no={yes_n}/{no_n}")


# ── Main ────────────────────────────────────────────────────────────────


def main():
    print("Loading data...")
    settled, snaps = load_data()
    common = [t for t in settled if t in snaps]
    print(f"  {len(common)} joinable tickers")
    print(f"  {sum(1 for t in common if settled[t][0]=='yes')} settled YES, "
          f"{sum(1 for t in common if settled[t][0]=='no')} NO")
    print()

    print("=" * 90)
    print("STRATEGY COMPARISON (entry before min 10, no entry-price cap)")
    print("=" * 90)

    summarize(run(settled, snaps, strat_btc_vs_strike,
                  max_offset_s=600, max_entry_c=99),
              "A. BTC vs strike (any time, any price)")
    summarize(run(settled, snaps, strat_momentum,
                  max_offset_s=600, max_entry_c=99),
              "B. BTC 5m momentum (>$10)")
    summarize(run(settled, snaps, strat_aligned,
                  max_offset_s=600, max_entry_c=99),
              "C. Sign-aligned (above-strike AND momentum)")
    summarize(run(settled, snaps, strat_prob,
                  max_offset_s=600, max_entry_c=99),
              "D. Model prob > 0.6 / < 0.4")
    summarize(run(settled, snaps, strat_dist_momentum,
                  max_offset_s=600, max_entry_c=99),
              "E. Dist > 0.05% AND momentum agree")
    summarize(run(settled, snaps, strat_market_implied,
                  max_offset_s=600, max_entry_c=99),
              "F. Market implied (mid > 50 -> YES) [sanity]")

    print()
    print("=" * 90)
    print("STRATEGY COMPARISON (entry before min 10, max_entry <= 50c)")
    print("=" * 90)

    summarize(run(settled, snaps, strat_btc_vs_strike,
                  max_offset_s=600, max_entry_c=50),
              "A. BTC vs strike (cheap side <= 50c)")
    summarize(run(settled, snaps, strat_momentum,
                  max_offset_s=600, max_entry_c=50),
              "B. BTC 5m momentum (>$10) cheap side")
    summarize(run(settled, snaps, strat_aligned,
                  max_offset_s=600, max_entry_c=50),
              "C. Sign-aligned cheap side")
    summarize(run(settled, snaps, strat_prob,
                  max_offset_s=600, max_entry_c=50),
              "D. Model prob > 0.6 cheap side")
    summarize(run(settled, snaps, strat_dist_momentum,
                  max_offset_s=600, max_entry_c=50),
              "E. Dist + momentum cheap side")

    print()
    print("=" * 90)
    print("ENTRY-TIME SENSITIVITY (strategy C: sign-aligned)")
    print("=" * 90)

    for cap in (180, 300, 600, 900):  # min 3, 5, 10, full window
        summarize(run(settled, snaps, strat_aligned,
                      max_offset_s=cap, max_entry_c=99),
                  f"C @ entry < min {cap//60} (any price)")

    print()
    print("=" * 90)
    print("STRATEGY C variants — momentum threshold sweep")
    print("=" * 90)

    for thresh in (5, 10, 20, 30, 50):
        f = lambda b, s, m, p, mi, _t=thresh: strat_aligned(
            b, s, m, p, mi, threshold=_t)
        summarize(run(settled, snaps, f, max_offset_s=600, max_entry_c=99),
                  f"C threshold=${thresh}/5min")

    print()
    print("=" * 90)
    print("STRATEGY E (distance + momentum) — distance threshold sweep")
    print("=" * 90)
    for d_pct in (0.0001, 0.0003, 0.0005, 0.001, 0.002):
        f = lambda b, s, m, p, mi, _d=d_pct: strat_dist_momentum(
            b, s, m, p, mi, dist_pct=_d, threshold=10)
        summarize(run(settled, snaps, f, max_offset_s=600, max_entry_c=99),
                  f"E dist > {d_pct*100:.3f}%")

    # --- Cheap-only variant of C (most promising aligned strat) ---
    print()
    print("=" * 90)
    print("STRATEGY C CHEAP-FILTERED — entry price sweep")
    print("=" * 90)
    for cap in (15, 25, 35, 50, 99):
        summarize(run(settled, snaps, strat_aligned,
                      max_offset_s=600, max_entry_c=cap),
                  f"C entry <= {cap}c")

    # --- Win/loss decomposition for the best-looking config ---
    print()
    print("=" * 90)
    print("DEEP DIVE: best-looking config")
    print("=" * 90)
    trades = run(settled, snaps, strat_aligned,
                 max_offset_s=600, max_entry_c=99)
    if trades:
        n = len(trades)
        wins = [t for t in trades if t.pnl_cents > 0]
        losses = [t for t in trades if t.pnl_cents <= 0]
        print(f"\nC sign-aligned, pre-min-10, no price cap: {n} trades")
        print(f"  wins:   {len(wins)} avg=${statistics.mean(t.pnl_cents for t in wins)/100:+.2f} "
              f"per win")
        if losses:
            print(f"  losses: {len(losses)} avg=${statistics.mean(t.pnl_cents for t in losses)/100:+.2f} "
                  f"per loss")
        # By entry price bucket
        bucket_data = defaultdict(list)
        for t in trades:
            bucket_data[(t.entry_c // 10) * 10].append(t)
        print(f"\n  by entry price (10c bins):")
        for b in sorted(bucket_data):
            ts = bucket_data[b]
            ps = [t.pnl_cents for t in ts]
            wc = sum(1 for t in ts if t.pnl_cents > 0)
            print(f"    {b:>3}-{b+9:<3}: n={len(ts):>3} "
                  f"win={wc/len(ts)*100:>5.1f}% "
                  f"mean=${statistics.mean(ps)/100:+.2f} "
                  f"total=${sum(ps)/100:+.2f}")

    # --- Equity curve simulation ---
    print()
    print("=" * 90)
    print("EQUITY CURVES — best config (sign-aligned, no price cap, 10ct flat)")
    print("=" * 90)
    trades = run(settled, snaps, strat_aligned,
                 max_offset_s=600, max_entry_c=99)
    trades_sorted = sorted(trades, key=lambda t: t.entry_offset_s)
    bal = 5000  # $50 in cents
    curve = [bal]
    for t in trades_sorted:
        cost = t.entry_c * t.contracts
        if cost > bal:
            curve.append(bal)
            continue
        bal += t.pnl_cents
        curve.append(bal)
    print(f"  Flat 10ct, $50 BR: ${curve[0]/100:.2f} -> ${curve[-1]/100:.2f}, "
          f"peak=${max(curve)/100:.2f}, trough=${min(curve)/100:.2f}")

    # 5ct flat
    bal = 5000
    curve = [bal]
    for t in trades_sorted:
        cost = t.entry_c * 5
        if cost > bal:
            curve.append(bal)
            continue
        # Re-derive PnL at 5ct
        fees = taker_fee_cents(t.entry_c, 5)
        pnl = (t.settle_c - t.entry_c) * 5 - fees
        bal += pnl
        curve.append(bal)
    print(f"  Flat  5ct, $50 BR: ${curve[0]/100:.2f} -> ${curve[-1]/100:.2f}, "
          f"peak=${max(curve)/100:.2f}, trough=${min(curve)/100:.2f}")

    # ── REALISTIC-FILL VALIDATION (2026-05-07 PT evening) ────────────────
    # The backtest above assumes 100% fills at historical ask. Live IOCs
    # observed ~30% NOFILL rate at slip=3-5c, and the actual fill price
    # often includes 5c+ of slippage. Re-run the best config with these
    # production-realistic settings to validate that the +90% WR claim
    # survives the live-execution adjustments.
    print()
    print("=" * 90)
    print("REALISTIC-FILL VALIDATION — strategy E (dist+momentum sweet spot)")
    print("=" * 90)
    print()
    print("Each row averages 10 RNG-seeded runs to reduce variance from")
    print("the random NOFILL sampling.")
    print()

    def _avg_runs(decide_fn, *, contracts, nofill_rate, slip_c, fee_buffer_c,
                  label, n_runs=10):
        """Run with N seeded RNGs, average the summary stats."""
        all_n, all_wins, all_pnl = 0, 0, 0
        for seed in range(n_runs):
            ts = run(settled, snaps, decide_fn,
                     max_offset_s=600, max_entry_c=99,
                     contracts=contracts,
                     nofill_rate=nofill_rate, slip_c=slip_c,
                     fee_buffer_c=fee_buffer_c, seed=seed)
            all_n += len(ts)
            all_wins += sum(1 for t in ts if t.pnl_cents > 0)
            all_pnl += sum(t.pnl_cents for t in ts)
        avg_n = all_n / n_runs
        avg_pnl = all_pnl / n_runs / 100
        wr = (all_wins / all_n * 100) if all_n > 0 else 0.0
        mean_c = (all_pnl / all_n) if all_n > 0 else 0.0
        print(f"  {label:<60} avg_n={avg_n:>5.1f} "
              f"win={wr:>5.1f}% mean={mean_c:+6.1f}c "
              f"total=${avg_pnl:>+7.2f}")

    print("--- Pure backtest (100% fill, 0 slip, 0 fee buffer) ---")
    _avg_runs(strat_dist_momentum, contracts=10,
              nofill_rate=0.0, slip_c=0, fee_buffer_c=0,
              label="E. Dist + momentum (idealized)", n_runs=1)

    print()
    print("--- Add 5c slippage (race-condition cost) ---")
    _avg_runs(strat_dist_momentum, contracts=10,
              nofill_rate=0.0, slip_c=5, fee_buffer_c=0,
              label="E. + 5c slip", n_runs=1)

    print()
    print("--- Add 30% NOFILL (live-observed depth-wall rate) ---")
    _avg_runs(strat_dist_momentum, contracts=10,
              nofill_rate=0.30, slip_c=5, fee_buffer_c=0,
              label="E. + 5c slip + 30% NOFILL")

    print()
    print("--- Add fee buffer 1c/contract (Kalshi taker fee underweight) ---")
    _avg_runs(strat_dist_momentum, contracts=10,
              nofill_rate=0.30, slip_c=5, fee_buffer_c=1,
              label="E. + 5c slip + 30% NOFILL + 1c fee buffer")

    print()
    print("--- Worst-case: 50% NOFILL + 8c slip + 2c fee buffer ---")
    _avg_runs(strat_dist_momentum, contracts=10,
              nofill_rate=0.50, slip_c=8, fee_buffer_c=2,
              label="E. worst-case live execution")

    print()
    print("--- 1ct sizing (post-2026-05-07 tuning) at realistic fills ---")
    _avg_runs(strat_dist_momentum, contracts=1,
              nofill_rate=0.30, slip_c=5, fee_buffer_c=1,
              label="E. 1ct + 5c slip + 30% NOFILL + 1c fee")

    print()
    print("Interpretation:")
    print("- If WR holds within ~5pp of pure across all rows, entry signal")
    print("  is robust. Live underperformance is in execution.")
    print("- If WR drops sharply with NOFILL/slip/fees, the signal's edge")
    print("  was conditional on idealized fills. The live ~28% WR observed")
    print("  on n=7 today is consistent with the realistic-fill column.")


if __name__ == "__main__":
    main()
