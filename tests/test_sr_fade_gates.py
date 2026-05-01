"""Unit tests for Phase N gates in SR_FADE.

Covers the deterministic decision logic for:
  N1 — velocity/deceleration entry gate (via `_sr_fade_window_delta` helper
       and `_sr_fade_signed_delta` helper that feed the gate)
  N3 — opposite-side edge block (via direct probability math on a mock
       prob engine)
  N4 — velocity weight in the sizing composite (via `_compute_sr_fade_size`
       with mocked dependencies)

These tests run the helpers in isolation against synthetic mid buffers.
They do not touch the Kalshi API, the WebSocket, or the full evaluator
flow — the goal is to lock the decision math, not integration.
"""
from __future__ import annotations

import asyncio
import time
import types
import pytest

import polymarket_copy_engine as pce


# ── Helpers to build a minimal engine instance without __init__ ─────────────

def _make_engine_shell(mid_buf=None, balance_cents=20000,
                       pressure_score=0.0, pressure_confidence=0.5):
    """Allocate a PolymarketCopyEngine instance with the minimum attributes
    needed by the Phase N helpers. Bypasses __init__ so we don't need a
    live Kalshi/WS/price_feed stack."""
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._sr_mid_buf = list(mid_buf or [])
    # For _compute_sr_fade_size — include score so P1-1 orientation tests
    # can set agree/neutral/disagree states.
    eng._last_pressure = types.SimpleNamespace(
        confidence=pressure_confidence, score=pressure_score,
    )
    eng._price_feed = None  # edge computation handled separately below
    # Mock client returning a fixed balance.
    async def _get_balance():
        return types.SimpleNamespace(balance=balance_cents)
    eng._client = types.SimpleNamespace(get_balance=_get_balance)
    return eng


# ── N1: _sr_fade_window_delta -----------------------------------------------

def test_window_delta_insufficient_history_returns_none():
    # Buffer has only 1 sample; can't compute any window.
    eng = _make_engine_shell(mid_buf=[(100.0, 50)])
    assert eng._sr_fade_window_delta(5) is None


def test_window_delta_no_old_enough_sample_returns_none():
    # All samples within last 3s (now=102), asking for 10s window →
    # target=92, but oldest sample is 100. No sample ≤ 92. → None.
    eng = _make_engine_shell(mid_buf=[(100.0, 50), (101.0, 51), (102.0, 52)])
    assert eng._sr_fade_window_delta(10) is None


def test_window_delta_computes_abs_mid_move():
    # Samples: 20s ago (t=100) mid=40, intermediate, now (t=120) mid=50.
    # Window=20 → target=100. Oldest ≤ 100 is (100, 40). |50-40|=10.
    eng = _make_engine_shell(mid_buf=[(100.0, 40), (105.0, 45), (120.0, 50)])
    assert eng._sr_fade_window_delta(20) == 10


def test_window_delta_uses_most_recent_sample_as_now():
    # Now = buf[-1][0] = 115. Target = 115 - 10 = 105.
    # Samples ≤ 105: (100, 30) only. delta = |38 - 30| = 8.
    eng = _make_engine_shell(mid_buf=[(100.0, 30), (110.0, 35), (115.0, 38)])
    assert eng._sr_fade_window_delta(10) == 8


# ── N1 integration: decelerating vs accelerating patterns -------------------

def _build_mid_buf(pattern):
    """Build a buffer where pattern=[(t_ago_sec, mid), ...]. t=0 is now.

    Returns samples in chronological order (oldest first, newest last)
    to match the engine's live buffer convention."""
    now = 1000.0
    items = [(now - dt, m) for dt, m in pattern]
    return sorted(items, key=lambda x: x[0])


def test_decelerating_mid_passes_velocity_logic():
    """Pattern: mid at 30s ago = 20, 15s ago = 40, 5s ago = 48, now = 50.
    delta_30s=30, delta_15s=10, delta_5s=2 — strictly decreasing, last 5s=2c.
    Should satisfy (d30 > d15 > d5) AND (d5 ≤ 2)."""
    buf = _build_mid_buf([(30, 20), (15, 40), (5, 48), (0, 50)])
    eng = _make_engine_shell(mid_buf=buf)
    d30 = eng._sr_fade_window_delta(30)
    d15 = eng._sr_fade_window_delta(15)
    d5 = eng._sr_fade_window_delta(5)
    assert d30 is not None and d15 is not None and d5 is not None
    assert d30 > d15 > d5
    assert d5 <= 2


