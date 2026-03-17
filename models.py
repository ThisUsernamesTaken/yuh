# models.py — Dataclasses for the BTC Multi-Timeframe Bias Engine

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candle:
    """A single OHLCV candle."""
    timestamp: int      # Unix ms — open time of candle
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True  # True when the candle is fully closed


@dataclass
class BiasResult:
    """Output from a single-timeframe BiasEngine for one candle."""
    timeframe: str
    timestamp: int

    # Raw component values
    cycle_return_pct: float
    ema_spread_pct: float
    rsi_bias: float
    candle_pressure: float
    rel_vol_clamped: float

    # Computed
    raw_score: float
    score: float            # EMA-smoothed score
    bull_conf: float        # clamp(score, 0, 100)
    bear_conf: float        # clamp(-score, 0, 100)

    # Decision (best signal within the cycle, tracked separately)
    decided_call: bool = False
    decided_put: bool = False
    decided_conf: float = 0.0
    bars_in_cycle: int = 0


@dataclass
class ConsensusSignal:
    """Aggregated multi-timeframe signal from the ConsensusLayer."""
    timestamp: int

    fourier_score: float        # Weighted sum: -100 to +100
    confidence: float           # abs(fourier_score), clamped to 100
    direction: str              # "CALL", "PUT", or "NONE"

    tf_scores: dict[str, float] = field(default_factory=dict)  # signed score per TF
    aligned_count: int = 0       # How many TFs agree with direction
    total_tfs: int = 0

    # Breakdown of best decided values per TF
    tf_results: dict[str, BiasResult] = field(default_factory=dict)

    @property
    def bucket(self) -> int:
        """Confidence bucket: 0 (0-25), 1 (25-50), 2 (50-75), 3 (75-100)."""
        c = self.confidence
        if c < 25:
            return 0
        if c < 50:
            return 1
        if c < 75:
            return 2
        return 3

    @property
    def alignment_ratio(self) -> float:
        """Fraction of TFs aligned with consensus direction."""
        if self.total_tfs == 0:
            return 0.0
        return self.aligned_count / self.total_tfs


@dataclass
class TradeRecord:
    """A fully resolved trade for logging."""
    trade_id: int
    side: str               # "CALL" or "PUT"
    confidence: float
    bucket: int
    entry_time: int         # Unix ms
    resolve_time: int       # Unix ms
    cycle_open: float
    cycle_close: float
    is_win: bool
    stake: float
    pnl: float
    equity_after: float

    # Component values at decision time
    score: float
    bull_conf: float
    bear_conf: float
    cycle_return_pct: float
    ema_spread_pct: float
    rsi_val: float
    rel_vol: float
    candle_pressure: float

    # Consensus info
    fourier_score: Optional[float] = None
    aligned_count: Optional[int] = None
