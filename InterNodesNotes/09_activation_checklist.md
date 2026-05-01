# Step 9: Activation checklist

After applying steps 1-8, run this verification sequence.

---

## 1. Imports clean

```bash
cd /path/to/btc-bias-engine
python -c "
import polymarket_copy_engine
import bb_pure
import kalshi_tape
import kalshi_ws
import signal_logger
import user_config
print('All imports clean')
"
```

Any `ModuleNotFoundError` or `SyntaxError` → stop and fix before
proceeding.

---

## 2. Unit tests pass

```bash
python -m pytest tests/test_bb_pure.py -v
```

Expected: **22 passed in <1s**.

If any fail, do not proceed.

---

## 3. Config flags read correctly

```bash
python -c "
import user_config as uc
flags = ['KALSHI_TAPE_ENABLED','STOP_REQUIRES_PERSISTENCE','ENTRY_FLOW_GATE_ENABLED','BOOK_DENSITY_GATE_ENABLED','PROTECTIVE_ORDER_MODE','BB_PURE_MODE']
for f in flags:
    print(f'{f:<32} = {getattr(uc, f, \"MISSING\")}')
"
```

All 6 should read `True` (or whatever your activation policy is).

---

## 4. Engine source markers present

```bash
python -c "
src = open('polymarket_copy_engine.py').read()
markers = [
    '_evaluate_entry_filter',
    '_evaluate_stop_persistence',
    '_maintain_protective_order',
    '_evaluate_bb_pure_signal',
    '_execute_bb_pure_signal',
    'BB_PURE_MODE — fair-value-anchored signal',
    '_protective_active',
    'gate=\"entry_flow\"',
    'gate=\"stop_persistence\"',
    '_tape_on_trade',
    'no_bid_data',
    'defer_max_exceeded',
]
missing = [m for m in markers if src.count(m) == 0]
print('All markers present' if not missing else f'MISSING: {missing}')
"
```

---

## 5. DB table created on engine init

The `gate_decisions` table is auto-created on `SignalLogger.initialize()`.
After first engine startup, verify:

```bash
sqlite3 data/trades.db ".schema gate_decisions" | head -3
# Expected first line:
# CREATE TABLE gate_decisions (
```

---

## 6. Smoke test the tape

```bash
python -c "
import kalshi_tape
t = kalshi_tape.KalshiTape(retention_s=10.0)
t.record_trade('TEST', 'yes', 5, 60)
t.record_trade('TEST', 'no', 10, 40)
t.record_mid('TEST', 50)
t.record_mid('TEST', 55)
print(t.flow('TEST', 5.0))
print(t.velocity_cps('TEST', 5.0))
"
```

Expected: dict with `yes_volume=5`, `no_volume=10`, `total_volume=15`,
`yes_share≈0.33`. Velocity will be 0.0 (mids recorded essentially
simultaneously).

---

## 7. Smoke test the density helpers

```bash
python -c "
from kalshi_ws import LocalOrderBook
b = LocalOrderBook()
b.apply_snapshot({'yes': [['0.32', 100], ['0.30', 200]], 'no': [['0.65', 500]]})
print(b.density('yes', 5))
print(b.imbalance_ratio())
"
```

Expected: `density('yes')` returns `top_volume=300`. `imbalance_ratio`
returns `300/(300+500) = 0.375`.

---

## 8. Restart engine

Use your machine's standard restart pattern:

```powershell
# NSSM (primary's setup)
nssm restart BTCBiasEngine

# OR interactive (secondary's setup per InterNodesNotes/# BTC Bias Engine - Current System Refer.md)
$env:SSL_CERT_FILE = "C:\Users\K\Desktop\btc-bias-engine\venv\Lib\site-packages\certifi\cacert.pem"
$env:PYTHONUNBUFFERED = "1"
& "C:\Users\K\Desktop\btc-bias-engine\venv\Scripts\python.exe" `
  "C:\Users\K\Desktop\btc-bias-engine\run_copy_engine.py" 2>&1 |
  Tee-Object -FilePath "data\engine.log"
```

---

## 9. Watch for new log lines

In the first ~1 minute of engine activity, you should see:

| Pattern | Means |
|---|---|
| `KalshiTape:` | Tape initialized (or warning if init failed) |
| `KalshiWS: websocket task started` | WS up (unchanged from before) |
| `terminal_copy DISABLED` | Wallet copy off (unchanged) |
| `bb_pure_signal` rows in `gate_decisions` | BB_PURE evaluator running |

If/when a TA_FORCED entry fires:
| Pattern | Means |
|---|---|
| `STOP DEFER (persistence): ...` | New persistence gate active |
| `ENTRY-GATE BLOCKED: ...` | New entry filter caught a knife |
| `PROTECTIVE [TP]: ...` or `PROTECTIVE [SL]:` | Protective-order mode managing exit |

If/when BB_PURE fires:
| Pattern | Means |
|---|---|
| `BB_PURE SIGNAL: ...` | BB model identified mispricing |
| `BB_PURE FIRE: ...` | Order being placed |
| `BB_PURE FILL: ...` | Order filled, protective mode taking over |

---

## 10. Observability query (run end of session)

```sql
SELECT gate, decision, reason, COUNT(*) AS n
FROM gate_decisions
WHERE ts_ms > strftime('%s','now','-12 hours')*1000
GROUP BY gate, decision, reason
ORDER BY n DESC;
```

Use this to tune thresholds. Pattern to look for:
- `gate='entry_flow' decision='block' reason='thin_same_density=...'`
   → density threshold may be too aggressive
- `gate='stop_persistence' decision='defer'` n=hundreds
   → persistence gate working as intended
- `gate='bb_pure_signal' decision='fire'`
   → BB_PURE is generating signals (Session 2 wired correctly)

---

## Rollback

If anything goes wrong, set `KALSHI_TAPE_ENABLED = False` in
`user_config.py`. This master flag short-circuits all 5 microstructure
gates. `BB_PURE_MODE = False` separately disables BB_PURE so the
composite cascade (TA_FORCED / LATE_DOMINANT / etc.) takes back over.

The bug-fix changes from step 8 (`no_bid_data`, `defer_max_exceeded`)
are dormant when `STOP_REQUIRES_PERSISTENCE = False` — no rollback
needed for those, just turn off the gate.

---

## Done

Engine should now be running with the same architecture as the primary
node. Watch `data/engine.log` (or your equivalent) for the next session
or two and check for any errors / unexpected behavior.

If you find a bug the primary doesn't have, log it and ping back so we
can keep both nodes in sync.
