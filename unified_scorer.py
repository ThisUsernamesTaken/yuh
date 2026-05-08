"""unified_scorer.py — single-EV decision module (Phase 2, NOT WIRED YET).

A standalone replacement for the rigid tier cascade. Consumes every signal
the engine already computes, blends them on a consistent [-1, +1] axis,
turns the composite into an explicit cents-of-EV estimate, and (optionally)
returns a Kelly-sized contract count.

Design intent
-------------
* PURE FUNCTION. No state, no side effects, no engine refs. Caller supplies
  a snapshot of every signal; we return a dataclass.
* SIDE-NEUTRAL. Each component contributes a signed value where +1 = YES
  and -1 = NO. Weighted sum lives on [-100, +100]. Sign of the sum picks
  the side; magnitude drives confidence.
* MISSING SIGNALS DO NOT DRAG. None / NaN / pre-warmup zeros contribute
  weight 0 instead of -0 — so a missing TA signal isn't a NO vote.
* EV IS THE GATE. A high score with a thin price edge still gets rejected.
  Only `ev_cents > UNIFIED_MIN_EV_C` passes. This separates "agreement
  among signals" (confidence) from "expected dollars" (EV).
* WALL CONSUMPTION IS A VETO. If the *opposing* ask stack is being eaten
  AGGRESSIVE_BUY, we return None regardless of score — someone with more
  information is taking the other side hard.

Signal weights (sum to 1.0)
---------------------------
    bb_mispricing      0.25  prob_engine.mispricing → BB fair value vs mid
    btc_momentum       0.20  direction_strategy: dist_pct + 5m move (90% WR)
    kalshi_lag         0.15  microstructure.last_score.kalshi_lag
    book_imbalance     0.10  book.imbalance + microprice skew
    taker_flow         0.10  tape.taker_imbalance — who crosses the spread
    ta_composite       0.08  ta_scorer.last_result.composite_score
    session_timing     0.07  seconds_left / 900 — early=uncertain, late=convergent
    wall_consumption   0.05  same-side AGGRESSIVE_BUY = +, opp AGGRESSIVE = veto

EV math
-------
For a YES buy at `ask` cents (out of 100):
    payout if win   = (100 - ask)
    cost if lose    = ask
    p_win           = fair_prob   (our estimate from BB)
    EV (cents)      = p_win * (100 - ask) - (1 - p_win) * ask
                    = p_win * 100 - ask

For a NO buy at `no_ask` cents:
    p_win           = 1 - fair_prob
    EV (cents)      = (1 - fair_prob) * 100 - no_ask

Entry rule
----------
Return a UnifiedSignal IFF all of:
    * side != None (composite has a sign)
    * confidence >= UNIFIED_MIN_CONFIDENCE   (default 0.25)
    * ev_cents   >  UNIFIED_MIN_EV_C         (default 3 cents)
    * seconds_left >= UNIFIED_MIN_SECONDS    (default 90; pre-expiry handler
                                              owns anything tighter)
    * opposing wall verdict != "AGGRESSIVE_BUY"   (veto)
    * book has the chosen ask priced > 0     (sanity)

Kelly sizing
------------
    edge      = ev_cents / 100
    p_win_est = (ask + ev_cents) / 100      # implied true prob
    kelly_f   = edge / (1 - p_win_est)      # standard Kelly for binary
    kelly_f   = clamp(kelly_f, 0, 0.10)     # cap at 10% of bankroll
    dollars   = balance_cents/100 * kelly_f
    contracts = dollars / (ask/100)         # contracts at YES-ask
    return clamp(contracts, 1, UNIFIED_MAX_CONTRACTS)

Config knobs (read once, passed in)
-----------------------------------
    UNIFIED_SCORER_ENABLED      = True
    UNIFIED_MIN_EV_C            = 3
    UNIFIED_MIN_CONFIDENCE      = 0.25
    UNIFIED_MIN_SECONDS         = 90
    UNIFIED_MAX_CONTRACTS       = 5
    UNIFIED_KELLY_CAP_FRAC      = 0.10
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_MIN_EV_C: float = 3.0
DEFAULT_MIN_CONFIDENCE: float = 0.25
DEFAULT_MIN_SECONDS: float = 90.0
DEFAULT_MAX_CONTRACTS: int = 5
DEFAULT_KELLY_CAP_FRAC: float = 0.10

# Component weights — must sum to 1.0
WEIGHTS: Dict[str, float] = {
    "bb_mispricing":    0.25,
    "btc_momentum":     0.20,
    "kalshi_lag":       0.15,
    "book_imbalance":   0.10,
    "taker_flow":       0.10,
    "ta_composite":     0.08,
    "session_timing":   0.07,
    "wall_consumption": 0.05,
}

# Direction-strategy thresholds (mirror direction_strategy defaults)
_DIST_THRESHOLD_PCT: float = 0.0010
_MOMENTUM_THRESHOLD: float = 10.0


# ── Helpers ───────────────────────────────────────────────────────────────


def _clip(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _is_finite(x: Any) -> bool:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


# ── Result ────────────────────────────────────────────────────────────────


@dataclass
class UnifiedSignal:
    side: str                              # "yes" or "no"
    score: float                           # signed [-100, +100], + = YES
    confidence: float                      # |score| / 100, clamped [0, 1]
    ev_cents: float                        # expected value at chosen ask
    ask_cents: int                         # the ask we'd hit (chosen side)
    fair_prob: float                       # our BB-derived prob estimate
    recommended_contracts: int             # Kelly-sized, capped
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def as_log_fragment(self) -> str:
        c = self.components
        parts = [
            f"bb={c.get('bb_mispricing', 0):+.1f}",
            f"mom={c.get('btc_momentum', 0):+.1f}",
            f"lag={c.get('kalshi_lag', 0):+.1f}",
            f"book={c.get('book_imbalance', 0):+.1f}",
            f"tape={c.get('taker_flow', 0):+.1f}",
            f"ta={c.get('ta_composite', 0):+.1f}",
            f"time={c.get('session_timing', 0):+.1f}",
            f"wall={c.get('wall_consumption', 0):+.1f}",
        ]
        return " ".join(parts)


# ── Component normalizers ────────────────────────────────────────────────
# Each returns a value in [-1, +1] (or None if the signal is unavailable).


def _norm_bb_mispricing(fair_prob: Optional[float],
                        mid_cents: Optional[float]) -> Optional[float]:
    """BB mispricing in cents → [-1, +1]. ±15c saturates."""
    if not _is_finite(fair_prob) or not _is_finite(mid_cents):
        return None
    if fair_prob <= 0 or mid_cents <= 0:
        return None
    misp = float(fair_prob) * 100.0 - float(mid_cents)
    return _clip(misp / 15.0, -1.0, 1.0)


def _norm_btc_momentum(btc_price: Optional[float],
                       strike: Optional[float],
                       btc_5m_move: Optional[float]) -> Optional[float]:
    """Combined dist + 5m move; sign requires alignment, magnitude grows
    with both. Saturates at ~0.20% / $40 (the 94%-WR sweet spot)."""
    if (not _is_finite(btc_price) or not _is_finite(strike)
            or not _is_finite(btc_5m_move)):
        return None
    if btc_price <= 0 or strike <= 0:
        return None
    dist_pct = (btc_price - strike) / strike
    mom = float(btc_5m_move)

    # Alignment: if signs disagree (BTC above strike but tape falling, etc.)
    # the signal is ambiguous → contribute small magnitude on weaker side.
    sign_dist = 1.0 if dist_pct > 0 else (-1.0 if dist_pct < 0 else 0.0)
    sign_mom = 1.0 if mom > 0 else (-1.0 if mom < 0 else 0.0)

    dist_abs = abs(dist_pct)
    mom_abs = abs(mom)

    dist_norm = _clip(dist_abs / 0.0020, 0.0, 1.0)
    mom_norm = _clip(mom_abs / 40.0, 0.0, 1.0)

    if sign_dist == 0 or sign_mom == 0:
        return 0.0
    if sign_dist == sign_mom:
        return sign_dist * (0.5 * dist_norm + 0.5 * mom_norm)
    # Disagreement → take the *weaker* signal's sign at half magnitude.
    weaker = min(dist_norm, mom_norm)
    direction = sign_dist if dist_norm >= mom_norm else sign_mom
    return -direction * 0.5 * weaker


def _norm_kalshi_lag(lag: Optional[float]) -> Optional[float]:
    """microstructure.kalshi_lag is already [-1, +1], YES-positive."""
    if not _is_finite(lag):
        return None
    return _clip(float(lag), -1.0, 1.0)


def _norm_book_imbalance(book_imbalance: Optional[float],
                         microprice_cents: Optional[float],
                         mid_cents: Optional[float]) -> Optional[float]:
    """Combine top-of-book imbalance with microprice skew off mid."""
    if not _is_finite(book_imbalance):
        # Can fall back to microprice-only if imbalance missing.
        if _is_finite(microprice_cents) and _is_finite(mid_cents) and float(mid_cents) > 0:
            skew = (float(microprice_cents) - float(mid_cents)) / 5.0
            return _clip(skew, -1.0, 1.0)
        return None
    imb = _clip(float(book_imbalance), -1.0, 1.0)
    if (_is_finite(microprice_cents) and _is_finite(mid_cents)
            and float(mid_cents) > 0):
        skew = (float(microprice_cents) - float(mid_cents)) / 5.0
        skew = _clip(skew, -1.0, 1.0)
        return _clip(0.6 * imb + 0.4 * skew, -1.0, 1.0)
    return imb


def _norm_taker_flow(taker_imbalance: Optional[float]) -> Optional[float]:
    """Tape taker imbalance is [-1, +1] already."""
    if not _is_finite(taker_imbalance):
        return None
    return _clip(float(taker_imbalance), -1.0, 1.0)


def _norm_ta_composite(composite_score: Optional[float]) -> Optional[float]:
    """TA composite is roughly [-100, +100]."""
    if not _is_finite(composite_score):
        return None
    return _clip(float(composite_score) / 100.0, -1.0, 1.0)


def _norm_session_timing(seconds_left: Optional[float],
                         fair_prob: Optional[float]) -> Optional[float]:
    """Late in window the BB convergence dominates; early it's noise.
    Tilt toward the side BB favors, scaled by how far through the window
    we are. Returns 0 when fair_prob is undefined or near 50/50."""
    if not _is_finite(seconds_left) or not _is_finite(fair_prob):
        return None
    if seconds_left < 0:
        return None
    # Fraction of 15-min window already elapsed.
    elapsed_frac = _clip(1.0 - float(seconds_left) / 900.0, 0.0, 1.0)
    # BB lean: -1 if BB strongly favors NO, +1 if YES, 0 at 50/50.
    bb_lean = _clip((float(fair_prob) - 0.5) * 2.0, -1.0, 1.0)
    return bb_lean * elapsed_frac


def _norm_wall_consumption(own_verdict: Optional[str],
                           opp_verdict: Optional[str],
                           side_hint: Optional[str]) -> Optional[float]:
    """Returns +1 if our side is being eaten (someone wants in same direction
    as us) and our hinted side is the consumed one; -1 if the *opposing*
    side is being eaten. None if no info.

    side_hint: the side we'd lean toward based on the rest of the score.
    Not a veto — the veto is computed separately in `should_enter`."""
    if not own_verdict and not opp_verdict:
        return None
    if not side_hint:
        # Without a side hint we can only neutrally report the asymmetry.
        own_agg = (own_verdict == "AGGRESSIVE_BUY")
        opp_agg = (opp_verdict == "AGGRESSIVE_BUY")
        if own_agg and not opp_agg:
            return 0.5
        if opp_agg and not own_agg:
            return -0.5
        return 0.0
    own_agg = (own_verdict == "AGGRESSIVE_BUY")
    opp_agg = (opp_verdict == "AGGRESSIVE_BUY")
    own_buy = (own_verdict == "BUYING")
    opp_buy = (opp_verdict == "BUYING")
    score = 0.0
    if own_agg:
        score += 1.0
    elif own_buy:
        score += 0.4
    if opp_agg:
        score -= 1.0
    elif opp_buy:
        score -= 0.4
    return _clip(score, -1.0, 1.0)


# ── Core scorer ──────────────────────────────────────────────────────────


@dataclass
class UnifiedScorer:
    min_ev_c: float = DEFAULT_MIN_EV_C
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    min_seconds: float = DEFAULT_MIN_SECONDS
    max_contracts: int = DEFAULT_MAX_CONTRACTS
    kelly_cap_frac: float = DEFAULT_KELLY_CAP_FRAC

    # ── Composite score (sign + confidence) ──────────────────────────────

    def score_composite(
        self,
        *,
        fair_prob: Optional[float] = None,
        mid_cents: Optional[float] = None,
        btc_price: Optional[float] = None,
        strike: Optional[float] = None,
        btc_5m_move: Optional[float] = None,
        kalshi_lag: Optional[float] = None,
        book_imbalance: Optional[float] = None,
        microprice_cents: Optional[float] = None,
        taker_imbalance: Optional[float] = None,
        ta_composite_score: Optional[float] = None,
        seconds_left: Optional[float] = None,
        own_wall_verdict: Optional[str] = None,
        opp_wall_verdict: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the [-100, +100] composite score and per-component breakdown.

        Components missing inputs contribute weight 0 (don't drag the sum).
        Returns a dict — easier to log than a dataclass and lets us reuse
        for dry-run analysis."""
        # First pass: get a side hint from non-wall components so we can
        # ask wall-consumption about our side specifically.
        raw = {
            "bb_mispricing":  _norm_bb_mispricing(fair_prob, mid_cents),
            "btc_momentum":   _norm_btc_momentum(btc_price, strike, btc_5m_move),
            "kalshi_lag":     _norm_kalshi_lag(kalshi_lag),
            "book_imbalance": _norm_book_imbalance(book_imbalance,
                                                  microprice_cents, mid_cents),
            "taker_flow":     _norm_taker_flow(taker_imbalance),
            "ta_composite":   _norm_ta_composite(ta_composite_score),
            "session_timing": _norm_session_timing(seconds_left, fair_prob),
        }
        provisional = 0.0
        used_w = 0.0
        for name, val in raw.items():
            if val is None:
                continue
            w = WEIGHTS[name]
            provisional += val * w
            used_w += w
        side_hint = "yes" if provisional > 0 else ("no" if provisional < 0 else None)

        # Now wall_consumption with side context.
        raw["wall_consumption"] = _norm_wall_consumption(
            own_wall_verdict, opp_wall_verdict, side_hint
        )

        # Second pass: full weighted sum on used weights only, then rescale
        # to the full [-100, +100] range so missing signals don't shrink
        # confidence artificially.
        weighted = 0.0
        used_w = 0.0
        contributions: Dict[str, float] = {}
        for name, val in raw.items():
            w = WEIGHTS[name]
            if val is None:
                contributions[name] = 0.0
                continue
            c = val * w * 100.0
            contributions[name] = round(c, 2)
            weighted += val * w
            used_w += w

        if used_w <= 0:
            score = 0.0
        else:
            score = (weighted / used_w) * 100.0
        score = _clip(score, -100.0, 100.0)
        confidence = _clip(abs(score) / 100.0, 0.0, 1.0)

        if score > 0:
            side = "yes"
        elif score < 0:
            side = "no"
        else:
            side = None

        return {
            "side": side,
            "score": round(score, 2),
            "confidence": round(confidence, 3),
            "components": contributions,
            "weight_used": round(used_w, 3),
        }

    # ── EV + entry decision ──────────────────────────────────────────────

    @staticmethod
    def ev_cents(side: str, fair_prob: float, ask_cents: int) -> float:
        """Expected value in cents of buying `side` at `ask_cents`.
        Simplifies to (p_win * 100 - ask)."""
        if side == "yes":
            p_win = fair_prob
        else:
            p_win = 1.0 - fair_prob
        return p_win * 100.0 - float(ask_cents)

    def kelly_contracts(self, ev_c: float, ask_c: int,
                        balance_cents: int) -> int:
        """Kelly fraction → contract count, capped at max_contracts."""
        if ev_c <= 0 or ask_c <= 0 or balance_cents <= 0:
            return 0
        edge = ev_c / 100.0
        p_win = (ask_c + ev_c) / 100.0
        if p_win >= 1.0 or p_win <= 0.0:
            return 0
        # Standard Kelly for a binary at price p_win paying 1 unit:
        # f* = edge / (1 - p_win)   when stake = ask, payout = 100 - ask.
        kelly_f = edge / (1.0 - p_win)
        kelly_f = max(0.0, min(kelly_f, self.kelly_cap_frac))
        dollars = (balance_cents / 100.0) * kelly_f
        per_contract_dollars = ask_c / 100.0
        if per_contract_dollars <= 0:
            return 0
        n = int(dollars / per_contract_dollars)
        return max(1, min(n, self.max_contracts)) if n > 0 else 0

    def should_enter(
        self,
        *,
        fair_prob: Optional[float],
        mid_cents: Optional[float],
        best_yes_ask: Optional[int],
        best_no_ask: Optional[int],
        balance_cents: int,
        seconds_left: Optional[float],
        btc_price: Optional[float] = None,
        strike: Optional[float] = None,
        btc_5m_move: Optional[float] = None,
        kalshi_lag: Optional[float] = None,
        book_imbalance: Optional[float] = None,
        microprice_cents: Optional[float] = None,
        taker_imbalance: Optional[float] = None,
        ta_composite_score: Optional[float] = None,
        own_wall_verdict: Optional[str] = None,
        opp_wall_verdict: Optional[str] = None,
    ) -> Optional[UnifiedSignal]:
        """Run the full pipeline. Return UnifiedSignal if we should fire,
        else None.

        VETOES (return None unconditionally):
            * seconds_left < min_seconds
            * fair_prob unusable
            * opposing wall AGGRESSIVE_BUY
        """
        if seconds_left is None or seconds_left < self.min_seconds:
            return None
        if not _is_finite(fair_prob) or fair_prob <= 0 or fair_prob >= 1:
            return None

        composite = self.score_composite(
            fair_prob=fair_prob,
            mid_cents=mid_cents,
            btc_price=btc_price,
            strike=strike,
            btc_5m_move=btc_5m_move,
            kalshi_lag=kalshi_lag,
            book_imbalance=book_imbalance,
            microprice_cents=microprice_cents,
            taker_imbalance=taker_imbalance,
            ta_composite_score=ta_composite_score,
            seconds_left=seconds_left,
            own_wall_verdict=own_wall_verdict,
            opp_wall_verdict=opp_wall_verdict,
        )
        side = composite["side"]
        if side is None:
            return None
        if composite["confidence"] < self.min_confidence:
            return None

        # Pick the ask and check opposing-wall veto on whichever side we'd hit.
        if side == "yes":
            ask = int(best_yes_ask) if _is_finite(best_yes_ask) else 0
            opp_v = opp_wall_verdict  # opp = the NO side from YES's POV
        else:
            ask = int(best_no_ask) if _is_finite(best_no_ask) else 0
            # If caller is consistent (own=our chosen side's stack), this is fine.
            opp_v = opp_wall_verdict
        if ask <= 0 or ask >= 100:
            return None
        if opp_v == "AGGRESSIVE_BUY":
            return None

        ev = self.ev_cents(side, float(fair_prob), ask)
        if ev <= self.min_ev_c:
            return None

        contracts = self.kelly_contracts(ev, ask, balance_cents)
        if contracts <= 0:
            return None

        reason = (
            f"{side.upper()} score={composite['score']:+.1f} "
            f"conf={composite['confidence']:.2f} ev={ev:+.1f}c ask={ask}c "
            f"size={contracts}"
        )

        return UnifiedSignal(
            side=side,
            score=composite["score"],
            confidence=composite["confidence"],
            ev_cents=round(ev, 2),
            ask_cents=ask,
            fair_prob=round(float(fair_prob), 4),
            recommended_contracts=contracts,
            components=composite["components"],
            reason=reason,
        )


