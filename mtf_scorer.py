# mtf_scorer.py — Multi-Timeframe Confluence Scorer
#
# Aggregates per-timeframe directional signals into a single confluence
# score in [-1, +1].  Positive = bullish, negative = bearish.
#
# Timeframe weights (redistributed, 4h excluded):
#   1m: 22.2%  (sourced from existing TAScorer result)
#   5m: 27.8%  (TFAnalyzer)
#   15m: 33.3% (TFAnalyzer)
#   1h: 16.7%  (TFAnalyzer)
#
# Usage:
#   scorer = MTFConfluenceScorer()
#   # Route candle closes from PriceFeedTask:
#   scorer.on_candle("5m", candle)
#   # Evaluate at signal time:
#   result = scorer.evaluate(side="yes", ta_result=ta_scorer.last_result)

import logging
from dataclasses import dataclass, field
from typing import Optional

from models import Candle
from tf_analyzer import TFAnalyzer, TFSignal

logger = logging.getLogger(__name__)

# ── Load user config ──────────────────────────────────────────────────────────
_user_cfg: dict = {}
try:
    import importlib as _il
    _uc_mod = _il.import_module("user_config")
    _user_cfg = {k: v for k, v in vars(_uc_mod).items() if not k.startswith("_")}
except ImportError:
    pass


def _uc(name: str, default):
    return _user_cfg.get(name, default)


# ── Timeframe weights (normalized to 1.0 without 4h) ─────────────────────────
# Raw: 1m=20, 5m=25, 15m=30, 1h=15  → sum=90 → normalize to 100
_RAW_WEIGHTS = {"1m": 20.0, "5m": 25.0, "15m": 30.0, "1h": 15.0}
_WEIGHT_SUM = sum(_RAW_WEIGHTS.values())
TF_WEIGHTS: dict[str, float] = {
    tf: w / _WEIGHT_SUM for tf, w in _RAW_WEIGHTS.items()
}

# Neutral contribution weight when a TF is missing or not warm
_NEUTRAL_WEIGHT_FRACTION = 0.5   # treat missing TF as 50% (no signal)

# Score thresholds for regime classification
_HIGH_CONFIDENCE_THRESHOLD = 0.60
_NORMAL_THRESHOLD = 0.30          # = MTF_MIN_CONFLUENCE default


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ConfluenceResult:
    """Output of MTFConfluenceScorer.evaluate() for one signal evaluation.

    confluence_score: -1.0 (all TFs bearish) to +1.0 (all TFs bullish)
    confluence_regime: human-readable tag for logging/DB
    side: "yes" or "no" (the side being evaluated)
    size_multiplier: 1.0 (NORMAL) or 1.5 (HIGH_CONFIDENCE); always 1.0 in shadow mode
    action: "NO_TRADE", "NORMAL", or "HIGH_CONFIDENCE"
    tf_signals: individual TFSignal per timeframe (None if unavailable)
    reasoning: compact log string
    """
    confluence_score: float
    confluence_regime: str
    side: str
    size_multiplier: float
    action: str                              # "NO_TRADE", "NORMAL", "HIGH_CONFIDENCE"
    tf_signals: dict[str, Optional[TFSignal]] = field(default_factory=dict)
    reasoning: str = ""

    @property
    def is_aligned(self) -> bool:
        """True if the score aligns with the requested side."""
        if self.side == "yes":
            return self.confluence_score >= _NORMAL_THRESHOLD
        return self.confluence_score <= -_NORMAL_THRESHOLD

    @property
    def is_opposing(self) -> bool:
        """True if the score strongly opposes the requested side."""
        if self.side == "yes":
            return self.confluence_score <= -_NORMAL_THRESHOLD
        return self.confluence_score >= _NORMAL_THRESHOLD


# ── MTFConfluenceScorer ───────────────────────────────────────────────────────

