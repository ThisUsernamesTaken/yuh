"""Pull last 24h of Kalshi fills (truth only, no DB) and summarize by PT hour.

Goal: compare today's quiet hours vs last night's $1k-in-2-hours run
to figure out what changed in sizing / frequency / hit-rate.

Usage:
    venv\\Scripts\\python.exe scripts\\kalshi_session_replay.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT)

# Load creds
env_path = os.path.join(_PROJECT, "credentials", "kalshi.env")
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

from kalshi_client import KalshiClient

PDT = timezone(timedelta(hours=-7))


async def main() -> None:
    pem = open(os.environ["KALSHI_PRIVATE_KEY_PATH"], "r").read()
    async with KalshiClient(
        key_id=os.environ["KALSHI_API_KEY"],
        private_key_pem=pem,
        demo=False,
    ) as c:
        all_fills = []
        cursor = ""
        for _ in range(50):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await c._request("GET", "/portfolio/fills", params=params)
            fills = data.get("fills", [])
            if not fills:
                break
            all_fills.extend(fills)
            cursor = data.get("cursor", "")
            if not cursor:
                break
        print(f"Fetched {len(all_fills)} fills total")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        buckets = {}
        for f in all_fills:
            ct = f.get("created_time", "")
            try:
                dt = datetime.strptime(
                    ct, "%Y-%m-%dT%H:%M:%S.%fZ"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if dt < cutoff:
                continue
            pt = dt.astimezone(PDT)
            hkey = pt.strftime("%Y-%m-%d %H:00 PT")
            b = buckets.setdefault(hkey, {
                "buy_n": 0,
                "sell_n": 0,
                "buy_d": 0.0,
                "sell_d": 0.0,
                "tickers": set(),
                "fills": 0,
            })
            ticker = f.get("ticker", "")
            b["tickers"].add(ticker)
            b["fills"] += 1
            action = f.get("action", "")
            side = f.get("side", "")
            yp = float(f.get("yes_price_dollars", "0") or 0)
            np_ = float(f.get("no_price_dollars", "0") or 0)
            px = yp if side == "yes" else np_
            cf = f.get("count_fp", "0")
            try:
                ct_n = float(cf or 0)
            except Exception:
                ct_n = 0
            if action == "buy":
                b["buy_n"] += ct_n
                b["buy_d"] += ct_n * px
            else:
                b["sell_n"] += ct_n
                b["sell_d"] += ct_n * px

        # Header
        h_hour = "PT hour"
        h_fills = "fills"
        h_buys = "buy ct"
        h_buy_d = "buy $"
        h_sells = "sell ct"
        h_sell_d = "sell $"
        h_net = "net $"
        h_tkrs = "tkrs"
        print(
            f"{h_hour:<22} {h_fills:>6} {h_buys:>8} {h_buy_d:>10} "
            f"{h_sells:>8} {h_sell_d:>10} {h_net:>10} {h_tkrs:>5}"
        )
        print("-" * 90)

        running = 0.0
        for hkey in sorted(buckets.keys()):
            b = buckets[hkey]
            net = b["sell_d"] - b["buy_d"]
            running += net
            print(
                f"{hkey:<22} {b['fills']:>6} {b['buy_n']:>8.0f} {b['buy_d']:>10.2f} "
                f"{b['sell_n']:>8.0f} {b['sell_d']:>10.2f} {net:>+10.2f} {len(b['tickers']):>5}"
            )
        print("-" * 90)
        print(f"24h running net: ${running:+.2f}")


asyncio.run(main())
