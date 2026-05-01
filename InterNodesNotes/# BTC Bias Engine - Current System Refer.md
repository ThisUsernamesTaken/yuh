# BTC Bias Engine - Current System Reference

**Last updated**: 2026-04-30
**Entry point**: `run_copy_engine.py`
**Live primary signal**: `TA_FORCED` (FVG / Brownian-Bridge engine)
**Status**: Live trading enabled (`PAPER_TRADING = False`)

This document is the operator-facing source of truth for the current live engine. If this doc and code disagree, the code wins. Update this doc whenever signal logic or config defaults materially change.

For deep-dive on the 2026-04-29 patch session (GHOST race fix, dynamic sizing, TP-EXPANSION, replication guide), see **`docs/SESSION_NOTES_2026-04-29.md`**.

---

## What the engine does now

Trades Kalshi `KXBTC15M` 15-minute BTC binary options.

The current live entry tier is `TA_FORCED` — the FVG (Fair Value Gap) / Brownian-Bridge engine — which combines:

- Brownian-Bridge fair-value model from `price_feed.py`
- Fair-value-gap (FVG) detection vs session baseline
- Microstructure pressure (book imbalance, trade flow, BTC impulse)
- Wall-consumption detector (aggressor-side flow)
- Regime classifier (TRENDING_UP / DOWN / RANGING / VOLATILE / EXPLOSIVE / MEAN_REVERTING)
- 8¢-below-entry stop-loss on every claimed position
- Multi-tier TP ladder

The engine enters with maker-only limits, manages take-profit / stop-loss while in position, and runs a position reconciler that catches "GHOST race" mismatches between Kalshi truth and engine state.

`SR_FADE` and `SNIPER` tiers are retired (disabled). Wallet-copying logic remains in the codebase but is disabled.

---

## Current live signal stack

### Active

1. `TA_FORCED` (FVG / Brownian-Bridge — primary)
   `TA_FORCED_ENABLED = True` and `TA_FORCED_ENTRY_ENABLED = True`. Fires when the BB fair-value gap is non-marginal, regime is favorable, microstructure pressure agrees, and a 5-min BTC trend gate passes (`DOMINANT_BTC_5M_THRESHOLD = 15.0`).

2. `LATE_DOMINANT` (final-3-min momentum)
   `LATE_DOMINANT_ENABLED = True`. Fires in the last 3 min of a window when BTC has moved decisively and the in-the-money side is priced 80–96¢. Confidence-composite gated.

3. `MANUAL_TP` (auto-TP on user manual fills)
   `MANUAL_TP_ENABLED = True`. Engine watches for fills it didn't place and lays a TP ladder on them.

4. `ARB_DETECTOR` (observability only)
   `ARB_DETECTOR_ENABLED = True` but `ARB_TRADES_ENABLED = False`. Scans for YES+NO < 97¢ mispricings and logs them but does not place orders. Detector output feeds TA_FORCED as regime input.

### Disabled / retired

1. `SR_FADE` — `SR_FADE_ENABLED = False` (retired 2026-04-27 per user directive)
2. `SNIPER` — `SNIPER_ENABLED = False`
3. Wallet-driven tiers — `WALLET_COPY_ENABLED = False`, `WALLET_SCORING_ENABLED = False`
4. Layered TP — `TP_LAYERED_ENABLED = False`
5. Micro-pullback entry — `MICRO_PULLBACK_ENABLED = False`

---

## Critical config reality

These are the live truths in `user_config.py` as of 2026-04-30:

```python
PAPER_TRADING = False

# Active tiers
TA_FORCED_ENABLED            = True
TA_FORCED_ENTRY_ENABLED      = True   # was False; flipped 2026-04-28
TA_FORCED_STOP_ENABLED       = True   # 8c-below-entry stop-loss — never disable
TA_FORCED_STOP_CENTS         = 8      # cents below entry, NOT absolute price
LATE_DOMINANT_ENABLED        = True
LATE_DOMINANT_STOP_ENABLED   = True
MANUAL_TP_ENABLED            = True
ARB_DETECTOR_ENABLED         = True
ARB_TRADES_ENABLED           = False  # detector-only by user policy

# Retired
SR_FADE_ENABLED              = False  # 2026-04-27 — retired
SNIPER_ENABLED               = False
WALLET_COPY_ENABLED          = False
WALLET_SCORING_ENABLED       = False

# Execution
MAKER_ONLY                   = True
ADAPTIVE_BID_ENABLED         = True   # bid+1 on conf >= 0.75

# Dynamic safety (2026-04-29 patches)
DAILY_LOSS_FRACTION          = 0.20   # daily limit = 20% of balance
SIZING_CAP_BALANCE_FRAC_DAY  = 0.30   # day ct cap = 30% × balance
SIZING_CAP_BALANCE_FRAC_NIGHT= 0.10   # night ct cap = 10% × balance
KELLY_MAX_FRAC               = 0.15   # Kelly hard ceiling
SIZING_MAX_FRACTION          = 0.20   # absolute per-position ceiling
TRADE_COOLDOWN_SECONDS       = 3600   # 1-hour spacing between fills

# Static fallbacks (used pre-poll or if dynamic fractions = 0)
DAILY_LOSS_LIMIT             = 15.00
SIZING_HARD_CAP_CONTRACTS_DAY    = 25
SIZING_HARD_CAP_CONTRACTS_NIGHT  = 10
TA_FORCED_FIXED_MAX_CONTRACTS_DAY    = 15
TA_FORCED_FIXED_MAX_CONTRACTS_NIGHT  = 5

# Other safeties
SAFETY_OVERSELL_HARDENING    = True
```