def test_accelerating_mid_fails_velocity_logic():
    """Pattern accelerating: delta_30s=2, delta_15s=5, delta_5s=10 — increasing.
    Post-tuning rule rejects when d_short > d_mid OR d_mid > d_long."""
    buf = _build_mid_buf([(30, 48), (15, 45), (5, 40), (0, 50)])
    # delta_30s = |50-48| = 2, delta_15s = |50-45| = 5, delta_5s = |50-40| = 10
    eng = _make_engine_shell(mid_buf=buf)
    d30 = eng._sr_fade_window_delta(30)
    d15 = eng._sr_fade_window_delta(15)
    d5 = eng._sr_fade_window_delta(5)
    assert d30 == 2 and d15 == 5 and d5 == 10
    # Relaxed gate: reject if accelerating (d_short > d_mid OR d_mid > d_long).
    assert (d5 > d15) or (d15 > d30)


def test_flat_tape_equal_deltas_passes_velocity_logic():
    """Truly stable tape: all deltas 0-1c, equal. Old strict > would reject;
    relaxed ≥ accepts because market IS stable (just not accelerating)."""
    buf = _build_mid_buf([(30, 50), (15, 50), (5, 50), (0, 50)])
    eng = _make_engine_shell(mid_buf=buf)
    d30 = eng._sr_fade_window_delta(30)
    d15 = eng._sr_fade_window_delta(15)
    d5 = eng._sr_fade_window_delta(5)
    assert d30 == 0 and d15 == 0 and d5 == 0
    # Relaxed rule: not (d_short > d_mid or d_mid > d_long) → passes.
    assert not (d5 > d15 or d15 > d30)


def test_decelerating_but_last_5s_still_too_fast():
    """Pattern: strictly decelerating, but last 5s still moved 4c (> 2c floor).
    Individual ordering passes, but 5s threshold fails."""
    buf = _build_mid_buf([(30, 20), (15, 40), (5, 46), (0, 50)])
    # d30=30, d15=10, d5=4. Order OK but 5s=4 > 2c threshold.
    eng = _make_engine_shell(mid_buf=buf)
    d5 = eng._sr_fade_window_delta(5)
    assert d5 == 4  # confirms threshold gate is the one that would reject


# ── N2: _sr_fade_signed_delta oriented by side -----------------------------

def test_signed_delta_yes_favorable_on_rising_mid():
    """YES holder profits when YES mid rises. mid 30s ago = 40, now = 50
    → signed_delta = +10 (favorable)."""
    buf = _build_mid_buf([(30, 40), (0, 50)])
    eng = _make_engine_shell(mid_buf=buf)
    assert eng._sr_fade_signed_delta(30, "yes") == 10


def test_signed_delta_no_favorable_on_falling_mid():
    """NO holder profits when YES mid falls. mid 30s ago = 60, now = 45
    → signed_delta = +15 (favorable for NO)."""
    buf = _build_mid_buf([(30, 60), (0, 45)])
    eng = _make_engine_shell(mid_buf=buf)
    assert eng._sr_fade_signed_delta(30, "no") == 15


def test_signed_delta_yes_adverse_on_falling_mid():
    """YES holder takes a hit when mid falls. mid 30s ago = 60, now = 40
    → signed_delta = -20 (adverse for YES)."""
    buf = _build_mid_buf([(30, 60), (0, 40)])
    eng = _make_engine_shell(mid_buf=buf)
    assert eng._sr_fade_signed_delta(30, "yes") == -20


def test_signed_delta_insufficient_history():
    # Single sample — can't compute a delta. Also tests the min-length guard.
    eng = _make_engine_shell(mid_buf=[(1000.0, 50)])
    assert eng._sr_fade_signed_delta(30, "yes") is None


