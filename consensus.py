# consensus.py — ConsensusLayer: Fourier-weighted multi-timeframe aggregation

from config import TF_WEIGHTS
from models import BiasResult, ConsensusSignal


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


class ConsensusLayer:
    """Aggregates BiasResults from multiple timeframes into a single ConsensusSignal.

    Uses Fourier framing: lower timeframes carry less weight (noise), higher
    timeframes carry more weight (structural trend).
    """

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        """
        Args:
            weights: Dict of timeframe -> weight. Defaults to TF_WEIGHTS from config.
        """
        self._weights: dict[str, float] = weights if weights is not None else dict(TF_WEIGHTS)

    def compute(
        self,
        results: dict[str, BiasResult],
        timestamp: int,
        weights: dict[str, float] | None = None,
    ) -> ConsensusSignal:
        """Compute consensus signal from per-TF BiasResults.

        Args:
            results:   Mapping of timeframe string to the latest BiasResult for that TF.
            timestamp: Unix ms timestamp for this signal.
            weights:   Optional weight override (e.g. AFTER_HRS_TF_WEIGHTS). If None,
                       uses the instance weights set at construction.

        Returns:
            ConsensusSignal with direction, confidence, alignment count.
        """
        effective_weights = weights if weights is not None else self._weights
        weighted_score = 0.0
        total_weight = 0.0
        tf_scores: dict[str, float] = {}

        for tf, result in results.items():
            weight = effective_weights.get(tf, 0.0)
            if weight == 0.0:
                continue

            # Signed score: positive = bullish, negative = bearish
            signed_score = result.bull_conf - result.bear_conf  # Range: -100 to +100
            tf_scores[tf] = signed_score
            weighted_score += signed_score * weight
            total_weight += weight

        if total_weight == 0.0:
            return ConsensusSignal(
                timestamp=timestamp,
                fourier_score=0.0,
                confidence=0.0,
                direction="NONE",
                tf_scores={},
                aligned_count=0,
                total_tfs=0,
                tf_results=dict(results),
            )

        fourier_score = weighted_score / total_weight
        confidence = _clamp(abs(fourier_score), 0.0, 100.0)

        if fourier_score > 0:
            direction = "CALL"
        elif fourier_score < 0:
            direction = "PUT"
        else:
            direction = "NONE"

        # Count TFs aligned with consensus direction
        aligned_count = 0
        for tf_score in tf_scores.values():
            if direction == "CALL" and tf_score > 0:
                aligned_count += 1
            elif direction == "PUT" and tf_score < 0:
                aligned_count += 1

        return ConsensusSignal(
            timestamp=timestamp,
            fourier_score=fourier_score,
            confidence=confidence,
            direction=direction,
            tf_scores=tf_scores,
            aligned_count=aligned_count,
            total_tfs=len(tf_scores),
            tf_results=dict(results),
        )
