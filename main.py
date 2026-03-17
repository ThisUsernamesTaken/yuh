# main.py — Orchestrator: live signal generation + Kalshi execution
#
# Data flow:
#   Binance WS (1m candles)
#     → BiasEngine per TF (1m/3m/5m/15m/1h)
#     → ConsensusLayer (Fourier-weighted)
#     → SignalFilter (regime + alignment + divergence + confidence)
#     → PositionManager (sizing + kill switch)
#     → KalshiClient (order execution)
#
# Primary trigger: 5m BiasEngine signal (best WR + enough time in window)

import asyncio
import ctypes
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from aggregator import CandleAggregator
from bias_engine import BiasEngine
from config import TF_WEIGHTS, ENTRY_THRESHOLD, \
    STRATEGY_INDEX_ENABLED, BLOCK_LOW_SAMPLE_CELLS, LOW_SAMPLE_THRESHOLD, \
    AFTER_HRS_TF_WEIGHTS, MAX_QUOTE_AGE_MS, \
    EARLY_EXIT_TF_THRESHOLD, EARLY_EXIT_MIN_MINUTES, EARLY_EXIT_MIN_SAVINGS_CENTS, \
    TAKE_PROFIT_CENTS, ENABLE_REENTRY, REENTRY_MIN_MINUTES, HFT_ENABLED, \
    STRONG_REVERSAL_TF_THRESHOLD, STRONG_REVERSAL_AGGRESSION_CENTS, \
    POLY_ENABLED, \
    TP_USE_LIMIT_HOLD, TP_LIMIT_PREMIUM_CENTS, TP_HARD_CEILING_CENTS, TP_HOLD_TIMEOUT_MINUTES
from polymarket_stream import PolymarketStream
from hft_engine import HFTEngine
from config_phase3 import (
    REGIME_TIMEFRAME, MIN_TF_ALIGNMENT, MIN_SIGNAL_CONFIDENCE,
    REQUIRE_FAVORABLE_REGIME,
)
from strategy_index import StrategyIndex, get_session
from consensus import ConsensusLayer
from kalshi_client import KalshiClient, KalshiAPIError
from models import Candle, BiasResult, ConsensusSignal
from position_manager import PositionManager
from regime_detector import RegimeDetector
from signal_intelligence import SignalFilter, WinRateTracker, _session_label
from signal_logger import SignalLogger
from ws_client import BinanceWSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "data/engine_history.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


def _ts_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _print_signal(signal: ConsensusSignal, label: str = "SIGNAL") -> None:
    bar = "=" * 60
    print(bar)
    print(f"  {label}  |  {_ts_str(signal.timestamp)}")
    print(f"  Direction : {signal.direction}")
    print(f"  Confidence: {signal.confidence:.2f}  (bucket {signal.bucket})")
    print(f"  Fourier   : {signal.fourier_score:.4f}")
    print(f"  Alignment : {signal.aligned_count}/{signal.total_tfs} TFs")
    for tf, sc in sorted(signal.tf_scores.items()):
        arrow = "UP" if sc > 0 else ("DN" if sc < 0 else "--")
        print(f"    {tf:>4s}  {arrow}  {sc:+.2f}")
    print(bar)


def _set_sleep_prevention(active: bool) -> None:
    """Prevent or allow Windows PC sleep via SetThreadExecutionState.

    When active=True, signals the OS that the system is required to stay awake
    (ES_SYSTEM_REQUIRED | ES_CONTINUOUS). When active=False, releases the lock
    so normal sleep policy resumes.
    """
    try:
        ES_CONTINUOUS      = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if active else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        logger.info("Sleep prevention: %s", "enabled" if active else "released")
    except Exception as e:
        logger.warning("Could not set sleep prevention state: %s", e)


