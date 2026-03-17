"""
Signal Intelligence Layer
=========================
Phase 3 components that sit between the ConsensusLayer and trade execution.
These filter, score, and gate signals based on:

1. RegimeFilter     — suppress signals in unfavorable regimes
2. WinRateTracker   — rolling win rate per confidence bucket (live accuracy)
3. AlignmentFilter  — require minimum TF alignment before signaling
4. DivergenceDetector — detect TF disagreement → potential reversal/chop
5. SignalFilter     — master orchestrator: runs all checks, returns go/no-go

Usage in main.py:
    signal_filter = SignalFilter(regime_detector, ...)
    decision = signal_filter.evaluate(consensus_signal)
    if decision.approved:
        execute_trade(...)
"""

from dataclasses import dataclass, field
from collections import deque
from typing import Optional
from enum import Enum

from config import MIN_RAW_SIGNAL_CONFIDENCE
from regime_detector import RegimeDetector, RegimeState, Regime
from models import ConsensusSignal


# ─────────────────────────────────────────────
# REGIME × SESSION WIN RATE TABLE
# ─────────────────────────────────────────────
# Empirical win rates from 90-day Strategy E backtest (analyze_regime_tod.py).
# Used as a lower-bound floor on adjusted_confidence so the position manager's
# edge check uses the real historical win rate, not a discounted signal score.
#
# Sessions (UTC): Asia=00-08, London=08-13, NY-Open=13-17, NY-Prime=17-21, After-Hrs=21-24
# VOLATILE × London is blocked upstream (46% WR), so no entry here.

_REGIME_SESSION_WR: dict[tuple, float] = {
    (Regime.TRENDING_UP,   "Asia"):      64.2,
    (Regime.TRENDING_UP,   "London"):    66.4,
    (Regime.TRENDING_UP,   "NY-Open"):   63.3,
    (Regime.TRENDING_UP,   "NY-Prime"):  60.3,
    (Regime.TRENDING_UP,   "After-Hrs"): 60.1,

    (Regime.TRENDING_DOWN, "Asia"):      59.9,
    (Regime.TRENDING_DOWN, "London"):    59.5,
    (Regime.TRENDING_DOWN, "NY-Open"):   67.4,
    (Regime.TRENDING_DOWN, "NY-Prime"):  58.0,
    (Regime.TRENDING_DOWN, "After-Hrs"): 58.9,

    (Regime.RANGING,       "Asia"):      63.5,
    (Regime.RANGING,       "London"):    67.5,
    (Regime.RANGING,       "NY-Open"):   66.8,
    (Regime.RANGING,       "NY-Prime"):  71.3,
    (Regime.RANGING,       "After-Hrs"): 66.2,

    # VOLATILE: small sample sizes — use conservative values except London (blocked).
    (Regime.VOLATILE,      "Asia"):      63.5,
    (Regime.VOLATILE,      "NY-Open"):   59.1,
    (Regime.VOLATILE,      "NY-Prime"):  63.5,
    (Regime.VOLATILE,      "After-Hrs"): 55.0,
}

_STRATEGY_E_DEFAULT_WR = 63.5  # Fallback when regime/session not in table


def _session_label(utc_hour: Optional[int]) -> str:
    if utc_hour is None:
        return "Asia"
    if utc_hour < 8:
        return "Asia"
    if utc_hour < 13:
        return "London"
    if utc_hour < 17:
        return "NY-Open"
    if utc_hour < 21:
        return "NY-Prime"
    return "After-Hrs"


def regime_session_wr(regime: Regime, utc_hour: Optional[int]) -> float:
    """Look up the empirical win rate for a regime × session cell."""
    session = _session_label(utc_hour)
    return _REGIME_SESSION_WR.get((regime, session), _STRATEGY_E_DEFAULT_WR)


# ─────────────────────────────────────────────
# ROLLING WIN RATE TRACKER
# ─────────────────────────────────────────────

