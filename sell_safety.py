"""Pure helpers for sell-order inventory safety.

These helpers keep the live engine's sell path simple and testable: never
place more sell contracts than verified inventory minus already-resting sells.
"""
from __future__ import annotations

from typing import Any, Iterable


TERMINAL_ORDER_STATUSES = {
    "canceled",
    "cancelled",
    "closed",
    "expired",
    "filled",
    "resolved",
    "terminal",
}


def order_status_terminal(status: str | None) -> bool:
    """Return True when an order status is safely terminal."""
    return (status or "").strip().lower() in TERMINAL_ORDER_STATUSES


def order_side(order: dict[str, Any]) -> str:
    return str(order.get("side") or "").strip().lower()


def order_action(order: dict[str, Any]) -> str:
    return str(order.get("action") or "").strip().lower()


def order_price(order: dict[str, Any], side: str) -> int:
    """Extract the relevant side price from Kalshi's order payload variants."""
    key = "yes_price" if side == "yes" else "no_price"
    value = order.get(key, order.get("price", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def order_remaining_count(order: dict[str, Any]) -> int:
    """Extract remaining count, falling back to total count when needed."""
    value = order.get("remaining_count", order.get("count", 0))
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def filter_resting_side_sells(orders: Iterable[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    """Return resting sell orders for a specific YES/NO side."""
    normalized_side = side.strip().lower()
    return [
        order
        for order in orders
        if order_side(order) == normalized_side
        and order_action(order) == "sell"
        and order_remaining_count(order) > 0
    ]


def resting_sell_count(orders: Iterable[dict[str, Any]], side: str) -> int:
    """Sum resting sell contracts for a side."""
    return sum(order_remaining_count(order) for order in filter_resting_side_sells(orders, side))


def cap_sell_count(position_count: int, already_resting_count: int, requested_count: int) -> int:
    """Cap a new sell to uncovered inventory.

    If we have 100 YES and 40 YES sells already resting, a new sell may be at
    most 60. Negative or missing inputs are treated as zero.
    """
    position = max(0, int(position_count or 0))
    resting = max(0, int(already_resting_count or 0))
    requested = max(0, int(requested_count or 0))
    available = max(0, position - resting)
    return min(requested, available)
