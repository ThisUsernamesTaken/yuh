# backtest.py — Offline backtester for validating Python output against Pine Script
#
# Usage:
#   python backtest.py --input data/btc_1m_candles.csv --output data/backtest_results.csv

import argparse
import csv
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from aggregator import CandleAggregator
from bias_engine import BiasEngine
from config import (
    TF_WEIGHTS, ENTRY_THRESHOLD, PAYOUT_ON_WIN, LOSS_ON_LOSE,
    MAX_PCT_EQUITY, STAKE_CAP, STARTING_EQUITY,
)
from consensus import ConsensusLayer
from models import Candle, BiasResult, ConsensusSignal, TradeRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


def _load_candles(csv_path: str) -> list[Candle]:
    """Load 1m candles from a CSV file.

    Expected columns (case-insensitive): timestamp, open, high, low, close, volume
    Timestamp may be Unix ms or a datetime string.
    """
    candles: list[Candle] = []
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {csv_path}")

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        headers = [h.lower().strip() for h in (reader.fieldnames or [])]

        for row in reader:
            row_lower = {k.lower().strip(): v for k, v in row.items()}

            ts_raw = row_lower.get("timestamp") or row_lower.get("time") or row_lower.get("open_time")
            if ts_raw is None:
                continue
            try:
                ts = int(ts_raw)
                # Auto-detect seconds vs ms
                if ts < 1e12:
                    ts *= 1000
            except ValueError:
                # Try parsing as datetime string
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    ts = int(dt.timestamp() * 1000)
                except ValueError:
                    logger.warning("Cannot parse timestamp: %s", ts_raw)
                    continue

            candles.append(Candle(
                timestamp=ts,
                open=float(row_lower["open"]),
                high=float(row_lower["high"]),
                low=float(row_lower["low"]),
                close=float(row_lower["close"]),
                volume=float(row_lower["volume"]),
                closed=True,
            ))

    candles.sort(key=lambda c: c.timestamp)
    logger.info("Loaded %d candles from %s", len(candles), csv_path)
    return candles


