"""Parse engine_history.log to extract MarketState timeline.

Produces a sequence of signal moments with:
  - Entry conditions (bid/ask/spread/fvg/pressure/velocity/etc.)
  - Post-entry bid trajectory (for TP fill simulation)
  - Final settlement outcome (from DECISION BACKFILL)
"""
import re
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict


@dataclass
class MarketState:
    """Snapshot at a single log moment."""
    ts: datetime
    side: str            # "yes" / "no"
    score: float
    conf: float
    persist: int
    imp: float
    book: float
    flow: float
    lag: float
    spread: int
    fvg: int
    prob: int
    secs_left: int

    # From associated SIZING line
    vol: float = 0.0     # annualized decimal (e.g., 0.35 = 35%)
    vel: float = 0.0     # BTC $/sec

    # Entry price (from MARKET ENTRY line if engine took this signal)
    actual_entry_price: Optional[int] = None
    actual_ct: Optional[int] = None


@dataclass
class SessionTimeline:
    """One 15-min contract session — the signals and outcomes in it."""
    ticker: str
    window_start_ts: Optional[datetime] = None
    baseline_price: int = 0
    strike_usd: float = 0.0

    # Signal events during the session
    signals: List[MarketState] = field(default_factory=list)

    # Bid/mid trajectory (from SIZING lines or pressure snapshots)
    trajectory: List[tuple] = field(default_factory=list)   # (ts, bid_est, mid_est)

    # Session outcome
    settled_result: Optional[str] = None  # "yes" or "no" from DECISION BACKFILL
    settled_ts: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════════════════
# Regex patterns for log parsing
# ═══════════════════════════════════════════════════════════════════════════
PRESSURE_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+CopyEngine PRESSURE ENTRY: (\w+) \| '
    r'score=([+-][\d.]+) conf=([\d.]+) persist=(\d+) \| '
    r'imp=([+-][\d.]+) book=([+-][\d.]+) flow=([+-][\d.]+) lag=([+-][\d.]+) \| '
    r'thresh=[\d.]+ spread=(\d+)c \| fvg=([+-]\d+)c .+ prob=(\d+)% \| (\d+)s left'
)
SIZING_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+CopyEngine SIZING: .+\| (\d+) contracts \| '
    r'vol=([\d.]+)% vel=\$([+-]?[\d.]+)/s .+'
)
MARKET_ENTRY_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+CopyEngine MARKET ENTRY: (\w+) @ (\d+)c \(bid=(\d+)c ask=(\d+)c'
)
FILL_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+TA_FORCED FILL: (\w+) KXBTC15M-([\d\w-]+) (\d+)x @ (\d+)c'
)
WINDOW_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+new Poly window — Bitcoin Up or Down.+\((\d+)s left\)'
)
BASELINE_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+CopyEngine BASELINE: (\d+)c from'
)
SETTLE_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+DECISION BACKFILL: (\S+) result=(\w+)'
)
STRIKE_PAT = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .+STRIKE CALIBRATED: \$([\d,]+\.?\d*)'
)


def parse_log(log_path: str = 'data/engine_history.log',
              max_bytes: int = 200_000_000,
              hours_back: int = 72) -> List[SessionTimeline]:
    """Parse log into session timelines."""
    sz = os.path.getsize(log_path)
    read = min(sz, max_bytes)
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        f.seek(max(0, sz - read))
        raw = f.read()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # First pass: collect all events chronologically
    sessions: Dict[str, SessionTimeline] = {}
    current_session: Optional[SessionTimeline] = None
    current_window_start_ts: Optional[datetime] = None
    last_pressure: Optional[MarketState] = None
    last_sizing_ct: Optional[int] = None
    last_sizing_vol: float = 0.0
    last_sizing_vel: float = 0.0

    def _ts(m): return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

    for line in raw.splitlines():
        m = WINDOW_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            current_window_start_ts = ts
            continue

        m = STRIKE_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            # Attach to next baseline/session
            # (strike logged right after window open; keep for later attachment)
            continue

        m = BASELINE_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            # Find/create session — need ticker. We don't have it in BASELINE line.
            # Use most recent MARKET ENTRY or wait for one.
            continue

        m = PRESSURE_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            last_pressure = MarketState(
                ts=ts, side=m.group(2).lower(),
                score=float(m.group(3)), conf=float(m.group(4)),
                persist=int(m.group(5)),
                imp=float(m.group(6)), book=float(m.group(7)),
                flow=float(m.group(8)), lag=float(m.group(9)),
                spread=int(m.group(10)), fvg=int(m.group(11)),
                prob=int(m.group(12)), secs_left=int(m.group(13)),
                vol=last_sizing_vol, vel=last_sizing_vel,
            )
            continue

        m = SIZING_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            last_sizing_ct = int(m.group(2))
            last_sizing_vol = float(m.group(3)) / 100.0
            last_sizing_vel = float(m.group(4))
            if last_pressure:
                last_pressure.vol = last_sizing_vol
                last_pressure.vel = last_sizing_vel
            continue

        m = MARKET_ENTRY_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            if last_pressure and (ts - last_pressure.ts).total_seconds() < 5:
                last_pressure.actual_entry_price = int(m.group(3))
                last_pressure.actual_ct = last_sizing_ct
            continue

        m = FILL_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            ticker_suffix = m.group(3)
            ticker = f"KXBTC15M-{ticker_suffix}"
            if ticker not in sessions:
                sessions[ticker] = SessionTimeline(
                    ticker=ticker, window_start_ts=current_window_start_ts,
                    baseline_price=0, signals=[], trajectory=[]
                )
            sess = sessions[ticker]
            if last_pressure and (ts - last_pressure.ts).total_seconds() < 5:
                last_pressure.actual_entry_price = int(m.group(5))
                last_pressure.actual_ct = int(m.group(4))
                sess.signals.append(last_pressure)
                last_pressure = None
            continue

        m = SETTLE_PAT.search(line)
        if m:
            ts = _ts(m)
            if ts < cutoff:
                continue
            key_suffix = m.group(2)
            # key_suffix = "APR141200-00" — match to ticker
            for ticker, sess in sessions.items():
                if key_suffix in ticker:
                    sess.settled_result = m.group(3).lower()
                    sess.settled_ts = ts
                    break
            continue

    return list(sessions.values())


if __name__ == '__main__':
    # Quick smoke test
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sessions = parse_log()
    print(f"Parsed {len(sessions)} sessions")
    with_fill = [s for s in sessions if s.signals and s.signals[0].actual_entry_price]
    with_outcome = [s for s in with_fill if s.settled_result]
    print(f"  With engine entries: {len(with_fill)}")
    print(f"  With known outcomes: {len(with_outcome)}")
    for s in with_outcome[:5]:
        sig = s.signals[0]
        print(f"  {s.ticker} | {sig.side} {sig.actual_ct}ct @{sig.actual_entry_price}c "
              f"→ settled {s.settled_result}")
