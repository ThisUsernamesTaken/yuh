"""SR_FADE attribution queries requested 2026-04-23 post-Codex review.

Runs 4 cuts on reconciled SR_FADE trades:
  1. PnL by entry band (10-35c cheap vs 36-55c mid)
  2. PnL by regime
  3. PnL by pressure-direction agreement (pressure sign vs buy-side direction)
  4. PnL by level_strength bucket if available in ladder_detail

Codex P1 (2026-04-23): PnL now computed from settlement_ledger truth via
the same formula as scripts/reconcile_pnl.py, not kalshi_trades.pnl which
can be stale. When a settlement row is missing for a trade's ticker, the
trade is labeled pnl_source='stale' and shown separately so calibration
doesn't mix corrupt labels with authoritative ones.

Usage: python scripts/sr_fade_attribution.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"


def _compute_settlement_pnl(side: str, filled_count: int, limit_price: int,
                            settle: dict, ticker_total_filled: int) -> float:
    """Port of scripts/reconcile_pnl.py::compute_pnl_for_row.
    Returns pnl in dollars using settlement truth."""
    if filled_count <= 0 or limit_price <= 0:
        return 0.0
    mr = (settle.get("market_result") or "").lower()
    settle_cents = 100 if side.lower() == mr else 0
    gross = (settle_cents - limit_price) * filled_count
    fee_total = float(settle.get("fee_cents", 0.0))
    if ticker_total_filled > 0 and fee_total > 0:
        fee_share = fee_total * filled_count / ticker_total_filled
    else:
        fee_share = 0.0
    return round((gross - fee_share) / 100.0, 4)


def _load_settlement_map(conn) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, market_result, yes_count, no_count, "
        "yes_total_cost, no_total_cost, fee_cents, pnl_cents "
        "FROM settlement_ledger"
    )
    out = {}
    for row in cur.fetchall():
        t = row[0]
        out[t] = {
            "market_result": row[1],
            "yes_count": row[2] or 0,
            "no_count": row[3] or 0,
            "yes_total_cost": row[4] or 0.0,
            "no_total_cost": row[5] or 0.0,
            "fee_cents": row[6] or 0.0,
            "pnl_cents": row[7] or 0.0,
        }
    return out


def _ticker_total_filled(conn, ticker: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(filled_count) FROM kalshi_trades WHERE ticker = ? "
        "AND filled_count IS NOT NULL",
        (ticker,),
    )
    r = cur.fetchone()
    return int(r[0] or 0)


def _bucket_sum(rows, keyfn):
    d = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "avg_px": 0.0, "px_total": 0})
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        d[k]["n"] += 1
        d[k]["wins"] += 1 if (r["pnl"] or 0) > 0 else 0
        d[k]["pnl"] += float(r["pnl"] or 0)
        d[k]["px_total"] += int(r["limit_price"] or 0)
    for k, v in d.items():
        v["wr"] = (v["wins"] / v["n"] * 100.0) if v["n"] else 0.0
        v["avg_px"] = (v["px_total"] / v["n"]) if v["n"] else 0.0
    return d


def _print_bucket(title, b):
    print(f"\n--{title}--")
    keys = sorted(b.keys(), key=lambda k: str(k))
    print(f"{'bucket':<24} {'n':>4} {'wr%':>6} {'pnl$':>8} {'avg_px':>7}")
    for k in keys:
        v = b[k]
        print(f"{str(k):<24} {v['n']:>4} {v['wr']:>6.1f} {v['pnl']:>8.2f} {v['avg_px']:>7.1f}")


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT order_id, placed_at, ticker, side, count, filled_count,
               limit_price, pnl, status, strategy_name, ladder_detail
        FROM kalshi_trades
        WHERE strategy_name = 'SR_FADE'
          AND status LIKE 'reconciled%'
        ORDER BY placed_at ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]

    # Codex P1 (2026-04-23): replace each row's pnl with settlement-truth
    # value. Mark pnl_source so rows missing a settlement row are shown
    # separately from authoritative ones.
    settle_map = _load_settlement_map(conn)
    total_filled_cache: dict = {}
    rows_with_truth = []
    stale_rows = []
    for r in rows:
        try:
            r["_ld"] = json.loads(r["ladder_detail"]) if r["ladder_detail"] else {}
        except Exception:
            r["_ld"] = {}
        ticker = r["ticker"]
        filled = int(r.get("filled_count") or r.get("count") or 0)
        settle = settle_map.get(ticker)
        if settle:
            if ticker not in total_filled_cache:
                total_filled_cache[ticker] = _ticker_total_filled(conn, ticker)
            truth_pnl = _compute_settlement_pnl(
                r["side"], filled, int(r["limit_price"] or 0),
                settle, total_filled_cache[ticker],
            )
            r["pnl"] = truth_pnl
            r["_pnl_source"] = "settlement_ledger"
            rows_with_truth.append(r)
        else:
            # No settlement row yet (still pending) — skip from main analysis.
            r["_pnl_source"] = "stale_no_settlement"
            stale_rows.append(r)

    print(f"Total SR_FADE reconciled trades: {len(rows)}")
    print(f"  with settlement truth: {len(rows_with_truth)}")
    print(f"  missing settlement (excluded): {len(stale_rows)}")
    if not rows_with_truth:
        return 1
    rows = rows_with_truth
    total_pnl = sum(float(r["pnl"] or 0) for r in rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    print(f"Overall (settlement truth): pnl=${total_pnl:+.2f}  "
          f"wins={wins}/{len(rows)}  wr={wins/len(rows)*100:.1f}%")

    # 1. Entry band
    def band(r):
        px = int(r["limit_price"] or 0)
        if px < 10:
            return "<10c"
        if px <= 35:
            return "cheap (10-35c)"
        if px <= 55:
            return "mid (36-55c)"
        return ">55c"
    _print_bucket("Entry band", _bucket_sum(rows, band))

    # 2. Regime
    def regime(r):
        return (r["_ld"].get("regime") or "?").lower()
    _print_bucket("Regime", _bucket_sum(rows, regime))

    # 3. Pressure agreement: sign(pressure_score) vs buy direction
    # SR_FADE buys NO to fade up-move, YES to fade down-move.
    # "Agreement" = pressure agrees with the buy side (confirms fade target).
    # "Disagreement" = pressure still favors continuation against our fade.
    def pressure_agree(r):
        ps = r["_ld"].get("pressure_score")
        if ps is None:
            return None
        ps = float(ps)
        side = (r["side"] or "").lower()
        eps = 0.02
        if abs(ps) <= eps:
            return "neutral (|ps|<=0.02)"
        pressure_up = ps > 0
        if side == "yes":
            # Buying YES = fading a down-move. Pressure up = agrees.
            return "agree" if pressure_up else "disagree"
        if side == "no":
            # Buying NO = fading an up-move. Pressure down = agrees.
            return "agree" if not pressure_up else "disagree"
        return None
    _print_bucket("Pressure agreement w/ fade", _bucket_sum(rows, pressure_agree))

    # 4. Level strength bucket (if present)
    def strength(r):
        s = r["_ld"].get("level_strength") or r["_ld"].get("sr_strength")
        if s is None:
            return "unlogged"
        s = float(s)
        if s < 0.3:
            return "low (<0.3)"
        if s < 0.5:
            return "mid (0.3-0.5)"
        if s < 0.7:
            return "high (0.5-0.7)"
        return "top (>=0.7)"
    _print_bucket("Level strength", _bucket_sum(rows, strength))

    # 5. Bonus: pressure confidence × agreement (Codex's P1-1 thesis)
    def conf_x_agree(r):
        pc = r["_ld"].get("pressure_confidence")
        if pc is None:
            return None
        agree = pressure_agree(r)
        if agree is None:
            return None
        pc_band = "high(>=0.7)" if float(pc) >= 0.7 else "low(<0.7)"
        return f"{agree}/{pc_band}"
    _print_bucket("Agreement × confidence", _bucket_sum(rows, conf_x_agree))

    return 0


if __name__ == "__main__":
    sys.exit(main())