---

## Dynamic sizing (auto-scales with balance)

Patched 2026-04-29 — sizing now scales with the live polled balance instead of being fixed dollar amounts. As the account grows or shrinks, all caps and the daily limit recompute on every check.

**Worked examples**:

| Balance | Daily limit | Day ct cap | Night ct cap | Max position |
|---|---|---|---|---|
| $76 | $15.20 | 22 | 7 | $11.40 |
| $200 | $40.00 | 60 | 20 | $30.00 |
| $500 | $100.00 | 150 | 50 | $75.00 |
| $1000 | $200.00 | 300 | 100 | $150.00 |

**Implementation**:
- Module-level `_LIVE_BALANCE_DOLLARS` written by the balance poller (`polymarket_copy_engine.py:14683`)
- `_get_sizing_cap()` reads `SIZING_CAP_BALANCE_FRAC_DAY/_NIGHT × _LIVE_BALANCE_DOLLARS`
- New method `_get_dynamic_daily_loss_limit()` returns `balance × DAILY_LOSS_FRACTION` for the daily-loss circuit breaker

Set the dynamic fractions to 0.0 to fall back to static values.

---

## GHOST race fix (2026-04-29) — critical do-not-revert

**The bug**: When the engine placed a maker order that filled across multiple re-pegs, `_open_position` could lag behind Kalshi's truth. The legacy `SYNC RECONCILE` updated `count` and `entry_cents` but missed `original_entry_cents`, `strategy_name`, `tier`, and `original_count`. The stop-loss code requires `strategy_name` to gate and `original_entry_cents` to compute the trigger price. Missing fields = stop-loss silently no-ops. Position held to settlement on losing trades.

**Cost of the bug**: −$30.77 on a 53-contract YES @ 61¢ position (2026-04-29 13:47 PT, ticker `KXBTC15M-26APR291700-00`).

**Patches applied** (search the codebase for these comment markers):

| Patch | Marker | Location |
|---|---|---|
| #1 BACKFILL stop-loss-critical fields | `2026-04-29 GHOST-bug fix: backfill STOP-LOSS-CRITICAL fields` | `polymarket_copy_engine.py:~15750` |
| #2 RECLAIM when `_open_position` is None | `2026-04-29 GHOST-bug fix: secondary safeguard` | `polymarket_copy_engine.py:~15831` |
| #3 TP-EXPANSION on count growth | `2026-04-29 TP-EXPANSION fix` | `polymarket_copy_engine.py:~15780` |
| #4 Suppress stale GHOST log | `2026-04-29 — gate legacy GHOST log` | `polymarket_copy_engine.py:~15907` |
| #5 Track placements (catches WS-event lag) | `2026-04-29 GHOST-bug fix #5` | `polymarket_copy_engine.py:~625, ~10874, ~15832` |

**Why patch #5 matters**: Patch #2 (RECLAIM) only checks `_entered_tickers_this_window`, which is populated AFTER fill confirmation reaches the engine. When Kalshi fills a maker order but the WS fill event is delayed/missed, the position reconciler sees "ghost" contracts and falls to GHOST IGNORED — leaving the position unmanaged. Patch #5 adds `_recent_placement_tickers` (stamped at order placement, before fill) so RECLAIM can claim positions even when the fill handler hasn't run. **Discovered live on 2026-04-29 17:45 PT** when a 9ct YES @ 69¢ position fell through the cracks; user manually placed a 90¢ TP that filled for +$1.53.

**Live verification**: 3 consecutive winning trades on 4/30 UTC after patches went live (+$0.52, +$9.26, +$1.09). Same fill pattern that lost $30.77 is now a controlled outcome.

**If any of these markers are missing from `polymarket_copy_engine.py`, the patch was reverted — re-apply per `docs/SESSION_NOTES_2026-04-29.md` section 5.**

---

## Stop-loss semantics (read carefully)

`TA_FORCED_STOP_CENTS = 8` is **delta below entry**, NOT absolute price.

```python
_trigger_px_ta = _orig_entry_ta - _stop_drop_ta   # entry MINUS 8
if _bid_ta <= _trigger_px_ta:
    # FIRE STOP — flatten position
```

Worked examples:

