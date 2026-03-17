# validate_vs_pinescript.py — Compare Python engine output to Pine Script CSV export
#
# Usage:
#   python validate_vs_pinescript.py --input "../results-pinescript/03122026CRYPTO_BTCUSD, 1_3ff74.csv"
#
# The Pine Script exports per-1m-bar values. We feed those same candles into
# BiasEngine("15m") which replicates the Pine Script behavior:
#   - Indicators (EMA, RSI, Vol SMA) updated on every 1m bar
#   - 15m cycle tracking for cycleReturnPct and barsInCycle
#
# NOTE: Pine Script indicators are pre-warmed from full chart history.
# Our engine cold-starts at bar 0 of the CSV. Expect divergence in the first
# ~50 bars while EMAs/RSI converge, then values should track closely.

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent))

from bias_engine import BiasEngine
from models import Candle


def _parse_ts(ts_str: str) -> int:
    """Parse ISO datetime string to Unix ms."""
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    return int(dt.timestamp() * 1000)


def _load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Skip rows with missing OHLC
            if not row.get("close") or not row["close"].strip():
                continue
            rows.append(row)
    return rows


def run_validation(csv_path: str, warmup_bars: int = 50) -> None:
    rows = _load_csv(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    engine = BiasEngine("15m")

    errors_score: list[float] = []
    errors_bull: list[float] = []
    errors_bear: list[float] = []

    comparison: list[dict] = []

    for i, row in enumerate(rows):
        ts = _parse_ts(row["time"])

        # Volume may be missing in TradingView exports for some instruments
        vol_str = row.get("Volume", "").strip()
        volume = float(vol_str) if vol_str else 0.001  # fallback to near-zero (non-zero for SMA)

        candle = Candle(
            timestamp=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=volume,
            closed=True,
        )

        result = engine.update(candle)

        pine_score = float(row["Score"])
        pine_bull = float(row["Bull Confidence"])
        pine_bear = float(row["Bear Confidence"])

        err_score = result.score - pine_score
        err_bull = result.bull_conf - pine_bull
        err_bear = result.bear_conf - pine_bear

        comparison.append({
            "bar": i,
            "time": row["time"],
            "pine_score": pine_score,
            "py_score": result.score,
            "err_score": err_score,
            "pine_bull": pine_bull,
            "py_bull": result.bull_conf,
            "err_bull": err_bull,
            "pine_bear": pine_bear,
            "py_bear": result.bear_conf,
            "err_bear": err_bear,
            "bars_in_cycle": result.bars_in_cycle,
        })

        if i >= warmup_bars:
            errors_score.append(abs(err_score))
            errors_bull.append(abs(err_bull))
            errors_bear.append(abs(err_bear))

    # ── Summary ──────────────────────────────────────────────────────────────
    total = len(rows)
    post_warmup = total - warmup_bars

    print(f"\n{'='*65}")
    print(f"  VALIDATION RESULTS  (warmup: first {warmup_bars} bars excluded)")
    print(f"  Total bars: {total}   Post-warmup bars: {post_warmup}")
    print(f"{'='*65}")

    if errors_score:
        print(f"\n  Score (smoothed):")
        print(f"    MAE   : {sum(errors_score)/len(errors_score):.6f}")
        print(f"    Max   : {max(errors_score):.6f}")

        print(f"\n  Bull Confidence:")
        print(f"    MAE   : {sum(errors_bull)/len(errors_bull):.6f}")
        print(f"    Max   : {max(errors_bull):.6f}")

        print(f"\n  Bear Confidence:")
        print(f"    MAE   : {sum(errors_bear)/len(errors_bear):.6f}")
        print(f"    Max   : {max(errors_bear):.6f}")

        # Bars with significant divergence post-warmup
        threshold = 1.0
        divergent = [c for c in comparison[warmup_bars:] if abs(c["err_score"]) > threshold]
        print(f"\n  Bars with |score error| > {threshold}: {len(divergent)} / {post_warmup}")

    print(f"\n  First 5 bars (raw comparison):")
    print(f"  {'Bar':>4}  {'Pine Score':>12}  {'Py Score':>12}  {'Δ Score':>10}  {'Pine Bull':>10}  {'Py Bull':>10}")
    for c in comparison[:5]:
        print(f"  {c['bar']:>4}  {c['pine_score']:>12.4f}  {c['py_score']:>12.4f}  {c['err_score']:>+10.4f}  {c['pine_bull']:>10.4f}  {c['py_bull']:>10.4f}")

    print(f"\n  Mid-series (bars {warmup_bars}-{warmup_bars+4}):")
    print(f"  {'Bar':>4}  {'Pine Score':>12}  {'Py Score':>12}  {'Δ Score':>10}  {'Pine Bull':>10}  {'Py Bull':>10}")
    for c in comparison[warmup_bars:warmup_bars+5]:
        print(f"  {c['bar']:>4}  {c['pine_score']:>12.4f}  {c['py_score']:>12.4f}  {c['err_score']:>+10.4f}  {c['pine_bull']:>10.4f}  {c['py_bull']:>10.4f}")

    print(f"\n  Last 5 bars:")
    print(f"  {'Bar':>4}  {'Pine Score':>12}  {'Py Score':>12}  {'Δ Score':>10}  {'Pine Bull':>10}  {'Py Bull':>10}")
    for c in comparison[-5:]:
        print(f"  {c['bar']:>4}  {c['pine_score']:>12.4f}  {c['py_score']:>12.4f}  {c['err_score']:>+10.4f}  {c['pine_bull']:>10.4f}  {c['py_bull']:>10.4f}")

    # ── Volume warning ────────────────────────────────────────────────────────
    vol_missing = sum(1 for r in rows if not r.get("Volume", "").strip())
    if vol_missing > 0:
        print(f"\n  ⚠  WARNING: {vol_missing}/{total} bars have missing Volume.")
        print(f"     relVolClamped will be wrong for those bars (set to 1.0 as fallback).")
        print(f"     Re-export with Volume included for accurate validation.")

    print(f"\n{'='*65}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Python engine against Pine Script CSV export")
    parser.add_argument("--input", required=True, help="Path to Pine Script CSV export")
    parser.add_argument("--warmup", type=int, default=50, help="Bars to exclude from error metrics (default: 50)")
    args = parser.parse_args()
    run_validation(args.input, warmup_bars=args.warmup)


if __name__ == "__main__":
    main()
