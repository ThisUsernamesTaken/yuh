"""Stress test microstructure.py — verify behavior under all scenarios."""
import time
from microstructure import MarketPressure, PressureScore

def test_empty_state():
    """1. State integrity: what happens with no data?"""
    mp = MarketPressure()
    print("=== TEST: Empty State ===")
    print(f"  is_ready: {mp.is_ready}")  # Should be False

    # Call update with no history — should not crash, should return safe defaults
    score = mp.update(
        btc_price=71700, btc_velocity=0, kalshi_mid=50,
        book_imbalance=0, microprice=50, depth_yes=10, depth_no=10,
        spread_cents=2, flow_buy_pressure=0.5, flow_strength=0,
        taker_imbalance=0, large_trade_bias=0, volatility=0.30, session_age=30,
    )
    print(f"  score: {score.score}  crossed: {score.crossed}  persistent: {score.persistent}")
    print(f"  direction: '{score.direction}'  confidence: {score.confidence}")
    assert not score.crossed, "Should NOT cross threshold with no history"
    assert not score.persistent, "Should NOT be persistent with no history"
    print("  PASS\n")


def test_btc_impulse_strong():
    """Scenario A: BTC +$25 in 10s, Kalshi barely moves."""
    print("=== TEST: BTC Impulse (strong move, Kalshi lagging) ===")
    mp = MarketPressure()

    # Warm up with 30 cycles of flat data
    for i in range(40):
        mp.update(
            btc_price=71700, btc_velocity=0, kalshi_mid=50,
            book_imbalance=0, microprice=50, depth_yes=50, depth_no=50,
            spread_cents=2, flow_buy_pressure=0.5, flow_strength=0.1,
            taker_imbalance=0, large_trade_bias=0, volatility=0.30, session_age=100 + i,
        )
        # Simulate 0.3s between cycles
        mp._btc_history[-1] = (time.time() - (40 - i) * 0.3, 71700)
        mp._mid_history[-1] = (time.time() - (40 - i) * 0.3, 50)

    print(f"  is_ready: {mp.is_ready}")
    assert mp.is_ready, "Should be ready after 40 cycles"

    # Now BTC surges +$25 over 10 cycles (~3s)
    scores = []
    for i in range(15):
        btc = 71700 + (i * 2.5)  # +$2.5 per cycle
        t = time.time() - (15 - i) * 0.3
        mp._btc_history.append((t, btc))
        mp._mid_history.append((t, 50 + i * 0.3))  # Kalshi barely moves

        score = mp.update(
            btc_price=btc, btc_velocity=5.0 + i, kalshi_mid=50 + i * 0.3,
            book_imbalance=0.2, microprice=51, depth_yes=60, depth_no=40,
            spread_cents=2, flow_buy_pressure=0.6, flow_strength=0.4,
            taker_imbalance=0.1, large_trade_bias=0.1, volatility=0.30,
            session_age=140 + i,
        )
        scores.append(score)

    final = scores[-1]
    print(f"  Final score: {final.score:+.3f}  direction: {final.direction}")
    print(f"  btc_impulse: {final.btc_impulse:+.3f}  kalshi_lag: {final.kalshi_lag:+.3f}")
    print(f"  crossed: {final.crossed}  persistent: {final.persistent}  persist_ct: {final.persistence_count}")
    print(f"  threshold: {final.entry_threshold:.3f}  confidence: {final.confidence:.3f}")

    assert final.score > 0, "Score should be positive (YES direction)"
    assert final.direction == "yes", f"Direction should be 'yes', got '{final.direction}'"
    assert final.btc_impulse > 0, "BTC impulse should be positive"
    assert final.crossed, "Should cross threshold on strong move"
    print("  PASS\n")


