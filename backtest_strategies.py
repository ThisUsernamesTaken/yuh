# backtest_strategies.py -- Multi-strategy backtest over historical 1m BTC/USDT candles
#
# Usage:
#   python backtest_strategies.py
#
# Reads  : data/btc_1m_90d.csv
# Outputs: per-strategy stats + a comparison table printed to stdout
#
# The full bias engine (BiasEngine + CandleAggregator + ConsensusLayer +
# RegimeDetector) is replayed candle-by-candle, exactly as in live main.py.
# At each 15m window boundary we record the outcome (CALL wins if
# window_close > window_open) and score each strategy independently.

import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aggregator import CandleAggregator
from bias_engine import BiasEngine
from config import TF_WEIGHTS
from config_phase3 import REGIME_TIMEFRAME
from consensus import ConsensusLayer
from models import Candle, BiasResult, ConsensusSignal
from regime_detector import RegimeDetector, RegimeState, Regime


# -- Constants -----------------------------------------------------------------

DATA_PATH        = Path("data/btc_1m_90d.csv")
WINDOW_MINUTES   = 15
WINDOW_MS        = WINDOW_MINUTES * 60 * 1000
WARMUP_CANDLES   = 60   # skip trade recording for first N candles (indicator warmup)

# Strategy A: regime multiplier threshold -- below this = unfavorable regime
REGIME_MULT_MIN  = 0.5  # VOLATILE/UNKNOWN will be below this in practice
# (see RegimeState.confidence_multiplier: TRENDING = 0.8-1.0, RANGING/VOLATILE = 0.3-0.7, UNKNOWN = 0.5)

# Strategy C: minimum absolute fourier_score to trade
FOURIER_THRESH_C = 35.0


# -- Data loading --------------------------------------------------------------

def _load_candles(csv_path: Path) -> list[Candle]:
    """Load sorted 1m candles from the CSV written by fetch_history.py."""
    if not csv_path.exists():
        print(f"[backtest_strategies] ERROR: data file not found: {csv_path}", file=sys.stderr)
        print("Run fetch_history.py first to download historical candles.", file=sys.stderr)
        sys.exit(1)

    candles: list[Candle] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            # Auto-detect seconds vs ms
            if ts < 1_000_000_000_000:
                ts *= 1000
            candles.append(Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                closed=True,
            ))

    candles.sort(key=lambda c: c.timestamp)
    print(f"[backtest_strategies] Loaded {len(candles):,} candles from {csv_path}")
    return candles


# -- Per-window record ---------------------------------------------------------

@dataclass
class WindowRecord:
    """Everything known about a completed 15m window."""
    window_ts: int              # Floor-aligned open time of this window (Unix ms)
    window_open: float          # Open price of the first 1m candle in the window
    window_close: float         # Close price of the last 1m candle before next window
    outcome: str                # "CALL" (close > open) | "PUT" (close < open) | "FLAT"

    # Best consensus signal recorded inside this window (highest confidence)
    best_signal: Optional[ConsensusSignal] = None
    # Last consensus signal before window boundary (used by strategies B/E)
    last_signal: Optional[ConsensusSignal] = None
    # Regime state at the time of the best signal
    best_regime: Optional[RegimeState] = None


# -- Strategy result accumulator -----------------------------------------------

@dataclass
class StrategyStats:
    name: str
    label: str   # short label for the comparison table
    notes: str   # column tag shown in comparison table

    trades:      int = 0
    wins:        int = 0
    losses:      int = 0

    # Bull/Bear breakdown: tracks windows where outcome == CALL vs PUT
    bull_trades: int = 0
    bull_wins:   int = 0
    bear_trades: int = 0
    bear_wins:   int = 0

    total_conf:  float = 0.0   # sum of confidence values for traded windows (Avg conf)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades > 0 else 0.0

    @property
    def bull_win_rate(self) -> float:
        return (self.bull_wins / self.bull_trades * 100.0) if self.bull_trades > 0 else 0.0

    @property
    def bear_win_rate(self) -> float:
        return (self.bear_wins / self.bear_trades * 100.0) if self.bear_trades > 0 else 0.0

    @property
    def avg_conf(self) -> float:
        return (self.total_conf / self.trades) if self.trades > 0 else 0.0

    def record_trade(
        self,
        direction: str,
        outcome: str,
        confidence: float,
    ) -> None:
        """Record a single trade.

        Args:
            direction: "CALL" or "PUT" -- what the strategy bet on
            outcome:   "CALL" or "PUT" -- what actually happened
            confidence: signal confidence value (for avg conf tracking)
        """
        is_win = direction == outcome
        self.trades += 1
        self.total_conf += confidence
        if is_win:
            self.wins += 1
        else:
            self.losses += 1

        # Bull/bear split: based on market outcome
        if outcome == "CALL":
            self.bull_trades += 1
            if is_win:
                self.bull_wins += 1
        elif outcome == "PUT":
            self.bear_trades += 1
            if is_win:
                self.bear_wins += 1