class MTFConfluenceScorer:
    """Combines per-timeframe signals into a single confluence score.

    One instance shared across the engine's lifetime.
    Thread safety: single-threaded asyncio — no locking needed.
    """

    def __init__(self) -> None:
        # TFAnalyzer for each timeframe except 1m
        # (1m is covered by the existing TAScorer — converted at evaluate() time)
        self._analyzers: dict[str, TFAnalyzer] = {
            "5m":  TFAnalyzer("5m"),
            "15m": TFAnalyzer("15m"),
            "1h":  TFAnalyzer("1h"),
        }
        self._eval_count: int = 0

    # ── Candle routing ────────────────────────────────────────────────────────

    def on_candle(self, tf: str, candle: Candle) -> None:
        """Route a closed candle to the appropriate TFAnalyzer.

        Call this from PriceFeedTask callbacks:
            feed.register_candle_close_callback("5m", scorer.on_candle)
        """
        analyzer = self._analyzers.get(tf)
        if analyzer is not None:
            analyzer.update(candle)

    # ── Evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, side: str, ta_result=None) -> ConfluenceResult:
        """Compute MTF confluence score for the given side.

        Args:
            side:       "yes" or "no" — the Kalshi trade side
            ta_result:  TASignalResult from the existing TAScorer (1m proxy).
                        If None, the 1m weight is treated as neutral.

        Returns:
            ConfluenceResult with score, regime, size multiplier, and diagnostics.
        """
        self._eval_count += 1

        # Collect per-TF scores
        tf_scores: dict[str, Optional[float]] = {}
        tf_signals: dict[str, Optional[TFSignal]] = {}

        # ── 1m: derive from existing TAScorer result ──────────────────────
        tf_signals["1m"] = _ta_result_to_tf_signal(ta_result) if ta_result is not None else None
        if tf_signals["1m"] is not None:
            tf_scores["1m"] = tf_signals["1m"].score
        else:
            tf_scores["1m"] = None

        # ── 5m / 15m / 1h: from TFAnalyzer instances ─────────────────────
        for tf, analyzer in self._analyzers.items():
            sig = analyzer.get_signal()
            tf_signals[tf] = sig
            if sig is not None and sig.is_warm:
                tf_scores[tf] = sig.score
            else:
                tf_scores[tf] = None

        # ── Weighted average ──────────────────────────────────────────────
        weighted_sum = 0.0
        for tf, weight in TF_WEIGHTS.items():
            raw_score = tf_scores.get(tf)
            if raw_score is None:
                # Missing or cold TF: contribute neutral (0.0 * weight)
                # Use fractional weight to avoid over-penalising early warmup
                weighted_sum += 0.0
            else:
                weighted_sum += weight * raw_score

        # Normalize by the sum of weights for which we have actual data
        # This avoids dragging the score toward zero due to cold TFs
        active_weight = sum(
            w for tf, w in TF_WEIGHTS.items() if tf_scores.get(tf) is not None
        )
        if active_weight < 0.10:
            # Less than 10% of weight has real data — treat as neutral
            confluence_score = 0.0
        else:
            # Scale by active weight fraction so the magnitude remains meaningful
            confluence_score = weighted_sum / active_weight
        confluence_score = max(-1.0, min(1.0, confluence_score))

        # ── Action + size multiplier ──────────────────────────────────────
        min_conf = _uc("MTF_MIN_CONFLUENCE", _NORMAL_THRESHOLD)
        high_conf = _HIGH_CONFIDENCE_THRESHOLD
        size_mult_high = _uc("MTF_SIZE_MULTIPLIER_HIGH", 1.5)

        # Side-adjusted score: positive means aligned with the trade
        if side == "yes":
            side_score = confluence_score
        else:
            side_score = -confluence_score

        if side_score >= high_conf:
            action = "HIGH_CONFIDENCE"
            size_multiplier = size_mult_high
        elif side_score >= min_conf:
            action = "NORMAL"
            size_multiplier = 1.0
        elif abs(confluence_score) < min_conf:
            action = "NO_TRADE"      # neutral zone — no conviction either way
            size_multiplier = 1.0
        else:
            action = "NO_TRADE"      # opposing
            size_multiplier = 1.0

        # ── Regime ───────────────────────────────────────────────────────
        confluence_regime = _build_regime(confluence_score, tf_signals, action)

        # ── Reasoning string (for logs) ──────────────────────────────────
        reasoning = _build_reasoning(tf_scores, tf_signals, confluence_score, confluence_regime)

        return ConfluenceResult(
            confluence_score=confluence_score,
            confluence_regime=confluence_regime,
            side=side,
            size_multiplier=size_multiplier,
            action=action,
            tf_signals=tf_signals,
            reasoning=reasoning,
        )

    # ── Diagnostics ──────────────────────────────────────────────────────────

    @property
    def warmup_status(self) -> dict[str, bool]:
        """Return {tf: is_warm} for all managed analyzers."""
        status = {"1m": True}   # 1m is always "warm" (delegated to TAScorer)
        for tf, analyzer in self._analyzers.items():
            status[tf] = analyzer.is_warm
        return status

    def is_ready(self) -> bool:
        """True if at least two timeframes (besides 1m) are warm."""
        warm_count = sum(1 for a in self._analyzers.values() if a.is_warm)
        return warm_count >= 2


