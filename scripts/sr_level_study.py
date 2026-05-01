#!/usr/bin/env python
"""Phase B falsification study — do round-number price levels actually hold?

Reads `window_snapshots` from data/trades.db. For each window, walks the
per-minute kalshi_mid_cents sequence and identifies "touches" (mid visits
a price, then departs ≥2c). For each touch, checks whether the following
window slice returned to within ±1c of the same price (bounce) or broke
through (continued in the departure direction).

Buckets touches by last digit:
  round:   ends in 0 or 5
  non-round: ends in 1-4 or 6-9

Reports bounce rate per bucket, per regime, per distance-from-50c.

Decision gate: if round-level bounce rate > non-round by >=5pp in CHOP
regime with n>=30 touches, the S/R detector build is justified.
Otherwise, rethink.

Usage: python scripts/sr_level_study.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ENGINE_DIR / "data" / "trades.db"


def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%"


def extract_touches(mids: list[int], regimes: list[str]) -> list[dict]:
    """Walk the mid sequence; emit one 'touch' dict per visited price.

    A touch is: price P observed, then price moves at least 2c away from P
    (in either direction). The 'bounce' label is True iff within the next
    12 minutes (12 samples, since snapshots are ~1/min) the price returns
    to within ±1c of P *without* first moving ≥3c in the break direction.

    regimes[i] is the regime at sample i; the touch inherits regimes[i].
    """
    touches = []
    n = len(mids)
    if n < 4:
        return touches
    i = 0
    while i < n - 2:
        p = mids[i]
        if p is None or p <= 0:
            i += 1
            continue
        # Find next point that's ≥2c away
        j = i + 1
        while j < n and mids[j] is not None and abs(mids[j] - p) < 2:
            j += 1
        if j >= n or mids[j] is None:
            break
        departure_dir = 1 if mids[j] > p else -1
        # Walk forward up to 12 samples from j. Classify:
        #   bounce: price returns to within ±1c of p
        #   break: price moves ≥3c further in departure_dir from its j-value
        bounce = False
        broke = False
        for k in range(j, min(j + 12, n)):
            if mids[k] is None:
                continue
            if abs(mids[k] - p) <= 1:
                bounce = True
                break
            if departure_dir * (mids[k] - p) >= 3:  # continued in break dir
                broke = True
                break
        if bounce or broke:
            touches.append({
                "price": p,
                "regime": regimes[i] or "unknown",
                "bounce": bounce,
                "dir": departure_dir,
            })
        i = j
    return touches


def last_digit(p: int) -> int:
    return p % 10


def bucket(p: int) -> str:
    return "round" if last_digit(p) in (0, 5) else "other"


def dist_from_50(p: int) -> str:
    d = abs(p - 50)
    if d <= 5:
        return "0-5"
    if d <= 10:
        return "6-10"
    if d <= 20:
        return "11-20"
    return ">20"


def summarize(touches: list[dict], key_fn) -> list[tuple]:
    buckets = defaultdict(lambda: {"n": 0, "bounces": 0})
    for t in touches:
        b = buckets[key_fn(t)]
        b["n"] += 1
        if t["bounce"]:
            b["bounces"] += 1
    rows = []
    for k in sorted(buckets.keys(), key=str):
        bn = buckets[k]["n"]
        bb = buckets[k]["bounces"]
        rows.append((str(k), bn, bb, bb / bn if bn else 0.0))
    return rows


def print_table(title: str, rows: list[tuple]) -> None:
    print(f"\n-- {title} --")
    print("  key                  n     bounces  bounce%")
    print("  " + "-" * 48)
    for k, n, b, r in rows:
        print(f"  {k:<20} {n:5d}   {b:5d}   {fmt_pct(r)}")


def main():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    # Pull per-window sequences of (t_offset, mid, regime)
    cur = conn.execute(
        "SELECT ticker, t_offset_sec, kalshi_mid_cents, regime "
        "FROM window_snapshots "
        "WHERE kalshi_mid_cents IS NOT NULL "
        "ORDER BY ticker, t_offset_sec"
    )
    windows = defaultdict(list)
    for ticker, t_off, mid, regime in cur.fetchall():
        windows[ticker].append((int(t_off or 0), int(mid), regime or "unknown"))

    all_touches: list[dict] = []
    for tkr, rows in windows.items():
        rows.sort(key=lambda r: r[0])
        mids = [r[1] for r in rows]
        regimes = [r[2] for r in rows]
        all_touches.extend(extract_touches(mids, regimes))

    n_windows = len(windows)
    n_touches = len(all_touches)
    print("== S/R falsification study ==")
    print(f"   DB:        {DB_PATH}")
    print(f"   windows:   {n_windows}")
    print(f"   touches:   {n_touches}")
    if n_touches == 0:
        print("   insufficient data — need more window_snapshots history")
        return

    # By round vs other (overall)
    print_table(
        "bucket: round (last digit 0 or 5) vs other — OVERALL",
        summarize(all_touches, lambda t: bucket(t["price"])),
    )

    # By bucket × regime
    print_table(
        "bucket × regime",
        summarize(all_touches,
                  lambda t: f"{bucket(t['price'])}/{t['regime']}"),
    )

    # By distance-from-50c
    print_table(
        "distance from 50c",
        summarize(all_touches, lambda t: dist_from_50(t["price"])),
    )

    # Decision gate
    chop_round = [t for t in all_touches
                  if t["regime"] == "chop" and bucket(t["price"]) == "round"]
    chop_other = [t for t in all_touches
                  if t["regime"] == "chop" and bucket(t["price"]) == "other"]
    r_round = (sum(1 for t in chop_round if t["bounce"]) / len(chop_round)
               if chop_round else 0.0)
    r_other = (sum(1 for t in chop_other if t["bounce"]) / len(chop_other)
               if chop_other else 0.0)
    delta = (r_round - r_other) * 100
    print()
    print("== DECISION GATE (Phase C ship criterion) ==")
    print(f"   CHOP + round:   n={len(chop_round):3d}  bounce%={fmt_pct(r_round)}")
    print(f"   CHOP + other:   n={len(chop_other):3d}  bounce%={fmt_pct(r_other)}")
    print(f"   delta:          {delta:+.1f}pp   (need >=+5pp with n>=30 round)")
    if len(chop_round) >= 30 and delta >= 5.0:
        print("   VERDICT:        SHIP the S/R detector (Phase C).")
    elif len(chop_round) < 30:
        print("   VERDICT:        INSUFFICIENT SAMPLE — run engine more, re-run study.")
    else:
        print("   VERDICT:        DO NOT SHIP — round-level bounce rate not significantly higher.")


if __name__ == "__main__":
    main()
