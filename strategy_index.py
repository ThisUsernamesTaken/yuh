"""
Strategy Index — Dynamic per-regime strategy selection.
Maps each (Regime, Session) cell to the optimal StrategyConfig
based on 90-day backtest results. Falls back to Strategy E defaults.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from regime_detector import Regime

# Sessions
SESSIONS = ["ASIA", "LONDON", "NY_OPEN", "NY_PRIME", "AFTER_HRS"]
# bad hours come from config_phase3 but we import here to avoid circular
_BAD_HOURS = {9, 12, 15, 21}


def get_session(utc_hour: int) -> str:
    if 0 <= utc_hour < 8:    return "ASIA"
    elif 8 <= utc_hour < 13: return "LONDON"
    elif 13 <= utc_hour < 17: return "NY_OPEN"
    elif 17 <= utc_hour < 21: return "NY_PRIME"
    else:                     return "AFTER_HRS"


@dataclass
class StrategyConfig:
    name: str
    min_alignment: int = 3
    min_confidence: float = 0.0
    min_fourier: float = 0.0
    velocity_cap: float = 30.0
    require_vel_align: bool = True
    confirm_bars: int = 1
    expected_wr: float = 63.5        # empirical WR for this cell
    live_wr: Optional[float] = None  # rolling live WR (future use)
    position_scale: float = 1.0      # multiplier on base position size
    blocked: bool = False
    notes: str = ""

    @property
    def effective_wr(self) -> float:
        if self.live_wr is not None:
            return self.live_wr
        return self.expected_wr


class StrategyIndex:
    def __init__(
        self,
        enabled: bool = True,
        block_low_sample: bool = True,
        low_sample_threshold: int = 30,
    ):
        self._enabled = enabled
        self._block_low_sample = block_low_sample
        self._low_sample_threshold = low_sample_threshold
        # Strategy E fallback defaults
        self._default = StrategyConfig(
            name="E-DEFAULT",
            min_alignment=3, min_confidence=0.0, expected_wr=63.5,
            position_scale=0.75,
            notes="Strategy E fallback for UNKNOWN regime or unmapped cell"
        )
        self._table: dict[tuple, StrategyConfig] = {}
        self._populate()

    def select(self, regime: Regime, utc_hour: int) -> StrategyConfig:
        """Select optimal strategy for regime × session. Returns blocked config for bad hours."""
        if not self._enabled:
            return self._default

        if utc_hour in _BAD_HOURS:
            return StrategyConfig(
                name=f"BLOCKED-BAD-HOUR-{utc_hour}h",
                blocked=True, expected_wr=0.0, position_scale=0.0,
                notes=f"UTC {utc_hour}h is a historically weak hour"
            )

        session = get_session(utc_hour)
        key = (regime, session)
        return self._table.get(key, self._default)

    def _populate(self):
        def cfg(name, wr, scale, n_samples, notes="", min_align=3, min_conf=0.0,
                min_four=0.0, block=False):
            # Auto-block low-sample cells if configured
            if self._block_low_sample and n_samples < self._low_sample_threshold and not block:
                block = True
                notes = f"Low sample (n={n_samples}) — {notes}"
            return StrategyConfig(
                name=name, min_alignment=min_align, min_confidence=min_conf,
                min_fourier=min_four, expected_wr=wr, position_scale=scale,
                blocked=block, notes=notes
            )

        T = self._table

        # ── RANGING (best backtest regime; PUT signals now blocked in signal_intelligence) ──
        # Live data showed 0/4 wins before directional filter was added (all were PUT entries).
        # CALL-only entries should recover the backtest WR. Position scales reduced to 0.75
        # until live CALL data confirms, except NY_PRIME which stays at 1.0 (still aggressive
        # relative to backtest but conservative vs the previous 1.5x).
        T[(Regime.RANGING, "NY_PRIME")]  = cfg("E-RANGING-NYPRIME",  71.3, 1.0, 477, "Best cell — 71.3% WR; was 1.5x, reduced pending live CALL confirmation")
        T[(Regime.RANGING, "LONDON")]    = cfg("E-RANGING-LONDON",   67.5, 0.75, 493, "Solid cell — 67.5% WR; reduced pending live CALL confirmation")
        T[(Regime.RANGING, "NY_OPEN")]   = cfg("E-RANGING-NYOPEN",   66.8, 0.75, 391, "Solid cell — 66.8% WR; reduced pending live CALL confirmation")
        T[(Regime.RANGING, "AFTER_HRS")] = cfg("E-RANGING-AFTERHRS", 66.2, 0.75, 393, "Strong after-hours ranging — 66.2% WR; reduced pending live CALL confirmation")
        T[(Regime.RANGING, "ASIA")]      = cfg("E-RANGING-ASIA",     63.5, 0.75, 926, "Solid Asia ranging — 63.5% WR; reduced pending live CALL confirmation")

        # ── TRENDING_UP ───────────────────────────────────────────────────
        T[(Regime.TRENDING_UP, "LONDON")]    = cfg("E-TRENDUP-LONDON",   66.4, 1.0, 444, "Strong London bull trend — 66.4% WR")
        T[(Regime.TRENDING_UP, "ASIA")]      = cfg("E-TRENDUP-ASIA",     64.2, 1.0, 579, "Good Asia bull trend — 64.2% WR")
        T[(Regime.TRENDING_UP, "NY_OPEN")]   = cfg("E-TRENDUP-NYOPEN",   63.3, 1.0, 267, "Moderate NY bull trend — 63.3% WR")
        T[(Regime.TRENDING_UP, "NY_PRIME")]  = cfg("E-TRENDUP-NYPRIME",  60.3, 0.5, 277, "Marginal bull NY prime — 60.3% WR, reduced size")
        T[(Regime.TRENDING_UP, "AFTER_HRS")] = cfg("E-TRENDUP-AFTERHRS", 60.1, 0.5, 193, "Marginal bull after-hrs — 60.1% WR, reduced size")

        # ── TRENDING_DOWN ─────────────────────────────────────────────────
        T[(Regime.TRENDING_DOWN, "NY_OPEN")]   = cfg("E-TRENDDN-NYOPEN",   67.4, 1.0, 319, "Best bear cell — 67.4% WR NY-Open")
        T[(Regime.TRENDING_DOWN, "ASIA")]      = cfg("E-TRENDDN-ASIA",     59.9, 0.5, 718, "Marginal Asia bear — 59.9% WR, reduced size")
        T[(Regime.TRENDING_DOWN, "LONDON")]    = cfg("E-TRENDDN-LONDON",   59.5, 0.5, 402, "Marginal London bear — 59.5% WR, reduced size")
        T[(Regime.TRENDING_DOWN, "AFTER_HRS")] = cfg("E-TRENDDN-AFTERHRS", 58.9, 0.5, 219, "Marginal bear after-hrs — 58.9% WR, reduced size")
        T[(Regime.TRENDING_DOWN, "NY_PRIME")]  = cfg("E-TRENDDN-NYPRIME",  58.0, 0.5, 381, "Lowest reliable bear cell — 58.0% WR, reduced size")

        # ── VOLATILE ──────────────────────────────────────────────────────
        T[(Regime.VOLATILE, "LONDON")]    = cfg("BLOCKED-VOLATILE-LONDON",    46.0, 0.0,  50, block=True, notes="BLOCKED — 46% WR, only losing cell")
        T[(Regime.VOLATILE, "ASIA")]      = cfg("E-VOLATILE-ASIA",            65.5, 0.5,  58, "VOLATILE Asia — 65.5% WR but low sample (n=58), half-size")
        T[(Regime.VOLATILE, "NY_OPEN")]   = cfg("E-VOLATILE-NYOPEN",          59.1, 0.5, 230, "Volatile NY-Open — 59.1% WR, reduced size")
        T[(Regime.VOLATILE, "NY_PRIME")]  = cfg("BLOCKED-VOLATILE-NYPRIME",   69.2, 0.0,  26, block=True, notes="BLOCKED — n=26 too small to trust 69.2% WR")
        T[(Regime.VOLATILE, "AFTER_HRS")] = cfg("BLOCKED-VOLATILE-AFTERHRS", 53.3, 0.0,  30, block=True, notes="BLOCKED — n=30 borderline, 53.3% WR not reliable")

        # ── UNKNOWN ───────────────────────────────────────────────────────
        # UNKNOWN regime uses _default (Strategy E, position_scale=0.75) for all sessions
        for session in SESSIONS:
            T[(Regime.UNKNOWN, session)] = StrategyConfig(
                name=f"E-UNKNOWN-{session}",
                min_alignment=3, min_confidence=0.0, expected_wr=63.5,
                position_scale=0.75,
                notes="UNKNOWN regime — Strategy E defaults at 75% size"
            )