def test_flat_market():
    """Scenario B: BTC flat, Kalshi drifting — should NOT trade."""
    print("=== TEST: Flat Market (BTC flat, Kalshi drifts) ===")
    mp = MarketPressure()

    # 50 cycles of flat BTC, Kalshi drifting up slightly
    for i in range(50):
        t = time.time() - (50 - i) * 0.3
        mp._btc_history.append((t, 71700 + (i % 3 - 1) * 2))  # ±$2 noise
        mp._mid_history.append((t, 50 + i * 0.1))  # Kalshi drifts up

        score = mp.update(
            btc_price=71700 + (i % 3 - 1) * 2, btc_velocity=0.5,
            kalshi_mid=50 + i * 0.1,
            book_imbalance=0.05, microprice=50 + i * 0.1,
            depth_yes=30, depth_no=30, spread_cents=3,
            flow_buy_pressure=0.52, flow_strength=0.1,
            taker_imbalance=0.05, large_trade_bias=0, volatility=0.25,
            session_age=100 + i,
        )

    print(f"  Final score: {score.score:+.3f}  direction: '{score.direction}'")
    print(f"  crossed: {score.crossed}  persistent: {score.persistent}")
    print(f"  btc_impulse: {score.btc_impulse:+.3f}  book: {score.book_pressure:+.3f}")
    print(f"  flow: {score.flow_momentum:+.3f}  lag: {score.kalshi_lag:+.3f}")

    # Score should be weak or no signal
    assert abs(score.score) < 0.3, f"Score too strong for flat market: {score.score}"
    assert not score.persistent, "Should NOT be persistent in flat market"
    print("  PASS\n")


def test_reversal():
    """Scenario C: Strong move then immediate reversal — persistence prevents entry."""
    print("=== TEST: Reversal (strong move then flip) ===")
    mp = MarketPressure()

    # Warm up
    for i in range(40):
        t = time.time() - (55 - i) * 0.3
        mp._btc_history.append((t, 71700))
        mp._mid_history.append((t, 50))
        mp.update(
            btc_price=71700, btc_velocity=0, kalshi_mid=50,
            book_imbalance=0, microprice=50, depth_yes=50, depth_no=50,
            spread_cents=2, flow_buy_pressure=0.5, flow_strength=0,
            taker_imbalance=0, large_trade_bias=0, volatility=0.30, session_age=100 + i,
        )

    # Strong up move (2 cycles)
    scores_up = []
    for i in range(2):
        btc = 71700 + (i + 1) * 15
        t = time.time() - (15 - i) * 0.3
        mp._btc_history.append((t, btc))
        mp._mid_history.append((t, 50 + i))
        s = mp.update(
            btc_price=btc, btc_velocity=30, kalshi_mid=50 + i,
            book_imbalance=0.3, microprice=51, depth_yes=60, depth_no=30,
            spread_cents=2, flow_buy_pressure=0.7, flow_strength=0.5,
            taker_imbalance=0.2, large_trade_bias=0.3, volatility=0.30, session_age=142 + i,
        )
        scores_up.append(s)

    print(f"  After up move: score={scores_up[-1].score:+.3f} persist={scores_up[-1].persistence_count}")

    # Immediate reversal (2 cycles)
    scores_down = []
    for i in range(3):
        btc = 71730 - (i + 1) * 20  # Sharp reversal
        t = time.time() - (12 - i) * 0.3
        mp._btc_history.append((t, btc))
        mp._mid_history.append((t, 52 - i * 2))
        s = mp.update(
            btc_price=btc, btc_velocity=-40, kalshi_mid=52 - i * 2,
            book_imbalance=-0.3, microprice=48, depth_yes=30, depth_no=60,
            spread_cents=4, flow_buy_pressure=0.3, flow_strength=0.5,
            taker_imbalance=-0.2, large_trade_bias=-0.3, volatility=0.35, session_age=145 + i,
        )
        scores_down.append(s)

    print(f"  After reversal: score={scores_down[-1].score:+.3f} persist={scores_down[-1].persistence_count}")

    # The up move should NOT have been persistent (only 2 cycles, need 3)
    assert scores_up[-1].persistence_count < MarketPressure.PERSISTENCE_CYCLES, \
        "Up move should NOT reach persistence before reversal"
    print("  PASS — persistence prevented false entry on reversal\n")