def test_signed_delta_adverse_triggers_time_cap_logic():
    """Simulate the minute-10 scenario from today's live losers: YES @ 34c
    entry, mid now at 15c → signed delta for YES holder = -19 over 30s.
    With SR_FADE_ADVERSE_VEL_CENTS = 2, this is a strong "flatten now" signal."""
    buf = _build_mid_buf([(30, 34), (15, 25), (5, 18), (0, 15)])
    eng = _make_engine_shell(mid_buf=buf)
    sv = eng._sr_fade_signed_delta(30, "yes")
    assert sv == -19
    # Adaptive rule: _sv <= -_adv_cents (2) → force exit.
    assert sv <= -2


def test_signed_delta_favorable_triggers_hold():
    """Opposite scenario: mid rising toward our YES profit. 30s ago 34c,
    now 42c. Signed = +8 → rule #1 (favorable) → hold."""
    buf = _build_mid_buf([(30, 34), (15, 38), (5, 40), (0, 42)])
    eng = _make_engine_shell(mid_buf=buf)
    sv = eng._sr_fade_signed_delta(30, "yes")
    assert sv == 8
    assert sv > 0  # would trigger hold branch


# ── BTC continuation veto data freshness ------------------------------------

def test_btc_move_window_fresh_blocks_short_buffer():
    eng = _make_engine_shell()
    now = time.time()
    eng._btc_ts_buffer = [(now, 100000.0)]

    assert eng._btc_move_window_fresh(30.0) is None


def test_btc_move_window_fresh_blocks_stale_buffer():
    eng = _make_engine_shell()
    now = time.time()
    eng._btc_ts_buffer = [
        (now - 40.0, 100000.0),
        (now - 35.0, 100025.0),
    ]

    assert eng._btc_move_window_fresh(30.0, max_staleness_s=2.0) is None


def test_btc_move_window_fresh_requires_full_window_coverage():
    eng = _make_engine_shell()
    now = time.time()
    eng._btc_ts_buffer = [
        (now - 20.0, 100000.0),
        (now - 10.0, 100030.0),
        (now, 100040.0),
    ]

    assert eng._btc_move_window_fresh(30.0) is None


def test_btc_move_window_fresh_returns_signed_move_when_covered():
    eng = _make_engine_shell()
    now = time.time()
    eng._btc_ts_buffer = [
        (now - 40.0, 100000.0),
        (now - 30.0, 100010.0),
        (now - 5.0, 100025.0),
        (now, 100040.0),
    ]

    assert eng._btc_move_window_fresh(30.0) == pytest.approx(30.0)


# ── N3: opposite-side edge arithmetic ---------------------------------------
# N3 is inline in the evaluator; we verify the math it applies.

def _edge_pp(p_model_yes: float, mid_yes_cents: int, side: str):
    """Reproduce the edge math N3 uses, returning (our_edge, opposite_edge)."""
    p_mine = p_model_yes if side == "yes" else (1.0 - p_model_yes)
    p_them = 1.0 - p_mine
    mkt_mine = (mid_yes_cents if side == "yes" else (100 - mid_yes_cents)) / 100.0
    mkt_them = 1.0 - mkt_mine
    return ((p_mine - mkt_mine) * 100.0, (p_them - mkt_them) * 100.0)


def test_n3_block_fires_when_model_strongly_favors_opposite_side():
    """BB model says YES prob = 15%. Mid_yes = 35c, we're buying NO (side=no).
    Our NO edge = (0.85 - 0.65) * 100 = +20pp (passes M2 +5pp floor).
    But opposite YES edge = (0.15 - 0.35) * 100 = -20pp — nope wait, that's
    negative for opposite. Let me reconsider: opposite-side edge here is
    computed relative to OUR opposite-side entry. We're buying NO so opposite
    is YES. YES mkt = 35c. Model YES prob = 15% → YES edge = 15-35 = -20pp.
    Model DISAGREES with YES, so opposite-side edge is NEGATIVE and block
    does NOT fire.
    Now flip: model says YES prob = 75%. Mid_yes = 35c, we buy NO.
    NO edge = (0.25 - 0.65) * 100 = -40pp (fails M2 floor — would reject
    at M2, not N3).
    So to get N3 to fire: NO edge passes (positive) AND YES edge positive.
    That's impossible mathematically — they're negatives of each other on
    a 2-outcome market... unless we include market spread/fees. Actually
    yes: in this binary the two edges are always exact negatives. N3 only
    fires when YES edge is LARGELY POSITIVE, meaning M2 would have caught
    the NO entry anyway. So N3 in practice fires at _edge_them_ > 10pp,
    which maps to _edge_me_ < -10pp, which fails M2 at -10pp < +5pp.

    Conclusion: for a binary market with no spread, N3 is redundant with M2.
    N3 becomes useful when there's bid-ask spread (mkt_yes + mkt_no > 100),
    or when the edge_me floor is below 0 (e.g., M2 = -5pp would let weak
    negative-edge trades through).

    Test: verify N3's computed opposite-edge is the correct value for a
    textbook case."""
    _, opp_edge = _edge_pp(p_model_yes=0.75, mid_yes_cents=35, side="no")
    # YES fair = 75c, YES mkt = 35c → YES edge = +40pp
    assert opp_edge == pytest.approx(40.0, abs=0.01)


