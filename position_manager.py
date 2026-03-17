# position_manager.py — Stake sizing, risk management, and kill switch
#
# Translates a filtered ConsensusSignal + FilterDecision into a concrete
# Kalshi order: which contract, which side, how many contracts, at what price.
#
# Sizing philosophy:
#   - Stake scales with ADJUSTED confidence (post-regime multiplier)
#   - Hard per-trade cap ($STAKE_CAP) regardless of confidence
#   - Daily loss limit is a hard stop — no exceptions, no restart until next day
#   - Kalshi contracts cost (price_cents / 100) per contract; max payout is $1
#   - Effective edge = win_rate - breakeven_win_rate (where breakeven = price/100)

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional

from config import STAKE_CAP, MAX_PCT_EQUITY, STARTING_EQUITY, PAYOUT_ON_WIN, LOSS_ON_LOSE, \
    MIN_MINUTES_REMAINING, MAX_SPREAD_CENTS, MIN_CONTRACT_VOLUME, \
    MIN_POSITION_SCALE, MAX_POSITION_SCALE, TRADES_DB_PATH, \
    DYNAMIC_SIZING_ENABLED, DYNAMIC_SIZING_LOOKBACK, DYNAMIC_SIZING_FLOOR_PCT, \
    DYNAMIC_SIZING_TIERS
from config_phase3 import MIN_SIGNAL_CONFIDENCE
from kalshi_client import KalshiClient, KalshiContract, KalshiOrder, KalshiAPIError
from models import ConsensusSignal
from signal_intelligence import FilterDecision

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class TradeIntent:
    """A fully resolved trade decision ready for execution."""
    signal: ConsensusSignal
    decision: FilterDecision
    contract: KalshiContract
    side: str               # "yes" or "no"
    count: int              # Number of contracts
    limit_price: int        # Cents (1-99)
    dollar_risk: float      # Max dollar loss if contract expires worthless
    expected_payout: float  # Max dollar gain if contract resolves in our favor

    @property
    def breakeven_win_rate(self) -> float:
        """Win rate needed to break even at this price."""
        return self.limit_price / 100.0

    @property
    def edge(self) -> float:
        """Signal confidence minus breakeven — positive = edge exists."""
        return (self.decision.adjusted_confidence / 100.0) - self.breakeven_win_rate


@dataclass
class ExecutedTrade:
    """A completed order placement."""
    intent: TradeIntent
    order: KalshiOrder
    placed_at_ms: int
    notes: str = ""


@dataclass
class DailyState:
    """Tracks daily P&L and kill switch status."""
    trade_date: date
    realized_pnl: float = 0.0
    open_positions: int = 0
    trades_placed: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    killed: bool = False
    kill_reason: str = ""


# ─────────────────────────────────────────────
# Position Manager
# ─────────────────────────────────────────────

