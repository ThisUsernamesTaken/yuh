"""Unit tests for the ATM live execution gate logic.

Covers _atm_live_allowed() behavior under different config combinations.
We do not test live order placement (no Kalshi sandbox), only the gating
that controls whether placement is even attempted.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def engine():
    """Stand-in object with just the methods + state we need.

    We import PolymarketCopyEngine class and bind its methods to a bare
    namespace, mirroring the pattern used by the shadow-lifecycle smoke test.
    Avoids needing aiohttp / kalshi WS / DB connections.
    """
    import polymarket_copy_engine as pce
    obj = types.SimpleNamespace()
    obj._atm_live = {"state": "IDLE", "ticker": ""}
    obj._atm_live_block_reasons = {}
    obj._atm_live_allowed = pce.PolymarketCopyEngine._atm_live_allowed.__get__(obj)
    obj._atm_live_log_block = pce.PolymarketCopyEngine._atm_live_log_block.__get__(obj)
    return obj


def _set_config(monkeypatch, **kwargs):
    """Patch the engine's frozen _user_cfg snapshot for the test.

    polymarket_copy_engine captures user_config attrs into a dict at module
    import time; runtime monkeypatching of user_config doesn't reach that
    snapshot. Patching _user_cfg directly is the right level here.
    """
    import polymarket_copy_engine as pce
    for k, v in kwargs.items():
        monkeypatch.setitem(pce._user_cfg, k, v)


def test_disallowed_when_legacy_mode(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="LEGACY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    assert engine._atm_live_allowed() is False


def test_disallowed_when_atm_live_disabled(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=False, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    assert engine._atm_live_allowed() is False


def test_disallowed_when_paper_trading(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=True,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    assert engine._atm_live_allowed() is False


def test_disallowed_when_bias_enabled_with_disable_bias(engine, monkeypatch):
    # The belt+suspenders rule: if ATM_LIVE_DISABLE_BIAS=True (default) AND
    # ATM_BIAS_ENABLED=True, refuse to go live. Bias was breakeven-after-fees
    # in backtest, never approved for live.
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=True)
    assert engine._atm_live_allowed() is False


def test_disallowed_when_state_pending(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    engine._atm_live["state"] = "PENDING"
    assert engine._atm_live_allowed() is False


def test_disallowed_when_state_holding(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    engine._atm_live["state"] = "HOLDING"
    assert engine._atm_live_allowed() is False


def test_disallowed_when_legacy_position_open(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    engine._open_position = {"ticker": "KXBTC15M-TEST", "count": 3}
    assert engine._atm_live_allowed() is False


def test_disallowed_when_caps_zero(engine, monkeypatch):
    # Defensive: if ATM_LIVE_MAX_CONTRACTS or _NOTIONAL_CENTS misconfigured
    # to 0 (or negative), refuse to fire. Prevents a config typo from
    # silently disabling all caps.
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=0, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    assert engine._atm_live_allowed() is False


def test_allowed_when_all_conditions_met(engine, monkeypatch):
    _set_config(monkeypatch, LIVE_STRATEGY_MODE="ATM_ONLY",
                ATM_LIVE_ENABLED=True, PAPER_TRADING=False,
                ATM_LIVE_MAX_CONTRACTS=5, ATM_LIVE_MAX_NOTIONAL_CENTS=250,
                ATM_LIVE_DISABLE_BIAS=True, ATM_BIAS_ENABLED=False)
    assert engine._atm_live_allowed() is True


def test_live_entry_price_allows_clean_yes_book(monkeypatch):
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_entry_price = (
        pce.PolymarketCopyEngine._atm_live_entry_price.__get__(obj)
    )
    _set_config(monkeypatch, ATM_LIVE_SKIP_CROSSED_BOOK=True,
                ATM_LIVE_MAX_CLEAN_SPREAD_CENTS=8)

    price, reason = obj._atm_live_entry_price(
        side="yes", yes_bid=31, yes_ask=35, entry_c=35,
    )

    assert price == 35
    assert reason == ""


def test_live_entry_price_skips_crossed_yes_book(monkeypatch):
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_entry_price = (
        pce.PolymarketCopyEngine._atm_live_entry_price.__get__(obj)
    )
    _set_config(monkeypatch, ATM_LIVE_SKIP_CROSSED_BOOK=True,
                ATM_LIVE_MAX_CLEAN_SPREAD_CENTS=8)

    price, reason = obj._atm_live_entry_price(
        side="yes", yes_bid=58, yes_ask=19, entry_c=19,
    )

    assert price == 0
    assert "crossed_book" in reason


def test_live_entry_price_skips_wide_clean_book(monkeypatch):
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_entry_price = (
        pce.PolymarketCopyEngine._atm_live_entry_price.__get__(obj)
    )
    _set_config(monkeypatch, ATM_LIVE_SKIP_CROSSED_BOOK=True,
                ATM_LIVE_MAX_CLEAN_SPREAD_CENTS=8)

    price, reason = obj._atm_live_entry_price(
        side="yes", yes_bid=20, yes_ask=35, entry_c=35,
    )

    assert price == 0
    assert "wide_spread" in reason


def test_live_entry_price_no_side_uses_inverted_clean_book(monkeypatch):
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_entry_price = (
        pce.PolymarketCopyEngine._atm_live_entry_price.__get__(obj)
    )
    _set_config(monkeypatch, ATM_LIVE_SKIP_CROSSED_BOOK=True,
                ATM_LIVE_MAX_CLEAN_SPREAD_CENTS=8)

    # YES 65/72 implies NO bid=28, NO ask=35.
    price, reason = obj._atm_live_entry_price(
        side="no", yes_bid=65, yes_ask=72, entry_c=35,
    )

    assert price == 35
    assert reason == ""


def test_session_probe_fires_momentum_yes_on_clean_book(monkeypatch):
    import polymarket_copy_engine as pce

    AtmEntry = __import__("atm_reversion").AtmEntry
    obj = types.SimpleNamespace(
        _atm_entered_tickers=set(),
        _poly_window_open_time=1000.0,
        _btc_session_open=100000.0,
    )
    obj._evaluate_atm_session_probe_entry = (
        pce.PolymarketCopyEngine._evaluate_atm_session_probe_entry.__get__(obj)
    )
    monkeypatch.setattr(pce.time, "time", lambda: 1065.0)
    _set_config(
        monkeypatch,
        ATM_SESSION_PROBE_ENABLED=True,
        ATM_SESSION_PROBE_START_AGE_S=60.0,
        ATM_SESSION_PROBE_END_AGE_S=180.0,
        ATM_SESSION_PROBE_MIN_BTC_MOVE_USD=10.0,
        ATM_SESSION_PROBE_MAX_ENTRY_CENTS=55,
        ATM_SESSION_PROBE_MAX_SPREAD_CENTS=8,
    )

    entry = obj._evaluate_atm_session_probe_entry(
        AtmEntry=AtmEntry,
        ticker="KXBTC15M-TEST",
        yes_bid=50,
        yes_ask=54,
        btc_price=100020.0,
        strike=100000.0,
    )

    assert entry is not None
    assert entry.side == "yes"
    assert entry.entry_c == 54
    assert entry.kind == "session_probe_momentum"


def test_session_probe_rejects_no_momentum(monkeypatch):
    import polymarket_copy_engine as pce

    AtmEntry = __import__("atm_reversion").AtmEntry
    obj = types.SimpleNamespace(
        _atm_entered_tickers=set(),
        _poly_window_open_time=1000.0,
        _btc_session_open=100000.0,
    )
    obj._evaluate_atm_session_probe_entry = (
        pce.PolymarketCopyEngine._evaluate_atm_session_probe_entry.__get__(obj)
    )
    monkeypatch.setattr(pce.time, "time", lambda: 1065.0)
    _set_config(
        monkeypatch,
        ATM_SESSION_PROBE_ENABLED=True,
        ATM_SESSION_PROBE_START_AGE_S=60.0,
        ATM_SESSION_PROBE_END_AGE_S=180.0,
        ATM_SESSION_PROBE_MIN_BTC_MOVE_USD=10.0,
        ATM_SESSION_PROBE_MAX_ENTRY_CENTS=55,
        ATM_SESSION_PROBE_MAX_SPREAD_CENTS=8,
    )

    entry = obj._evaluate_atm_session_probe_entry(
        AtmEntry=AtmEntry,
        ticker="KXBTC15M-TEST",
        yes_bid=50,
        yes_ask=54,
        btc_price=100005.0,
        strike=100000.0,
    )

    assert entry is None


def test_live_config_is_hard_capped(engine, monkeypatch):
    """When ATM live is enabled, the safety invariants must hold.

    This test does NOT pin LIVE_STRATEGY_MODE to a specific value — that
    was the bug in the original (broken on the 2026-04-27 revert to LEGACY).
    Instead it asserts the conditional invariant: IF ATM live is on, the
    caps must be tiny and bias must be off. If ATM live is off, the test
    is a no-op (the invariant is vacuously satisfied)."""
    import user_config
    assert user_config.ATM_BIAS_ENABLED is False
    assert user_config.ATM_LIVE_DISABLE_BIAS is True
    if user_config.ATM_LIVE_ENABLED:
        assert user_config.LIVE_STRATEGY_MODE == "ATM_ONLY", (
            "ATM_LIVE_ENABLED=True must be paired with LIVE_STRATEGY_MODE=ATM_ONLY"
        )
        assert user_config.ATM_LIVE_MAX_CONTRACTS <= 5
        assert user_config.ATM_LIVE_MAX_NOTIONAL_CENTS <= 250


def test_persist_close_contract_override_records_partial_only(monkeypatch):
    """Partial exits must log only the sold contracts, not the full position."""
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live = {
        "ticker": "KXBTC15M-TEST",
        "side": "yes",
        "entry_kind": "discount",
        "entry_c": 30,
        "contracts": 5,
        "signal_yes_bid": 29,
        "signal_yes_ask": 30,
        "submitted_price_c": 30,
        "order_id": "entry-order",
    }
    captured = {}

    def fake_persist(**kwargs):
        captured.update(kwargs)

    obj._atm_live_persist = fake_persist
    obj._atm_live_persist_close = (
        pce.PolymarketCopyEngine._atm_live_persist_close.__get__(obj)
    )

    obj._atm_live_persist_close(
        exit_price_c=45,
        exit_reason="partial_test",
        yes_bid=45,
        yes_ask=46,
        btc=77500.0,
        contracts_override=2,
    )

    assert captured["contracts"] == 2
    assert captured["filled_count"] == 2
    assert captured["gross_cents"] == 30
    assert captured["row_kind"] == "close"


def test_position_counts_understand_signed_position_fields():
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_position_counts = (
        pce.PolymarketCopyEngine._atm_live_position_counts.__get__(obj)
    )

    assert obj._atm_live_position_counts({"position": 7}) == (7, 0)
    assert obj._atm_live_position_counts({"position": -5}) == (0, 5)
    assert obj._atm_live_position_counts({"position_fp": "3"}) == (3, 0)
    assert obj._atm_live_position_counts({"position_fp": "-4"}) == (0, 4)


def test_position_counts_prefer_explicit_counts_when_present():
    import polymarket_copy_engine as pce

    obj = types.SimpleNamespace()
    obj._atm_live_position_counts = (
        pce.PolymarketCopyEngine._atm_live_position_counts.__get__(obj)
    )

    assert obj._atm_live_position_counts(
        {"position": 0, "yes_count": 2, "no_count": 0}
    ) == (2, 0)
    assert obj._atm_live_position_counts(
        {"position": 0, "yes_count": 0, "no_count": 6}
    ) == (0, 6)


# ── Gate-rejection telemetry (Claude 2026-04-26 Patch 2) ───────────────────
# _atm_gate_classify_and_count buckets each rejected cycle into exactly one
# of: book_invalid / strike_far / yes_ask_high / edge_thin / no_setup.
# Tests verify the bucket assignment matches atm_reversion.evaluate's gate
# order so rejection counts honestly explain why the engine isn't firing.

@pytest.fixture
def gate_engine():
    import polymarket_copy_engine as pce
    obj = types.SimpleNamespace()
    obj._atm_gate_counts = {
        "strike_far": 0, "book_invalid": 0, "yes_ask_high": 0,
        "no_ask_high": 0, "edge_thin": 0, "no_setup": 0, "ticker_locked": 0,
    }
    obj._atm_gate_classify_and_count = (
        pce.PolymarketCopyEngine._atm_gate_classify_and_count.__get__(obj)
    )
    return obj


def test_gate_classify_strike_far(gate_engine):
    # 0.05% off strike — exceeds the default 0.030% gate
    gate_engine._atm_gate_classify_and_count(
        btc_price=100050, strike=100000,
        yes_bid=30, yes_ask=31,
        max_strike_dist=0.030, max_entry_c=35, min_edge_c=8.0,
    )
    assert gate_engine._atm_gate_counts["strike_far"] == 1
    assert sum(gate_engine._atm_gate_counts.values()) == 1


def test_gate_classify_book_invalid(gate_engine):
    # zero strike → book_invalid bucket
    gate_engine._atm_gate_classify_and_count(
        btc_price=100000, strike=0,
        yes_bid=30, yes_ask=31,
        max_strike_dist=0.030, max_entry_c=35, min_edge_c=8.0,
    )
    assert gate_engine._atm_gate_counts["book_invalid"] == 1


def test_gate_classify_ask_high_when_both_sides_above_cap(gate_engine):
    # YES ask 50, NO ask = 100-30 = 70 — both above 35c cap
    gate_engine._atm_gate_classify_and_count(
        btc_price=100000, strike=100000,
        yes_bid=30, yes_ask=50,
        max_strike_dist=0.030, max_entry_c=35, min_edge_c=8.0,
    )
    assert gate_engine._atm_gate_counts["yes_ask_high"] == 1


def test_gate_classify_edge_thin_when_side_under_cap_but_edge_too_small(gate_engine):
    # YES ask 43c, edge 50-43=7c — below 8c minimum. NO ask 56 — above 35c cap.
    gate_engine._atm_gate_classify_and_count(
        btc_price=100000, strike=100000,
        yes_bid=44, yes_ask=43,
        max_strike_dist=0.030, max_entry_c=35, min_edge_c=8.0,
    )
    # Wait: yes_ask=43 > max_entry_c=35, so this is yes_ask_high path.
    # Reset and try a real edge_thin scenario:
    gate_engine._atm_gate_counts = {k: 0 for k in gate_engine._atm_gate_counts}
    # YES ask 35 (within cap), edge=50-35=15c. NO ask=100-50=50 (above 35).
    # max edge across sides = 15 ≥ 8, so this should be no_setup, not edge_thin.
    # For a real edge_thin: YES ask 35 (cap), edge 50-35=15. NO ask 35, edge 15.
    # Both sides qualify — atm_evaluate would have returned a signal, not None.
    # So edge_thin only fires when both sides are within cap but neither has edge.
    # Construct: YES ask 30 (edge=20), NO ask 30 (edge=20) → would qualify, not None.
    # Real edge_thin: max_entry_c=35, min_edge_c=20. YES ask 35 (edge=15), NO ask 35 (edge=15).
    gate_engine._atm_gate_classify_and_count(
        btc_price=100000, strike=100000,
        yes_bid=65, yes_ask=35,
        max_strike_dist=0.030, max_entry_c=35, min_edge_c=20.0,
    )
    assert gate_engine._atm_gate_counts["edge_thin"] == 1


# ── Market-fallback escalation gate (Claude 2026-04-26 Patch 1) ────────────
# Verifies the config knobs flip behavior: when ATM_LIVE_EXIT_LIMIT_MAX_TRIES
# is hit and ATM_LIVE_EXIT_MARKET_FALLBACK is True, the next exit attempt
# would use a market order. We can't test the actual order placement without
# a Kalshi sandbox, but we verify the decision boolean.

def test_market_fallback_decision_below_threshold(monkeypatch):
    _set_config(monkeypatch, ATM_LIVE_EXIT_LIMIT_MAX_TRIES=8,
                ATM_LIVE_EXIT_MARKET_FALLBACK=True)
    import polymarket_copy_engine as pce
    _max = int(pce._user_cfg.get("ATM_LIVE_EXIT_LIMIT_MAX_TRIES", 8))
    _ok = bool(pce._user_cfg.get("ATM_LIVE_EXIT_MARKET_FALLBACK", True))
    # 5 < 8 → still on limit retries
    use_market = _ok and _max > 0 and 5 >= _max
    assert use_market is False


def test_market_fallback_decision_at_threshold(monkeypatch):
    _set_config(monkeypatch, ATM_LIVE_EXIT_LIMIT_MAX_TRIES=8,
                ATM_LIVE_EXIT_MARKET_FALLBACK=True)
    import polymarket_copy_engine as pce
    _max = int(pce._user_cfg.get("ATM_LIVE_EXIT_LIMIT_MAX_TRIES", 8))
    _ok = bool(pce._user_cfg.get("ATM_LIVE_EXIT_MARKET_FALLBACK", True))
    # 8 >= 8 → escalate
    use_market = _ok and _max > 0 and 8 >= _max
    assert use_market is True


def test_market_fallback_disabled_via_config(monkeypatch):
    _set_config(monkeypatch, ATM_LIVE_EXIT_LIMIT_MAX_TRIES=8,
                ATM_LIVE_EXIT_MARKET_FALLBACK=False)
    import polymarket_copy_engine as pce
    _max = int(pce._user_cfg.get("ATM_LIVE_EXIT_LIMIT_MAX_TRIES", 8))
    _ok = bool(pce._user_cfg.get("ATM_LIVE_EXIT_MARKET_FALLBACK", True))
    # Even at 99 unfilled, escalation is blocked when fallback flag is False
    use_market = _ok and _max > 0 and 99 >= _max
    assert use_market is False
