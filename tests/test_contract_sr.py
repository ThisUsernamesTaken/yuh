"""Unit tests for contract_sr.py — S/R detector behavior on synthetic tracks."""
from contract_sr import (
    ContractSRState, LevelEvidence, update, level_strength,
    nearest_support, nearest_resistance,
    snapshot, seed_from,
)


def _feed(state: ContractSRState, prices: list[int]) -> None:
    for p in prices:
        update(state, p)


def test_single_visit_records_samples_not_dwell():
    """One sample at a price doesn't close a dwell (it's just the start)."""
    s = ContractSRState(ticker="T")
    _feed(s, [40])
    # Samples_at counts the visit, but dwells only registers after we leave.
    assert s.levels[40].samples_at >= 1
    assert s.levels[40].dwells == 0


def test_dwell_registered_on_departure():
    """Visit 40, leave to 45 → dwell at 40 is registered."""
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45])
    assert s.levels[40].dwells == 1


def test_long_dwell_counts_as_one_dwell_with_depth():
    """Five samples at 40 then leave → 1 dwell, high samples_at."""
    s = ContractSRState(ticker="T")
    _feed(s, [40, 40, 40, 40, 40, 45])
    lv = s.levels[40]
    assert lv.dwells == 1
    assert lv.samples_at >= 5


def test_repeat_dwells_stack():
    """Bounce 40 → 45 → 40 → 45 → 40 → 45 — three dwells at 40."""
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45, 40, 45, 40, 45])
    lv = s.levels[40]
    assert lv.dwells >= 3


def test_bounce_from_above_registered():
    """Price goes 40→45→40 — returning to 40 from above."""
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45, 40])
    lv = s.levels[40]
    assert lv.last_bounced_from_above >= 1


def test_strength_grows_with_repeated_dwells():
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45, 40, 45, 40, 45, 40])
    assert level_strength(s, 40) > 0.3


def test_bounce_quality_boosts_defended_level_strength():
    defended = ContractSRState(ticker="T")
    broken = ContractSRState(ticker="T")
    for state in (defended, broken):
        lv = state.levels.setdefault(40, LevelEvidence())
        lv.dwells = 4
        lv.samples_at = 20
        lv.last_seen_sample = 10
        state.samples_seen = 10

    defended.levels[40].last_bounced_from_above = 3

    assert level_strength(defended, 40) > level_strength(broken, 40)


def test_broken_level_no_longer_scores_like_defended_level():
    s = ContractSRState(ticker="T")
    lv = s.levels.setdefault(50, LevelEvidence())
    lv.dwells = 5
    lv.samples_at = 20
    lv.last_seen_sample = 100
    s.samples_seen = 100

    no_bounce_strength = level_strength(s, 50)
    lv.last_bounced_from_below = 5
    defended_strength = level_strength(s, 50)

    assert no_bounce_strength == 0.8
    assert defended_strength == 1.0


def test_nearest_support_picks_strongest_below_mid():
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45, 40, 45, 40, 45, 40])
    _feed(s, [50])
    r = nearest_support(s, current_mid=50, min_strength=0.2)
    assert r is not None
    assert r[0] == 40


def test_nearest_resistance_picks_strongest_above_mid():
    s = ContractSRState(ticker="T")
    _feed(s, [60, 55, 60, 55, 60, 55, 60])
    _feed(s, [50])
    r = nearest_resistance(s, current_mid=50, min_strength=0.2)
    assert r is not None
    assert r[0] == 60


def test_strength_threshold_filters_weak_levels():
    """A level with only 1 dwell shouldn't meet 0.8 strength bar."""
    s = ContractSRState(ticker="T")
    _feed(s, [40, 45])
    r = nearest_support(s, current_mid=50, min_strength=0.8)
    assert r is None


def test_degenerate_prices_ignored():
    s = ContractSRState(ticker="T")
    _feed(s, [0, 100, -5, 150])
    assert s.levels == {}


def test_snapshot_and_seed_roundtrip():
    s1 = ContractSRState(ticker="T")
    _feed(s1, [45, 48, 45, 48, 45, 48, 45])
    snap = snapshot(s1)
    assert len(snap) > 0
    assert any(row["price"] == 45 for row in snap)

    s2 = ContractSRState(ticker="T")
    seed_from(s2, snap, decay=0.5)
    assert 45 in s2.levels
    assert s2.levels[45].dwells >= 1


def test_seed_decay_halves_dwells():
    s1 = ContractSRState(ticker="T")
    _feed(s1, [45, 48, 45, 48, 45, 48, 45])
    orig_dwells = s1.levels[45].dwells
    snap = snapshot(s1)
    s2 = ContractSRState(ticker="T")
    seed_from(s2, snap, decay=0.5)
    assert s2.levels[45].dwells <= orig_dwells
    assert s2.levels[45].dwells >= int(orig_dwells * 0.5) - 1
