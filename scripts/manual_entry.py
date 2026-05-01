"""Manual-entry helper for KXBTC15M trading.

Companion CLI for placing limit entries / TPs / exits on the active 15-min
contract without touching the Kalshi web UI. Mirrors the engine's behavior:
post-only limits, auto-attached TP at +N% of entry price, etc.

Why this exists
---------------
The user's manual trading is the project's strongest alpha source (see
`manual_fills` table + AI_COLLAB_LOG 2026-04-28). This script is for the
moments when they want to fire a limit at the level they're seeing on
their terminal but the engine has the active ticker locked. It uses its
own KalshiClient instance — does NOT touch the engine's state. Engine's
manual-detection gates (size > 150ct, untouched ticker, etc.) will treat
fills from this script as user manual trades and snapshot them into
`manual_fills` for pattern analysis later.

Examples
--------
    # Show what's tradeable right now
    python scripts/manual_entry.py book

    # Place a 30ct YES buy at 35c with auto-TP at +10% (= 38c)
    python scripts/manual_entry.py buy yes 30 --price 35 --tp 10

    # Place a 50ct NO buy at 28c, no TP
    python scripts/manual_entry.py buy no 50 --price 28 --no-tp

    # Cancel everything resting
    python scripts/manual_entry.py cancel

    # Flatten everything (sell every position at the best bid)
    python scripts/manual_entry.py flatten

    # Show current balance + open positions
    python scripts/manual_entry.py status

Auto-TP math
------------
TP price = round(entry_price * (1 + tp_pct/100)). Floored at entry+1c so
we never accidentally place a TP below entry. Capped at 95c (Kalshi's
practical ceiling for limit orders). Posted as a limit-sell at the same
side as the entry — that's how Kalshi closes a position.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

# Make the engine modules importable when running from scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kalshi_client import KalshiClient  # noqa: E402


def _load_credentials() -> tuple[str, str, bool]:
    """Match the loading flow in run_copy_engine.py.

    Returns (key_id, pem_string, demo_flag).
    """
    env_file = os.path.join(ROOT, "credentials", "kalshi.env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    key_id = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    demo = os.environ.get("KALSHI_DEMO", "false").lower() == "true"
    if not key_id or not pem_path or not os.path.exists(pem_path):
        sys.stderr.write(
            "ERROR: Kalshi credentials missing. Expected "
            "credentials/kalshi.env with KALSHI_API_KEY and "
            "KALSHI_PRIVATE_KEY_PATH set.\n"
        )
        sys.exit(2)
    with open(pem_path, "r", encoding="utf-8") as f:
        pem = f.read()
    return key_id, pem, demo


async def _active_contract(client: KalshiClient):
    """Return the closest-to-expiry tradeable KXBTC15M contract."""
    contracts = await client.find_btc_contracts(min_minutes_remaining=1.0)
    if not contracts:
        sys.stderr.write("ERROR: no tradeable KXBTC15M contracts.\n")
        sys.exit(3)
    return contracts[0]


async def _print_book(client: KalshiClient) -> None:
    c = await _active_contract(client)
    bal = await client.get_balance()
    book = await client.get_orderbook(c.ticker)
    minutes_left = (c.expiry_ts - int(__import__("time").time() * 1000)) / 60_000
    # Use the book's own quotes — the contract-listing snapshot is up to a
    # second stale and produced an off-by-one with the depth listing below.
    yes_bid = book.best_yes_bid
    yes_ask = book.best_yes_ask
    no_bid = book.best_no_bid
    no_ask = book.best_no_ask
    yes_spread = yes_ask - yes_bid
    no_spread = no_ask - no_bid
    sum_ask = yes_ask + no_ask  # cross-side arb metric
    print()
    print(f"  TICKER:   {c.ticker}")
    print(f"  EXPIRES:  {minutes_left:.1f} min")
    print(f"  BALANCE:  ${bal.balance / 100:.2f}")
    print()
    print(f"  YES  bid={yes_bid:2d}c  ask={yes_ask:2d}c  spread={yes_spread}c")
    print(f"  NO   bid={no_bid:2d}c  ask={no_ask:2d}c  spread={no_spread}c")
    print(f"  SUM  yes_ask + no_ask = {sum_ask}c "
          f"({'ARB' if sum_ask < 97 else 'no arb'} threshold=97c)")
    # Top 3 ask levels per side, derived per Kalshi book mechanics:
    #   YES ask price = 100 - NO bid price (someone selling YES is the
    #   inverse of someone bidding for NO at 100−price). Same for NO ask.
    print()
    print("  Top-3 YES asks (price, qty):")
    for bid_px, qty in (book.no_bids or [])[:3]:
        print(f"    {100 - int(bid_px):3d}c  x  {qty}")
    print("  Top-3 NO asks (price, qty):")
    for bid_px, qty in (book.yes_bids or [])[:3]:
        print(f"    {100 - int(bid_px):3d}c  x  {qty}")
    # Microprice (built-in property does the bid/ask + size weighting)
    print(f"  YES microprice: {book.microprice_cents:.1f}c "
          f"(top_yes_qty={book.top_yes_qty} top_no_qty={book.top_no_qty} "
          f"imbalance={book.imbalance:+.2f})")


async def _status(client: KalshiClient) -> None:
    bal = await client.get_balance()
    positions = await client.get_positions()
    # Kalshi's positions API returns:
    #   position_fp = signed fixed-point string ("337.00", "-21.00", etc.)
    #   market_exposure_dollars = cost basis as decimal string ("107.62")
    #   realized_pnl_dollars = realized P&L for this market as decimal string
    # 2026-04-28 (Claude follow-up): we previously read p["position"] which
    # doesn't exist on this API shape — counted 13 archived/zero rows but
    # printed nothing. Now uses position_fp.
    def _fp(p, key):
        try:
            return float(p.get(key, 0) or 0)
        except (ValueError, TypeError):
            return 0.0
    live = [p for p in positions if abs(_fp(p, "position_fp")) > 0.01]
    print()
    print(f"  BALANCE:  ${bal.balance / 100:.2f}")
    print(f"  PORT VAL: ${bal.portfolio_value / 100:.2f}")
    print(f"  POSITIONS: {len(live)} live ({len(positions)} total rows)")
    for p in sorted(live, key=lambda x: x.get("ticker", "")):
        ticker = p.get("ticker", "?")
        pos = int(_fp(p, "position_fp"))
        exposure = _fp(p, "market_exposure_dollars")
        realized = _fp(p, "realized_pnl_dollars")
        print(f"    {ticker[-28:]:28s}  pos={pos:+5d}  cost=${exposure:7.2f}  "
              f"realized=${realized:+.2f}")


async def _cancel_all(client: KalshiClient) -> None:
    n = await client.cancel_all_resting_orders()
    print(f"  Cancelled {n} resting orders.")


async def _flatten(client: KalshiClient) -> None:
    """Market-sell every open position at the best available bid."""
    positions = await client.get_positions()
    sold = 0
    for p in positions:
        # Kalshi field is position_fp (signed fixed-point string).
        try:
            cnt = int(float(p.get("position_fp", 0) or 0))
        except (ValueError, TypeError):
            cnt = 0
        if cnt == 0:
            continue
        ticker = p.get("ticker", "")
        if not ticker:
            continue
        # Determine side from sign: positive = YES, negative = NO
        side = "yes" if cnt > 0 else "no"
        try:
            book = await client.get_orderbook(ticker)
            if side == "yes":
                bid = int(book.yes_bids[0][0]) if book.yes_bids else 1
            else:
                bid = int(book.no_bids[0][0]) if book.no_bids else 1
        except Exception:
            bid = 1
        try:
            order = await client.place_order(
                ticker=ticker, side=side, count=abs(cnt),
                price=max(1, bid - 1), action="sell",
            )
            print(f"  FLATTEN {ticker[-20:]}: sold {abs(cnt)}ct {side.upper()} "
                  f"@ {bid - 1}c  (order={order.order_id[:12]})")
            sold += 1
        except Exception as e:
            print(f"  FLATTEN {ticker[-20:]} FAILED: {e}")
    if sold == 0:
        print("  No open positions to flatten.")


async def _buy(
    client: KalshiClient,
    side: str,
    count: int,
    price: Optional[int],
    tp_pct: float,
    no_tp: bool,
    market: bool,
) -> None:
    c = await _active_contract(client)
    bal = await client.get_balance()
    side = side.lower()
    if side not in ("yes", "no"):
        sys.stderr.write("ERROR: side must be 'yes' or 'no'.\n")
        sys.exit(2)
    if count <= 0:
        sys.stderr.write("ERROR: count must be > 0.\n")
        sys.exit(2)

    if market:
        # Use the ask as a defensive limit so we don't overpay if the book
        # moves between fetch and order; Kalshi rejects over-cap orders.
        book = await client.get_orderbook(c.ticker)
        if side == "yes":
            ask = int(book.yes_asks[0][0]) if book.yes_asks else 99
        else:
            ask = int(book.no_asks[0][0]) if book.no_asks else 99
        entry_px = ask
        order_type = "limit"  # crosses the spread but bounded
        post_only = False
    else:
        if price is None:
            sys.stderr.write("ERROR: --price required for limit orders "
                             "(or pass --market).\n")
            sys.exit(2)
        if not (1 <= price <= 99):
            sys.stderr.write("ERROR: price must be 1..99.\n")
            sys.exit(2)
        entry_px = price
        order_type = "limit"
        post_only = True  # rest at our price; don't pay spread

    cost_dollars = count * entry_px / 100.0
    print()
    print(f"  PLACE BUY: {count}ct {side.upper()} @ {entry_px}c "
          f"({'market-cross' if market else 'post-only'})")
    print(f"  TICKER:    {c.ticker}")
    print(f"  COST:      ${cost_dollars:.2f}  "
          f"({100 * cost_dollars / max(bal.balance / 100, 0.01):.1f}% of bal)")

    try:
        order = await client.place_order(
            ticker=c.ticker, side=side, count=count, price=entry_px,
            order_type=order_type, action="buy", post_only=post_only,
        )
    except Exception as e:
        sys.stderr.write(f"ERROR placing order: {e}\n")
        sys.exit(4)

    print(f"  ORDER:     {order.order_id} status={order.status} "
          f"filled={order.filled_count}")

    # Auto-TP at entry × (1 + tp_pct/100), bounded
    if no_tp or order.filled_count == 0:
        if no_tp:
            print("  (skipping auto-TP per --no-tp)")
        else:
            print("  (no fill yet — TP not placed; cancel + re-place when "
                  "filled, or run with --tp-on-pending)")
        return

    tp_target = max(entry_px + 1, round(entry_px * (1 + tp_pct / 100.0)))
    tp_target = min(tp_target, 95)  # Kalshi practical limit
    if tp_target <= entry_px:
        print(f"  WARN: tp_target {tp_target}c not above entry {entry_px}c, "
              "skipping auto-TP")
        return
    try:
        tp_order = await client.place_order(
            ticker=c.ticker, side=side, count=order.filled_count,
            price=tp_target, action="sell",
        )
        gain_dollars = order.filled_count * (tp_target - entry_px) / 100.0
        print(f"  AUTO-TP:   sell {order.filled_count}ct @ {tp_target}c  "
              f"(+{tp_target - entry_px}c, +{tp_pct:.0f}% = +${gain_dollars:.2f})")
        print(f"  TP_ORDER:  {tp_order.order_id}")
    except Exception as e:
        sys.stderr.write(f"ERROR placing TP: {e}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manual_entry",
        description="Manual-entry helper for KXBTC15M (BTC Bias Engine).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("book", help="Show active ticker + book + microprice")
    sub.add_parser("status", help="Show balance + open positions")
    sub.add_parser("cancel", help="Cancel all resting orders")
    sub.add_parser("flatten", help="Market-sell every open position at bid-1c")

    pb = sub.add_parser("buy", help="Place a limit buy with optional auto-TP")
    pb.add_argument("side", choices=["yes", "no"], help="Side to buy")
    pb.add_argument("count", type=int, help="Number of contracts")
    pb.add_argument("--price", type=int, default=None,
                    help="Limit price in cents (1-99). Required unless --market.")
    pb.add_argument("--market", action="store_true",
                    help="Cross the spread at the current ask instead of "
                         "resting post-only at --price.")
    pb.add_argument("--tp", type=float, default=10.0,
                    help="Auto-TP percentage above entry (default 10).")
    pb.add_argument("--no-tp", action="store_true",
                    help="Skip the auto-TP placement.")
    return p


async def _main() -> None:
    args = _build_parser().parse_args()
    key_id, pem, demo = _load_credentials()
    client = KalshiClient(key_id=key_id, private_key_pem=pem, demo=demo)
    async with client:
        if args.cmd == "book":
            await _print_book(client)
        elif args.cmd == "status":
            await _status(client)
        elif args.cmd == "cancel":
            await _cancel_all(client)
        elif args.cmd == "flatten":
            await _flatten(client)
        elif args.cmd == "buy":
            await _buy(
                client,
                side=args.side, count=args.count, price=args.price,
                tp_pct=args.tp, no_tp=args.no_tp, market=args.market,
            )


if __name__ == "__main__":
    asyncio.run(_main())