class WinRateTracker:
    """Tracks rolling win rate per confidence bucket in real-time.

    Maintains a sliding window of recent trade outcomes and computes
    win rate overall and per bucket. Detects when the model is degrading
    (win rate dropping below expected thresholds).

    Expected baseline win rates (from Pine Script backtest):
        Bucket 0 (0-25):   67.57%
        Bucket 1 (25-50):  75.56%
        Bucket 2 (50-75):  84.97%
        Bucket 3 (75-100): 95.82%
    """

    BASELINE_WR = {0: 67.57, 1: 75.56, 2: 84.97, 3: 95.82}

    def __init__(self, window_size: int = 100) -> None:
        """
        Args:
            window_size: Max number of recent trades to track per bucket.
        """
        self._window_size = window_size
        # Per-bucket sliding windows: deque of booleans (True=win)
        self._buckets: dict[int, deque[bool]] = {
            i: deque(maxlen=window_size) for i in range(4)
        }
        # Global window
        self._all_trades: deque[bool] = deque(maxlen=window_size * 4)

    def record(self, bucket: int, is_win: bool) -> None:
        """Record a trade outcome.

        Args:
            bucket: Confidence bucket (0-3).
            is_win: True if the trade was a win.
        """
        bucket = max(0, min(bucket, 3))
        self._buckets[bucket].append(is_win)
        self._all_trades.append(is_win)

    def win_rate(self, bucket: Optional[int] = None) -> float:
        """Get rolling win rate.

        Args:
            bucket: If specified, win rate for that bucket. If None, overall.

        Returns:
            Win rate as percentage (0-100), or 0.0 if no trades.
        """
        if bucket is not None:
            trades = self._buckets.get(bucket, deque())
        else:
            trades = self._all_trades

        if not trades:
            return 0.0
        return (sum(trades) / len(trades)) * 100.0

    def trade_count(self, bucket: Optional[int] = None) -> int:
        """Number of trades in the rolling window."""
        if bucket is not None:
            return len(self._buckets.get(bucket, deque()))
        return len(self._all_trades)

    def is_degraded(self, bucket: int, threshold_pct: float = 10.0, min_trades: int = 10) -> bool:
        """Check if a bucket's win rate has dropped significantly below baseline.

        Args:
            bucket: Confidence bucket to check.
            threshold_pct: How many percentage points below baseline triggers degradation.
            min_trades: Minimum trades needed before degradation check is meaningful.

        Returns:
            True if the bucket is underperforming its historical baseline.
        """
        if self.trade_count(bucket) < min_trades:
            return False
        baseline = self.BASELINE_WR.get(bucket, 80.0)
        return self.win_rate(bucket) < (baseline - threshold_pct)

    def any_degraded(self, threshold_pct: float = 10.0, min_trades: int = 10) -> bool:
        """Check if any bucket is degraded."""
        return any(
            self.is_degraded(b, threshold_pct, min_trades) for b in range(4)
        )

    def summary(self) -> dict:
        """Full snapshot of rolling win rates."""
        return {
            "overall": {
                "win_rate": self.win_rate(),
                "trades": self.trade_count(),
            },
            "buckets": {
                i: {
                    "win_rate": self.win_rate(i),
                    "trades": self.trade_count(i),
                    "baseline": self.BASELINE_WR.get(i, 0.0),
                    "degraded": self.is_degraded(i),
                }
                for i in range(4)
            },
        }


# ─────────────────────────────────────────────
# DIVERGENCE DETECTOR
# ─────────────────────────────────────────────

class DivergenceType(Enum):
    NONE = "NONE"               # All TFs agree
    MILD = "MILD"               # Minor disagreement (1 TF dissenting)
    MODERATE = "MODERATE"        # Multiple TFs disagree
    SEVERE = "SEVERE"           # Higher TFs oppose lower TFs (reversal signal)