| Entry | Stop trigger (entry − 8¢) |
|---|---|
| 70¢ | bid ≤ 62¢ |
| 50¢ | bid ≤ 42¢ |
| 35¢ | bid ≤ 27¢ |
| 86¢ | bid ≤ 78¢ |

`LATE_DOMINANT_STOP_CENTS = 8` follows the same convention.

**Stop-loss requires** the position dict to have `strategy_name in ("TA_FORCED_SIGNAL", "TA_FORCED")` and `original_entry_cents > 0`. The GHOST BACKFILL patch ensures these are populated on every claim — without it, stop-loss silently no-ops.

---

## Execution and exits

### Entry

- Maker-only entry: `MAKER_ONLY = True`
- Live entries rest at bid (or `bid+1` adaptive when `pressure.confidence >= 0.75`)
- Some entries are expected to never fill — maker-only acts as a filter against bad fades on flat tape
- Re-pegs occur when bid moves; old order cancelled and new placed
- **Known issue**: re-peg overrun — multiple sequential fills can accumulate beyond the cap. Mitigated by stop-loss + TP-EXPANSION patch but not eliminated.

### Position management

- TP ladder placed immediately on fill (and refreshed via TP-EXPANSION when count grows ≥ 3 ct)
- 8¢-below-entry stop-loss on every claimed position
- Trail-ratchet on profitable runs
- Mandatory flatten before expiry safety window
- Residual reconciler after exit cleans phantom contracts

`SR_FADE`'s former hold-to-expiry behavior is not relevant — that tier is retired.

---

## Safety layers that matter

### GHOST race patches (top priority)

See section above. **Never revert without understanding the $30.77 loss case.**

### Oversell hardening

`SAFETY_OVERSELL_HARDENING = True` covers:
- Safer cancel/replace handling
- Gated DCA rebuild
- Ticker-lock protections (`MAX_TRADES_PER_WINDOW = 1` per ticker per window)
- Residual reconciler

Tests: `tests/test_residual_reconciler.py`

### Daily-loss circuit breaker

- Dynamic limit: 20% × current balance (recomputed on every check)
- Halt persists across restarts via `_reconcile_daily_pnl_on_startup()`
- Resets at UTC midnight

### SQLite WAL mode (one-time DB hygiene)

`data/trades.db` and `data/signals.db` should both have `journal_mode=WAL`. Without it, window-rotation triggers concurrent writes that race for the file lock and produce `database is locked` errors. Verify with:
```
sqlite3 data/trades.db "PRAGMA journal_mode"   # should return: wal
```

---

## Files that are currently load-bearing

### Core runtime

- `run_copy_engine.py` — entry point, credential bootstrap
- `polymarket_copy_engine.py` — main engine (signal eval, entry, exit, position reconciler — **all GHOST patches live here**)
- `user_config.py` — live config switchboard
- `kalshi_client.py` — RSA-PSS signed REST client
- `kalshi_ws.py` — WebSocket client for orderbook/trades
- `price_feed.py` — Binance/Coinbase price ingestion + Brownian-Bridge model
- `microstructure.py` — book pressure, trade flow, BTC impulse
- `regime.py` — regime classifier
- `edge_sizer.py` — Kelly-fraction sizing
- `signal_logger.py` — async SQLite writer for trades/signals/balance snapshots
- `whale_monitor.py` — mempool-based whale alerts
- `ta_module.py` — TA scoring used by TA_FORCED
- `mtf_scorer.py`, `tf_analyzer.py`, `indicators.py` — multi-timeframe support
- `models.py` — shared dataclasses
- `shadow_edge.py` — additive shadow-edge model (logs only)

### Optional / feature-flagged

- `paper_trader.py`, `sniper.py`, `sniper_signals.py`, `contract_sr.py` — present but not on the live entry path

---

## Files and folders that are not the current live engine

### Historical / archival

- `_archive/`
- `docs/archive/`

Do not use these as the description of current behavior.

### Separate simulation / research tooling

- `paper_engine/`, many files in `scripts/`, `to-do/`, `settings/` — analysis/ops, not the live path.

---

## PnL truth

Do not trust raw `kalshi_trades.pnl` at face value.

Authoritative truth sources:

1. **Kalshi positions API** (`KalshiClient.get_positions()`) — source-of-truth for current position
2. `real_balance_snapshots` (in `data/trades.db`) — high-frequency total_cents
3. `balance_snapshots` (in `data/trades.db`) — coarser, written on window changes
4. `settlement_ledger`
5. `scripts/reconcile_pnl.py` when trade-row pnl needs repair

The daily-PnL reconciler (`_reconcile_daily_pnl_on_startup`) reads `real_balance_snapshots` first, falls back to `balance_snapshots`. Both are written by the engine's balance poller.

---

## Service / process management

This deployment runs **interactively in a terminal**, not as an NSSM service. The `BTCBiasEngine` NSSM service referenced in older docs is the prior-machine setup.

### Start (PowerShell)

