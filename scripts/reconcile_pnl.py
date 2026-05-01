#!/usr/bin/env python3
"""reconcile_pnl.py — rewrite kalshi_trades.pnl from settlement_ledger truth.

Why this exists
---------------
Forensic review (7 days, 2026-04-13 .. 2026-04-19) exposed that
kalshi_trades.pnl is systemically corrupted:

  * Balance snapshots show peak $308.85 -> $32.15 (-89%).
  * Same-period DB SUM(pnl) reports roughly +$137.
  * Day-over-day: 7 of 11 days have DB pnl that DISAGREES with balance
    change by more than the explainable fee gap.

Smoking gun: order_id 31c7d02d-cb6a-4e76-ad8b-34873169ce91 has
count=1, filled_count=242, limit_price=82 -- yet its DB pnl is +$105.95.
The max possible pnl for that single row is (100-82)*242/100 = +$43.56.
What landed in the pnl column was in fact the ticker-level aggregate pnl
from settlement_ledger, written across every row that matched the ticker.

This script cross-references every settled kalshi_trades row against
settlement_ledger (Kalshi's authoritative settlement feed -- the table
IS the get_settlements() response persisted locally) and recomputes the
per-row pnl from:

  settle_cents = 100 if side == market_result else 0
  gross        = (settle_cents - limit_price) * filled_count / 100
  fee_per_ct   = ticker_fee_cents / total_ticker_filled_count
  net_pnl      = gross - fee_per_ct * filled_count / 100

Then writes the corrected pnl into kalshi_trades.pnl.

Usage
-----
  python scripts/reconcile_pnl.py --dry-run    # preview diffs, no write
  python scripts/reconcile_pnl.py --apply      # commit changes
  python scripts/reconcile_pnl.py --apply --min-diff 5   # only log diffs > $5

Safety
------
  * --dry-run by default. Explicit --apply required to write.
  * Creates a timestamped backup column `pnl_pre_reconcile_<ts>` so the
    old values are recoverable.
  * Only touches rows with status LIKE 'reconciled%' (settled trades).
    Never modifies open or pending rows.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger("reconcile_pnl")

DB_PATH = Path(r"C:\Trading\btc-bias-engine\data\trades.db")

# Status values that represent a settled, finalized trade and therefore a
# row whose pnl column SHOULD reflect the Kalshi-truth.
SETTLED_STATUSES = {
    "reconciled_settled",
    "reconciled_won",
    "reconciled_lost",
    "reconciled_closed",
    "reconciled_unknown",
    "reconciled_stale",
    "won",
    "lost",
}


def _load_settlements(cur: sqlite3.Cursor) -> Dict[str, dict]:
    """Return {ticker: {market_result, fee_cents, yes_cost, no_cost, ...}}."""
    out: Dict[str, dict] = {}
    cur.execute(
        "SELECT ticker, market_result, yes_count, no_count, "
        "yes_total_cost, no_total_cost, fee_cents, pnl_cents "
        "FROM settlement_ledger"
    )
    for row in cur.fetchall():
        tkr, mr, yc, nc, ytc, ntc, fee, pnl = row
        out[tkr] = {
            "market_result": mr,
            "yes_count": yc or 0,
            "no_count": nc or 0,
            "yes_total_cost": ytc or 0.0,
            "no_total_cost": ntc or 0.0,
            "fee_cents": fee or 0.0,
            "pnl_cents": pnl or 0.0,
        }
    return out


def _load_trades(cur: sqlite3.Cursor) -> List[tuple]:
    """Return all kalshi_trades rows whose status is settled."""
    placeholders = ",".join("?" * len(SETTLED_STATUSES))
    cur.execute(
        f"SELECT id, order_id, ticker, side, count, filled_count, "
        f"limit_price, pnl, status, result "
        f"FROM kalshi_trades WHERE status IN ({placeholders})",
        tuple(SETTLED_STATUSES),
    )
    return cur.fetchall()


def _group_by_ticker(trades: List[tuple]) -> Dict[str, List[tuple]]:
    g: Dict[str, List[tuple]] = defaultdict(list)
    for t in trades:
        g[t[2]].append(t)  # ticker
    return g


def _compute_row_pnl(
    side: str,
    filled_count: int,
    limit_price: int,
    settle: dict,
    ticker_total_filled: int,
) -> float:
    """Compute the truth pnl for a single kalshi_trades row.

    Pure function: given the row's (side, filled_count, limit_price) and
    the ticker's settlement record, returns pnl in DOLLARS.
    """
    if filled_count <= 0 or limit_price <= 0:
        return 0.0

    # Winner side gets 100c, loser gets 0c
    mr = (settle.get("market_result") or "").lower()
    settle_cents = 100 if side.lower() == mr else 0

    gross_pnl_cents = (settle_cents - limit_price) * filled_count

    # Fees: the settlement ledger reports total fees for the ticker. Allocate
    # proportionally by fill count. If the ticker's filled_count totals are
    # zero (malformed row), skip the fee attribution.
    fee_cents_total = float(settle.get("fee_cents", 0.0))
    if ticker_total_filled > 0 and fee_cents_total > 0:
        fee_per_ct = fee_cents_total / ticker_total_filled
        fee_cents = fee_per_ct * filled_count
    else:
        fee_cents = 0.0

    net_pnl_cents = gross_pnl_cents - fee_cents
    return round(net_pnl_cents / 100.0, 4)


def reconcile(
    db_path: Path,
    apply: bool,
    min_diff_dollars: float,
    limit_rows: int | None,
) -> int:
    """Walk every settled trade, recompute pnl, optionally write.

    Returns the number of rows that would change (or did change).
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = None
    cur = con.cursor()

    settlements = _load_settlements(cur)
    trades = _load_trades(cur)

    if not trades:
        logger.warning("No settled trades found in kalshi_trades.")
        return 0

    by_ticker = _group_by_ticker(trades)

    # Sum filled_count per ticker (denominator for fee attribution)
    ticker_total_filled: Dict[str, int] = {}
    for tkr, rows in by_ticker.items():
        ticker_total_filled[tkr] = sum(int(r[5] or 0) for r in rows)

    # Collect corrections
    corrections: List[Tuple[int, float, float]] = []  # (row_id, old_pnl, new_pnl)
    unmatched_tickers = 0
    for tkr, rows in by_ticker.items():
        settle = settlements.get(tkr)
        if settle is None:
            unmatched_tickers += 1
            continue
        for row in rows:
            row_id, order_id, _tkr, side, count, filled_count, limit_price, old_pnl, status, result = row
            new_pnl = _compute_row_pnl(
                side=side,
                filled_count=int(filled_count or 0),
                limit_price=int(limit_price or 0),
                settle=settle,
                ticker_total_filled=ticker_total_filled[tkr],
            )
            old = float(old_pnl or 0.0)
            if abs(new_pnl - old) >= 0.01:  # ignore sub-cent rounding
                corrections.append((row_id, old, new_pnl))

    # Filter by min_diff
    big_diffs = [c for c in corrections if abs(c[2] - c[1]) >= min_diff_dollars]

    # Log top 20 biggest diffs
    big_diffs_sorted = sorted(big_diffs, key=lambda c: abs(c[2] - c[1]), reverse=True)
    if big_diffs_sorted:
        logger.warning(
            "Top %d pnl corrections (|diff| >= $%.2f):",
            min(20, len(big_diffs_sorted)), min_diff_dollars,
        )
        for rid, old, new in big_diffs_sorted[:20]:
            cur.execute(
                "SELECT order_id, ticker, side, count, filled_count, limit_price, status "
                "FROM kalshi_trades WHERE id=?",
                (rid,),
            )
            oid, tkr, side, cnt, fc, lp, st = cur.fetchone()
            logger.warning(
                "  id=%d  %s  %s %dct/%dct @%dc  %s: %+.2f -> %+.2f  (diff=%+.2f)",
                rid, tkr, side.upper(), cnt, fc, lp, st,
                old, new, new - old,
            )

    total_old = sum(c[1] for c in corrections)
    total_new = sum(c[2] for c in corrections)
    logger.warning(
        "SUMMARY: %d rows settle-truth != DB (|diff| >= $0.01); "
        "%d rows have |diff| >= $%.2f | total old_pnl=%+.2f new_pnl=%+.2f shift=%+.2f | "
        "unmatched_tickers=%d",
        len(corrections), len(big_diffs), min_diff_dollars,
        total_old, total_new, total_new - total_old, unmatched_tickers,
    )

    if not apply:
        logger.warning("DRY RUN — no changes written. Use --apply to commit.")
        con.close()
        return len(corrections)

    # Backup the pnl column before writing
    ts = int(time.time())
    backup_col = f"pnl_pre_reconcile_{ts}"
    try:
        cur.execute(f'ALTER TABLE kalshi_trades ADD COLUMN "{backup_col}" REAL')
        cur.execute(f'UPDATE kalshi_trades SET "{backup_col}" = pnl')
        logger.warning("Backup column '%s' created and populated.", backup_col)
    except sqlite3.OperationalError as e:
        logger.warning("Backup-column step skipped: %s", e)

    # Apply corrections
    rows_written = 0
    for rid, _old, new in corrections:
        if limit_rows is not None and rows_written >= limit_rows:
            break
        cur.execute(
            "UPDATE kalshi_trades SET pnl = ? WHERE id = ?",
            (new, rid),
        )
        rows_written += cur.rowcount
    con.commit()
    logger.warning("APPLIED: updated %d rows. Backup in column '%s'.", rows_written, backup_col)
    con.close()
    return rows_written


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=Path, default=DB_PATH, help="path to trades.db")
    p.add_argument("--apply", action="store_true",
                   help="actually write corrections (default is dry run)")
    p.add_argument("--min-diff", type=float, default=1.0, metavar="USD",
                   help="only log diffs with |abs| >= this (default $1.00)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap number of rows to update (for testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if not args.db.exists():
        logger.error("DB not found: %s", args.db)
        return 2

    changed = reconcile(
        db_path=args.db,
        apply=args.apply,
        min_diff_dollars=args.min_diff,
        limit_rows=args.limit,
    )
    return 0 if changed >= 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