@dataclass
class DivergenceState:
    """Describes the current level of TF disagreement."""
    divergence_type: DivergenceType
    dissenting_tfs: list[str]           # TFs that disagree with consensus
    higher_tf_direction: Optional[str]  # What 15m+1h say ("CALL"/"PUT"/"MIXED")
    lower_tf_direction: Optional[str]   # What 1m+3m say ("CALL"/"PUT"/"MIXED")
    is_reversal_risk: bool              # Higher TFs oppose consensus

    @property
    def should_skip(self) -> bool:
        """True if divergence is severe enough to skip the signal."""
        return self.divergence_type == DivergenceType.SEVERE or self.is_reversal_risk


class DivergenceDetector:
    """Detects disagreement between timeframes.

    Key insight: when higher TFs (15m, 1h) oppose the consensus direction
    from lower TFs, the signal is likely noise that will reverse. This is
    the single most dangerous scenario for the momentum-based engine.
    """

    # TFs classified as "higher" (structural) vs "lower" (noise-prone)
    HIGHER_TFS = {"15m", "1h"}
    LOWER_TFS = {"1m", "3m", "5m"}

    def evaluate(self, signal: ConsensusSignal) -> DivergenceState:
        """Analyze TF agreement for a consensus signal.

        Args:
            signal: The ConsensusSignal from ConsensusLayer.

        Returns:
            DivergenceState describing the level of disagreement.
        """
        if signal.direction == "NONE" or not signal.tf_scores:
            return DivergenceState(
                divergence_type=DivergenceType.NONE,
                dissenting_tfs=[],
                higher_tf_direction=None,
                lower_tf_direction=None,
                is_reversal_risk=False,
            )

        consensus_dir = signal.direction  # "CALL" or "PUT"
        dissenting: list[str] = []

        for tf, score in signal.tf_scores.items():
            if consensus_dir == "CALL" and score < 0:
                dissenting.append(tf)
            elif consensus_dir == "PUT" and score > 0:
                dissenting.append(tf)

        # Classify higher vs lower TF directions
        higher_tf_direction = self._group_direction(signal.tf_scores, self.HIGHER_TFS)
        lower_tf_direction = self._group_direction(signal.tf_scores, self.LOWER_TFS)

        # Is this a reversal risk? Higher TFs oppose consensus
        is_reversal = False
        if higher_tf_direction is not None and higher_tf_direction != "MIXED":
            if consensus_dir == "CALL" and higher_tf_direction == "PUT":
                is_reversal = True
            elif consensus_dir == "PUT" and higher_tf_direction == "CALL":
                is_reversal = True

        # Classify divergence severity
        n_dissenting = len(dissenting)
        higher_dissenting = sum(1 for tf in dissenting if tf in self.HIGHER_TFS)

        if n_dissenting == 0:
            div_type = DivergenceType.NONE
        elif higher_dissenting >= 2:
            div_type = DivergenceType.SEVERE
        elif higher_dissenting == 1 or n_dissenting >= 3:
            div_type = DivergenceType.MODERATE
        else:
            div_type = DivergenceType.MILD

        return DivergenceState(
            divergence_type=div_type,
            dissenting_tfs=dissenting,
            higher_tf_direction=higher_tf_direction,
            lower_tf_direction=lower_tf_direction,
            is_reversal_risk=is_reversal,
        )

    def _group_direction(self, tf_scores: dict[str, float], group: set[str]) -> Optional[str]:
        """Determine the overall direction of a group of TFs."""
        scores = [tf_scores[tf] for tf in group if tf in tf_scores]
        if not scores:
            return None
        bullish = sum(1 for s in scores if s > 0)
        bearish = sum(1 for s in scores if s < 0)
        if bullish == len(scores):
            return "CALL"
        if bearish == len(scores):
            return "PUT"
        return "MIXED"


# ─────────────────────────────────────────────
# SIGNAL FILTER (MASTER GATE)
# ─────────────────────────────────────────────

