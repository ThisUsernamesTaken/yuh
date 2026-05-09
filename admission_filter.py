"""
Admission Filter — the rejection layer.

Every signal must pass ALL 5 gates before the engine is allowed to trade.
Target: reject ~87% of opportunities (1 trade per ~2 hours / 8 windows).
Each gate returns (passed: bool, reason: str).

Philosophy: build a bot that is mostly bored.

Gates (short-circuited in order for efficiency):
    1. Session structurally tradable  (velocity, volume, spread)
    2. Mispricing sufficient           (edge > slippage + fee + buffer)
    3. Liquidity favorable             (depth >= minimum fill target)
    4. Time window favorable           (not too early, not too late)
    5. Regime match                    (signal type vs market regime)

The filter is PURE — no engine state, no DB calls, no side effects.
Inputs are passed in by the caller. Returns AdmissionResult so the
caller can log uniformly and skip the tick on rejection.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict


# Type alias for gate result: (passed, reason)
GateResult = Tuple[bool, str]


@dataclass
class AdmissionResult:
    admitted: bool
    gate_results: Dict[str, GateResult] = field(default_factory=dict)
    rejection_reason: Optional[str] = None  # first failed gate's reason

    def log_line(self) -> str:
        if self.admitted:
            gates = " ".join(f"{k}=OK" for k in self.gate_results)
            return f"ADMISSION PASS: {gates}"
        gates = " ".join(
            f"{k}={'OK' if v[0] else 'FAIL'}"
            for k, v in self.gate_results.items()
        )
        return f"ADMISSION REJECT [{self.rejection_reason}]: {gates}"


class AdmissionFilter:
    """Stateless 5-gate rejection layer.

    Construct once, reuse forever. Thresholds are passed per-call so the
    caller can read live values from user_config without filter restart.
    """

    def evaluate(
        self,
        # Gate 1: Session structure
        btc_velocity_30s: float,        # $/s over last 30s (signed)
        contract_volume_session: int,    # contracts traded this window so far
        book_spread_c: int,              # yes ask - yes bid (cents)
        # Gate 2: Mispricing sufficient
        bb_mispricing_c: float,          # fair - market (signed cents)
        estimated_slippage_c: float,     # expected fill cost above mid
        fee_c: float = 1.0,              # per-contract fee estimate
        # Gate 3: Liquidity
        ask_depth_at_target: int = 0,    # contracts within 2c of best ask
        min_fill_target: int = 3,        # minimum contracts we want filled
        # Gate 4: Time window
        seconds_elapsed: float = 0.0,    # seconds into 15-min window
        seconds_remaining: float = 0.0,  # seconds until settlement
        # Gate 5: Regime match
        signal_type: str = "momentum",   # "momentum" | "reversion" | "certainty"
        btc_trending: bool = False,      # True if BTC has clear direction
        btc_choppy: bool = False,        # True if BTC oscillating
        # Tunable thresholds (callers pass live values)
        min_velocity: float = 0.50,      # $/s magnitude for "moving"
        min_volume: int = 5,             # contracts/window
        max_spread_c: int = 5,
        big_mispricing_bypass_c: float = 15.0,  # huge edge bypasses Gate 1
        mispricing_buffer_c: float = 2.0,
        early_reject_s: float = 120.0,   # first 2 min are noise
        late_reject_s: float = 60.0,     # last min — too late for entry+exit
        preferred_window_start_s: float = 420.0,  # min 7
        preferred_window_end_s: float = 720.0,    # min 12
        certainty_min_seconds_remaining: float = 120.0,
    ) -> AdmissionResult:
        """Evaluate all 5 gates. Short-circuits on first failure."""
        result = AdmissionResult(admitted=False)

        # ── Gate 1 — Session structurally tradable ───────────────────────
        # Bypass: huge mispricing trumps structural concerns.
        big_edge = abs(bb_mispricing_c) >= big_mispricing_bypass_c
        if big_edge:
            g1: GateResult = (True, f"bypass(edge={bb_mispricing_c:+.1f}c)")
        else:
            v_ok = abs(btc_velocity_30s) >= min_velocity
            vol_ok = contract_volume_session >= min_volume
            spread_ok = book_spread_c <= max_spread_c
            if not v_ok:
                g1 = (False, f"session_dead(vel={btc_velocity_30s:+.2f}$/s)")
            elif not vol_ok:
                g1 = (False, f"no_volume(vol={contract_volume_session}ct)")
            elif not spread_ok:
                g1 = (False, f"spread_blown(spread={book_spread_c}c)")
            else:
                g1 = (True,
                      f"vel={btc_velocity_30s:+.2f}$/s "
                      f"vol={contract_volume_session}ct "
                      f"spr={book_spread_c}c")
        result.gate_results["G1_session"] = g1
        if not g1[0]:
            result.rejection_reason = g1[1]
            return result

        # ── Gate 2 — Mispricing sufficient ───────────────────────────────
        # We need edge > slippage + fee + buffer. Buffer covers model error.
        required = estimated_slippage_c + fee_c + mispricing_buffer_c
        if abs(bb_mispricing_c) > required:
            g2: GateResult = (
                True,
                f"edge={bb_mispricing_c:+.1f}c>req={required:.1f}c",
            )
        else:
            g2 = (
                False,
                f"mispricing_insufficient("
                f"edge={bb_mispricing_c:+.1f}c need>{required:.1f}c)",
            )
        result.gate_results["G2_mispricing"] = g2
        if not g2[0]:
            result.rejection_reason = g2[1]
            return result

        # ── Gate 3 — Liquidity favorable ─────────────────────────────────
        if ask_depth_at_target >= min_fill_target:
            g3: GateResult = (
                True,
                f"depth={ask_depth_at_target}ct>={min_fill_target}",
            )
        else:
            g3 = (
                False,
                f"depth_insufficient("
                f"depth={ask_depth_at_target}ct need>={min_fill_target})",
            )
        result.gate_results["G3_liquidity"] = g3
        if not g3[0]:
            result.rejection_reason = g3[1]
            return result

        # ── Gate 4 — Time favorable ──────────────────────────────────────
        if seconds_elapsed < early_reject_s:
            g4: GateResult = (
                False,
                f"too_early(elapsed={seconds_elapsed:.0f}s<{early_reject_s:.0f})",
            )
        elif seconds_remaining < late_reject_s:
            g4 = (
                False,
                f"too_late(left={seconds_remaining:.0f}s<{late_reject_s:.0f})",
            )
        else:
            in_pref = (
                preferred_window_start_s
                <= seconds_elapsed
                <= preferred_window_end_s
            )
            label = "pref" if in_pref else "ok"
            g4 = (
                True,
                f"{label}(elapsed={seconds_elapsed:.0f}s "
                f"left={seconds_remaining:.0f}s)",
            )
        result.gate_results["G4_time"] = g4
        if not g4[0]:
            result.rejection_reason = g4[1]
            return result

        # ── Gate 5 — Regime match ────────────────────────────────────────
        st = (signal_type or "").lower()
        if st == "momentum" and btc_choppy:
            g5: GateResult = (False, "momentum_in_chop")
        elif st == "reversion" and btc_trending:
            g5 = (False, "reversion_in_trend")
        elif st == "certainty" and seconds_remaining > certainty_min_seconds_remaining:
            g5 = (
                False,
                f"certainty_too_early(left={seconds_remaining:.0f}s)",
            )
        else:
            trend_str = "trend" if btc_trending else ("chop" if btc_choppy else "neutral")
            g5 = (True, f"{st}/{trend_str}")
        result.gate_results["G5_regime"] = g5
        if not g5[0]:
            result.rejection_reason = g5[1]
            return result

        # All 5 passed
        result.admitted = True
        return result
