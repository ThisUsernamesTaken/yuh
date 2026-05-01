from __future__ import annotations

import sqlite3

from paper_engine.codex_snapback import (
    Params,
    Snap,
    _choose_entry,
    _simulate_exit,
)


def _snap(t: int, btc: float, yes_mid: int, ps: float = 0.0) -> Snap:
    return Snap(
        ticker="KXBTC15M-TEST",
        t_offset_sec=t,
        ts_ms=t * 1000,
        btc_price=btc,
        yes_mid=yes_mid,
        pressure_score=ps,
        pressure_confidence=1.0,
        regime="chop",
    )


def test_choose_entry_waits_for_btc_stall_and_mid_band():
    snaps = [
        _snap(0, 100000.0, 50),
        _snap(60, 100020.0, 55),
        _snap(120, 100052.0, 61),  # NO side = 39, but BTC still accelerating.
        _snap(180, 100060.0, 59),  # NO side = 41, BTC stalled enough.
    ]

    choice = _choose_entry(snaps, Params())

    assert choice is not None
    entry_i, side, entry, btc_move = choice
    assert entry_i == 3
    assert side == "no"
    assert entry == 41
    assert btc_move == 60.0


def test_simulate_exit_captures_fixed_target_conservatively():
    snaps = [
        _snap(0, 100000.0, 50),
        _snap(60, 100030.0, 59),   # entry NO at 41
        _snap(120, 100020.0, 50),  # NO side mid 50, executable 49
    ]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settlement_ledger (ticker TEXT, market_result TEXT)")

    exit_i, exit_px, reason, mfe, mae = _simulate_exit(
        conn, snaps, entry_i=1, side="no", entry=41, p=Params(),
    )

    assert exit_i == 2
    assert exit_px == 45  # fixed +4c target, not full sampled spike.
    assert reason == "target_capture"
    assert mfe == 8
    assert mae == 8


def test_simulate_exit_can_apply_hard_stop_before_target():
    snaps = [
        _snap(0, 100000.0, 50),
        _snap(60, 100030.0, 59),   # entry NO at 41
        _snap(120, 100045.0, 66),  # NO side mid 34, executable 33
        _snap(180, 100020.0, 50),
    ]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settlement_ledger (ticker TEXT, market_result TEXT)")

    exit_i, exit_px, reason, mfe, mae = _simulate_exit(
        conn, snaps, entry_i=1, side="no", entry=41,
        p=Params(stop_loss_cents=5),
    )

    assert exit_i == 2
    assert exit_px == 36
    assert reason == "stop_loss"
    assert mae <= -5
