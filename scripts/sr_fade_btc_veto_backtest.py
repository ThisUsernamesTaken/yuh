"""SR_FADE BTC continuation veto backtest (2026-04-23 per Codex spec).

Pulls all reconciled SR_FADE trades with their ladder_detail attribution,
cross-references pre-entry BTC context (from window_snapshots and
SR-FADE-DBG log lines where available), and reports:

  - side / entry / edge / regime / sr_level state
  - pre-entry btc_5m (from SR-FADE-DBG log line immediately prior)
  - pressure_score, pressure_confidence, oriented pressure
  - veto verdict with provisional thresholds
  - outcome pnl / status

HISTORICAL DATA LIMITATION:
  5s and 30s BTC moves are not cleanly retrievable from the DB — they live
  in _btc_ts_buffer at runtime. Pre-entry SR-FADE-DBG log lines do report
  btc_5m though (with $ precision). This script uses btc_5m as the
  continuation indicator for historical backtest and flags the gap.
  The LIVE veto will use fine-grained 5s/30s from _btc_ts_buffer.

Usage: python scripts/sr_fade_btc_veto_backtest.py
"""
import json
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

DB = Path(__file__).resolve().parent.parent / "data" / "trades.db"
LOG = Path(__file__).resolve().parent.parent / "data" / "engine_history.log"

# Provisional thresholds per Codex v1 spec. 30s/5s USD thresholds will
# calibrate later. For this historical analysis we use btc_5m as proxy.
BTC_VETO_5M_USD_PROXY = 15.0  # proxy threshold on 5m move


def _compute_settlement_pnl(side, filled_count, limit_price, settle, ticker_total_filled):
    """Port of scripts/reconcile_pnl.py::compute_pnl_for_row. Returns dollars."""
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


def _to_ts_ms(placed_at: str) -> int:
    if not placed_at:
        return 0
    try:
        s = placed_at.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _load_btc_context_from_logs(trade_ts_ms: int):
    """Scan engine_history.log for SR-FADE-DBG lines with btc_5m=$X
    timestamped within ~60s before the trade. Returns float or None."""
    if not LOG.exists():
        return None
    # Parse log lines like:
    # 2026-04-23 19:34:23,362 [...] CopyEngine SR-FADE-DBG: btc_5m=$-4.5 below...
    target_s = trade_ts_ms / 1000.0
    best_val = None
    best_age = 999
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "btc_5m=$" not in line:
                    continue
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=timezone.utc).timestamp()
                    if ts > target_s:
                        continue
                    age = target_s - ts
                    if age > 90:
                        continue
                    m = re.search(r"btc_5m=\$(-?\d+\.?\d*)", line)
                    if not m:
                        continue
                    val = float(m.group(1))
                    if age < best_age:
                        best_age = age
                        best_val = val
                except Exception:
                    continue
    except Exception:
        return None
    return best_val


def _orient_pressure(side: str, ps: float, neutral_band: float = 0.02) -> str:
    if abs(ps) <= neutral_band:
        return "neutral"
    if (side == "yes" and ps > 0) or (side == "no" and ps < 0):
        return "agree"
    return "disagree"


