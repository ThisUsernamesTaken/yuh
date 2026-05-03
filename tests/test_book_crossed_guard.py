"""Tests for the book-crossed guard in BB_PURE eval (2026-05-03).

Live regression observed 2026-05-03 10:50 PT: ticker 26MAY031400-00 had
an empty REST orderbook but the WS book object reported phantom
best_yes_bid=47, best_no_bid=87 (sum > 100, crossed/inverted). On
Kalshi:

    YES_ask = 100 - NO_bid
    YES_bid + NO_bid > 100  =>  YES_ask < YES_bid  (crossed)

bb_pure.evaluate() doesn't see the book directly — it gets the
pre-computed mid_price_cents which falls back to nonsense values when
the book is crossed (mid = (47+13)/2 = 30 in our live case). The
engine's downstream slippage check caught the resulting phantom signal
but spammed logs at ~3-4/sec for the entire window.

The fix: reject BB_PURE eval at the WS-book stage when the sum of
bids exceeds 100. Lives in _evaluate_bb_pure_signal in the engine
itself (not bb_pure.py — bb_pure is pure math, doesn't read the book).

These tests pin the invariant; the integration is exercised in live
behavior (look for an *absence* of SIGNAL/SLIPPAGE-SKIP loops on
fresh-market windows).
"""
from __future__ import annotations


def is_book_crossed(yes_bid: int, no_bid: int) -> bool:
    """Mirrors the guard logic in _evaluate_bb_pure_signal."""
    if yes_bid <= 0 or no_bid <= 0:
        return False  # empty book — different failure mode handled elsewhere
    return (yes_bid + no_bid) > 100


def test_normal_book_passes():
    # YES bid 30, NO bid 65 → YES ask = 100-65 = 35. Spread 30-35, mid 32.
    # Sum = 95, within bounds.
    assert not is_book_crossed(yes_bid=30, no_bid=65)


def test_tight_normal_book_passes():
    # 1c spread: YES bid 49, NO bid 50 → YES ask 50. Spread 49-50.
    # Sum = 99, just below threshold.
    assert not is_book_crossed(yes_bid=49, no_bid=50)


def test_exactly_100_passes():
    # YES bid 49, NO bid 51 → YES ask = 49 (touch, zero spread).
    # Sum = 100; Kalshi matches these immediately so it's transient
    # but not yet inverted. Allow.
    assert not is_book_crossed(yes_bid=49, no_bid=51)


def test_crossed_book_rejects():
    # The 2026-05-03 10:50 PT live case: YES bid 47, NO bid 87.
    # Sum = 134, way over.
    assert is_book_crossed(yes_bid=47, no_bid=87)


def test_marginally_crossed_rejects():
    # 1c crossed: sum = 101.
    assert is_book_crossed(yes_bid=50, no_bid=51)


def test_empty_book_passes_through():
    """Empty book (bid=0 on either side) is handled by other guards."""
    assert not is_book_crossed(yes_bid=0, no_bid=0)
    assert not is_book_crossed(yes_bid=30, no_bid=0)
    assert not is_book_crossed(yes_bid=0, no_bid=65)


def test_extreme_one_sided_passes():
    """Very wide spreads but valid: YES bid 5, NO bid 5 → YES ask 95.
    Spread 5-95 (90c wide), sum 10. Fine."""
    assert not is_book_crossed(yes_bid=5, no_bid=5)
