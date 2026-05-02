from sell_safety import (
    cap_sell_count,
    filter_resting_side_sells,
    order_status_terminal,
    resting_sell_count,
)


def test_cap_sell_count_respects_existing_resting_inventory():
    assert cap_sell_count(position_count=100, already_resting_count=40, requested_count=80) == 60


def test_cap_sell_count_blocks_when_resting_covers_position():
    assert cap_sell_count(position_count=100, already_resting_count=100, requested_count=10) == 0
    assert cap_sell_count(position_count=100, already_resting_count=140, requested_count=10) == 0


def test_filter_resting_side_sells_ignores_other_side_and_buys():
    orders = [
        {"side": "yes", "action": "sell", "remaining_count": 5},
        {"side": "no", "action": "sell", "remaining_count": 7},
        {"side": "yes", "action": "buy", "remaining_count": 11},
        {"side": "yes", "action": "sell", "remaining_count": 0},
    ]
    ours = filter_resting_side_sells(orders, "yes")
    assert len(ours) == 1
    assert resting_sell_count(orders, "yes") == 5


def test_order_status_terminal_accepts_cancel_variants():
    assert order_status_terminal("canceled")
    assert order_status_terminal("cancelled")
    assert order_status_terminal("filled")
    assert not order_status_terminal("resting")
    assert not order_status_terminal("")
