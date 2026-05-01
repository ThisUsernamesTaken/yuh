"""ATM Reversion / Strike-Pin Reversion backtest.

Per Codex handoff 2026-04-25: replays 30 days of Kalshi BTC 15m contracts
using the local backtest DB (data/kalshi_external_backtest.db) and tests
the "buy underpriced side near strike, harvest repricing" strategy.

Entry hypothesis:
  When BTC is very close to the contract strike (|dist| <= max_strike_dist_pct),
  Kalshi prices can briefly dislocate away from a 50/50 fair value. Buy the
  side that's significantly under fair, harvest repricing via limit exit.

Two entry modes:
  1. DISCOUNT — ask <= max_entry_c AND fair >= min_fair_c AND edge >= min_edge_c
  2. BIAS (optional) — slightly off-strike: BTC > strike implies YES favored,
     buy YES if ask <= max_bias_entry_c with edge >= min_bias_edge_c

Exit modes (whichever fires first):
  - Bid >= target_c
  - Bid >= entry_c + profit_target_c
  - Strike escapes band (|strike_dist| >= stop_strike_dist_pct)
  - Force time exit (age >= force_exit_age_s)
  - Hold to settlement (default off — adds binary tail risk)

Fair value proxy:
  Default: 50.0c when within strike band (ATM = coin flip).
  When `--use-mid-fair` is set: fair = current mid, treats deviation
  from mid as the dislocation rather than treating it as fair.

Usage:
  python scripts/atm_reversion_backtest.py
  python scripts/atm_reversion_backtest.py --bias-enabled --target-c 49
  python scripts/atm_reversion_backtest.py --max-strike-dist-pct 0.02 --min-fair-c 47
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "kalshi_external_backtest.db"


@dataclass
class Trade:
    ticker: str
    entry_ts: int
    entry_c: int
    side: str  # "yes" or "no"
    kind: str  # "discount" | "bias"
    fair_at_entry_c: float
    edge_at_entry_c: float
    strike_dist_pct: float
    strike: float
    btc_at_entry: float
    exit_ts: int = 0
    exit_c: int = 0
    pnl_c: int = 0
    peak_c: int = 0
    exit_reason: str = ""


def _load_markets(conn: sqlite3.Connection) -> dict:
    """Return ticker -> {strike, result, expiration_value, open_ts, close_ts}."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, open_ts, close_ts, result, status, raw_json FROM markets"
    )
    out = {}
    for row in cur.fetchall():
        ticker, open_ts, close_ts, result, status, raw_json = row
        if status != "finalized":
            continue
        try:
            d = json.loads(raw_json or "{}")
        except Exception:
            continue
        strike = d.get("floor_strike")
        if strike is None:
            continue
        out[ticker] = {
            "strike": float(strike),
            "result": (result or "").lower(),
            "open_ts": int(open_ts or 0),
            "close_ts": int(close_ts or 0),
        }
    return out


def _load_btc_at(conn: sqlite3.Connection) -> dict:
    """Return open_ts -> btc_close so we can match candle timestamps."""
    cur = conn.cursor()
    cur.execute("SELECT open_ts, close FROM btc_1m")
    return {int(r[0]): float(r[1]) for r in cur.fetchall()}


def _ohlc_cents(raw_json: str | None) -> tuple[int, int, int, int, int, int]:
    """Return (yes_bid_low, yes_bid_high, yes_ask_low, yes_ask_high,
              price_low, price_high) cents.

    Parsed from candle.raw_json. 0 means "no data" (used as a guard so we
    don't treat an absent bid as a fill at 0c). Per Codex 2026-04-25 entry
    study — needed for maker_bid_wait / pullback_wait fill simulation.

    `price` = last-trade OHLC. For maker simulation, a trade at or below
    our resting bid is the realistic fill trigger (an aggressive seller
    crossing the book). `yes_ask` low is *offered* ask; some offers don't
    trade. Codex's study used trade-price low.
    """
    if not raw_json:
        return (0, 0, 0, 0, 0, 0)
    try:
        d = json.loads(raw_json)
    except Exception:
        return (0, 0, 0, 0, 0, 0)
    yb = d.get("yes_bid", {}) or {}
    ya = d.get("yes_ask", {}) or {}
    px = d.get("price", {}) or {}
    def _c(s) -> int:
        try:
            return int(round(float(s) * 100))
        except (TypeError, ValueError):
            return 0
    return (_c(yb.get("low_dollars")), _c(yb.get("high_dollars")),
            _c(ya.get("low_dollars")), _c(ya.get("high_dollars")),
            _c(px.get("low_dollars")), _c(px.get("high_dollars")))


