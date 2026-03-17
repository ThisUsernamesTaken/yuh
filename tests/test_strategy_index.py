"""
Tests for the Regime Strategy Index.
Covers: session mapping, table content, bad-hour blocking, backward compat.
"""
import sys
import os
import time

# Ensure the project root is on the path for all tests in this file
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest
from regime_detector import Regime
from strategy_index import StrategyIndex, StrategyConfig, get_session, SESSIONS


# ─────────────────────────────────────────────────────────────────────────────
# get_session
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSession:
    def test_all_hours(self):
        expected = {
            **{h: "ASIA"     for h in range(0,  8)},
            **{h: "LONDON"   for h in range(8,  13)},
            **{h: "NY_OPEN"  for h in range(13, 17)},
            **{h: "NY_PRIME" for h in range(17, 21)},
            **{h: "AFTER_HRS" for h in range(21, 24)},
        }
        for hour, session in expected.items():
            assert get_session(hour) == session, f"Hour {hour} should be {session}"


# ─────────────────────────────────────────────────────────────────────────────
# StrategyIndex
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyIndex:
    @pytest.fixture
    def index(self):
        return StrategyIndex(enabled=True, block_low_sample=True, low_sample_threshold=30)

    def test_all_cells_have_config(self, index):
        regimes = [
            Regime.TRENDING_UP, Regime.TRENDING_DOWN,
            Regime.RANGING, Regime.VOLATILE, Regime.UNKNOWN,
        ]
        for regime in regimes:
            for hour in range(24):
                config = index.select(regime, hour)
                assert config is not None
                assert isinstance(config, StrategyConfig)

    def test_bad_hours_blocked(self, index):
        for hour in [9, 12, 15, 21]:
            config = index.select(Regime.RANGING, hour)
            assert config.blocked, f"Hour {hour} should be blocked"

    def test_volatile_london_blocked(self, index):
        for hour in range(8, 13):
            config = index.select(Regime.VOLATILE, hour)
            assert config.blocked, f"VOLATILE×LONDON hour {hour} should be blocked"

    def test_ranging_nyprime_aggressive(self, index):
        for hour in range(17, 21):
            config = index.select(Regime.RANGING, hour)
            assert not config.blocked
            assert config.position_scale >= 1.5
            assert abs(config.expected_wr - 71.3) < 0.1

    def test_position_scale_bounds(self, index):
        regimes = [
            Regime.TRENDING_UP, Regime.TRENDING_DOWN,
            Regime.RANGING, Regime.VOLATILE, Regime.UNKNOWN,
        ]
        for regime in regimes:
            for hour in range(24):
                config = index.select(regime, hour)
                if not config.blocked:
                    assert 0.0 < config.position_scale <= 2.0, (
                        f"{config.name} scale={config.position_scale} out of bounds"
                    )

    def test_expected_wr_matches_table(self, index):
        # Spot-check key cells
        checks = [
            (Regime.RANGING,        18, 71.3),   # NY_PRIME
            (Regime.RANGING,        10, 67.5),   # LONDON
            (Regime.TRENDING_DOWN,  14, 67.4),   # NY_OPEN
            (Regime.RANGING,        16, 66.8),   # NY_OPEN (hour 16 → NY_OPEN)
            (Regime.TRENDING_UP,     9, None),   # BAD HOUR — blocked
        ]
        for regime, hour, expected_wr in checks:
            config = index.select(regime, hour)
            if expected_wr is None:
                assert config.blocked
            else:
                assert abs(config.expected_wr - expected_wr) < 0.1, (
                    f"{config.name}: expected {expected_wr}, got {config.expected_wr}"
                )

    def test_unknown_regime_fallback(self, index):
        for hour in [0, 10, 14, 18, 22]:
            config = index.select(Regime.UNKNOWN, hour)
            # Only bad hours should be blocked; regular UNKNOWN hours should not be
            assert config.blocked or hour not in [9, 12, 15, 21]
            if not config.blocked:
                assert config.expected_wr == 63.5
                assert config.position_scale == 0.75

    def test_disabled_returns_default(self):
        disabled_index = StrategyIndex(enabled=False)
        config = disabled_index.select(Regime.VOLATILE, 10)
        assert not config.blocked  # disabled index never blocks
        assert config.name == "E-DEFAULT"

    def test_effective_wr_uses_live_when_set(self):
        config = StrategyConfig(name="test", expected_wr=63.5, live_wr=70.0)
        assert config.effective_wr == 70.0

    def test_effective_wr_uses_backtest_when_no_live(self):
        config = StrategyConfig(name="test", expected_wr=63.5)
        assert config.effective_wr == 63.5


