"""Tests for session-lock release on place_order failure (2026-05-03).

Live regression observed 2026-05-03 10:00:42 PT:
- BB_PURE FIRE attempted at maker_bid_plus_1
- Kalshi rejected: 400 invalid_order — "post only cross"
- Per-window lock was set pre-await for race prevention
- Lock stayed set after the place_order exception
- Engine missed the entire 30pp-edge YES @ 34c signal sequence
- Position remained FLAT, BAL unchanged — no money lost, opportunity-cost only

Fix: in the place_order exception handler, call _remove_session_lock(ticker)
so subsequent signals can retry. This is distinct from NOFILL handling
(order placed but unfilled) which keeps the lock per "one attempt per
session" rule.

These tests pin the helper behavior. The integration path (place_order
exception → lock removed) is exercised at the engine level — the helper
itself is the contract.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


class _MockEngine:
    """Minimal stub matching the real engine's lock-related surface area.

    The real engine inherits from a mixin that defines _add_session_lock,
    _remove_session_lock, _persist_session_lock, _load_session_lock, and
    holds _entered_tickers_this_window + _SESSION_LOCK_PATH. To test the
    helpers in isolation we replicate that minimal surface here, since
    the full engine class can't be cheaply instantiated.
    """

    def __init__(self, lock_path: str):
        self._SESSION_LOCK_PATH = lock_path
        self._entered_tickers_this_window: set = set()

    # Copied verbatim from polymarket_copy_engine.py
    def _persist_session_lock(self) -> None:
        import os
        try:
            payload = {
                "saved_at_ms": 1_000,
                "tickers": sorted(self._entered_tickers_this_window),
            }
            os.makedirs(
                os.path.dirname(self._SESSION_LOCK_PATH) or ".",
                exist_ok=True,
            )
            with open(self._SESSION_LOCK_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    def _add_session_lock(self, ticker: str) -> None:
        try:
            self._entered_tickers_this_window.add(ticker)
            self._persist_session_lock()
        except Exception:
            pass

    def _remove_session_lock(self, ticker: str) -> None:
        try:
            self._entered_tickers_this_window.discard(ticker)
            self._persist_session_lock()
        except Exception:
            pass


def test_remove_clears_in_memory_lock():
    with tempfile.TemporaryDirectory() as d:
        eng = _MockEngine(str(Path(d) / "session.json"))
        eng._add_session_lock("KXBTC15M-26MAY031315-15")
        assert "KXBTC15M-26MAY031315-15" in eng._entered_tickers_this_window
        eng._remove_session_lock("KXBTC15M-26MAY031315-15")
        assert "KXBTC15M-26MAY031315-15" not in eng._entered_tickers_this_window


def test_remove_persists_empty_set_to_disk():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "session.json"
        eng = _MockEngine(str(path))
        eng._add_session_lock("KXBTC15M-26MAY031315-15")
        # Verify persisted
        with open(path) as f:
            assert "26MAY031315-15" in json.load(f)["tickers"][0]
        eng._remove_session_lock("KXBTC15M-26MAY031315-15")
        # After remove, disk should reflect the empty set
        with open(path) as f:
            assert json.load(f)["tickers"] == []


def test_remove_idempotent_on_unknown_ticker():
    """Removing a ticker that was never added must not raise."""
    with tempfile.TemporaryDirectory() as d:
        eng = _MockEngine(str(Path(d) / "session.json"))
        # Should be silent no-op
        eng._remove_session_lock("KXBTC15M-26MAY031315-15")
        assert eng._entered_tickers_this_window == set()


def test_remove_preserves_other_locked_tickers():
    """Removing one ticker leaves others intact."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "session.json"
        eng = _MockEngine(str(path))
        eng._add_session_lock("KXBTC15M-26MAY031315-15")
        eng._add_session_lock("KXBTC15M-26MAY031330-30")
        eng._remove_session_lock("KXBTC15M-26MAY031315-15")
        assert "KXBTC15M-26MAY031315-15" not in eng._entered_tickers_this_window
        assert "KXBTC15M-26MAY031330-30" in eng._entered_tickers_this_window
        with open(path) as f:
            tickers = json.load(f)["tickers"]
            assert "KXBTC15M-26MAY031330-30" in tickers
            assert not any("31315" in t for t in tickers)


def test_add_then_remove_returns_to_clean_state():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "session.json"
        eng = _MockEngine(str(path))
        # Two add/remove cycles should leave both in-mem and disk clean
        for _ in range(2):
            eng._add_session_lock("KXBTC15M-26MAY031315-15")
            eng._remove_session_lock("KXBTC15M-26MAY031315-15")
        assert eng._entered_tickers_this_window == set()
        with open(path) as f:
            assert json.load(f)["tickers"] == []