class FilterReason(Enum):
    APPROVED = "APPROVED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNFAVORABLE_REGIME = "UNFAVORABLE_REGIME"
    INSUFFICIENT_ALIGNMENT = "INSUFFICIENT_ALIGNMENT"
    SEVERE_DIVERGENCE = "SEVERE_DIVERGENCE"
    MODEL_DEGRADED = "MODEL_DEGRADED"
    BAD_HOUR = "BAD_HOUR"
    NO_DIRECTION = "NO_DIRECTION"
    BLOCKED_CELL = "BLOCKED_CELL"
    LOW_SAMPLE = "LOW_SAMPLE"
    DIRECTIONAL_FILTER = "DIRECTIONAL_FILTER"


@dataclass
class FilterDecision:
    """Result of the signal filter evaluation."""
    approved: bool
    reason: FilterReason
    original_confidence: float
    adjusted_confidence: float      # After regime multiplier
    regime: RegimeState
    divergence: DivergenceState
    details: str = ""
    strategy_name: str = "E-DEFAULT"     # Strategy selected for this regime × session
    expected_wr: float = 63.5            # Expected win rate from backtest table
    position_scale: float = 1.0          # Position size multiplier

    @property
    def confidence_bucket(self) -> int:
        c = self.adjusted_confidence
        if c < 25: return 0
        if c < 50: return 1
        if c < 75: return 2
        return 3