# ─────────────────────────────────────────────────────────────────────────────
# SignalFilter backward compat & new strategy param
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalFilterWithStrategy:
    """Verify backward compatibility of SignalFilter.evaluate()."""

    def _make_signal(self):
        from models import ConsensusSignal
        return ConsensusSignal(
            timestamp=1_000_000,
            fourier_score=-70.0,
            confidence=70.0,
            direction='PUT',
            tf_scores={
                '1m': -20.0, '3m': -30.0, '5m': -50.0,
                '15m': -70.0, '1h': -90.0,
            },
            aligned_count=5,
            total_tfs=5,
        )

    def test_evaluate_without_strategy_still_works(self):
        from signal_intelligence import SignalFilter
        from regime_detector import RegimeDetector

        rd = RegimeDetector()
        filt = SignalFilter(regime_detector=rd, min_confidence=0.0, min_alignment=3)
        sig = self._make_signal()

        decision = filt.evaluate(sig, utc_hour=22)
        assert decision.approved
        assert hasattr(decision, 'strategy_name')
        assert hasattr(decision, 'expected_wr')
        assert hasattr(decision, 'position_scale')

    def test_blocked_strategy_rejects(self):
        from signal_intelligence import SignalFilter, FilterReason
        from strategy_index import StrategyConfig
        from regime_detector import RegimeDetector

        rd = RegimeDetector()
        filt = SignalFilter(regime_detector=rd, min_confidence=0.0)
        sig = self._make_signal()

        blocked = StrategyConfig(
            name="BLOCKED-TEST", blocked=True,
            expected_wr=46.0, position_scale=0.0,
        )
        decision = filt.evaluate(sig, utc_hour=22, strategy=blocked)
        assert not decision.approved
        assert decision.reason == FilterReason.BLOCKED_CELL

    def test_approved_decision_carries_strategy_fields(self):
        from signal_intelligence import SignalFilter
        from strategy_index import StrategyConfig
        from regime_detector import RegimeDetector

        rd = RegimeDetector()
        filt = SignalFilter(regime_detector=rd, min_confidence=0.0, min_alignment=3)
        sig = self._make_signal()

        strat = StrategyConfig(
            name="E-RANGING-NYPRIME", expected_wr=71.3,
            position_scale=1.5, blocked=False,
        )
        decision = filt.evaluate(sig, utc_hour=18, strategy=strat)
        assert decision.approved
        assert decision.strategy_name == "E-RANGING-NYPRIME"
        assert abs(decision.expected_wr - 71.3) < 0.1
        assert decision.position_scale == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# PositionManager position scale
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionManagerWithScale:
    def _make_decision(self, position_scale: float, expected_wr: float = 71.3):
        from signal_intelligence import FilterDecision, FilterReason
        from regime_detector import RegimeDetector
        from signal_intelligence import DivergenceState, DivergenceType

        rd = RegimeDetector()
        regime = rd.state
        div = DivergenceState(DivergenceType.NONE, [], None, None, False)
        return FilterDecision(
            approved=True, reason=FilterReason.APPROVED,
            original_confidence=70.0, adjusted_confidence=71.3,
            regime=regime, divergence=div,
            strategy_name="E-RANGING-NYPRIME",
            expected_wr=expected_wr,
            position_scale=position_scale,
        )

    def _make_signal(self):
        from models import ConsensusSignal
        return ConsensusSignal(
            timestamp=1_000_000, fourier_score=-70.0, confidence=70.0,
            direction='PUT',
            tf_scores={
                '1m': -20.0, '3m': -30.0, '5m': -50.0,
                '15m': -70.0, '1h': -90.0,
            },
            aligned_count=5, total_tfs=5,
        )

    def _make_contract(self):
        from kalshi_client import KalshiContract
        return KalshiContract(
            ticker="KXBTC15M-TEST",
            title="test",
            status="active",
            yes_ask=0.30, yes_bid=0.28,
            no_ask=0.71, no_bid=0.70,
            volume=100,
            expiry_ts=int(time.time() * 1000) + 600_000,
            result=None,
        )

    def test_position_scale_applied(self):
        from position_manager import PositionManager

        signal = self._make_signal()
        contract = self._make_contract()

        decision_1x  = self._make_decision(position_scale=1.0)
        decision_15x = self._make_decision(position_scale=1.5)

        pm1 = PositionManager(starting_equity=100.0)
        intent_1x = pm1.size_trade(signal, decision_1x, contract)

        pm2 = PositionManager(starting_equity=100.0)
        intent_15x = pm2.size_trade(signal, decision_15x, contract)

        if intent_1x is not None and intent_15x is not None:
            assert intent_15x.count >= intent_1x.count, (
                "1.5x scale should produce >= contracts than 1x"
            )

    def test_scale_zero_blocked_fallback(self):
        """A blocked cell with position_scale=0 should still produce at least 1 contract
        IF it somehow reaches size_trade (which should not happen in practice since
        BLOCKED_CELL is rejected before sizing). This tests the MIN_POSITION_SCALE clamp."""
        from position_manager import PositionManager

        signal = self._make_signal()
        contract = self._make_contract()
        # decision with position_scale=0 (blocked cells have this)
        # The MIN_POSITION_SCALE clamp (0.25) will floor it
        decision_zero = self._make_decision(position_scale=0.0)

        pm = PositionManager(starting_equity=100.0)
        intent = pm.size_trade(signal, decision_zero, contract)
        # It should either produce None (no edge) or a valid count >= 1
        if intent is not None:
            assert intent.count >= 1
