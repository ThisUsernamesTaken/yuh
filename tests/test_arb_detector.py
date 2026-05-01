"""Unit tests for cross-side arbitrage detector (Claude 2026-04-28).

Covers:
- Disabled flag = no-op
- No active ticker / no book = no-op
- Sum below threshold + sufficient depth + balance → fires both legs
- Sum above threshold = no-op
- Insufficient depth = skip
- Per-ticker cooldown enforced
- Partial fill rebalancing
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polymarket_copy_engine as pce


class _FakeOrder:
    def __init__(self, oid, fc):
        self.order_id = oid
        self.filled_count = fc
        self.status = "filled"


class _FakeBalance:
    def __init__(self, cents):
        self.balance = cents


class _FakeBook:
    def __init__(self, yes_bid=0, yes_ask=0, no_bid=0, no_ask=0,
                 yes_asks_stack=None, no_asks_stack=None,
                 yes_bids_stack=None, no_bids_stack=None):
        self.best_yes_bid = yes_bid
        self.best_yes_ask = yes_ask
        self.best_no_bid = no_bid
        self.best_no_ask = no_ask
        self.yes_asks = yes_asks_stack or []
        self.no_asks = no_asks_stack or []
        self.yes_bids = yes_bids_stack or []
        self.no_bids = no_bids_stack or []


class _FakeWS:
    def __init__(self, books):
        self._books = books

    def get_book(self, ticker):
        return self._books.get(ticker)


class _FakeClient:
    def __init__(self, balance_cents=10000, fill_yes=None, fill_no=None,
                 fail_no=False):
        self._balance_cents = balance_cents
        self._fill_yes = fill_yes  # if None, fills the requested count
        self._fill_no = fill_no
        self._fail_no = fail_no
        self.placed_orders = []

    async def get_balance(self):
        return _FakeBalance(self._balance_cents)

    async def place_order(self, *, ticker, side, price, count, action, **_):
        self.placed_orders.append({
            "ticker": ticker, "side": side, "price": price,
            "count": count, "action": action,
        })
        if side == "yes" and action == "buy":
            f = count if self._fill_yes is None else self._fill_yes
            return _FakeOrder("y_oid", f)
        if side == "no" and action == "buy":
            if self._fail_no:
                raise RuntimeError("simulated NO failure")
            f = count if self._fill_no is None else self._fill_no
            return _FakeOrder("n_oid", f)
        # rebalance sells fall through here
        return _FakeOrder(f"r_{side}", count)


def _make_engine(book_yes_ask=30, book_no_ask=66, balance_cents=10000,
                 yes_depth=100, no_depth=100, fill_yes=None, fill_no=None,
                 fail_no=False, current_ticker="KXBTC15M-T"):
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    # 2026-04-28 (Claude follow-up): the WS LocalOrderBook stores
    # yes_bids / no_bids only; YES-ask depth lives on the NO-bid stack
    # (since yes_ask_price = 100 - best_no_bid). Mirror that shape here
    # so the depth gate sees the test's intended liquidity. We populate
    # yes_asks_stack / no_asks_stack as well for any back-compat lookups.
    book = _FakeBook(
        yes_bid=book_yes_ask - 1, yes_ask=book_yes_ask,
        no_bid=book_no_ask - 1, no_ask=book_no_ask,
        yes_asks_stack=[(book_yes_ask, yes_depth)],
        no_asks_stack=[(book_no_ask, no_depth)],
        # YES ask side ↔ NO bid stack (price = 100 - yes_ask)
        no_bids_stack=[(100 - book_yes_ask, yes_depth)],
        # NO ask side ↔ YES bid stack (price = 100 - no_ask)
        yes_bids_stack=[(100 - book_no_ask, no_depth)],
    )
    eng._kalshi_ws = _FakeWS({current_ticker: book})
    eng._client = _FakeClient(
        balance_cents=balance_cents, fill_yes=fill_yes, fill_no=fill_no,
        fail_no=fail_no,
    )
    eng._current_kalshi_ticker = current_ticker
    eng._arb_last_fire_ts = {}
    return eng


def _set_cfg(**kwargs):
    defaults = {
        "ARB_DETECTOR_ENABLED": True,
        "ARB_TRADES_ENABLED": True,  # tests verify trade behavior; observability mode skipped
        "ARB_POLL_INTERVAL_S": 0.5,
        "ARB_MAX_SUM_CENTS": 97,
        "ARB_MIN_NET_CENTS_PER_PAIR": 1.0,
        "ARB_FRACTION_OF_BALANCE": 0.10,
        "ARB_MAX_CONTRACTS": 200,
        "ARB_MIN_CONTRACTS": 10,
        "ARB_MIN_TOP_DEPTH": 5,
        "ARB_MIN_BALANCE": 5.0,
        "ARB_TICKER_COOLDOWN_S": 30.0,
        # 2026-04-28 session classifier: relax to 5ct min depth for tests
        # (production default is 10). Tests that explicitly check
        # depth-cap sizing use values as low as 8ct, which need a
        # classifier-friendly threshold to allow firing.
        "ARB_TIME_CUTOFF_MIN": 99.0,
        "ARB_FILL_GRACE_S": 0.0,
        "ARB_MIN_UNWIND_DEPTH": 1,
        "ARB_SESSION_DECIDED_LOW": 0,
        "ARB_SESSION_DECIDED_HIGH": 100,
        "ARB_SESSION_BILATERAL_ASK_LO": 0,
        "ARB_SESSION_BILATERAL_ASK_HI": 100,
        "ARB_SESSION_BILATERAL_MIN_DEPTH": 1,
        "ARB_SESSION_BILATERAL_SUM_LO": 0,
        "ARB_SESSION_BILATERAL_SUM_HI": 100,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        pce._user_cfg[k] = v


# ── Sum threshold ──────────────────────────────────────────────────────────

def test_arb_fires_when_sum_below_threshold():
    """30 + 66 = 96 < 97 → should fire."""
    _set_cfg()
    eng = _make_engine(book_yes_ask=30, book_no_ask=66)
    asyncio.run(eng._check_arb_opportunity())
    # Should have placed 2 orders (one each side)
    assert len(eng._client.placed_orders) == 2
    sides = [o["side"] for o in eng._client.placed_orders]
    assert "yes" in sides and "no" in sides


def test_arb_skips_when_sum_above_threshold():
    """40 + 60 = 100 → no fire."""
    _set_cfg()
    eng = _make_engine(book_yes_ask=40, book_no_ask=60)
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


def test_arb_skips_at_threshold():
    """30 + 67 = 97 = threshold → no fire (must be strictly less)."""
    _set_cfg(ARB_MAX_SUM_CENTS=97)
    eng = _make_engine(book_yes_ask=30, book_no_ask=67)
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


# ── Sizing ────────────────────────────────────────────────────────────────

def test_arb_size_capped_by_max_contracts():
    """Big balance → cap at ARB_MAX_CONTRACTS, not full balance."""
    _set_cfg(ARB_MAX_CONTRACTS=50, ARB_FRACTION_OF_BALANCE=1.0)
    # $1000 balance, sum=96, would buy 1041 each side — cap at 50
    eng = _make_engine(balance_cents=100000, yes_depth=500, no_depth=500)
    asyncio.run(eng._check_arb_opportunity())
    assert len(eng._client.placed_orders) == 2
    assert eng._client.placed_orders[0]["count"] == 50


def test_arb_size_capped_by_top_depth():
    """If only 8ct visible depth, don't try to sweep — bound to 8."""
    _set_cfg(ARB_MIN_CONTRACTS=5, ARB_MAX_CONTRACTS=200)
    eng = _make_engine(balance_cents=100000, yes_depth=8, no_depth=8)
    asyncio.run(eng._check_arb_opportunity())
    assert len(eng._client.placed_orders) == 2
    assert eng._client.placed_orders[0]["count"] == 8


