from protective_math import compute_mfe_trail_price


def test_mfe_trail_does_not_arm_before_threshold():
    decision = compute_mfe_trail_price(
        entry_cents=30,
        bid_cents=37,
        previous_mfe_cents=0,
        threshold_cents=8,
    )
    assert decision.mfe_cents == 7
    assert decision.trail_price is None
    assert not decision.armed


def test_mfe_trail_locks_profit_after_threshold():
    decision = compute_mfe_trail_price(
        entry_cents=30,
        bid_cents=50,
        previous_mfe_cents=0,
        min_profit_cents=4,
        base_trail_cents=3,
        trail_ratio=0.4,
        threshold_cents=8,
    )
    assert decision.mfe_cents == 20
    assert decision.trail_price == 42
    assert decision.armed


def test_mfe_trail_uses_previous_high_water_mark_for_giveback():
    decision = compute_mfe_trail_price(
        entry_cents=30,
        bid_cents=44,
        previous_mfe_cents=20,
        min_profit_cents=4,
        base_trail_cents=3,
        trail_ratio=0.4,
        threshold_cents=8,
    )
    assert decision.mfe_cents == 20
    assert decision.trail_price == 36
    assert decision.armed


def test_mfe_trail_never_below_min_profit():
    decision = compute_mfe_trail_price(
        entry_cents=60,
        bid_cents=66,
        previous_mfe_cents=12,
        min_profit_cents=4,
        base_trail_cents=10,
        trail_ratio=0.4,
        threshold_cents=8,
    )
    assert decision.trail_price == 64