# -- Strategy decision functions -----------------------------------------------

def _strategy_a(window: WindowRecord) -> Optional[str]:
    """Strategy A -- Current Live.

    Requires: confidence >= 50 AND aligned_count >= 3 AND regime mult >= 0.5.
    Direction: signal.direction.
    Uses best signal (highest confidence) in the window.
    """
    sig = window.best_signal
    if sig is None or sig.direction == "NONE":
        return None
    if sig.confidence < 50.0:
        return None
    if sig.aligned_count < 3:
        return None
    regime = window.best_regime
    if regime is not None and regime.confidence_multiplier < REGIME_MULT_MIN:
        return None
    return sig.direction


def _strategy_b(window: WindowRecord) -> Optional[str]:
    """Strategy B -- Direction Only.

    Trade every window, no filters.
    Direction: sign of signal.fourier_score (positive = CALL, negative = PUT).
    Uses last signal before window boundary.
    """
    sig = window.last_signal
    if sig is None:
        return None
    if sig.fourier_score > 0:
        return "CALL"
    elif sig.fourier_score < 0:
        return "PUT"
    return None


def _strategy_c(window: WindowRecord) -> Optional[str]:
    """Strategy C -- Fourier Threshold.

    Trade when abs(fourier_score) >= 35. No regime filter.
    Direction: sign of fourier_score.
    Uses best signal in window.
    """
    sig = window.best_signal
    if sig is None:
        return None
    if abs(sig.fourier_score) < FOURIER_THRESH_C:
        return None
    if sig.fourier_score > 0:
        return "CALL"
    elif sig.fourier_score < 0:
        return "PUT"
    return None


def _strategy_d(window: WindowRecord) -> Optional[str]:
    """Strategy D -- Relaxed.

    Requires: confidence >= 30 AND aligned_count >= 2. No regime filter.
    Direction: signal.direction.
    Uses best signal in window.
    """
    sig = window.best_signal
    if sig is None or sig.direction == "NONE":
        return None
    if sig.confidence < 30.0:
        return None
    if sig.aligned_count < 2:
        return None
    return sig.direction


def _strategy_e(window: WindowRecord) -> Optional[str]:
    """Strategy E -- TF Majority.

    Trade every window.
    CALL if aligned_count >= 3 AND majority TF scores positive.
    PUT  if aligned_count >= 3 AND majority TF scores negative.
    Else skip.
    No confidence filter.
    Uses last signal before window boundary.
    """
    sig = window.last_signal
    if sig is None:
        return None
    if sig.aligned_count < 3:
        return None
    tf_scores = list(sig.tf_scores.values())
    if not tf_scores:
        return None
    positive = sum(1 for s in tf_scores if s > 0)
    negative = sum(1 for s in tf_scores if s < 0)
    if positive > negative:
        return "CALL"
    elif negative > positive:
        return "PUT"
    return None


# -- Engine replay -------------------------------------------------------------