def test_arb_skips_when_depth_too_low():
    """Both sides only 3ct depth, min=5 → skip."""
    _set_cfg(ARB_MIN_TOP_DEPTH=5)
    eng = _make_engine(yes_depth=3, no_depth=3)
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


def test_arb_skips_when_count_below_min():
    """Tiny balance → computed size below min — skip."""
    _set_cfg(ARB_MIN_CONTRACTS=10, ARB_FRACTION_OF_BALANCE=0.1)
    # $0.50 balance × 10% = 5c → 5/96 = 0 contracts
    eng = _make_engine(balance_cents=50)
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


# ── Cooldown ──────────────────────────────────────────────────────────────

def test_arb_cooldown_blocks_repeat_on_same_ticker():
    """After firing, cooldown blocks next attempt on same ticker."""
    import time as _time
    _set_cfg()
    eng = _make_engine()
    asyncio.run(eng._check_arb_opportunity())
    n_after_first = len(eng._client.placed_orders)
    assert n_after_first == 2
    # Second call immediately — cooldown blocks
    asyncio.run(eng._check_arb_opportunity())
    assert len(eng._client.placed_orders) == n_after_first


def test_arb_cooldown_clears_after_window():
    """Once cooldown elapses, next opportunity fires."""
    import time as _time
    _set_cfg(ARB_TICKER_COOLDOWN_S=0.0)  # zero cooldown for the test
    eng = _make_engine()
    asyncio.run(eng._check_arb_opportunity())
    assert len(eng._client.placed_orders) == 2
    asyncio.run(eng._check_arb_opportunity())
    # Should fire again with no cooldown
    assert len(eng._client.placed_orders) == 4