# ── TASignalResult → TFSignal conversion ─────────────────────────────────────

def _ta_result_to_tf_signal(ta) -> Optional[TFSignal]:
    """Convert a TASignalResult (from the existing TAScorer) to a TFSignal.

    Maps the 1m composite score to the -1 to +1 range used by TFAnalyzer.
    The existing score range is roughly -100 to +100 — we normalize by /100
    and clamp.
    """
    if ta is None:
        return None

    try:
        from tf_analyzer import TFSignal
        # Normalize composite_score to -1..+1
        score = max(-1.0, min(1.0, ta.composite_score / 100.0))

        # Map EMA spread to EMA cross string
        if ta.ema_spread_pct > 0.005:
            ema_cross = "bull"
        elif ta.ema_spread_pct < -0.005:
            ema_cross = "bear"
        else:
            ema_cross = "flat"

        rsi_direction = (
            "up" if ta.score_velocity > 0.5 else
            "down" if ta.score_velocity < -0.5 else
            "flat"
        )

        return TFSignal(
            timeframe="1m",
            score=score,
            regime="TA_SCORER",
            ema_cross=ema_cross,
            ema_cross_bars=0,
            rsi=ta.rsi_7,
            rsi_direction=rsi_direction,
            bb_squeeze=False,
            bb_position=0.5,
            candle_structure="normal",
            market_structure="flat",
            volume_surge=ta.rel_volume >= 1.5,
            is_warm=ta.bars_in_cycle >= 3,
        )
    except Exception as e:
        logger.debug("MTFScorer: _ta_result_to_tf_signal error: %s", e)
        return None


# ── Regime and reasoning helpers ──────────────────────────────────────────────

def _build_regime(
    score: float,
    tf_signals: dict[str, Optional[TFSignal]],
    action: str,
) -> str:
    """Build a compact regime string from the aggregate score and TF regimes."""
    if abs(score) < 0.15:
        return "NEUTRAL"

    # Check dominant regime from highest-weight TF (15m)
    sig_15m = tf_signals.get("15m")
    if sig_15m is not None and sig_15m.is_warm:
        regime_15m = sig_15m.regime
        if "SQUEEZE" in regime_15m:
            return regime_15m   # Propagate squeeze regime directly

    if score >= 0.6:
        return "IMPULSE_BULL"
    if score >= 0.3:
        return "PULLBACK_BULL" if _any_bearish_tf(tf_signals) else "TREND_BULL"
    if score <= -0.6:
        return "IMPULSE_BEAR"
    if score <= -0.3:
        return "PULLBACK_BEAR" if _any_bullish_tf(tf_signals) else "TREND_BEAR"
    return "MIXED"


def _any_bearish_tf(tf_signals: dict) -> bool:
    return any(
        s is not None and s.score < -0.2
        for s in tf_signals.values()
    )


def _any_bullish_tf(tf_signals: dict) -> bool:
    return any(
        s is not None and s.score > 0.2
        for s in tf_signals.values()
    )


def _build_reasoning(
    tf_scores: dict[str, Optional[float]],
    tf_signals: dict[str, Optional[TFSignal]],
    confluence_score: float,
    regime: str,
) -> str:
    """Build a compact log string for signal diagnostics.

    Format: "1m:+0.42(bull) 5m:+0.31(TREND_BULL) 15m:-0.12(RANGING) 1h:+0.55(TREND_BULL)"
    """
    parts = []
    for tf in ("1m", "5m", "15m", "1h"):
        raw = tf_scores.get(tf)
        sig = tf_signals.get(tf)
        if raw is None:
            parts.append(f"{tf}:cold")
        else:
            regime_tag = sig.regime if sig else "?"
            parts.append(f"{tf}:{raw:+.2f}({regime_tag})")
    return f"[{' '.join(parts)}] → {confluence_score:+.2f} {regime}"
