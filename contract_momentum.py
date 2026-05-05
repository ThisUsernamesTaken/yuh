"""ContractMomentumAnalyzer — per-fill momentum/reversion classifier on the
Kalshi contract mid stream. See RESEARCH_MOMENTUM_REVERSION_COVARIANCE.md.

The class is purposefully self-contained — no Kalshi/BTC deps, only stdlib.
Designed to be additive: any internal exception inside .update() must NOT
propagate out. Caller is also expected to guard with try/except.

Public surface:
    a = ContractMomentumAnalyzer(side="yes")
    a.update(mid_cents=52.0, timestamp=time.time(), seconds_left=720.0)
    a.momentum_score        # CMS in [-1, +1]
    a.reversion_index       # CRI in [0, 1]
    a.covariance            # MRC in [-1, +1]
    a.path_signature        # str: FLAT / STAIRCASE_UP / ... / MIXED
    a.convergence_rate      # SCR in [0, 1]
    a.recommended_tp_multiplier   # float in [0.7, 1.3]
    a.should_force_exit     # bool — caller should cross-spread sell
    a.is_warm               # bool — True once buffer has >= MIN_OBS samples

Micro-timeframe surface (sub-15-min momentum/reversion covariance):
    a.micro_momentum_5s     # float in [-1, +1] — last 5-6s direction
    a.micro_momentum_25s    # float in [-1, +1] — last ~25s trend direction
    a.micro_reversion_25s   # float in [0, 1]   — 25s oscillation measure
    a.micro_alignment       # float in [-1, +1] — 5s vs 25s agreement
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Optional, Tuple


class ContractMomentumAnalyzer:
    BUFFER_LEN = 90              # ~4.5 min at 3s polls
    COV_WINDOW = 60              # ~3 min at 3s polls
    SMA_WINDOW_S = 120.0         # 2-min SMA for residual stdev
    EMA_ALPHA = 0.3              # smoothing for CMS
    MIN_OBS = 20                 # 2026-05-04: was 30 → 20 → ~60s warmup at
                                 # 3s polling (was ~90s). Engages MRC earlier
                                 # in the 15-minute window for better coverage.
    STALE_DT_S = 5.0             # drop obs spaced more than this apart
    EMA_60S_ALPHA = 0.15         # 2026-05-04: 60s mid-EMA window for mid-term
                                 # trend detection alongside the existing CMS
                                 # short-EMA buffer.

    CMS_NORM = 0.005             # ~0.5% per-poll relative return → ±1
    CRI_NORM = 3.0               # 3c residual stdev → 1.0
    MRC_NORM = 0.05              # covariance scale

    PATH_FLAT_RANGE_C = 2.0      # below this range → FLAT
    PATH_NEAR_C = 1.5            # "near" tolerance for staircase / M-top
    PATH_DOUBLE_TOUCH_TOL_C = 1.0
    PATH_DOUBLE_TOUCH_AWAY_C = 2.0  # must move this far away to count as a touch
    EXTREME_LO = 2.0             # mid <= → at extreme, suppress signals
    EXTREME_HI = 98.0
    SCR_HIGH = 0.7

    TP_MULT_FLOOR = 0.7
    TP_MULT_CEIL = 1.3

    # ── Micro-timeframe windows (sub-15-min MRC) ────────────────────
    MICRO_5S_LEN = 2             # last 2 obs ≈ 5-6s at 3s polling
    MICRO_25S_LEN = 8            # last 8 obs ≈ 24s at 3s polling
    MICRO_5S_NORM_C = 0.5        # 0.5c per-step delta → ±1
    MICRO_25S_NORM_C = 2.0       # 2c net move across window → ±1
    MICRO_25S_REV_NORM_C = 1.5   # 1.5c stdev → 1.0
    MICRO_DEAD_ZONE = 0.05       # |reading| below this → treat as zero
    MICRO_TREND_THRESHOLD = 0.30 # |25s| above this counts as "trending"
    MICRO_FLIP_THRESHOLD = 0.20  # |5s| above this counts as a flip
    MICRO_TP_WIDEN_MULT = 1.20   # alignment > +0.5 → TP widened
    MICRO_TP_TIGHTEN_MULT = 0.75 # alignment < -0.5 → TP tightened

    def __init__(self, side: str = "yes") -> None:
        side_l = (side or "yes").lower().strip()
        self._side: str = side_l if side_l in ("yes", "no") else "yes"

        self._buf: Deque[Tuple[float, float]] = deque(maxlen=self.BUFFER_LEN)
        self._cms_history: Deque[float] = deque(maxlen=self.COV_WINDOW)
        self._cri_history: Deque[float] = deque(maxlen=self.COV_WINDOW)

        self._cms: Optional[float] = None
        self._prev_mid: Optional[float] = None
        self._prev_ts: Optional[float] = None
        self._opening_mid: Optional[float] = None

        # Latest readouts (exposed as properties)
        self._momentum_score: float = 0.0
        self._reversion_index: float = 0.0
        self._covariance: float = 0.0
        self._path_signature: str = "FLAT"
        self._convergence_rate: float = 0.0
        self._tp_multiplier: float = 1.0
        self._force_exit: bool = False
        self._obs_count: int = 0

        # Micro-timeframe state
        self._micro_5s_buf: Deque[Tuple[float, float]] = deque(maxlen=self.MICRO_5S_LEN)
        self._micro_25s_buf: Deque[Tuple[float, float]] = deque(maxlen=self.MICRO_25S_LEN)
        self._micro_momentum_5s: float = 0.0
        self._micro_momentum_25s: float = 0.0
        self._micro_reversion_25s: float = 0.0
        self._micro_alignment: float = 0.0

        # 60s mid-EMA window (2026-05-04) — exponential smoothing on the
        # raw mid for mid-term trend detection. Filled alongside the
        # 5s/25s micro buffers; consumed by mid60s_ema property.
        self._mid_ema_60s: Optional[float] = None

    # ── Properties (read-only snapshots of last update) ─────────────
    @property
    def side(self) -> str:
        return self._side

    @property
    def momentum_score(self) -> float:
        return self._momentum_score

    @property
    def reversion_index(self) -> float:
        return self._reversion_index

    @property
    def covariance(self) -> float:
        return self._covariance

    @property
    def path_signature(self) -> str:
        return self._path_signature

    @property
    def convergence_rate(self) -> float:
        return self._convergence_rate

    @property
    def recommended_tp_multiplier(self) -> float:
        return self._tp_multiplier

    @property
    def should_force_exit(self) -> bool:
        return self._force_exit

    @property
    def is_warm(self) -> bool:
        return self._obs_count >= self.MIN_OBS

    @property
    def micro_momentum_5s(self) -> float:
        return self._micro_momentum_5s

    @property
    def micro_momentum_25s(self) -> float:
        return self._micro_momentum_25s

    @property
    def micro_reversion_25s(self) -> float:
        return self._micro_reversion_25s

    @property
    def micro_alignment(self) -> float:
        return self._micro_alignment

    @property
    def observation_count(self) -> int:
        return self._obs_count

    @property
    def mid_ema_60s(self) -> Optional[float]:
        """60-second EMA of contract mid. None until first observation."""
        return self._mid_ema_60s

    # ── Core update ─────────────────────────────────────────────────
    def update(
        self,
        mid_cents: float,
        timestamp: float,
        seconds_left: Optional[float] = None,
    ) -> None:
        """Feed one observation. Never raises — internal failures degrade
        to a neutral readout."""
        try:
            self._update_inner(mid_cents, timestamp, seconds_left)
        except Exception:
            # Fail-soft. Neutralize so caller defaults to baseline TP.
            self._tp_multiplier = 1.0
            self._force_exit = False

    def _update_inner(
        self,
        mid_cents: float,
        timestamp: float,
        seconds_left: Optional[float],
    ) -> None:
        if mid_cents is None or timestamp is None:
            return
        try:
            mid = float(mid_cents)
            ts = float(timestamp)
        except (TypeError, ValueError):
            return
        if not (0.0 < mid < 100.0):
            # Mid at exchange bounds — analyzer can't do much; defer to
            # protective layer.
            self._tp_multiplier = 1.0
            self._force_exit = False
            return

        # Buffer the observation.
        self._buf.append((ts, mid))
        self._micro_5s_buf.append((ts, mid))
        self._micro_25s_buf.append((ts, mid))
        self._obs_count += 1
        if self._opening_mid is None:
            self._opening_mid = mid

        # 60s mid-EMA (2026-05-04). Alpha tuned to ~60s effective window
        # at 3s polling (alpha=0.15 → ~6.7-poll memory ≈ 20s, smoothed by
        # the long-tail influence to roughly 60s of data weight).
        if self._mid_ema_60s is None:
            self._mid_ema_60s = mid
        else:
            self._mid_ema_60s = (
                self.EMA_60S_ALPHA * mid
                + (1.0 - self.EMA_60S_ALPHA) * self._mid_ema_60s
            )

        # First observation seeds prev_*; can't compute returns yet.
        if self._prev_mid is None or self._prev_ts is None:
            self._prev_mid = mid
            self._prev_ts = ts
            return

        # Stale poll — reset prev_* and skip return calc to keep EMA clean.
        if (ts - self._prev_ts) > self.STALE_DT_S:
            self._prev_mid = mid
            self._prev_ts = ts
            return

        # ── 1) CMS — EMA of single-step relative returns ────────────
        denom = max(self._prev_mid, 1e-6)
        r = (mid - self._prev_mid) / denom
        if self._cms is None:
            self._cms = r
        else:
            self._cms = self.EMA_ALPHA * r + (1.0 - self.EMA_ALPHA) * self._cms
        cms_norm = max(-1.0, min(1.0, self._cms / self.CMS_NORM))
        self._cms_history.append(cms_norm)

        # ── 2) CRI — stdev of residuals around 2-min SMA ────────────
        cutoff = ts - self.SMA_WINDOW_S
        sma_window = [m for (t, m) in self._buf if t >= cutoff]
        if len(sma_window) >= 5:
            sma = sum(sma_window) / len(sma_window)
            recent = list(self._buf)[-30:]
            residuals = [m - sma for (_, m) in recent]
            n = len(residuals)
            mean_r = sum(residuals) / n
            var = sum((x - mean_r) ** 2 for x in residuals) / n
            cri_raw = math.sqrt(max(0.0, var))
            cri_norm = max(0.0, min(1.0, cri_raw / self.CRI_NORM))
        else:
            cri_norm = 0.0
        self._cri_history.append(cri_norm)

        # ── 3) MRC — covariance(|CMS_norm|, CRI_norm) ───────────────
        if len(self._cms_history) >= 10:
            abs_cms = [abs(c) for c in self._cms_history]
            cri_arr = list(self._cri_history)
            n = min(len(abs_cms), len(cri_arr))
            abs_cms = abs_cms[-n:]
            cri_arr = cri_arr[-n:]
            mean_a = sum(abs_cms) / n
            mean_c = sum(cri_arr) / n
            cov = sum(
                (abs_cms[i] - mean_a) * (cri_arr[i] - mean_c)
                for i in range(n)
            ) / n
            mrc = max(-1.0, min(1.0, cov / self.MRC_NORM))
        else:
            mrc = 0.0

        # ── 4) Path signature ───────────────────────────────────────
        path = self._classify_path() if self._obs_count >= self.MIN_OBS else "FLAT"

        # ── 5) SCR — settlement convergence rate ────────────────────
        if seconds_left is not None and seconds_left > 0:
            try:
                sec_left = float(seconds_left)
                distance = min(mid, 100.0 - mid)
                expected = 50.0 * math.sqrt(max(0.0, sec_left) / 900.0)
                scr = max(0.0, min(1.0, 1.0 - distance / max(expected, 1.0)))
            except Exception:
                scr = 0.0
        else:
            scr = 0.0

        # ── 6) Micro-timeframes (5s / 25s) ──────────────────────────
        m5, m25, r25, align = self._compute_micro()

        # ── 7) Recommendation ───────────────────────────────────────
        if self._obs_count < self.MIN_OBS:
            tp_mult, force_exit = 1.0, False
        elif mid <= self.EXTREME_LO or mid >= self.EXTREME_HI:
            tp_mult, force_exit = 1.0, False
        else:
            tp_mult, force_exit = self._recommend(
                cms_norm, cri_norm, mrc, path, scr,
            )

        # Micro-timeframe modulation. Layered AFTER the base recommend so
        # the macro/path signals still anchor the multiplier; micro just
        # widens or tightens by ≤20-25%. The clamp to [TP_MULT_FLOOR,
        # TP_MULT_CEIL] below absorbs combinations that would overshoot.
        if self._obs_count >= self.MIN_OBS and len(self._micro_25s_buf) >= 4:
            if align > 0.5:
                tp_mult = tp_mult * self.MICRO_TP_WIDEN_MULT
            elif align < -0.5:
                tp_mult = tp_mult * self.MICRO_TP_TIGHTEN_MULT

            # Early-reversion force-exit: 5s flipped against the trend
            # while 25s is still favorable to our side. Caller still
            # gates on profit, so we won't bail at a loss.
            if not force_exit:
                if self._side == "yes":
                    if (m25 > self.MICRO_TREND_THRESHOLD
                            and m5 < -self.MICRO_FLIP_THRESHOLD):
                        force_exit = True
                else:
                    if (m25 < -self.MICRO_TREND_THRESHOLD
                            and m5 > self.MICRO_FLIP_THRESHOLD):
                        force_exit = True

        # ── 8) Publish ──────────────────────────────────────────────
        self._momentum_score = float(cms_norm)
        self._reversion_index = float(cri_norm)
        self._covariance = float(mrc)
        self._path_signature = str(path)
        self._convergence_rate = float(scr)
        self._micro_momentum_5s = float(m5)
        self._micro_momentum_25s = float(m25)
        self._micro_reversion_25s = float(r25)
        self._micro_alignment = float(align)
        self._tp_multiplier = max(self.TP_MULT_FLOOR, min(self.TP_MULT_CEIL, float(tp_mult)))
        self._force_exit = bool(force_exit)

        self._prev_mid = mid
        self._prev_ts = ts

    # ── Micro-timeframe computations (5s / 25s) ─────────────────────
    def _compute_micro(self) -> Tuple[float, float, float, float]:
        """Returns (micro_momentum_5s, micro_momentum_25s,
        micro_reversion_25s, micro_alignment). Defaults to zeros when
        either window is too cold."""
        # 5s — instantaneous direction from the last 2 observations.
        if len(self._micro_5s_buf) >= 2:
            (_, m_first_5s) = self._micro_5s_buf[0]
            (_, m_last_5s) = self._micro_5s_buf[-1]
            delta_5s = m_last_5s - m_first_5s
            m5 = max(-1.0, min(1.0, delta_5s / self.MICRO_5S_NORM_C))
        else:
            m5 = 0.0

        # 25s — net move across the window normalized; reversion = stdev
        # of mids in the window.
        if len(self._micro_25s_buf) >= 4:
            mids_25 = [m for (_, m) in self._micro_25s_buf]
            net_25 = mids_25[-1] - mids_25[0]
            m25 = max(-1.0, min(1.0, net_25 / self.MICRO_25S_NORM_C))
            n25 = len(mids_25)
            mean_25 = sum(mids_25) / n25
            var_25 = sum((x - mean_25) ** 2 for x in mids_25) / n25
            std_25 = math.sqrt(max(0.0, var_25))
            r25 = max(0.0, min(1.0, std_25 / self.MICRO_25S_REV_NORM_C))
        else:
            m25 = 0.0
            r25 = 0.0

        # Alignment — agreement between 5s and 25s.
        # Both readings near zero → neutral (0).
        # Same sign → positive, scaled by averaged magnitude.
        # Opposite sign → negative, scaled by averaged magnitude.
        if abs(m5) < self.MICRO_DEAD_ZONE or abs(m25) < self.MICRO_DEAD_ZONE:
            align = 0.0
        elif (m5 > 0) == (m25 > 0):
            align = max(-1.0, min(1.0, (m5 + m25) / 2.0))
        else:
            align = -max(0.0, min(1.0, (abs(m5) + abs(m25)) / 2.0))

        return (m5, m25, r25, align)

    # ── Path classifier ─────────────────────────────────────────────
    def _classify_path(self) -> str:
        if len(self._buf) < self.MIN_OBS:
            return "FLAT"
        mids = [m for (_, m) in self._buf]
        timestamps = [t for (t, _) in self._buf]
        m_open = mids[0]
        m_now = mids[-1]
        m_hwm = max(mids)
        m_lwm = min(mids)
        i_hwm = mids.index(m_hwm)
        i_lwm = mids.index(m_lwm)
        rng = m_hwm - m_lwm
        age = timestamps[-1] - timestamps[0]
        if age <= 0 or rng < self.PATH_FLAT_RANGE_C:
            return "FLAT"

        # STAIRCASE: near one extreme + monotonic-ish drift
        if (m_now - m_lwm) > 0.7 * rng and (m_hwm - m_now) < self.PATH_NEAR_C:
            return "STAIRCASE_UP"
        if (m_hwm - m_now) > 0.7 * rng and (m_now - m_lwm) < self.PATH_NEAR_C:
            return "STAIRCASE_DOWN"

        # V_SHAPE: dipped then recovered (low came before high, recently)
        if (
            m_now > m_open
            and m_lwm < m_open - 1
            and i_lwm < i_hwm
            and (timestamps[i_hwm] - timestamps[0]) > 0.6 * age
        ):
            return "V_SHAPE"

        # INVERTED_V: spiked then collapsed (high came before low, recently)
        if (
            m_now < m_open
            and m_hwm > m_open + 1
            and i_hwm < i_lwm
            and (timestamps[i_lwm] - timestamps[0]) > 0.6 * age
        ):
            return "INVERTED_V"

        # M_TOP: double-touch near HWM, currently retracing
        if (
            (m_hwm - m_now) < self.PATH_NEAR_C
            and (m_hwm - m_open) > 2.0
            and self._count_visits(m_hwm, mids) >= 2
        ):
            return "M_TOP"

        # W_BOTTOM: double-touch near LWM, currently bouncing
        if (
            (m_now - m_lwm) < self.PATH_NEAR_C
            and (m_open - m_lwm) > 2.0
            and self._count_visits(m_lwm, mids) >= 2
        ):
            return "W_BOTTOM"

        return "MIXED"

    def _count_visits(self, level: float, mids) -> int:
        """Number of times the series came within tol of `level` and then
        moved away by >= AWAY_C. Crude but effective."""
        tol = self.PATH_DOUBLE_TOUCH_TOL_C
        away = self.PATH_DOUBLE_TOUCH_AWAY_C
        visits = 0
        in_zone = False
        last_away = True
        for m in mids:
            near = abs(m - level) <= tol
            if near and last_away:
                visits += 1
                in_zone = True
                last_away = False
            elif near:
                in_zone = True
            else:
                if in_zone and abs(m - level) >= away:
                    last_away = True
                in_zone = False
        return visits

    # ── Recommendation logic ────────────────────────────────────────
    def _recommend(
        self,
        cms_norm: float,
        cri_norm: float,
        mrc: float,
        path: str,
        scr: float,
    ) -> Tuple[float, bool]:
        # Force-exit overrides — path against our side
        against_yes = self._side == "yes" and path in (
            "STAIRCASE_DOWN", "INVERTED_V", "M_TOP",
        )
        against_no = self._side == "no" and path in (
            "STAIRCASE_UP", "V_SHAPE", "W_BOTTOM",
        )
        if against_yes or against_no:
            return (self.TP_MULT_FLOOR, True)

        # Settlement convergence — market has decided
        if scr > self.SCR_HIGH:
            going_yes = cms_norm > 0
            on_converging_side = (
                (self._side == "yes" and going_yes)
                or (self._side == "no" and not going_yes)
            )
            if on_converging_side:
                return (self.TP_MULT_CEIL, False)
            else:
                return (self.TP_MULT_FLOOR, True)

        # MRC-based default
        if mrc < -0.3:
            return (self.TP_MULT_CEIL, False)   # clean trend, hold longer
        if mrc > 0.3:
            return (self.TP_MULT_FLOOR, False)  # choppy, take profit sooner
        return (1.0, False)
