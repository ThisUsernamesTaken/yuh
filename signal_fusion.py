# signal_fusion.py — Cross-venue signal fusion: Polymarket 5m → Kalshi 15m
#
# Core thesis (from research paper): Polymarket's 5m BTC markets reprice faster
# than Kalshi's 15m markets during short bursts of directional conviction. When
# Polymarket has moved but Kalshi has not yet fully repriced, passive Kalshi
# limit orders can be posted at prices favorable relative to the faster venue.
#
# This module evaluates that gap and decides whether entry is warranted.
# It NEVER maps Polymarket price directly to Kalshi fair value. It applies a
# translation layer that accounts for signal age, time remaining, noise, endgame
# instability, and whether Kalshi has already priced in the move.
#
# Usage (from HFTEngine._check_scalp_entry):
#   result = SignalFusion.evaluate(book, features, side, contract, expected_wr)
#   if not result.approved: return  (reject)

import time
from dataclasses import dataclass
from typing import Optional

from kalshi_client import KalshiOrderBook
from polymarket_features import PolyFeatures
from config import (
    FUSION_W_KALSHI_MICROPRICE, FUSION_W_KALSHI_MID, FUSION_W_POLY_SIGNAL,
    FUSION_MIN_POLY_PROB_CHANGE_3S, FUSION_MIN_POLY_IMBALANCE,
    FUSION_MAX_POLY_NOISE, FUSION_MAX_SIGNAL_SHIFT_CENTS,
    FUSION_MIN_NET_EDGE_WITH_POLY, FUSION_ALREADY_REPRICED_THRESHOLD,
    FUSION_POLY_ALPHA, FUSION_POLY_BETA, FUSION_POLY_GAMMA, FUSION_POLY_DELTA,
    POLY_ENDGAME_SECONDS, POLY_STALE_SIGNAL_MS,
)

import logging
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Result model
# ─────────────────────────────────────────────

@dataclass
class FusionResult:
    """Output of one SignalFusion evaluation."""
    approved: bool
    reason_code: str            # "approved" or the specific rejection reason
    fused_fair_cents: float     # estimated Kalshi fair value in cents (0–100)
    translated_poly_cents: float  # Polymarket's contribution to fair value
    gross_edge_cents: float     # fused_fair - market_ask
    net_edge_cents: float       # gross_edge - spread (friction proxy)

    # Diagnostic context
    up_mid: float = 0.0
    prob_change_3s: float = 0.0
    top_imbalance: float = 0.0
    noise_score: float = 0.0
    poly_seconds_to_expiry: float = 0.0


# ─────────────────────────────────────────────
# Fusion engine
# ─────────────────────────────────────────────

