"""Kalshi-truth-only monitor. Polls Kalshi directly, logs ONLY confirmed
account state changes. Bypasses the engine log entirely.

Run from project root:
    venv\\Scripts\\python.exe scripts\\kalshi_truth_monitor.py

Output is one line per real event:
    [HH:MM:SS] BAL $1615.45 (+$0.00)  POS{} ORDERS{}
    [HH:MM:SS] FILL: BUY YES KXBTC15M-26MAY011145-45 449x @ $0.55 (taker)
    [HH:MM:SS] POS+ KXBTC15M-26MAY011145-45 = 449
    [HH:MM:SS] BAL $1368.50 (-$246.95) — bought 449 @ 55c
    [HH:MM:SS] ORDER+ KXBTC...: sell 70x @ 78c (resting)

Designed to run alongside the engine — read-only, no orders placed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

# Allow running from project root
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT)

from kalshi_client import KalshiClient


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _short(ticker: str) -> str:
    """Trim KXBTC15M- prefix for readability."""
    if ticker.startswith("KXBTC15M-"):
        return ticker[len("KXBTC15M-"):]
    return ticker


def _load_env() -> None:
    """Load Kalshi credentials from credentials/kalshi.env."""
    env_path = os.path.join(_PROJECT, "credentials", "kalshi.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


async def _snapshot(c: KalshiClient) -> Dict[str, Any]:
    """Take a complete snapshot of relevant Kalshi state."""
    snap: Dict[str, Any] = {
        "balance_cents": 0,
        "portfolio_value_cents": 0,
        "positions": {},   # ticker -> count (signed)
        "orders": {},      # order_id -> dict
        "fills_seen": set(),
    }
    try:
        bal = await c.get_balance()
        snap["balance_cents"] = bal.balance
        snap["portfolio_value_cents"] = bal.portfolio_value
    except Exception as e:
        _log(f"!! balance fetch failed: {e}")

    try:
        pdata = await c._request(
            "GET", "/portfolio/positions", params={"limit": 100}
        )
        for p in pdata.get("market_positions", []):
            tk = p.get("ticker", "")
            pos = p.get("position")
            try:
                pos_int = int(float(pos)) if pos is not None else 0
            except Exception:
                pos_int = 0
            if pos_int != 0:
                snap["positions"][tk] = pos_int
    except Exception as e:
        _log(f"!! positions fetch failed: {e}")

    try:
        odata = await c._request(
            "GET", "/portfolio/orders",
            params={"status": "resting", "limit": 100},
        )
        for o in odata.get("orders", []):
            oid = o.get("order_id", "")
            if oid:
                snap["orders"][oid] = {
                    "ticker": o.get("ticker", ""),
                    "side": o.get("side", ""),
                    "action": o.get("action", ""),
                    "remaining": o.get("remaining_count"),
                    "yes_price": o.get("yes_price"),
                    "no_price": o.get("no_price"),
                }
    except Exception as e:
        _log(f"!! orders fetch failed: {e}")

    return snap


async def _recent_fills(c: KalshiClient, since_ts: float) -> list:
    """Fetch fills since a unix timestamp."""
    try:
        data = await c._request(
            "GET", "/portfolio/fills", params={"limit": 30}
        )
        out = []
        for f in data.get("fills", []):
            ct = f.get("created_time", "")
            try:
                # ISO 8601 with Z
                dt = datetime.strptime(ct, "%Y-%m-%dT%H:%M:%S.%fZ")
                ts = dt.timestamp()
            except Exception:
                continue
            if ts >= since_ts:
                out.append({
                    "fill_id": f.get("fill_id", ""),
                    "order_id": f.get("order_id", ""),
                    "ticker": f.get("ticker", ""),
                    "side": f.get("side", ""),
                    "action": f.get("action", ""),
                    "yes_price": f.get("yes_price_dollars", ""),
                    "no_price": f.get("no_price_dollars", ""),
                    "is_taker": f.get("is_taker", False),
                    "ts": ts,
                })
        return sorted(out, key=lambda x: x["ts"])
    except Exception as e:
        _log(f"!! fills fetch failed: {e}")
        return []


def _diff_log(prev: Dict[str, Any], curr: Dict[str, Any]) -> None:
    """Log every change between two snapshots."""
    # Balance changes
    bp = prev["balance_cents"]
    bc = curr["balance_cents"]
    if bp != bc:
        delta = (bc - bp) / 100.0
        sign = "+" if delta >= 0 else ""
        _log(
            f"BAL ${bc/100:.2f}  ({sign}${delta:.2f})  "
            f"port=${curr['portfolio_value_cents']/100:.2f}"
        )

    # Position changes
    prev_pos = prev["positions"]
    curr_pos = curr["positions"]
    for tk in sorted(set(list(prev_pos.keys()) + list(curr_pos.keys()))):
        before = prev_pos.get(tk, 0)
        after = curr_pos.get(tk, 0)
        if before != after:
            sign_after = "+" if after > 0 else ("-" if after < 0 else "")
            sign_change = "+" if (after - before) > 0 else ""
            _log(
                f"POS  {_short(tk)}  {before}  →  {sign_after}{abs(after) if after else 0}"
                f"  ({sign_change}{after - before})"
            )

    # Order changes
    prev_o = prev["orders"]
    curr_o = curr["orders"]
    new_oids = set(curr_o) - set(prev_o)
    gone_oids = set(prev_o) - set(curr_o)
    for oid in sorted(new_oids):
        d = curr_o[oid]
        px_field = "yes_price" if d.get("side") == "yes" else "no_price"
        px = d.get(px_field) or "?"
        _log(
            f"ORD+ {_short(d.get('ticker',''))}: "
            f"{d.get('action','?')} {d.get('side','?')} {d.get('remaining','?')}ct "
            f"@ {px}c  id={oid[:8]}"
        )
    for oid in sorted(gone_oids):
        d = prev_o[oid]
        _log(
            f"ORD- {_short(d.get('ticker',''))}: "
            f"{d.get('action','?')} {d.get('side','?')} {d.get('remaining','?')}ct gone "
            f"id={oid[:8]}"
        )


def _log_fill(f: Dict[str, Any]) -> None:
    side = f.get("side", "?")
    action = f.get("action", "?")
    px_y = f.get("yes_price", "")
    px_n = f.get("no_price", "")
    px_str = f"yes={px_y} no={px_n}"
    taker = "taker" if f.get("is_taker") else "maker"
    _log(
        f"FILL {action.upper()} {side.upper()} {_short(f.get('ticker',''))} "
        f"{px_str} ({taker})  id={f.get('order_id','?')[:8]}"
    )


async def main() -> None:
    _load_env()
    api_key = os.environ.get("KALSHI_API_KEY", "")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not api_key or not pem_path or not os.path.exists(pem_path):
        _log("!! missing credentials — set KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH")
        return
    pem = open(pem_path, "r").read()

    poll_s = float(os.environ.get("POLL_S", "2.0"))
    _log(f"START — polling every {poll_s}s, Ctrl+C to stop")

    async with KalshiClient(
        key_id=api_key, private_key_pem=pem, demo=False,
    ) as c:
        prev = await _snapshot(c)
        _log(
            f"INIT bal=${prev['balance_cents']/100:.2f} "
            f"port=${prev['portfolio_value_cents']/100:.2f} "
            f"pos={len(prev['positions'])} orders={len(prev['orders'])}"
        )
        for tk, ct in prev["positions"].items():
            _log(f"     POS  {_short(tk)} = {ct}")
        for oid, d in list(prev["orders"].items())[:10]:
            px_field = "yes_price" if d.get("side") == "yes" else "no_price"
            _log(
                f"     ORD  {_short(d.get('ticker',''))}: "
                f"{d.get('action','?')} {d.get('side','?')} {d.get('remaining','?')}ct "
                f"@ {d.get(px_field,'?')}c id={oid[:8]}"
            )

        last_fill_ts = time.time() - 60.0  # show last 60s of fills on first poll
        while True:
            await asyncio.sleep(poll_s)
            try:
                curr = await _snapshot(c)
            except Exception as e:
                _log(f"!! snapshot failed: {e}")
                continue
            # Log diffs first
            _diff_log(prev, curr)
            # Then any new fills since last poll
            try:
                fills = await _recent_fills(c, last_fill_ts)
                for f in fills:
                    _log_fill(f)
                    last_fill_ts = max(last_fill_ts, f["ts"] + 0.001)
            except Exception:
                pass
            prev = curr


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("STOP")