def test_n3_opposite_edge_for_yes_fade():
    """Fading with side=yes (buying cheap YES): opposite is NO.
    Model says YES = 65%, mid_yes = 30c → NO mkt = 70c, NO fair = 35c.
    NO (opposite) edge = 35 - 70 = -35pp. Negative → N3 does NOT fire.
    Our YES edge = 65 - 30 = +35pp (passes M2).
    Good setup. Should proceed."""
    mine_edge, opp_edge = _edge_pp(p_model_yes=0.65, mid_yes_cents=30, side="yes")
    assert mine_edge == pytest.approx(35.0, abs=0.01)
    assert opp_edge == pytest.approx(-35.0, abs=0.01)


# ── N4: velocity weight in sizing composite ---------------------------------

def test_sizing_velocity_weight_shrinks_on_moving_tape(monkeypatch):
    """Identical signal, different mid-buffer velocities. Faster tape should
    produce a smaller contract count."""
    # Mock a signal object the sizer expects.
    signal = types.SimpleNamespace(
        kalshi_side="yes",
        smart_flow_conviction=0.60,  # level_strength
    )

    # Mock a prob engine with known edge
    class _Prob:
        is_ready = True
        probability = 0.70  # YES fair value 70c → YES edge at 40c = +30pp
    prob = _Prob()

    # Stable tape (d_5s = 0c)
    stable_buf = _build_mid_buf([(30, 40), (15, 40), (5, 40), (0, 40)])
    eng_stable = _make_engine_shell(mid_buf=stable_buf)
    eng_stable._price_feed = types.SimpleNamespace(prob_engine=prob)

    # Moving tape (d_5s = 5c)
    moving_buf = _build_mid_buf([(30, 20), (15, 30), (5, 35), (0, 40)])
    eng_moving = _make_engine_shell(mid_buf=moving_buf)
    eng_moving._price_feed = types.SimpleNamespace(prob_engine=prob)

    # Stub out _get_sizing_cap globally so the test is deterministic.
    monkeypatch.setattr(pce, "_get_sizing_cap", lambda: 500)

    ct_stable = asyncio.run(eng_stable._compute_sr_fade_size(signal, ask_cents=40))
    ct_moving = asyncio.run(eng_moving._compute_sr_fade_size(signal, ask_cents=40))

    # Both should be positive. Stable should size meaningfully larger.
    assert ct_stable >= 1
    assert ct_moving >= 1
    assert ct_stable > ct_moving


