"""
Regime x Time-of-Day Win Rate Analysis
=======================================
Uses Strategy E (TF Majority -- 63.5% WR best performer) logic to cross-tabulate:
  - Regime (TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, UNKNOWN)
  - UTC hour bucket (Asia, London Open, Pre-NY, NY Prime, After Hours)

Output: win rates per cell + actionable findings.

Usage:
    python analyze_regime_tod.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the replay machinery from the backtest module
from backtest_strategies import _load_candles, _replay_candles, WindowRecord
from regime_detector import Regime

DATA_PATH = Path("data/btc_1m_90d.csv")

# ── Time-of-day session buckets (UTC hour, inclusive lower, exclusive upper) ──
SESSIONS = [
    ("Asia",        0,   8),   # 00-08 UTC
    ("London",      8,  13),   # 08-13 UTC  (London open + morning)
    ("NY-Open",    13,  17),   # 13-17 UTC  (NY open / London-NY overlap)
    ("NY-Prime",   17,  21),   # 17-21 UTC  (NY prime hours)
    ("After-Hrs",  21,  24),   # 21-24 UTC  (after hours / Asia pre-market)
]

REGIME_ORDER = [
    Regime.TRENDING_UP,
    Regime.TRENDING_DOWN,
    Regime.RANGING,
    Regime.VOLATILE,
    Regime.UNKNOWN,
]

REGIME_LABELS = {
    Regime.TRENDING_UP:   "TREND_UP  ",
    Regime.TRENDING_DOWN: "TREND_DOWN",
    Regime.RANGING:       "RANGING   ",
    Regime.VOLATILE:      "VOLATILE  ",
    Regime.UNKNOWN:       "UNKNOWN   ",
}


def _session_for_hour(hour: int) -> str:
    for name, lo, hi in SESSIONS:
        if lo <= hour < hi:
            return name
    return "After-Hrs"  # fallback for hour==24 edge


def _strategy_e_direction(window: WindowRecord):
    """Strategy E -- TF Majority (best performer, 63.5% WR)."""
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


# ── Cell accumulator ──────────────────────────────────────────────────────────

class Cell:
    def __init__(self):
        self.trades = 0
        self.wins = 0

    def add(self, win: bool):
        self.trades += 1
        if win:
            self.wins += 1

    @property
    def wr(self) -> float:
        return self.wins / self.trades * 100.0 if self.trades else 0.0

    def __str__(self):
        if self.trades == 0:
            return "  --  "
        return f"{self.wr:5.1f}% ({self.trades:4d})"


def analyze(windows: list[WindowRecord]):
    # regime -> session -> Cell
    matrix: dict[Regime, dict[str, Cell]] = {
        r: {s[0]: Cell() for s in SESSIONS}
        for r in REGIME_ORDER
    }

    # Also: per-hour raw data for deeper time-of-day analysis (no regime split)
    hour_cells: dict[int, Cell] = {h: Cell() for h in range(24)}

    # Volume proxy: how many windows trade per session (frequency)
    session_freq: dict[str, int] = {s[0]: 0 for s in SESSIONS}
    session_total: dict[str, int] = {s[0]: 0 for s in SESSIONS}

    skipped = 0
    traded = 0

    for window in windows:
        if window.outcome == "FLAT":
            continue

        # Regime
        regime = window.best_regime.regime if window.best_regime else Regime.UNKNOWN

        # UTC hour
        dt = datetime.fromtimestamp(window.window_ts / 1000, tz=timezone.utc)
        hour = dt.hour
        session = _session_for_hour(hour)

        session_total[session] += 1

        # Strategy E decision
        direction = _strategy_e_direction(window)
        if direction is None:
            skipped += 1
            continue

        traded += 1
        is_win = direction == window.outcome

        matrix[regime][session].add(is_win)
        hour_cells[hour].add(is_win)
        session_freq[session] += 1

    return matrix, hour_cells, session_freq, session_total, traded, skipped


def print_matrix(matrix, session_freq, session_total):
    session_names = [s[0] for s in SESSIONS]
    col_w = 18

    print()
    print("=" * 80)
    print("  REGIME x TIME-OF-DAY  |  Strategy E Win Rate  (UTC)")
    print("=" * 80)
    header = f"{'Regime':<12}" + "".join(f"{s:>{col_w}}" for s in session_names)
    print(header)
    print("-" * (12 + col_w * len(session_names)))

    for regime in REGIME_ORDER:
        row = f"{REGIME_LABELS[regime]:<12}"
        for sname in session_names:
            cell = matrix[regime][sname]
            row += f"{str(cell):>{col_w}}"
        print(row)

    print("-" * (12 + col_w * len(session_names)))
    # Totals row (all regimes combined)
    totals = {s: Cell() for s in session_names}
    for regime in REGIME_ORDER:
        for sname in session_names:
            c = matrix[regime][sname]
            totals[sname].trades += c.trades
            totals[sname].wins += c.wins

    total_row = f"{'ALL':>12}"
    for sname in session_names:
        total_row += f"{str(totals[sname]):>{col_w}}"
    print(total_row)
    print()


def print_hour_breakdown(hour_cells):
    print("=" * 50)
    print("  WIN RATE BY UTC HOUR  (Strategy E, all regimes)")
    print("=" * 50)
    print(f"{'Hour':>5}  {'WR':>7}  {'Trades':>7}")
    print("-" * 30)
    for h in range(24):
        c = hour_cells[h]
        if c.trades < 10:
            continue  # suppress low-sample hours
        print(f"  {h:02d}h   {c.wr:5.1f}%   {c.trades:6d}")
    print()


def print_findings(matrix, hour_cells):
    session_names = [s[0] for s in SESSIONS]

    print("=" * 70)
    print("  FINDINGS  (threshold: WR >= 65% with >= 50 trades)")
    print("=" * 70)

    findings = []
    for regime in REGIME_ORDER:
        for sname in session_names:
            cell = matrix[regime][sname]
            if cell.trades >= 50 and cell.wr >= 65.0:
                findings.append((regime, sname, cell.wr, cell.trades))

    if not findings:
        print("  No cell met threshold. Lowering to >= 60%...")
        for regime in REGIME_ORDER:
            for sname in session_names:
                cell = matrix[regime][sname]
                if cell.trades >= 30 and cell.wr >= 60.0:
                    findings.append((regime, sname, cell.wr, cell.trades))

    findings.sort(key=lambda x: -x[2])
    for i, (regime, session, wr, n) in enumerate(findings, 1):
        print(f"  {i}. {REGIME_LABELS[regime].strip()} x {session:<10}  WR={wr:.1f}%  n={n}")

    # Hour findings
    print()
    print("  Top hours by WR (>=50 trades):")
    hour_sorted = sorted(
        [(h, c) for h, c in hour_cells.items() if c.trades >= 50],
        key=lambda x: -x[1].wr
    )
    for h, c in hour_sorted[:8]:
        session = _session_for_hour(h)
        print(f"    {h:02d}h UTC  ({session:<10})  WR={c.wr:.1f}%  n={c.trades}")

    # Worst cells to avoid
    print()
    print("  Worst cells to skip (WR <= 45%, >= 30 trades):")
    bad = []
    for regime in REGIME_ORDER:
        for sname in session_names:
            cell = matrix[regime][sname]
            if cell.trades >= 30 and cell.wr <= 45.0:
                bad.append((regime, sname, cell.wr, cell.trades))
    bad.sort(key=lambda x: x[2])
    for regime, session, wr, n in bad[:5]:
        print(f"    {REGIME_LABELS[regime].strip()} x {session:<10}  WR={wr:.1f}%  n={n}")

    print()


def main():
    candles = _load_candles(DATA_PATH)
    windows = _replay_candles(candles)

    if not windows:
        print("No windows -- run fetch_history.py first.")
        sys.exit(1)

    print(f"Total completed windows: {len(windows):,}")
    matrix, hour_cells, session_freq, session_total, traded, skipped = analyze(windows)
    print(f"Strategy E trades: {traded:,}  |  Skipped (no signal): {skipped:,}")

    print_matrix(matrix, session_freq, session_total)
    print_hour_breakdown(hour_cells)
    print_findings(matrix, hour_cells)


if __name__ == "__main__":
    main()