class PositionManager:
    """Manages trade sizing, contract selection, execution, and daily risk limits.

    Workflow:
        1. receive_signal(signal, decision) → TradeIntent or None
        2. execute(intent, client) → ExecutedTrade or None
        3. record_outcome(order_id, is_win, pnl) → updates daily state + win tracker
    """

    def __init__(
        self,
        daily_loss_limit: float = 500.0,
        starting_equity: float = STARTING_EQUITY,
        max_pct_equity: float = MAX_PCT_EQUITY,
        stake_cap: float = STAKE_CAP,
        min_edge: float = 0.05,
        min_minutes_remaining: float = MIN_MINUTES_REMAINING,
        max_spread_cents: float = MAX_SPREAD_CENTS,
        min_volume: int = MIN_CONTRACT_VOLUME,
        trades_db_path: str = TRADES_DB_PATH,
    ) -> None:
        self._daily_loss_limit = daily_loss_limit
        self._equity = starting_equity
        self._max_pct_equity = max_pct_equity
        self._stake_cap = stake_cap
        self._min_edge = min_edge
        self._min_minutes_remaining = min_minutes_remaining
        self._max_spread_cents = max_spread_cents
        self._min_volume = min_volume
        self._trades_db_path = trades_db_path

        self._daily = DailyState(trade_date=date.today())
        self._open_orders: dict[str, ExecutedTrade] = {}  # order_id → trade
        self._last_rejection: str = ""  # set by size_trade() for execution logging
        self._last_dynamic_pct: float = max_pct_equity  # tracks last reported tier for change logging

    # ── Kill switch ───────────────────────────────────────────────────────

    @property
    def is_killed(self) -> bool:
        """True if daily loss limit has been hit — no trading allowed."""
        self._check_day_rollover()
        return self._daily.killed

    def _check_day_rollover(self) -> None:
        """Reset daily state at UTC midnight."""
        today = datetime.now(timezone.utc).date()
        if today != self._daily.trade_date:
            logger.info("Day rollover: resetting daily state for %s", today)
            self._daily = DailyState(trade_date=today)

    def _check_kill_switch(self) -> bool:
        """Check if daily loss limit is hit. Returns True if trading should stop."""
        if self._daily.realized_pnl <= -self._daily_loss_limit:
            if not self._daily.killed:
                self._daily.killed = True
                self._daily.kill_reason = (
                    f"Daily loss limit hit: ${self._daily.realized_pnl:.2f} "
                    f"<= -${self._daily_loss_limit:.2f}"
                )
                logger.error("KILL SWITCH ACTIVATED: %s", self._daily.kill_reason)
            return True
        return False

    # ── Dynamic sizing ────────────────────────────────────────────────────

    def _compute_dynamic_max_pct(self) -> float:
        """Return the effective max_pct_equity based on rolling live win rate.

        Queries the last DYNAMIC_SIZING_LOOKBACK settled trades from DB and
        walks DYNAMIC_SIZING_TIERS top-down. First matching tier wins.
        Falls back to DYNAMIC_SIZING_FLOOR_PCT if no tier matches.
        """
        if not DYNAMIC_SIZING_ENABLED:
            return self._max_pct_equity

        try:
            conn = sqlite3.connect(self._trades_db_path)
            rows = conn.execute(
                """
                SELECT status FROM kalshi_trades
                WHERE status IN ('won', 'lost', 'exited_win', 'exited_loss')
                ORDER BY placed_at DESC
                LIMIT ?
                """,
                (DYNAMIC_SIZING_LOOKBACK,),
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning("Dynamic sizing DB query failed: %s", e)
            return self._max_pct_equity

        n = len(rows)
        wins = sum(1 for (s,) in rows if s in ("won", "exited_win"))
        wr = wins / n if n > 0 else 0.0

        for min_n, min_wr, pct in DYNAMIC_SIZING_TIERS:
            if n >= min_n and wr >= min_wr:
                if pct != self._last_dynamic_pct:
                    logger.warning(
                        "Dynamic sizing tier change: %.1f%% → %.1f%%  "
                        "(last %d trades: %d/%d = %.1f%% WR)",
                        self._last_dynamic_pct, pct, n, wins, n, wr * 100,
                    )
                    self._last_dynamic_pct = pct
                return pct

        effective = DYNAMIC_SIZING_FLOOR_PCT
        if effective != self._last_dynamic_pct:
            logger.info(
                "Dynamic sizing: floor %.1f%%  (last %d trades: %d/%d = %.1f%% WR)",
                effective, n, wins, n, wr * 100,
            )
            self._last_dynamic_pct = effective
        return effective

    # ── Signal → TradeIntent ──────────────────────────────────────────────

    def size_trade(
        self,
        signal: ConsensusSignal,
        decision: FilterDecision,
        contract: KalshiContract,
    ) -> Optional[TradeIntent]:
        """Convert a signal + contract into a sized TradeIntent.

        Returns None if:
        - Kill switch is active
        - Contract has insufficient time remaining
        - Edge is below minimum
        - Calculated position size is zero

        Args:
            signal:   The ConsensusSignal from the engine.
            decision: The FilterDecision (must be approved).
            contract: The Kalshi contract to trade.

        Returns:
            TradeIntent or None.
        """
        self._check_day_rollover()
        self._last_rejection = ""

        if self.is_killed:
            logger.warning("Trade rejected: kill switch active (%s)", self._daily.kill_reason)
            self._last_rejection = "KILL_SWITCH"
            return None

        if not decision.approved:
            logger.warning("Trade rejected: signal not approved (%s)", decision.reason.value)
            self._last_rejection = "NOT_APPROVED"
            return None

        if contract.minutes_to_expiry < self._min_minutes_remaining:
            logger.info(
                "Trade rejected: only %.1f min remaining on %s (need %.1f)",
                contract.minutes_to_expiry, contract.ticker, self._min_minutes_remaining
            )
            self._last_rejection = "TIME_EXPIRED"
            return None

        # Direction mapping for KXBTC15M:
        # CALL → buy YES (resolves yes if BTC closes >= open)
        # PUT  → buy NO  (resolves yes if BTC closes < open, i.e. the no side)
        side = "yes" if signal.direction == "CALL" else "no"

        bid = contract.yes_bid if side == "yes" else contract.no_bid
        ask = contract.yes_ask if side == "yes" else contract.no_ask

        # Liquidity filter: reject if bid-ask spread is too wide.
        # Wide spreads mean poor fills and high implicit slippage cost.
        spread_cents = (ask - bid) * 100.0
        if spread_cents > self._max_spread_cents:
            logger.info(
                "Trade rejected: spread %.0f¢ > max %.0f¢ on %s",
                spread_cents, self._max_spread_cents, contract.ticker
            )
            self._last_rejection = "SPREAD_TOO_WIDE"
            return None

        # Volume filter: reject thin markets with very few trades.
        if contract.volume < self._min_volume:
            logger.info(
                "Trade rejected: volume %d < min %d on %s",
                contract.volume, self._min_volume, contract.ticker
            )
            self._last_rejection = "LOW_VOLUME"
            return None

        # Limit price = ask price to guarantee an immediate fill.
        # Using mid-market risks the order resting and expiring unfilled.
        limit_price = max(1, min(99, round(ask * 100)))

        # Edge check: signal confidence must exceed breakeven by min_edge.
        # Use expected_wr from strategy if available for a more accurate edge measure.
        breakeven = limit_price / 100.0
        expected_wr = getattr(decision, 'expected_wr', 0.0)
        if expected_wr > 0:
            effective_conf = expected_wr / 100.0
        else:
            effective_conf = decision.adjusted_confidence / 100.0
        edge = effective_conf - breakeven

        # Near-midpoint penalty: contracts priced 45–55¢ are genuinely ~50/50 by market
        # consensus. Live data shows 2W/7L (-$2.40) in this bucket. The backtest WR of 63%
        # only gives ~13¢ gross edge vs a 50¢ ask — not enough buffer for BTC noise.
        # Require 2× the normal min_edge to enter near midpoint.
        if 45 <= limit_price <= 55:
            effective_min_edge = self._min_edge * 2.0
        else:
            effective_min_edge = self._min_edge

        if edge < effective_min_edge:
            logger.info(
                "Trade rejected: insufficient edge %.3f (conf=%.1f%%, breakeven=%.0f%%, min=%.3f%s)",
                edge, decision.adjusted_confidence, breakeven * 100, effective_min_edge,
                " [mid-price 2x]" if 45 <= limit_price <= 55 else ""
            )
            self._last_rejection = "LOW_EDGE"
            return None

        # Stake sizing: confidence-scaled with strategy position scale, capped.
        # Dollar risk = contracts × (price/100) per contract (cost of YES contracts)
        cost_per_contract = limit_price / 100.0

        # Position scale from strategy index (1.0 = standard, 1.5 = aggressive, 0.5 = conservative)
        position_scale = getattr(decision, 'position_scale', 1.0)
        position_scale = max(MIN_POSITION_SCALE, min(position_scale, MAX_POSITION_SCALE))

        effective_max_pct = self._compute_dynamic_max_pct()
        conf_frac = decision.adjusted_confidence / 100.0
        base_risk = self._equity * (effective_max_pct / 100.0) * conf_frac
        scaled_risk = base_risk * position_scale
        target_dollar_risk = min(scaled_risk, self._stake_cap)

        count = max(1, int(target_dollar_risk / cost_per_contract))

        # Recalculate actual dollar risk with rounded count
        dollar_risk = count * cost_per_contract
        expected_payout = count * 1.00  # $1 max payout per contract

        return TradeIntent(
            signal=signal,
            decision=decision,
            contract=contract,
            side=side,
            count=count,
            limit_price=limit_price,
            dollar_risk=dollar_risk,
            expected_payout=expected_payout,
        )

    # ── Execution ─────────────────────────────────────────────────────────

    async def execute(
        self,
        intent: TradeIntent,
        client: KalshiClient,
    ) -> Optional[ExecutedTrade]:
        """Place the Kalshi order for a TradeIntent.

        Logs the order and tracks it in open positions.
        Returns None if the order fails.

        Args:
            intent: Sized TradeIntent from size_trade().
            client: Authenticated KalshiClient.
        """
        self._check_day_rollover()
        if self.is_killed:
            return None

        logger.info(
            "Placing order: %s %s x%d [market, ask_ref=%d¢] (edge=%.1f%%, risk=$%.2f)",
            intent.contract.ticker, intent.side.upper(),
            intent.count, intent.limit_price,
            intent.edge * 100, intent.dollar_risk,
        )

        try:
            order = await client.place_order(
                ticker=intent.contract.ticker,
                side=intent.side,
                count=intent.count,
                price=intent.limit_price,
                order_type="limit",
            )
        except KalshiAPIError as e:
            logger.error("Order placement failed: %s", e)
            self._last_rejection = f"API_ERROR: {e}"
            return None
        except Exception as e:
            logger.error("Unexpected error placing order: %s", e, exc_info=True)
            self._last_rejection = f"UNEXPECTED_ERROR: {type(e).__name__}"
            return None

        trade = ExecutedTrade(
            intent=intent,
            order=order,
            placed_at_ms=int(time.time() * 1000),
        )
        self._open_orders[order.order_id] = trade
        self._daily.trades_placed += 1
        self._daily.open_positions += 1

        logger.info("Order placed: id=%s status=%s", order.order_id, order.status)
        return trade

    # ── Outcome recording ─────────────────────────────────────────────────

    def record_outcome(self, order_id: str, is_win: bool, pnl: float) -> None:
        """Record the resolved outcome of a trade.

        Updates equity, daily P&L, and triggers kill switch check.

        Args:
            order_id: The Kalshi order ID.
            is_win:   True if the contract resolved in our favor.
            pnl:      Dollar P&L (positive = profit, negative = loss).
        """
        trade = self._open_orders.pop(order_id, None)
        if trade is None:
            logger.warning("record_outcome: unknown order_id %s", order_id)
            return

        self._equity += pnl
        self._daily.realized_pnl += pnl
        self._daily.open_positions = max(0, self._daily.open_positions - 1)

        if is_win:
            self._daily.trades_won += 1
            logger.info("Trade WON: %s  pnl=+$%.2f  equity=$%.2f", order_id, pnl, self._equity)
        else:
            self._daily.trades_lost += 1
            logger.info("Trade LOST: %s  pnl=-$%.2f  equity=$%.2f", order_id, abs(pnl), self._equity)

        self._check_kill_switch()

    # ── State ─────────────────────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def daily_state(self) -> DailyState:
        self._check_day_rollover()
        return self._daily

    @property
    def open_orders(self) -> dict[str, ExecutedTrade]:
        return dict(self._open_orders)

    @property
    def last_rejection(self) -> str:
        """Rejection reason from the most recent size_trade() call. Empty string if approved."""
        return self._last_rejection
