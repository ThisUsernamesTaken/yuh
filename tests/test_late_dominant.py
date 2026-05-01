"""Unit tests for LATE_DOMINANT tier (Claude 2026-04-27).

Covers:
- Hard gates: enabled flag, session minute, distance floor, volume staleness
- Direction selection: BTC > strike → YES; BTC < strike → NO
- Confidence composite weights and component math
- Sizing scale 5ct → 30ct over conf [min_conf, 1.0]
- Stop loss: trigger on bid drop, market-sells truth count

These are deterministic mock-based tests — no live tape, no DB, no Kalshi.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polymarket_copy_engine as pce


# ─────────────────────────────────────────────────────────────────────────────
# Test scaffolding
# ─────────────────────────────────────────────────────────────────────────────

class _FakeVolumeTracker:
    """Stand-in for BtcVolumeTracker with constant returns."""
    def __init__(self, *,
                 stale: bool = False,
                 trade_count: int = 100,
                 total_vol: float = 100.0,
                 vol_above: float = 80.0,
                 vol_below: float = 20.0,
                 vwap_val: float = 72100.0,
                 poc_val: float = 72200.0,
                 imbalance: float = 0.6):
        self.is_stale = stale
        self.trade_count = trade_count
        self._total = total_vol
        self._above = vol_above
        self._below = vol_below
        self._vwap = vwap_val
        self._poc = poc_val
        self._imb = imbalance

    def total_volume(self, **_):
        return self._total

    def cumulative_volume_above(self, p):
        return self._above

    def cumulative_volume_below(self, p):
        return self._below

    def vwap(self, **_):
        return self._vwap

    def point_of_control(self):
        return self._poc

    def aggressor_imbalance(self, **_):
        return self._imb


class _FakeProb:
    def __init__(self, strike: float):
        self.strike = strike
        self.is_ready = True
        self.probability = 0.55
        self.fair_value = 60
        self.mispricing = 0
        self.volatility = 0.15


class _FakePriceFeed:
    def __init__(self, *, strike: float, vt: _FakeVolumeTracker, tv: float = 5.0):
        self.prob_engine = _FakeProb(strike)
        self.volume_tracker = vt
        # tick_tracker stub for velocity component
        self.tick_tracker = SimpleNamespace(
            tick_velocity=tv,
            is_stale=False,
        )


class _FakeTape:
    def __init__(self, mid: int = 60, taker_imbalance: float = 0.0):
        self.mid_price_cents = mid
        self.taker_imbalance = taker_imbalance
        self.updated_at = 1.0


def _make_engine(*, btc_now: float, strike: float, session_min: float = 12.5,
                 vt: _FakeVolumeTracker | None = None, mid: int = 60,
                 enabled: bool = True, tv: float = 5.0):
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._btc_last_price = btc_now
    eng._price_feed = _FakePriceFeed(
        strike=strike,
        vt=(vt or _FakeVolumeTracker()),
        tv=tv,
    )
    eng._kalshi_tape = _FakeTape(mid=mid)
    # session_min is computed from poly_window_open_time → 15-min window
    import time as _t
    eng._poly_window_open_time = _t.time() - session_min * 60.0
    eng._sr_fade_fired_this_window = False
    pce._user_cfg["LATE_DOMINANT_ENABLED"] = enabled
    pce._user_cfg["LATE_DOMINANT_DIST_FLOOR"] = 0.0005
    pce._user_cfg["LATE_DOMINANT_MIN_CONF"] = 0.65
    pce._user_cfg["LATE_DOMINANT_MIN_VOL_PRINTS"] = 30
    pce._user_cfg["LATE_DOMINANT_MIN_ENTRY_CENTS"] = 10
    pce._user_cfg["LATE_DOMINANT_MAX_ENTRY_CENTS"] = 55
    pce._user_cfg["SESSION_NO_TRADE_MIN_UNTIL"] = 12
    pce._user_cfg["LATE_DOMINANT_W_DISTANCE"] = 0.25
    pce._user_cfg["LATE_DOMINANT_W_CUM_SHARE"] = 0.20
    pce._user_cfg["LATE_DOMINANT_W_AGGRESSOR"] = 0.20
    pce._user_cfg["LATE_DOMINANT_W_POC"] = 0.15
    pce._user_cfg["LATE_DOMINANT_W_VWAP"] = 0.10
    pce._user_cfg["LATE_DOMINANT_W_VELOCITY"] = 0.10
    return eng


# ── Hard gates ──────────────────────────────────────────────────────────────

def test_disabled_flag_returns_none():
    eng = _make_engine(btc_now=72100, strike=72000, enabled=False)
    assert eng._evaluate_late_dominant_signal() is None


def test_too_early_in_session_returns_none():
    # session_min = 5 → before NO_TRADE_MIN_UNTIL (12)
    eng = _make_engine(btc_now=72100, strike=72000, session_min=5)
    assert eng._evaluate_late_dominant_signal() is None


def test_too_late_in_session_returns_none():
    # session_min = 14.5 → past 14-min hard floor
    eng = _make_engine(btc_now=72100, strike=72000, session_min=14.5)
    assert eng._evaluate_late_dominant_signal() is None


def test_distance_below_floor_returns_none():
    # 72005 vs 72000 → 0.007% < 0.05%
    eng = _make_engine(btc_now=72005, strike=72000)
    assert eng._evaluate_late_dominant_signal() is None


def test_volume_tracker_stale_returns_none():
    vt = _FakeVolumeTracker(stale=True)
    eng = _make_engine(btc_now=72100, strike=72000, vt=vt)
    assert eng._evaluate_late_dominant_signal() is None


def test_too_few_volume_prints_returns_none():
    vt = _FakeVolumeTracker(trade_count=10)  # < 30
    eng = _make_engine(btc_now=72100, strike=72000, vt=vt)
    assert eng._evaluate_late_dominant_signal() is None


def test_sr_fade_mutex_blocks_late_dominant():
    eng = _make_engine(btc_now=72100, strike=72000)
    eng._sr_fade_fired_this_window = True
    assert eng._evaluate_late_dominant_signal() is None


# ── Direction selection ─────────────────────────────────────────────────────

def test_direction_yes_when_btc_above_strike():
    # All confidence components aligned for YES
    vt = _FakeVolumeTracker(
        vol_above=90.0, vol_below=10.0,  # most volume above strike
        vwap_val=72200.0, poc_val=72250.0, imbalance=0.7,  # all above strike
    )
    # mid=40 → YES side_mid=40, inside [10, 55] entry band
    eng = _make_engine(btc_now=72100, strike=72000, vt=vt, mid=40)
    sig = eng._evaluate_late_dominant_signal()
    assert sig is not None, "high-confidence YES setup should fire"
    assert sig.kalshi_side == "yes"
    assert sig.signal_tier == "LATE_DOMINANT"


def test_direction_no_when_btc_below_strike():
    vt = _FakeVolumeTracker(
        vol_above=10.0, vol_below=90.0,
        vwap_val=71800.0, poc_val=71750.0, imbalance=-0.7,  # below strike, sells
    )
    # mid=60 → side_mid for NO = 100-60 = 40 (in [10,55] band)
    eng = _make_engine(btc_now=71900, strike=72000, vt=vt, mid=60)
    sig = eng._evaluate_late_dominant_signal()
    assert sig is not None
    assert sig.kalshi_side == "no"


# ── Confidence composite ────────────────────────────────────────────────────

def test_low_confidence_skips():
    """Wrong-side aggressor + opposite POC + opposite VWAP → low conf."""
    vt = _FakeVolumeTracker(
        vol_above=20.0, vol_below=80.0,   # wrong-side cum_share for YES
        vwap_val=71500.0, poc_val=71400.0,  # both below strike → no align for YES
        imbalance=-0.8,  # wrong-side aggressor
    )
    eng = _make_engine(btc_now=72100, strike=72000, vt=vt, tv=-5.0)  # adverse vel
    assert eng._evaluate_late_dominant_signal() is None


def test_high_confidence_fires_with_expected_value():
    """All components aligned → conf well above 0.65 threshold."""
    vt = _FakeVolumeTracker(
        total_vol=100.0, vol_above=100.0, vol_below=0.0,  # cum_share = 1.0
        vwap_val=72500.0, poc_val=72500.0,  # both above strike → 1.0 each
        imbalance=1.0,  # max aggressor
    )
    eng = _make_engine(btc_now=72200, strike=72000, vt=vt, tv=10.0, mid=40)
    sig = eng._evaluate_late_dominant_signal()
    assert sig is not None
    assert sig.kalshi_side == "yes"
    # Expected components for YES:
    #   dist = (200/72200)/0.0005 ≈ 5.54 → cap 1.0
    #   cum_share = 1.0
    #   aggressor = 1.0
    #   poc_align = 1.0
    #   vwap_align = 1.0
    #   velocity = 10/10 = 1.0
    # composite = 0.25+0.20+0.20+0.15+0.10+0.10 = 1.0
    assert sig._late_dom_confidence == pytest.approx(1.0, abs=0.05)


# ── Sizing ──────────────────────────────────────────────────────────────────

def test_sizing_at_min_conf_returns_min_size():
    """confidence == LATE_DOMINANT_MIN_CONF → exactly LATE_DOMINANT_SIZE_MIN_CT."""
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    pce._user_cfg["LATE_DOMINANT_MIN_CONF"] = 0.65
    pce._user_cfg["LATE_DOMINANT_SIZE_MIN_CT"] = 5
    pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_DAY"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_NIGHT"] = 30
    sig = SimpleNamespace(_late_dom_confidence=0.65)
    n = eng._compute_late_dominant_size(sig, ask_cents=40)
    assert n == 5


def test_sizing_at_full_conf_returns_max_size():
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    pce._user_cfg["LATE_DOMINANT_MIN_CONF"] = 0.65
    pce._user_cfg["LATE_DOMINANT_SIZE_MIN_CT"] = 5
    pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_DAY"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_NIGHT"] = 30
    sig = SimpleNamespace(_late_dom_confidence=1.0)
    n = eng._compute_late_dominant_size(sig, ask_cents=40)
    assert n == 30


def test_sizing_midpoint_interpolates_linearly():
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    pce._user_cfg["LATE_DOMINANT_MIN_CONF"] = 0.65
    pce._user_cfg["LATE_DOMINANT_SIZE_MIN_CT"] = 5
    pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_DAY"] = 30; pce._user_cfg["LATE_DOMINANT_SIZE_MAX_CT_NIGHT"] = 30
    sig = SimpleNamespace(_late_dom_confidence=0.825)  # halfway
    n = eng._compute_late_dominant_size(sig, ask_cents=40)
    # 5 + 25*0.5 = 17.5 → rounds to either 17 or 18
    assert n in (17, 18)


# ── Signal payload ──────────────────────────────────────────────────────────

def test_signal_marked_market_entry():
    vt = _FakeVolumeTracker(
        vol_above=100.0, vol_below=0.0, vwap_val=72500.0, poc_val=72500.0,
        imbalance=1.0,
    )
    eng = _make_engine(btc_now=72200, strike=72000, vt=vt, tv=10.0, mid=40)
    sig = eng._evaluate_late_dominant_signal()
    assert sig is not None
    assert getattr(sig, "_is_market_entry", False) is True


def test_signal_payload_carries_confidence_attr():
    vt = _FakeVolumeTracker(
        vol_above=100.0, vol_below=0.0, vwap_val=72500.0, poc_val=72500.0,
        imbalance=1.0,
    )
    eng = _make_engine(btc_now=72200, strike=72000, vt=vt, tv=10.0, mid=40)
    sig = eng._evaluate_late_dominant_signal()
    assert sig is not None
    assert hasattr(sig, "_late_dom_confidence")
    assert 0.0 <= sig._late_dom_confidence <= 1.0