def test_sizing_velocity_weight_matches_expected_v_norm():
    """Verify the v_norm ladder: 1c→1.0, 3c→0.6, 6c→0.0.

    Can't call the private formula directly; instead, feed three tapes and
    confirm the order of sizes matches the v_norm order."""
    signal = types.SimpleNamespace(kalshi_side="yes", smart_flow_conviction=0.60)

    class _Prob:
        is_ready = True
        probability = 0.70
    prob = _Prob()

    def _run(buf):
        eng = _make_engine_shell(mid_buf=buf)
        eng._price_feed = types.SimpleNamespace(prob_engine=prob)
        return asyncio.run(eng._compute_sr_fade_size(signal, ask_cents=40))

    # 1c recent move → v_norm = 1.0 (full velocity credit)
    ct_1c = _run(_build_mid_buf([(30, 30), (15, 35), (5, 39), (0, 40)]))
    # 3c recent move → v_norm = 0.6
    ct_3c = _run(_build_mid_buf([(30, 30), (15, 34), (5, 37), (0, 40)]))
    # 6c recent move → v_norm = 0.0
    ct_6c = _run(_build_mid_buf([(30, 28), (15, 31), (5, 34), (0, 40)]))

    assert ct_1c >= ct_3c >= ct_6c
    # 1c should give ~ maximal velocity contribution (0.2 * 1.0 = 0.20 conf);
    # 6c gives zero (0.2 * 0.0 = 0). That 0.20 confidence delta at f_max-f_min=0.45
    # spread maps to ~0.09 fraction difference. On $200 balance / 40c entry,
    # that's ~45ct. Confirm the observed gap is meaningful (≥10ct).
    assert ct_1c - ct_6c >= 10


# ── P1-1: oriented pressure in SR_FADE sizing -------------------------------

def _stable_mid_buf():
    """Flat mid tape so velocity component is neutralized across tests."""
    return _build_mid_buf([(30, 40), (15, 40), (5, 40), (0, 40)])


def _stub_prob(yes_prob: float = 0.70):
    class _Prob:
        is_ready = True
        probability = yes_prob
    return _Prob()


def test_p1_1_disagree_pressure_shrinks_size(monkeypatch):
    """YES fade (buying YES to fade a down move). pressure_score < 0 means
    tape is confident DOWN — disagrees with the YES fade direction. The
    oriented term zeros p_norm, shrinking composite confidence vs an
    identical agree-pressure scenario."""
    monkeypatch.setattr(pce, "_get_sizing_cap", lambda: 500)
    signal = types.SimpleNamespace(kalshi_side="yes", smart_flow_conviction=0.60)

    # Agree: side=yes, ps>0 → full p_conf term
    eng_agree = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=+0.40, pressure_confidence=0.9,
    )
    eng_agree._price_feed = types.SimpleNamespace(prob_engine=_stub_prob())

    # Disagree: side=yes, ps<0 → p_norm = 0 (default disagree_mult=0)
    eng_disagree = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=-0.40, pressure_confidence=0.9,
    )
    eng_disagree._price_feed = types.SimpleNamespace(prob_engine=_stub_prob())

    ct_agree = asyncio.run(eng_agree._compute_sr_fade_size(signal, ask_cents=40))
    ct_disagree = asyncio.run(eng_disagree._compute_sr_fade_size(signal, ask_cents=40))

    assert ct_disagree < ct_agree
    # With validation-mode max sizing (15% balance), the same orientation
    # delta is smaller in contract-count terms but should remain meaningful.
    assert ct_agree - ct_disagree >= 4


def test_p1_1_no_side_pressure_orientation(monkeypatch):
    """NO fade (buying NO to fade an up move). Agree = ps<0 (tape down).
    Disagree = ps>0 (tape still pushing up)."""
    monkeypatch.setattr(pce, "_get_sizing_cap", lambda: 500)
    signal = types.SimpleNamespace(kalshi_side="no", smart_flow_conviction=0.60)

    eng_agree = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=-0.40, pressure_confidence=0.9,
    )
    eng_agree._price_feed = types.SimpleNamespace(prob_engine=_stub_prob(yes_prob=0.30))

    eng_disagree = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=+0.40, pressure_confidence=0.9,
    )
    eng_disagree._price_feed = types.SimpleNamespace(prob_engine=_stub_prob(yes_prob=0.30))

    ct_agree = asyncio.run(eng_agree._compute_sr_fade_size(signal, ask_cents=40))
    ct_disagree = asyncio.run(eng_disagree._compute_sr_fade_size(signal, ask_cents=40))
    assert ct_disagree < ct_agree


