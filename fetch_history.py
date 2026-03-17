# fetch_history.py — Download 90 days of 1m BTC/USDT candles from Binance.US
#
# Usage:
#   python fetch_history.py
#
# Output: data/btc_1m_90d.csv
# Columns: timestamp,open,high,low,close,volume  (timestamp = Unix ms)
#
# The file is resumable: if it already exists and has >100,000 rows the
# script exits immediately without making any requests.

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Constants ─────────────────────────────────────────────────────────────────

BINANCE_US_KLINES = "https://api.binance.us/api/v3/klines"
SYMBOL            = "BTCUSDT"
INTERVAL          = "1m"
LIMIT             = 1000           # max rows per request (Binance US limit)
DAYS_BACK         = 90
OUTPUT_PATH       = Path("data/btc_1m_90d.csv")
PROGRESS_EVERY    = 10             # print progress every N requests
REQUEST_SLEEP     = 0.1            # seconds between requests (be polite)
RESUME_ROW_THRESH = 100_000        # skip fetch if file already has this many rows


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_csv_rows(path: Path) -> int:
    """Count data rows (excluding header) in a CSV file without loading it all."""
    with open(path, "r", newline="") as f:
        # Subtract 1 for the header row
        return sum(1 for _ in f) - 1


def _fetch_klines(start_ms: int) -> list[list]:
    """Fetch up to LIMIT klines starting from start_ms (inclusive).

    Returns a list of raw kline arrays from the Binance API.
    Each kline: [open_time, open, high, low, close, volume, ...]
    """
    params = {
        "symbol":    SYMBOL,
        "interval":  INTERVAL,
        "limit":     LIMIT,
        "startTime": start_ms,
    }
    resp = requests.get(BINANCE_US_KLINES, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── Resume check ─────────────────────────────────────────────────────────
    if OUTPUT_PATH.exists():
        row_count = _count_csv_rows(OUTPUT_PATH)
        if row_count > RESUME_ROW_THRESH:
            print(
                f"[fetch_history] File already exists with {row_count:,} rows "
                f"(>{RESUME_ROW_THRESH:,}). Skipping download."
            )
            return
        else:
            print(
                f"[fetch_history] Existing file has {row_count:,} rows — below threshold, "
                f"re-fetching from scratch."
            )

    # ── Time range ───────────────────────────────────────────────────────────
    now_ms    = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms  = now_ms - (DAYS_BACK * 24 * 60 * 60 * 1000)  # 90 days ago

    print(
        f"[fetch_history] Fetching {DAYS_BACK}-day 1m candles for {SYMBOL} "
        f"starting {datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    # ── Output setup ─────────────────────────────────────────────────────────
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    total_rows   = 0
    request_num  = 0
    current_ms   = start_ms

    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])

        while current_ms < now_ms:
            # ── Fetch one page ────────────────────────────────────────────────
            try:
                klines = _fetch_klines(current_ms)
            except requests.RequestException as exc:
                print(f"[fetch_history] Request error at ts={current_ms}: {exc}")
                raise

            if not klines:
                # No more data available (reached present)
                break

            # ── Write rows ────────────────────────────────────────────────────
            for kline in klines:
                open_time = int(kline[0])   # Unix ms
                # Only write candles within the intended range
                if open_time >= now_ms:
                    break
                writer.writerow([
                    open_time,
                    float(kline[1]),   # open
                    float(kline[2]),   # high
                    float(kline[3]),   # low
                    float(kline[4]),   # close
                    float(kline[5]),   # volume
                ])
                total_rows += 1

            request_num += 1

            # Progress report
            if request_num % PROGRESS_EVERY == 0:
                last_ts = int(klines[-1][0])
                pct = (last_ts - start_ms) / max(now_ms - start_ms, 1) * 100.0
                dt_str = datetime.fromtimestamp(
                    last_ts / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                print(
                    f"[fetch_history] Request {request_num:4d} — "
                    f"{total_rows:>8,} rows  |  up to {dt_str}  ({pct:.1f}%)"
                )

            # Advance cursor past the last returned candle
            last_open_time = int(klines[-1][0])
            next_ms = last_open_time + 60_000  # 1-minute step

            if next_ms <= current_ms:
                # Guard against infinite loop if API returns unexpected data
                print("[fetch_history] Warning: cursor did not advance — stopping.")
                break

            current_ms = next_ms

            # Be polite to the API
            time.sleep(REQUEST_SLEEP)

    print(
        f"[fetch_history] Done. Wrote {total_rows:,} rows to {OUTPUT_PATH}  "
        f"({request_num} requests)"
    )


if __name__ == "__main__":
    main()
