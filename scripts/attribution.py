# scripts/attribution.py — Phase 1 post-hoc attribution cross-tabs
# ============================================================================
# Reads `data/trades.db` and prints six cross-tabs for the 72-hour observation
# window. All cuts key off the JSON blob stored in `kalshi_trades.ladder_detail`
# which is written at entry by `polymarket_copy_engine.py` (tags: regime,
# drawdown_state, raw_edge_pp, dominant_margin, pressure_confidence, etc.).
#
# PURPOSE: answer "where is PnL actually coming from?" without guessing.
#
# NOTES
# -----
# * PnL source: `COALESCE(settlement_ledger.pnl_cents/100.0, kalshi_trades.pnl)`.
#   `settlement_ledger` is authoritative truth (Kalshi-reported settlement);
#   `kalshi_trades.pnl` can drift (see CLAUDE.md warning). We prefer ledger truth
#   and fall back to kt.pnl only when the ticker has no settlement row yet
#   (unsettled / still in-flight). A top-of-report diagnostic line flags rows
#   where the two disagree by >=$0.01.
# * Trades without a tagged `ladder_detail` (pre-2026-04-21 history) are
#   dropped from regime/edge cuts with a warning count at the top.
# * No arguments required; pass `--since YYYY-MM-DD` to restrict.
# ============================================================================
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class TradeRow:
    rowid: int
    order_id: str
    placed_at: str
    ticker: str
    side: str
    limit_price: int
    filled_count: int
    pnl: float                       # EFFECTIVE PnL used by cross-tabs. For canonical rows:
                                     #   settled → sl.pnl_cents/100
                                     #   unsettled → sum(kt.pnl) across all rows sharing this ticker
                                     # For non-canonical (trail_ratchet rollup) rows: 0.0
    kt_pnl: float                    # raw kalshi_trades.pnl (for drift diagnostic)
    sl_pnl: Optional[float]          # settlement_ledger.pnl_cents/100.0 (None if unsettled)
    settled: bool                    # True iff sl.pnl_cents was non-NULL
    status: str
    strategy_name: str
    mtf_regime: Optional[str]
    # Attribution tags (may be None for pre-2026-04-21 rows)
    regime: Optional[str] = None
    regime_vol_bps: Optional[float] = None
    regime_consistency: Optional[float] = None
    drawdown_state: Optional[str] = None
    raw_edge_pp: Optional[float] = None
    dominant_margin: Optional[float] = None
    pressure_confidence: Optional[float] = None
    pressure_score: Optional[float] = None
    # Derived
    mae_cents: Optional[int] = None
    mfe_cents: Optional[int] = None
    minutes_into_window: Optional[float] = None
    # Dedup flag. One canonical row per ticker; rollups set to False.
    canonical: bool = True


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _bucket_edge(pp: Optional[float]) -> str:
    if pp is None:
        return "unk"
    if pp < 0:
        return "<0pp (NEG)"
    if pp < 3.0:
        return "0-3pp"
    if pp < 6.0:
        return "3-6pp"
    if pp < 10.0:
        return "6-10pp"
    return "10+pp"


def _bucket_price(p: int) -> str:
    if p < 45:
        return "<45c"
    if p < 55:
        return "45-54c"
    if p < 65:
        return "55-64c"
    if p < 75:
        return "65-74c"
    return "75+c"


def _bucket_hold(mins: Optional[float]) -> str:
    if mins is None:
        return "unk"
    # "minutes_into_window" — higher = entered later in the 15-min window =
    # shorter time held. So hold_time = 15 - mins_into.
    hold = 15.0 - mins
    if hold < 3.0:
        return "<3min hold"
    if hold < 6.0:
        return "3-6min hold"
    if hold < 9.0:
        return "6-9min hold"
    if hold < 12.0:
        return "9-12min hold"
    return "12-15min hold"


def _bucket_confidence(c: Optional[float]) -> str:
    if c is None:
        return "unk"
    if c < 0.40:
        return "<0.40 (weak)"
    if c < 0.55:
        return "0.40-0.54"
    if c < 0.70:
        return "0.55-0.69"
    if c < 0.85:
        return "0.70-0.84"
    return "0.85+ (max)"


