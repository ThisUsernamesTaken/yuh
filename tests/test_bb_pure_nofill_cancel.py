"""Tests for cancel-on-stale-entry (Phase 0.1.8).

Live test 2026-05-02 15:04:21 PT exposed the bug: BB_PURE fired with
post_only=False (taker) at the current ask, but by the time Kalshi
processed the order, the ask had moved. The unfilled portion rested
as a maker bid at the entry price and would have been filled minutes
later when conditions had changed (the "market went straight through
our bid twice" pattern from the user).

Fix: after BB_PURE NOFILL, schedule a one-shot async task that cancels
the order after BB_PURE_ENTRY_NOFILL_TIMEOUT_S seconds if it's still
resting.
"""
from __future__ import annotations

import asyncio
import polymarket_copy_engine as pce


class _FakeClient:
    def __init__(self, status: str, filled_count: int = 0):
        self.status = status
        self.filled_count = filled_count
        self.cancelled: list[str] = []

    async def get_order(self, oid):
        return _FakeOrder(oid, status=self.status, filled_count=self.filled_count)

    async def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True


class _FakeOrder:
    def __init__(self, oid, status="resting", filled_count=0):
        self.order_id = oid
        self.status = status
        self.filled_count = filled_count


def _make_engine(client):
    eng = pce.PolymarketCopyEngine.__new__(pce.PolymarketCopyEngine)
    eng._client = client
    return eng


def _set_uc(monkeypatch, **kwargs):
    orig = pce._uc

    def _patched(name, default=None):
        if name in kwargs:
            return kwargs[name]
        return orig(name, default)

    monkeypatch.setattr(pce, "_uc", _patched)


def test_cancels_when_still_resting(monkeypatch):
    """The actual bug: order still resting past timeout → cancel."""
    client = _FakeClient(status="resting", filled_count=0)
    eng = _make_engine(client)
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("ord-stale", "T", 26))
    assert client.cancelled == ["ord-stale"], (
        "stale resting BB_PURE entry must be cancelled"
    )


def test_does_not_cancel_when_filled(monkeypatch):
    """Late-fill: order filled during the timeout → leave it (RECLAIM handles)."""
    client = _FakeClient(status="filled", filled_count=42)
    eng = _make_engine(client)
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("ord-late-fill", "T", 26))
    assert client.cancelled == [], "filled order must NOT be cancelled"


def test_does_not_cancel_when_already_canceled(monkeypatch):
    """Order in terminal state already → nothing to do."""
    client = _FakeClient(status="canceled", filled_count=0)
    eng = _make_engine(client)
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("ord-already-canc", "T", 26))
    assert client.cancelled == []


def test_does_not_cancel_when_partially_filled(monkeypatch):
    """Partial fills stay — RECLAIM picks up the partial position."""
    client = _FakeClient(status="resting", filled_count=10)
    eng = _make_engine(client)
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("ord-partial", "T", 26))
    assert client.cancelled == [], (
        "partial fills must NOT be cancelled — partial position is real "
        "and protective_maintain will manage it"
    )


def test_handles_get_order_failure_gracefully(monkeypatch):
    """If get_order raises, log + return without crashing."""

    class _BrokenClient:
        async def get_order(self, oid):
            raise RuntimeError("Kalshi 500")

        async def cancel_order(self, oid):
            return True

    eng = _make_engine(_BrokenClient())
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    # Should not raise
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("ord-x", "T", 26))


def test_empty_order_id_is_noop(monkeypatch):
    """Defensive: empty order_id should not call get_order or cancel."""
    client = _FakeClient(status="resting", filled_count=0)
    eng = _make_engine(client)
    _set_uc(monkeypatch, BB_PURE_ENTRY_NOFILL_TIMEOUT_S=1.0)
    asyncio.run(eng._cancel_unfilled_bb_pure_entry("", "T", 26))
    assert client.cancelled == []