# ── Backtest adapter ─────────────────────────────────────────────────────


def backtest_decide(
    btc: float,
    strike: float,
    mom: float,
    prob: float,
    mid: float,
    ask_yes: int,
    ask_no: int,
    seconds_left: float,
    *,
    balance_cents: int = 5_000,
    scorer: Optional[UnifiedScorer] = None,
) -> Optional[UnifiedSignal]:
    """Backtest-friendly entry point.

    Maps the historical-snapshot columns to should_enter() inputs, leaving
    live-only signals (kalshi_lag, book_imbalance, taker_imbalance, TA,
    wall) as None so they contribute weight 0. This isolates the
    BB + momentum + timing weights for corpus testing."""
    if scorer is None:
        scorer = UnifiedScorer()
    return scorer.should_enter(
        fair_prob=prob,
        mid_cents=mid,
        best_yes_ask=ask_yes,
        best_no_ask=ask_no,
        balance_cents=balance_cents,
        seconds_left=seconds_left,
        btc_price=btc,
        strike=strike,
        btc_5m_move=mom,
        kalshi_lag=None,
        book_imbalance=None,
        microprice_cents=None,
        taker_imbalance=None,
        ta_composite_score=None,
        own_wall_verdict=None,
        opp_wall_verdict=None,
    )