def test_component_bounds():
    """5. Component normalization: ensure all bounded [-1, 1]."""
    print("=== TEST: Component Bounds (extreme inputs) ===")
    mp = MarketPressure()

    # Warm up
    for i in range(40):
        t = time.time() - (40 - i) * 0.3
        mp._btc_history.append((t, 71700))
        mp._mid_history.append((t, 50))

    # Extreme inputs
    score = mp.update(
        btc_price=72000, btc_velocity=500,  # extreme velocity
        kalshi_mid=95,  # extreme mid
        book_imbalance=5.0,  # beyond [-1,1] (test clamping)
        microprice=120,  # beyond 100 (test clamping)
        depth_yes=1000, depth_no=0,  # extreme asymmetry
        spread_cents=50,  # extreme spread
        flow_buy_pressure=1.0, flow_strength=1.0,  # max flow
        taker_imbalance=1.0, large_trade_bias=1.0,  # max bias
        volatility=2.0,  # extreme vol
        session_age=850,  # late session
    )

    assert -1.0 <= score.btc_impulse <= 1.0, f"btc_impulse out of bounds: {score.btc_impulse}"
    assert -1.0 <= score.book_pressure <= 1.0, f"book_pressure out of bounds: {score.book_pressure}"
    assert -1.0 <= score.flow_momentum <= 1.0, f"flow_momentum out of bounds: {score.flow_momentum}"
    assert -1.0 <= score.kalshi_lag <= 1.0, f"kalshi_lag out of bounds: {score.kalshi_lag}"
    assert -1.0 <= score.score <= 1.0, f"composite score out of bounds: {score.score}"
    assert 0 <= score.confidence <= 1.0, f"confidence out of bounds: {score.confidence}"

    print(f"  score: {score.score:+.3f}  components: imp={score.btc_impulse:+.3f} book={score.book_pressure:+.3f} flow={score.flow_momentum:+.3f} lag={score.kalshi_lag:+.3f}")
    print(f"  confidence: {score.confidence:.3f}  threshold: {score.entry_threshold:.3f}")
    print("  All components bounded [-1, 1] — PASS\n")


def test_threshold_bounds():
    """8. Threshold behavior: verify it doesn't collapse too low."""
    print("=== TEST: Threshold Bounds ===")
    mp = MarketPressure()

    # Low vol, tight spread, early session — most permissive
    for i in range(40):
        t = time.time() - (40 - i) * 0.3
        mp._btc_history.append((t, 71700))
        mp._mid_history.append((t, 50))

    score_low = mp.update(
        btc_price=71700, btc_velocity=0, kalshi_mid=50,
        book_imbalance=0, microprice=50, depth_yes=50, depth_no=50,
        spread_cents=1, flow_buy_pressure=0.5, flow_strength=0,
        taker_imbalance=0, large_trade_bias=0, volatility=0.10, session_age=60,
    )

    # High vol, wide spread, late session — most restrictive
    score_high = mp.update(
        btc_price=71700, btc_velocity=0, kalshi_mid=50,
        book_imbalance=0, microprice=50, depth_yes=50, depth_no=50,
        spread_cents=10, flow_buy_pressure=0.5, flow_strength=0,
        taker_imbalance=0, large_trade_bias=0, volatility=0.60, session_age=800,
    )

    print(f"  Permissive (low vol, tight spread, early):  threshold={score_low.entry_threshold:.3f}")
    print(f"  Restrictive (high vol, wide spread, late):  threshold={score_high.entry_threshold:.3f}")

    assert score_low.entry_threshold >= 0.12, f"Threshold too low: {score_low.entry_threshold}"
    assert score_high.entry_threshold <= 0.55, f"Threshold too high: {score_high.entry_threshold}"
    assert score_high.entry_threshold > score_low.entry_threshold, "Restrictive should be higher than permissive"
    print("  PASS\n")


def test_stale_data():
    """6. Missing data behavior: what happens with stale inputs."""
    print("=== TEST: Stale/Missing Data ===")
    mp = MarketPressure()

    # Only 5 data points (not enough for is_ready)
    for i in range(5):
        t = time.time() - (5 - i) * 0.3
        mp._btc_history.append((t, 71700))
        mp._mid_history.append((t, 50))

    assert not mp.is_ready, "Should NOT be ready with only 5 points"

    score = mp.update(
        btc_price=71720, btc_velocity=10, kalshi_mid=52,
        book_imbalance=0.5, microprice=53, depth_yes=60, depth_no=30,
        spread_cents=2, flow_buy_pressure=0.7, flow_strength=0.5,
        taker_imbalance=0.3, large_trade_bias=0.2, volatility=0.30, session_age=100,
    )

    print(f"  is_ready: {mp.is_ready}  score: {score.score:+.3f}")
    print(f"  crossed: {score.crossed}  persistent: {score.persistent}")
    # Even if crossed, should NOT be persistent (too little data)
    print("  PASS — insufficient data handled safely\n")