def run_backtest(candles: list[Candle]) -> tuple[list[dict], dict]:
    """Run the full bias engine backtest over historical 1m candles.

    Returns:
        (trade_records_as_dicts, summary_stats)
    """
    bias_engines: dict[str, BiasEngine] = {tf: BiasEngine(tf) for tf in TF_WEIGHTS}
    aggregators: dict[str, CandleAggregator] = {
        tf: CandleAggregator(tf) for tf in TF_WEIGHTS if tf != "1m"
    }
    consensus = ConsensusLayer()
    latest_results: dict[str, BiasResult] = {}

    # Track pending trade (decided in current cycle, resolved at cycle close)
    pending_signal: ConsensusSignal | None = None
    pending_cycle_open: float | None = None
    pending_entry_time: int | None = None

    # Equity and counters
    equity = STARTING_EQUITY
    trades: list[dict] = []
    trade_id = 1

    # Stats
    wins = losses = 0
    bucket_wins = [0, 0, 0, 0]
    bucket_trades = [0, 0, 0, 0]

    prev_cycle_ts: int | None = None

    for candle in candles:
        # Compute cycle timestamp for 15m
        cycle_ms = 15 * 60 * 1000
        cycle_ts = (candle.timestamp // cycle_ms) * cycle_ms
        new_cycle = cycle_ts != prev_cycle_ts

        # --- Resolve previous cycle trade at cycle boundary ---
        if new_cycle and prev_cycle_ts is not None and pending_signal is not None:
            sig = pending_signal
            cyc_open = pending_cycle_open
            cyc_close = candle.open  # The open of the new cycle = close of the last bar of previous

            conf_frac = sig.confidence / 100.0
            raw_stake = (STARTING_EQUITY * (MAX_PCT_EQUITY / 100.0)) * conf_frac
            stake = _clamp(raw_stake, 0.0, STAKE_CAP)

            if sig.direction == "CALL":
                is_win = cyc_close > cyc_open
            else:
                is_win = cyc_close < cyc_open

            pnl = (stake * PAYOUT_ON_WIN) if is_win else (-stake * LOSS_ON_LOSE)
            equity += pnl

            wins += is_win
            losses += (not is_win)
            bkt = sig.bucket
            bucket_trades[bkt] += 1
            bucket_wins[bkt] += is_win

            # Get component values from the 15m result if available
            r15 = sig.tf_results.get("15m")
            record = {
                "trade_id": trade_id,
                "side": sig.direction,
                "confidence": sig.confidence,
                "bucket": bkt,
                "entry_time": pending_entry_time,
                "resolve_time": candle.timestamp,
                "cycle_open": cyc_open,
                "cycle_close": cyc_close,
                "is_win": int(is_win),
                "stake": stake,
                "pnl": pnl,
                "equity_after": equity,
                "fourier_score": sig.fourier_score,
                "aligned_count": sig.aligned_count,
                "score": r15.score if r15 else None,
                "bull_conf": r15.bull_conf if r15 else None,
                "bear_conf": r15.bear_conf if r15 else None,
                "cycle_return_pct": r15.cycle_return_pct if r15 else None,
                "ema_spread_pct": r15.ema_spread_pct if r15 else None,
                "rsi_val": None,  # RSI bias stored, raw val not in BiasResult
                "rel_vol": r15.rel_vol_clamped if r15 else None,
                "candle_pressure": r15.candle_pressure if r15 else None,
            }
            trades.append(record)
            trade_id += 1
            pending_signal = None
            pending_cycle_open = None
            pending_entry_time = None

        if new_cycle:
            prev_cycle_ts = cycle_ts

        # --- Update indicators ---
        result_1m = bias_engines["1m"].update(candle)
        latest_results["1m"] = result_1m

        for tf, agg in aggregators.items():
            completed = agg.update(candle)
            if completed is not None:
                result = bias_engines[tf].update(completed)
                latest_results[tf] = result

        if len(latest_results) < len(TF_WEIGHTS):
            continue

        signal = consensus.compute(latest_results, candle.timestamp)

        # Track best signal for this cycle (highest confidence above threshold)
        if (
            signal.confidence >= ENTRY_THRESHOLD
            and signal.direction != "NONE"
            and (pending_signal is None or signal.confidence > pending_signal.confidence)
        ):
            pending_signal = signal
            if pending_cycle_open is None:
                pending_cycle_open = candle.close  # proxy for cycle open at decision time
            pending_entry_time = candle.timestamp

    # Summary stats
    total = wins + losses
    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total * 100) if total > 0 else 0.0,
        "final_equity": equity,
        "pnl": equity - STARTING_EQUITY,
        "bucket_0_25": {
            "trades": bucket_trades[0],
            "wins": bucket_wins[0],
            "win_rate": (bucket_wins[0] / bucket_trades[0] * 100) if bucket_trades[0] > 0 else 0.0,
        },
        "bucket_25_50": {
            "trades": bucket_trades[1],
            "wins": bucket_wins[1],
            "win_rate": (bucket_wins[1] / bucket_trades[1] * 100) if bucket_trades[1] > 0 else 0.0,
        },
        "bucket_50_75": {
            "trades": bucket_trades[2],
            "wins": bucket_wins[2],
            "win_rate": (bucket_wins[2] / bucket_trades[2] * 100) if bucket_trades[2] > 0 else 0.0,
        },
        "bucket_75_100": {
            "trades": bucket_trades[3],
            "wins": bucket_wins[3],
            "win_rate": (bucket_wins[3] / bucket_trades[3] * 100) if bucket_trades[3] > 0 else 0.0,
        },
    }
    return trades, summary


def _save_results(trades: list[dict], output_path: str) -> None:
    if not trades:
        logger.info("No trades to save.")
        return
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    logger.info("Saved %d trade records to %s", len(trades), output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Bias Engine Backtester")
    parser.add_argument("--input", required=True, help="Path to 1m OHLCV CSV file")
    parser.add_argument("--output", default="data/backtest_results.csv", help="Output CSV path")
    args = parser.parse_args()

    candles = _load_candles(args.input)
    trades, summary = run_backtest(candles)
    _save_results(trades, args.output)

    print("\n=== BACKTEST SUMMARY ===")
    print(f"Total Trades : {summary['total_trades']}")
    print(f"Wins         : {summary['wins']}")
    print(f"Losses       : {summary['losses']}")
    print(f"Win Rate     : {summary['win_rate']:.2f}%")
    print(f"Final Equity : ${summary['final_equity']:.2f}")
    print(f"PnL          : ${summary['pnl']:.2f}")
    print(f"\nBucket Performance:")
    for bkt, key in [(0, "bucket_0_25"), (1, "bucket_25_50"), (2, "bucket_50_75"), (3, "bucket_75_100")]:
        b = summary[key]
        label = ["0-25", "25-50", "50-75", "75-100"][bkt]
        print(f"  {label:>7s}%: {b['trades']:>5d} trades, {b['win_rate']:.2f}% WR")


if __name__ == "__main__":
    main()