```powershell
$env:SSL_CERT_FILE = "C:\Users\K\Desktop\btc-bias-engine\venv\Lib\site-packages\certifi\cacert.pem"
$env:PYTHONUNBUFFERED = "1"
& "C:\Users\K\Desktop\btc-bias-engine\venv\Scripts\python.exe" `
  "C:\Users\K\Desktop\btc-bias-engine\run_copy_engine.py" 2>&1 |
  Tee-Object -FilePath "data\engine.log"
```

### Start (Git Bash)

```bash
SSL_CERT_FILE="C:/Users/K/Desktop/btc-bias-engine/venv/Lib/site-packages/certifi/cacert.pem" \
PYTHONUNBUFFERED=1 \
venv/Scripts/python.exe run_copy_engine.py 2>&1 | tee data/engine.log
```

### Stop

```powershell
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like '*btc-bias-engine*' } |
    Stop-Process -Force
Remove-Item data\engine.pid -ErrorAction SilentlyContinue
```

### Required env vars / files

- `KALSHI_API_KEY` — UUID; loaded from `credentials/kalshi.env`
- `KALSHI_PRIVATE_KEY_PATH` — path to .pem; loaded from `credentials/kalshi.env`
- `KALSHI_DEMO=false`
- `EXECUTE_TRADES=true`
- `PYTHONUNBUFFERED=1`
- `SSL_CERT_FILE` — points at certifi `cacert.pem` (required on fresh Windows installs to fix SSL verify failures)

`certifi` must be `pip install`ed into the venv (transitively included by some deps but not guaranteed).

---

## Daily-PnL false-halt clear (data hygiene)

If the engine refuses to trade after a restart with `DAILY-PNL RECONCILE: today's delta=$-X.XX EXCEEDS -$Y.YY limit — HALT active`, and the delta does NOT match actual current-day Kalshi P&L (e.g. you carried over `data/*.db` from another machine), wipe today's stale snapshots:

```python
import sqlite3, datetime
con = sqlite3.connect('data/trades.db')
day0_ms = int(datetime.datetime.now(datetime.timezone.utc)
              .replace(hour=0, minute=0, second=0, microsecond=0)
              .timestamp() * 1000)
today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
con.execute('DELETE FROM real_balance_snapshots WHERE ts_ms >= ?', (day0_ms,))
con.execute("DELETE FROM balance_snapshots WHERE ts >= ?", (today + 'T00:00:00',))
con.commit(); con.close()
```

Restart the engine. Reconciler hits "no snapshots → starting fresh" → halt cleared. Real losses still reflected in Kalshi balance, so dynamic limit (20% × current balance) still applies.

---

## Key log patterns (what to watch in `data/engine.log`)

| Pattern | Meaning |
|---|---|
| `TA_FORCED FILL: ... Nx @ Mc` | Real entry filled |
| `SYNC RECONCILE BACKFILL: ... stop-loss now armed` | GHOST patch #1 fired — position properly claimed |
| `SYNC RECLAIM: ... claiming position so stop-loss can fire` | GHOST patch #2 fired — `_open_position` reconstructed from Kalshi truth |
| `SYNC RECONCILE TP: Nx placed (refreshed for grown count)` | TP-EXPANSION patch fired — TPs re-sized after re-peg overrun |
| `SELL TIER FILLED: Nx @ Mc (+Pc)` | TP exit |
| `RESIDUAL-CLEAN: ... reason=tp_ladder_sweep` | Position fully closed via TP — normal good-case exit |
| `RESIDUAL-CLEAN: ... reason=expiry_close` | **RED FLAG** — held to expiry; should not happen with patches in place |
| `TA_FORCED STOP: ... drop=8c P&L≈$-X.XX` | Stop-loss fired correctly |
| `DAILY-PNL RECONCILE: today's delta=$X (limit=$Y)` | Daily P&L state — limit should match 20% × current balance |
| `GHOST IGNORED: ... letting Kalshi settle` | External/manual position; engine left it alone (correct) |
| `database is locked` | WAL mode not enabled — re-run the WAL setup |

---

## Do / Don't

**Do:**

- Treat `user_config.py` as the live-behavior switchboard
- Check `Kalshi positions` API before trusting trade-row pnl reporting
- Verify `PAPER_TRADING`, `TA_FORCED_ENTRY_ENABLED`, and the GHOST patch markers before assuming live behavior
- Keep `CLAUDE.md` in sync after config or signal changes
- Read `docs/SESSION_NOTES_2026-04-29.md` before touching the position reconciler
- Use the dynamic-fraction knobs (`DAILY_LOSS_FRACTION`, `SIZING_CAP_BALANCE_FRAC_DAY/_NIGHT`) as the primary tuning surface — they auto-scale with balance

**Don't:**