def _replay_candles(candles: list[Candle]) -> list[WindowRecord]:
    """Replay the full bias engine over 1m candles and collect per-window records.

    This mirrors the wiring in main.py exactly:
      - BiasEngine per TF
      - CandleAggregator for all TFs except 1m
      - ConsensusLayer
      - RegimeDetector fed on completed REGIME_TIMEFRAME candles
    """
    # -- Engine setup (identical to main.py Engine.__init__) ------------------
    bias_engines: dict[str, BiasEngine] = {tf: BiasEngine(tf) for tf in TF_WEIGHTS}
    aggregators: dict[str, CandleAggregator] = {
        tf: CandleAggregator(tf) for tf in TF_WEIGHTS if tf != "1m"
    }
    consensus     = ConsensusLayer()
    regime_det    = RegimeDetector()
    latest_results: dict[str, BiasResult] = {}

    # -- Window state tracking ------------------------------------------------
    completed_windows: list[WindowRecord] = []

    # Current open window
    current_window_ts:    Optional[int]   = None
    current_window_open:  Optional[float] = None
    last_close:           Optional[float] = None  # tracks prev candle close for boundary

    # Accumulate signals within the current open window
    best_signal_in_window:  Optional[ConsensusSignal] = None
    last_signal_in_window:  Optional[ConsensusSignal] = None
    best_regime_in_window:  Optional[RegimeState]     = None

    n_total = len(candles)

    for idx, candle in enumerate(candles):
        window_ts = (candle.timestamp // WINDOW_MS) * WINDOW_MS

        # -- Window boundary detected ------------------------------------------
        if current_window_ts is not None and window_ts != current_window_ts:
            # Close off the previous window -- window_close is the last candle's close
            # before this new window starts (i.e. last_close)
            if (
                current_window_open is not None
                and last_close is not None
                and idx >= WARMUP_CANDLES
            ):
                wo = current_window_open
                wc = last_close

                if wc > wo:
                    outcome = "CALL"
                elif wc < wo:
                    outcome = "PUT"
                else:
                    outcome = "FLAT"

                rec = WindowRecord(
                    window_ts    = current_window_ts,
                    window_open  = wo,
                    window_close = wc,
                    outcome      = outcome,
                    best_signal  = best_signal_in_window,
                    last_signal  = last_signal_in_window,
                    best_regime  = best_regime_in_window,
                )
                completed_windows.append(rec)

            # Start the new window
            current_window_ts   = window_ts
            current_window_open = candle.open
            best_signal_in_window  = None
            last_signal_in_window  = None
            best_regime_in_window  = None

        elif current_window_ts is None:
            # Very first candle
            current_window_ts   = window_ts
            current_window_open = candle.open

        # -- Update indicators (mirror of main.py _on_candle) -----------------

        # 1m engine -- always fed directly
        result_1m = bias_engines["1m"].update(candle)
        latest_results["1m"] = result_1m

        # Higher TF engines -- fed via aggregators
        for tf, agg in aggregators.items():
            completed = agg.update(candle)
            if completed is not None:
                result = bias_engines[tf].update(completed)
                latest_results[tf] = result

                # Feed regime detector on the configured TF (default: 5m)
                if tf == REGIME_TIMEFRAME:
                    regime_det.update(completed)

        # Grab current regime state (updated above if a regime-TF candle completed)
        current_regime = regime_det.state

        # Need at least one result per TF before computing consensus
        if len(latest_results) < len(TF_WEIGHTS):
            last_close = candle.close
            continue

        # -- Consensus signal -------------------------------------------------
        signal = consensus.compute(latest_results, candle.timestamp)

        # Track last signal (always update)
        last_signal_in_window = signal

        # Track best signal (highest confidence, any direction including NONE)
        if (
            best_signal_in_window is None
            or signal.confidence > best_signal_in_window.confidence
        ):
            best_signal_in_window = signal
            best_regime_in_window = current_regime

        last_close = candle.close

    # -- Handle the final open window (discard -- it's incomplete) -------------
    # We intentionally do not record it because we have no confirmed window_close.

    print(
        f"[backtest_strategies] Engine replay complete. "
        f"Completed windows: {len(completed_windows):,}  "
        f"(from {n_total:,} candles, warmup={WARMUP_CANDLES})"
    )
    return completed_windows


# -- Strategy evaluation -------------------------------------------------------

STRATEGIES: list[tuple[str, str, str]] = [
    # (full name,                        label,         notes)
    ("A -- Current Live",                 "A -- Current Live",   "strict  "),
    ("B -- Direction Only",               "B -- Direction Only", "fluid   "),
    ("C -- Fourier >=35",                  "C -- Fourier >=35",    "balanced"),
    ("D -- Relaxed",                      "D -- Relaxed",        "moderate"),
    ("E -- TF Majority",                  "E -- TF Majority",    "fluid   "),
]

STRATEGY_FNS = [_strategy_a, _strategy_b, _strategy_c, _strategy_d, _strategy_e]


def _evaluate_strategies(windows: list[WindowRecord]) -> list[StrategyStats]:
    """Run all 5 strategies over the completed window records."""
    stats = [
        StrategyStats(name=name, label=label, notes=notes)
        for (name, label, notes) in STRATEGIES
    ]

    for window in windows:
        # Skip flat outcomes -- no clear winner, not tradeable
        if window.outcome == "FLAT":
            continue

        for stat, fn in zip(stats, STRATEGY_FNS):
            direction = fn(window)
            if direction is None:
                continue
            # Determine confidence to record for avg_conf
            # Use best_signal for A/C/D, last_signal for B/E
            if fn in (_strategy_b, _strategy_e):
                sig = window.last_signal
            else:
                sig = window.best_signal

            conf = sig.confidence if sig is not None else 0.0
            stat.record_trade(direction, window.outcome, conf)

    return stats


# -- Printing ------------------------------------------------------------------

def _print_strategy_detail(stat: StrategyStats) -> None:
    """Print the detailed block for one strategy."""
    print(f"Strategy {stat.name}")
    print(f"  Trades    : {stat.trades}")
    if stat.trades == 0:
        print("  No trades recorded.")
        return
    print(f"  Win Rate  : {stat.win_rate:.1f}%")
    print(f"  Wins/Loss : {stat.wins}W / {stat.losses}L")
    print(
        f"  Bull WR   : {stat.bull_win_rate:.1f}% "
        f"({stat.bull_wins}/{stat.bull_trades} bullish windows traded)"
    )
    print(
        f"  Bear WR   : {stat.bear_win_rate:.1f}% "
        f"({stat.bear_wins}/{stat.bear_trades} bearish windows traded)"
    )
    print(f"  Avg conf  : {stat.avg_conf:.1f}%")


def _print_comparison_table(stats_list: list[StrategyStats]) -> None:
    """Print the Unicode box-drawing comparison table."""
    # Column widths
    W_NAME  = 22
    W_TRADE = 8
    W_WR    = 10
    W_NOTES = 10

    # Helper
    def _row(name: str, trades: str, wr: str, notes: str) -> str:
        return (
            f"| {name:<{W_NAME}} | {trades:>{W_TRADE}} | {wr:>{W_WR}} | {notes:<{W_NOTES}} |"
        )

    top    = f"+{'='*(W_NAME+2)}╦{'='*(W_TRADE+2)}╦{'='*(W_WR+2)}╦{'='*(W_NOTES+2)}+"
    sep    = f"+{'='*(W_NAME+2)}+{'='*(W_TRADE+2)}+{'='*(W_WR+2)}+{'='*(W_NOTES+2)}|"
    bot    = f"+{'='*(W_NAME+2)}╩{'='*(W_TRADE+2)}╩{'='*(W_WR+2)}╩{'='*(W_NOTES+2)}+"
    header = _row("Strategy", "Trades", "Win Rate", "Notes")

    print(top)
    print(header)
    print(sep)
    for stat in stats_list:
        wr_str = f"{stat.win_rate:.1f}%" if stat.trades > 0 else "--"
        print(_row(stat.label, str(stat.trades), wr_str, stat.notes))
    print(bot)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    print("=" * 66)
    print("  BTC Bias Engine -- Multi-Strategy Backtest")
    print("=" * 66)

    # 1. Load candles
    candles = _load_candles(DATA_PATH)

    # 2. Replay the engine and collect per-window records
    windows = _replay_candles(candles)

    if not windows:
        print("No completed windows found -- not enough data.")
        sys.exit(1)

    # 3. Evaluate all strategies
    stats_list = _evaluate_strategies(windows)

    # 4. Print individual strategy details
    print()
    for stat in stats_list:
        _print_strategy_detail(stat)
        print()

    # 5. Print comparison table
    print("-- Comparison Table ----------------------------------------------")
    _print_comparison_table(stats_list)


if __name__ == "__main__":
    main()