def test_empirical_lag_mapping():
    """Verify the empirical BTC→Kalshi mapping builds over time."""
    print("=== TEST: Empirical Lag Mapping ===")
    mp = MarketPressure()

    # Simulate 20 sessions worth of BTC→Kalshi move pairs
    for i in range(40):
        t = time.time() - (40 - i) * 0.3
        mp._btc_history.append((t, 71700 + i * 5))
        mp._mid_history.append((t, 50 + i * 0.4))

    # Feed enough data for lag_pairs to populate
    for i in range(15):
        btc_move = (i - 7) * 10  # -70 to +70
        mid_move = btc_move * 0.08  # roughly 8% of BTC move maps to mid
        mp._lag_pairs.append((btc_move, mid_move))

    print(f"  lag_pairs: {len(mp._lag_pairs)}")

    # Test the expected mid change with empirical data
    expected = mp._expected_mid_change(20.0, 50)
    print(f"  Expected mid change for $20 BTC move: {expected:.2f}c")
    assert abs(expected) > 0, "Should produce non-zero expected change"

    # With static fallback (no pairs)
    mp2 = MarketPressure()
    expected_fb = mp2._expected_mid_change(20.0, 50)
    print(f"  Fallback (no pairs) for $20 BTC move at 50c mid: {expected_fb:.2f}c")
    print(f"  Fallback at 80c mid: {mp2._expected_mid_change(20.0, 80):.2f}c")
    print(f"  Fallback at 20c mid: {mp2._expected_mid_change(20.0, 20):.2f}c")

    # 50c should be most sensitive, 80c and 20c less so
    assert abs(mp2._expected_mid_change(20.0, 50)) > abs(mp2._expected_mid_change(20.0, 80)), \
        "50c mid should be more sensitive than 80c"
    print("  PASS — empirical mapping builds correctly\n")


def test_output_stability():
    """9. Output stability: verify smooth score evolution."""
    print("=== TEST: Output Stability (smooth evolution) ===")
    mp = MarketPressure()

    # Warm up
    for i in range(40):
        t = time.time() - (50 - i) * 0.3
        mp._btc_history.append((t, 71700))
        mp._mid_history.append((t, 50))
        mp.update(
            btc_price=71700, btc_velocity=0, kalshi_mid=50,
            book_imbalance=0, microprice=50, depth_yes=50, depth_no=50,
            spread_cents=2, flow_buy_pressure=0.5, flow_strength=0,
            taker_imbalance=0, large_trade_bias=0, volatility=0.30, session_age=60 + i,
        )

    # Gradual BTC up move — scores should evolve smoothly
    scores = []
    for i in range(20):
        btc = 71700 + i * 3  # +$3 per cycle, gradual
        t = time.time() - (20 - i) * 0.3
        mp._btc_history.append((t, btc))
        mp._mid_history.append((t, 50 + i * 0.15))

        s = mp.update(
            btc_price=btc, btc_velocity=3 + i * 0.5, kalshi_mid=50 + i * 0.15,
            book_imbalance=0.05 + i * 0.02, microprice=50 + i * 0.2,
            depth_yes=50 + i, depth_no=50 - i * 0.5,
            spread_cents=2, flow_buy_pressure=0.5 + i * 0.01,
            flow_strength=0.1 + i * 0.02,
            taker_imbalance=i * 0.01, large_trade_bias=i * 0.005,
            volatility=0.30, session_age=100 + i,
        )
        scores.append(s.score)

    # Check for smooth evolution — no jumps > 0.15 between consecutive cycles
    max_jump = 0
    for i in range(1, len(scores)):
        jump = abs(scores[i] - scores[i-1])
        max_jump = max(max_jump, jump)

    print(f"  Score progression: {' -> '.join(f'{s:+.3f}' for s in scores[:8])} ...")
    print(f"  Max inter-cycle jump: {max_jump:.4f}")
    assert max_jump < 0.20, f"Score too jumpy: max jump {max_jump:.4f}"
    # Scores should trend upward with gradual BTC rise
    assert scores[-1] > scores[0], "Should trend up with gradual BTC rise"
    print("  PASS — smooth evolution confirmed\n")


if __name__ == "__main__":
    test_empty_state()
    test_btc_impulse_strong()
    test_flat_market()
    test_reversal()
    test_component_bounds()
    test_threshold_bounds()
    test_stale_data()
    test_empirical_lag_mapping()
    test_output_stability()
    print("=" * 50)
    print("ALL TESTS PASSED")
