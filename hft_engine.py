# hft_engine.py — High-frequency order-book-driven trading loop
#
# Runs as a parallel asyncio task alongside the candle-driven Engine.
# Three strategies:
#
#   1. YES+NO Arbitrage
#      When yes_ask + no_ask < (100 - MIN_EDGE), buy both sides simultaneously.
#      Guaranteed profit regardless of BTC direction.
#
#   2. Directional Scalping (candle-engine-triggered)
#      Requires a live ConsensusSignal + FilterDecision from the main engine.
#      Uses expected_wr from the calibrated regime×session WR table as probability.
#      Edge: gross_edge = expected_wr_cents - market_ask_cents
#            net_edge   = gross_edge - spread_cents
#
#   3. Poly-Primary Directional Scalp
#      Fires in the elif branch — only when Strategy 2 has no signal/decision.
#      Direction and fair value come entirely from Polymarket's 5m order book.
#      Uses SignalFusion with a neutral 50¢ prior instead of regime WR.
#      Poll rate adapts to 1.5s (from 4s) whenever Poly state is active and fresh.
#
# Shared references (all written by main.py, read here — asyncio single-threaded, no locks):
#   signal_ref["signal"]        — latest ConsensusSignal (updated every 1m candle)
#   decision_ref["decision"]    — latest FilterDecision (regime, expected_wr, reason)
#   submit_lock["active"]       — True while main engine is mid place_order
#   submit_lock["ticker"]       — which ticker main is submitting on
#   position_mgr.open_orders    — main engine's live positions (for coordination)

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import (
    HFT_POLL_INTERVAL, HFT_MIN_MINUTES_REMAINING,
    HFT_ARB_MIN_EDGE_CENTS, HFT_ARB_MAX_CONTRACTS,
    HFT_SCALP_GROSS_EDGE_FLOOR, HFT_SCALP_NET_EDGE_FLOOR,
    HFT_SCALP_TARGET_CENTS, HFT_SCALP_STOP_CENTS, HFT_TP_LIMIT_PREMIUM_CENTS,
    HFT_SCALP_ENTRY_TIMEOUT, HFT_SCALP_MAX_PER_WINDOW,
    HFT_SCALP_MIN_ENTRY_CENTS, HFT_SCALP_MAX_ENTRY_CENTS,
    HFT_REQUIRE_FAVORABLE_REGIME,
    HFT_ALLOW_OPPOSE_MAIN, HFT_MAX_ADDITIVE_CONTRACTS,
    # Microstructure controls
    HFT_MAX_BOOK_AGE_MS, HFT_MAX_REPRICES_PER_ORDER,
    HFT_TIGHTEN_INVENTORY_MINUTES, HFT_MIN_DEPTH_CONTRACTS,
    HFT_USE_IMBALANCE_GATE, HFT_IMBALANCE_OPPOSE_THRESHOLD,
    HFT_POST_FILL_DRIFT_SECONDS,
    # Polymarket fusion
    POLY_ENABLED, POLY_STALE_SIGNAL_MS,
    # Poly-primary strategy (Strategy 3)
    HFT_POLY_PRIMARY_ENABLED, HFT_POLY_FAST_POLL_INTERVAL,
    HFT_POLY_PRIMARY_NET_EDGE_FLOOR, HFT_POLY_PRIMARY_MIN_PROB_CHANGE_3S,
    HFT_POLY_PRIMARY_MIN_IMBALANCE, HFT_POLY_PRIMARY_FEED_HEALTH_MIN,
    HFT_POLY_PRIMARY_MAX_NOISE, HFT_POLY_PRIMARY_MID_OFFSET,
    HFT_POLY_PRIMARY_MIN_KALSHI_MINUTES,
)
from kalshi_client import KalshiClient, KalshiOrderBook, KalshiAPIError
from polymarket_features import PolyFeatureComputer, PolyFeatures
from signal_fusion import SignalFusion
from regime_detector import Regime
from signal_intelligence import FilterReason

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class HFTPosition:
    """An open HFT scalp or pending-fill entry."""
    order_id: str
    ticker: str
    side: str           # "yes" or "no"
    count: int
    limit_cents: int    # price the order was posted at
    entry_cents: float  # actual fill price (set when confirmed filled)
    target_cents: float # take-profit exit price
    stop_cents: float   # stop-loss exit price
    posted_at: float    # time.time() when limit was posted
    filled: bool = False
    expiry_ts: int = 0
    log_row_id: int = 0  # hft_log row id for later fill/exit updates
    submit_ts_ms: int = 0
    reprice_count: int = 0  # number of cancel/replace cycles so far


@dataclass
class HFTArbLeg:
    """One leg of a YES+NO arbitrage."""
    order_id: str
    ticker: str
    side: str           # "yes" or "no"
    count: int
    limit_cents: int
    filled: bool = False
    fill_cents: float = 0.0
    log_row_id: int = 0  # hft_log row id


@dataclass
class WindowStats:
    """Per-15m-window HFT activity tracker."""
    ticker: str
    scalp_count: int = 0
    arb_count: int = 0
    realized_pnl: float = 0.0


# ─────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────

