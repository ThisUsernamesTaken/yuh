"""Tests for the strategic reset entry gates (2026-05-02).

Two new gates encode user-validated alpha conditions:

1. STRIKE-DISTANCE GATE: only fire when contract is < ±0.04% of strike.
   Inside that band, gamma is high and prices are meaningful. Outside,
   the contract is mostly-settled and the book is noise.

2. TIME-OF-DAY GATE: pause overnight (default 22:00–06:00 PT). User
   reported manual trading overnight is "ruthless" — less liquidity,
   more noise.

Tests pin down the helper logic (computed inline in
_evaluate_bb_pure_signal). We re-implement the exact branch logic
in test helpers to assert behavior without spinning up the full
engine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── Strike-distance gate helper (mirrors the engine code) ────────────────


def _strike_dist_block(btc_price: float, strike: float, max_pct: float) -> bool:
    """Return True if entry should be BLOCKED."""
    if btc_price <= 0 or strike <= 0:
        return False  # missing data → don't block
    dist_pct = abs(btc_price - strike) / btc_price
    return dist_pct > max_pct


def test_strike_dist_at_strike_passes():
    assert not _strike_dist_block(78000, 78000, 0.0004)


def test_strike_dist_just_inside_band_passes():
    # 78000 vs 78031 = $31 = 0.0397% — just under 0.04%
    assert not _strike_dist_block(78000, 78031, 0.0004)


def test_strike_dist_just_outside_band_blocks():
    # 78000 vs 78032 = $32 = 0.04102% — just over 0.04%
    assert _strike_dist_block(78000, 78032, 0.0004)


def test_strike_dist_far_from_strike_blocks():
    # 78000 vs 78500 = $500 = 0.64%
    assert _strike_dist_block(78000, 78500, 0.0004)


def test_strike_dist_below_strike_blocks_too():
    # symmetry check
    assert _strike_dist_block(78000, 77000, 0.0004)


def test_strike_dist_zero_btc_does_not_block():
    """Missing data shouldn't false-block (defensive default)."""
    assert not _strike_dist_block(0, 78000, 0.0004)


def test_strike_dist_zero_strike_does_not_block():
    assert not _strike_dist_block(78000, 0, 0.0004)


# ── Time-of-day gate helper ──────────────────────────────────────────────


def _hours_block(now_utc: datetime, pt_offset_h: float, start_h: int, end_h: int) -> bool:
    """Return True if entry should be BLOCKED (outside trading window)."""
    now_pt = now_utc + timedelta(hours=pt_offset_h)
    hour_pt = now_pt.hour
    if start_h <= end_h:
        in_window = start_h <= hour_pt < end_h
    else:
        # Wraps midnight
        in_window = hour_pt >= start_h or hour_pt < end_h
    return not in_window


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_hours_passes_in_active_window():
    # 14:00 UTC = 07:00 PT (PDT) → inside [6, 22)
    assert not _hours_block(_utc(2026, 5, 2, 14), -7, 6, 22)


def test_hours_blocks_at_midnight_pt():
    # 07:00 UTC = 00:00 PT → outside [6, 22)
    assert _hours_block(_utc(2026, 5, 2, 7), -7, 6, 22)


def test_hours_blocks_late_night_pt():
    # 06:00 UTC = 23:00 PT (previous day) → outside [6, 22)
    assert _hours_block(_utc(2026, 5, 2, 6), -7, 6, 22)


def test_hours_passes_at_window_start():
    # 13:00 UTC = 06:00 PT → at window start [6, 22) — passes
    assert not _hours_block(_utc(2026, 5, 2, 13), -7, 6, 22)


def test_hours_blocks_at_window_end():
    # 05:00 UTC = 22:00 PT → at window end (exclusive) [6, 22) — blocks
    assert _hours_block(_utc(2026, 5, 2, 5), -7, 6, 22)


def test_hours_wrap_midnight_window():
    """If start > end (e.g., 22→6), the window wraps midnight."""
    # 23:00 PT — should be IN a [22, 6) window
    assert not _hours_block(_utc(2026, 5, 2, 6), -7, 22, 6)
    # 02:00 PT — also in [22, 6)
    assert not _hours_block(_utc(2026, 5, 2, 9), -7, 22, 6)
    # 12:00 PT — outside [22, 6)
    assert _hours_block(_utc(2026, 5, 2, 19), -7, 22, 6)


def test_hours_pst_offset():
    """Winter offset is -8 (PST). 16:00 UTC = 08:00 PST → in window."""
    assert not _hours_block(_utc(2026, 1, 15, 16), -8, 6, 22)
