"""Unit tests for ``PolymarketCopyEngine._exposure_cap_check`` (2026-05-04).

The exposure cap is the second post-catastrophe safeguard. Today's loss
had ``engine_count=13``, ``kalshi_count=156``, ``entry≈44c``, and a
bankroll of ~$1.89 (cash) + ~$68.64 (escrowed) ≈ $70 total. Adopting
that 156-count position via SYNC RECONCILE BACKFILL was the moment the
engine sealed the loss. With the cap active and a 0.25 default fraction,
that adoption is refused — engine state stays at engine_count=13 and the
oversold Kalshi position is left for human review.

These tests pin down the arithmetic. The integration with SYNC RECONCILE
is a separate concern (read the call site in polymarket_copy_engine.py
near line 20592 — the helper is invoked there with live Kalshi truth).
"""
from __future__ import annotations

from polymarket_copy_engine import PolymarketCopyEngine


def _check(**kw):
    return PolymarketCopyEngine._exposure_cap_check(**kw)


# ── Catastrophe scenario regression ─────────────────────────────────────


def test_blocks_catastrophe_scenario():
    """The exact 2026-05-04 shape: 156ct @ 44c on a ~$70 bankroll-equivalent.

    Cash at the moment of reconcile was ~$1.89 because Kalshi had already
    escrowed the 156-contract NO position at $0.44/ct. Total bankroll-
    equivalent = $1.89 + $68.64 = $70.53. Implied cost $68.64 is 97% of
    that — well above the 0.25 cap. Must block.
    """
    blocked, implied_c, cap_c = _check(
        kalshi_count=156,
        entry_cents=44,
        bankroll_cents=189,        # $1.89 cash, escrow already deducted
        cap_frac=0.25,
        enabled=True,
    )
    assert blocked is True
    assert implied_c == 156 * 44     # $68.64
    # cap = 0.25 * (189 + 6864) = 0.25 * 7053 = 1763 cents
    assert cap_c == 1763
    assert implied_c > cap_c


# ── Normal (small) entry must NOT trigger ───────────────────────────────


def test_passes_typical_bb_pure_entry():
    """A 13ct @ 55c entry on a $40 bankroll = $7.15 of $40+$7.15 = $47.15.
    7.15 / 47.15 ≈ 0.152 — below the 0.25 cap. Must NOT block.
    """
    blocked, implied_c, cap_c = _check(
        kalshi_count=13,
        entry_cents=55,
        bankroll_cents=4000,         # $40
        cap_frac=0.25,
        enabled=True,
    )
    assert blocked is False
    assert implied_c == 715
    assert implied_c <= cap_c


def test_passes_at_exact_cap():
    """Edge case: implied cost == cap should NOT block (use ``>``, not ``>=``)
    so a position sized exactly at the cap is allowed."""
    # bankroll=300, kalshi=10ct @ 10c → implied=100, total=400, cap_frac=0.25 → cap=100
    blocked, implied_c, cap_c = _check(
        kalshi_count=10,
        entry_cents=10,
        bankroll_cents=300,
        cap_frac=0.25,
        enabled=True,
    )
    assert implied_c == 100
    assert cap_c == 100
    assert blocked is False, "implied == cap is allowed (boundary inclusive)"


# ── Disabled / bypass behavior ──────────────────────────────────────────


def test_disabled_never_blocks():
    """With ``enabled=False`` the helper is a no-op regardless of inputs."""
    blocked, implied_c, cap_c = _check(
        kalshi_count=10_000,
        entry_cents=99,
        bankroll_cents=10,
        cap_frac=0.25,
        enabled=False,
    )
    assert blocked is False
    assert implied_c == 0
    assert cap_c == 0


def test_zero_count_returns_unblocked():
    """Defensive: count=0 means no exposure to check."""
    blocked, implied_c, cap_c = _check(
        kalshi_count=0,
        entry_cents=44,
        bankroll_cents=1000,
        cap_frac=0.25,
        enabled=True,
    )
    assert blocked is False
    assert implied_c == 0
    assert cap_c == 0


# ── Cap fraction tunability ─────────────────────────────────────────────


def test_tighter_cap_blocks_borderline_position():
    """Same position, tighter cap fraction → blocked."""
    # bankroll=400, kalshi=10ct @ 10c → implied=100, total=500, cap_frac=0.15 → cap=75
    blocked, _, cap_c = _check(
        kalshi_count=10,
        entry_cents=10,
        bankroll_cents=400,
        cap_frac=0.15,
        enabled=True,
    )
    assert cap_c == 75
    assert blocked is True


def test_looser_cap_allows_aggressive_position():
    """Same position, looser cap fraction → allowed."""
    blocked, _, cap_c = _check(
        kalshi_count=10,
        entry_cents=10,
        bankroll_cents=400,
        cap_frac=0.50,
        enabled=True,
    )
    assert cap_c == 250
    assert blocked is False
