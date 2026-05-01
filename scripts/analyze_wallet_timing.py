"""Extract a single wallet's entry-timing pattern from Polymarket trade history.

Re-runs the BTC-15m market discovery + trade fetch but filters every trade
to a single wallet of interest, then prints time-to-expiry distribution.

Usage:
    python scripts/analyze_wallet_timing.py 0x931bf4a0429f48b2 [--days 7]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

import aiohttp

POLY_GAMMA_API = "https://gamma-api.polymarket.com"
POLY_DATA_API = "https://data-api.polymarket.com"
WINDOW_S = 15 * 60
SLUG_PREFIX = "btc-updown-15m-"


def _market_close_ts(market: dict) -> int | None:
    for k in ("endDate", "closeTime"):
        v = market.get(k)
        if not v:
            continue
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            continue
    slug = market.get("slug", "")
    if slug.startswith(SLUG_PREFIX):
        try:
            return int(slug[len(SLUG_PREFIX):]) + WINDOW_S
        except ValueError:
            pass
    return None


async def discover(session, days: int) -> list[dict]:
    cutoff = time.time() - days * 86400
    out = []
    seen = set()
    offset = 0
    for _ in range(120):
        try:
            async with session.get(
                f"{POLY_GAMMA_API}/markets",
                params={"closed": "true", "limit": 500, "offset": offset,
                        "order": "endDate", "ascending": "false"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status != 200:
                    break
                data = await r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        page_recent = 0
        for m in data:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug", "") or ""
            if not slug.startswith(SLUG_PREFIX):
                continue
            cid = m.get("conditionId") or m.get("id")
            if not cid or cid in seen:
                continue
            ct = _market_close_ts(m)
            if ct is None or ct < cutoff:
                continue
            seen.add(cid)
            out.append(m)
            page_recent += 1
        if len(data) < 500:
            break
        offset += 500
        await asyncio.sleep(0.05)
    return out


async def fetch_trades(session, condition_id: str) -> list[dict]:
    out = []
    offset = 0
    for _ in range(20):
        try:
            async with session.get(
                f"{POLY_DATA_API}/trades",
                params={"market": condition_id, "limit": 500,
                        "offset": offset},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    break
                data = await r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 500:
            break
        offset += 500
        await asyncio.sleep(0.04)
    return out


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("wallet", help="Wallet address (0x...)")
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    target = args.wallet.lower()

    async with aiohttp.ClientSession() as s:
        markets = await discover(s, args.days)
        print(f"  Discovered {len(markets)} markets in last {args.days} days")

        sem = asyncio.Semaphore(10)
        wallet_trades = []
        analyzed = 0

        async def go(m):
            nonlocal analyzed
            async with sem:
                trades = await fetch_trades(s, m.get("conditionId") or "")
            analyzed += 1
            close_ts = _market_close_ts(m) or 0
            resolved = None
            try:
                prices = m.get("outcomePrices")
                outcomes = m.get("outcomes")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if (isinstance(prices, list) and isinstance(outcomes, list)
                        and len(prices) == len(outcomes)):
                    for n_, p_ in zip(outcomes, prices):
                        try:
                            if float(p_) >= 0.99:
                                resolved = (n_ or "").lower()
                                break
                        except ValueError:
                            continue
            except Exception:
                pass
            for t in trades:
                if (t.get("proxyWallet") or "").lower() != target:
                    continue
                if (t.get("side") or "").upper() != "BUY":
                    continue
                ts = int(t.get("timestamp") or 0)
                if ts == 0:
                    continue
                outcome = (t.get("outcome") or "").lower()
                won = (resolved is not None and outcome == resolved)
                wallet_trades.append({
                    "trade_ts": ts,
                    "close_ts": close_ts,
                    "secs_to_expiry": close_ts - ts,
                    "outcome": outcome,
                    "price": float(t.get("price") or 0),
                    "size": float(t.get("size") or 0),
                    "won": won,
                    "resolved": resolved,
                })

        await asyncio.gather(*(go(m) for m in markets))

    print(f"\n  {len(wallet_trades)} BUY trades by {args.wallet} in 15m markets")
    if not wallet_trades:
        return

    wallet_trades.sort(key=lambda x: x["secs_to_expiry"])

    # Bucket by minute-to-expiry
    print(f"\n  Time-to-expiry distribution (15m markets):")
    print(f"  {'min_to_exp':>12} {'n_trades':>10} {'wins':>6} {'WR':>6} "
          f"{'avg_price':>10} {'avg_size':>10} {'total_$':>10}")
    print(f"  {'-'*70}")
    buckets = [
        (0, 60,    "  0-1 min"),
        (60, 120,  "  1-2 min"),
        (120, 180, "  2-3 min"),
        (180, 300, "  3-5 min"),
        (300, 600, "  5-10 min"),
        (600, 900, " 10-15 min"),
        (900, 9999, "  15+ min"),
    ]
    for lo, hi, label in buckets:
        b = [t for t in wallet_trades if lo <= t["secs_to_expiry"] < hi]
        if not b:
            continue
        wins = sum(1 for t in b if t["won"])
        wr = wins / len(b) if b else 0
        avg_p = sum(t["price"] for t in b) / len(b)
        avg_s = sum(t["size"] for t in b) / len(b)
        tot = sum(t["price"] * t["size"] for t in b)
        print(f"  {label:>12} {len(b):>10} {wins:>6} {wr*100:>5.1f}% "
              f"{avg_p:>10.3f} {avg_s:>10.0f} ${tot:>9.0f}")

    # Show first 15 and last 5 for visual confirmation
    print(f"\n  Earliest 5 entries (closest to expiry):")
    for t in wallet_trades[:5]:
        print(f"    {t['secs_to_expiry']:>4}s to exp  {t['outcome']:<5}  "
              f"@ {t['price']:.3f}  size={t['size']:>6.0f}  won={t['won']}")

    print(f"\n  Latest 5 entries (furthest from expiry):")
    for t in wallet_trades[-5:]:
        print(f"    {t['secs_to_expiry']:>4}s to exp  {t['outcome']:<5}  "
              f"@ {t['price']:.3f}  size={t['size']:>6.0f}  won={t['won']}")


if __name__ == "__main__":
    asyncio.run(main())
