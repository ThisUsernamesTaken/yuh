"""BORED Signal — selectivity-first entry/exit signals for 15-min binaries.

Pure module. No engine dependencies, no async, no I/O. The caller passes
in a MarketSnapshot, the evaluator returns a Signal verdict. State is
tracked per-ticker for stability checks only.

Philosophy: 80%+ of windows should be BORED = no trade. The 20% we trade
must have multiple independent confirmations stacked. Better to miss
trades than to enter on noise.

Designed in response to live-session evidence (2026-05-09 PM) that the
favorite-side picker fires on weak microstructure signals at second 0
of every window — entries on 4-5c skews are coin-flips and account for
most of the session's losses. The "trade everything, manage actively"
architecture is mismatched to a market where most 15-min windows are
chop.

Entry verdict requires all of:
  - Session has aged past min_session_age_s (let market confirm)
  - BB probability is decisive (|prob - 0.5| >= bb_strong_distance)
  - At least min_sources_agreeing independent signals favor the same side
  - Composite conviction >= min_conviction
  - Signal has been stable for stability_required_ticks consecutive calls
  - Vol regime is normal (not dead, not chaos)
  - BTC has actually moved (not flat tape)

Exit verdict fires on any of:
  - BB probability drift against position >= drift_threshold
  - Position has reached near-certain (hold to settlement)
  - Pre-expiry window with positive probability
  - Vol regime broken (chaotic spike — model unreliable)

The engine (caller) is responsible for execution: when this module
returns Signal(action="enter"), the caller fires its IOC machinery; on
Signal(action="exit"), the caller does the IOC sell. This module
contains zero I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class MarketSnapshot:
    """Read-only view of market state at a single instant.

    The engine populates this from prob_engine + kalshi_ws + kalshi_tape
    + price_feed. The Signal evaluator does not reach back into engine
    state — every input it cares about is here.
    """
    # ── Brownian Bridge probability layer (primary signal) ─────────────
    bb_probability: float = 0.5     # P(YES wins at expiry), 0.0-1.0
    bb_volatility: float = 0.5      # annualized realized vol
    bb_is_ready: bool = False       # True when prob_engine has 10+ candles

    # ── Time + session ────────────────────────────────────────────────
    session_age_s: float = 0.0      # seconds since window open
    seconds_left: float = 900.0     # seconds until contract expiry

    # ── BTC underlying ────────────────────────────────────────────────
    btc_price: float = 0.0
    strike: float = 0.0             # BTC at session open
    btc_velocity_60s: float = 0.0   # $/sec, signed (over last 60s)
    btc_velocity_300s: float = 0.0  # $/sec, signed (over last 5 min)

    # ── Kalshi book microstructure ────────────────────────────────────
    yes_bid: int = 0                # cents
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0
    book_imbalance: float = 0.5     # 0..1; YES depth / (YES + NO)
    book_is_ready: bool = False

    # ── Recent tape (last 30s) ────────────────────────────────────────
    tape_yes_share_30s: float = 0.5         # YES volume / total
    tape_velocity_cps_30s: float = 0.0      # cents/sec mid change, signed
    tape_total_volume_30s: int = 0          # contracts in last 30s

    # ── Position context (set when evaluating exit) ───────────────────
    position_side: str = "none"     # "yes" / "no" / "none"
    prob_at_entry: float = 0.5      # BB prob when we entered (for drift)


@dataclass
class Signal:
    """Verdict from the signal evaluator."""
    action: str = "skip"            # "enter" / "exit" / "hold" / "skip"
    side: str = "none"              # "yes" / "no" / "none"
    conviction: float = 0.0         # 0..1 composite confidence
    sources: list = field(default_factory=list)  # which evidence agreed
    reason: str = ""                # human-readable summary

    def is_actionable(self) -> bool:
        return self.action in ("enter", "exit")


@dataclass
class BoredConfig:
    """Tunable thresholds. Defaults set conservatively."""
    # Entry hard gates
    min_session_age_s: float = 60.0          # let market confirm direction
    min_btc_move_dollars: float = 20.0       # past 5 min absolute move floor
    min_conviction: float = 0.70             # composite floor for entry
    min_sources_agreeing: int = 2            # how many of 4 must agree
    stability_required_ticks: int = 3        # consecutive ticks of same side

    # BB thresholds
    bb_strong_distance: float = 0.15         # |prob - 0.5| >= this for "strong"
    bb_drift_threshold: float = 0.15         # exit when own-side prob drops this
    near_certain_threshold: float = 0.85     # own-side prob >= this → hold

    # Vol regime
    vol_min: float = 0.10                    # below this = dead market
    vol_max: float = 2.50                    # above this = chaos
    vol_blowup_multiplier: float = 2.0       # vol_now > X * vol_at_entry → exit

    # Book confirmation
    book_imbalance_offset: float = 0.10      # |imbalance - 0.5| > this
    min_book_depth_total: int = 10           # min contracts at top of book

    # Pre-expiry
    pre_expiry_seconds: float = 90.0         # last 90s: take any positive prob


# ──────────────────────────────────────────────────────────────────────
# Evaluator
# ──────────────────────────────────────────────────────────────────────


class BoredSignal:
    """Stateful signal evaluator. Stability tracking only — no other state.

    Use as a singleton across the engine lifecycle. Call evaluate() each
    tick; reset_ticker() on window flip.
    """

    def __init__(self, config: Optional[BoredConfig] = None) -> None:
        self.config = config or BoredConfig()
        # ticker -> (last_side, consecutive_count)
        self._stability: dict = {}

    # ── Public API ────────────────────────────────────────────────────

    def evaluate(self, ticker: str, snap: MarketSnapshot) -> Signal:
        """Return Signal verdict for this snapshot.

        Branches on whether a position is open: entry path vs exit path.
        """
        if snap.position_side in ("yes", "no"):
            return self._evaluate_exit(ticker, snap)
        return self._evaluate_entry(ticker, snap)

    def reset_ticker(self, ticker: str) -> None:
        """Call on window flip / position close. Clears stability counter."""
        self._stability.pop(ticker, None)

    # ── Entry evaluation ──────────────────────────────────────────────

    def _evaluate_entry(self, ticker: str, snap: MarketSnapshot) -> Signal:
        cfg = self.config

        # Hard gates — first failure short-circuits
        gate_failures = self._check_entry_gates(snap)
        if gate_failures:
            return Signal(
                action="skip",
                side="none",
                conviction=0.0,
                sources=[],
                reason=f"gate_fail:{gate_failures[0]}",
            )

        # Gather independent sources
        sources_yes, sources_no = self._collect_sources(snap)

        # Determine candidate side
        if (
            len(sources_yes) >= cfg.min_sources_agreeing
            and len(sources_yes) > len(sources_no)
        ):
            return self._build_entry_signal(
                ticker, snap, "yes", sources_yes,
            )
        if (
            len(sources_no) >= cfg.min_sources_agreeing
            and len(sources_no) > len(sources_yes)
        ):
            return self._build_entry_signal(
                ticker, snap, "no", sources_no,
            )

        # No clear winner — BORED window
        return Signal(
            action="skip",
            side="none",
            conviction=0.0,
            sources=sources_yes + sources_no,
            reason=(
                f"bored: yes_sources={len(sources_yes)} "
                f"no_sources={len(sources_no)} "
                f"need>={cfg.min_sources_agreeing} agreeing"
            ),
        )

    def _check_entry_gates(self, snap: MarketSnapshot) -> list:
        """Return list of failed gate names; empty list = all passed."""
        cfg = self.config
        failures = []
        if snap.session_age_s < cfg.min_session_age_s:
            failures.append("session_too_young")
        if not snap.bb_is_ready:
            failures.append("bb_not_ready")
        if not snap.book_is_ready:
            failures.append("book_not_ready")
        if snap.bb_volatility < cfg.vol_min:
            failures.append("vol_too_low")
        if snap.bb_volatility > cfg.vol_max:
            failures.append("vol_too_high")
        # 5-min absolute BTC movement floor
        if abs(snap.btc_velocity_300s * 300) < cfg.min_btc_move_dollars:
            failures.append("btc_flat")
        # Book depth sanity
        if (snap.yes_bid <= 0 or snap.no_bid <= 0):
            failures.append("book_one_sided")
        return failures

    def _collect_sources(self, snap: MarketSnapshot) -> tuple:
        """Collect independent evidence sources for each side.

        Returns (sources_yes, sources_no) lists.
        """
        cfg = self.config
        sources_yes: list = []
        sources_no: list = []

        # Source 1: BB strong conviction
        bb_dist = snap.bb_probability - 0.5
        if bb_dist >= cfg.bb_strong_distance:
            sources_yes.append("bb_strong")
        elif bb_dist <= -cfg.bb_strong_distance:
            sources_no.append("bb_strong")

        # Source 2: HTF velocity (5min BTC trend)
        htf_move = snap.btc_velocity_300s * 300
        if htf_move >= cfg.min_btc_move_dollars:
            sources_yes.append("htf_aligned")
        elif htf_move <= -cfg.min_btc_move_dollars:
            sources_no.append("htf_aligned")

        # Source 3: Book imbalance
        book_offset = snap.book_imbalance - 0.5
        if book_offset >= cfg.book_imbalance_offset:
            sources_yes.append("book_confirms")
        elif book_offset <= -cfg.book_imbalance_offset:
            sources_no.append("book_confirms")

        # Source 4: Tape velocity (recent flow direction)
        if snap.tape_velocity_cps_30s > 0.05:
            sources_yes.append("tape_pressure")
        elif snap.tape_velocity_cps_30s < -0.05:
            sources_no.append("tape_pressure")

        return sources_yes, sources_no

    def _build_entry_signal(
        self,
        ticker: str,
        snap: MarketSnapshot,
        side: str,
        sources: list,
    ) -> Signal:
        cfg = self.config
        conviction = self._compute_conviction(snap, side)

        if conviction < cfg.min_conviction:
            return Signal(
                action="skip",
                side=side,
                conviction=conviction,
                sources=sources,
                reason=(
                    f"conviction_low {conviction:.2f}<"
                    f"{cfg.min_conviction:.2f}"
                ),
            )

        if not self._is_stable(ticker, side):
            return Signal(
                action="skip",
                side=side,
                conviction=conviction,
                sources=sources,
                reason="awaiting_stability",
            )

        return Signal(
            action="enter",
            side=side,
            conviction=conviction,
            sources=sources,
            reason=(
                f"{side.upper()} conv={conviction:.2f} "
                f"bb_p={snap.bb_probability:.2f} "
                f"sources={','.join(sources)}"
            ),
        )

    def _compute_conviction(
        self, snap: MarketSnapshot, side: str,
    ) -> float:
        """Composite conviction in [0, 1].

        Weights: BB 50%, HTF 20%, book 15%, tape 15%.
        BB dominates because it directly models P(side wins). Other
        sources confirm/disconfirm. Saturation tuned so bb=0.70 +
        reasonable supporting evidence clears the default 0.70 floor:
            bb=0.70 → bb_score=0.80
            bb=0.75 → bb_score=1.00
            book_imbalance=0.70 → book_score=0.80
            5-min BTC move $60 → htf_score=1.00
            tape velocity 0.30 cps → tape_score=1.00
        """
        if side == "yes":
            bb_score = max(0.0, min(1.0, (snap.bb_probability - 0.5) * 4.0))
            book_score = max(
                0.0, min(1.0, (snap.book_imbalance - 0.5) * 4.0),
            )
            tape_score = max(
                0.0, min(1.0, snap.tape_velocity_cps_30s / 0.30),
            )
            htf_signed = snap.btc_velocity_300s * 300.0
        else:  # "no"
            bb_score = max(0.0, min(1.0, (0.5 - snap.bb_probability) * 4.0))
            book_score = max(
                0.0, min(1.0, (0.5 - snap.book_imbalance) * 4.0),
            )
            tape_score = max(
                0.0, min(1.0, -snap.tape_velocity_cps_30s / 0.30),
            )
            htf_signed = -snap.btc_velocity_300s * 300.0

        htf_score = max(0.0, min(1.0, htf_signed / 60.0))

        return (
            0.50 * bb_score
            + 0.20 * htf_score
            + 0.15 * book_score
            + 0.15 * tape_score
        )

    # ── Exit evaluation ───────────────────────────────────────────────

    def _evaluate_exit(self, ticker: str, snap: MarketSnapshot) -> Signal:
        cfg = self.config
        side = snap.position_side
        if side not in ("yes", "no"):
            return Signal(action="hold", reason="no_position")

        # "Own-side" probability: P(this side wins)
        prob_now_own = (
            snap.bb_probability if side == "yes"
            else (1.0 - snap.bb_probability)
        )
        prob_entry_own = (
            snap.prob_at_entry if side == "yes"
            else (1.0 - snap.prob_at_entry)
        )

        # 1. NEAR-CERTAIN — ride to settlement
        if prob_now_own >= cfg.near_certain_threshold:
            return Signal(
                action="hold",
                side=side,
                conviction=prob_now_own,
                sources=["near_certain"],
                reason=(
                    f"near_certain own_prob={prob_now_own:.2f} "
                    f">={cfg.near_certain_threshold:.2f}"
                ),
            )

        # 2. Pre-expiry — take any positive probability
        if (
            snap.seconds_left <= cfg.pre_expiry_seconds
            and prob_now_own > 0.50
        ):
            return Signal(
                action="exit",
                side=side,
                conviction=1.0,
                sources=["pre_expiry"],
                reason=(
                    f"pre_expiry secs_left={snap.seconds_left:.0f} "
                    f"own_prob={prob_now_own:.2f}"
                ),
            )

        # 3. BB drift — model says edge has decayed
        drift = prob_entry_own - prob_now_own
        if drift >= cfg.bb_drift_threshold:
            return Signal(
                action="exit",
                side=side,
                conviction=min(1.0, drift / cfg.bb_drift_threshold),
                sources=["bb_drift"],
                reason=(
                    f"bb_drift {drift:.2f} "
                    f">={cfg.bb_drift_threshold:.2f} "
                    f"(entry={prob_entry_own:.2f} now={prob_now_own:.2f})"
                ),
            )

        # 4. Default: hold, signal still valid
        return Signal(
            action="hold",
            side=side,
            conviction=prob_now_own,
            sources=[],
            reason=(
                f"hold own_prob={prob_now_own:.2f} drift={drift:+.2f}"
            ),
        )

    # ── Stability tracker ────────────────────────────────────────────

    def _is_stable(self, ticker: str, side: str) -> bool:
        prev = self._stability.get(ticker, ("none", 0))
        prev_side, count = prev
        if prev_side == side:
            count += 1
        else:
            count = 1
        self._stability[ticker] = (side, count)
        return count >= self.config.stability_required_ticks
