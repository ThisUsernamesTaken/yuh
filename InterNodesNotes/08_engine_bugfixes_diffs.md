# Step 8: Bug fixes — `polymarket_copy_engine.py`

These fixes address real failure modes hit on the primary today.
Apply BEFORE activation if you're using the same flag set.

---

## Bug 1: Stop-persistence infinite loop on settled book (CRITICAL)

**Symptom:** When the Kalshi window settles, the bid drops to 0c (dead
book). The stop persistence gate sees `bid (0) ≤ trigger (43c)`, demands
volume confirmation, finds zero volume on a dead book, and **defers
forever**. The engine never clears `_open_position`, so it can't take
new entries on subsequent windows.

**Observed live:** Primary engine stuck for 16+ minutes after a winning
trade closed at +$2.70. Window already settled, but engine kept logging
`STOP DEFER (persistence): bid=0c reason=volume=0<10 in 10.0s` every 5
seconds. New session entries blocked.

**Fix in `_evaluate_stop_persistence()`:**

**Find:**
```python
        # Persistence requirement: track when bid first crossed trigger
        bid_at_trigger_first = pos.get("_bid_at_trigger_first_ts", 0.0)
        now = time.time()
        if current_bid <= trigger_px:
            if bid_at_trigger_first <= 0:
                pos["_bid_at_trigger_first_ts"] = now
                bid_at_trigger_first = now
        else:
            # Bid above trigger — reset the timer
            pos["_bid_at_trigger_first_ts"] = 0.0
            return "passthrough", "bid_above_trigger", snapshot
```

**Replace with:**
```python
        # Persistence requirement: track when bid first crossed trigger
        # 2026-04-30 BUG-FIX: when current_bid <= 0 the book is empty
        # (settlement / dead market). The persistence gate must NOT defer
        # in that state — it would loop forever, blocking the legacy
        # Patch #11 "accept flat after 180s" timeout from running. Pass
        # through so legacy logic can clear state.
        if current_bid <= 0:
            return "passthrough", "no_bid_data", snapshot
        bid_at_trigger_first = pos.get("_bid_at_trigger_first_ts", 0.0)
        now = time.time()
        if current_bid <= trigger_px:
            if bid_at_trigger_first <= 0:
                pos["_bid_at_trigger_first_ts"] = now
                bid_at_trigger_first = now
        else:
            # Bid above trigger — reset the timer
            pos["_bid_at_trigger_first_ts"] = 0.0
            return "passthrough", "bid_above_trigger", snapshot
```

---

## Bug 2: Max-defer cap (belt-and-suspenders)

Even with Bug 1 fixed, add a hard 5-min cap on persistence defer
duration. If we've been deferring for > 5 min on any path, fall through
to legacy stop logic to handle.

**Find** (just after the bid_seconds calculation in
`_evaluate_stop_persistence`):
```python
        bid_seconds = now - bid_at_trigger_first
        snapshot["bid_at_trigger_seconds"] = round(bid_seconds, 2)

        required_secs = self._session_stop_persistence_s()
        if bid_seconds < required_secs:
            return "defer", (
                f"persistence={bid_seconds:.1f}s<{required_secs:.0f}s"), snapshot
```

**Replace with:**
```python
        bid_seconds = now - bid_at_trigger_first
        snapshot["bid_at_trigger_seconds"] = round(bid_seconds, 2)

        # 2026-04-30 BUG-FIX: hard ceiling on defer duration. If we've
        # been deferring for > 5 min, the legacy stop / Patch #11 timeout
        # path needs to run.
        if bid_seconds > 300.0:
            return "passthrough", "defer_max_exceeded", snapshot

        required_secs = self._session_stop_persistence_s()
        if bid_seconds < required_secs:
            return "defer", (
                f"persistence={bid_seconds:.1f}s<{required_secs:.0f}s"), snapshot
```

---

## Reference: other bug fixes already on primary (verify yours match)

The primary engine has these earlier bug fixes from 2026-04-29 / 30 AM
that you should verify are present. Search for these comment markers:

| Marker | What it fixes |
|---|---|
| `2026-04-29 GHOST-bug fix` | Patches #1-5 — original ghost-race fix |
| `Patch #11 (2026-04-30)` | Defer-on-zero from cache lag (TA_FORCED + LATE_DOMINANT stop blocks) |
| `Patch #12` | Limit-sell at bid-1 instead of penny on stop fire |
| `Patch #15 (2026-04-30)` | OVER-FILL gate fallback when `_engine_recent_ct=0` but ticker is engine-touched. Uses `SIZING_HARD_CAP_CONTRACTS_DAY` as cap fallback. |
| `Patch #16 (2026-04-30)` | Penny sell on SYNC RESIDUAL FLATTEN to guarantee fill on sticky books |

```bash
python -c "
src = open('polymarket_copy_engine.py').read()
markers = [
    '2026-04-29 GHOST-bug fix',
    'Patch #11 (2026-04-30)',
    'Patch #12',
    'Patch #15 (2026-04-30)',
    'Patch #16 (2026-04-30)',
    'no_bid_data',
    'defer_max_exceeded',
]
for m in markers:
    n = src.count(m)
    print(f'{m:<40} hits={n} {\"OK\" if n>0 else \"VERIFY MANUALLY\"}')
"
```

If any earlier patch is missing, refer to `REFERENCE_polymarket_copy_engine.py`
in this directory for the canonical version.
