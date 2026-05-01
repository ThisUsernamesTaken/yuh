"""Identify Polymarket BTC-15m wallets that buy in the final minutes
of a session and have >=95% win rate.

These are the *exact population* we want to copy: wallets that consistently
take the right side in the last few minutes of a BTC up/down market, when
most directional information is already embedded in the order book but a
last-minute BTC tick can still flip the outcome.

Design notes
------------
- Standalone script. Does NOT touch the live engine's state. Reads
  Polymarket public APIs (Gamma + Data) only. No Kalshi calls.
- Concurrent fetches via asyncio.Semaphore for speed (10 parallel).
- The scoring logic is INVERTED relative to the engine's
  `_analyze_settled_market` function: we INCLUDE winners who bought at
  >85c (those are *exactly* the late-window high-conviction trades we
  want to identify), and we filter to trades within the last N minutes
  of the window.

Usage
-----
    python scripts/identify_terminal_wallets.py
    python scripts/identify_terminal_wallets.py --days 90 --last-min 5 --min-wr 0.95 --min-trades 10
    python scripts/identify_terminal_wallets.py --days 365 --last-min 3 --min-wr 0.95 --min-trades 20

Output
------
- Pretty-prints top N candidates to terminal
- Writes data/terminal_wallets.json with the full ranked list
- Writes data/terminal_wallets_raw.json with per-wallet trade detail

The output file is then consumed by the (forthcoming) terminal_copy.py
module which monitors live Polymarket trades during minute 12-15 of each
Kalshi window and mirrors fills from these wallets.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp


# -- Constants ----------------------------------------------------------------

POLY_GAMMA_API = "https://gamma-api.polymarket.com"
POLY_DATA_API = "https://data-api.polymarket.com"

WINDOW_S = 15 * 60          # 15-min BTC up/down markets
SLUG_PREFIX = "btc-updown-15m-"
# Note: Polymarket has since retired the 15m product in favor of 5m
# rolling markets. Historical 15m markets still exist in the API, just
# no new ones are being created. This scanner identifies wallets that
# performed well on the 15m product (= same time horizon as our Kalshi
# KXBTC15M engine). Those wallets often migrated to the current 5m
# product, so the live monitor (forthcoming) will track them on
# `btc-updown-5m-{ts}` markets whose settlement aligns with our
# Kalshi 15m window expiry.

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data"
OUT_FILE = OUT_DIR / "terminal_wallets.json"
RAW_FILE = OUT_DIR / "terminal_wallets_raw.json"

# Existing engine blacklist — always exclude these
WALLET_BLACKLIST_PREFIXES = {
    "0x546a516629543",
    "0x47933b7114140",
    "0x7b4c7140dfd37",
    "0x488152f356e95",
}


def _is_blacklisted(addr: str) -> bool:
    return any(addr.startswith(p) for p in WALLET_BLACKLIST_PREFIXES)


def _classify_outcome(name: str) -> str | None:
    """Map a Polymarket outcome name to 'up' or 'down'."""
    n = (name or "").lower().strip()
    if n in ("up", "yes", "above"):
        return "up"
    if n in ("down", "no", "below"):
        return "down"
    return None


def _resolved_outcome(market: dict) -> str | None:
    """Determine which side won a settled market, returning 'up' or 'down'."""
    if not market.get("closed"):
        return None
    # Polymarket markets carry the resolved outcomes in `outcomePrices`
    # (a JSON-encoded list of "1.0" / "0.0" strings) or in resolved
    # outcome fields. Try multiple shapes for compatibility.
    try:
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        outcomes = market.get("outcomes")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if (isinstance(prices, list) and isinstance(outcomes, list)
                and len(prices) == len(outcomes)):
            for name, price_str in zip(outcomes, prices):
                try:
                    if float(price_str) >= 0.99:
                        return _classify_outcome(name)
                except ValueError:
                    continue
    except Exception:
        pass
    return None


# -- Market discovery --------------------------------------------------------

async def discover_markets(session: aiohttp.ClientSession,
                           days: int) -> list[dict]:
    """Find settled BTC 15m markets via bulk paginated /markets API.

    Uses the Gamma `/markets` endpoint with `closed=true` and pagination
    to enumerate all closed BTC 15m markets within the time horizon.
    More reliable than deterministic slug-walking because Polymarket's
    timestamp boundaries don't always align with `now // window_s` math.
    """
    cutoff_ts = time.time() - days * 86400
    found: list[dict] = []
    seen_ids: set = set()
    offset = 0
    page_size = 500
    pages_scanned = 0
    consecutive_empty_pages = 0

    print(f"  Paginating /markets (closed=true) for last {days} days...",
          file=sys.stderr)

    while True:
        try:
            async with session.get(
                f"{POLY_GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "limit": page_size,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    print(f"    API returned {resp.status}, stopping",
                          file=sys.stderr)
                    break
                data = await resp.json()
        except Exception as e:
            print(f"    fetch error: {e}", file=sys.stderr)
            break
        pages_scanned += 1

        if not isinstance(data, list) or not data:
            consecutive_empty_pages += 1
            if consecutive_empty_pages >= 3:
                break
            offset += page_size
            await asyncio.sleep(0.2)
            continue
        consecutive_empty_pages = 0

        page_btc_15m = 0
        oldest_in_page = None
        for m in data:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug", "") or ""
            if not slug.startswith(SLUG_PREFIX):
                continue
            cid = m.get("conditionId") or m.get("id")
            if not cid or cid in seen_ids:
                continue
            close_ts = _market_close_ts(m)
            if close_ts is None:
                continue
            if close_ts < cutoff_ts:
                # Past horizon — keep tracking oldest to know when to stop
                if oldest_in_page is None or close_ts < oldest_in_page:
                    oldest_in_page = close_ts
                continue
            seen_ids.add(cid)
            found.append(m)
            page_btc_15m += 1

        if pages_scanned % 5 == 1:
            print(f"    page {pages_scanned}: scanned {len(data)}, "
                  f"15m kept {page_btc_15m}, total found {len(found)}, "
                  f"offset {offset}", file=sys.stderr)

        # Stop if oldest market in this page is past our horizon
        if oldest_in_page is not None and oldest_in_page < cutoff_ts:
            # check if all 15m markets in page are past cutoff → stop
            page_15m_recent = sum(
                1 for m in data
                if isinstance(m, dict)
                and (m.get("slug", "") or "").startswith(SLUG_PREFIX)
                and (_market_close_ts(m) or 0) >= cutoff_ts
            )
            if page_15m_recent == 0:
                print(f"    reached horizon (oldest 15m before cutoff)",
                      file=sys.stderr)
                break

        if len(data) < page_size:
            break
        offset += page_size
        await asyncio.sleep(0.1)

    print(f"  Discovered {len(found)} settled BTC-15m markets in last "
          f"{days} days ({pages_scanned} API pages scanned)",
          file=sys.stderr)
    return found


# -- Trade fetch --------------------------------------------------------------

async def fetch_trades(session: aiohttp.ClientSession,
                       condition_id: str) -> list[dict]:
    """Fetch all trades for a market, paginated."""
    all_trades: list[dict] = []
    offset = 0
    for _ in range(20):  # max 10k trades / market
        try:
            async with session.get(
                f"{POLY_DATA_API}/trades",
                params={"market": condition_id, "limit": 500,
                        "offset": offset},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    break
                trades = await resp.json()
        except Exception:
            break
        if not isinstance(trades, list) or not trades:
            break
        all_trades.extend(trades)
        if len(trades) < 500:
            break
        offset += 500
        await asyncio.sleep(0.05)
    return all_trades


# -- Trade filtering & wallet stats -------------------------------------------

def _trade_ts(trade: dict) -> int | None:
    """Best-effort extraction of trade timestamp (unix seconds)."""
    for k in ("timestamp", "createdAt", "transactionTimestamp"):
        v = trade.get(k)
        if v is None:
            continue
        try:
            n = int(v)
            # Heuristic: accept seconds (10 digits) or milliseconds (13)
            if n > 10 ** 12:
                return n // 1000
            return n
        except (ValueError, TypeError):
            continue
    return None


def _market_close_ts(market: dict) -> int | None:
    """Extract market close time as unix seconds."""
    for k in ("endDate", "closeTime", "endDateIso"):
        v = market.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, str):
                # ISO 8601
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return int(dt.timestamp())
            return int(v)
        except Exception:
            continue
    # Fallback: derive from slug `btc-updown-15m-<start_ts>` + 15 min
    slug = market.get("slug", "")
    if slug.startswith(SLUG_PREFIX):
        try:
            start_ts = int(slug[len(SLUG_PREFIX):])
            return start_ts + WINDOW_S
        except ValueError:
            pass
    return None


async def analyze_market(session: aiohttp.ClientSession,
                          market: dict, last_min: int,
                          wallet_stats: dict) -> int:
    """Analyze a single market's trades. Returns count of qualifying trades."""
    cid = market.get("conditionId") or market.get("id") or ""
    if not cid:
        return 0
    resolved = _resolved_outcome(market)
    if resolved is None:
        return 0
    close_ts = _market_close_ts(market)
    if close_ts is None:
        return 0

    cutoff_ts = close_ts - (last_min * 60)
    qualifying = 0

    trades = await fetch_trades(session, cid)
    for t in trades:
        if (t.get("side") or "").upper() != "BUY":
            continue

        wallet = t.get("proxyWallet") or t.get("user") or ""
        if not wallet or _is_blacklisted(wallet):
            continue

        ts = _trade_ts(t)
        if ts is None or ts < cutoff_ts:
            continue

        side_raw = (t.get("outcome") or t.get("name") or "").lower()
        direction = _classify_outcome(side_raw)
        if direction is None:
            continue

        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        if size < 0.01 or price <= 0:
            continue

        won = (direction == resolved)

        s = wallet_stats[wallet]
        s["wallet"] = wallet
        s["username"] = (t.get("pseudonym")
                         or s.get("username")
                         or wallet[:10])
        s["trades"] += 1
        s["wins" if won else "losses"] += 1
        s["total_size"] += size
        s["total_notional"] += size * price
        s["markets"].add(cid)
        s["last_ts"] = max(s["last_ts"], ts)
        s["first_ts"] = min(s["first_ts"], ts) if s["first_ts"] > 0 else ts
        if direction == "up":
            s["up_trades"] += 1
            if won:
                s["up_wins"] += 1
        else:
            s["down_trades"] += 1
            if won:
                s["down_wins"] += 1
        qualifying += 1

    return qualifying


