"""Unit tests for BtcVolumeTracker.

Covers:
- Maker-side → aggressor-side inversion at ingest
- VWAP, POC, cumulative-above/below, aggressor imbalance
- Window filtering (since_ts and window_s)
- Bucket boundary semantics
- Stale-window eviction (max_age_s)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from price_feed import BtcVolumeTracker


# ── Sanity / ingest ────────────────────────────────────────────────────────

def test_empty_tracker_returns_none_or_zero():
    t = BtcVolumeTracker()
    assert t.trade_count == 0
    assert t.is_stale is True
    assert t.total_volume() == 0.0
    assert t.vwap() is None
    assert t.point_of_control() is None
    assert t.aggressor_imbalance() is None
    assert t.cumulative_volume_above(77000) == 0.0
    assert t.cumulative_volume_below(77000) == 0.0


def test_aggressor_inverted_from_maker_buy():
    # maker_side="buy" → maker bought → taker (aggressor) sold
    t = BtcVolumeTracker()
    t.on_match(price=77000, size=1.0, maker_side="buy", ts=1000.0)
    # imbalance: 100% sell aggressor → -1.0
    assert t.aggressor_imbalance() == -1.0


def test_aggressor_inverted_from_maker_sell():
    # maker_side="sell" → maker sold → taker (aggressor) bought
    t = BtcVolumeTracker()
    t.on_match(price=77000, size=1.0, maker_side="sell", ts=1000.0)
    # imbalance: 100% buy aggressor → +1.0
    assert t.aggressor_imbalance() == 1.0


def test_zero_or_negative_inputs_ignored():
    t = BtcVolumeTracker()
    t.on_match(price=0, size=1.0, maker_side="buy", ts=1000.0)
    t.on_match(price=77000, size=0, maker_side="buy", ts=1000.0)
    t.on_match(price=-77000, size=1.0, maker_side="buy", ts=1000.0)
    assert t.trade_count == 0


# ── VWAP ────────────────────────────────────────────────────────────────────

def test_vwap_weighted_by_size():
    t = BtcVolumeTracker()
    # Big trade at 77000, small at 78000.
    # VWAP = (77000*10 + 78000*1) / 11 = 770000 + 78000 = 848000 / 11 ≈ 77090.9
    t.on_match(price=77000, size=10.0, maker_side="buy", ts=1000.0)
    t.on_match(price=78000, size=1.0, maker_side="sell", ts=1001.0)
    expected = (77000 * 10 + 78000 * 1) / 11
    assert abs(t.vwap() - expected) < 1e-6


# ── Point of control ───────────────────────────────────────────────────────

def test_poc_returns_bucket_with_most_volume():
    t = BtcVolumeTracker(bucket_dollars=10.0)
    # Three trades into bucket 77300, two into 77310
    t.on_match(price=77300.5, size=2.0, maker_side="buy", ts=1.0)
    t.on_match(price=77302.0, size=2.0, maker_side="sell", ts=2.0)
    t.on_match(price=77305.0, size=2.0, maker_side="buy", ts=3.0)
    t.on_match(price=77311.0, size=1.5, maker_side="buy", ts=4.0)
    t.on_match(price=77318.0, size=1.5, maker_side="sell", ts=5.0)
    # bucket 77300 has 6.0 BTC, bucket 77310 has 3.0 BTC
    assert t.point_of_control() == 77300.0


def test_poc_with_tiebreak_prefers_first_max():
    # If two buckets have equal volume, max() returns the first encountered.
    # We don't pin tie-break behavior, just verify it returns one of them.
    t = BtcVolumeTracker(bucket_dollars=10.0)
    t.on_match(price=77300, size=1.0, maker_side="buy", ts=1.0)
    t.on_match(price=77310, size=1.0, maker_side="buy", ts=2.0)
    poc = t.point_of_control()
    assert poc in (77300.0, 77310.0)


# ── Cumulative above/below ─────────────────────────────────────────────────

def test_cumulative_above_below_sums_correctly():
    t = BtcVolumeTracker()
    t.on_match(price=77000, size=2.0, maker_side="buy", ts=1.0)
    t.on_match(price=77500, size=3.0, maker_side="sell", ts=2.0)
    t.on_match(price=78000, size=1.0, maker_side="buy", ts=3.0)
    # Above 77400: 77500 + 78000 = 4.0 BTC
    assert t.cumulative_volume_above(77400) == 4.0
    # Below 77400: 77000 = 2.0 BTC
    assert t.cumulative_volume_below(77400) == 2.0
    # At exact price: strictly greater / strictly less
    assert t.cumulative_volume_above(77000) == 4.0  # 77500 + 78000
    assert t.cumulative_volume_below(77000) == 0.0  # nothing strictly < 77000


# ── Window filtering ───────────────────────────────────────────────────────

def test_since_ts_filter():
    t = BtcVolumeTracker(max_age_s=10000.0)
    t.on_match(price=77000, size=5.0, maker_side="buy", ts=100.0)
    t.on_match(price=78000, size=5.0, maker_side="sell", ts=200.0)
    # since_ts=150 → only the 78000 trade
    assert t.total_volume(since_ts=150.0) == 5.0
    assert t.vwap(since_ts=150.0) == 78000.0


def test_window_s_filter_uses_last_update():
    # window_s is computed as last_update - window_s. Add three trades at
    # ts=1000, 1100, 1200; last_update=1200; window_s=150 → cutoff=1050.
    # Trades at 1100 and 1200 included (ts >= 1050); trade at 1000 excluded.
    t = BtcVolumeTracker(max_age_s=10000.0)
    t.on_match(price=70000, size=1.0, maker_side="buy", ts=1000.0)
    t.on_match(price=71000, size=1.0, maker_side="buy", ts=1100.0)
    t.on_match(price=72000, size=1.0, maker_side="buy", ts=1200.0)
    assert t.total_volume(window_s=150.0) == 2.0
    assert t.total_volume(window_s=10000.0) == 3.0  # all three


def test_max_age_evicts_old_trades_at_ingest():
    # Old trades evicted when a new trade lands beyond the max_age.
    t = BtcVolumeTracker(max_age_s=100.0)
    t.on_match(price=77000, size=1.0, maker_side="buy", ts=1000.0)
    assert t.trade_count == 1
    # ts=1500 → cutoff=1400 → old trade at 1000 evicted
    t.on_match(price=78000, size=1.0, maker_side="sell", ts=1500.0)
    assert t.trade_count == 1
    # vwap reflects only the most recent
    assert t.vwap() == 78000.0


# ── Aggressor imbalance ────────────────────────────────────────────────────

def test_imbalance_balanced_returns_zero():
    t = BtcVolumeTracker()
    # Equal buy and sell aggressor volume
    t.on_match(price=77000, size=2.0, maker_side="sell", ts=1.0)  # aggressor buy
    t.on_match(price=77000, size=2.0, maker_side="buy", ts=2.0)   # aggressor sell
    assert t.aggressor_imbalance() == 0.0


def test_imbalance_skewed_buy():
    t = BtcVolumeTracker()
    t.on_match(price=77000, size=3.0, maker_side="sell", ts=1.0)  # aggressor buy
    t.on_match(price=77000, size=1.0, maker_side="buy", ts=2.0)   # aggressor sell
    # (3 - 1) / 4 = 0.5
    assert t.aggressor_imbalance() == 0.5


# ── Bucket size config ─────────────────────────────────────────────────────

def test_bucket_dollars_controls_grouping():
    # With $1 buckets, 77001 and 77002 are different.
    # With $50 buckets, 77001 and 77049 are the same.
    t1 = BtcVolumeTracker(bucket_dollars=1.0)
    t1.on_match(price=77001, size=1.0, maker_side="buy", ts=1.0)
    t1.on_match(price=77002, size=2.0, maker_side="buy", ts=2.0)
    assert t1.point_of_control() == 77002.0  # bucket 77002

    t50 = BtcVolumeTracker(bucket_dollars=50.0)
    t50.on_match(price=77001, size=1.0, maker_side="buy", ts=1.0)
    t50.on_match(price=77049, size=2.0, maker_side="buy", ts=2.0)
    # Both fall in bucket 77000 (77000-77049)
    assert t50.point_of_control() == 77000.0
    assert t50.total_volume() == 3.0