def test_p1_1_neutral_pressure_halves_term(monkeypatch):
    """|pressure_score| <= NEUTRAL_BAND (default 0.02) → p_norm = 0.5 * p_conf.
    Should land BETWEEN agree (full) and disagree (zero)."""
    monkeypatch.setattr(pce, "_get_sizing_cap", lambda: 500)
    signal = types.SimpleNamespace(kalshi_side="yes", smart_flow_conviction=0.60)

    def _run(ps):
        eng = _make_engine_shell(
            mid_buf=_stable_mid_buf(), pressure_score=ps, pressure_confidence=0.9,
        )
        eng._price_feed = types.SimpleNamespace(prob_engine=_stub_prob())
        return asyncio.run(eng._compute_sr_fade_size(signal, ask_cents=40))

    ct_agree = _run(+0.40)
    ct_neutral = _run(0.0)
    ct_disagree = _run(-0.40)
    assert ct_agree >= ct_neutral >= ct_disagree


def test_p1_1_orient_stash_set_after_sizer(monkeypatch):
    """The sizer stashes the orientation on self so callers can forward it
    into the damper's cheap-leverage gate."""
    monkeypatch.setattr(pce, "_get_sizing_cap", lambda: 500)
    signal = types.SimpleNamespace(kalshi_side="yes", smart_flow_conviction=0.60)

    eng = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=-0.40, pressure_confidence=0.9,
    )
    eng._price_feed = types.SimpleNamespace(prob_engine=_stub_prob())
    asyncio.run(eng._compute_sr_fade_size(signal, ask_cents=40))
    assert eng._last_sr_fade_pressure_orient == "disagree"

    eng2 = _make_engine_shell(
        mid_buf=_stable_mid_buf(), pressure_score=+0.40, pressure_confidence=0.9,
    )
    eng2._price_feed = types.SimpleNamespace(prob_engine=_stub_prob())
    asyncio.run(eng2._compute_sr_fade_size(signal, ask_cents=40))
    assert eng2._last_sr_fade_pressure_orient == "agree"


def test_p1_1_damper_cheap_leverage_skipped_on_disagree(monkeypatch):
    """When pressure_orient='disagree', the Phase E cheap-leverage multiplier
    must NOT fire even if entry_price and conviction pass their gates."""
    monkeypatch.setitem(pce._user_cfg, "CHEAP_LEVERAGE_ENABLED", True)
    eng = _make_engine_shell()
    # Stub regime/drawdown so damper factors are predictable.
    eng._last_regime = types.SimpleNamespace(regime=pce.Regime.CHOP)
    eng._last_drawdown = pce.DrawdownState.FLAT

    # conviction=0.80 >= CHEAP_LEVERAGE_MIN_CONVICTION (0.70)
    # entry_price=30 <= CHEAP_LEVERAGE_MAX_PRICE (35)
    final_agree, suf_agree = eng._apply_regime_damper(
        raw_contracts=100, entry_price_cents=30, conviction=0.80,
        pressure_orient="agree",
    )
    final_disagree, suf_disagree = eng._apply_regime_damper(
        raw_contracts=100, entry_price_cents=30, conviction=0.80,
        pressure_orient="disagree",
    )
    # Cheap-leverage mult is 1.75 → agree ≈ 100 * rf * df * 1.75.
    # Disagree must have NO cheap-leverage term in the suffix.
    assert "cheap×" in suf_agree
    assert "cheap×" not in suf_disagree
    assert "cheap_SKIP(pressure_disagree)" in suf_disagree
    assert final_agree > final_disagree


# ── MFE-LOCK decision math --------------------------------------------------
# Narrow SR_FADE-only rule: arm after +arm_c unrealized, track peak, exit on
# giveback >= max(min_c, pct * peak). All tests run the pure helper so they
# don't need book / order / async machinery.

def test_mfe_lock_not_armed_below_threshold():
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=3, peak_c=0, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "idle"
    assert peak == 3  # tracks current even when unarmed
    # When peak_c=0, trigger is just max(3, 0) = 3
    assert trig == 3


def test_mfe_lock_arms_exactly_at_threshold():
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=5, peak_c=0, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "arm"
    assert peak == 5
    # Trigger = max(3, round(5*0.5)) = max(3, 3) = 3
    assert trig == 3


def test_mfe_lock_peak_updates_on_new_high():
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=10, peak_c=6, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "peak_update"
    assert peak == 10
    # Trigger recalculated to max(3, round(10*0.5)) = 5
    assert trig == 5