def _new_wallet_stats() -> dict:
    return {
        "trades": 0, "wins": 0, "losses": 0,
        "total_size": 0.0, "total_notional": 0.0,
        "markets": set(), "last_ts": 0, "first_ts": 0,
        "up_trades": 0, "up_wins": 0,
        "down_trades": 0, "down_wins": 0,
    }


# -- Filtering & ranking -----------------------------------------------------

def filter_and_rank(stats: dict, min_wr: float, min_trades: int,
                    max_age_days: int = 14) -> list[dict]:
    """Apply criteria and rank candidates."""
    now = time.time()
    age_cutoff = now - max_age_days * 24 * 3600
    out: list[dict] = []

    for wallet, s in stats.items():
        if s["trades"] < min_trades:
            continue
        wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0.0
        if wr < min_wr:
            continue
        if s["last_ts"] < age_cutoff:
            continue  # stale wallet

        avg_size = s["total_size"] / s["trades"]
        avg_notional = s["total_notional"] / s["trades"]
        days_active = max(1, (s["last_ts"] - s["first_ts"]) / 86400)
        trades_per_week = s["trades"] / days_active * 7

        out.append({
            "wallet": wallet,
            "username": s.get("username", wallet[:10]),
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["losses"],
            "wr": round(wr, 4),
            "up_trades": s["up_trades"],
            "up_wr": round(s["up_wins"] / s["up_trades"], 3)
                     if s["up_trades"] else 0.0,
            "down_trades": s["down_trades"],
            "down_wr": round(s["down_wins"] / s["down_trades"], 3)
                       if s["down_trades"] else 0.0,
            "avg_size": round(avg_size, 2),
            "avg_notional_usd": round(avg_notional, 2),
            "n_markets": len(s["markets"]),
            "days_active": round(days_active, 1),
            "trades_per_week": round(trades_per_week, 2),
            "last_seen_age_days": round((now - s["last_ts"]) / 86400, 1),
        })

    # Rank by trades_per_week DESC, then WR DESC, then trades DESC.
    # Frequency matters per user directive: "buying frequently enough
    # with full account size into the determined up/down condition."
    out.sort(key=lambda r: (-r["trades_per_week"], -r["wr"], -r["trades"]))
    return out


