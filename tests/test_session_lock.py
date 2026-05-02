"""Tests for the per-window ticker-lock persistence (Phase 0.1.2).

Live test 2026-05-02 01:35 PT exposed the failure mode: an engine
restart cleared `_entered_tickers_this_window`, which let a 3rd entry
fire on a ticker the engine had already exited within the same
15-min window. Lock now persists to data/session_state.json with a
saved_at timestamp; on startup we restore tickers if the file is < 15
min old.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import polymarket_copy_engine as pcm


class _Probe(pcm.PolymarketCopyEngine):
    """Subclass that overrides the persistence path to a temp file."""

    def __init__(self, tmp_path: Path):
        # Bypass the real __init__ so we don't need a Kalshi client
        self._SESSION_LOCK_PATH = str(tmp_path)
        self._SESSION_LOCK_MAX_AGE_S = 900
        self._entered_tickers_this_window = self._load_session_lock()


def test_load_returns_empty_when_no_file(tmp_path):
    p = tmp_path / "missing.json"
    eng = _Probe(p)
    assert eng._entered_tickers_this_window == set()


def test_persist_then_load_round_trip(tmp_path):
    p = tmp_path / "lock.json"
    eng = _Probe(p)
    eng._add_session_lock("KXBTC15M-T1")
    eng._add_session_lock("KXBTC15M-T2")
    # New "engine restart" — same path, fresh subclass instance
    eng2 = _Probe(p)
    assert eng2._entered_tickers_this_window == {"KXBTC15M-T1", "KXBTC15M-T2"}


def test_lock_expires_after_window(tmp_path):
    """A saved_at older than 15 min must NOT be restored — window has rolled."""
    p = tmp_path / "expired.json"
    stale_ts_ms = int((time.time() - 1200) * 1000)  # 20 min ago
    p.write_text(
        json.dumps({"saved_at_ms": stale_ts_ms, "tickers": ["KXBTC15M-OLD"]}),
        encoding="utf-8",
    )
    eng = _Probe(p)
    assert eng._entered_tickers_this_window == set()


def test_lock_within_window_is_restored(tmp_path):
    """A saved_at within 15 min must be restored."""
    p = tmp_path / "fresh.json"
    fresh_ts_ms = int((time.time() - 60) * 1000)  # 1 min ago
    p.write_text(
        json.dumps({"saved_at_ms": fresh_ts_ms, "tickers": ["KXBTC15M-FRESH"]}),
        encoding="utf-8",
    )
    eng = _Probe(p)
    assert eng._entered_tickers_this_window == {"KXBTC15M-FRESH"}


def test_corrupt_file_returns_empty(tmp_path):
    """Corrupt JSON should not crash startup; empty set is the safe default."""
    p = tmp_path / "corrupt.json"
    p.write_text("not-json-at-all", encoding="utf-8")
    eng = _Probe(p)
    assert eng._entered_tickers_this_window == set()


def test_persist_writes_timestamp(tmp_path):
    p = tmp_path / "timestamp.json"
    eng = _Probe(p)
    eng._add_session_lock("KXBTC15M-X")
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert "saved_at_ms" in saved
    assert "tickers" in saved
    assert saved["tickers"] == ["KXBTC15M-X"]
    # Saved-at should be very recent (within 5s)
    assert (time.time() * 1000 - saved["saved_at_ms"]) < 5000


def test_persist_round_trip_simulates_restart_within_window(tmp_path):
    """The actual bug from 2026-05-02 01:35 PT:
    1. Engine entered ticker T at time t0
    2. Engine restarted at t0+90s (still within 15 min window)
    3. Old lock state was lost, allowing re-entry on T

    With persistence, the second instance must see T in the lock.
    """
    p = tmp_path / "scenario.json"
    eng1 = _Probe(p)
    eng1._add_session_lock("KXBTC15M-26MAY020445-45")
    # Simulate restart 90s later — same path
    time.sleep(0.01)  # ensure timestamp differs
    eng2 = _Probe(p)
    assert "KXBTC15M-26MAY020445-45" in eng2._entered_tickers_this_window, (
        "restart within window must restore lock — otherwise re-entry will fire"
    )