class SignalFilter:
    """Master signal gate — runs all Phase 3 checks on a consensus signal.

    Checks applied in order:
    1. Direction exists (not NONE)
    2. Minimum confidence threshold
    3. Bad hour filter (UTC 9, 15, 16)
    4. Regime check (suppress in RANGING/VOLATILE)
    5. TF alignment minimum
    6. Divergence check (skip if higher TFs oppose)
    7. Model degradation check (rolling win rate below baseline)

    All checks can be individually tuned or disabled.
    """

    # UTC hours that consistently produce losses across all TFs
    # Findings 3 & 4: 16h removed (68.5% WR = best hour), 12h+21h added (59% WR)
    BAD_HOURS = {9, 12, 15, 21}

    def __init__(
        self,
        regime_detector: RegimeDetector,
        win_rate_tracker: Optional[WinRateTracker] = None,
        min_confidence: float = 50.0,
        min_raw_confidence: float = MIN_RAW_SIGNAL_CONFIDENCE,
        min_alignment: int = 3,
        filter_bad_hours: bool = True,
        require_favorable_regime: bool = True,
        allow_volatile_trending: bool = True,
        check_model_degradation: bool = True,
    ) -> None:
        """
        Args:
            regime_detector:         Initialized RegimeDetector.
            win_rate_tracker:        Optional WinRateTracker for degradation checks.
            min_confidence:          Minimum raw confidence to consider (default 50).
            min_alignment:           Minimum TFs aligned with direction (default 3/5).
            filter_bad_hours:        Skip UTC 9, 15, 16 (default True).
            require_favorable_regime: Suppress in RANGING/VOLATILE (default True).
            allow_volatile_trending:  Allow VOLATILE if trending strength is high (default True).
            check_model_degradation:  Check rolling win rate (default True).
        """
        self._regime = regime_detector
        self._divergence = DivergenceDetector()
        self._win_tracker = win_rate_tracker or WinRateTracker()
        self._min_confidence = min_confidence
        self._min_raw_confidence = min_raw_confidence
        self._min_alignment = min_alignment
        self._filter_bad_hours = filter_bad_hours
        self._require_favorable_regime = require_favorable_regime
        self._allow_volatile_trending = allow_volatile_trending
        self._check_degradation = check_model_degradation

        # Stats
        self._total_evaluated: int = 0
        self._total_approved: int = 0
        self._rejection_counts: dict[FilterReason, int] = {r: 0 for r in FilterReason}

    def evaluate(
        self,
        signal: ConsensusSignal,
        utc_hour: Optional[int] = None,
        strategy=None,   # Optional[StrategyConfig] — duck typed to avoid circular import
    ) -> FilterDecision:
        """Run all signal intelligence checks.

        Args:
            signal:   ConsensusSignal from the ConsensusLayer.
            utc_hour: Current UTC hour (0-23). If None, bad hour filter is skipped.
            strategy: Optional StrategyConfig from StrategyIndex. If provided, its
                      thresholds override instance defaults and blocked cells are rejected.

        Returns:
            FilterDecision with approval status, adjusted confidence, and details.
        """
        self._total_evaluated += 1
        regime_state = self._regime.state
        div_state = self._divergence.evaluate(signal)

        # ── Strategy override: check blocked cell first ──
        if strategy is not None and getattr(strategy, 'blocked', False):
            self._rejection_counts[FilterReason.BLOCKED_CELL] += 1
            return FilterDecision(
                approved=False,
                reason=FilterReason.BLOCKED_CELL,
                original_confidence=signal.confidence,
                adjusted_confidence=0.0,
                regime=regime_state,
                divergence=div_state,
                details=f"Cell blocked: {strategy.name} — {strategy.notes}",
                strategy_name=strategy.name,
                expected_wr=getattr(strategy, 'expected_wr', 0.0),
                position_scale=0.0,
            )

        # Resolve thresholds — strategy overrides instance defaults
        eff_min_confidence = (
            strategy.min_confidence if strategy is not None else self._min_confidence
        )
        eff_min_alignment = (
            strategy.min_alignment if strategy is not None else self._min_alignment
        )

        # ── Check 1: Direction exists ──
        if signal.direction == "NONE":
            return self._reject(
                FilterReason.NO_DIRECTION, signal.confidence,
                regime_state, div_state, "No directional signal"
            )

        # ── Check 1b: Raw signal floor (not strategy-overridable) ──
        # Rejects noise signals where the WR floor would otherwise do all the
        # work. A 0.10% confidence signal is TF cancellation, not a real edge.
        if signal.confidence < self._min_raw_confidence:
            return self._reject(
                FilterReason.LOW_CONFIDENCE, signal.confidence,
                regime_state, div_state,
                f"Raw confidence {signal.confidence:.1f}% < floor {self._min_raw_confidence:.1f}%"
            )

        # ── Check 2: Minimum confidence (strategy-overridable) ──
        if signal.confidence < eff_min_confidence:
            return self._reject(
                FilterReason.LOW_CONFIDENCE, signal.confidence,
                regime_state, div_state,
                f"Confidence {signal.confidence:.1f} < {eff_min_confidence}"
            )

        # ── Check 3: Bad hour filter ──
        if self._filter_bad_hours and utc_hour is not None and utc_hour in self.BAD_HOURS:
            return self._reject(
                FilterReason.BAD_HOUR, signal.confidence,
                regime_state, div_state,
                f"UTC hour {utc_hour} is historically weak"
            )

        # ── Check 4: Regime filter ──
        if self._require_favorable_regime:
            # Finding 1: RANGING is our best-performing regime (67–71% WR) for CALL signals.
            # However, live data shows RANGING + PUT = 0% WR (0/4 trades, -$1.83).
            # In a ranging market price oscillates — a downmove PUT signal gets mean-reverted
            # back to YES resolution consistently. Block PUT/NO in RANGING.
            if regime_state.regime == Regime.RANGING and signal.direction == "PUT":
                return self._reject(
                    FilterReason.DIRECTIONAL_FILTER, signal.confidence,
                    regime_state, div_state,
                    "RANGING + PUT blocked: mean-reversion invalidates downside momentum (0% live WR)"
                )

            if regime_state.regime == Regime.VOLATILE:
                # Finding 5: VOLATILE × London (08–12h UTC) = 46% WR — block it.
                if utc_hour is not None and 8 <= utc_hour < 13:
                    return self._reject(
                        FilterReason.UNFAVORABLE_REGIME, signal.confidence,
                        regime_state, div_state,
                        f"VOLATILE regime during London session (UTC {utc_hour}h)"
                    )
                if not self._allow_volatile_trending:
                    return self._reject(
                        FilterReason.UNFAVORABLE_REGIME, signal.confidence,
                        regime_state, div_state,
                        f"VOLATILE regime (ATR ratio={regime_state.atr_ratio:.2f})"
                    )
                if self._allow_volatile_trending and regime_state.trending_strength < 40.0:
                    return self._reject(
                        FilterReason.UNFAVORABLE_REGIME, signal.confidence,
                        regime_state, div_state,
                        f"VOLATILE with low trend strength ({regime_state.trending_strength:.1f})"
                    )

        # ── Check 5: TF alignment ──
        if signal.aligned_count < eff_min_alignment:
            return self._reject(
                FilterReason.INSUFFICIENT_ALIGNMENT, signal.confidence,
                regime_state, div_state,
                f"Only {signal.aligned_count}/{signal.total_tfs} TFs aligned (need {eff_min_alignment})"
            )

        # ── Check 6: Divergence ──
        if div_state.should_skip:
            return self._reject(
                FilterReason.SEVERE_DIVERGENCE, signal.confidence,
                regime_state, div_state,
                f"Divergence: {div_state.divergence_type.value}, "
                f"dissenters={div_state.dissenting_tfs}, reversal_risk={div_state.is_reversal_risk}"
            )

        # ── Check 7: Model degradation ──
        if self._check_degradation and self._win_tracker.any_degraded():
            bucket = signal.bucket
            if self._win_tracker.is_degraded(bucket):
                return self._reject(
                    FilterReason.MODEL_DEGRADED, signal.confidence,
                    regime_state, div_state,
                    f"Bucket {bucket} WR degraded: "
                    f"{self._win_tracker.win_rate(bucket):.1f}% vs "
                    f"{self._win_tracker.BASELINE_WR.get(bucket, 0):.1f}% baseline"
                )

        # ── All checks passed — apply regime confidence multiplier ──
        adjusted_conf = signal.confidence * regime_state.confidence_multiplier
        # Use strategy's expected_wr if available, else fall back to regime×session lookup.
        # This ensures position sizing and edge checks reflect the actual historical WR.
        if strategy is not None:
            cell_wr = strategy.effective_wr
        else:
            cell_wr = regime_session_wr(regime_state.regime, utc_hour)
        adjusted_conf = max(cell_wr, min(adjusted_conf, 100.0))

        self._total_approved += 1
        self._rejection_counts[FilterReason.APPROVED] += 1

        return FilterDecision(
            approved=True,
            reason=FilterReason.APPROVED,
            original_confidence=signal.confidence,
            adjusted_confidence=adjusted_conf,
            regime=regime_state,
            divergence=div_state,
            details=f"Regime={regime_state.regime.value} "
                    f"(mult={regime_state.confidence_multiplier:.2f}), "
                    f"Alignment={signal.aligned_count}/{signal.total_tfs}, "
                    f"Divergence={div_state.divergence_type.value}",
            strategy_name=strategy.name if strategy is not None else "E-DEFAULT",
            expected_wr=cell_wr,
            position_scale=strategy.position_scale if strategy is not None else 1.0,
        )

    def record_outcome(self, bucket: int, is_win: bool) -> None:
        """Record a trade outcome for the rolling win rate tracker."""
        self._win_tracker.record(bucket, is_win)

    def _reject(
        self, reason: FilterReason, confidence: float,
        regime: RegimeState, divergence: DivergenceState, details: str
    ) -> FilterDecision:
        self._rejection_counts[reason] += 1
        return FilterDecision(
            approved=False,
            reason=reason,
            original_confidence=confidence,
            adjusted_confidence=0.0,
            regime=regime,
            divergence=divergence,
            details=details,
        )

    @property
    def win_rate_tracker(self) -> WinRateTracker:
        return self._win_tracker

    @property
    def stats(self) -> dict:
        """Summary of filter performance."""
        return {
            "total_evaluated": self._total_evaluated,
            "total_approved": self._total_approved,
            "approval_rate": (
                self._total_approved / self._total_evaluated * 100.0
                if self._total_evaluated > 0 else 0.0
            ),
            "rejections": {
                r.value: count for r, count in self._rejection_counts.items()
                if r != FilterReason.APPROVED
            },
            "win_rate_summary": self._win_tracker.summary(),
        }