# -- Main ---------------------------------------------------------------------

async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=365,
                   help="History window in days (default 365)")
    p.add_argument("--last-min", type=int, default=5,
                   help="Define 'the very end' as last N minutes before "
                        "15m market expiry (default 5)")
    p.add_argument("--min-wr", type=float, default=0.95,
                   help="Minimum win rate (default 0.95)")
    p.add_argument("--min-trades", type=int, default=10,
                   help="Minimum sample size (default 10)")
    p.add_argument("--max-age-days", type=int, default=14,
                   help="Wallet must have traded within last N days "
                        "(default 14)")
    p.add_argument("--top", type=int, default=30,
                   help="Print top N candidates (default 30)")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n  Polymarket BTC-15m terminal-buyer scan", file=sys.stderr)
    print(f"  ----------------------------------------------", file=sys.stderr)
    print(f"  history       : {args.days} days", file=sys.stderr)
    print(f"  last_min      : {args.last_min}  (= last {args.last_min} min "
          f"before settlement)", file=sys.stderr)
    print(f"  min_wr        : {args.min_wr}", file=sys.stderr)
    print(f"  min_trades    : {args.min_trades}", file=sys.stderr)
    print(f"  max_age_days  : {args.max_age_days}", file=sys.stderr)
    print(file=sys.stderr)

    t0 = time.time()
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20)
    ) as session:
        markets = await discover_markets(session, args.days)
        if not markets:
            print("  No markets discovered. Polymarket API issue?",
                  file=sys.stderr)
            return 1

        print(f"  Fetching trades for {len(markets)} markets...",
              file=sys.stderr)
        sem = asyncio.Semaphore(10)
        wallet_stats = defaultdict(_new_wallet_stats)
        total_qualifying = 0
        analyzed = 0

        async def go(m: dict) -> int:
            nonlocal analyzed
            async with sem:
                n = await analyze_market(session, m, args.last_min,
                                          wallet_stats)
            analyzed += 1
            if analyzed % 100 == 0:
                print(f"    analyzed {analyzed}/{len(markets)} markets, "
                      f"{total_qualifying + n} late-window buys, "
                      f"{len(wallet_stats)} wallets seen",
                      file=sys.stderr)
            return n

        results = await asyncio.gather(*(go(m) for m in markets))
        total_qualifying = sum(results)

    elapsed = time.time() - t0
    print(f"\n  Scan complete in {elapsed:.1f}s. "
          f"{total_qualifying} late-window buys across "
          f"{len(wallet_stats)} wallets.", file=sys.stderr)

    ranked = filter_and_rank(
        wallet_stats, args.min_wr, args.min_trades,
        max_age_days=args.max_age_days,
    )

    # -- Output -----------------------------------------------------------
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "days": args.days,
            "last_min": args.last_min,
            "min_wr": args.min_wr,
            "min_trades": args.min_trades,
            "max_age_days": args.max_age_days,
        },
        "n_candidates": len(ranked),
        "n_markets_scanned": len(markets) if markets else 0,
        "n_late_window_buys": total_qualifying,
        "wallets": ranked,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2))

    # Raw stats (with set serialized as count)
    raw = {}
    for w, s in wallet_stats.items():
        raw[w] = {**s, "markets": len(s["markets"])}
    RAW_FILE.write_text(json.dumps(raw, indent=2, default=str))

    print(f"\n  -- Top {min(args.top, len(ranked))} candidates "
          f"(of {len(ranked)} qualifying) --")
    print(f"  {'rank':>4}  {'wallet':<20}  {'WR':>5}  {'n':>4}  "
          f"{'/wk':>5}  {'avg$':>6}  {'up/dn':>9}  {'last_age':>8}  "
          f"{'username'}")
    print(f"  {'-' * 100}")
    for i, r in enumerate(ranked[:args.top], 1):
        wkt = f"{r['up_trades']}/{r['down_trades']}"
        print(f"  {i:>4}  {r['wallet'][:18]:<20}  "
              f"{r['wr']*100:>4.1f}%  {r['trades']:>4}  "
              f"{r['trades_per_week']:>5.1f}  "
              f"${r['avg_notional_usd']:>5.0f}  "
              f"{wkt:>9}  {r['last_seen_age_days']:>5.1f}d  "
              f"{r['username'][:30]}")

    print(f"\n  Full output: {OUT_FILE}")
    print(f"  Raw stats  : {RAW_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
