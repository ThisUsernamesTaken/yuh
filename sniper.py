# sniper.py — Session-open momentum scalp
#
# One order. One price. One TP. No ladder. No ghosts.
#
# Signal: is BTC ticking up or down right now? (from Coinbase ticks)
# Entry: single market buy at 51c the moment Kalshi activates the contract
# Exit: limit sell at entry + TP offset (4-6c based on signal strength)
#
# Integration:
#   from sniper import sniper_check
#   await sniper_check(self)

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Optional

from kalshi_client import KalshiAPIError

logger = logging.getLogger(__name__)

_WINDOW_S = 15 * 60
_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

# Timing
_LOCK_WINDOW_S = 3.0
_LOCK_CUTOFF_S = 1.0
_ACTIVATION_TIMEOUT_S = 60.0

# Sizing
_BALANCE_FRACTION = 0.50
_MAX_DOLLARS = 50.0
_MAX_CONTRACTS = 100
_MAX_ENTRY = 65          # Skip if ask > 65c (avoid overpaying)


class _Phase(Enum):
    IDLE = auto()
    LOCKED = auto()
    WAITING = auto()      # Waiting for Kalshi to activate contract
    DEPLOYING = auto()    # Placing the single order
    FILLED = auto()
    DONE = auto()


class _State:
    STATE_VERSION = 10

    __slots__ = (
        "version", "phase", "boundary", "side", "count", "ticker",
        "regime", "tp_offset", "activation_ts",
        "fill_count", "fill_price", "fill_order_id", "tp_order_id",
    )

    def __init__(self):
        self.version = self.STATE_VERSION
        self.phase = _Phase.IDLE
        self.boundary = 0.0
        self.side = None
        self.count = 0
        self.ticker = ""
        self.regime = "skip"
        self.tp_offset = 5
        self.activation_ts = 0.0
        self.fill_count = 0
        self.fill_price = 0
        self.fill_order_id = ""
        self.tp_order_id = ""

    def reset(self):
        self.__init__()


# ── Ticker computation ─────────────────────────────────────────────────

