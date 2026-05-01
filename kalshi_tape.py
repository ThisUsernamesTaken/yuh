"""Millisecond-resolution per-ticker trade tape.

Built 2026-04-30 to support entry-flow gating and stop-persistence checks.

Existing TradeFlowTracker in kalshi_ws.py provides 60s rolling stats but
doesn't slice into sub-windows or detect deceleration / absorption. This
module provides those richer queries on the same underlying trade stream.

Wired up in PolymarketCopyEngine startup: every WebSocket trade event is
recorded both into the existing TradeFlowTracker AND into the new tape.

Side semantics (Kalshi `taker_side`):
    "yes" trade  = aggressive YES taker (lifting a NO offer)
    "no"  trade  = aggressive NO  taker (hitting a YES bid)
Net YES pressure = yes_volume - no_volume in the window.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class KalshiTape:
    """Per-ticker rolling tape of trades + mid-price samples.

    Memory bounded by retention_s (default 120s) AND a hard per-ticker cap
    (default 5000 entries) so a flood doesn't grow unbounded.
    """

    def __init__(self, retention_s: float = 120.0, max_per_ticker: int = 5000):
        self._trades: Dict[str, deque] = defaultdict(deque)
        # Each entry: (ts_ms: int, side: str, count: int, price_cents: int)
        self._mids: Dict[str, deque] = defaultdict(deque)
        # Each entry: (ts_ms: int, mid_cents: int)
        self._retention_s = retention_s
        self._max_per_ticker = max_per_ticker

    # ── Ingest ──────────────────────────────────────────────────────────

    def record_trade(self, ticker: str, side: str, count: int,
                     price_cents: int, ts_ms: Optional[int] = None) -> None:
        """Record a single trade. Called from the WS on_trade callback."""
        if not ticker or count <= 0:
            return
        side_norm = (side or "").lower()
        if side_norm not in ("yes", "no"):
            return
        now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        dq = self._trades[ticker]
        dq.append((now_ms, side_norm, int(count), int(price_cents)))
        # Hard cap; prune stale by time too
        while len(dq) > self._max_per_ticker:
            dq.popleft()
        cutoff = now_ms - int(self._retention_s * 1000)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def record_mid(self, ticker: str, mid_cents: int,
                   ts_ms: Optional[int] = None) -> None:
        """Record a mid-price sample. Called when book mid updates."""
        if not ticker or mid_cents <= 0:
            return
        now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        dq = self._mids[ticker]
        # Dedup unchanged
        if dq and dq[-1][1] == int(mid_cents):
            return
        dq.append((now_ms, int(mid_cents)))
        # Hard cap
        while len(dq) > 1000:
            dq.popleft()
        cutoff = now_ms - int(self._retention_s * 1000)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def reset_ticker(self, ticker: str) -> None:
        """Drop tape for a single ticker. Called on window rotation."""
        self._trades.pop(ticker, None)
        self._mids.pop(ticker, None)

    # ── Queries ─────────────────────────────────────────────────────────

    def flow(self, ticker: str, window_s: float) -> dict:
        """Return aggregated flow stats over the last `window_s` seconds.

        Returns:
            {
                "window_s": ...,
                "yes_volume", "no_volume", "total_volume",
                "yes_trades", "no_trades", "trade_count",
                "yes_share": yes_volume / total (0.5 if no trades),
                "max_yes_print", "max_no_print",
                "first_ts_ms", "last_ts_ms",  (None if window empty)
            }
        """
        if window_s <= 0:
            return _empty_flow(window_s)
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(window_s * 1000)
        dq = self._trades.get(ticker)
        if not dq:
            return _empty_flow(window_s)

        yes_vol = no_vol = 0
        yes_trades = no_trades = 0
        max_yes_print = max_no_print = 0
        first_ts = last_ts = None

        for ts, side, count, _px in dq:
            if ts < cutoff:
                continue
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            if side == "yes":
                yes_vol += count
                yes_trades += 1
                if count > max_yes_print:
                    max_yes_print = count
            else:  # "no"
                no_vol += count
                no_trades += 1
                if count > max_no_print:
                    max_no_print = count

        total = yes_vol + no_vol
        return {
            "window_s": window_s,
            "yes_volume": yes_vol,
            "no_volume": no_vol,
            "total_volume": total,
            "yes_trades": yes_trades,
            "no_trades": no_trades,
            "trade_count": yes_trades + no_trades,
            "yes_share": (yes_vol / total) if total > 0 else 0.5,
            "max_yes_print": max_yes_print,
            "max_no_print": max_no_print,
            "first_ts_ms": first_ts,
            "last_ts_ms": last_ts,
        }

    def velocity_cps(self, ticker: str, window_s: float) -> float:
        """Cents-per-second mid-price change over the window.

        Positive = YES going up. Useful directionally (sign tells you which
        way price is moving) and in magnitude (how fast).
        """
        if window_s <= 0:
            return 0.0
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(window_s * 1000)
        dq = self._mids.get(ticker)
        if not dq or len(dq) < 2:
            return 0.0

        first = last = None
        for ts, mid in dq:
            if ts < cutoff:
                continue
            if first is None:
                first = (ts, mid)
            last = (ts, mid)

        if not first or not last or first[0] >= last[0]:
            return 0.0
        elapsed_s = (last[0] - first[0]) / 1000.0
        if elapsed_s <= 0:
            return 0.0
        return (last[1] - first[1]) / elapsed_s

    def is_decelerating(self, ticker: str, side: str,
                        recent_s: float = 3.0,
                        baseline_s: float = 10.0,
                        threshold: float = 0.6) -> bool:
        """True if the per-second volume rate for `side` over `recent_s` is
        below `threshold` × the baseline-window rate.

        Default 0.6 means "recent rate is at least 40% lower than baseline" —
        a meaningful slowdown. Used to detect exhaustion of one-sided flow
        before entering on the opposite side.
        """
        if baseline_s <= recent_s:
            return False
        recent = self.flow(ticker, recent_s)
        baseline = self.flow(ticker, baseline_s)
        side_key = f"{side}_volume" if side in ("yes", "no") else None
        if side_key is None:
            return False
        recent_rate = recent.get(side_key, 0) / max(recent_s, 0.001)
        baseline_rate = baseline.get(side_key, 0) / max(baseline_s, 0.001)
        if baseline_rate < 1.0:  # < 1ct/s baseline → no signal
            return False
        return recent_rate < (baseline_rate * threshold)

    def absorption_score(self, ticker: str, opposing_side: str,
                         window_s: float = 5.0) -> float:
        """Score 0.0-1.0 of how much `opposing_side` volume has been
        absorbed without moving the mid-price.

        High score = book is absorbing heavy adverse flow → likely the bid
        is real and a reversal may be forming. Used to defer stops in
        sticky-bid scenarios.

        For a YES position worried about NO selling pressure, pass
        opposing_side="no". For a NO position worried about YES taking,
        pass opposing_side="yes".
        """
        if window_s <= 0:
            return 0.0
        flow_data = self.flow(ticker, window_s)
        opposing_vol = flow_data.get(f"{opposing_side}_volume", 0)
        if opposing_vol < 5:
            return 0.0
        velocity_mag = abs(self.velocity_cps(ticker, window_s))
        # 50ct of opposing volume + 0c/sec velocity → 1.0 (full absorption)
        # 50ct of opposing volume + 5c/sec velocity → ~0.18 (knife)
        denom = max(velocity_mag, 0.1) + 0.5
        score = (opposing_vol / 50.0) / denom
        return min(1.0, max(0.0, score))

    def volume_at_or_below(self, ticker: str, side: str,
                           threshold_cents: int, window_s: float) -> int:
        """Count contracts of `side` traded at price ≤ threshold_cents in
        the window.

        Used by stop-persistence gate: bid touch alone isn't enough; we
        also require real volume confirming the move. This counts only
        trades on the relevant side at-or-below the trigger price.

        Note: each trade has a single price_cents, which Kalshi WS reports
        as the YES price for both yes-taker and no-taker trades. For a
        NO-side stop, threshold_cents should be expressed in NO price too
        (= 100 - YES_price). Caller's responsibility.
        """
        if window_s <= 0:
            return 0
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(window_s * 1000)
        dq = self._trades.get(ticker)
        if not dq:
            return 0
        total = 0
        side_norm = (side or "").lower()
        for ts, t_side, count, px in dq:
            if ts < cutoff:
                continue
            if t_side != side_norm:
                continue
            # For YES trades: px is YES price; threshold is YES price.
            # For NO  trades: px is YES price (Kalshi convention); convert
            #                  threshold from NO to YES for comparison.
            if t_side == "yes":
                if px <= threshold_cents:
                    total += count
            else:  # "no" — px is yes_price, NO price = 100 - px
                no_price = 100 - px
                if no_price <= threshold_cents:
                    total += count
        return total

    # ── Convenience for logging snapshots ───────────────────────────────

    def snapshot(self, ticker: str) -> dict:
        """Compact multi-window snapshot for gate-decision logging."""
        return {
            "f3":  self.flow(ticker, 3.0),
            "f5":  self.flow(ticker, 5.0),
            "f15": self.flow(ticker, 15.0),
            "f30": self.flow(ticker, 30.0),
            "v3":  round(self.velocity_cps(ticker, 3.0), 3),
            "v15": round(self.velocity_cps(ticker, 15.0), 3),
        }


def _empty_flow(window_s: float) -> dict:
    return {
        "window_s": window_s,
        "yes_volume": 0, "no_volume": 0, "total_volume": 0,
        "yes_trades": 0, "no_trades": 0, "trade_count": 0,
        "yes_share": 0.5,
        "max_yes_print": 0, "max_no_print": 0,
        "first_ts_ms": None, "last_ts_ms": None,
    }
