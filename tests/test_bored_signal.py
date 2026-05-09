"""Unit tests for bored_signal.py — the selectivity-first signal layer.

Pure logic tests. No engine, no I/O. Pinning expected behavior of the
gates and conviction composite so future refactors don't accidentally
loosen the selectivity.

Test groupings:
  - Hard entry gates (each gate's failure → action=skip)
  - Source aggregation (which evidence agrees on which side)
  - Conviction floor (sub-threshold → skip even if sources agree)
  - Stability tracking (single-tick spikes don't fire entries)
  - Exit logic (BB drift, near-certain hold, pre-expiry)
  - End-to-end happy paths (entry + exit across full lifecycle)
"""

from __future__ import annotations

import pytest

from bored_signal import BoredConfig, BoredSignal, MarketSnapshot


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _ready_snapshot(**overrides) -> MarketSnapshot:
    """A baseline snapshot that passes all hard gates with minimal signal.

    Override individual fields via kwargs to test each gate or signal.
    """
    base = dict(
        bb_probability=0.50,
        bb_volatility=0.50,
        bb_is_ready=True,
        session_age_s=120.0,
        seconds_left=780.0,
        btc_price=80_000.0,
        strike=80_000.0,
        btc_velocity_60s=0.0,
        btc_velocity_300s=0.10,   # $30 over 5 min — passes flat gate
        yes_bid=49,
        yes_ask=51,
        no_bid=49,
        no_ask=51,
        book_imbalance=0.50,
        book_is_ready=True,
        tape_yes_share_30s=0.50,
        tape_velocity_cps_30s=0.0,
        tape_total_volume_30s=20,
        position_side="none",
        prob_at_entry=0.50,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _strong_yes_snapshot() -> MarketSnapshot:
    """A snapshot that should clearly fire a YES entry (all 4 sources agree)."""
    return _ready_snapshot(
        bb_probability=0.70,        # bb_strong YES
        btc_velocity_300s=0.15,     # +$45 over 5 min, htf YES
        book_imbalance=0.70,        # book strongly YES
        tape_velocity_cps_30s=0.30, # tape pressure YES
    )


def _strong_no_snapshot() -> MarketSnapshot:
    """A snapshot that should clearly fire a NO entry."""
    return _ready_snapshot(
        bb_probability=0.30,
        btc_velocity_300s=-0.15,
        book_imbalance=0.30,
        tape_velocity_cps_30s=-0.30,
    )


# ──────────────────────────────────────────────────────────────────────
# Hard gates
# ──────────────────────────────────────────────────────────────────────


class TestHardGates:

    def test_session_too_young_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.session_age_s = 30.0  # < 60s default
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "session_too_young" in result.reason

    def test_bb_not_ready_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.bb_is_ready = False
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "bb_not_ready" in result.reason

    def test_book_not_ready_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.book_is_ready = False
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "book_not_ready" in result.reason

    def test_vol_too_low_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.bb_volatility = 0.05  # 5% annual
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "vol_too_low" in result.reason

    def test_vol_too_high_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.bb_volatility = 3.0  # 300% annual = chaos
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "vol_too_high" in result.reason

    def test_btc_flat_skips(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.btc_velocity_300s = 0.01  # $3 over 5 min
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "btc_flat" in result.reason

    def test_book_one_sided_skips(self):
        """A book with only YES bids (no NO bids) is broken — don't trade."""
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        snap.no_bid = 0
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "book_one_sided" in result.reason


# ──────────────────────────────────────────────────────────────────────
# Source aggregation
# ──────────────────────────────────────────────────────────────────────


class TestSourceAggregation:

    def test_zero_sources_is_bored(self):
        """All evidence neutral → no entry."""
        sig = BoredSignal()
        snap = _ready_snapshot()
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "bored" in result.reason

    def test_one_source_insufficient(self):
        """Single source agreement isn't enough to fire."""
        sig = BoredSignal()
        snap = _ready_snapshot(bb_probability=0.70)  # only bb_strong
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        # Should be either bored or stability-related
        assert result.action != "enter"

    def test_two_sources_yes_can_fire(self):
        """Two YES sources passing conviction can enter (after stability)."""
        sig = BoredSignal(BoredConfig(stability_required_ticks=1))
        snap = _ready_snapshot(
            bb_probability=0.75,
            btc_velocity_300s=0.20,  # $60 over 5min
        )
        result = sig.evaluate("T", snap)
        # With stability=1 and both sources strong, should be enter
        assert result.action == "enter"
        assert result.side == "yes"
        assert "bb_strong" in result.sources
        assert "htf_aligned" in result.sources

    def test_mixed_sources_skips(self):
        """If YES and NO each have 1 source, neither side qualifies."""
        sig = BoredSignal(BoredConfig(stability_required_ticks=1))
        snap = _ready_snapshot(
            bb_probability=0.70,        # bb_strong YES
            btc_velocity_300s=-0.15,    # htf NO
            book_imbalance=0.70,        # book YES
            tape_velocity_cps_30s=-0.30,  # tape NO
        )
        # 2 yes vs 2 no → tied, falls through to bored
        result = sig.evaluate("T", snap)
        assert result.action == "skip"


# ──────────────────────────────────────────────────────────────────────
# Conviction floor
# ──────────────────────────────────────────────────────────────────────


class TestConvictionFloor:

    def test_low_conviction_skips_even_with_sources(self):
        """Sources agree but composite is below conviction floor."""
        # Very weak signal: bb just over threshold, no other evidence
        sig = BoredSignal(BoredConfig(
            stability_required_ticks=1,
            min_conviction=0.95,  # very high floor
        ))
        snap = _strong_yes_snapshot()
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "conviction_low" in result.reason

    def test_strong_signal_clears_floor(self):
        sig = BoredSignal(BoredConfig(stability_required_ticks=1))
        snap = _strong_yes_snapshot()
        result = sig.evaluate("T", snap)
        assert result.action == "enter"
        assert result.conviction >= 0.70


# ──────────────────────────────────────────────────────────────────────
# Stability tracking
# ──────────────────────────────────────────────────────────────────────


class TestStability:

    def test_single_tick_does_not_fire(self):
        sig = BoredSignal()  # default stability_required_ticks=3
        snap = _strong_yes_snapshot()
        result = sig.evaluate("T", snap)
        assert result.action == "skip"
        assert "awaiting_stability" in result.reason

    def test_three_consecutive_yes_ticks_fire(self):
        sig = BoredSignal()
        snap = _strong_yes_snapshot()
        # First two — accumulating
        sig.evaluate("T", snap)
        sig.evaluate("T", snap)
        # Third — fires
        result = sig.evaluate("T", snap)
        assert result.action == "enter"
        assert result.side == "yes"

    def test_side_flip_resets_counter(self):
        sig = BoredSignal()
        # Two YES ticks
        sig.evaluate("T", _strong_yes_snapshot())
        sig.evaluate("T", _strong_yes_snapshot())
        # Then a NO tick
        result = sig.evaluate("T", _strong_no_snapshot())
        # Stability should be 1 for NO, not enter yet
        assert result.action == "skip"
        # Two more NO ticks → enter
        sig.evaluate("T", _strong_no_snapshot())
        result = sig.evaluate("T", _strong_no_snapshot())
        assert result.action == "enter"
        assert result.side == "no"

    def test_reset_ticker_clears_stability(self):
        sig = BoredSignal()
        sig.evaluate("T", _strong_yes_snapshot())
        sig.evaluate("T", _strong_yes_snapshot())
        sig.reset_ticker("T")
        # After reset, third tick is now first → should not fire
        result = sig.evaluate("T", _strong_yes_snapshot())
        assert result.action == "skip"


# ──────────────────────────────────────────────────────────────────────
# Exit evaluation
# ──────────────────────────────────────────────────────────────────────


class TestExits:

    def test_near_certain_holds(self):
        """Position with prob > 0.85 owns side → hold to settlement."""
        sig = BoredSignal()
        snap = _ready_snapshot(
            position_side="yes",
            prob_at_entry=0.60,
            bb_probability=0.90,
        )
        result = sig.evaluate("T", snap)
        assert result.action == "hold"
        assert "near_certain" in result.reason

    def test_pre_expiry_takes_profit(self):
        """Last 90s and prob > 0.5 → exit anything positive."""
        sig = BoredSignal()
        snap = _ready_snapshot(
            position_side="yes",
            prob_at_entry=0.60,
            bb_probability=0.55,
            seconds_left=60.0,
        )
        result = sig.evaluate("T", snap)
        assert result.action == "exit"
        assert "pre_expiry" in result.reason

    def test_bb_drift_triggers_exit(self):
        """Own-side probability has dropped past drift threshold → exit."""
        sig = BoredSignal()
        snap = _ready_snapshot(
            position_side="yes",
            prob_at_entry=0.65,
            bb_probability=0.45,  # drift = 0.20 ≥ 0.15
        )
        result = sig.evaluate("T", snap)
        assert result.action == "exit"
        assert "bb_drift" in result.reason

    def test_no_drift_no_exit(self):
        """Probability stable → hold."""
        sig = BoredSignal()
        snap = _ready_snapshot(
            position_side="yes",
            prob_at_entry=0.60,
            bb_probability=0.55,  # drift = 0.05 (below 0.15)
            seconds_left=300.0,    # not pre-expiry
        )
        result = sig.evaluate("T", snap)
        assert result.action == "hold"
        assert "drift" in result.reason

    def test_no_position_passthrough(self):
        """Snapshot with position_side='none' goes through entry path."""
        sig = BoredSignal()
        snap = _ready_snapshot()
        result = sig.evaluate("T", snap)
        # Bored — but action should be skip (entry path), not hold
        assert result.action == "skip"

    def test_no_side_drift_exit(self):
        """For NO position, drift is computed on (1 - bb_probability)."""
        sig = BoredSignal()
        snap = _ready_snapshot(
            position_side="no",
            prob_at_entry=0.30,  # entry: P(YES)=0.30 → P(NO)=0.70
            bb_probability=0.50,  # now: P(NO)=0.50 → drift on NO = 0.20
        )
        result = sig.evaluate("T", snap)
        assert result.action == "exit"
        assert "bb_drift" in result.reason


# ──────────────────────────────────────────────────────────────────────
# End-to-end lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestLifecycle:

    def test_entry_then_drift_exit(self):
        """Full cycle: stable YES signal → enter → BB drifts → exit."""
        sig = BoredSignal()
        # Phase 1: 3 stable strong-YES ticks → enter
        for _ in range(3):
            entry_result = sig.evaluate("T", _strong_yes_snapshot())
        assert entry_result.action == "enter"
        assert entry_result.side == "yes"
        prob_at_entry = 0.70  # (matches _strong_yes_snapshot)

        # Phase 2: BB has drifted against us
        exit_snap = _ready_snapshot(
            position_side="yes",
            prob_at_entry=prob_at_entry,
            bb_probability=0.50,  # drift = 0.20
        )
        exit_result = sig.evaluate("T", exit_snap)
        assert exit_result.action == "exit"
        assert "bb_drift" in exit_result.reason

    def test_bored_session_no_entries(self):
        """Flat-tape, neutral-book session: 100 ticks, zero entries."""
        sig = BoredSignal()
        snap = _ready_snapshot()  # all neutral
        for _ in range(100):
            result = sig.evaluate("T", snap)
            assert result.action == "skip"

    def test_window_flip_via_reset(self):
        """Reset clears stability so new window starts fresh."""
        sig = BoredSignal()
        # Build up YES stability over 3 ticks
        for _ in range(3):
            sig.evaluate("T", _strong_yes_snapshot())
        # Window flips, reset
        sig.reset_ticker("T")
        # Single tick on new window should NOT enter
        result = sig.evaluate("T", _strong_yes_snapshot())
        assert result.action == "skip"
        assert "awaiting_stability" in result.reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