class HFTEngine:
    """Fast order-book-driven HFT loop.

    Usage:
        signal_ref = {"signal": None}   # shared with main engine
        hft = HFTEngine(client, position_mgr_equity_ref, signal_logger, signal_ref)
        asyncio.create_task(hft.run())

        # In main engine, after each consensus signal:
        signal_ref["signal"] = latest_consensus_signal
    """

    def __init__(
        self,
        client: KalshiClient,
        equity_ref: list,       # [current_equity_float] — mutable single-element list
        signal_logger,          # SignalLogger instance for DB writes
        signal_ref: dict,       # {"signal": ConsensusSignal | None}
        decision_ref: dict,     # {"decision": FilterDecision | None} — calibrated WR + regime
        position_mgr,           # main engine's PositionManager — read open_orders for coordination
        submit_lock: dict,      # {"active": bool, "ticker": str} — True while main is placing
        poly_ref: Optional[dict] = None,  # {"state": Poly5mState | None} — from PolymarketStream
    ) -> None:
        self._client = client
        self._equity_ref = equity_ref
        self._signal_logger = signal_logger
        self._signal_ref = signal_ref
        self._decision_ref = decision_ref
        self._position_mgr = position_mgr
        self._submit_lock = submit_lock
        self._poly_ref = poly_ref
        self._poly_features = PolyFeatureComputer() if (POLY_ENABLED and poly_ref is not None) else None

        self._scalp: Optional[HFTPosition] = None          # current scalp position
        self._pending_entry: Optional[HFTPosition] = None  # limit order waiting to fill
        self._arb_legs: list[HFTArbLeg] = []               # open arb legs
        self._window: Optional[WindowStats] = None         # current 15m window stats
        self._current_ticker: str = ""

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Runs forever. Call as asyncio.create_task(hft.run())."""
        logger.info(
            "HFT engine started (poll=%.1fs / poly_fast=%.1fs)",
            HFT_POLL_INTERVAL, HFT_POLY_FAST_POLL_INTERVAL,
        )
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error("HFT tick error: %s", e, exc_info=True)
            await asyncio.sleep(self._current_poll_interval())

    async def _tick(self) -> None:
        contracts = await self._client.find_btc_contracts(min_minutes_remaining=0.5)
        if not contracts:
            await self._handle_no_contract()
            return

        contract = contracts[0]
        self._maybe_reset_window(contract.ticker)

        # Fetch order book
        book = await self._client.get_orderbook(contract.ticker)

        # Log book state at debug level every tick
        logger.debug(
            "HFT book: %s | yes=%d/%d¢ no=%d/%d¢ spread=%d¢ mid=%.1f¢ | "
            "yes_liq=%d no_liq=%d | %.1f min left",
            contract.ticker,
            book.best_yes_bid, book.best_yes_ask,
            book.best_no_bid, book.best_no_ask,
            book.spread_cents, book.mid_cents,
            book.total_yes_liquidity(), book.total_no_liquidity(),
            contract.minutes_to_expiry,
        )

        # ── Strategy 1: YES+NO arbitrage ──────────────────────────────────
        arb_fired = await self._check_arb(book, contract)
        if arb_fired:
            return

        # ── Poll pending arb fills ─────────────────────────────────────────
        if self._arb_legs:
            await self._poll_arb_fills(contract)
            return

        # ── Strategy 2: manage open scalp position ────────────────────────
        if self._scalp or self._pending_entry:
            await self._manage_scalp(book, contract)
            return

        # ── New entry: Strategy 2 (candle) or Strategy 3 (Poly-primary) ──
        # Mutually exclusive per tick: Strategy 2 requires a live candle-engine
        # signal/decision; Strategy 3 fires only when that is unavailable.
        # This also prevents _poly_features.update() running twice in one tick.
        if contract.minutes_to_expiry >= HFT_MIN_MINUTES_REMAINING:
            signal = self._signal_ref.get("signal")
            decision = self._decision_ref.get("decision")
            if signal is not None and decision is not None:
                await self._check_scalp_entry(book, contract)
            elif (HFT_POLY_PRIMARY_ENABLED and POLY_ENABLED
                    and self._poly_features is not None):
                await self._check_poly_primary_entry(book, contract)

    # ── Window reset ──────────────────────────────────────────────────────

    def _maybe_reset_window(self, ticker: str) -> None:
        """Reset per-window stats when a new 15m contract opens."""
        if ticker != self._current_ticker:
            if self._window:
                logger.info(
                    "HFT window closed: %s | scalps=%d arbs=%d pnl=%+.2f",
                    self._window.ticker, self._window.scalp_count,
                    self._window.arb_count, self._window.realized_pnl,
                )
            self._current_ticker = ticker
            self._window = WindowStats(ticker=ticker)
            self._scalp = None
            self._pending_entry = None
            self._arb_legs = []
            logger.info("HFT window opened: %s", ticker)

    def _current_poll_interval(self) -> float:
        """Return the appropriate sleep interval for the next tick.

        Drops from 4s to 1.5s whenever Polymarket has a fresh, non-endgame
        active state — keeping the Kalshi book snapshot within the stale-book
        gate window while a Poly-primary entry might be imminent.
        """
        if not (HFT_POLY_PRIMARY_ENABLED and POLY_ENABLED and self._poly_ref is not None):
            return HFT_POLL_INTERVAL
        state = self._poly_ref.get("state")
        if state is None or state.is_endgame:
            return HFT_POLL_INTERVAL
        if state.book_age_ms < POLY_STALE_SIGNAL_MS:
            return HFT_POLY_FAST_POLL_INTERVAL
        return HFT_POLL_INTERVAL

    async def _handle_no_contract(self) -> None:
        """Called when no tradeable contract is found (between windows)."""
        if self._current_ticker:
            logger.debug("HFT: no active contract (between windows)")
            self._current_ticker = ""
            self._scalp = None
            self._pending_entry = None
            self._arb_legs = []

    # ── Strategy 1: YES+NO Arbitrage ─────────────────────────────────────

    async def _check_arb(self, book: KalshiOrderBook, contract) -> bool:
        """Detect and execute YES+NO arbitrage.

        Logs every evaluation where edge >= 1¢ to hft_log, whether or not
        it executes (reject_reason set if blocked before execution).
        """
        if not book.yes_bids or not book.no_bids:
            return False  # no depth — nothing to log

        # Stale book gate: don't arb on stale data
        if book.book_age_ms > HFT_MAX_BOOK_AGE_MS:
            logger.debug("ARB: stale book (%dms), skipping", book.book_age_ms)
            return False

        total_ask = book.best_yes_ask + book.best_no_ask
        edge = 100 - total_ask

        # Only log when edge is meaningful (≥1¢) to avoid noise on every poll
        if edge < 1:
            return False

        eval_ts_ms = int(time.time() * 1000)
        common = dict(
            eval_ts_ms=eval_ts_ms,
            ticker=contract.ticker,
            strategy="ARB",
            spread_cents=book.spread_cents,
            gross_edge_cents=float(edge),
            net_edge_cents=float(edge),  # arb has no friction — both sides guaranteed
            yes_liq=book.total_yes_liquidity(),
            no_liq=book.total_no_liquidity(),
            minutes_to_expiry=round(contract.minutes_to_expiry, 2),
        )

        if edge < HFT_ARB_MIN_EDGE_CENTS:
            # Log the near-miss so we can see how many arb windows we see but don't take
            logger.debug("ARB near-miss: %d¢ edge (need %.0f¢) on %s", edge, HFT_ARB_MIN_EDGE_CENTS, contract.ticker)
            await self._signal_logger.log_hft_eval(
                decision="rejected", reject_reason="edge_below_min",
                market_ask_cents=total_ask, **common
            )
            return False

        # Liquidity check
        yes_avail = book.liquidity_within("yes", 0)
        no_avail  = book.liquidity_within("no",  0)
        count = min(yes_avail, no_avail, HFT_ARB_MAX_CONTRACTS)

        if count < 1:
            logger.info("ARB blocked: no liquidity at both asks on %s", contract.ticker)
            await self._signal_logger.log_hft_eval(
                decision="rejected", reject_reason="insufficient_liquidity",
                market_ask_cents=total_ask, **common
            )
            return False

        logger.warning(
            "ARB OPPORTUNITY: %s | yes_ask=%d¢ + no_ask=%d¢ = %d¢ | edge=%d¢ | count=%d",
            contract.ticker, book.best_yes_ask, book.best_no_ask, total_ask, edge, count,
        )

        # Log before placing — capture the opportunity even if order fails
        yes_row_id = await self._signal_logger.log_hft_eval(
            decision="entered", side="yes", market_ask_cents=book.best_yes_ask,
            entry_limit_cents=book.best_yes_ask, **common
        )
        no_row_id = await self._signal_logger.log_hft_eval(
            decision="entered", side="no", market_ask_cents=book.best_no_ask,
            entry_limit_cents=book.best_no_ask, **common
        )

        # Place both legs simultaneously
        try:
            yes_coro = self._client.place_order(
                ticker=contract.ticker, side="yes", count=count,
                price=book.best_yes_ask, order_type="limit",
            )
            no_coro = self._client.place_order(
                ticker=contract.ticker, side="no", count=count,
                price=book.best_no_ask, order_type="limit",
            )
            submit_ts_ms = int(time.time() * 1000)
            yes_order, no_order = await asyncio.gather(yes_coro, no_coro)
            ack_ts_ms = int(time.time() * 1000)
        except KalshiAPIError as e:
            logger.error("ARB order placement failed: %s", e)
            await self._signal_logger.update_hft_row(yes_row_id, reject_reason="order_failed", decision="rejected")
            await self._signal_logger.update_hft_row(no_row_id,  reject_reason="order_failed", decision="rejected")
            return False

        # Update log rows with order ids and submit/ack timestamps
        await self._signal_logger.update_hft_row(
            yes_row_id, order_id=yes_order.order_id,
            submit_ts_ms=submit_ts_ms, ack_ts_ms=ack_ts_ms,
        )
        await self._signal_logger.update_hft_row(
            no_row_id, order_id=no_order.order_id,
            submit_ts_ms=submit_ts_ms, ack_ts_ms=ack_ts_ms,
        )

        self._arb_legs = [
            HFTArbLeg(
                order_id=yes_order.order_id, ticker=contract.ticker,
                side="yes", count=count, limit_cents=book.best_yes_ask,
                log_row_id=yes_row_id,
            ),
            HFTArbLeg(
                order_id=no_order.order_id, ticker=contract.ticker,
                side="no", count=count, limit_cents=book.best_no_ask,
                log_row_id=no_row_id,
            ),
        ]
        if self._window:
            self._window.arb_count += 1

        logger.info(
            "ARB legs placed: YES=%s @ %d¢  NO=%s @ %d¢  ack_latency=%dms",
            yes_order.order_id[:8], book.best_yes_ask,
            no_order.order_id[:8], book.best_no_ask,
            ack_ts_ms - submit_ts_ms,
        )
        return True

    async def _poll_arb_fills(self, contract) -> None:
        """Check fill status of open arb legs. Log when both confirmed."""
        all_filled = True
        for leg in self._arb_legs:
            if leg.filled:
                continue
            try:
                order = await self._client.get_order(leg.order_id)
                if order.filled_count > 0:
                    leg.filled = True
                    leg.fill_cents = order.average_price or float(leg.limit_cents)
                    logger.info(
                        "ARB leg filled: %s %s x%d @ %.1f¢",
                        leg.side.upper(), leg.ticker[:20], order.filled_count, leg.fill_cents,
                    )
                else:
                    all_filled = False
            except Exception as e:
                logger.warning("ARB fill poll failed for %s: %s", leg.order_id[:8], e)
                all_filled = False

        if all_filled and all(l.fill_cents > 0 for l in self._arb_legs):
            total_cost = sum(l.fill_cents for l in self._arb_legs)
            guaranteed_pnl = (100.0 - total_cost) * self._arb_legs[0].count / 100.0
            logger.info(
                "ARB COMPLETE: cost=%.1f¢ | guaranteed pnl=+$%.4f per contract | total=+$%.4f",
                total_cost, (100.0 - total_cost) / 100.0, guaranteed_pnl,
            )
            placed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            exit_ts_ms = int(time.time() * 1000)
            for leg in self._arb_legs:
                # Update hft_log with fill + pnl
                leg_pnl = guaranteed_pnl / 2  # split evenly between legs for accounting
                await self._signal_logger.update_hft_row(
                    leg.log_row_id,
                    fill_cents=leg.fill_cents,
                    pnl=leg_pnl,
                    exit_ts_ms=exit_ts_ms,
                )
                # Also log to kalshi_trades for ledger visibility
                await self._signal_logger.log_kalshi_trade(
                    order_id=leg.order_id,
                    placed_at=placed_at,
                    ticker=leg.ticker,
                    side=leg.side,
                    count=leg.count,
                    limit_price=leg.limit_cents,
                    dollar_risk=leg.count * leg.limit_cents / 100.0,
                    strategy_name="HFT_ARB",
                    expected_wr=100.0,
                )
            if self._window:
                self._window.realized_pnl += guaranteed_pnl
            self._equity_ref[0] += guaranteed_pnl
            self._arb_legs = []

    # ── Strategy 2: Directional Scalping ─────────────────────────────────

    async def _check_scalp_entry(self, book: KalshiOrderBook, contract) -> None:
        """Enter a new scalp only when net edge (after friction proxy) is positive.

        Edge calculation:
          expected_wr    — from main engine's calibrated FilterDecision (regime×session WR)
          gross_edge     = expected_wr_cents - market_ask_cents
          friction_proxy = spread_cents  (conservative: assumes worst-case round-trip cost)
          net_edge       = gross_edge - friction_proxy

        Coordination gates (evaluated before any order):
          1. Regime: blocks VOLATILE/UNKNOWN same as main engine
          2. Bad hour: respects main engine's hour filter
          3. No-oppose: if main has open position on same ticker in opposite direction
          4. Additive cap: total contracts across main+HFT in same direction
          5. Submit lock: pause if main engine is mid-submit on same ticker
        """
        eval_ts_ms = int(time.time() * 1000)

        async def _reject(reason: str, **extra) -> None:
            logger.debug("HFT scalp rejected [%s]: %s", reason, contract.ticker[:20])
            await self._signal_logger.log_hft_eval(
                eval_ts_ms=eval_ts_ms, ticker=contract.ticker,
                strategy="SCALP", decision="rejected", reject_reason=reason,
                spread_cents=book.spread_cents,
                yes_liq=book.total_yes_liquidity(), no_liq=book.total_no_liquidity(),
                minutes_to_expiry=round(contract.minutes_to_expiry, 2),
                **extra,
            )

        # Window cap
        if self._window and self._window.scalp_count >= HFT_SCALP_MAX_PER_WINDOW:
            return  # silent — not a real rejection, just throttle

        # ── Gate 0: book freshness ─────────────────────────────────────────
        # REST polls can lag; never trade on stale data
        if book.book_age_ms > HFT_MAX_BOOK_AGE_MS:
            await _reject("stale_book", book_age_ms=book.book_age_ms)
            return

        signal = self._signal_ref.get("signal")
        decision = self._decision_ref.get("decision")

        if signal is None or decision is None:
            return

        if signal.direction not in ("CALL", "PUT"):
            return

        # ── Gate 1: regime ────────────────────────────────────────────────
        # When HFT_REQUIRE_FAVORABLE_REGIME=False (default), allow all regimes.
        # The edge floor (gross_edge >= HFT_SCALP_GROSS_EDGE_FLOOR) is the real
        # quality gate — expected_wr already incorporates regime via the WR table.
        regime = decision.regime.regime
        if HFT_REQUIRE_FAVORABLE_REGIME:
            if regime in (Regime.UNKNOWN,):
                await _reject("regime_unknown", regime=regime.value)
                return
            if not decision.regime.is_favorable:
                await _reject("regime_unfavorable", regime=regime.value)
                return

        # ── Gate 2: bad hour ──────────────────────────────────────────────
        if decision.reason == FilterReason.BAD_HOUR:
            await _reject("bad_hour", regime=regime.value)
            return

        # ── Set side and market prices ─────────────────────────────────────
        side = "yes" if signal.direction == "CALL" else "no"
        market_ask = book.best_yes_ask if side == "yes" else book.best_no_ask
        market_bid = book.best_yes_bid if side == "yes" else book.best_no_bid

        if market_ask <= 0 or market_bid <= 0:
            await _reject("no_depth", side=side, regime=regime.value)
            return

        # ── Gate 3: entry price band ──────────────────────────────────────
        # Avoid contracts already priced far from 50¢ — the move has happened,
        # the edge model's expected_wr is a regime-level estimate, not a
        # contract-specific one. Near-certain contracts (e.g. 12¢ YES after a
        # big BTC drop) have high apparent edge but low real probability of reversal.
        if market_ask < HFT_SCALP_MIN_ENTRY_CENTS or market_ask > HFT_SCALP_MAX_ENTRY_CENTS:
            await _reject(
                "entry_price_out_of_band",
                side=side, market_ask_cents=market_ask, regime=regime.value,
            )
            return

        # ── Gate 4: opposite-side depth ───────────────────────────────────
        # Require at least HFT_MIN_DEPTH_CONTRACTS within 2¢ of best ask on
        # the side we intend to buy. Thin books mean we're buying into a wall
        # that can vanish before our limit fills.
        opp_depth = book.liquidity_within(side, 2)
        if opp_depth < HFT_MIN_DEPTH_CONTRACTS:
            await _reject(
                "insufficient_depth",
                side=side, market_ask_cents=market_ask, regime=regime.value,
            )
            return

        # ── Gate 5: imbalance ──────────────────────────────────────────────
        # Block entries when top-of-book pressure strongly opposes the trade
        # direction. Imbalance > 0 = YES demand dominant; < 0 = NO dominant.
        imb = book.imbalance  # always capture for logging regardless of gate config
        if HFT_USE_IMBALANCE_GATE:
            imb_opposes = (
                (side == "yes" and imb < HFT_IMBALANCE_OPPOSE_THRESHOLD) or
                (side == "no"  and imb > -HFT_IMBALANCE_OPPOSE_THRESHOLD)
            )
            if imb_opposes:
                await _reject(
                    "imbalance_opposes_side",
                    side=side, market_ask_cents=market_ask,
                    regime=regime.value,
                )
                return
        # ── Edge calculation ───────────────────────────────────────────────
        # Use calibrated WR from regime×session table, not raw signal.confidence
        expected_wr = decision.expected_wr          # e.g. 63.5  (percentage)
        gross_edge  = expected_wr - market_ask      # e.g. 63.5 - 55 = 8.5¢
        friction    = float(book.spread_cents)      # conservative proxy: full round-trip spread
        net_edge    = gross_edge - friction

        if gross_edge < HFT_SCALP_GROSS_EDGE_FLOOR:
            await _reject(
                "gross_edge_insufficient",
                side=side, expected_wr=expected_wr,
                market_ask_cents=market_ask,
                gross_edge_cents=round(gross_edge, 2), net_edge_cents=round(net_edge, 2),
                regime=regime.value,
            )
            return

        if net_edge < HFT_SCALP_NET_EDGE_FLOOR:
            await _reject(
                "net_edge_insufficient",
                side=side, expected_wr=expected_wr,
                market_ask_cents=market_ask,
                gross_edge_cents=round(gross_edge, 2), net_edge_cents=round(net_edge, 2),
                regime=regime.value,
            )
            return

        # ── Gate: near-expiry tightening ──────────────────────────────────
        # Below HFT_TIGHTEN_INVENTORY_MINUTES, adverse selection and resolution
        # risk increase sharply. Require an extra 2¢ of net edge as compensation.
        if contract.minutes_to_expiry < HFT_TIGHTEN_INVENTORY_MINUTES:
            tightened_floor = HFT_SCALP_NET_EDGE_FLOOR + 2.0
            if net_edge < tightened_floor:
                await _reject(
                    "near_expiry_edge_insufficient",
                    side=side, expected_wr=expected_wr,
                    market_ask_cents=market_ask,
                    gross_edge_cents=round(gross_edge, 2), net_edge_cents=round(net_edge, 2),
                    regime=regime.value, minutes_to_expiry=contract.minutes_to_expiry,
                )
                return

        # ── Gate 3: submit lock ───────────────────────────────────────────
        if self._submit_lock.get("active") and self._submit_lock.get("ticker") == contract.ticker:
            await _reject("main_submitting", side=side, regime=regime.value)
            return

        # ── Gate 6: Polymarket signal fusion ──────────────────────────────
        # If Polymarket is connected, evaluate the cross-venue signal. The fusion
        # result either approves entry and provides a better fair-value estimate,
        # or rejects with a reason. When Polymarket is unavailable (poly_ref=None
        # or no active market), this gate is skipped and we fall back to the
        # expected_wr-based edge that already passed Gate 2 above.
        fusion_result = None
        if self._poly_features is not None and self._poly_ref is not None:
            poly_state = self._poly_ref.get("state")
            poly_feats = self._poly_features.update(poly_state)
            fusion_result = SignalFusion.evaluate(
                book=book,
                features=poly_feats,
                side=side,
                contract_minutes=contract.minutes_to_expiry,
                expected_wr=expected_wr,
            )
            # Log fusion attempt regardless of outcome (for research)
            if poly_feats is not None:
                logger.debug(
                    "Fusion [%s]: %s | up_mid=%.3f change_3s=%.3f imb=%.2f noise=%.2f edge=%.1f¢",
                    side, fusion_result.reason_code,
                    fusion_result.up_mid, fusion_result.prob_change_3s,
                    fusion_result.top_imbalance, fusion_result.noise_score,
                    fusion_result.net_edge_cents,
                )
            # Only reject if Polymarket is active but the signal is bad.
            # If Polymarket is unavailable (poly_unavailable / poly_expired), pass through.
            if fusion_result is not None and not fusion_result.approved:
                passthrough_reasons = {"poly_unavailable", "poly_expired"}
                if fusion_result.reason_code not in passthrough_reasons:
                    await _reject(
                        f"fusion_{fusion_result.reason_code}",
                        side=side, market_ask_cents=market_ask,
                        regime=regime.value,
                        gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
                        net_edge_cents=round(fusion_result.net_edge_cents, 2),
                    )
                    return

        # ── Gate 4+5: coordination with main engine open orders ───────────
        for trade in self._position_mgr.open_orders.values():
            if trade.intent.contract.ticker != contract.ticker:
                continue
            main_side = trade.intent.side
            if _sides_oppose(main_side, side) and not HFT_ALLOW_OPPOSE_MAIN:
                await _reject(
                    "opposes_main_position", side=side,
                    regime=regime.value, gross_edge_cents=round(gross_edge, 2),
                )
                return
            if main_side == side:
                total = trade.intent.count + 1  # 1 = what HFT would add
                if total > HFT_MAX_ADDITIVE_CONTRACTS:
                    await _reject(
                        "additive_cap_reached", side=side,
                        regime=regime.value, gross_edge_cents=round(gross_edge, 2),
                    )
                    return

        # ── All gates passed — log entry and place order ──────────────────
        spread = market_ask - market_bid
        step = max(1, spread // 2)
        limit = min(market_ask, market_bid + step)

        target = limit + HFT_SCALP_TARGET_CENTS
        stop   = limit - HFT_SCALP_STOP_CENTS

        log_row_id = await self._signal_logger.log_hft_eval(
            eval_ts_ms=eval_ts_ms, ticker=contract.ticker,
            strategy="SCALP", decision="entered", side=side,
            expected_wr=expected_wr, market_ask_cents=market_ask,
            spread_cents=book.spread_cents,
            gross_edge_cents=round(gross_edge, 2), net_edge_cents=round(net_edge, 2),
            yes_liq=book.total_yes_liquidity(), no_liq=book.total_no_liquidity(),
            minutes_to_expiry=round(contract.minutes_to_expiry, 2),
            regime=regime.value, entry_limit_cents=limit,
            imbalance=round(imb, 3), microprice_cents=round(book.microprice_cents, 2),
            # Polymarket fusion fields (null when Polymarket unavailable)
            poly_up_mid=round(fusion_result.up_mid, 4) if fusion_result else None,
            poly_prob_change_3s=round(fusion_result.prob_change_3s, 4) if fusion_result else None,
            poly_imbalance=round(fusion_result.top_imbalance, 3) if fusion_result else None,
            poly_noise=round(fusion_result.noise_score, 3) if fusion_result else None,
            fusion_fair_cents=round(fusion_result.fused_fair_cents, 2) if fusion_result and fusion_result.approved else None,
        )

        logger.info(
            "HFT SCALP ENTRY: %s %s x1 @ %d¢ | wr=%.1f%% gross=%.1f¢ net=%.1f¢ spread=%d¢ | "
            "target=%d¢ stop=%d¢ | %.1f min left",
            side.upper(), contract.ticker[:20], limit,
            expected_wr, gross_edge, net_edge, book.spread_cents,
            round(target), round(stop), contract.minutes_to_expiry,
        )

        try:
            submit_ts_ms = int(time.time() * 1000)
            order = await self._client.place_order(
                ticker=contract.ticker, side=side, count=1,
                price=limit, order_type="limit",
            )
            ack_ts_ms = int(time.time() * 1000)
        except KalshiAPIError as e:
            logger.warning("HFT scalp entry failed: %s", e)
            await self._signal_logger.update_hft_row(
                log_row_id, decision="rejected", reject_reason="order_failed"
            )
            return

        await self._signal_logger.update_hft_row(
            log_row_id,
            order_id=order.order_id,
            submit_ts_ms=submit_ts_ms,
            ack_ts_ms=ack_ts_ms,
        )

        self._pending_entry = HFTPosition(
            order_id=order.order_id,
            ticker=contract.ticker,
            side=side,
            count=1,
            limit_cents=limit,
            entry_cents=float(limit),
            target_cents=target,
            stop_cents=stop,
            posted_at=time.time(),
            expiry_ts=contract.expiry_ts,
            log_row_id=log_row_id,
            submit_ts_ms=submit_ts_ms,
        )

    async def _check_poly_primary_entry(self, book: KalshiOrderBook, contract) -> None:
        """Strategy 3: enter a directional scalp using Polymarket 5m as the signal source.

        Unlike Strategy 2, this path does not require a ConsensusSignal or FilterDecision
        from the candle engine. Direction and fair value are derived from Polymarket's live
        order book. Uses SignalFusion with a neutral 50¢ prior in place of regime WR.

        Stricter Poly quality gates than the fusion gate in Strategy 2 — no candle
        confirmation means the Poly signal must be cleaner to compensate.
        """
        eval_ts_ms = int(time.time() * 1000)

        async def _reject(reason: str, **extra) -> None:
            await self._signal_logger.log_hft_eval(
                eval_ts_ms=eval_ts_ms, ticker=contract.ticker,
                strategy="SCALP_POLY", decision="rejected", reject_reason=reason,
                spread_cents=book.spread_cents,
                yes_liq=book.total_yes_liquidity(), no_liq=book.total_no_liquidity(),
                minutes_to_expiry=round(contract.minutes_to_expiry, 2),
                **extra,
            )

        # Window cap (shared with regular scalp count)
        if self._window and self._window.scalp_count >= HFT_SCALP_MAX_PER_WINDOW:
            return

        # ── Gate 0: Kalshi book freshness ────────────────────────────────
        if book.book_age_ms > HFT_MAX_BOOK_AGE_MS:
            await _reject("stale_book", book_age_ms=book.book_age_ms)
            return

        # ── Gate 1: window correlation ───────────────────────────────────
        # Poly 5m ≈ Kalshi 15m only in the first half of the Kalshi window.
        # After ~7 min the current Poly 5m window no longer overlaps the
        # BTC price path the Kalshi contract is measuring.
        if contract.minutes_to_expiry < HFT_POLY_PRIMARY_MIN_KALSHI_MINUTES:
            await _reject(
                "kalshi_window_late",
                minutes_to_expiry=round(contract.minutes_to_expiry, 2),
            )
            return

        # ── Gate 2: Poly feature extraction ─────────────────────────────
        poly_state = self._poly_ref.get("state") if self._poly_ref else None
        features = self._poly_features.update(poly_state)
        if features is None:
            return  # silent — stale or missing state

        # ── Gate 3: Poly feed quality ────────────────────────────────────
        if features.feed_health < HFT_POLY_PRIMARY_FEED_HEALTH_MIN:
            await _reject("poly_feed_unhealthy", feed_health=round(features.feed_health, 2))
            return

        if features.noise_score > HFT_POLY_PRIMARY_MAX_NOISE:
            await _reject("poly_noise_too_high", noise_score=round(features.noise_score, 2))
            return

        if features.is_endgame:
            await _reject("poly_endgame", seconds_to_expiry=round(features.seconds_to_expiry, 1))
            return

        # ── Gate 4: directional conviction ──────────────────────────────
        # Require both a clear mid away from 0.50 AND momentum in that direction.
        up_mid = features.up_mid
        prob_change = features.prob_change_3s
        if (up_mid > (0.50 + HFT_POLY_PRIMARY_MID_OFFSET)
                and prob_change >= HFT_POLY_PRIMARY_MIN_PROB_CHANGE_3S):
            side = "yes"
        elif (up_mid < (0.50 - HFT_POLY_PRIMARY_MID_OFFSET)
                and prob_change <= -HFT_POLY_PRIMARY_MIN_PROB_CHANGE_3S):
            side = "no"
        else:
            await _reject(
                "poly_no_direction",
                up_mid=round(up_mid, 3),
                prob_change_3s=round(prob_change, 4),
            )
            return

        # ── Gate 5: Poly order-book imbalance aligned ────────────────────
        aligned_imbalance = features.top_imbalance if side == "yes" else -features.top_imbalance
        if aligned_imbalance < HFT_POLY_PRIMARY_MIN_IMBALANCE:
            await _reject(
                "poly_imbalance_insufficient",
                side=side, up_mid=round(up_mid, 3),
                imbalance=round(features.top_imbalance, 3),
            )
            return

        # ── Gate 6: Kalshi market prices ────────────────────────────────
        market_ask = book.best_yes_ask if side == "yes" else book.best_no_ask
        market_bid = book.best_yes_bid if side == "yes" else book.best_no_bid

        if market_ask <= 0 or market_bid <= 0:
            await _reject("no_depth", side=side)
            return

        # ── Gate 7: entry price band ─────────────────────────────────────
        if market_ask < HFT_SCALP_MIN_ENTRY_CENTS or market_ask > HFT_SCALP_MAX_ENTRY_CENTS:
            await _reject("entry_price_out_of_band", side=side, market_ask_cents=market_ask)
            return

        # ── Gate 8: opposite-side depth ─────────────────────────────────
        opp_depth = book.liquidity_within(side, 2)
        if opp_depth < HFT_MIN_DEPTH_CONTRACTS:
            await _reject("insufficient_depth", side=side, market_ask_cents=market_ask)
            return

        # ── Gate 9: Kalshi book imbalance ────────────────────────────────
        imb = book.imbalance
        if HFT_USE_IMBALANCE_GATE:
            imb_opposes = (
                (side == "yes" and imb < HFT_IMBALANCE_OPPOSE_THRESHOLD) or
                (side == "no"  and imb > -HFT_IMBALANCE_OPPOSE_THRESHOLD)
            )
            if imb_opposes:
                await _reject("imbalance_opposes_side", side=side, market_ask_cents=market_ask)
                return

        # ── Edge via SignalFusion — neutral 50¢ prior ────────────────────
        # No candle regime WR available; 50.0 leaves fair value determined
        # entirely by the Poly-translated signal blended with Kalshi microprice/mid.
        fusion_result = SignalFusion.evaluate(
            book=book,
            features=features,
            side=side,
            contract_minutes=contract.minutes_to_expiry,
            expected_wr=50.0,
        )

        if not fusion_result.approved:
            await _reject(
                f"fusion_{fusion_result.reason_code}",
                side=side, market_ask_cents=market_ask,
                gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
                net_edge_cents=round(fusion_result.net_edge_cents, 2),
                up_mid=round(up_mid, 3),
            )
            return

        if fusion_result.net_edge_cents < HFT_POLY_PRIMARY_NET_EDGE_FLOOR:
            await _reject(
                "poly_net_edge_insufficient",
                side=side, market_ask_cents=market_ask,
                gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
                net_edge_cents=round(fusion_result.net_edge_cents, 2),
                up_mid=round(up_mid, 3),
            )
            return

        # ── Gate 10: near-expiry tightening ─────────────────────────────
        if contract.minutes_to_expiry < HFT_TIGHTEN_INVENTORY_MINUTES:
            tightened = HFT_POLY_PRIMARY_NET_EDGE_FLOOR + 2.0
            if fusion_result.net_edge_cents < tightened:
                await _reject(
                    "near_expiry_edge_insufficient",
                    side=side, market_ask_cents=market_ask,
                    net_edge_cents=round(fusion_result.net_edge_cents, 2),
                    minutes_to_expiry=round(contract.minutes_to_expiry, 2),
                )
                return

        # ── Gate 11: submit lock ─────────────────────────────────────────
        if self._submit_lock.get("active") and self._submit_lock.get("ticker") == contract.ticker:
            await _reject("main_submitting", side=side)
            return

        # ── Gate 12: coordination with main engine open orders ───────────
        for trade in self._position_mgr.open_orders.values():
            if trade.intent.contract.ticker != contract.ticker:
                continue
            main_side = trade.intent.side
            if _sides_oppose(main_side, side) and not HFT_ALLOW_OPPOSE_MAIN:
                await _reject(
                    "opposes_main_position", side=side,
                    gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
                )
                return
            if main_side == side and (trade.intent.count + 1) > HFT_MAX_ADDITIVE_CONTRACTS:
                await _reject(
                    "additive_cap_reached", side=side,
                    gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
                )
                return

        # ── All gates passed — log and place order ───────────────────────
        spread = market_ask - market_bid
        step = max(1, spread // 2)
        limit = min(market_ask, market_bid + step)
        target = limit + HFT_SCALP_TARGET_CENTS
        stop   = limit - HFT_SCALP_STOP_CENTS

        log_row_id = await self._signal_logger.log_hft_eval(
            eval_ts_ms=eval_ts_ms, ticker=contract.ticker,
            strategy="SCALP_POLY", decision="entered", side=side,
            expected_wr=50.0, market_ask_cents=market_ask,
            spread_cents=book.spread_cents,
            gross_edge_cents=round(fusion_result.gross_edge_cents, 2),
            net_edge_cents=round(fusion_result.net_edge_cents, 2),
            yes_liq=book.total_yes_liquidity(), no_liq=book.total_no_liquidity(),
            minutes_to_expiry=round(contract.minutes_to_expiry, 2),
            regime="poly_primary",
            entry_limit_cents=limit,
            imbalance=round(imb, 3),
            microprice_cents=round(book.microprice_cents, 2),
            poly_up_mid=round(fusion_result.up_mid, 4),
            poly_prob_change_3s=round(fusion_result.prob_change_3s, 4),
            poly_imbalance=round(fusion_result.top_imbalance, 3),
            poly_noise=round(fusion_result.noise_score, 3),
            fusion_fair_cents=round(fusion_result.fused_fair_cents, 2),
        )

        logger.info(
            "POLY SCALP ENTRY: %s %s x1 @ %d¢ | up_mid=%.3f chg3s=%.3f imb=%.2f | "
            "fused=%.1f¢ edge=%.1f¢ spread=%d¢ | target=%d¢ stop=%d¢ | %.1f min left",
            side.upper(), contract.ticker[:20], limit,
            up_mid, prob_change, features.top_imbalance,
            fusion_result.fused_fair_cents, fusion_result.net_edge_cents,
            book.spread_cents, round(target), round(stop),
            contract.minutes_to_expiry,
        )

        try:
            submit_ts_ms = int(time.time() * 1000)
            order = await self._client.place_order(
                ticker=contract.ticker, side=side, count=1,
                price=limit, order_type="limit",
            )
            ack_ts_ms = int(time.time() * 1000)
        except KalshiAPIError as e:
            logger.warning("Poly scalp entry failed: %s", e)
            await self._signal_logger.update_hft_row(
                log_row_id, decision="rejected", reject_reason="order_failed",
            )
            return

        await self._signal_logger.update_hft_row(
            log_row_id,
            order_id=order.order_id,
            submit_ts_ms=submit_ts_ms,
            ack_ts_ms=ack_ts_ms,
        )

        self._pending_entry = HFTPosition(
            order_id=order.order_id,
            ticker=contract.ticker,
            side=side,
            count=1,
            limit_cents=limit,
            entry_cents=float(limit),
            target_cents=target,
            stop_cents=stop,
            posted_at=time.time(),
            expiry_ts=contract.expiry_ts,
            log_row_id=log_row_id,
            submit_ts_ms=submit_ts_ms,
        )

    async def _manage_scalp(self, book: KalshiOrderBook, contract) -> None:
        """Manage pending entry fill or open position take-profit/stop."""

        # ── Phase A: waiting for entry fill ──────────────────────────────
        if self._pending_entry and not self._scalp:
            await self._check_pending_fill(book, contract)
            return

        # ── Phase B: position is open, manage exit ────────────────────────
        if not self._scalp:
            return

        pos = self._scalp
        bid = book.best_yes_bid if pos.side == "yes" else book.best_no_bid
        current_pnl = (bid - pos.entry_cents) * pos.count / 100.0

        # Force exit when approaching expiry
        if contract.minutes_to_expiry < HFT_MIN_MINUTES_REMAINING:
            await self._exit_scalp(contract, bid, "expiry_force")
            return

        # Take profit
        if bid >= pos.target_cents:
            await self._exit_scalp(contract, bid, "take_profit")
            return

        # Stop loss
        if bid <= pos.stop_cents:
            await self._exit_scalp(contract, bid, "stop_loss")
            return

        # Signal reversal: exit if technical model now opposes our position
        signal = self._signal_ref.get("signal")
        if signal and _opposes(signal.direction, pos.side):
            await self._exit_scalp(contract, bid, "signal_reversal")
            return

        logger.debug(
            "HFT scalp holding: %s %s | entry=%.1f¢ bid=%d¢ pnl=%+.4f | target=%d¢ stop=%d¢",
            pos.side.upper(), pos.ticker[:20],
            pos.entry_cents, bid, current_pnl,
            round(pos.target_cents), round(pos.stop_cents),
        )

    async def _check_pending_fill(self, book: KalshiOrderBook, contract) -> None:
        """Poll fill status of the pending entry order. Step price or cancel if stale."""
        pos = self._pending_entry
        elapsed = time.time() - pos.posted_at

        try:
            order = await self._client.get_order(pos.order_id)
        except Exception as e:
            logger.warning("HFT pending fill poll failed: %s", e)
            return

        if order.filled_count > 0:
            fill_price = order.average_price or float(pos.limit_cents)
            pos.entry_cents = fill_price
            pos.filled = True
            self._scalp = pos
            self._pending_entry = None
            if self._window:
                self._window.scalp_count += 1
            fill_ts_ms = int(time.time() * 1000)
            logger.info(
                "HFT scalp FILLED: %s %s x%d @ %.1f¢ | target=%d¢ stop=%d¢ | fill_latency=%dms",
                pos.side.upper(), pos.ticker[:20], order.filled_count,
                fill_price, round(pos.target_cents), round(pos.stop_cents),
                fill_ts_ms - pos.submit_ts_ms,
            )
            await self._signal_logger.update_hft_row(
                pos.log_row_id, fill_cents=fill_price,
            )
            # Capture post-fill drift to distinguish good fills from toxic picks
            asyncio.create_task(
                self._log_post_fill_drift(pos.log_row_id, pos.ticker, pos.side)
            )
            return

        # Not yet filled — check if we should step price or cancel
        if elapsed > HFT_SCALP_ENTRY_TIMEOUT:
            # Cancel stale order
            logger.info(
                "HFT entry timeout (%.0fs) — cancelling %s",
                elapsed, pos.order_id[:8],
            )
            await self._client.cancel_order(pos.order_id)
            self._pending_entry = None
            return

        # Reprice limit: stop stepping after too many cancel/replace cycles
        if pos.reprice_count >= HFT_MAX_REPRICES_PER_ORDER:
            logger.info(
                "HFT entry max reprices reached (%d) — cancelling %s",
                pos.reprice_count, pos.order_id[:8],
            )
            await self._client.cancel_order(pos.order_id)
            self._pending_entry = None
            return

        # Optional: step up by 1¢ if we're more than 15s in and still not filled
        if elapsed > 15 and contract.minutes_to_expiry > HFT_MIN_MINUTES_REMAINING + 1:
            ask = book.best_yes_ask if pos.side == "yes" else book.best_no_ask
            new_limit = min(ask, pos.limit_cents + 1)
            if new_limit > pos.limit_cents:
                logger.debug(
                    "HFT entry step: %d¢ → %d¢ (%.0fs elapsed, reprice #%d)",
                    pos.limit_cents, new_limit, elapsed, pos.reprice_count + 1,
                )
                await self._client.cancel_order(pos.order_id)
                try:
                    new_order = await self._client.place_order(
                        ticker=pos.ticker, side=pos.side, count=pos.count,
                        price=new_limit, order_type="limit",
                    )
                    pos.order_id = new_order.order_id
                    pos.limit_cents = new_limit
                    pos.posted_at = time.time()
                    pos.reprice_count += 1
                except KalshiAPIError as e:
                    logger.warning("HFT price step failed: %s", e)
                    self._pending_entry = None

    async def _log_post_fill_drift(self, log_row_id: int, ticker: str, side: str) -> None:
        """Fetch book state at 1s/3s/10s after fill and write bid prices to hft_log.

        These drift snapshots reveal whether fills were good microstructure captures
        (bid moves up after YES buy) or toxic picks (bid moves down immediately).
        Runs as a background asyncio task — never blocks the main HFT loop.
        """
        field_map = {1: "drift_1s_cents", 3: "drift_3s_cents", 10: "drift_10s_cents"}
        prev_delay = 0
        for delay_s in HFT_POST_FILL_DRIFT_SECONDS:
            await asyncio.sleep(delay_s - prev_delay)
            prev_delay = delay_s
            try:
                book = await self._client.get_orderbook(ticker)
                bid = book.best_yes_bid if side == "yes" else book.best_no_bid
                field = field_map.get(delay_s)
                if field:
                    await self._signal_logger.update_hft_row(log_row_id, **{field: float(bid)})
            except Exception as e:
                logger.debug("Post-fill drift fetch failed at +%ds: %s", delay_s, e)

    async def _exit_scalp(self, contract, exit_bid: int, reason: str) -> None:
        """Exit an open scalp position, stepping price down until filled."""
        pos = self._scalp
        if pos is None:
            return

        logger.info(
            "HFT scalp EXIT [%s]: %s %s | entry=%.1f¢ exit_bid=%d¢ | pnl=%+.4f",
            reason, pos.side.upper(), pos.ticker[:20],
            pos.entry_cents, exit_bid,
            (exit_bid - pos.entry_cents) * pos.count / 100.0,
        )

        # Step price down until filled — never hold a trapped position
        actual_exit = exit_bid
        # On take-profit, open above the current bid to attempt a better fill;
        # the step-down retry loop handles fallback if it doesn't fill.
        if reason == "take_profit":
            price = min(99, exit_bid + HFT_TP_LIMIT_PREMIUM_CENTS)
        else:
            price = max(exit_bid, 1)
        filled = False
        for attempt in range(6):
            try:
                order = await self._client.place_order(
                    ticker=pos.ticker, side=pos.side, count=pos.count,
                    price=max(price, 1), order_type="limit", action="sell",
                )
            except KalshiAPIError as e:
                logger.error("HFT scalp exit attempt %d FAILED: %s", attempt + 1, e)
                break

            if order.filled_count > 0:
                actual_exit = order.average_price or price
                filled = True
                break

            await asyncio.sleep(1.5)
            try:
                check = await self._client.get_order(order.order_id)
                if check.filled_count > 0:
                    actual_exit = check.average_price or price
                    filled = True
                    break
                await self._client.cancel_order(order.order_id)
            except KalshiAPIError:
                pass

            # Step down: re-fetch live bid, subtract escalating offset
            try:
                book = await self._client.get_orderbook(pos.ticker)
                live_bid = book.best_yes_bid if pos.side == "yes" else book.best_no_bid
                price = max(1, live_bid - 3 * (attempt + 1))
            except Exception:
                price = max(1, price - 4)

            logger.info("HFT exit step %d → %d¢", attempt + 2, price)

        if not filled:
            logger.warning("HFT scalp exit exhausted retries — final 1¢ attempt")
            try:
                await self._client.place_order(
                    ticker=pos.ticker, side=pos.side, count=pos.count,
                    price=1, order_type="limit", action="sell",
                )
                actual_exit = 1
                filled = True
            except KalshiAPIError as e:
                logger.error("HFT scalp exit final attempt FAILED: %s", e)
                return

        exit_ts_ms = int(time.time() * 1000)
        pnl = (actual_exit - pos.entry_cents) * pos.count / 100.0
        placed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Update hft_log with exit outcome — primary source of truth for HFT trades
        await self._signal_logger.update_hft_row(
            pos.log_row_id,
            exit_cents=float(actual_exit),
            pnl=round(pnl, 6),
            exit_ts_ms=exit_ts_ms,
        )
        # Also write to kalshi_trades for ledger / outcome-poller visibility
        await self._signal_logger.log_kalshi_trade(
            order_id=pos.order_id,
            placed_at=placed_at,
            ticker=pos.ticker,
            side=pos.side,
            count=pos.count,
            limit_price=pos.limit_cents,
            dollar_risk=pos.count * pos.limit_cents / 100.0,
            strategy_name=f"HFT_SCALP_{reason.upper()}",
            expected_wr=0.0,
        )
        if self._window:
            self._window.realized_pnl += pnl
        self._equity_ref[0] += pnl
        self._scalp = None


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _opposes(signal_direction: str, position_side: str) -> bool:
    """True when the signal direction is opposite to the open position side."""
    return (
        (signal_direction == "CALL" and position_side == "no") or
        (signal_direction == "PUT"  and position_side == "yes")
    )


def _sides_oppose(side_a: str, side_b: str) -> bool:
    """True when two order sides are opposite (yes vs no)."""
    return (side_a == "yes" and side_b == "no") or (side_a == "no" and side_b == "yes")