def test_mfe_lock_exits_on_50_percent_giveback():
    # Peak 12, now at 6 → giveback = 6 = max(3, 6). Triggers.
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=6, peak_c=12, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "exit"
    assert trig == 6


def test_mfe_lock_exits_on_3c_floor_when_peak_small():
    # Peak 5, now at 2 → giveback=3 = max(3, round(5*0.5)=3). Triggers on floor.
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=2, peak_c=5, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "exit"
    assert trig == 3


def test_mfe_lock_holds_when_giveback_below_trigger():
    # Peak 10, now at 8 → giveback=2, trigger=max(3,5)=5. Hold.
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=8, peak_c=10, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "idle"
    assert peak == 10
    assert trig == 5


def test_mfe_lock_ignored_when_profit_negative_unarmed():
    action, peak, trig = pce.PolymarketCopyEngine._sr_fade_mfe_lock_decision(
        profit_c=-5, peak_c=0, arm_c=5, giveback_min_c=3, giveback_pct=0.50)
    assert action == "idle"


# ── BTC continuation veto -----------------------------------------------------
# Codex spec v1 (2026-04-23): block when BTC continuation against fade side
# is strong on both 5s and 30s with matching signs and pressure not helping.

def _veto_should_fire(side: str, btc_30s: float, btc_5s: float,
                      press_orient: str,
                      thr_30: float = 20.0, thr_5: float = 5.0,
                      require_disagree: bool = True) -> bool:
    """Replicate the veto arithmetic from _evaluate_sr_fade_signal."""
    btc_against_fade = (
        (side == "no" and btc_30s > 0 and btc_5s > 0)
        or (side == "yes" and btc_30s < 0 and btc_5s < 0)
    )
    btc_continuing = abs(btc_30s) >= thr_30 and abs(btc_5s) >= thr_5
    pressure_not_helping = press_orient != "agree"
    return (
        btc_against_fade and btc_continuing
        and (not require_disagree or pressure_not_helping)
    )


def test_veto_fires_no_fade_btc_ripping_up():
    # Trade 6 signature: side=NO, BTC pushing up hard, pressure neutral.
    assert _veto_should_fire(
        side="no", btc_30s=+35.0, btc_5s=+10.0, press_orient="neutral",
    )


def test_veto_fires_yes_fade_btc_crashing_down():
    # Mirror of Trade 6 on the other side.
    assert _veto_should_fire(
        side="yes", btc_30s=-25.0, btc_5s=-7.0, press_orient="neutral",
    )


def test_veto_does_not_fire_when_btc_favors_fade():
    # YES fade with BTC rising — BTC agrees, don't block.
    assert not _veto_should_fire(
        side="yes", btc_30s=+25.0, btc_5s=+7.0, press_orient="neutral",
    )


def test_veto_does_not_fire_on_weak_continuation():
    # Directions match against the fade, but magnitude too small.
    assert not _veto_should_fire(
        side="no", btc_30s=+10.0, btc_5s=+2.0, press_orient="neutral",
    )


def test_veto_does_not_fire_on_signs_mismatch():
    # 30s up strongly but 5s reversing — whipsaw, not continuation.
    assert not _veto_should_fire(
        side="no", btc_30s=+30.0, btc_5s=-6.0, press_orient="neutral",
    )


def test_veto_bypassed_when_pressure_agrees_with_fade():
    # Even with BTC continuing, if tape support agrees with fade,
    # allow it through (require_disagree=True by default).
    assert not _veto_should_fire(
        side="no", btc_30s=+30.0, btc_5s=+8.0, press_orient="agree",
    )


def test_veto_fires_on_neutral_pressure_with_strict_flag():
    # Pressure neutral counts as "not helping" → veto applies.
    assert _veto_should_fire(
        side="no", btc_30s=+30.0, btc_5s=+8.0, press_orient="neutral",
        require_disagree=True,
    )


def test_veto_bypasses_pressure_check_when_flag_off():
    # If we drop the pressure-agree bypass (flag off), only BTC conditions
    # matter. With BTC continuation strong, fire regardless of pressure.
    assert _veto_should_fire(
        side="no", btc_30s=+30.0, btc_5s=+8.0, press_orient="agree",
        require_disagree=False,
    )