def _parse_minutes_into_window(placed_at: str) -> Optional[float]:
    """KXBTC15M opens at :00/:15/:30/:45 UTC. Minutes into window =
    minute_of_hour mod 15 plus the seconds fraction."""
    try:
        # Normalize trailing Z
        s = placed_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        minute = dt.minute % 15
        return minute + dt.second / 60.0
    except Exception:
        return None


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────────
# Data load
# ──────────────────────────────────────────────────────────────────────────
def _load_trades(db_path: str, since: Optional[str]) -> List[TradeRow]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    where_clauses = ["status NOT IN ('pending','unfilled')"]
    params: List = []
    if since:
        where_clauses.append("DATE(placed_at) >= ?")
        params.append(since)

    # Prefer settlement_ledger as PnL source. One settlement row per ticker;
    # LEFT JOIN so unsettled/in-flight trades still surface (they'll fall back
    # to kt.pnl). Aggregate defensively via MAX() in case the ledger ever
    # contains duplicate ticker rows.
    sql = f"""
        SELECT kt.id as rowid, kt.order_id, kt.placed_at, kt.ticker, kt.side,
               kt.limit_price, kt.filled_count, kt.pnl as kt_pnl, kt.status,
               kt.strategy_name, kt.mtf_regime, kt.ladder_detail,
               te.mae_cents, te.mfe_cents,
               sl.pnl_cents as sl_pnl_cents
        FROM kalshi_trades kt
        LEFT JOIN trade_excursions te ON te.trade_rowid = kt.id
        LEFT JOIN (
            SELECT ticker, MAX(pnl_cents) AS pnl_cents
            FROM settlement_ledger
            GROUP BY ticker
        ) sl ON sl.ticker = kt.ticker
        WHERE {' AND '.join(where_clauses)}
        ORDER BY kt.placed_at
    """

    rows: List[TradeRow] = []
    for r in conn.execute(sql, params):
        tags: Dict = {}
        raw = r["ladder_detail"]
        if raw:
            try:
                tags = json.loads(raw)
            except Exception:
                tags = {}

        kt_pnl = float(r["kt_pnl"] or 0.0)
        sl_cents = r["sl_pnl_cents"]
        if sl_cents is not None:
            sl_pnl = float(sl_cents) / 100.0
            effective_pnl = sl_pnl
            settled = True
        else:
            sl_pnl = None
            effective_pnl = kt_pnl
            settled = False

        rows.append(TradeRow(
            rowid=r["rowid"],
            order_id=r["order_id"] or "",
            placed_at=r["placed_at"] or "",
            ticker=r["ticker"] or "",
            side=r["side"] or "",
            limit_price=int(r["limit_price"] or 0),
            filled_count=int(r["filled_count"] or 0),
            pnl=effective_pnl,
            kt_pnl=kt_pnl,
            sl_pnl=sl_pnl,
            settled=settled,
            status=r["status"] or "",
            strategy_name=r["strategy_name"] or "",
            mtf_regime=r["mtf_regime"],
            regime=tags.get("regime"),
            regime_vol_bps=_coerce_float(tags.get("regime_vol_bps")),
            regime_consistency=_coerce_float(tags.get("regime_consistency")),
            drawdown_state=tags.get("drawdown_state"),
            raw_edge_pp=_coerce_float(tags.get("raw_edge_pp")),
            dominant_margin=_coerce_float(tags.get("dominant_margin")),
            pressure_confidence=_coerce_float(tags.get("pressure_confidence")),
            pressure_score=_coerce_float(tags.get("pressure_score")),
            mae_cents=r["mae_cents"],
            mfe_cents=r["mfe_cents"],
            minutes_into_window=_parse_minutes_into_window(r["placed_at"] or ""),
        ))

    conn.close()

    # Canonical-row dedup: kalshi_trades can carry multiple rows per ticker
    # when trail_ratchet rolls one order into another (the zero-fill rollup
    # rows carry non-zero pnl that sums with the filled row to the true total).
    # A naive LEFT JOIN on settlement_ledger would then apply the same
    # settlement PnL to every row, double-counting.
    #
    # Per ticker, pick ONE canonical row (max filled_count, tiebreak earliest
    # placed_at). Canonical row gets:
    #   - settled  → sl.pnl_cents/100 (authoritative truth, attributed once)
    #   - unsettled → sum(kt.pnl) across the group (total position PnL)
    # Non-canonical rows get pnl=0 and canonical=False so downstream cross-tabs
    # that filter on canonical exclude them.
    groups: Dict[str, List[TradeRow]] = defaultdict(list)
    for r in rows:
        groups[r.ticker].append(r)

    for ticker, group in groups.items():
        if len(group) == 1:
            group[0].canonical = True
            continue
        # Pick canonical: max filled_count, tiebreak earliest placed_at
        canonical = max(
            group,
            key=lambda r: (r.filled_count, -(int.from_bytes(
                (r.placed_at or "").encode("utf-8")[:8].ljust(8, b"\xff"),
                "big", signed=False,
            ))),
        )
        group_sl_pnl = next(
            (r.sl_pnl for r in group if r.sl_pnl is not None), None
        )
        group_kt_sum = sum(r.kt_pnl for r in group)
        for r in group:
            if r is canonical:
                r.canonical = True
                if group_sl_pnl is not None:
                    r.pnl = group_sl_pnl
                    r.sl_pnl = group_sl_pnl
                    r.settled = True
                else:
                    r.pnl = group_kt_sum
                    r.settled = False
            else:
                r.canonical = False
                r.pnl = 0.0

    return rows


