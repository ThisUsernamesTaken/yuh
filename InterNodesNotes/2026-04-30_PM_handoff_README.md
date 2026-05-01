# Handoff to Secondary PC — 2026-04-30 PM

**From:** Primary node (Claude/coleb instance)
**To:** Secondary node (Claude/K instance)
**Date:** 2026-04-30 evening PT
**Status of primary engine:** Live with all changes active

This directory contains the complete instructions to apply the
2026-04-30 PM-session work to the secondary machine. Apply in the order
listed below. Each file in this directory is a discrete unit of work.

---

## Apply order (sequential, each fully before the next)

| Step | File | What it does |
|------|------|--------------|
| 1 | `01_user_config_additions.md` | Adds ~30 new config knobs. Default OFF for the 5 microstructure gates + `BB_PURE_MODE`. Safe to ship. |
| 2 | `02_kalshi_tape_NEW_FILE.py` | New file: `kalshi_tape.py`. Drop into repo root. |
| 3 | `03_bb_pure_NEW_FILE.py` | New file: `bb_pure.py`. Drop into repo root. |
| 4 | `04_test_bb_pure_NEW_FILE.py` | New file: `tests/test_bb_pure.py`. 22/22 must pass. |
| 5 | `05_signal_logger_diffs.md` | Adds `gate_decisions` table + `log_gate_decision()` method. Auto-creates table on next engine startup. |
| 6 | `06_kalshi_ws_diffs.md` | Adds `density()` / `volume_at_or_below()` / `volume_at_or_above()` / `imbalance_ratio()` helpers to `LocalOrderBook`. |
| 7 | `07_engine_microstructure_diffs.md` | Largest change: wire tape into engine init, add `_evaluate_entry_filter()`, `_evaluate_stop_persistence()`, `_maintain_protective_order()`, `_evaluate_bb_pure_signal()`, `_execute_bb_pure_signal()`. Wire all into the cascade + position management. |
| 8 | `08_engine_bugfixes_diffs.md` | Bug fixes from earlier today (overnight $30+ losses): persistence-gate `no_bid_data` early return + `defer_max_exceeded` cap. |
| 9 | `09_activation_checklist.md` | Final smoke tests + activation sequence. Run pytest, verify imports, restart engine. |

---

## What this work does (1-paragraph summary)

The primary engine took ~$45 in losses overnight 2026-04-29 → 2026-04-30
from held-to-expiry positions caused by Kalshi positions-API cache lag
defeating the bid-check stop loss. Today's PM work shipped a 5-component
microstructure-aware gating layer (`KALSHI_TAPE_ENABLED`,
`STOP_REQUIRES_PERSISTENCE`, `ENTRY_FLOW_GATE_ENABLED`,
`BOOK_DENSITY_GATE_ENABLED`, `PROTECTIVE_ORDER_MODE`), a full unit-tested
`gate_decisions` observability table for tuning gates against trade
outcomes, plus Sessions 1+2 of a `BB_PURE_MODE` refactor that returns
the engine to its founding-philosophy roots — Brownian-Bridge fair-value
mispricing as the ONLY signal, microstructure as execution-quality
filters, Kelly-on-edge sizing.

---

## Live activation status on primary

```python
KALSHI_TAPE_ENABLED          = True
STOP_REQUIRES_PERSISTENCE    = True
ENTRY_FLOW_GATE_ENABLED      = True
BOOK_DENSITY_GATE_ENABLED    = True
PROTECTIVE_ORDER_MODE        = True
BB_PURE_MODE                 = True   # Session 2 wired in cascade
```

Per user direction: paper testing is inaccurate, real execution behavior
is the learning signal. All flags activated together. Match this on
secondary if you want symmetrical behavior.

---

## How to verify success (after applying everything)

Run from repo root:

```bash
cd /path/to/btc-bias-engine
python -m pytest tests/test_bb_pure.py -v
python -c "import polymarket_copy_engine, kalshi_tape, bb_pure, kalshi_ws, signal_logger, user_config as uc; print('Imports OK'); print('BB_PURE_MODE =', uc.BB_PURE_MODE)"
```

Expected:
- 22/22 tests pass
- Imports clean
- All 6 flags read `True`

Then restart engine and watch for `BB_PURE FIRE` log lines in
`data/engine.log` (or `data/engine_history.log` depending on machine).

---

## Open issues (heads-up for secondary)

1. **Cache-lag window 60-180s** — the bid-check stop's "deferred-on-zero"
   handling (Patch #11 from earlier today) keeps `_open_position` alive
   during this window. Protective-order mode only takes over once Kalshi
   confirms `truth_ct > 0`. Both layers coexist; legacy stops gate on
   `not pos.get("_protective_active", False)` so they don't double-fire.

2. **Stop-persistence false-zero loop** — first iteration of stop
   persistence had a bug where bid=0 (settled book) caused infinite
   defer. Fixed via `no_bid_data` early return in `_evaluate_stop_persistence`.
   See `08_engine_bugfixes_diffs.md`.

3. **Composite cascade still firing some entries** — `BB_PURE_MODE=True`
   tries to bypass the composite cascade, but only when BB model
   produces a qualifying signal (≥8pp mispricing). When BB has no edge,
   the legacy SR_FADE / LATE_DOMINANT / TA_FORCED cascade can still fire.
   This is intentional fallback during the BB_PURE rollout.

---

## Contact

If anything is unclear, the primary's CLAUDE.md (in repo root) has the
full architectural docs. This handoff is the delta — CLAUDE.md is the
state of the world.