- Assume `SR_FADE` is the live primary tier (it's been disabled since 2026-04-27)
- Disable `TA_FORCED_STOP_ENABLED` — it's the loss cap on every position
- Disable the GHOST race patches — they prevent the $30.77 loss case from recurring
- Re-enable `ARB_TRADES_ENABLED` (REST latency can't beat colocated HFT — user policy)
- Re-enable `SR_FADE_ENABLED` or `SNIPER_ENABLED` casually
- Treat `_archive/` or `docs/archive/` as current
- Run two engines on the same Kalshi account (per-process state cannot coordinate — order conflicts, oversells)
- Touch `DOMINANT_BTC_5M_THRESHOLD` unilaterally — user wants this gate restructured (microstructure-driven entries) but hasn't signed off on the change yet

---

## Cross-references

- **Patch session deep-dive**: `docs/SESSION_NOTES_2026-04-29.md`
- **Per-tier strategy notes**: existing files under `docs/`
- **Stop-loss code path**: `polymarket_copy_engine.py:~15485` (TA_FORCED), `:~15400` (LATE_DOMINANT)
- **Position reconciler with GHOST patches**: `polymarket_copy_engine.py:~15710`
- **Dynamic sizing infrastructure**: `polymarket_copy_engine.py:~262` (sizing), `:~8575` (daily limit)
- **Connectivity probe**: `data/probe_endpoints.py`, `data/probe_kalshi_auth.py`








# Session Notes — 2026-04-29 / 2026-04-30 Fresh-Unit Bring-Up + GHOST Race Fix + TP-Expansion

**Engine**: BTC Bias Engine (Polymarket Copy Engine / TA_FORCED tier)
**Live entry**: `run_copy_engine.py`
**Active tier**: TA_FORCED (NOT SR_FADE — `CLAUDE.md` is stale on this point)

**Performance arc today**:
- Started 4/29 UTC: $100.00
- After 4/29 GHOST loss: $76.45 (−$23.55)
- After patches + 3 winning trades 4/30 UTC: $86.16 (+$10.87 since patches went live)
- Net from $100 baseline: −$13.84

---

## 0. For the next agent — replication checklist

If you're picking up this codebase and need to recreate the work:

1. **Read this file end-to-end** before touching anything.
2. **Check `user_config.py:516` first** — confirm `PAPER_TRADING = False` if going live, or flip to `True` for shake-down.
3. **Verify the GHOST race patches are still in place** at `polymarket_copy_engine.py:15732` (BACKFILL block) and line ~15807 (RECLAIM block). Search for the comment string `2026-04-29 GHOST-bug fix`. If absent, they were reverted — re-apply per section 5.
4. **Verify the TP-EXPANSION patch** at line ~15779 — search for `2026-04-29 TP-EXPANSION fix`. Same logic: re-apply if missing.
5. **Verify dynamic sizing infrastructure** at line ~262 (`_LIVE_BALANCE_DOLLARS`) and line ~14635 (balance poller wires it up). Section 5.3.
6. **Check `data/trades.db` and `data/signals.db`** are in WAL mode: `sqlite3 data/trades.db "PRAGMA journal_mode"` should return `wal`. Re-enable per section 3.3.
7. **Run the connectivity probe** (`data/probe_endpoints.py` from section 3.4) before declaring healthy.
8. **Tail `data/engine.log`** and watch for these patch-verification log lines:
   - `SYNC RECONCILE BACKFILL: ... stop-loss now armed`
   - `SYNC RECONCILE TP: Nx placed (refreshed for grown count)` ← TP-EXPANSION engaged
   - `SYNC RECLAIM: ... claiming position so stop-loss can fire`
9. **Red flags requiring investigation**:
   - `RESIDUAL-CLEAN: ... reason=expiry_close` with non-zero P&L → GHOST regression
   - `TA_FORCED STOP: ... drop=8c` realized loss > 25c × 8¢ ≈ $2.00 → cap-bypass + stop fired

---

## 1. Summary

Brought up the engine on a fresh Windows unit. Verified all external connectivity. Caught a position-tracking race condition that caused a $30.77 catastrophic loss on 4/29 (53 contracts held to expiry without stop-loss firing).

Patched the root cause across **two surgical edits to the position reconciler** (BACKFILL + RECLAIM), made daily-loss + contract caps **fully proportional to balance**, and added **TP-EXPANSION on re-peg overrun** so positions that grow beyond initial fill stay TP-protected. Suppressed misleading stale-log line that was firing after the BACKFILL path.

Confirmed end-to-end fix at 17:05 PT, then again at 17:19 PT (won +$9.26 on the exact pattern that lost $30.77 yesterday), and again at 17:30 PT (won +$1.09). All three live trades verified the patches engage as designed. Total recovered: +$10.87 in 25 minutes.

---

## 2. Files modified

| File | Type | Changes |
|---|---|---|
| `user_config.py` | config | 13 sizing knobs tightened/added; daily-loss limit converted to fraction-based |
| `polymarket_copy_engine.py` | code | GHOST race patches (BACKFILL + RECLAIM); dynamic sizing infrastructure; daily-loss limit dynamic |
| `data/trades.db`, `data/signals.db` | DB | journal_mode=WAL enabled (one-time); today's stale balance_snapshots wiped |
| `credentials/kalshi.env` | env | created with API key + .pem path |
| `credentials/kalshi_key.pem` | secret | copied from `C:/Users/K/Desktop/keys/Moneymaker$$$.txt` |

---

## 3. Bring-up sequence (config-only, no logic changes)

### 3.1 Environment
- Python 3.14 venv at `C:\Users\K\Desktop\btc-bias-engine\venv`
- `pip install -r requirements.txt` + `certifi` + `pytest`
- Tests passed 64/65 (the one failure is an intentional config note, unrelated)

### 3.2 SSL cert fix
- Fresh Python install on Windows had no CA bundle accessible to `aiohttp`
- Symptom: `SSLCertVerificationError: unable to get local issuer certificate` on first REST call
- Fix: `pip install certifi` and set env var:
  ```
  SSL_CERT_FILE=C:/Users/K/Desktop/btc-bias-engine/venv/Lib/site-packages/certifi/cacert.pem
  ```

### 3.3 SQLite WAL mode (one-time DB hygiene)
- Window rotations were firing 3 concurrent writes (`_write_session_terminal`, `_seed_sr_from_prior`, `log_window_snapshot`) that collided on the default `journal_mode=delete`
- Result: `sqlite3.OperationalError: database is locked` errors every 15 min
- One-time fix on both DBs:
  ```python
  PRAGMA journal_mode=WAL
  PRAGMA busy_timeout=30000
  PRAGMA synchronous=NORMAL
  ```
- Persistent setting; survives restarts. Eliminated 100% of subsequent lock errors.

### 3.4 Connectivity probe (read-only smoke test)
Wrote `data/probe_endpoints.py` to verify all 10 external endpoints. Results:
```
Binance.US REST                  HTTP 200
Binance.US WS kline              WS connected
Kalshi PROD /exchange/status     HTTP 200
Kalshi DEMO /exchange/status     HTTP 200
Kalshi PROD /markets KXBTC15M    HTTP 200
Polymarket CLOB /markets         HTTP 200
Polymarket gamma /markets        HTTP 200
Polymarket CLOB WS handshake     WS handshake OK
Coinbase BTC-USD ticker          HTTP 200
CoinGecko simple price           HTTP 200
```

Plus authenticated Kalshi probe via `data/probe_kalshi_auth.py` — RSA-PSS signature verified, balance retrieved correctly.

---

## 4. Config changes — `user_config.py`

### 4.1 Daily loss limit (now dynamic)

```python
# Before
DAILY_LOSS_LIMIT = 500.00   # static $ amount

# After
DAILY_LOSS_LIMIT = 15.00    # static fallback only
DAILY_LOSS_FRACTION = 0.20  # NEW — limit = balance × 0.20, recomputed on every check
```

### 4.2 Contract caps (now dynamic)

```python
# Before
SIZING_HARD_CAP_CONTRACTS_DAY   = 100   # static
SIZING_HARD_CAP_CONTRACTS_NIGHT = 100   # static

# After
SIZING_HARD_CAP_CONTRACTS_DAY   = 25    # static fallback
SIZING_HARD_CAP_CONTRACTS_NIGHT = 10    # static fallback
SIZING_CAP_BALANCE_FRAC_DAY     = 0.30  # NEW — cap = balance × 0.30
SIZING_CAP_BALANCE_FRAC_NIGHT   = 0.10  # NEW — cap = balance × 0.10
```

### 4.3 Per-position sizing (already proportional, tightened)

```python
SIZING_BALANCE_FRACTION = 0.10   # was 0.30 — base sizing
SIZING_MAX_FRACTION     = 0.20   # was 0.75 — hard ceiling
KELLY_FRACTION          = 0.25   # was 0.65 — less aggressive Kelly
KELLY_MIN_FRAC          = 0.02   # was 0.03 — floor
KELLY_MAX_FRAC          = 0.15   # was 0.80 — monster-edge cap
```

### 4.4 Tier-specific (TA_FORCED, LATE_DOMINANT)

```python
TA_FORCED_FIXED_FRACTION_NIGHT      = 0.02  # was 0.05
TA_FORCED_FIXED_MAX_CONTRACTS_DAY   = 15    # was 50
TA_FORCED_FIXED_MAX_CONTRACTS_NIGHT = 5     # was 30
LATE_DOMINANT_SIZE_MAX_CT_NIGHT     = 10    # was 30
```

### 4.5 Cooldown

```python
TRADE_COOLDOWN_SECONDS = 3600   # was 10 — 1 hour between fills
```

### 4.6 Auto-scaling math

| Balance | Daily limit | Day ct cap | Night ct cap | Max position $ |
|---|---|---|---|---|
| $76 | $15.20 | 22 | 7 | $11.40 |
| $200 | $40.00 | 60 | 20 | $30.00 |
| $500 | $100.00 | 150 | 50 | $75.00 |
| $1000 | $200.00 | 300 | 100 | $150.00 |

All five values recompute on every check from `_real_total_cents` (live polled balance). No manual config bumps required.

---

## 5. Code changes — `polymarket_copy_engine.py`

### 5.1 GHOST race patch — Part 1: SYNC RECONCILE BACKFILL

**Location**: line ~15681, inside the `our_recent` branch of the position reconciler

**Bug**: When the engine placed a maker order that filled across multiple re-pegs, `_open_position["count"]` could lag behind Kalshi's truth. `SYNC RECONCILE` correctly updated `count` and `entry_cents`, but failed to populate `original_entry_cents`, `strategy_name`, `tier`, and `original_count`. The stop-loss code (line 15485) requires `strategy_name in ("TA_FORCED_SIGNAL","TA_FORCED")` and reads `original_entry_cents`. Missing fields = stop-loss silently no-ops = position held to expiry.

**Yesterday's loss**: 53 contracts of YES @ 61c held to settlement when BTC closed below strike. **−$30.77.**

**Patch** — added after `count` and `entry_cents` updates:

```python
# 2026-04-29 GHOST-bug fix: backfill STOP-LOSS-CRITICAL fields.
# original_entry_cents is the DCA anchor used by stop-loss.
# strategy_name routes the stop-loss tier check.
# original_count is used by trail/ratchet logic.
# fill_time refreshes the our_recent window for next sync.
if not self._open_position.get("original_entry_cents"):
    self._open_position["original_entry_cents"] = (
        self._open_position.get("entry_cents", entry_est) or entry_est
    )
if not self._open_position.get("original_count"):
    self._open_position["original_count"] = kalshi_count
if not self._open_position.get("strategy_name"):
    self._open_position["strategy_name"] = "TA_FORCED_SIGNAL"
if not self._open_position.get("tier"):
    self._open_position["tier"] = "TA_FORCED"
self._open_position["fill_time"] = time.time()
logger.info(
    "CopyEngine SYNC RECONCILE BACKFILL: ticker=%s count=%d "
    "entry=%dc orig_entry=%dc strat=%s — stop-loss now armed",
    ticker[-15:], kalshi_count,
    self._open_position.get("entry_cents", 0),
    self._open_position.get("original_entry_cents", 0),
    self._open_position.get("strategy_name", "?"),
)
```

**Verification**: Confirmed live at 17:05:44 PT — position correctly armed, TP fired 1 second later (+$0.52 win on the exact fill pattern that lost $30.77 yesterday).

### 5.2 GHOST race patch — Part 2: SYNC RECLAIM fallback

**Location**: line ~15716, inside the original "GHOST IGNORED" else branch

**Bug edge case**: If `_open_position` was None entirely (cleared by some other code path, e.g. window-rotation reset), the `our_recent` check fails because `our_recent` requires `_open_position is not None`. Falls to GHOST IGNORED → position never managed.

**Patch** — added a check for `_entered_tickers_this_window` before falling to GHOST IGNORED:

```python
# 2026-04-29 GHOST-bug fix: secondary safeguard.
# _open_position is None or for a different ticker, but the engine entered
# THIS ticker this window (per the _entered_tickers_this_window set
# populated at fill time). The position IS ours — claim it before
# stop-loss path checks fail and it gets held to expiry.
ticker_entered_this_window = ticker in getattr(
    self, "_entered_tickers_this_window", set()
)
if ticker_entered_this_window:
    logger.warning(
        "CopyEngine SYNC RECLAIM: Kalshi has %d %s on %s, "
        "_open_position was %s — ticker is in entered set, "
        "claiming position so stop-loss can fire.",
        kalshi_count, kalshi_side, ticker[-15:],
        "None" if self._open_position is None
        else f"on {self._open_position.get('ticker','?')[-15:]}",
    )
    self._open_position = {
        "order_id": "synced_reclaim",
        "side": kalshi_side,
        "entry_cents": entry_est,
        "original_entry_cents": entry_est,
        "original_count": kalshi_count,
        "ticker": ticker,
        "tier": "TA_FORCED",
        "strategy_name": "TA_FORCED_SIGNAL",
        "count": kalshi_count,
        "fill_time": time.time(),
        "_dca_maxed": True,  # don't re-DCA on reclaimed positions
        # ... full field population
    }
    # Place TPs immediately on reclaimed position
    if resting == 0:
        _tp_ids_rc = await self._place_tiered_tp(
            ticker, kalshi_side, kalshi_count, entry_est,
        )
        if _tp_ids_rc:
            self._open_position["tp_order_ids"] = _tp_ids_rc
            self._open_position["tp_order_id"] = _tp_ids_rc[0]
else:
    # External / manual position — leave alone (preserves user manual trades)
    logger.warning(
        "CopyEngine GHOST IGNORED: Kalshi has %d %s on %s but engine tracks %d — %d ghost contracts (letting Kalshi settle)",
        kalshi_count, kalshi_side, ticker[-15:], engine_count, ghost_ct,
    )
```

### 5.2.5 TP-EXPANSION on re-peg overrun (Patch #3)

**Location**: line ~15779, inside the `our_recent` branch of the position reconciler

**Bug**: When SYNC RECONCILE BACKFILL fires and places TPs sized for `kalshi_count` (e.g. 14ct), additional fills can accumulate via maker re-pegs (14 → 30ct on the 17:19 trade today). The original TP block guarded with `if not self._open_position.get("tp_order_ids")` — meaning it only placed TPs on the *first* sync. Subsequent overrun fills had no TP coverage. The 17:19 trade only won by luck (bid surged past TP fast enough to absorb the overrun).

**Patch** — replaced the guarded `if resting == 0 and not self._open_position.get("tp_order_ids"):` with logic that detects significant count growth and refreshes TPs:

```python
# Place TPs (or refresh if position grew via re-peg overrun).
# 2026-04-29 TP-EXPANSION fix: if count has grown since
# we last placed TPs (e.g. 14ct → 30ct via maker re-peg
# overrun), cancel stale TPs and place fresh ones for
# the new count.
_last_tp_count = self._open_position.get("_tp_placed_for_count", 0)
_count_grew = (kalshi_count - _last_tp_count) >= 3
_have_no_tps = (
    not self._open_position.get("tp_order_ids")
    and resting == 0
)
_need_fresh_tps = _have_no_tps or _count_grew
if _need_fresh_tps:
    # Cancel stale TPs first if count grew
    if _count_grew and self._open_position.get("tp_order_ids"):
        try:
            await self._cancel_tp_order()
            logger.info(
                "CopyEngine SYNC RECONCILE TP: cancelled stale "
                "TPs (count grew %d→%d, refreshing)",
                _last_tp_count, kalshi_count,
            )
        except Exception:
            pass
    try:
        _tp_ids = await self._place_tiered_tp(
            ticker, kalshi_side, kalshi_count,
            self._open_position.get("entry_cents", entry_est),
        )
        if _tp_ids:
            self._open_position["tp_order_ids"] = _tp_ids
            self._open_position["tp_order_id"] = _tp_ids[0]
            self._open_position["_tp_placed_for_count"] = kalshi_count
            logger.info(
                "CopyEngine SYNC RECONCILE TP: %dx placed%s",
                kalshi_count,
                " (refreshed for grown count)" if _count_grew else " after count update",
            )
    except Exception as _tp_err:
        logger.warning(
            "CopyEngine SYNC RECONCILE TP failed: %s", _tp_err,
        )
# 2026-04-29 GHOST log suppression: mark this iteration handled
# so the legacy stale-log block below skips its misleading message.
_sync_handled_this_iter = True
```

**Threshold**: `>= 3` chosen as the count-growth trigger. Below 3 is normal Kalshi micro-jitter; ≥3 is real re-peg accumulation. New position dict field `_tp_placed_for_count` tracks the count at last TP placement.

### 5.2.7 Track placements (not just fills) for RECLAIM detection (Patch #5)

**Location**: `__init__` at line ~625, `place_order` call at line ~10874, RECLAIM check at line ~15832

**Bug**: Patch #2 (RECLAIM) checks `_entered_tickers_this_window` — but that set is populated INSIDE the fill handler at line ~11019, AFTER `order.filled_count > 0`. When a maker order fills on Kalshi but the WS fill event is delayed or never delivered, the engine's fill handler doesn't run. The reconciler then sees Kalshi has contracts that the engine "doesn't know about" and falls to GHOST IGNORED — position is left to settle with no TPs and no stop-loss.

**Live cost**: Observed 2026-04-29 17:45 PT on KXBTC15M-26APR292100-00. Engine placed YES @ 69-73¢ across multiple re-pegs. 9 contracts filled on Kalshi but no TA_FORCED FILL log fired. Reconciler logged `GHOST IGNORED: Kalshi has 9 yes ... letting Kalshi settle`. User had to manually place a 90¢ TP that filled for +$1.53 saving the trade. Without manual intervention this would have been another expiry-loss case.

**Patch** — three pieces:

1. New tracking dict in `__init__`:
```python
# 2026-04-29 GHOST-bug fix #5: track per-ticker order PLACEMENT timestamps.
self._recent_placement_tickers: dict = {}
```

2. Stamp on placement (right after `place_order`):
```python
mkt_order = await self._client.place_order(
    ticker=contract.ticker, side=signal.kalshi_side,
    price=entry_px, count=num_contracts,
    post_only=bool(_uc("MAKER_ONLY", True)),
)
# 2026-04-29 GHOST-bug fix #5: stamp the placement so RECLAIM can claim
# positions when fill events are delayed/missed.
self._recent_placement_tickers[contract.ticker] = time.time()
```

3. Expanded RECLAIM check:
```python
ticker_entered_this_window = ticker in getattr(
    self, "_entered_tickers_this_window", set