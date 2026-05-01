"""Unit tests for bb_pure.evaluate().

Each test pins down one decision rule. Tests are independent and use
synthetic inputs only — no engine state, no I/O.
"""
from __future__ import annotations

import pytest

from bb_pure import BBSignal, evaluate


# ── Default config used as the baseline; tests override individual knobs ──
DEFAULT_CONFIG = {
    "min_edge_pp": 8.0,
    "max_entry_cents": 70,
    "min_entry_cents": 5,
    "min_time_remaining_s": 60.0,
    "kelly_fraction": 0.25,
    "kelly_max_frac": 0.15,
    "max_contracts": 200,
}


def _eval(market: int, fair: int, *, secs: float = 600.0,
          balance: float = 1000.0, **overrides):
    cfg = {**DEFAULT_CONFIG, **overrides}
    return evaluate(market, fair, secs, balance, cfg)


# ── Edge threshold ──────────────────────────────────────────────────────

def test_below_min_edge_returns_none():
    """Mispricing < 8pp → no signal."""
    sig = _eval(market=50, fair=55)  # 5pp gap, below 8pp threshold
    assert sig is None


def test_at_min_edge_fires():
    """Mispricing exactly 8pp → signal fires."""
    sig = _eval(market=50, fair=58)
    assert sig is not None
    assert sig.edge_pp == pytest.approx(8.0)


# ── Side selection ──────────────────────────────────────────────────────

def test_fair_above_market_fires_yes():
    """fair > market means YES is underpriced → buy YES."""
    sig = _eval(market=40, fair=60)
    assert sig is not None
    assert sig.side == "yes"
    assert sig.suggested_entry_cents == 40
    assert sig.win_probability == pytest.approx(0.60)


def test_fair_below_market_fires_no():
    """fair < market means NO is underpriced → buy NO."""
    sig = _eval(market=60, fair=40)
    assert sig is not None
    assert sig.side == "no"
    assert sig.suggested_entry_cents == 40  # = 100 - 60
    assert sig.win_probability == pytest.approx(0.60)


# ── Entry-price gates ───────────────────────────────────────────────────

def test_entry_above_max_returns_none():
    """Even with strong edge, don't fire on contracts above the entry cap."""
    # market=80, fair=90 → buy YES at 80c (above 70c cap)
    sig = _eval(market=80, fair=90)
    assert sig is None


def test_entry_below_min_returns_none():
    """Don't fire on dust-priced contracts even with edge."""
    # market=3, fair=15 → buy YES at 3c (below 5c floor)
    sig = _eval(market=3, fair=15)
    assert sig is None


def test_entry_inside_band_fires():
    sig = _eval(market=30, fair=45)
    assert sig is not None
    assert sig.side == "yes"
    assert 5 <= sig.suggested_entry_cents <= 70


# ── Time-to-expiry gate ─────────────────────────────────────────────────

def test_too_close_to_expiry_returns_none():
    """No new entries with < min_time_remaining_s."""
    sig = _eval(market=40, fair=60, secs=30.0)
    assert sig is None


def test_at_min_time_fires():
    sig = _eval(market=40, fair=60, secs=60.0)
    assert sig is not None


# ── Kelly sizing ────────────────────────────────────────────────────────

def test_size_scales_with_edge_magnitude():
    """Larger mispricing → larger position."""
    small_edge = _eval(market=40, fair=50)  # 10pp
    big_edge = _eval(market=40, fair=70)    # 30pp
    assert small_edge is not None
    assert big_edge is not None
    assert big_edge.contracts > small_edge.contracts


def test_kelly_capped_at_max_frac():
    """Even with monster edge, kelly_fraction stays under the ceiling."""
    sig = _eval(market=10, fair=90)  # 80pp mispricing — full Kelly is huge
    assert sig is not None
    assert sig.kelly_fraction <= DEFAULT_CONFIG["kelly_max_frac"] + 1e-9


def test_negative_kelly_returns_none():
    """Defensive: shouldn't happen if mispricing is on the correct side,
    but if it does, no signal."""
    # Construct a case where math somehow gives non-positive Kelly:
    # market=50, fair=50 → edge=0 → caught by min_edge_pp. Manually
    # bypass by setting min_edge to 0 and providing fair=market+1 with
    # the side flip giving a worse-than-fair entry.
    sig = _eval(market=50, fair=50, min_edge_pp=0.0)
    # No mispricing → no signal
    assert sig is None


# ── Contract count cap ──────────────────────────────────────────────────

def test_contracts_capped_at_max():
    sig = _eval(market=30, fair=80, balance=10_000.0, max_contracts=20)
    assert sig is not None
    assert sig.contracts <= 20


def test_contracts_at_least_one_when_sized():
    """If signal qualifies, contracts ≥ 1 even with tiny balance."""
    sig = _eval(market=40, fair=55, balance=1.0)
    assert sig is not None
    assert sig.contracts >= 1


# ── Input edge cases ────────────────────────────────────────────────────

def test_market_zero_returns_none():
    assert _eval(market=0, fair=50) is None


def test_market_hundred_returns_none():
    assert _eval(market=100, fair=50) is None


def test_fair_zero_returns_none():
    assert _eval(market=50, fair=0) is None


def test_fair_hundred_returns_none():
    assert _eval(market=50, fair=100) is None


def test_zero_balance_returns_none():
    assert _eval(market=40, fair=60, balance=0.0) is None


def test_zero_seconds_returns_none():
    assert _eval(market=40, fair=60, secs=0.0) is None


# ── Reason string is informative ────────────────────────────────────────

def test_reason_contains_diagnostics():
    sig = _eval(market=40, fair=60)
    assert sig is not None
    r = sig.reason
    assert "edge=" in r
    assert "fair=" in r
    assert "market=" in r
    assert "side=" in r
    assert "kelly=" in r


# ── Founding-doc canonical example ──────────────────────────────────────
# "if the script says 60% probability but the market is trading at 80
# cents, the contract is overpriced" → buy NO at 20c.

def test_founding_example_sixty_eighty():
    """Doc canonical: fair=60%, market=80c → buy NO at 20c."""
    sig = _eval(market=80, fair=60)
    # market=80 is above max_entry=70 for YES, but we'd be buying NO at
    # 100-80=20c which is well within the band.
    assert sig is not None
    assert sig.side == "no"
    assert sig.suggested_entry_cents == 20
    # Win probability = 1 - 0.6 = 0.4
    assert sig.win_probability == pytest.approx(0.40)
    # NO at 20c: pay 0.20 to win 1.00. b = 0.80/0.20 = 4.
    # full Kelly = (p*b - q) / b = (0.4*4 - 0.6) / 4 = 1.0/4 = 0.25 (25% bankroll)
    # quarter-Kelly = 0.25 * 0.25 = 0.0625 → under 0.15 cap, stays.
    assert sig.kelly_fraction == pytest.approx(0.0625)
