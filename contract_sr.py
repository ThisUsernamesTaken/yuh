"""contract_sr.py — contract-native support/resistance detector.

Not a theory about round numbers or BTC TA — purely empirical. Watches the
tick stream of a Kalshi binary contract's YES mid and maintains per-cent
evidence of "dwells": consecutive samples where price stayed within ±1c.

A level's strength = composite of:
  - dwell count  (how many distinct visits saturates at 5)
  - dwell depth  (total samples at or near the level, saturates at 20)
  - recency      (linear decay over `recency_halflife` samples)
  - bounce quality (defended returns vs total touches)

Bounce quality is intentionally part of level_strength because SR_FADE
consumes that score directly. A repeatedly defended level is a better fade
target than a level that merely attracted dwell before breaking.

No live orders. No gating of the DOMINANT gate. Pure observation until a
signal tier consumes it.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class LevelEvidence:
    dwells: int = 0                # distinct visits (entered then left)
    samples_at: int = 0            # total samples within ±1c of this price
    last_seen_sample: int = -1
    last_bounced_from_below: int = 0   # times price came from above and reversed up
    last_bounced_from_above: int = 0   # times price came from below and reversed down


@dataclass
class ContractSRState:
    ticker: str
    samples_seen: int = 0
    mid_history: Deque[int] = field(default_factory=lambda: deque(maxlen=256))
    levels: Dict[int, LevelEvidence] = field(default_factory=dict)

    # Tracks the current dwell zone. When mid stays within `dwell_tol_cents`
    # of `_dwell_anchor` for ≥1 sample, that's part of the same dwell. When
    # mid departs (≥ `depart_cents`), we register one dwell at the anchor.
    _dwell_anchor: Optional[int] = None
    _dwell_samples: int = 0

    # Thresholds
    dwell_tol_cents: int = 1
    depart_cents: int = 2
    recency_halflife: int = 60


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else (hi if x > hi else x)


def update(state: ContractSRState, mid_cents: int) -> None:
    """Feed one price sample."""
    if mid_cents <= 0 or mid_cents >= 100:
        return
    state.samples_seen += 1
    idx = state.samples_seen
    state.mid_history.append(mid_cents)

    # Increment samples_at for the cent level and its ±1c neighbours.
    for p in (mid_cents - 1, mid_cents, mid_cents + 1):
        if 0 < p < 100:
            lv = state.levels.setdefault(p, LevelEvidence())
            lv.samples_at += 1
            lv.last_seen_sample = idx

    # Dwell tracking.
    anchor = state._dwell_anchor
    if anchor is None:
        # Start a new dwell at this price.
        state._dwell_anchor = mid_cents
        state._dwell_samples = 1
        return

    if abs(mid_cents - anchor) <= state.dwell_tol_cents:
        # Still in the same dwell.
        state._dwell_samples += 1
        return

    # Departed from the anchor. Register the dwell.
    if state._dwell_samples >= 1:
        lv = state.levels.setdefault(anchor, LevelEvidence())
        lv.dwells += 1
        # Bounce direction: we left the level in which direction?
        if mid_cents > anchor:
            # Left upward — future return from above will count as "bounced_from_above"
            # toward this level as a SUPPORT.
            pass
        else:
            pass
        # Bounce registration: has this price RETURNED to a prior dwell anchor?
        # Walk back up to 12 samples to find the most recent prior dwell anchor
        # that was ≥depart_cents from intermediate samples.
        _register_bounce_if_returning(state, mid_cents, idx)

    # Start a new dwell at the current price.
    state._dwell_anchor = mid_cents
    state._dwell_samples = 1


def _register_bounce_if_returning(state: ContractSRState,
                                    mid_cents: int, idx: int) -> None:
    """When price returns to a previously-dwelled level, register a bounce."""
    # Walk the last ~15 samples (excluding the current) looking for a prior
    # mid within ±1c of the current price, separated by at least `depart_cents`
    # of price excursion in between.
    hist = list(state.mid_history)[:-1]
    if len(hist) < 2:
        return
    max_depart = 0
    for i in range(len(hist) - 1, max(-1, len(hist) - 16), -1):
        if abs(hist[i] - mid_cents) <= state.dwell_tol_cents:
            if max_depart >= state.depart_cents:
                lv = state.levels.setdefault(mid_cents, LevelEvidence())
                # Direction: did we return from above or from below?
                # Find the excursion direction by looking between i and now.
                mid_of_excursion = max(hist[i + 1:], default=mid_cents) \
                    if hist[i + 1:] and max(hist[i + 1:]) > mid_cents \
                    else min(hist[i + 1:], default=mid_cents)
                if mid_of_excursion > mid_cents:
                    lv.last_bounced_from_above += 1
                else:
                    lv.last_bounced_from_below += 1
                return
            # Keep scanning further back
        depart = abs(hist[i] - mid_cents)
        if depart > max_depart:
            max_depart = depart


def level_strength(state: ContractSRState, price: int) -> float:
    """Composite 0..1 strength score."""
    lv = state.levels.get(price)
    if lv is None or lv.dwells == 0:
        return 0.0
    dwell_score = _clip(lv.dwells / 5.0)
    depth_score = _clip(lv.samples_at / 20.0)
    bounce_total = lv.last_bounced_from_above + lv.last_bounced_from_below
    touch_total = max(lv.dwells, bounce_total, 1)
    bounce_score = _clip(bounce_total / touch_total)
    if lv.last_seen_sample < 0:
        recency = 0.0
    else:
        age = max(0, state.samples_seen - lv.last_seen_sample)
        recency = _clip(1.0 - age / max(state.recency_halflife, 1))
    return round(
        0.4 * dwell_score
        + 0.2 * depth_score
        + 0.2 * recency
        + 0.2 * bounce_score,
        3,
    )


def _ranked_levels(state: ContractSRState, min_strength: float
                    ) -> List[Tuple[int, float]]:
    out = []
    for p in state.levels:
        s = level_strength(state, p)
        if s >= min_strength:
            out.append((p, s))
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def nearest_support(state: ContractSRState, current_mid: int,
                     min_strength: float = 0.4,
                     max_distance: int = 20) -> Optional[Tuple[int, float]]:
    """Strongest level below `current_mid` within max_distance cents."""
    if current_mid <= 0:
        return None
    best: Optional[Tuple[int, float]] = None
    for price, strength in _ranked_levels(state, min_strength):
        if 0 < current_mid - price <= max_distance:
            if best is None or strength > best[1]:
                best = (price, strength)
    return best


def nearest_resistance(state: ContractSRState, current_mid: int,
                        min_strength: float = 0.4,
                        max_distance: int = 20) -> Optional[Tuple[int, float]]:
    """Strongest level above `current_mid` within max_distance cents."""
    if current_mid <= 0:
        return None
    best: Optional[Tuple[int, float]] = None
    for price, strength in _ranked_levels(state, min_strength):
        if 0 < price - current_mid <= max_distance:
            if best is None or strength > best[1]:
                best = (price, strength)
    return best


def snapshot(state: ContractSRState) -> List[Dict]:
    """Serialize for cross-session persistence (top 40 by dwells)."""
    out = []
    for p, lv in state.levels.items():
        if lv.dwells >= 1:
            out.append({
                "price": p,
                "dwells": lv.dwells,
                "samples_at": lv.samples_at,
                "last_bounced_from_above": lv.last_bounced_from_above,
                "last_bounced_from_below": lv.last_bounced_from_below,
                "last_seen_sample": lv.last_seen_sample,
            })
    return sorted(out, key=lambda d: -d["dwells"])[:40]


def seed_from(state: ContractSRState, snapshot_rows: List[Dict],
               decay: float = 0.5) -> None:
    """Seed from a prior session at `decay` strength."""
    if not snapshot_rows:
        return
    for row in snapshot_rows:
        p = int(row.get("price", 0) or 0)
        if not (0 < p < 100):
            continue
        lv = state.levels.setdefault(p, LevelEvidence())
        # G4 (2026-04-22): apply decay but floor seeded dwells at 1. Without
        # the floor, `int(round(1 * 0.5)) = 0` drops single-dwell levels
        # and `snapshot()` filter (dwells>=1) excludes them — the seed population
        # collapses after one quiet window. Recency (last_seen_sample=0)
        # already de-weights stale priors so the floor is safe.
        _src_dwells = int(row.get("dwells", 0) or 0)
        _src_samples = int(row.get("samples_at", 0) or 0)
        lv.dwells += max(1, int(round(_src_dwells * decay))) if _src_dwells > 0 else 0
        lv.samples_at += max(1, int(round(_src_samples * decay))) if _src_samples > 0 else 0
        lv.last_bounced_from_above += int(round(
            int(row.get("last_bounced_from_above", 0) or 0) * decay))
        lv.last_bounced_from_below += int(round(
            int(row.get("last_bounced_from_below", 0) or 0) * decay))
        lv.last_seen_sample = 0  # stale by definition