class SignalFusion:
    """Stateless evaluator: all logic in the class method evaluate()."""

    @classmethod
    def evaluate(
        cls,
        book: KalshiOrderBook,
        features: Optional[PolyFeatures],
        side: str,              # "yes" or "no"
        contract_minutes: float,
        expected_wr: float,     # fallback from main engine regime×session WR
    ) -> FusionResult:
        """Evaluate whether Polymarket evidence supports a Kalshi entry.

        If Polymarket features are absent, invalid, or fail quality gates, returns
        an unapproved result with the appropriate reason code. The HFT engine then
        falls back to the existing expected_wr-based edge logic.

        If approved, returns the fused fair value and edge for the caller to use
        in place of (or alongside) the expected_wr calculation.
        """

        # ── Null guard ────────────────────────────────────────────────────
        if features is None:
            return cls._reject("poly_unavailable", book, side)

        # ── Feed quality gates ────────────────────────────────────────────
        if features.feed_health < 0.30:
            return cls._reject("poly_feed_unhealthy", book, side, features)

        if features.noise_score > FUSION_MAX_POLY_NOISE:
            return cls._reject("poly_noise_too_high", book, side, features)

        if features.is_endgame:
            return cls._reject("poly_endgame", book, side, features)

        if features.seconds_to_expiry <= 0:
            return cls._reject("poly_expired", book, side, features)

        # ── Directional signal check ───────────────────────────────────────
        # For a YES (UP) entry: Polymarket UP should be moving upward.
        # For a NO (DOWN) entry: Polymarket UP should be moving downward.
        if side == "yes":
            prob_change = features.prob_change_3s
            imbalance_aligned = features.top_imbalance
        else:
            # NO entry: invert direction — falling UP prob = rising DOWN prob
            prob_change = -features.prob_change_3s
            imbalance_aligned = -features.top_imbalance

        if prob_change < FUSION_MIN_POLY_PROB_CHANGE_3S:
            return cls._reject("poly_signal_too_weak", book, side, features)

        if imbalance_aligned < FUSION_MIN_POLY_IMBALANCE:
            return cls._reject("poly_imbalance_insufficient", book, side, features)

        # ── Translation layer ─────────────────────────────────────────────
        # Converts the raw Polymarket UP mid (0–1) into a Kalshi fair-value
        # adjustment in cents. Components:
        #   base:       raw probability × 100 (naive direct mapping, then modulated)
        #   velocity:   alpha × velocity × 100 (momentum continuation signal)
        #   imbalance:  beta × top_imbalance × 100 (order-flow direction signal)
        #   noise:      -gamma × noise_score × 100 (penalise noisy markets)
        #   endgame:    -delta × endgame_factor (penalise approaching expiry)
        base_signal = features.up_mid * 100.0          # 0–100 cents
        velocity_boost = FUSION_POLY_ALPHA * features.velocity * 100.0
        imbalance_boost = FUSION_POLY_BETA * features.top_imbalance * 100.0
        noise_penalty = FUSION_POLY_GAMMA * features.noise_score * 100.0

        # Endgame factor rises from 0 at 90s to 1 at POLY_ENDGAME_SECONDS
        endgame_t = features.seconds_to_expiry
        endgame_factor = max(0.0, 1.0 - (endgame_t - POLY_ENDGAME_SECONDS) / 45.0)
        endgame_penalty = FUSION_POLY_DELTA * endgame_factor * 100.0

        raw_translated = (
            base_signal
            + velocity_boost
            + imbalance_boost
            - noise_penalty
            - endgame_penalty
        )

        # Compress: cap how far the Polymarket signal can shift the Kalshi fair
        # value away from 50¢. Prevents over-relying on a single external signal.
        shift = raw_translated - 50.0
        clamped_shift = max(-FUSION_MAX_SIGNAL_SHIFT_CENTS, min(FUSION_MAX_SIGNAL_SHIFT_CENTS, shift))
        translated_poly_cents = 50.0 + clamped_shift

        # For NO entry, invert: low UP probability → high NO fair value
        if side == "no":
            translated_poly_cents = 100.0 - translated_poly_cents

        # ── Fused fair value ───────────────────────────────────────────────
        # Weighted blend: microprice + mid + Polymarket translated signal.
        # The BTC spot and regime weights are currently folded into microprice.
        w_micro = FUSION_W_KALSHI_MICROPRICE
        w_mid   = FUSION_W_KALSHI_MID
        w_poly  = FUSION_W_POLY_SIGNAL
        w_sum   = w_micro + w_mid + w_poly

        kalshi_microprice = book.microprice_cents
        kalshi_mid = book.mid_cents

        fused_fair = (
            w_micro * kalshi_microprice
            + w_mid  * kalshi_mid
            + w_poly * translated_poly_cents
        ) / w_sum

        # ── Already-repriced guard ─────────────────────────────────────────
        # If the Kalshi ask is already close to (or past) the fused fair value,
        # the venue has already internalized the Polymarket signal. No edge left.
        market_ask = book.best_yes_ask if side == "yes" else book.best_no_ask
        gross_edge = fused_fair - market_ask

        if gross_edge < -FUSION_ALREADY_REPRICED_THRESHOLD:
            return cls._reject("kalshi_already_repriced", book, side, features,
                               fused_fair, translated_poly_cents, gross_edge)

        # ── Net edge check ─────────────────────────────────────────────────
        net_edge = gross_edge - book.spread_cents   # conservative: full spread friction

        if net_edge < FUSION_MIN_NET_EDGE_WITH_POLY:
            return cls._reject("fusion_net_edge_insufficient", book, side, features,
                               fused_fair, translated_poly_cents, gross_edge, net_edge)

        logger.debug(
            "Fusion approved [%s]: up_mid=%.3f poly_translated=%.1f¢ fused=%.1f¢ ask=%d¢ edge=%.1f¢",
            side, features.up_mid, translated_poly_cents, fused_fair, market_ask, net_edge,
        )

        return FusionResult(
            approved=True,
            reason_code="approved",
            fused_fair_cents=round(fused_fair, 2),
            translated_poly_cents=round(translated_poly_cents, 2),
            gross_edge_cents=round(gross_edge, 2),
            net_edge_cents=round(net_edge, 2),
            up_mid=features.up_mid,
            prob_change_3s=features.prob_change_3s,
            top_imbalance=features.top_imbalance,
            noise_score=features.noise_score,
            poly_seconds_to_expiry=features.seconds_to_expiry,
        )

    # ── Helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _reject(
        reason: str,
        book: KalshiOrderBook,
        side: str,
        features: Optional[PolyFeatures] = None,
        fused_fair: float = 0.0,
        translated_poly: float = 0.0,
        gross_edge: float = 0.0,
        net_edge: float = 0.0,
    ) -> FusionResult:
        return FusionResult(
            approved=False,
            reason_code=reason,
            fused_fair_cents=round(fused_fair, 2),
            translated_poly_cents=round(translated_poly, 2),
            gross_edge_cents=round(gross_edge, 2),
            net_edge_cents=round(net_edge, 2),
            up_mid=features.up_mid if features else 0.0,
            prob_change_3s=features.prob_change_3s if features else 0.0,
            top_imbalance=features.top_imbalance if features else 0.0,
            noise_score=features.noise_score if features else 0.0,
            poly_seconds_to_expiry=features.seconds_to_expiry if features else 0.0,
        )
