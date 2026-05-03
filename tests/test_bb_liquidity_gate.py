"""Tests for the pre-fire exit-liquidity gate (2026-05-03).

Live regression 2026-05-03 11:30-11:45 PT:
  - BB_PURE FIRE: YES 7x @ 51c on KXBTC15M-26MAY031445-45 (filled)
  - PROTECTIVE [SL] fired at 11:37 PT (BTC velocity adverse) — sell @50c
    (taker, post_only=False)
  - Order RESTED. Empty orderbook had no buyer at 50c.
  - Replaced at 11:39 PT — also rested.
  - Window settled NO @ 11:45 PT, full -$3.57 loss.

The book had liquidity at entry but evaporated by exit time. A
pre-fire gate can't catch that exact failure mode, but it CAN catch
the obvious case where the book is thin or empty AT ENTRY. This
prevents trade entry into one-way streets.

Logic mirrored here for deterministic unit testing. Live integration
runs in `_evaluate_bb_pure_signal`'s execute path.
"""
from __future__ import annotations


def has_sufficient_exit_depth(
    bid_book: dict[int, int],   # price_cents → quantity for relevant side
    entry_bid_cents: int,        # current best bid for our entry side
    entry_size: int,             # contracts we'd buy
    *,
    max_exit_loss_cents: int = 25,
    min_depth_floor: int = 1,
) -> bool:
    """Return True if exit-side has enough bid depth at acceptable prices.

    Mirrors the engine's BB_PURE_LIQUIDITY_GATE logic.
    """
    req_depth = max(int(entry_size), int(min_depth_floor))
    exit_floor_px = max(1, entry_bid_cents - max_exit_loss_cents)
    depth = sum(int(qty) for px, qty in bid_book.items()
                if int(px) >= exit_floor_px)
    return depth >= req_depth


# ─── Empty book always blocks ───────────────────────────────────────────


def test_empty_book_blocks():
    """The 2026-05-03 case: orderbook empty → no exit possible."""
    assert not has_sufficient_exit_depth(
        bid_book={}, entry_bid_cents=51, entry_size=7
    )


def test_no_bids_at_acceptable_price_blocks():
    """Bids exist but only at far-below-acceptable prices."""
    # Entry bid 51c, max_loss 25c → exit_floor = 26c
    # Only bidder is at 5c — well below floor.
    assert not has_sufficient_exit_depth(
        bid_book={5: 100}, entry_bid_cents=51, entry_size=7
    )


# ─── Sufficient depth allows entry ──────────────────────────────────────


def test_sufficient_depth_at_entry_passes():
    """Bid stack covers our exit at entry price."""
    # Entry 51c, 7 contracts. Bids: 50c→10ct, 49c→20ct.
    # exit_floor = 51-25 = 26c. Depth at >=26c = 30 ≥ 7. Pass.
    assert has_sufficient_exit_depth(
        bid_book={50: 10, 49: 20}, entry_bid_cents=51, entry_size=7
    )


def test_just_enough_depth_passes():
    """Edge case: depth exactly equals entry size."""
    assert has_sufficient_exit_depth(
        bid_book={50: 7}, entry_bid_cents=51, entry_size=7
    )


def test_one_short_blocks():
    """6 depth for a 7-contract entry → block."""
    assert not has_sufficient_exit_depth(
        bid_book={50: 6}, entry_bid_cents=51, entry_size=7
    )


# ─── Depth at multiple prices ───────────────────────────────────────────


def test_depth_aggregates_across_prices():
    """Total depth across multiple price levels above the floor counts."""
    # exit_floor = 51-25 = 26c. Bids: 50→3, 40→2, 30→2, 20→100 (below floor).
    # Above floor (>=26): 50→3 + 40→2 + 30→2 = 7. Just enough for 7-ct entry.
    assert has_sufficient_exit_depth(
        bid_book={50: 3, 40: 2, 30: 2, 20: 100},
        entry_bid_cents=51, entry_size=7
    )


def test_depth_below_floor_excluded():
    """Below-floor bids aren't counted."""
    # exit_floor = 26c. Bids: 50→3, 25→100 (below floor by 1c).
    # Counted: 50→3. Required: 7. Block.
    assert not has_sufficient_exit_depth(
        bid_book={50: 3, 25: 100}, entry_bid_cents=51, entry_size=7
    )


# ─── Floor knob behavior ────────────────────────────────────────────────


def test_min_depth_floor_governs_for_small_entries():
    """Even a 1-contract entry needs at least min_depth_floor depth."""
    # entry_size=1, but require >=3 depth via floor knob.
    assert not has_sufficient_exit_depth(
        bid_book={50: 2}, entry_bid_cents=51, entry_size=1,
        min_depth_floor=3,
    )
    assert has_sufficient_exit_depth(
        bid_book={50: 5}, entry_bid_cents=51, entry_size=1,
        min_depth_floor=3,
    )


def test_max_exit_loss_zero_requires_at_entry_price():
    """No-loss tolerance: bid must exist at entry price or above."""
    # entry_bid 51c, max_loss=0 → exit_floor=51c.
    # Bid at 50c won't count. Bid at 51c does.
    assert not has_sufficient_exit_depth(
        bid_book={50: 100}, entry_bid_cents=51, entry_size=7,
        max_exit_loss_cents=0,
    )
    assert has_sufficient_exit_depth(
        bid_book={51: 7}, entry_bid_cents=51, entry_size=7,
        max_exit_loss_cents=0,
    )


# ─── Defensive ───────────────────────────────────────────────────────────


def test_zero_size_passes_trivially():
    """Edge: zero-contract entry — gate passes with min floor."""
    assert has_sufficient_exit_depth(
        bid_book={50: 1}, entry_bid_cents=51, entry_size=0
    )


def test_tiny_entry_bid_floor_clamps_to_one():
    """If entry_bid - max_loss goes negative, floor at 1c."""
    # Entry at 5c, max_loss=25c → would give -20c. Clamp to 1c.
    # Any bid >= 1c counts.
    assert has_sufficient_exit_depth(
        bid_book={1: 7}, entry_bid_cents=5, entry_size=7
    )