def _next_boundary(now):
    return (int(now) // _WINDOW_S + 1) * _WINDOW_S

def _is_dst(dt):
    year = dt.year
    mar1 = datetime(year, 3, 1)
    s = 14 - mar1.weekday()
    if s <= 7: s += 7
    nov1 = datetime(year, 11, 1)
    e = 7 - nov1.weekday()
    if e == 0: e = 7
    return datetime(year, 3, s, 2) <= dt.replace(tzinfo=None) < datetime(year, 11, e, 2)

def compute_next_ticker(now):
    boundary = _next_boundary(now)
    close_utc = boundary + _WINDOW_S
    offset = timedelta(hours=-4) if _is_dst(datetime.now(timezone.utc)) else timedelta(hours=-5)
    dt = datetime.fromtimestamp(close_utc, tz=timezone(offset))
    return f"KXBTC15M-{dt.year%100}{_MONTH_ABBR[dt.month]}{dt.day:02d}{dt.hour:02d}{dt.minute:02d}-{dt.minute:02d}"


# ── Main ───────────────────────────────────────────────────────────────

async def sniper_check(engine) -> None:
    return  # DISABLED — sniper was never turned off and drained account from $172 to $8
    now = time.time()
    boundary = _next_boundary(now)
    ttb = boundary - now

    state = getattr(engine, '_sniper_state', None)
    if state is None or getattr(state, 'version', None) != _State.STATE_VERSION:
        state = _State()
        engine._sniper_state = state

    # Reset stale state
    if state.boundary > 0 and now > state.boundary + _ACTIVATION_TIMEOUT_S + 10:
        if state.phase != _Phase.IDLE:
            state.reset()

    # ── IDLE → LOCKED ────────────────────────────────────────
    if state.phase == _Phase.IDLE:
        if not (_LOCK_CUTOFF_S < ttb <= _LOCK_WINDOW_S):
            return
        if getattr(engine, '_open_position', None) is not None:
            return

        # No signal analysis. Lock every session. The contract mid decides the side at activation.
        try:
            bal = await engine._client.get_balance()
            dollars = bal.balance / 100.0
        except Exception:
            dollars = 100.0
        budget = min(dollars * _BALANCE_FRACTION, _MAX_DOLLARS)
        count = max(1, min(int(budget / (55 / 100.0)), _MAX_CONTRACTS))  # Estimate; resized at deploy

        state.phase = _Phase.LOCKED
        state.boundary = boundary
        state.side = "pending"  # Decided at activation from contract mid
        state.count = count
        state.ticker = compute_next_ticker(now)
        state.regime = "strong"
        state.tp_offset = 5

        logger.warning("SNIPER LOCKED: %dx (market) | %s | T-%.1fs",
                        count, state.ticker, ttb)
        return

    # ── LOCKED → WAITING ─────────────────────────────────────
    if state.phase == _Phase.LOCKED:
        if ttb > 0.1:
            return
        state.phase = _Phase.WAITING
        logger.info("SNIPER: boundary reached, waiting for activation")

    # ── WAITING: contract activated externally via _poll_kalshi_tape ──
    if state.phase == _Phase.WAITING:
        elapsed = now - state.boundary
        if elapsed > _ACTIVATION_TIMEOUT_S:
            logger.info("SNIPER: activation timeout (%.0fs)", elapsed)
            state.phase = _Phase.DONE
            return
        if getattr(engine, '_open_position', None) is not None:
            state.phase = _Phase.DONE
            return
        # Activation is set by _poll_kalshi_tape in the engine
        # when it detects a new contract ticker — it sets phase to DEPLOYING
        return

    # ── DEPLOYING: read contract mid, market buy the winning side ──
    if state.phase == _Phase.DEPLOYING:
        elapsed = now - state.boundary

        # Direction from WS orderbook (NOT tape — tape may still show old contract)
        ws = getattr(engine, '_kalshi_ws', None)
        book = None
        if ws:
            try:
                book = ws.get_book(state.ticker)
            except Exception:
                pass

        mid = book.mid_price_cents if book and book.is_ready else 50

        if mid > 50:
            state.side = "yes"
        elif mid < 50:
            state.side = "no"
        else:
            # Exactly 50 — wait for next poll cycle (0.5s), timeout after 30s
            since_activation = now - state.activation_ts
            if since_activation > 30:
                logger.info("SNIPER: mid stuck at 50c for 30s, skipping")
                state.phase = _Phase.DONE
            return

        if state.side == "yes":
            ask = book.best_yes_ask if book and book.is_ready else mid + 3
        else:
            ask = book.best_no_ask if book and book.is_ready else (100 - mid) + 3

        if ask > _MAX_ENTRY or ask < 1:
            logger.info("SNIPER: ask=%dc (outside %dc cap), skipping", ask, _MAX_ENTRY)
            state.phase = _Phase.DONE
            return

        # Size on the ask price (what we'll actually pay)
        try:
            bal = await engine._client.get_balance()
            dollars = bal.balance / 100.0
        except Exception:
            dollars = 100.0
        budget = min(dollars * _BALANCE_FRACTION, _MAX_DOLLARS)
        state.count = max(1, min(int(budget / (ask / 100.0)), _MAX_CONTRACTS))

        # Limit at ask = crosses spread = instant fill + price protection
        entry_price = min(ask + 1, _MAX_ENTRY)  # +1c buffer for racing

        logger.info("SNIPER DEPLOY: mid=%dc ask=%dc → %s %dx @ %dc (limit-cross)",
                     mid, ask, state.side.upper(), state.count, entry_price)

        try:
            order = await engine._client.place_order(
                ticker=state.ticker, side=state.side,
                price=entry_price, count=state.count,
            )
        except Exception as e:
            logger.error("SNIPER: order failed: %s", e)
            state.phase = _Phase.DONE
            return

        if not order:
            state.phase = _Phase.DONE
            return

        filled = order.filled_count
        # Cancel unfilled remainder IMMEDIATELY — prevent ghost fills from resting orders
        if order.order_id and filled < state.count:
            try:
                await engine._client.cancel_order(order.order_id)
                logger.info("SNIPER: cancelled unfilled remainder (%dx of %dx)",
                            state.count - filled, state.count)
            except Exception:
                pass

        if filled == 0:
            logger.info("SNIPER: 0 filled at %dc (no liquidity at ask)", entry_price)
            state.phase = _Phase.DONE
            return

        state.fill_count = filled
        state.fill_price = int(order.average_price) if order.average_price else ask
        state.fill_order_id = order.order_id or ""
        state.phase = _Phase.FILLED

        logger.warning(
            "SNIPER FILLED: %s %dx @ %dc ($%.2f) | %.1fs after boundary | %.1fs after activation",
            state.side.upper(), filled, state.fill_price,
            filled * state.fill_price / 100.0,
            elapsed, now - state.activation_ts,
        )
        # Fall through to FILLED

    # ── FILLED: place TP and register position ───────────────
    if state.phase == _Phase.FILLED:
        tp_price = min(state.fill_price + state.tp_offset, 99)

        tp_ids = []
        try:
            tp = await engine._client.place_order(
                ticker=state.ticker, side=state.side,
                price=tp_price, count=state.fill_count, action="sell",
            )
            if tp and tp.order_id:
                tp_ids.append(tp.order_id)
                logger.info("SNIPER TP: %dx @ %dc (+%dc) [%s]",
                            state.fill_count, tp_price, state.tp_offset, state.regime)
        except Exception as e:
            logger.error("SNIPER TP failed: %s", e)

        # Register position
        engine._open_position = {
            "order_id": state.fill_order_id,
            "side": state.side,
            "entry_cents": state.fill_price,
            "ticker": state.ticker,
            "tier": "SNIPER",
            "count": state.fill_count,
            "fill_time": time.time(),
            "entry_conviction": 0.95,
            "entry_wallets": 0,
            "entry_wallet_count_at_last_scale": 0,
            "high_water_bid": state.fill_price,
            "had_flow_at_entry": False,
            "tiers_in": {"SNIPER"},
            "entry_elite_wallets": set(),
            "_initial_fill_count": state.fill_count,
            "shallow_filled": state.fill_count,
            "shallow_price": state.fill_price,
            "deep_filled": 0,
            "deep_price": state.fill_price,
            "signal_wallet_names": [],
            "tp_order_id": tp_ids[0] if tp_ids else None,
            "tp_order_ids": tp_ids,
            "_resting_buy_ids": [],
            "tp_price": tp_price,
        }
        engine._window_start_time = time.time()
        engine._window_locked = True
        engine._trades_this_window = getattr(engine, '_trades_this_window', 0) + 1

        state.phase = _Phase.DONE
        logger.info("SNIPER COMPLETE: %s %dx @ %dc | TP @ %dc | activation=%.1fs",
                     state.side.upper(), state.fill_count, state.fill_price,
                     tp_price, state.activation_ts - state.boundary)
        return
