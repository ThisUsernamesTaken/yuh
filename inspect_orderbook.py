"""inspect_orderbook.py — Print live order book depth for the current KXBTC15M contract.

Usage:
    cd F:/Trading/btc-bias-engine
    python inspect_orderbook.py           # snapshot once
    python inspect_orderbook.py --watch   # refresh every 5s

Reads KALSHI_API_KEY and KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH from environment.
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone


def _load_private_key() -> str:
    pem = os.getenv("KALSHI_PRIVATE_KEY", "")
    if pem:
        return pem.replace("\\n", "\n")
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if path:
        with open(os.path.expanduser(path)) as f:
            return f.read()
    raise RuntimeError("Set KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH")


def _bar(qty: int, max_qty: int, width: int = 20) -> str:
    filled = int(width * qty / max_qty) if max_qty > 0 else 0
    return "█" * filled + "░" * (width - filled)


def _print_book(book, contract) -> None:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    mins = contract.minutes_to_expiry
    age_ms = int(time.time() * 1000) - book.fetched_at_ms

    print(f"\n{'─'*62}")
    print(f"  {contract.ticker}")
    print(f"  {now}  |  {mins:.1f} min to expiry  |  vol={contract.volume}  |  book age={age_ms}ms")
    print(f"{'─'*62}")

    # Header
    print(f"  {'YES BIDS (buy YES)':^30}  {'NO BIDS (buy NO)':^28}")
    print(f"  {'price':>6}  {'qty':>5}  {'depth bar':<20}  {'price':>6}  {'qty':>5}  {'depth bar'}")
    print(f"  {'─'*6}  {'─'*5}  {'─'*20}  {'─'*6}  {'─'*5}  {'─'*20}")

    max_yes = max((q for _, q in book.yes_bids), default=1)
    max_no  = max((q for _, q in book.no_bids),  default=1)
    rows = max(len(book.yes_bids), len(book.no_bids))

    for i in range(rows):
        if i < len(book.yes_bids):
            yp, yq = book.yes_bids[i]
            yes_str = f"  {yp:>5}¢  {yq:>5}  {_bar(yq, max_yes)}"
        else:
            yes_str = f"  {'':>5}   {'':>5}  {'':20}"

        if i < len(book.no_bids):
            np_, nq = book.no_bids[i]
            no_str = f"  {np_:>5}¢  {nq:>5}  {_bar(nq, max_no)}"
        else:
            no_str = ""

        print(yes_str + no_str)

    print(f"{'─'*62}")

    # Summary
    yes_liq  = book.total_yes_liquidity()
    no_liq   = book.total_no_liquidity()
    yes5     = book.liquidity_within("yes", 5)
    no5      = book.liquidity_within("no",  5)

    print(f"  best YES bid : {book.best_yes_bid:>3}¢   best YES ask : {book.best_yes_ask:>3}¢   spread : {book.spread_cents}¢")
    print(f"  best NO  bid : {book.best_no_bid:>3}¢   best NO  ask : {book.best_no_ask:>3}¢   mid    : {book.mid_cents:.1f}¢")
    print(f"  YES liquidity: {yes_liq:>4} contracts total  ({yes5} within 5¢ of ask)")
    print(f"  NO  liquidity: {no_liq:>4} contracts total  ({no5} within 5¢ of ask)")

    # Market impact for 1, 2, 5 contracts
    print(f"\n  Market impact (buying YES):")
    for n in (1, 2, 5):
        avg, cost, filled = book.fill_cost("yes", n)
        if filled < n:
            print(f"    {n:>2} contracts → only {filled} available in book")
        else:
            print(f"    {n:>2} contracts → avg fill {avg:.1f}¢  (vs ask {book.best_yes_ask}¢  slippage {avg - book.best_yes_ask:+.1f}¢)")

    print(f"\n  Market impact (buying NO):")
    for n in (1, 2, 5):
        avg, cost, filled = book.fill_cost("no", n)
        if filled < n:
            print(f"    {n:>2} contracts → only {filled} available in book")
        else:
            print(f"    {n:>2} contracts → avg fill {avg:.1f}¢  (vs ask {book.best_no_ask}¢  slippage {avg - book.best_no_ask:+.1f}¢)")


async def run(watch: bool) -> None:
    from kalshi_client import KalshiClient

    key_id = os.getenv("KALSHI_API_KEY", "")
    pem    = _load_private_key()
    if not key_id:
        raise RuntimeError("Set KALSHI_API_KEY")

    async with KalshiClient(key_id=key_id, private_key_pem=pem) as client:
        while True:
            contracts = await client.find_btc_contracts(min_minutes_remaining=0.5)
            if not contracts:
                print("No open KXBTC15M contracts found.")
            else:
                contract = contracts[0]
                book = await client.get_orderbook(contract.ticker)
                _print_book(book, contract)

            if not watch:
                break
            print("\n  [refreshing in 5s — Ctrl+C to stop]")
            await asyncio.sleep(5)


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    try:
        asyncio.run(run(watch))
    except KeyboardInterrupt:
        pass