def _load_candles_per_ticker(conn: sqlite3.Connection) -> dict:
    """Return ticker -> sorted list of dicts with end_period_ts, yes_bid, yes_ask, price."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, end_period_ts, price_close, yes_bid_close, yes_ask_close, "
        "       volume_fp, raw_json "
        "FROM candles ORDER BY ticker, end_period_ts ASC"
    )
    out: dict = defaultdict(list)
    for row in cur.fetchall():
        ticker, ts, price, ybid, yask, vol, raw_json = row
        ybl, ybh, yal, yah, pl, ph = _ohlc_cents(raw_json)
        out[ticker].append({
            "ts": int(ts or 0),
            "price_c": int(round((price or 0) * 100)),
            "yes_bid_c": int(round((ybid or 0) * 100)),
            "yes_ask_c": int(round((yask or 0) * 100)),
            "yes_bid_low_c": ybl,
            "yes_bid_high_c": ybh,
            "yes_ask_low_c": yal,
            "yes_ask_high_c": yah,
            "price_low_c": pl,
            "price_high_c": ph,
            "vol": float(vol or 0),
        })
    return out


def _get_btc_at_ts(btc_map: dict, ts: int) -> float | None:
    """Find BTC close for the 1m bar containing ts. Each btc_1m row has open_ts;
    the bar covers [open_ts, open_ts+60). Round ts down to the minute."""
    minute_ts = (ts // 60) * 60
    return btc_map.get(minute_ts)


def _eval_atm_discount_entry(
    yes_bid_c: int, yes_ask_c: int, fair_yes_c: float, args
) -> tuple[str, int, float, str] | None:
    """Return (side, entry_c, edge_c, kind) if a discount entry triggers, else None."""
    no_ask_c = 100 - yes_bid_c  # NO ask = 100 - YES bid
    no_bid_c = 100 - yes_ask_c  # NO bid = 100 - YES ask
    fair_no_c = 100.0 - fair_yes_c

    # Check both sides for discount
    yes_edge = fair_yes_c - yes_ask_c
    no_edge = fair_no_c - no_ask_c

    cand_yes = (
        yes_ask_c <= args.max_entry_c
        and fair_yes_c >= args.min_fair_c
        and yes_edge >= args.min_edge_c
    )
    cand_no = (
        no_ask_c <= args.max_entry_c
        and fair_no_c >= args.min_fair_c
        and no_edge >= args.min_edge_c
    )

    if cand_yes and (not cand_no or yes_edge >= no_edge):
        return ("yes", yes_ask_c, yes_edge, "discount")
    if cand_no:
        return ("no", no_ask_c, no_edge, "discount")
    return None


def _eval_bias_entry(
    yes_bid_c: int, yes_ask_c: int, fair_yes_c: float,
    strike_dist_pct: float, args,
) -> tuple[str, int, float, str] | None:
    """Bias side based on which side of strike BTC is on."""
    no_ask_c = 100 - yes_bid_c
    fair_no_c = 100.0 - fair_yes_c
    if strike_dist_pct > 0:  # BTC above strike → YES favored
        edge = fair_yes_c - yes_ask_c
        if yes_ask_c <= args.max_bias_entry_c and edge >= args.min_bias_edge_c:
            return ("yes", yes_ask_c, edge, "bias")
    elif strike_dist_pct < 0:
        edge = fair_no_c - no_ask_c
        if no_ask_c <= args.max_bias_entry_c and edge >= args.min_bias_edge_c:
            return ("no", no_ask_c, edge, "bias")
    return None


ENTRY_MODES = ("immediate_ask", "maker_bid_wait", "pullback_wait")


def _intended_entry_c(side: str, mode: str,
                      signal_yes_bid: int, signal_yes_ask: int,
                      pullback_c: int) -> int:
    """Map (side, mode) → intended fill price in cents.

    immediate_ask:  pay the same-side ask now.
    maker_bid_wait: rest at the same-side bid; fill if the same-side ask
                    later trades down to (or through) our bid.
    pullback_wait:  rest pullback_c BELOW the signal-time same-side ask.
                    Tighter than the bid → fewer fills, lower entry.
    """
    if side == "yes":
        if mode == "immediate_ask":
            return signal_yes_ask
        if mode == "maker_bid_wait":
            return signal_yes_bid
        if mode == "pullback_wait":
            return max(1, signal_yes_ask - pullback_c)
    else:  # no
        no_bid = 100 - signal_yes_ask
        no_ask = 100 - signal_yes_bid
        if mode == "immediate_ask":
            return no_ask
        if mode == "maker_bid_wait":
            return no_bid
        if mode == "pullback_wait":
            return max(1, no_ask - pullback_c)
    raise ValueError(f"unknown entry mode: {mode}")


def _resolve_entry(side: str, mode: str, signal_candle: dict,
                   candles: list, signal_idx: int, args
                   ) -> tuple[int, int] | None:
    """Return (entry_c, fill_idx) or None if the maker order goes unfilled
    within the wait window.

    immediate_ask is unconditional and fills at the signal candle.
    Wait modes scan forward up to args.entry_wait_s seconds and require
    a cross at our intended price.

    Two orthogonal switches govern the fill assumption (Codex 2026-04-25):

    --entry-fill-source
        quote: YES fills if yes_ask_low <= intended,
               NO fills if yes_bid_high >= 100 - intended.
               Strict — only counts a fill when an offer actually appeared
               at our level. Closer to live maker queue semantics.
        trade: YES fills if price_low <= intended,
               NO fills if price_high >= 100 - intended.
               Permissive — counts any print clearing through our price,
               which optimistically assumes our resting order had queue
               priority over everyone else at the same level.

    --entry-include-signal-candle
        False (default): scan starts at signal_idx + 1. The signal-candle
                         OHLC is *lookahead* relative to a signal that
                         fires on candle close, so excluding it is the
                         honest backtest assumption.
        True (diagnostic): scan includes the signal candle's intrabar
                           OHLC. Useful for comparing against earlier
                           informal numbers but should never be used to
                           justify a live promotion.
    """
    sig_yb = signal_candle["yes_bid_c"]
    sig_ya = signal_candle["yes_ask_c"]
    pullback = int(getattr(args, "entry_pullback_c", 2))
    intended = _intended_entry_c(side, mode, sig_yb, sig_ya, pullback)
    if intended <= 0 or intended >= 100:
        return None

    if mode == "immediate_ask":
        return (intended, signal_idx)

    fill_source = getattr(args, "entry_fill_source", "quote")
    include_signal = bool(getattr(args, "entry_include_signal_candle", False))
    wait_s = int(getattr(args, "entry_wait_s", 60))
    sig_ts = signal_candle["ts"]

    start_idx = signal_idx if include_signal else signal_idx + 1
    for j in range(start_idx, len(candles)):
        c = candles[j]
        # Wait window is measured from signal_ts. Including signal candle
        # means j == signal_idx is allowed (delta=0 ≤ wait_s).
        if c["ts"] - sig_ts > wait_s:
            return None
        if side == "yes":
            cross_px = c["price_low_c"] if fill_source == "trade" else c["yes_ask_low_c"]
            if cross_px > 0 and cross_px <= intended:
                return (intended, j)
        else:
            cross_px = c["price_high_c"] if fill_source == "trade" else c["yes_bid_high_c"]
            if cross_px > 0 and cross_px >= 100 - intended:
                return (intended, j)
    return None


def _close_trade(
    trade: Trade, candles: list, candle_idx: int, market: dict, args,
) -> Trade:
    """Walk forward through candles from entry to find exit price/reason.
    Exits in priority order: target / profit_target / strike escape / time / settle."""
    entry_c = trade.entry_c
    side = trade.side
    fair_target = args.target_c
    profit_target = entry_c + args.profit_target_c
    stretch_target = entry_c + args.stretch_profit_target_c

    peak_c = -entry_c  # peak unrealized profit
    for i in range(candle_idx + 1, len(candles)):
        c = candles[i]
        # Same-side bid for exit valuation
        side_bid = c["yes_bid_c"] if side == "yes" else (100 - c["yes_ask_c"])
        unrealized = side_bid - entry_c
        peak_c = max(peak_c, unrealized)

        age_s = c["ts"] - trade.entry_ts

        # Target exits
        if side_bid >= fair_target:
            trade.exit_ts = c["ts"]
            trade.exit_c = side_bid
            trade.exit_reason = f"target_{fair_target}"
            trade.peak_c = peak_c
            trade.pnl_c = side_bid - entry_c
            return trade
        if side_bid >= profit_target:
            trade.exit_ts = c["ts"]
            trade.exit_c = side_bid
            trade.exit_reason = f"profit_{args.profit_target_c}"
            trade.peak_c = peak_c
            trade.pnl_c = side_bid - entry_c
            return trade

        # Strike escape stop
        btc_now = _get_btc_at_ts({}, c["ts"])  # needs btc_map; use args._btc_map
        if hasattr(args, "_btc_map"):
            btc_now = _get_btc_at_ts(args._btc_map, c["ts"])
        if btc_now is not None:
            strike_dist_pct = (btc_now - market["strike"]) / market["strike"] * 100.0
            if abs(strike_dist_pct) >= args.stop_strike_dist_pct:
                trade.exit_ts = c["ts"]
                trade.exit_c = side_bid
                trade.exit_reason = "strike_escape"
                trade.peak_c = peak_c
                trade.pnl_c = side_bid - entry_c
                return trade

        if age_s >= args.force_exit_age_s:
            trade.exit_ts = c["ts"]
            trade.exit_c = side_bid
            trade.exit_reason = "time"
            trade.peak_c = peak_c
            trade.pnl_c = side_bid - entry_c
            return trade

    # No exit in candles: settle
    settle_c = 100 if market["result"] == side else 0
    trade.exit_ts = market["close_ts"]
    trade.exit_c = settle_c
    trade.exit_reason = "settled"
    trade.peak_c = peak_c
    trade.pnl_c = settle_c - entry_c
    return trade


def run_backtest(args, markets: dict, btc_map: dict,
                 candles_per_ticker: dict) -> dict:
    """Callable entry point. Returns dict with trades list and summary stats."""
    args._btc_map = btc_map
    trades: list[Trade] = []
    skipped_no_btc = 0
    skipped_no_market = 0
    missed = 0  # entry mode wait window expired without fill
    tickers_processed = 0

    entry_mode = getattr(args, "entry_mode", "immediate_ask")

    for ticker in sorted(candles_per_ticker.keys()):
        market = markets.get(ticker)
        if market is None:
            skipped_no_market += 1
            continue
        candles = candles_per_ticker[ticker]
        if len(candles) < 2:
            continue
        tickers_processed += 1
        if args.limit_tickers and tickers_processed > args.limit_tickers:
            break

        entered = False
        for i, c in enumerate(candles):
            if entered:
                break
            if c["vol"] < args.min_volume:
                continue
            yes_bid = c["yes_bid_c"]
            yes_ask = c["yes_ask_c"]
            if yes_bid <= 0 or yes_ask <= 0 or yes_ask >= 100:
                continue
            btc_now = _get_btc_at_ts(btc_map, c["ts"])
            if btc_now is None:
                skipped_no_btc += 1
                continue
            strike_dist_pct = (btc_now - market["strike"]) / market["strike"] * 100.0
            if abs(strike_dist_pct) > args.max_strike_dist_pct:
                continue

            if args.use_mid_fair:
                fair_yes = (yes_bid + yes_ask) / 2.0
            else:
                fair_yes = 50.0

            disc = _eval_atm_discount_entry(yes_bid, yes_ask, fair_yes, args)
            chosen = disc
            if chosen is None and args.bias_enabled:
                chosen = _eval_bias_entry(yes_bid, yes_ask, fair_yes,
                                          strike_dist_pct, args)
            if chosen is None:
                continue
            side, _ask_entry_c, edge_c, kind = chosen
            if args.side_filter != "both" and side != args.side_filter:
                continue

            resolved = _resolve_entry(side, entry_mode, c, candles, i, args)
            if resolved is None:
                # Codex: "If not filled, skip that ticker. Preserve one
                # trade per ticker." Consume the slot so we don't enter
                # later in the window via a different signal.
                missed += 1
                entered = True
                break
            entry_c, fill_idx = resolved

            trade = Trade(
                ticker=ticker, entry_ts=candles[fill_idx]["ts"],
                entry_c=entry_c,
                side=side, kind=kind,
                fair_at_entry_c=fair_yes if side == "yes" else (100.0 - fair_yes),
                edge_at_entry_c=edge_c, strike_dist_pct=strike_dist_pct,
                strike=market["strike"], btc_at_entry=btc_now,
            )
            trade = _close_trade(trade, candles, fill_idx, market, args)
            trades.append(trade)
            entered = True

    return {
        "trades": trades,
        "missed": missed,
        "skipped_no_btc": skipped_no_btc,
        "skipped_no_market": skipped_no_market,
    }


def kalshi_taker_fee_c(price_c: float) -> float:
    """Kalshi taker fee in cents per contract: 7 * p * (1-p) where p = price/100.
    Per Codex 2026-04-25: this is the right model. The previous flat 7%
    was over-conservative at low prices.
    Examples: 20c→1.12c, 35c→1.59c, 50c→1.75c (max), 80c→1.12c."""
    p = max(0.0, min(1.0, price_c / 100.0))
    return 7.0 * p * (1.0 - p)


def summarize(trades: list) -> dict:
    """Summary stats with Kalshi-accurate per-trade fee model.
    Net per trade = pnl - entry_fee - exit_fee, both at Kalshi taker rate."""
    if not trades:
        return {"n": 0, "wr": 0, "avg_c": 0, "total_c": 0,
                "avg_net_c": 0, "max_dd_c": 0, "total_net_c": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_c > 0)
    total = sum(t.pnl_c for t in trades)
    avg = total / n
    # Per-trade Kalshi taker fees on both legs
    nets = []
    for t in trades:
        fee = kalshi_taker_fee_c(t.entry_c) + kalshi_taker_fee_c(t.exit_c)
        nets.append(t.pnl_c - fee)
    total_net = sum(nets)
    avg_net = total_net / n
    avg_entry = sum(t.entry_c for t in trades) / n
    # Max drawdown across trade-by-trade cumulative (using net)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for net in nets:
        cum += net
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    return {
        "n": n,
        "wr": wins / n * 100,
        "avg_c": avg,
        "total_c": total,
        "avg_entry_c": avg_entry,
        "avg_net_c": avg_net,
        "total_net_c": total_net,
        "max_dd_c": max_dd,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-strike-dist-pct", type=float, default=0.02,
                    help="Max |BTC vs strike| pct for ATM entry")
    ap.add_argument("--stop-strike-dist-pct", type=float, default=0.06,
                    help="Strike escape stop distance pct")
    ap.add_argument("--max-entry-c", type=int, default=40,
                    help="Discount: max entry ask in cents")
    ap.add_argument("--min-fair-c", type=float, default=47.0,
                    help="Discount: min fair_yes_c floor")
    ap.add_argument("--min-edge-c", type=float, default=8.0,
                    help="Discount: min edge in cents")
    ap.add_argument("--bias-enabled", action="store_true",
                    help="Enable bias-side entries (loosens criteria)")
    ap.add_argument("--max-bias-entry-c", type=int, default=62,
                    help="Bias: max entry ask")
    ap.add_argument("--min-bias-edge-c", type=float, default=1.0,
                    help="Bias: min edge in cents")
    ap.add_argument("--target-c", type=int, default=49,
                    help="Exit: same-side bid reaches this")
    ap.add_argument("--profit-target-c", type=int, default=5,
                    help="Exit: same-side bid reaches entry + this")
    ap.add_argument("--stretch-profit-target-c", type=int, default=8,
                    help="(unused — kept for symmetry with live config)")
    ap.add_argument("--force-exit-age-s", type=int, default=840,
                    help="Force flatten at this age in seconds")
    ap.add_argument("--use-mid-fair", action="store_true",
                    help="Use current mid as fair instead of 50c (less robust)")
    ap.add_argument("--min-volume", type=float, default=10.0,
                    help="Skip candles with less than this volume_fp")
    ap.add_argument("--limit-tickers", type=int, default=0,
                    help="Cap analysis to N tickers (0 = all)")
    ap.add_argument("--side-filter", choices=["yes", "no", "both"], default="both",
                    help="Restrict to one side or both")
    # ── Entry mechanics (Codex 2026-04-25 entry-quality study) ──────────────
    # immediate_ask reproduces the original baseline; the wait modes are
    # experimental and do NOT replace the default. A/B path only.
    ap.add_argument("--entry-mode", choices=list(ENTRY_MODES),
                    default="immediate_ask",
                    help="Fill model: immediate_ask | maker_bid_wait | "
                         "pullback_wait")
    ap.add_argument("--entry-wait-s", type=int, default=60,
                    help="Wait window in seconds for maker_bid_wait / "
                         "pullback_wait (default 60)")
    ap.add_argument("--entry-pullback-c", type=int, default=2,
                    help="Cents below same-side ask for pullback_wait")
    # Fill-model switches (Codex 2026-04-25 follow-up). Orthogonal to mode.
    ap.add_argument("--entry-fill-source", choices=["quote", "trade"],
                    default="quote",
                    help="quote = ask_low/bid_high (strict, conservative); "
                         "trade = price_low/price_high (permissive, "
                         "assumes our resting order had queue priority)")
    ap.add_argument("--entry-include-signal-candle",
                    action="store_true",
                    help="Diagnostic only — include the signal candle's "
                         "intrabar OHLC in the fill scan. Conservative "
                         "default scans from signal_idx + 1 (no lookahead).")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: {DB} not found.")
        return 1

    conn = sqlite3.connect(str(DB))
    print("Loading markets...", end=" ", flush=True)
    markets = _load_markets(conn)
    print(f"{len(markets)}")
    print("Loading BTC 1m...", end=" ", flush=True)
    btc_map = _load_btc_at(conn)
    print(f"{len(btc_map)}")
    args._btc_map = btc_map
    print("Loading candles...", end=" ", flush=True)
    candles_per_ticker = _load_candles_per_ticker(conn)
    print(f"{sum(len(v) for v in candles_per_ticker.values())} rows across "
          f"{len(candles_per_ticker)} tickers")

    # Delegate trade generation to run_backtest so this CLI and the sweep
    # share one source of truth. main() is the human-facing report layer.
    result = run_backtest(args, markets, btc_map, candles_per_ticker)
    trades: list[Trade] = result["trades"]
    missed = result.get("missed", 0)
    skipped_no_btc = result.get("skipped_no_btc", 0)
    skipped_no_market = result.get("skipped_no_market", 0)

    print(f"\n=== Entry mode: {args.entry_mode} "
          f"(wait_s={args.entry_wait_s} pullback_c={args.entry_pullback_c}) ===")

    n = len(trades)
    if n == 0:
        print("\nNo trades generated.")
        print(f"Missed (wait expired): {missed}")
        return 0
    wins = sum(1 for t in trades if t.pnl_c > 0)
    total_pnl = sum(t.pnl_c for t in trades)
    avg = total_pnl / n
    summary = summarize(trades)
    print(f"\n--- Results ({n} trades, {missed} missed) ---")
    print(f"WR: {wins/n*100:.1f}%  ({wins}/{n})")
    print(f"Total P&L (gross): {total_pnl:+}c    Avg: {avg:+.2f}c/contract")
    print(f"Total P&L (net):   {summary['total_net_c']:+.1f}c    "
          f"Avg net: {summary['avg_net_c']:+.2f}c/contract")
    print(f"Max drawdown (net): {summary['max_dd_c']:.1f}c")
    print(f"Median peak: {sorted(t.peak_c for t in trades)[n//2]}c")

    print("\n-- By kind --")
    for kind in ("discount", "bias"):
        sub = [t for t in trades if t.kind == kind]
        if not sub:
            continue
        sub_n = len(sub)
        sub_wins = sum(1 for t in sub if t.pnl_c > 0)
        sub_pnl = sum(t.pnl_c for t in sub)
        print(f"  {kind:<10} n={sub_n:<4} WR={sub_wins/sub_n*100:>5.1f}%  "
              f"avg={sub_pnl/sub_n:+.2f}c  total={sub_pnl:+}c")

    print("\n-- By side --")
    for side in ("yes", "no"):
        sub = [t for t in trades if t.side == side]
        if not sub:
            continue
        sub_n = len(sub)
        sub_wins = sum(1 for t in sub if t.pnl_c > 0)
        sub_pnl = sum(t.pnl_c for t in sub)
        print(f"  {side.upper():<3} n={sub_n:<4} WR={sub_wins/sub_n*100:>5.1f}%  "
              f"avg={sub_pnl/sub_n:+.2f}c")

    print("\n-- By exit reason --")
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason].append(t.pnl_c)
    for reason, pnls in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        n_r = len(pnls)
        wins_r = sum(1 for p in pnls if p > 0)
        avg_r = sum(pnls) / n_r
        print(f"  {reason:<22} n={n_r:<4} WR={wins_r/n_r*100:>5.1f}%  avg={avg_r:+.2f}c")

    print(f"\n-- Skipped --")
    print(f"  no_btc: {skipped_no_btc}")
    print(f"  no_market: {skipped_no_market}")

    print("\nFee model is approximate. Net of taker fee (~7% of entry) is "
          "not subtracted from pnl above; rough net = avg - 0.07*entry_avg.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