def _veto_fires_proxy(side: str, btc_5m: float, pressure_orient: str) -> tuple[bool, str]:
    """Proxy version of Codex veto v1 using btc_5m as continuation signal.

    Codex's live veto uses 30s/5s BTC moves + sign agreement. Historical DB
    only gives us 5m so we use abs(btc_5m) + directional check as proxy.
    """
    if btc_5m is None:
        return (False, "no_btc_data")
    btc_against = (
        (side == "no" and btc_5m > 0) or
        (side == "yes" and btc_5m < 0)
    )
    btc_continuing = abs(btc_5m) >= BTC_VETO_5M_USD_PROXY
    pressure_not_helping = pressure_orient != "agree"
    if btc_against and btc_continuing and pressure_not_helping:
        return (True, f"btc_5m={btc_5m:+.1f} against+strong+press={pressure_orient}")
    reasons = []
    if not btc_against:
        reasons.append(f"btc_5m={btc_5m:+.1f}_favors_side")
    if not btc_continuing:
        reasons.append(f"abs(btc_5m)={abs(btc_5m):.1f}<thresh={BTC_VETO_5M_USD_PROXY}")
    if not pressure_not_helping:
        reasons.append(f"press=agree")
    return (False, "+".join(reasons))


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT order_id, placed_at, ticker, side, count, filled_count,
               limit_price, pnl, status, ladder_detail
        FROM kalshi_trades
        WHERE strategy_name='SR_FADE' AND status LIKE 'reconciled%'
        ORDER BY placed_at ASC
    """)
    trades = [dict(r) for r in cur.fetchall()]

    # Codex P1 (2026-04-23): replace pnl with settlement-truth values.
    cur.execute(
        "SELECT ticker, market_result, fee_cents FROM settlement_ledger",
    )
    settle_map = {row[0]: {"market_result": row[1], "fee_cents": row[2] or 0.0}
                  for row in cur.fetchall()}
    total_filled_cache = {}
    for t in trades:
        ticker = t["ticker"]
        settle = settle_map.get(ticker)
        if settle:
            if ticker not in total_filled_cache:
                cur.execute(
                    "SELECT COALESCE(SUM(filled_count),0) FROM kalshi_trades "
                    "WHERE ticker=?", (ticker,),
                )
                total_filled_cache[ticker] = int(cur.fetchone()[0] or 0)
            t["pnl"] = _compute_settlement_pnl(
                t["side"], int(t.get("filled_count") or t.get("count") or 0),
                int(t["limit_price"] or 0), settle, total_filled_cache[ticker],
            )
            t["_pnl_source"] = "settlement_ledger"
        else:
            t["_pnl_source"] = "stale_no_settlement"

    print(f"Reconciled SR_FADE trades: {len(trades)}\n")
    print(f"{'placed':<19} {'side':<4} {'entry':>5} {'pnl$':>8} "
          f"{'edge_pp':>8} {'regime':<10} {'dwells':>6} {'bnc':>4} "
          f"{'ps':>6} {'orient':<9} {'btc_5m':>7} {'veto':<6} {'note'}")
    print("-" * 150)

    winners_vetoed = 0
    losers_vetoed = 0
    winners_kept = 0
    losers_kept = 0
    vetoed_pnl = 0.0
    kept_pnl = 0.0

    for t in trades:
        try:
            ld = json.loads(t["ladder_detail"]) if t["ladder_detail"] else {}
        except Exception:
            ld = {}

        side = (t["side"] or "").lower()
        entry = int(t["limit_price"] or 0)
        pnl = float(t["pnl"] or 0)
        edge = float(ld.get("raw_edge_pp") or 0.0)
        regime = ld.get("regime", "?")
        ps = float(ld.get("pressure_score") or 0.0)
        pc = float(ld.get("pressure_confidence") or 0.0)
        sr = ld.get("sr_level") or {}
        dwells = int(sr.get("dwells", 0) or 0)
        bounces = int(sr.get("bounce_total", 0) or 0)
        orient = _orient_pressure(side, ps)

        ts_ms = _to_ts_ms(t["placed_at"])
        btc_5m = _load_btc_context_from_logs(ts_ms)
        veto, veto_reason = _veto_fires_proxy(side, btc_5m, orient)

        is_win = pnl > 0
        if veto:
            vetoed_pnl += pnl
            if is_win:
                winners_vetoed += 1
            else:
                losers_vetoed += 1
        else:
            kept_pnl += pnl
            if is_win:
                winners_kept += 1
            else:
                losers_kept += 1

        btc_str = f"{btc_5m:+.1f}" if btc_5m is not None else "n/a"
        print(f"{t['placed_at'][:19]:<19} {side:<4} {entry:>5} "
              f"{pnl:>+8.2f} {edge:>7.1f} {regime:<10} {dwells:>6} "
              f"{bounces:>4} {ps:>+6.2f} {orient:<9} {btc_str:>7} "
              f"{'YES' if veto else '-':<6} {veto_reason}")

    print()
    print("-- Summary --")
    total = len(trades)
    total_pnl = sum(float(t["pnl"] or 0) for t in trades)
    print(f"Total trades: {total}  total pnl=${total_pnl:+.2f}")
    print(f"Proxy veto would block {winners_vetoed + losers_vetoed}/{total} "
          f"(wins={winners_vetoed} losers={losers_vetoed} blocked_pnl=${vetoed_pnl:+.2f})")
    print(f"Kept trades: {winners_kept + losers_kept}/{total} "
          f"(wins={winners_kept} losers={losers_kept} kept_pnl=${kept_pnl:+.2f})")
    print()
    print(f"Blocked-pnl / kept-pnl split: ${vetoed_pnl:+.2f} vetoed, "
          f"${kept_pnl:+.2f} kept. Good veto = blocks bad trades (losers),"
          f" keeps good ones.")
    print()
    print("HISTORICAL DATA LIMITATION NOTE:")
    print("  This backtest uses btc_5m as a proxy for Codex's 30s/5s veto")
    print("  because 5s and 30s BTC moves are only in the live")
    print("  _btc_ts_buffer. Proxy suffers from aliasing (5m window is")
    print("  much longer than 30s). The real veto at live-runtime will be")
    print("  more selective than this proxy suggests.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