# ──────────────────────────────────────────────────────────────────────────
# Cross-tab engine
# ──────────────────────────────────────────────────────────────────────────
def _tabulate(
    rows: Iterable[TradeRow],
    key_fn,
    metric_fns: Dict[str, callable],
    order: Optional[List[str]] = None,
) -> Tuple[List[str], List[Dict[str, float]]]:
    buckets: Dict[str, List[TradeRow]] = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(r)

    headers = ["bucket", "n"] + list(metric_fns.keys())

    def _sort_key(k: str):
        if order and k in order:
            return (0, order.index(k))
        return (1, k)

    out_rows = []
    for bucket in sorted(buckets, key=_sort_key):
        group = buckets[bucket]
        row: Dict[str, float] = {"bucket": bucket, "n": len(group)}
        for name, fn in metric_fns.items():
            row[name] = fn(group)
        out_rows.append(row)

    return headers, out_rows


# Metric functions ----------------------------------------------------------
def _pnl(group: List[TradeRow]) -> float:
    return sum(r.pnl for r in group)


def _avg_pnl(group: List[TradeRow]) -> float:
    return _pnl(group) / len(group) if group else 0.0


def _wr(group: List[TradeRow]) -> float:
    wins = sum(1 for r in group if r.pnl > 0)
    return wins / len(group) * 100.0 if group else 0.0