# ── Failure handling ─────────────────────────────────────────────────────

def test_arb_emergency_unwind_when_no_leg_fails():
    """If YES fills but NO place_order raises, emergency-sell YES.
    Note: my fake client appends to placed_orders before raising, so we
    see 3 entries: yes_buy + no_buy (raise after append) + yes_sell.
    Real Kalshi raises BEFORE recording; in production we'd see 2.
    Either way, the key assertion is that an emergency sell fired."""
    _set_cfg()
    eng = _make_engine(fail_no=True, fill_yes=None)  # fill matches placed count
    asyncio.run(eng._check_arb_opportunity())
    # Confirm an emergency sell on the YES side fired
    sells = [o for o in eng._client.placed_orders
             if o["action"] == "sell" and o["side"] == "yes"]
    assert len(sells) == 1, "Emergency YES unwind sell should fire"
    assert sells[0]["count"] > 0


def test_arb_partial_fill_rebalance():
    """YES fills 20 but NO only fills 15 — v5 rebalance: BUY 5 more NO
    (complete-pair via opposite-side buy, not sell-the-over-leg).

    v5 phase 1: post_only BUY at bid+1c (capped at ask-1) to wait for
    sellers to cross. In this test the fake book has no_bid=65 (=no_ask-1)
    so post_only buy at 66c would cross → capped at ask-1=65. Fake client
    fills the full 5 at 65c. Profit per pair = 100 - 30 - 65 = 5c.
    """
    _set_cfg(ARB_FRACTION_OF_BALANCE=0.20, ARB_MIN_CONTRACTS=5,
             ARB_REBALANCE_MIN_PROFIT_C=1,
             ARB_REBALANCE_BUY_MAKER_TIMEOUT_S=0.5,  # quick test
             ARB_REBALANCE_BUY_MAKER_OFFSET_C=1)
    # Force count=20 by sizing $20 / $0.96 ≈ 20
    eng = _make_engine(balance_cents=10000, fill_yes=20, fill_no=15)
    asyncio.run(eng._check_arb_opportunity())
    # Orders: yes_buy 20 + no_buy 20 (initial pair) + no_buy 5 (rebalance) = 3 orders
    assert len(eng._client.placed_orders) == 3
    buys = [o for o in eng._client.placed_orders if o["action"] == "buy"]
    sells = [o for o in eng._client.placed_orders if o["action"] == "sell"]
    assert len(sells) == 0, "v5: no sells, only buys for rebalance"
    assert len(buys) == 3
    # Last buy is the rebalance: buy 5 more NO
    rebal = buys[-1]
    assert rebal["side"] == "no"
    assert rebal["count"] == 5
    # v5 maker price = min(no_bid+1, no_ask-1) = min(66, 65) = 65c
    # (no_bid=65 because fake fixture sets no_bid = no_ask - 1 = 66 - 1)
    assert rebal["price"] == 65


# ── Sanity gates ─────────────────────────────────────────────────────────

def test_arb_skips_when_no_ticker():
    _set_cfg()
    eng = _make_engine()
    eng._current_kalshi_ticker = ""
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


def test_arb_skips_when_balance_below_floor():
    _set_cfg(ARB_MIN_BALANCE=10.0)
    eng = _make_engine(balance_cents=500)  # $5 < $10
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []


def test_arb_skips_when_book_missing():
    _set_cfg()
    eng = _make_engine()
    eng._kalshi_ws = SimpleNamespace(get_book=lambda t: None)
    asyncio.run(eng._check_arb_opportunity())
    assert eng._client.placed_orders == []
