"""Unit tests for protective_math.decide_tp_placement (2026-05-05).

Today's portfolio-mode session produced 3 winning trades but tripped the
monitoring rule because Trade 3's PROTECTIVE TP layer fired 8 "post only
cross" rejections in 12 seconds when the bid raced past our +5c target.

The taker-convert logic existed in the engine but was disabled
(TP_TAKER_CONVERT_ENABLED=False) pending the MIN-TRUTH safe-sell helper,
which has since shipped. These tests pin the decision boundary so we
can re-enable safely.
"""
from __future__ import annotations

from protective_math import decide_tp_placement


# ─── Standard maker path ────────────────────────────────────────────────


def test_tp_maker_when_bid_below_target():
    """Bid 60c, target 65c → maker post_only at 65c (standard path)."""
    d = decide_tp_placement(target_state="tp", target_px=65, bid=60)
    assert d.place_px == 65
    assert d.post_only is True
    assert d.converted_to_taker is False


def test_tp_maker_when_bid_just_below_target():
    """Boundary: bid 64c, target 65c → still maker (no cross)."""
    d = decide_tp_placement(target_state="tp", target_px=65, bid=64)
    assert d.place_px == 65 and d.post_only is True
    assert d.converted_to_taker is False


# ─── Taker-convert path ─────────────────────────────────────────────────


def test_tp_taker_convert_when_bid_meets_target():
    """Boundary: bid exactly at target → taker-convert (Kalshi rejects
    post_only crosses, including equal-price crosses)."""
    d = decide_tp_placement(target_state="tp", target_px=65, bid=65)
    assert d.place_px == 65
    assert d.post_only is False
    assert d.converted_to_taker is True


def test_tp_taker_convert_when_bid_above_target():
    """Today's Trade 3 scenario: target 62c, bid 69c → taker at 69c."""
    d = decide_tp_placement(target_state="tp", target_px=62, bid=69)
    assert d.place_px == 69
    assert d.post_only is False
    assert d.converted_to_taker is True


def test_tp_taker_convert_far_above_target():
    """Bid raced way past target → still cross at bid (not target)."""
    d = decide_tp_placement(target_state="tp", target_px=55, bid=88)
    assert d.place_px == 88
    assert d.post_only is False
    assert d.converted_to_taker is True


# ─── Non-TP states pass through ─────────────────────────────────────────


def test_sl_state_does_not_taker_convert():
    """SL state has its own placement logic upstream — leave alone."""
    d = decide_tp_placement(target_state="sl", target_px=42, bid=85)
    assert d.place_px == 42
    assert d.post_only is True  # caller will override for SL
    assert d.converted_to_taker is False


def test_hold_state_does_not_taker_convert():
    d = decide_tp_placement(target_state="hold", target_px=42, bid=85)
    assert d.converted_to_taker is False


# ─── Kill-switch + defensive fallbacks ──────────────────────────────────


def test_kill_switch_disables_taker_convert():
    """When operator flips TP_TAKER_CONVERT_ENABLED=False, default to maker."""
    d = decide_tp_placement(
        target_state="tp", target_px=62, bid=69,
        enable_taker_convert=False,
    )
    assert d.place_px == 62
    assert d.post_only is True
    assert d.converted_to_taker is False


def test_zero_bid_falls_back_to_maker():
    """Invalid bid (0) → maker default (don't cross to nothing)."""
    d = decide_tp_placement(target_state="tp", target_px=65, bid=0)
    assert d.place_px == 65 and d.post_only is True
    assert d.converted_to_taker is False


def test_zero_target_falls_back_to_maker():
    """Invalid target (0) → maker default."""
    d = decide_tp_placement(target_state="tp", target_px=0, bid=50)
    assert d.place_px == 0 and d.post_only is True
    assert d.converted_to_taker is False


def test_negative_inputs_fall_back():
    d = decide_tp_placement(target_state="tp", target_px=-5, bid=50)
    assert d.converted_to_taker is False
    d = decide_tp_placement(target_state="tp", target_px=50, bid=-3)
    assert d.converted_to_taker is False