class Engine:
    """Top-level engine — Binance data → signal → Kalshi execution."""

    def __init__(
        self,
        kalshi_key_id: str = "",
        kalshi_private_key_pem: str = "",
        kalshi_demo: bool = True,
        daily_loss_limit: float = 500.0,
        execute_trades: bool = False,
    ) -> None:
        """
        Args:
            kalshi_key_id:        Kalshi API key ID (UUID). Required if execute_trades=True.
            kalshi_private_key_pem: RSA private key PEM. Required if execute_trades=True.
            kalshi_demo:          Use Kalshi demo environment (default True for safety).
            daily_loss_limit:     Hard stop for the day in dollars.
            execute_trades:       If False, signals are logged but no orders are placed.
        """
        self._execute = execute_trades
        self._kalshi_key_id = kalshi_key_id
        self._kalshi_pem = kalshi_private_key_pem
        self._kalshi_demo = kalshi_demo

        # ── Bias engines (one per TF) ──
        self._bias_engines: dict[str, BiasEngine] = {
            tf: BiasEngine(tf) for tf in TF_WEIGHTS
        }
        self._aggregators: dict[str, CandleAggregator] = {
            tf: CandleAggregator(tf) for tf in TF_WEIGHTS if tf != "1m"
        }
        self._latest_results: dict[str, BiasResult] = {}

        # ── Signal pipeline ──
        self._consensus = ConsensusLayer()
        self._regime_detector = RegimeDetector()
        self._win_tracker = WinRateTracker()
        self._signal_filter = SignalFilter(
            regime_detector=self._regime_detector,
            win_rate_tracker=self._win_tracker,
            min_confidence=MIN_SIGNAL_CONFIDENCE,
            min_alignment=MIN_TF_ALIGNMENT,
            require_favorable_regime=REQUIRE_FAVORABLE_REGIME,
        )

        # ── Strategy Index ──
        self._strategy_index = StrategyIndex(
            enabled=STRATEGY_INDEX_ENABLED,
            block_low_sample=BLOCK_LOW_SAMPLE_CELLS,
            low_sample_threshold=LOW_SAMPLE_THRESHOLD,
        )

        # ── Execution ──
        self._position_mgr = PositionManager(daily_loss_limit=daily_loss_limit)
        self._signal_logger = SignalLogger()
        self._ws = BinanceWSClient(callback=self._on_candle)

        # Cache confirmed fill info so we don't poll get_order every candle
        # order_id → {"count": int, "entry_cents": float}
        self._confirmed_fills: dict[str, dict] = {}

        # HFT shared state — written by main engine, read by HFTEngine (asyncio single-threaded)
        self._hft_signal_ref: dict = {"signal": None}
        self._hft_decision_ref: dict = {"decision": None}  # latest FilterDecision with calibrated WR
        self._hft_equity_ref: list = [self._position_mgr.equity]
        self._hft_submit_lock: dict = {"active": False, "ticker": ""}  # True while main is placing

        # Polymarket cross-venue signal ref — written by PolymarketStream, read by HFTEngine
        self._poly_ref: dict = {"state": None} if POLY_ENABLED else None

        # Pending TP limit sell orders: buy_order_id → {tp_order_id, posted_at_ms, limit_cents, count, entry_cents}
        self._tp_sell_orders: dict[str, dict] = {}

    async def run(self) -> None:
        """Initialize and start the live engine."""
        await self._signal_logger.initialize()

        mode = "LIVE" if (self._execute and not self._kalshi_demo) else \
               "DEMO" if (self._execute and self._kalshi_demo) else "SIGNAL-ONLY"
        logger.info("BTC Bias Engine starting — mode=%s", mode)

        if self._execute and (not self._kalshi_key_id or not self._kalshi_pem):
            logger.error(
                "execute_trades=True but KALSHI_API_KEY / KALSHI_PRIVATE_KEY not set. Aborting."
            )
            return

        # Pre-load historical candles so all TF aggregators are warm before
        # the first live WebSocket candle arrives.
        await self._warm_start()

        _set_sleep_prevention(True)
        try:
            if self._execute:
                async with KalshiClient(
                    self._kalshi_key_id, self._kalshi_pem, demo=self._kalshi_demo
                ) as client:
                    self._kalshi = client
                    # Sync real Kalshi balance so equity tracking starts accurate
                    try:
                        bal = await client.get_balance()
                        real_equity = bal.balance / 100.0
                        self._position_mgr._equity = real_equity
                        logger.info("Balance synced from Kalshi: $%.2f", real_equity)
                    except Exception as e:
                        logger.warning(
                            "Balance sync failed, using configured equity $%.2f: %s",
                            self._position_mgr.equity, e,
                        )
                    # Settle any orders that were pending when the engine last stopped
                    await self._scrub_pending_orders()
                    # Sync equity ref so HFT starts with correct balance
                    self._hft_equity_ref[0] = self._position_mgr.equity

                    # Start outcome poller and optional HFT loop alongside the WebSocket feed
                    poller_task = asyncio.create_task(self._outcome_poller())
                    watchdog_task = asyncio.create_task(self._sleep_watchdog())

                    # Start Polymarket stream (optional, degrades gracefully if unreachable)
                    poly_task = None
                    if POLY_ENABLED and self._poly_ref is not None:
                        poly_stream = PolymarketStream(self._poly_ref)
                        poly_task = asyncio.create_task(poly_stream.run())
                        logger.info("Polymarket stream task started")

                    hft_task = None
                    if HFT_ENABLED:
                        hft = HFTEngine(
                            client=client,
                            equity_ref=self._hft_equity_ref,
                            signal_logger=self._signal_logger,
                            signal_ref=self._hft_signal_ref,
                            decision_ref=self._hft_decision_ref,
                            position_mgr=self._position_mgr,
                            submit_lock=self._hft_submit_lock,
                            poly_ref=self._poly_ref,
                        )
                        hft_task = asyncio.create_task(hft.run())
                        logger.info("HFT engine task started")
                    try:
                        await self._ws.start()
                    finally:
                        poller_task.cancel()
                        watchdog_task.cancel()
                        if poly_task:
                            poly_task.cancel()
                        if hft_task:
                            hft_task.cancel()
            else:
                self._kalshi = None
                watchdog_task = asyncio.create_task(self._sleep_watchdog())
                try:
                    await self._ws.start()
                finally:
                    watchdog_task.cancel()
        finally:
            _set_sleep_prevention(False)

    # ── Sleep watchdog ────────────────────────────────────────────────────

    async def _sleep_watchdog(self) -> None:
        """Detect PC sleep/wake events and clear stale indicator state on wake.

        Compares wall-clock time (time.time) with monotonic time (time.monotonic)
        every 5 seconds. A large gap means the monotonic clock paused (PC slept)
        while wall-clock advanced. On detection, stale HFT signal refs are cleared
        and _warm_start() is called to re-populate indicator state from live data.
        """
        CHECK_INTERVAL = 5.0
        SLEEP_THRESHOLD = 20.0  # seconds of gap before we treat it as a sleep event
        last_mono = time.monotonic()
        last_wall = time.time()
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            now_mono = time.monotonic()
            now_wall = time.time()
            gap = (now_wall - last_wall) - (now_mono - last_mono)
            last_mono = now_mono
            last_wall = now_wall
            if gap < SLEEP_THRESHOLD:
                continue
            logger.warning(
                "Wake from sleep detected (%.0fs gap) — clearing stale state and re-warming",
                gap,
            )
            self._hft_signal_ref["signal"] = None
            self._hft_decision_ref["decision"] = None
            await self._warm_start()
            logger.info("Post-wake re-warm complete")

    # ── Warm start ────────────────────────────────────────────────────────

    async def _warm_start(self) -> None:
        """Fetch the last 100 closed 1m candles from Binance.US REST and feed
        them through the indicator pipeline (no trade execution).

        This pre-populates all five TF aggregators (1m/3m/5m/15m/1h) and the
        RegimeDetector so the first live WebSocket candle immediately produces
        a valid consensus signal, eliminating the ~60-minute warmup delay.
        """
        url = "https://api.binance.us/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 100}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning("Warm start fetch failed: HTTP %d", resp.status)
                        return
                    rows = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("Warm start fetch error: %s", e)
            return

        candles = []
        for row in rows:
            # Skip the last row — it's the currently open (not closed) candle
            candles.append(Candle(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                closed=True,
            ))

        # Drop the last entry — it's the in-progress candle
        candles = candles[:-1]

        for candle in candles:
            await self._on_candle(candle, execute=False)

        logger.info(
            "Warm start complete: fed %d historical candles. "
            "TFs ready: %s",
            len(candles),
            list(self._latest_results.keys()),
        )

    # ── Dedup recovery ────────────────────────────────────────────────────

    async def _restore_last_signal_ts(self) -> None:
        """Set _last_signal_ts from the most recent pending order so a restart
        within the same 15-minute window doesn't place a duplicate order."""
        import aiosqlite
        from config import TRADES_DB_PATH
        async with aiosqlite.connect(TRADES_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute_fetchall(
                "SELECT placed_at FROM kalshi_trades WHERE status='pending' ORDER BY created_at DESC LIMIT 1"
            )
            if not row:
                return
            placed_at_str = row[0]["placed_at"]  # "2026-03-13 23:16:00"
            placed_at_dt = datetime.strptime(placed_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            placed_at_ms = int(placed_at_dt.timestamp() * 1000)
            cycle_5m_ms = 15 * 60 * 1000
            self._last_signal_ts = (placed_at_ms // cycle_5m_ms) * cycle_5m_ms
            logger.info(
                "Restored last_signal_ts from pending order at %s — dedup active for current window",
                placed_at_str,
            )

    async def _scrub_pending_orders(self) -> None:
        """On startup, resolve any orders still marked 'pending' in the DB.

        When the engine restarts, previously placed orders are no longer in
        _open_orders so the outcome poller can never settle them. This method
        queries Kalshi directly for each pending order and writes the result
        to kalshi_trades — keeping the ledger accurate without needing the
        full in-memory TradeIntent.

        Equity is already correct (synced via get_balance()), so only the DB
        needs updating here.
        """
        if not self._kalshi:
            return
        import aiosqlite
        from config import TRADES_DB_PATH
        async with aiosqlite.connect(TRADES_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            pending = await db.execute_fetchall(
                "SELECT order_id, ticker, side, limit_price FROM kalshi_trades WHERE status='pending'"
            )

        if not pending:
            return

        logger.info("Scrubbing %d pending order(s) from previous session(s)...", len(pending))
        for row in pending:
            order_id = row["order_id"]
            ticker   = row["ticker"]
            side     = row["side"]
            price    = row["limit_price"]
            try:
                contract = await self._kalshi.get_contract(ticker)
                order    = await self._kalshi.get_order(order_id)
                filled   = order.filled_count

                if contract.result is None:
                    # Contract not yet settled — leave as pending
                    continue

                if filled == 0:
                    await self._signal_logger.log_kalshi_outcome(
                        order_id=order_id, is_win=False, pnl=0.0,
                        filled_count=0, result=contract.result,
                    )
                    logger.info("Scrub: %s unfilled (result=%s)", order_id[:8], contract.result)
                else:
                    fill_price = order.average_price or float(price)
                    is_win = contract.result == side
                    pnl = filled * (1.0 - fill_price / 100.0) if is_win else -filled * (fill_price / 100.0)
                    await self._signal_logger.log_kalshi_outcome(
                        order_id=order_id, is_win=is_win, pnl=pnl,
                        filled_count=filled, result=contract.result,
                    )
                    logger.info(
                        "Scrub: %s %s filled=%d pnl=%+.2f (result=%s)",
                        order_id[:8], "WIN" if is_win else "LOSS", filled, pnl, contract.result
                    )
            except Exception as e:
                logger.warning("Scrub failed for %s: %s", order_id[:8], e)

    # ── Candle handler ────────────────────────────────────────────────────

    async def _on_candle(self, candle: Candle, execute: bool = True) -> None:
        """Called for every closed 1m candle from Binance."""
        # 1m engine
        result_1m = self._bias_engines["1m"].update(candle)
        self._latest_results["1m"] = result_1m

        # Feed regime detector on 5m candles
        completed_5m = self._aggregators["5m"].update(candle)

        # Aggregate all other TFs
        for tf, agg in self._aggregators.items():
            if tf == "5m":
                completed = completed_5m
            else:
                completed = agg.update(candle)

            if completed is not None:
                result = self._bias_engines[tf].update(completed)
                self._latest_results[tf] = result

                # Feed regime detector whenever a 5m candle completes
                if tf == REGIME_TIMEFRAME:
                    self._regime_detector.update(completed)

        # Need results from all TFs before computing consensus
        if len(self._latest_results) < len(TF_WEIGHTS):
            return

        # Resolve session and TF weights before computing consensus.
        # After-hours (UTC 21–08): 1h weight is reduced to prevent a slow-updating
        # 1h indicator from swamping real short-term momentum signals.
        utc_hour = datetime.fromtimestamp(candle.timestamp / 1000, tz=timezone.utc).hour
        tf_weights = AFTER_HRS_TF_WEIGHTS if (utc_hour >= 21 or utc_hour < 8) else None

        # Compute consensus
        signal = self._consensus.compute(self._latest_results, candle.timestamp, weights=tf_weights)

        # Keep HFT engine's signal reference current (every tick, even during warm start)
        self._hft_signal_ref["signal"] = signal

        # During warm start, only update indicators — no logging or execution
        if not execute:
            return

        # Log every signal regardless of approval
        await self._signal_logger.log_signal(signal)

        # Check early exits before evaluating new entries
        if self._execute and self._kalshi and self._position_mgr.open_orders:
            await self._check_early_exits(signal, utc_hour)

        # Select strategy for this regime × session cell
        regime_state = self._regime_detector.state
        strategy = self._strategy_index.select(regime_state.regime, utc_hour)

        decision = self._signal_filter.evaluate(signal, utc_hour=utc_hour, strategy=strategy)

        # Always update HFT decision ref — gives HFT current regime + WR even when main rejects
        self._hft_decision_ref["decision"] = decision

        if not decision.approved:
            return

        # Gate: don't enter while a position is already open.
        # The early-exit system handles in-window cycling — once a position
        # is closed (early exit or expiry), the next approved signal can enter.
        if self._execute and self._position_mgr.open_orders:
            return

        _print_signal(signal, label="APPROVED SIGNAL")
        logger.info(
            "Filter: %s | regime=%s | align=%d/%d | adj_conf=%.1f",
            decision.reason.value,
            decision.regime.regime.value,
            signal.aligned_count, signal.total_tfs,
            decision.adjusted_confidence,
        )
        logger.info(
            "Strategy: %s | WR=%.1f%% | Scale=%.1fx",
            decision.strategy_name, decision.expected_wr, decision.position_scale,
        )

        # Execute if enabled
        if self._execute and self._kalshi and not self._position_mgr.is_killed:
            session = _session_label(utc_hour)
            decision_ts_ms = int(time.time() * 1000)
            await self._execute_signal(signal, decision, decision_ts_ms, utc_hour, session)

    # ── Execution ─────────────────────────────────────────────────────────

    async def _execute_signal(
        self,
        signal: ConsensusSignal,
        decision,
        decision_ts_ms: int,
        utc_hour: int,
        session: str,
    ) -> None:
        """Translate an approved signal into a Kalshi order.

        Logs every approved signal to execution_log regardless of whether an
        order is placed, with gate-level flags so the diagnostic report can show
        exactly where the pipeline is breaking.
        """
        regime_label = self._regime_detector.state.regime.value

        # Base fields written to execution_log regardless of outcome
        base_fields = dict(
            signal_ts=signal.timestamp,
            session=session,
            regime=regime_label,
            utc_hour=utc_hour,
            strategy_name=decision.strategy_name,
            raw_confidence=signal.confidence,
            adjusted_confidence=decision.adjusted_confidence,
            expected_wr=decision.expected_wr,
            position_scale=decision.position_scale,
            decision_ts_ms=decision_ts_ms,
            signal_approved=1,
        )

        try:
            contracts = await self._kalshi.find_btc_contracts(
                window_minutes=15,
                min_minutes_remaining=2.0,
            )
            quote_ts_ms = int(time.time() * 1000)

            if not contracts:
                logger.warning("No tradeable BTC contracts found")
                await self._signal_logger.log_execution(
                    **base_fields,
                    quote_ts_ms=quote_ts_ms,
                    quote_age_ms=quote_ts_ms - decision_ts_ms,
                    pricing_eligible=0,
                    liquidity_eligible=0,
                    execution_eligible=0,
                )
                return

            # KXBTC15M: one contract per window, direction encoded via YES/NO side.
            contract = contracts[0]
            side = "yes" if signal.direction == "CALL" else "no"
            bid = contract.yes_bid if side == "yes" else contract.no_bid
            ask = contract.yes_ask if side == "yes" else contract.no_ask
            spread_cents = (ask - bid) * 100.0

            logger.info(
                "Contract selected: %s (%.1f min remaining, yes_ask=%.0f¢, spread=%.0f¢, vol=%d)",
                contract.ticker, contract.minutes_to_expiry, contract.yes_ask * 100,
                spread_cents, contract.volume,
            )

            contract_fields = dict(
                contract_ticker=contract.ticker,
                side=side,
                minutes_to_expiry=contract.minutes_to_expiry,
                best_bid=round(bid * 100),
                best_ask=round(ask * 100),
                spread_cents=spread_cents,
                display_volume=contract.volume,
                quote_ts_ms=quote_ts_ms,
            )

            # Size the trade — rejection reason tracked on position_mgr
            intent = self._position_mgr.size_trade(signal, decision, contract)
            rej = self._position_mgr.last_rejection

            pricing_eligible   = 0 if rej in ("TIME_EXPIRED", "LOW_EDGE", "KILL_SWITCH") else 1
            liquidity_eligible = 0 if rej in ("SPREAD_TOO_WIDE", "LOW_VOLUME") else 1

            # Quote-age gate: if the book snapshot is stale, mark execution ineligible
            quote_age_ms = int(time.time() * 1000) - quote_ts_ms
            if intent is not None and quote_age_ms > MAX_QUOTE_AGE_MS:
                logger.info(
                    "Trade rejected: quote age %dms > max %dms on %s",
                    quote_age_ms, MAX_QUOTE_AGE_MS, contract.ticker,
                )
                intent = None
                rej = "QUOTE_STALE"
                pricing_eligible = 0

            execution_eligible = 1 if intent is not None else 0

            if intent is None:
                await self._signal_logger.log_execution(
                    **base_fields, **contract_fields,
                    quote_age_ms=quote_age_ms,
                    pricing_eligible=pricing_eligible,
                    liquidity_eligible=liquidity_eligible,
                    execution_eligible=0,
                )
                logger.info("Execution gate blocked: %s", rej or "unknown")
                return

            logger.info(
                "Trade intent: %s x%d @ %d¢  risk=$%.2f  edge=%.1f%%",
                intent.side.upper(), intent.count, intent.limit_price,
                intent.dollar_risk, intent.edge * 100,
            )

            # Set submit lock so HFT engine pauses entry on this ticker during our place_order
            self._hft_submit_lock["active"] = True
            self._hft_submit_lock["ticker"] = intent.contract.ticker
            submit_ts_ms = int(time.time() * 1000)
            try:
                trade = await self._position_mgr.execute(intent, self._kalshi)
            finally:
                self._hft_submit_lock["active"] = False
            ack_ts_ms = int(time.time() * 1000)

            if not trade:
                exec_rej = self._position_mgr.last_rejection or "EXECUTE_FAILED"
                logger.warning("Trade execute failed: %s", exec_rej)
                await self._signal_logger.log_execution(
                    **base_fields, **contract_fields,
                    quote_age_ms=quote_age_ms,
                    pricing_eligible=pricing_eligible,
                    liquidity_eligible=liquidity_eligible,
                    execution_eligible=1,
                    submitted=0,
                )
                return

            order_id = trade.order.order_id
            logger.info("Order confirmed: %s  ack_latency=%dms", order_id, ack_ts_ms - submit_ts_ms)

            # Write execution_log row
            exec_id = await self._signal_logger.log_execution(
                **base_fields, **contract_fields,
                quote_age_ms=quote_age_ms,
                ack_ts_ms=ack_ts_ms,
                pricing_eligible=pricing_eligible,
                liquidity_eligible=liquidity_eligible,
                execution_eligible=1,
                submitted=1,
                submit_price=intent.limit_price,
                submit_ts_ms=submit_ts_ms,
                order_id=order_id,
            )

            # Log to kalshi_trades for ledger display
            placed_at = datetime.fromtimestamp(
                trade.placed_at_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            await self._signal_logger.log_kalshi_trade(
                order_id=order_id,
                placed_at=placed_at,
                ticker=intent.contract.ticker,
                side=intent.side,
                count=intent.count,
                limit_price=intent.limit_price,
                dollar_risk=intent.dollar_risk,
                strategy_name=decision.strategy_name,
                expected_wr=decision.expected_wr,
            )

            # Schedule delayed quote snapshots (3s and 10s after ack)
            asyncio.create_task(
                self._capture_delayed_quotes(exec_id, contract.ticker)
            )

        except Exception as e:
            logger.error("Execution error: %s", e, exc_info=True)

    # ── Force-sell helper ─────────────────────────────────────────────────

    async def _force_sell(
        self,
        ticker: str,
        side: str,
        count: int,
        initial_price: int,
        label: str,
        step_cents: int = 4,
        wait_secs: float = 2.0,
        max_steps: int = 5,
    ) -> tuple[bool, int]:
        """Place a limit sell and step the price down until filled.

        Starts at initial_price (usually current bid), waits wait_secs, then
        cancels and retries at price - step_cents each round. Final attempt is
        always at 1¢ to guarantee a fill rather than being trapped in the position.

        Returns (filled: bool, fill_price: int).
        """
        price = initial_price
        for attempt in range(max_steps):
            price = max(1, price)
            try:
                order = await self._kalshi.place_order(
                    ticker=ticker, side=side, count=count,
                    price=price, order_type="limit", action="sell",
                )
            except KalshiAPIError as e:
                logger.warning("Force-sell attempt %d failed to place [%s]: %s", attempt + 1, label, e)
                break

            if order.filled_count > 0:
                fill = order.average_price or price
                logger.info("Force-sell filled on attempt %d @ %d¢ [%s]", attempt + 1, fill, label)
                return True, fill

            await asyncio.sleep(wait_secs)
            check = await self._kalshi.get_order(order.order_id)
            if check.filled_count > 0:
                fill = check.average_price or price
                logger.info("Force-sell filled (post-wait) attempt %d @ %d¢ [%s]", attempt + 1, fill, label)
                return True, fill

            await self._kalshi.cancel_order(order.order_id)

            # Re-fetch fresh bid — market moves while we wait
            try:
                fresh = await self._kalshi.get_contract(ticker)
                fresh_bid = max(1, round(
                    (fresh.yes_bid if side == "yes" else fresh.no_bid) * 100
                ))
            except Exception:
                fresh_bid = price

            next_price = max(1, fresh_bid - step_cents * (attempt + 1))
            logger.info(
                "Force-sell attempt %d unfilled at %d¢ — stepping to %d¢ (fresh_bid=%d¢) [%s]",
                attempt + 1, price, next_price, fresh_bid, label,
            )
            price = next_price

        logger.warning("Force-sell exhausted %d attempts — final attempt at 1¢ [%s]", max_steps, label)
        try:
            order = await self._kalshi.place_order(
                ticker=ticker, side=side, count=count,
                price=1, order_type="limit", action="sell",
            )
            fill = order.average_price or 1
            return order.filled_count > 0, fill
        except KalshiAPIError as e:
            logger.error("Force-sell final 1¢ attempt failed [%s]: %s", label, e)
            return False, 0

    # ── Early exit / arbitration ──────────────────────────────────────────

    async def _check_early_exits(
        self,
        signal: ConsensusSignal,
        utc_hour: int,
    ) -> None:
        """Monitor open positions each candle for early exit opportunities.

        Two exit triggers:
        1. Reversal: EARLY_EXIT_TF_THRESHOLD+ TFs have flipped against our position.
           Only fires if the savings exceed EARLY_EXIT_MIN_SAVINGS_CENTS (avoids
           exiting for trivial 1-2¢ moves that don't justify the round-trip).
           After a reversal exit, attempts a re-entry on the opposite side.

        2. Take-profit: contract has moved TAKE_PROFIT_CENTS+ in our favor.
           Locks in the gain regardless of signal direction. No re-entry.

        Fill info is cached after the first confirmed fill so get_order is called
        at most once per position. Stale reentry-window entries are pruned each run.
        """
        now_ms = int(time.time() * 1000)

        for order_id, trade in list(self._position_mgr.open_orders.items()):
            minutes_remaining = (trade.intent.contract.expiry_ts - now_ms) / 60_000
            if minutes_remaining < EARLY_EXIT_MIN_MINUTES:
                continue  # Too close to expiry — let it settle naturally

            our_side = trade.intent.side  # "yes" or "no"
            our_direction = "CALL" if our_side == "yes" else "PUT"

            reversal = (
                signal.direction != our_direction
                and signal.aligned_count >= EARLY_EXIT_TF_THRESHOLD
            )
            strong_reversal = (
                signal.direction != our_direction
                and signal.aligned_count >= STRONG_REVERSAL_TF_THRESHOLD
            )

            # ── Service any pending TP limit sell for this position ────────
            if order_id in self._tp_sell_orders:
                await self._service_tp_hold(
                    order_id, trade, signal, reversal, strong_reversal,
                    minutes_remaining, utc_hour, now_ms,
                )
                continue  # Either closed or still holding — skip normal eval

            # Skip API calls entirely when nothing to act on
            if not reversal and not TAKE_PROFIT_CENTS:
                continue

            try:
                # ── Confirm fill (cached after first successful check) ────────
                if order_id not in self._confirmed_fills:
                    order = await self._kalshi.get_order(order_id)
                    if order.filled_count == 0:
                        continue  # Still resting — nothing to sell yet
                    self._confirmed_fills[order_id] = {
                        "count": order.filled_count,
                        "entry_cents": order.average_price or float(trade.intent.limit_price),
                    }

                fill = self._confirmed_fills[order_id]
                entry_cents = fill["entry_cents"]
                count = fill["count"]

                # ── Fetch fresh contract price ───────────────────────────────
                contract = await self._kalshi.get_contract(trade.intent.contract.ticker)
                sell_price = max(1, round(
                    (contract.yes_bid if our_side == "yes" else contract.no_bid) * 100
                ))
                profit_cents = sell_price - entry_cents  # positive = we're winning

                # ── Determine exit condition ─────────────────────────────────
                if reversal:
                    savings_cents = entry_cents - sell_price  # money saved vs full loss
                    if savings_cents < EARLY_EXIT_MIN_SAVINGS_CENTS:
                        logger.info(
                            "Exit skipped [%s]: reversal but savings only %.0fc < %.0fc min",
                            order_id[:8], savings_cents, EARLY_EXIT_MIN_SAVINGS_CENTS,
                        )
                        continue
                    exit_reason = "reversal"
                    do_reentry = True
                elif TAKE_PROFIT_CENTS and profit_cents >= TAKE_PROFIT_CENTS:
                    exit_reason = "take_profit"
                    do_reentry = False
                else:
                    continue  # Neither condition met — hold

                # ── Execute sell order ───────────────────────────────────────
                # All exits use _force_sell — never hold to expiry when we want out.
                # strong_reversal starts more aggressively (bid - aggression¢, faster steps).
                # reversal starts at bid and steps down every 2s.
                # take_profit starts at ask (best price) and steps down every 3s.
                actual_sell_cents = sell_price  # default

                if strong_reversal:
                    start_price = max(1, sell_price - STRONG_REVERSAL_AGGRESSION_CENTS)
                    logger.info(
                        "STRONG REVERSAL exit [%d/5 TFs]: force-sell from %d¢ (bid=%d¢) [%s]",
                        signal.aligned_count, start_price, sell_price, order_id[:8],
                    )
                    filled, fill_price = await self._force_sell(
                        trade.intent.contract.ticker, our_side, count,
                        start_price, label=order_id[:8], step_cents=5, wait_secs=1.5,
                    )
                    if filled:
                        actual_sell_cents = fill_price

                elif exit_reason == "reversal":
                    filled, fill_price = await self._force_sell(
                        trade.intent.contract.ticker, our_side, count,
                        sell_price, label=order_id[:8], step_cents=4, wait_secs=2.0,
                    )
                    if not filled:
                        logger.warning("Force-sell gave up — holding to expiry [%s]", order_id[:8])
                        continue
                    actual_sell_cents = fill_price

                else:
                    # Take-profit
                    ask_cents = max(1, round(
                        (contract.yes_ask if our_side == "yes" else contract.no_ask) * 100
                    ))
                    if TP_USE_LIMIT_HOLD:
                        limit_cents = min(99, ask_cents + int(TP_LIMIT_PREMIUM_CENTS))
                        try:
                            tp_order = await self._kalshi.place_order(
                                ticker=trade.intent.contract.ticker,
                                side=our_side, count=count,
                                price=limit_cents, order_type="limit", action="sell",
                            )
                            self._tp_sell_orders[order_id] = {
                                "tp_order_id": tp_order.order_id,
                                "posted_at_ms": now_ms,
                                "limit_cents": limit_cents,
                                "count": count,
                                "entry_cents": entry_cents,
                            }
                            logger.info(
                                "TP hold: limit sell @ %d¢ (ask=%d¢ +%.0f¢ premium) | "
                                "profit=%.0f¢ | %.1f min left [%s]",
                                limit_cents, ask_cents, TP_LIMIT_PREMIUM_CENTS,
                                profit_cents, minutes_remaining, order_id[:8],
                            )
                            continue  # Skip immediate close — service next candle
                        except KalshiAPIError as e:
                            logger.warning(
                                "TP hold order failed: %s — falling back to force-sell", e
                            )
                    # TP_USE_LIMIT_HOLD=False or limit order failed: immediate force-sell at ask
                    filled, fill_price = await self._force_sell(
                        trade.intent.contract.ticker, our_side, count,
                        ask_cents, label=order_id[:8], step_cents=3, wait_secs=3.0,
                    )
                    if filled:
                        actual_sell_cents = fill_price

                pnl = count * (actual_sell_cents - entry_cents) / 100.0
                is_gain = pnl > 0
                status = "exited_win" if is_gain else "exited_loss"

                logger.info(
                    "Early exit [%s]: %s | %s | entry=%.0fc sell=%.0fc | pnl=%+.2f | "
                    "%.1f min left | signal=%s %d/%d TFs",
                    exit_reason,
                    trade.intent.contract.ticker, our_side.upper(),
                    entry_cents, actual_sell_cents, pnl,
                    minutes_remaining,
                    signal.direction, signal.aligned_count, signal.total_tfs,
                )

                # ── Record outcome ───────────────────────────────────────────
                self._confirmed_fills.pop(order_id, None)
                self._position_mgr.record_outcome(order_id, is_gain, pnl)
                await self._signal_logger.log_kalshi_outcome(
                    order_id=order_id,
                    is_win=is_gain,
                    pnl=pnl,
                    filled_count=count,
                    result=f"early_exit_{exit_reason}",
                    status_override=status,
                )

                # ── Re-entry on reversal ─────────────────────────────────────
                if (do_reentry
                        and ENABLE_REENTRY
                        and minutes_remaining >= REENTRY_MIN_MINUTES
                        and not self._position_mgr.is_killed):
                    await self._execute_reentry(signal, contract, utc_hour)

            except Exception as e:
                logger.warning("Early exit check failed for %s: %s", order_id[:8], e)

    async def _service_tp_hold(
        self,
        order_id: str,
        trade,
        signal: ConsensusSignal,
        reversal: bool,
        strong_reversal: bool,
        minutes_remaining: float,
        utc_hour: int,
        now_ms: int,
    ) -> None:
        """Poll a pending TP limit sell order and act on fill, ceiling, reversal, or timeout.

        Called each candle while a position is in the TP hold state. Handles all
        exit paths: limit fill (best), hard ceiling (lock in), reversal (cut), timeout (give up).
        Always removes the TP state when it exits the hold — no stale state left behind.
        """
        tp = self._tp_sell_orders[order_id]
        our_side = trade.intent.side
        entry_cents = tp["entry_cents"]
        count = tp["count"]
        ticker = trade.intent.contract.ticker

        # ── 1. Check if the limit sell has already filled ─────────────────
        try:
            tp_order = await self._kalshi.get_order(tp["tp_order_id"])
        except Exception as e:
            logger.warning("TP hold poll failed [%s]: %s", order_id[:8], e)
            return

        if tp_order.filled_count > 0:
            fill_price = int(tp_order.average_price or tp["limit_cents"])
            pnl = count * (fill_price - entry_cents) / 100.0
            logger.info(
                "TP hold FILLED @ %d¢ | entry=%.0f¢ pnl=%+.2f [%s]",
                fill_price, entry_cents, pnl, order_id[:8],
            )
            del self._tp_sell_orders[order_id]
            self._confirmed_fills.pop(order_id, None)
            self._position_mgr.record_outcome(order_id, True, pnl)
            await self._signal_logger.log_kalshi_outcome(
                order_id=order_id, is_win=True, pnl=pnl,
                filled_count=count, result="early_exit_take_profit",
                status_override="exited_win",
            )
            return

        # ── 2. Fetch fresh price for ceiling / reversal / timeout checks ──
        try:
            contract = await self._kalshi.get_contract(ticker)
            bid = max(1, round(
                (contract.yes_bid if our_side == "yes" else contract.no_bid) * 100
            ))
            ask = max(1, round(
                (contract.yes_ask if our_side == "yes" else contract.no_ask) * 100
            ))
            profit_cents = bid - entry_cents
        except Exception as e:
            logger.warning("TP hold contract fetch failed [%s]: %s", order_id[:8], e)
            return

        # ── 3. Determine if a forced exit is needed ───────────────────────
        elapsed_ms = now_ms - tp["posted_at_ms"]
        force_reason = None

        if profit_cents >= TP_HARD_CEILING_CENTS:
            force_reason = "tp_ceiling"
            logger.info(
                "TP ceiling: profit=%.0f¢ >= %.0f¢ — force-selling [%s]",
                profit_cents, TP_HARD_CEILING_CENTS, order_id[:8],
            )
        elif reversal or strong_reversal:
            force_reason = "reversal"
            logger.info(
                "TP hold reversal [%d/%d TFs] — cancelling hold and force-selling [%s]",
                signal.aligned_count, signal.total_tfs, order_id[:8],
            )
        elif elapsed_ms > TP_HOLD_TIMEOUT_MINUTES * 60_000:
            force_reason = "tp_timeout"
            logger.info(
                "TP hold timeout (%.0fm) — force-selling at ask=%d¢ [%s]",
                TP_HOLD_TIMEOUT_MINUTES, ask, order_id[:8],
            )

        if force_reason is None:
            # Still holding cleanly — log and return
            logger.debug(
                "TP hold: limit @ %d¢ | bid=%d¢ profit=%.0f¢ | elapsed=%.0fs [%s]",
                tp["limit_cents"], bid, profit_cents, elapsed_ms / 1000, order_id[:8],
            )
            return

        # ── 4. Cancel the pending TP limit sell ───────────────────────────
        try:
            await self._kalshi.cancel_order(tp["tp_order_id"])
        except Exception as e:
            logger.warning("TP hold cancel failed [%s]: %s", order_id[:8], e)
        del self._tp_sell_orders[order_id]

        # ── 5. Force-sell at current prices ───────────────────────────────
        if strong_reversal and force_reason == "reversal":
            start_price = max(1, bid - STRONG_REVERSAL_AGGRESSION_CENTS)
            step_c, wait_s = 5, 1.5
        else:
            start_price = ask  # ceiling / timeout: try ask first for best fill
            step_c, wait_s = 3, 2.5

        filled, fill_price = await self._force_sell(
            ticker, our_side, count,
            start_price, label=order_id[:8], step_cents=step_c, wait_secs=wait_s,
        )
        actual_exit = fill_price if filled else bid
        pnl = count * (actual_exit - entry_cents) / 100.0
        is_gain = pnl > 0
        status = "exited_win" if is_gain else "exited_loss"

        logger.info(
            "TP hold force-exit [%s]: entry=%.0f¢ sell=%.0f¢ pnl=%+.2f [%s]",
            force_reason, entry_cents, actual_exit, pnl, order_id[:8],
        )

        self._confirmed_fills.pop(order_id, None)
        self._position_mgr.record_outcome(order_id, is_gain, pnl)
        await self._signal_logger.log_kalshi_outcome(
            order_id=order_id, is_win=is_gain, pnl=pnl,
            filled_count=count, result=f"early_exit_{force_reason}",
            status_override=status,
        )

        # Re-entry after reversal (same logic as normal reversal exit)
        if (force_reason == "reversal"
                and ENABLE_REENTRY
                and minutes_remaining >= REENTRY_MIN_MINUTES
                and not self._position_mgr.is_killed):
            await self._execute_reentry(signal, contract, utc_hour)

    async def _execute_reentry(
        self,
        signal: ConsensusSignal,
        contract,
        utc_hour: int,
    ) -> None:
        """Open a new position in the reversal direction on the same contract.

        Uses the same filter/sizing pipeline as a normal entry, but bypasses
        the 15-minute dedup so the re-entry trade is independent of the initial
        position that was just exited.
        """
        session = _session_label(utc_hour)
        regime_state = self._regime_detector.state
        strategy = self._strategy_index.select(regime_state.regime, utc_hour)
        decision = self._signal_filter.evaluate(signal, utc_hour=utc_hour, strategy=strategy)

        if not decision.approved:
            logger.info("Re-entry skipped: signal not approved (%s)", decision.reason.value)
            return

        intent = self._position_mgr.size_trade(signal, decision, contract)
        if intent is None:
            logger.info("Re-entry skipped: %s", self._position_mgr.last_rejection)
            return

        logger.info(
            "Re-entry: %s x%d @ %dc  edge=%.1f%%  %.1f min remaining",
            intent.side.upper(), intent.count, intent.limit_price,
            intent.edge * 100, contract.minutes_to_expiry,
        )

        self._hft_submit_lock["active"] = True
        self._hft_submit_lock["ticker"] = intent.contract.ticker
        try:
            trade = await self._position_mgr.execute(intent, self._kalshi)
        finally:
            self._hft_submit_lock["active"] = False

        if not trade:
            return

        placed_at = datetime.fromtimestamp(
            trade.placed_at_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        await self._signal_logger.log_kalshi_trade(
            order_id=trade.order.order_id,
            placed_at=placed_at,
            ticker=intent.contract.ticker,
            side=intent.side,
            count=intent.count,
            limit_price=intent.limit_price,
            dollar_risk=intent.dollar_risk,
            strategy_name=decision.strategy_name + "_REENTRY",
            expected_wr=decision.expected_wr,
        )
        logger.info("Re-entry confirmed: %s", trade.order.order_id)

    async def _capture_delayed_quotes(self, exec_id: int, ticker: str) -> None:
        """Fetch the book 3s and 10s after order placement to measure post-entry drift."""
        try:
            await asyncio.sleep(3)
            c3 = await self._kalshi.get_contract(ticker)
            await self._signal_logger.update_execution(
                exec_id,
                quote_3s_bid=round(c3.yes_bid * 100),
                quote_3s_ask=round(c3.yes_ask * 100),
            )
            await asyncio.sleep(7)  # 3 + 7 = 10s total
            c10 = await self._kalshi.get_contract(ticker)
            await self._signal_logger.update_execution(
                exec_id,
                quote_10s_bid=round(c10.yes_bid * 100),
                quote_10s_ask=round(c10.yes_ask * 100),
            )
        except Exception as e:
            logger.warning("Delayed quote capture failed (exec_id=%d): %s", exec_id, e)

    # ── Outcome tracking ──────────────────────────────────────────────────

    async def _outcome_poller(self) -> None:
        """Background coroutine: check for settled contracts every 60 seconds.

        For each open order whose contract expiry has passed (+ 90s grace for
        settlement), fetches the contract result and records the outcome in:
          - PositionManager (updates equity, daily P&L, kill switch)
          - SignalFilter.win_rate_tracker (updates rolling WR per bucket)
          - SignalLogger.kalshi_trades DB (sets status=won/lost, pnl, filled_count)

        Also cancels any resting orders on contracts with < 2 min remaining to
        prevent stale fills near expiry.
        """
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_and_cancel_stale_orders()
                await self._settle_expired_orders()
            except Exception as e:
                logger.error("Outcome poller error: %s", e, exc_info=True)

    async def _check_and_cancel_stale_orders(self) -> None:
        """Cancel any resting orders on contracts with < 2 min remaining."""
        if not self._kalshi:
            return
        now_ms = int(time.time() * 1000)
        stale_threshold_ms = 2 * 60 * 1000  # 2 minutes

        for order_id, trade in list(self._position_mgr.open_orders.items()):
            time_left = trade.intent.contract.expiry_ts - now_ms
            if time_left > stale_threshold_ms:
                continue
            # Fetch current order status before deciding to cancel
            try:
                order = await self._kalshi.get_order(order_id)
                if order.status == "resting":
                    cancelled = await self._kalshi.cancel_order(order_id)
                    if cancelled:
                        logger.info(
                            "Cancelled stale resting order %s on %s (%.1f min left)",
                            order_id, trade.intent.contract.ticker, time_left / 60_000
                        )
                        # Record as unfilled — no equity impact
                        self._confirmed_fills.pop(order_id, None)
                        self._tp_sell_orders.pop(order_id, None)
                        self._position_mgr.record_outcome(order_id, False, 0.0)
                        await self._signal_logger.log_kalshi_outcome(
                            order_id=order_id,
                            is_win=False,
                            pnl=0.0,
                            filled_count=0,
                            result="cancelled",
                        )
                        await self._signal_logger.update_execution_by_order_id(
                            order_id, filled=0, expiry_outcome="cancelled", pnl=0.0,
                        )
            except Exception as e:
                logger.warning("Could not cancel order %s: %s", order_id, e)

    async def _settle_expired_orders(self) -> None:
        """For contracts that have expired and settled, record outcomes."""
        if not self._kalshi:
            return
        now_ms = int(time.time() * 1000)
        grace_ms = 90 * 1000  # 90s after expiry for Kalshi to settle

        for order_id, trade in list(self._position_mgr.open_orders.items()):
            if now_ms < trade.intent.contract.expiry_ts + grace_ms:
                continue  # not settled yet

            try:
                contract = await self._kalshi.get_contract(trade.intent.contract.ticker)
                if contract.result is None:
                    continue  # still pending settlement

                order = await self._kalshi.get_order(order_id)
                filled = order.filled_count

                if filled == 0:
                    # Order was never filled
                    logger.info("Order %s unfilled — no P&L impact", order_id)
                    self._confirmed_fills.pop(order_id, None)
                    self._tp_sell_orders.pop(order_id, None)
                    self._position_mgr.record_outcome(order_id, False, 0.0)
                    await self._signal_logger.log_kalshi_outcome(
                        order_id=order_id, is_win=False, pnl=0.0,
                        filled_count=0, result=contract.result,
                    )
                    await self._signal_logger.update_execution_by_order_id(
                        order_id, filled=0, expiry_outcome=contract.result, pnl=0.0,
                    )
                    continue

                # Determine win: our side matches the contract result
                is_win = contract.result == trade.intent.side
                fill_price = order.average_price or float(trade.intent.limit_price)

                if is_win:
                    pnl = filled * (1.0 - fill_price / 100.0)
                else:
                    pnl = -filled * (fill_price / 100.0)

                # Update position manager equity + daily state
                self._confirmed_fills.pop(order_id, None)
                self._tp_sell_orders.pop(order_id, None)
                self._position_mgr.record_outcome(order_id, is_win, pnl)

                # Update rolling WinRateTracker for degradation detection
                bucket = trade.intent.signal.bucket
                self._signal_filter.record_outcome(bucket, is_win)

                # Persist to DB
                await self._signal_logger.log_kalshi_outcome(
                    order_id=order_id,
                    is_win=is_win,
                    pnl=pnl,
                    filled_count=filled,
                    result=contract.result,
                )
                await self._signal_logger.update_execution_by_order_id(
                    order_id,
                    filled=1,
                    fill_price=fill_price,
                    fill_ts_ms=int(time.time() * 1000),
                    expiry_outcome=contract.result,
                    pnl=pnl,
                )

                logger.info(
                    "Outcome: %s | %s | filled=%d @ %.0f¢ | pnl=%+.2f | equity=%.2f",
                    "WIN" if is_win else "LOSS",
                    trade.intent.contract.ticker,
                    filled, fill_price, pnl,
                    self._position_mgr.equity,
                )

            except Exception as e:
                logger.error("Settle order %s failed: %s", order_id, e, exc_info=True)


def _load_private_key() -> str:
    """Load RSA private key PEM from env var or file path."""
    pem = os.getenv("KALSHI_PRIVATE_KEY", "")
    if pem:
        return pem.replace("\\n", "\n")  # handle single-line env var format
    path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if path:
        with open(os.path.expanduser(path)) as f:
            return f.read()
    return ""


_PID_FILE = Path(__file__).parent / "data" / "engine.pid"


def _acquire_pid_lock() -> bool:
    """Write PID file. Returns False if another instance is already running.

    Uses os.kill(pid, 0) — stdlib only, no psutil dependency. Signal 0 does
    not kill the process but raises OSError if the PID doesn't exist (stale
    file) or PermissionError if it exists but belongs to another user (still
    running). Either way we can distinguish live vs dead without psutil.
    """
    if _PID_FILE.exists():
        try:
            existing_pid = int(_PID_FILE.read_text().strip())
            os.kill(existing_pid, 0)  # Raises OSError if PID is dead
            # PID is alive — refuse to start a second instance
            return False
        except (OSError, ValueError):
            pass  # Stale PID file (process dead or file corrupt) — safe to overwrite
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    # Read config from environment variables for secrets
    key_id = os.getenv("KALSHI_API_KEY", "")          # API key ID (UUID)
    private_key_pem = _load_private_key()              # RSA private key PEM
    demo_mode = os.getenv("KALSHI_DEMO", "true").lower() != "false"
    execute = os.getenv("EXECUTE_TRADES", "false").lower() == "true"
    loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "500"))

    if not _acquire_pid_lock():
        logger.error(
            "Another engine instance is already running (PID %s). Aborting.",
            _PID_FILE.read_text().strip()
        )
        sys.exit(1)

    engine = Engine(
        kalshi_key_id=key_id,
        kalshi_private_key_pem=private_key_pem,
        kalshi_demo=demo_mode,
        daily_loss_limit=loss_limit,
        execute_trades=execute,
    )
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    main()