def _avg_mae(group: List[TradeRow]) -> float:
    vals = [r.mae_cents for r in group if r.mae_cents is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _avg_mfe(group: List[TradeRow]) -> float:
    vals = [r.mfe_cents for r in group if r.mfe_cents is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _mfe_mae_ratio(group: List[TradeRow]) -> float:
    mae = _avg_mae(group)
    mfe = _avg_mfe(group)
    if mae == 0:
        return 0.0
    return mfe / abs(mae)


# Print helpers -------------------------------------------------------------
def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:+.2f}"
    return str(v)


def _print_table(title: str, headers: List[str], rows: List[Dict]) -> None:
    print(f"\n-- {title} " + "-" * max(1, 60 - len(title)))
    if not rows:
        print("  (no data)")
        return
    # Column widths
    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            widths[h] = max(widths[h], len(_fmt(r.get(h, ""))))
    line = "  " + "  ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("  " + "  ".join("-" * widths[h] for h in headers))
    for r in rows:
        print("  " + "  ".join(_fmt(r.get(h, "")).ljust(widths[h]) for h in headers))


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 1 post-hoc attribution cross-tabs for BTC Bias Engine."
    )
    ap.add_argument(
        "--db",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "trades.db"),
        help="Path to trades.db (default: ../data/trades.db)",
    )
    ap.add_argument(
        "--since",
        default=None,
        help="Restrict to trades placed on or after this UTC date (YYYY-MM-DD)",
    )
    args = ap.parse_args(argv)

    db_path = os.path.normpath(args.db)
    if not os.path.exists(db_path):
        print(f"ERR: db not found at {db_path}", file=sys.stderr)
        return 2

    all_rows = _load_trades(db_path, args.since)
    # Use canonical rows only for aggregation (one per ticker). Non-canonical
    # rows are trail_ratchet rollup artifacts with their pnl zeroed out.
    rows = [r for r in all_rows if r.canonical]
    rollup_n = len(all_rows) - len(rows)
    tagged = [r for r in rows if r.regime]
    untagged_n = len(rows) - len(tagged)

    # Drift diagnostic: per-ticker kt-sum vs sl.pnl disagreement.
    # Compare sum(kt.pnl) across the whole group vs sl.pnl_cents/100.
    by_ticker: Dict[str, List[TradeRow]] = defaultdict(list)
    for r in all_rows:
        by_ticker[r.ticker].append(r)
    drift_ticker_rows: List[TradeRow] = []
    drift_net = 0.0
    for ticker, group in by_ticker.items():
        canonical = next((g for g in group if g.canonical), group[0])
        if not canonical.settled or canonical.sl_pnl is None:
            continue
        kt_group_sum = sum(g.kt_pnl for g in group)
        diff = kt_group_sum - canonical.sl_pnl
        if abs(diff) >= 0.01:
            canonical._kt_group_sum = kt_group_sum  # type: ignore[attr-defined]
            canonical._drift = diff                  # type: ignore[attr-defined]
            drift_ticker_rows.append(canonical)
            drift_net += diff
    unsettled_n = sum(1 for r in rows if not r.settled)

    print("=" * 68)
    print(" PHASE 1 ATTRIBUTION -- BTC Bias Engine")
    print("=" * 68)
    print(f" db            : {db_path}")
    print(f" since         : {args.since or '(all)'}")
    print(f" trades (canon): {len(rows)}   (one row per ticker; rollups dropped)")
    print(f" rollups       : {rollup_n}   (trail_ratchet duplicate rows, excluded from sums)")
    print(f" tagged        : {len(tagged)}   (regime/edge cross-tabs use these)")
    print(f" untagged      : {untagged_n}   (pre-2026-04-21 or missing ladder_detail)")
    print(f" PnL source    : settlement_ledger (fallback=sum(kt.pnl) per ticker)")
    print(f" unsettled     : {unsettled_n}   (fell back to kt.pnl sum)")
    print(f" kt/sl drift   : {len(drift_ticker_rows)} ticker(s), net {drift_net:+.2f} (kt-sum minus sl truth)")
    print(f" PnL total     : ${sum(r.pnl for r in rows):+.2f}  (effective, canonical-only)")
    print(f" PnL tagged    : ${sum(r.pnl for r in tagged):+.2f}")
    if drift_ticker_rows:
        print()
        print(" -- per-ticker kt.pnl-sum vs sl.pnl disagreement --")
        for r in drift_ticker_rows:
            tstr = (r.placed_at or "")[:19].replace("T", " ")
            sl_str = f"{r.sl_pnl:+.2f}" if r.sl_pnl is not None else "NULL"
            kt_sum = getattr(r, "_kt_group_sum", r.kt_pnl)
            drift = getattr(r, "_drift", 0.0)
            print(f"   {tstr}  {r.ticker[-18:]:<18}  kt_sum={kt_sum:+7.2f}  sl={sl_str:>7}  drift={drift:+7.2f}")

    # 1) PnL/WR by regime
    h, t = _tabulate(
        tagged,
        lambda r: r.regime or "unk",
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=["structured", "chop", "chaotic"],
    )
    _print_table("1) PnL / WR by REGIME", h, t)

    # 2) MAE / MFE by regime
    h, t = _tabulate(
        tagged,
        lambda r: r.regime or "unk",
        {"avg_mae_c": _avg_mae, "avg_mfe_c": _avg_mfe, "mfe/|mae|": _mfe_mae_ratio,
         "pnl": _pnl},
        order=["structured", "chop", "chaotic"],
    )
    _print_table("2) MAE / MFE by REGIME", h, t)

    # 3) PnL by raw-edge bucket
    h, t = _tabulate(
        tagged,
        lambda r: _bucket_edge(r.raw_edge_pp),
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=["<0pp (NEG)", "0-3pp", "3-6pp", "6-10pp", "10+pp"],
    )
    _print_table("3) PnL by RAW-EDGE bucket (model vs market)", h, t)

    # 4) PnL by entry price bucket (uses ALL rows — price doesn't need tags)
    h, t = _tabulate(
        rows,
        lambda r: _bucket_price(r.limit_price),
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=["<45c", "45-54c", "55-64c", "65-74c", "75+c"],
    )
    _print_table("4) PnL by ENTRY PRICE bucket", h, t)

    # 5) PnL by hold-time bucket (also uses ALL rows)
    h, t = _tabulate(
        rows,
        lambda r: _bucket_hold(r.minutes_into_window),
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=[
            "<3min hold", "3-6min hold", "6-9min hold",
            "9-12min hold", "12-15min hold",
        ],
    )
    _print_table("5) PnL by HOLD TIME bucket (15min - entry offset)", h, t)

    # 6) PnL by pressure-confidence bucket
    h, t = _tabulate(
        tagged,
        lambda r: _bucket_confidence(r.pressure_confidence),
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=[
            "<0.40 (weak)", "0.40-0.54", "0.55-0.69",
            "0.70-0.84", "0.85+ (max)",
        ],
    )
    _print_table("6) PnL by PRESSURE CONFIDENCE bucket", h, t)

    # Bonus cross-tab: drawdown state (instant value if we see attrition patterns)
    h, t = _tabulate(
        tagged,
        lambda r: r.drawdown_state or "unk",
        {"pnl": _pnl, "avg_pnl": _avg_pnl, "wr%": _wr},
        order=["flat", "soft_dd", "hard_dd"],
    )
    _print_table("7) PnL by DRAWDOWN STATE (bonus -- revenge-trade detector)", h, t)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
