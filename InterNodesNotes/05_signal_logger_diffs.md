# Step 5: `signal_logger.py` modifications

Two changes:
1. Add `time` import
2. Add `_CREATE_GATE_DECISIONS_TABLE` and `_CREATE_GATE_DECISIONS_INDEXES` after `_CREATE_MANUAL_FILLS_INDEXES`
3. Wire table creation into `initialize()`
4. Add new `log_gate_decision()` method before `log_kalshi_trade()`

---

## Change 1: Imports

**Find:**
```python
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional
```

**Replace with:**
```python
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional
```

---

## Change 2: New table SQL

**Find** (end of `_CREATE_MANUAL_FILLS_INDEXES = """..."""` block):
```python
_CREATE_MANUAL_FILLS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_manual_fills_ticker ON manual_fills(ticker);
CREATE INDEX IF NOT EXISTS idx_manual_fills_created ON manual_fills(created_at_ms);
CREATE INDEX IF NOT EXISTS idx_manual_fills_settled ON manual_fills(settled);
"""
```

**Append immediately after:**
```python

# 2026-04-30: Gate-decision observability table.
# Every entry / stop / TP-flip gate writes a row here so we can correlate
# pass/block decisions with subsequent trade outcomes and tune thresholds.
_CREATE_GATE_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS gate_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    ticker          TEXT,
    gate            TEXT NOT NULL,        -- "entry_flow", "stop_persistence",
                                          -- "protective_order", "book_density"
    decision        TEXT NOT NULL,        -- "pass" | "block" | "defer" | "fire"
    reason          TEXT,                 -- short reason code
    side            TEXT,                 -- yes | no | (null)
    -- Inputs / context (sparse — rows leave irrelevant fields NULL)
    session_min     REAL,
    btc_spot        REAL,
    btc_vel_5m      REAL,
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    no_bid          INTEGER,
    no_ask          INTEGER,
    -- Flow snapshot (last 5/15s)
    flow5_yes_vol   INTEGER,
    flow5_no_vol    INTEGER,
    flow15_yes_vol  INTEGER,
    flow15_no_vol   INTEGER,
    velocity_5s     REAL,
    velocity_15s    REAL,
    -- Density snapshot (top-5 levels)
    yes_bid_density INTEGER,
    no_bid_density  INTEGER,
    density_below_trigger INTEGER,    -- bid volume <= trigger px
    -- Stop-specific
    bid_at_trigger_seconds REAL,      -- duration trigger has been touched
    trigger_volume  INTEGER,          -- volume at-or-below trigger in window
    -- Position context
    position_count  INTEGER,
    entry_cents     INTEGER,
    fill_age_s      REAL,
    -- Linkage
    linked_trade_id TEXT,             -- order_id or fill_id for joining
    -- Catch-all blob for fields not surfaced above
    snapshot_json   TEXT
)
"""

_CREATE_GATE_DECISIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_gate_decisions_ts ON gate_decisions(ts_ms);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_ticker ON gate_decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_gate ON gate_decisions(gate);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_decision ON gate_decisions(decision);
"""
```

---

## Change 3: Wire table creation into `initialize()`

**Find** (inside `async def initialize()`, in the `async with aiosqlite.connect(self._trades_db) as db:` block):
```python
            await db.execute(_CREATE_MANUAL_FILLS_TABLE)
            for stmt in _CREATE_MANUAL_FILLS_INDEXES.strip().split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            await db.commit()
```

**Replace with:**
```python
            await db.execute(_CREATE_MANUAL_FILLS_TABLE)
            for stmt in _CREATE_MANUAL_FILLS_INDEXES.strip().split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            # 2026-04-30: gate decisions
            await db.execute(_CREATE_GATE_DECISIONS_TABLE)
            for stmt in _CREATE_GATE_DECISIONS_INDEXES.strip().split(";"):
                if stmt.strip():
                    await db.execute(stmt)
            await db.commit()
```

---

## Change 4: New `log_gate_decision()` method

**Find:**
```python
    async def log_kalshi_trade(
        self,
        order_id: str,
```

**Insert immediately before** (just before `async def log_kalshi_trade`):
```python
    async def log_gate_decision(self, **fields) -> None:
        """Persist a gate decision to gate_decisions for post-hoc analysis.

        Required fields: gate (str), decision (str), ts_ms (int).
        Everything else is optional and goes to the matching column or
        snapshot_json blob if no column exists.
        """
        if not self._initialized:
            await self.initialize()
        # Whitelist of columns we know about; anything else lands in
        # snapshot_json so the schema stays stable.
        known_cols = {
            "ts_ms", "ticker", "gate", "decision", "reason", "side",
            "session_min", "btc_spot", "btc_vel_5m",
            "yes_bid", "yes_ask", "no_bid", "no_ask",
            "flow5_yes_vol", "flow5_no_vol",
            "flow15_yes_vol", "flow15_no_vol",
            "velocity_5s", "velocity_15s",
            "yes_bid_density", "no_bid_density", "density_below_trigger",
            "bid_at_trigger_seconds", "trigger_volume",
            "position_count", "entry_cents", "fill_age_s",
            "linked_trade_id",
        }
        col_values = {k: fields[k] for k in fields if k in known_cols}
        extras = {k: fields[k] for k in fields if k not in known_cols}
        # Required defaults
        if "ts_ms" not in col_values:
            col_values["ts_ms"] = int(time.time() * 1000)
        if "gate" not in col_values:
            col_values["gate"] = "?"
        if "decision" not in col_values:
            col_values["decision"] = "?"
        if extras:
            try:
                col_values["snapshot_json"] = json.dumps(extras, default=str)
            except Exception:
                col_values["snapshot_json"] = str(extras)
        col_list = list(col_values.keys())
        placeholders = ", ".join("?" for _ in col_list)
        col_str = ", ".join(col_list)
        try:
            async with aiosqlite.connect(self._trades_db) as db:
                await db.execute(
                    f"INSERT INTO gate_decisions ({col_str}) VALUES ({placeholders})",
                    tuple(col_values[k] for k in col_list),
                )
                await db.commit()
        except Exception as e:
            logger.warning("log_gate_decision failed: %s", e)

```

---

## Verification

After saving, restart engine. The `gate_decisions` table is auto-created
in `data/trades.db` on startup via `SignalLogger.initialize()`.

```bash
sqlite3 data/trades.db ".schema gate_decisions"
```

Should show the table definition. If the table already existed from a
previous version, `CREATE TABLE IF NOT EXISTS` will skip — safe to apply.
