"""Score a plethora of entry-filter rules against tonight's actual SCALP results.

Approach:
1. Parse every SCALP MARKET FILL in engine_history.log → (ts, side, skew, entry_px, ticker)
2. Pull all matching Kalshi settlements via API → outcome ($payout per $cost)
3. Compute per-trade PnL: rev - cost (treat each fill as 5ct standalone)
4. Score 25+ filter rules + combinations
5. Output ranked leaderboard

Run: ./venv/Scripts/python.exe scripts/score_scalp_filters.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_client import KalshiClient

LOG = Path("C:/Trading/btc-bias-engine/data/engine_history.log")

ENTRY_RE = re.compile(
    r"^(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .* SCALP MARKET FILL: "
    r"(YES|NO) (\d+)x @ (\d+)c IOC \(ask=(\d+)c\+\d+c\) "
    r"yes_ask=(\d+)c no_ask=(\d+)c \| ticker=(-?\w+-\w+)"
)


def _normalize_ticker(t: str) -> str:
    """Convert truncated '-26MAY090145-45' to full 'KXBTC15M-26MAY090145-45'."""
    if t.startswith("KXBTC15M-"):
        return t
    if t.startswith("-26MAY"):
        return "KXBTC15M" + t
    return t


def _parse_log() -> list[dict]:
    """Return list of entry dicts."""
    rows = []
    with LOG.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "SCALP MARKET FILL" not in line:
                continue
            m = ENTRY_RE.search(line)
            if not m:
                continue
            ts, side, ct, px, ask, yes_ask, no_ask, ticker = m.groups()
            t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
            yes = int(yes_ask)
            no = int(no_ask)
            rows.append({
                "ts": t,
                "ts_str": ts,
                "side": side.lower(),
                "count": int(ct),
                "entry_px": int(px),
                "yes_ask": yes,
                "no_ask": no,
                "skew": abs(yes - no),
                "favorite_side": "yes" if yes > no else ("no" if no > yes else "tie"),
                "ticker": _normalize_ticker(ticker),
                "session_minute": int(ts[14:16]) % 15,  # 0-14 within window
                "hour_pt": int(ts[11:13]) - 7,  # rough PT hour
            })
    return rows


async def _pull_settlements(tickers: list[str]) -> dict:
    """Return {ticker: settlement_dict} for the given tickers."""
    cred_path = "C:/Trading/btc-bias-engine/credentials/kalshi.env"
    with open(cred_path) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip()
    pem = open(os.environ["KALSHI_PRIVATE_KEY_PATH"]).read()
    out = {}
    async with KalshiClient(os.environ["KALSHI_API_KEY"], pem) as c:
        all_settlements = []
        cursor = ""
        for _ in range(20):  # paginate
            page, cursor = await c.get_settlements(limit=200, cursor=cursor)
            all_settlements.extend(page)
            if not cursor:
                break
        for s in all_settlements:
            tk = s.get("ticker", "")
            if tk in tickers:
                out[tk] = s
    return out


def _compute_pnl(entry: dict, settlement: dict | None) -> float | None:
    """Compute per-trade PnL based on settlement.

    Position cost = entry_px * count / 100
    Payout = count if settlement matches our side, else 0
    PnL = payout - cost
    """
    if settlement is None:
        return None
    result = settlement.get("market_result", "?")
    if result not in ("yes", "no"):
        return None
    cost = entry["entry_px"] * entry["count"] / 100.0
    payout = entry["count"] if result == entry["side"] else 0
    return payout - cost


# ─── Filter rules ─────────────────────────────────────────────────────


def _filter_skew_min(min_skew: int):
    return lambda e: e["skew"] >= min_skew


def _filter_side_only(side: str):
    return lambda e: e["side"] == side


def _filter_entry_range(low: int, high: int):
    """Only entries where entry price is in [low, high] cents."""
    return lambda e: low <= e["entry_px"] <= high


def _filter_session_minute(low: int, high: int):
    """Only entries fired in [low, high] minute of the 15-min window."""
    return lambda e: low <= e["session_minute"] <= high


def _filter_combine_all(*filters):
    return lambda e: all(f(e) for f in filters)


def _filter_combine_any(*filters):
    return lambda e: any(f(e) for f in filters)


# ─── Scoring ─────────────────────────────────────────────────────


def _score(name: str, entries_with_pnl: list[tuple], filt) -> dict:
    """Return scoring dict for a filter."""
    kept = [(e, pnl) for e, pnl in entries_with_pnl if filt(e)]
    skipped = [(e, pnl) for e, pnl in entries_with_pnl if not filt(e)]
    kept_pnl = sum(pnl for _, pnl in kept)
    skipped_pnl = sum(pnl for _, pnl in skipped)
    kept_wins = sum(1 for _, pnl in kept if pnl > 0)
    n_kept = len(kept)
    n_skipped = len(skipped)
    wr = (kept_wins / n_kept * 100) if n_kept > 0 else 0.0
    avg_pnl = (kept_pnl / n_kept) if n_kept > 0 else 0.0
    return {
        "name": name,
        "n_kept": n_kept,
        "n_skipped": n_skipped,
        "kept_pnl": kept_pnl,
        "skipped_pnl": skipped_pnl,
        "wr_kept": wr,
        "avg_pnl": avg_pnl,
        "delta_vs_no_filter": kept_pnl - (kept_pnl + skipped_pnl),
    }


# ─── Main ─────────────────────────────────────────────────────


async def main():
    entries = _parse_log()
    print(f"Parsed {len(entries)} SCALP MARKET FILL entries from log")
    if not entries:
        return

    tickers = list({e["ticker"] for e in entries})
    print(f"Pulling settlements for {len(tickers)} unique tickers...")
    settlements = await _pull_settlements(tickers)
    print(f"Got {len(settlements)} settlements")

    # Compute per-trade PnL
    rows = []
    for e in entries:
        s = settlements.get(e["ticker"])
        pnl = _compute_pnl(e, s)
        if pnl is not None:
            rows.append((e, pnl))

    print(f"Settled trades available: {len(rows)}")
    total_pnl = sum(pnl for _, pnl in rows)
    n_wins = sum(1 for _, pnl in rows if pnl > 0)
    n_loss = sum(1 for _, pnl in rows if pnl < 0)
    print(
        f"Baseline (no filter): n={len(rows)} wins={n_wins} losses={n_loss} "
        f"WR={n_wins/len(rows)*100:.1f}% total_pnl=${total_pnl:+.2f} "
        f"avg=${total_pnl/len(rows):+.2f}",
    )
    print()

    # ─── Build filter library ─────────────────────────────────────────
    filters = []

    # Skew thresholds
    for s in (5, 8, 10, 12, 15, 18, 20, 25, 30):
        filters.append((f"skew>={s}c", _filter_skew_min(s)))

    # Side only (compare yes-only vs no-only)
    filters.append(("side=YES only", _filter_side_only("yes")))
    filters.append(("side=NO only", _filter_side_only("no")))

    # Entry price ranges
    filters.append(("entry 50-60c", _filter_entry_range(50, 60)))
    filters.append(("entry 55-65c", _filter_entry_range(55, 65)))
    filters.append(("entry >=60c", _filter_entry_range(60, 100)))
    filters.append(("entry <=55c", _filter_entry_range(0, 55)))
    filters.append(("entry 60-70c", _filter_entry_range(60, 70)))

    # Skew + side combos
    for s in (10, 15, 20):
        filters.append((f"YES & skew>={s}c", _filter_combine_all(_filter_side_only("yes"), _filter_skew_min(s))))
        filters.append((f"NO  & skew>={s}c", _filter_combine_all(_filter_side_only("no"), _filter_skew_min(s))))

    # Skew + entry-range combos
    for s in (10, 15):
        for lo, hi in ((50, 60), (55, 65), (60, 70)):
            filters.append((
                f"skew>={s}c & entry {lo}-{hi}",
                _filter_combine_all(_filter_skew_min(s), _filter_entry_range(lo, hi)),
            ))

    # Session-minute (filter early-window noise)
    filters.append(("minute 0-2", _filter_session_minute(0, 2)))
    filters.append(("minute 3-14", _filter_session_minute(3, 14)))

    # ─── Score all ─────────────────────────────────────────────────────
    results = [_score(name, rows, filt) for name, filt in filters]
    # Add baseline
    baseline = _score("baseline (all entries)", rows, lambda e: True)
    results.insert(0, baseline)

    # Sort by total_pnl_kept descending
    results.sort(key=lambda r: r["kept_pnl"], reverse=True)

    print(f"{'rule':<35} {'n':<5} {'WR%':<5} {'avg':<8} {'total':<10} {'skip-PnL':<10}")
    print("-" * 90)
    for r in results:
        print(
            f"{r['name']:<35} {r['n_kept']:<5} "
            f"{r['wr_kept']:<5.0f} "
            f"${r['avg_pnl']:<+7.2f} "
            f"${r['kept_pnl']:<+9.2f} "
            f"${r['skipped_pnl']:<+9.2f}",
        )


if __name__ == "__main__":
    asyncio.run(main())
