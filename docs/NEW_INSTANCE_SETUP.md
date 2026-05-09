# New Instance Setup — for AI agents and humans

**Last updated**: 2026-05-09 PT — post MOMENTUM_SCALP overhaul + 5 paralysis fixes.
**Reading prerequisite**: `CLAUDE.md` (root). Read it first.

This guide walks through cleanly setting up the BTC Bias Engine on a new
Windows machine, validating it works, and flipping it to live trading.
**The doc is rewritten to current state — older versions described
DIRECTION / FVG / TA_FORCED as live; those tiers are all retired or
feature-flagged off as of 2026-05-09.**

---

## 1. What's actually live (2026-05-09)

**One active entry tier**: `MOMENTUM_SCALP` (`MOMENTUM_SCALP_ENABLED = True`).

The cascade documented in older `CLAUDE.md` versions
(UNIFIED → DIRECTION → BB_PURE → PENNY) is **not** what runs today —
every one of those tiers is gated off in `user_config.py`. The only
entry path firing in production right now is the SCALP MARKET handler
in `polymarket_copy_engine.py` (~line 5050+).

### Strategy summary

At every new 15-minute Kalshi BTC binary window:

1. **Pick the favorite side** — whichever side has the higher ask
   (yes_ask vs no_ask). In thin overnight liquidity this side typically
   has the directional momentum.
2. **IOC at ask + slip** — `SCALP_REENTRY_SLIP_C` cents above ask, capped
   at 95c. 5ct per fill (`SCALP_CONTRACTS = 5`).
3. **Manage with a velocity-aware exit layer**:
   - `SCALP BTC-TRAIL` — exits when BTC retraces by a strike-distance-
     scaled threshold from its hwm. Defaults: `clamp($25, $100, 0.4 ×
     abs(btc - strike))`. The trail is tight near the strike (where
     contract delta is highest) and loose deep ITM (where BTC moves
     barely affect contract price).
   - `SCALP TRAIL` — bid-cents trail (phase-based: 15 / 10 / 5 / 3c).
   - `SCALP LOSS-CUT` — exits if bid drops 15c+ below entry.
   - `SCALP PRE-EXPIRY` — sells profitably with ≤60s left.
4. **Inverse re-entry on trail exit** — when BTC-TRAIL or bid-TRAIL
   fires, the engine evaluates `SCALP REVERSAL-SCORE` (4 components:
   time, BTC, book, velocity); if total > 0.5, IOC the *opposite* side.
   This is the "follow the BTC trend, flip when it reverses" loop.
5. **Indefinite flips per window** — bounded by
   `MAX_RISK_PER_WINDOW_DOLLARS = $15`. Typical: 1-3 flips per window
   in a trending tape; 0 flips in a flat tape (position rides to
   pre-expiry exit or settles).

### Active config (live values)

```python
# Master toggles
PAPER_TRADING                          = False
MOMENTUM_SCALP_ENABLED                 = True
SAFETY_OVERSELL_HARDENING              = True
ADMISSION_FILTER_ENABLED               = True

# All other entry tiers — OFF (do not re-enable without explicit user direction)
UNIFIED_SCORER_ENABLED                 = False
DIRECTION_STRATEGY_ENABLED             = False
BB_PURE_MODE                           = False
BB_MOMENTUM_ENABLED                    = False
PENNY_MODE_ENABLED                     = False
TA_FORCED_ENTRY_ENABLED                = False
LATE_DOMINANT_ENABLED                  = False
SR_FADE_ENABLED                        = False
PAPER_FVG_LIVE_MODE                    = False
SNIPER_ENABLED                         = False
WALLET_COPY_ENABLED                    = False
ATM_REVERSION_ENABLED                  = False
ARB_DETECTOR_ENABLED                   = False
MICRO_PULLBACK_ENABLED                 = False
TP_LAYERED_ENABLED                     = False
SCALP_DCA_ENABLED                      = False

# SCALP knobs
SCALP_CONTRACTS                        = 5
SCALP_REENTRY_SLIP_C                   = 5
SCALP_MAX_RISK_DOLLARS                 = 5.0
SCALP_PLACE_MAX_AGE_S                  = 30.0
SCALP_IOC_COOLDOWN_S                   = 5.0
SCALP_MAX_CONTRACTS_WINDOW             = 10
SCALP_PRE_IOC_TICKER_LOCK_CHECK        = True   # 2026-05-08 retry-on-same-ticker fix
SCALP_INVERSE_REENTRY_ENABLED          = True
SCALP_REVERSAL_THRESHOLD               = 0.50

# SCALP exit layer (cents-trail)
SCALP_TRAIL_C                          = 5
SCALP_TRAIL_PHASE1_C                   = 15
SCALP_TRAIL_PHASE2_C                   = 10
SCALP_TRAIL_PHASE3_C                   = 5
SCALP_TRAIL_PHASE4_C                   = 3
SCALP_MAX_LOSS_C                       = 15
SCALP_NEAR_CERTAIN_C                   = 90
SCALP_PRE_EXPIRY_S                     = 60

# SCALP exit layer (BTC-trail) — 2026-05-09 dynamic strike-distance scaling
SCALP_BTC_TRAIL_DOLLARS                = 60.0   # fallback when DYNAMIC=False
SCALP_BTC_TRAIL_DYNAMIC_ENABLED        = True
SCALP_BTC_TRAIL_DYNAMIC_K              = 0.4
SCALP_BTC_TRAIL_DYNAMIC_MIN            = 25.0
SCALP_BTC_TRAIL_DYNAMIC_MAX            = 100.0

# Universal safety layer (cap-per-window, applies cross-tier)
MAX_RISK_PER_WINDOW_DOLLARS            = 15.0
MAX_ENTRIES_PER_WINDOW                 = 99    # relaxed for multi-flip
MAX_TRADES_PER_SESSION_TICKER          = 99    # relaxed 2026-05-08
```

### Recent commit history (last 10 — all in master)

| Commit | Description |
|---|---|
| `53aaf55` | Fix E — ghost-desync clear must skip current-window ticker |
| `96c6bb2` | Fix A/B/C/D — STARTUP RECOVERY → SCALP collision paralysis (4 fixes) |
| `f4a0853` | Dynamic BTC trail scaled by strike distance |
| `97cc419` | docs: monitoring notes |
| `13a9b8f` | Fix SCALP IOC fill detection: `count_filled` → `filled_count` |
| `ea32bb5` | REVERT detection-disable experiment + fix `_window_locked` clear bug |
| `56acf0b` | Disable PROTECTIVE auto-TP via `_hold_to_settle` + SCALP pre-IOC ticker lock |
| `224c0d7` | `INVERSE_REENTRY_ON_CLOSE`: +4c IOC opposite-side buy on close |
| `5c15d90` | Disable SYNC RECLAIM auto-TP + SCALP→HOLD UPGRADE |
| `f8b4528` | ADMISSION FILTER + MOMENTUM SCALP tier (initial) |

---

## 2. Prerequisites

- **Windows 10 / 11** (NSSM-only, not Linux/Mac)
- **Python 3.11+** on PATH
- **Admin PowerShell** for service management
- **Kalshi account** with API credentials:
  - `KALSHI_API_KEY` — UUID
  - `KALSHI_PRIVATE_KEY_PATH` — RSA private key in PEM format
- **Funded Kalshi balance** (start with $20-50 — engine assumes small bankroll)
- **NSSM** — installed via `scripts/setup.ps1`, or manually from
  [nssm.cc](https://nssm.cc)

---

## 3. Clone and inspect

```powershell
cd C:\Trading
git clone https://github.com/ThisUsernamesTaken/yuh.git btc-bias-engine
cd btc-bias-engine
```

**Read in this order before installing**:

1. `CLAUDE.md` — operator-facing source of truth. **If CLAUDE.md and
   the code disagree, the code wins.** As of 2026-05-09 the file
   describes a multi-tier cascade that is mostly disabled in
   `user_config.py` — the live state is single-tier MOMENTUM_SCALP
   per Section 1 above.
2. `polymarket_copy_engine.py` — main engine. Key sections:
   - `_check_window_safety` / `_record_window_fill` (~line 4676) —
     universal $15/window cap
   - SCALP MARKET handler (~line 5050) — entry path
   - `_scalp_fire_exit` (~line 5800) — exit + INVERSE_REENTRY hook
   - SCALP BTC-TRAIL (~line 5681) — dynamic-trail evaluator
   - SCALP INVERSE re-entry helper (~line 5980) — opposite-side IOC
   - STARTUP RECOVERY (~line 1490-1591) — Fix A tier-aware routing
   - `_on_new_poly_window` (~line 2840) — Fix B per-window lock preserve
     + Fix E ghost-desync skip
   - `_maintain_protective_order` (~line 21000) — Fix D 404 handling
3. `user_config.py` — every live-behavior knob in one file. SCALP
   section starts ~line 2557.

If any doc contradicts the code, **the code wins**.

---

## 4. Python environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` is intentionally tiny (5 deps): `aiohttp`,
`aiosqlite`, `cryptography`, `psutil`, `websockets`. If any sub-dep
fails to compile on Windows, install MSVC build tools:
`winget install Microsoft.VisualStudio.2022.BuildTools`.

---

## 5. Credentials

```powershell
mkdir credentials -ErrorAction SilentlyContinue
notepad credentials\kalshi.env
```

Contents (replace with your actual values):

```
KALSHI_API_KEY=your-uuid-here
KALSHI_PRIVATE_KEY_PATH=C:\Trading\btc-bias-engine\credentials\kalshi_key.pem
EXECUTE_TRADES=true
KALSHI_DEMO=false
```

Save your PEM private key to the path above. Restrict file permissions
to your user only.

**Never commit `credentials/`** — it's in `.gitignore`. Sanity check:

```powershell
git check-ignore credentials\kalshi.env
# expected: credentials/kalshi.env
```

---

## 6. First-run smoke test (PAPER MODE)

Before the engine touches a real Kalshi order, force paper mode in
`user_config.py`:

```python
PAPER_TRADING                = True
MOMENTUM_SCALP_ENABLED       = True   # paper sim still exercises the SCALP path
```

Verify credentials load:

```powershell
.\venv\Scripts\python -c "
import asyncio, os, sys; sys.path.insert(0, '.')
from kalshi_client import KalshiClient
async def main():
    with open('credentials/kalshi.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k,v = line.strip().split('=',1)
                os.environ[k.strip()] = v.strip()
    pem = open(os.environ['KALSHI_PRIVATE_KEY_PATH']).read()
    async with KalshiClient(os.environ['KALSHI_API_KEY'], pem) as c:
        bal = await c.get_balance()
        print(f'BAL: \${bal.balance/100:.2f}')
        positions = await c.get_positions()
        flat = all(int(p.get('position', 0)) == 0 for p in positions or [])
        print(f'Position: {\"FLAT\" if flat else \"NOT FLAT\"}')
asyncio.run(main())
"
```

Run the test suite (some pre-existing failures are expected — see below):

```powershell
.\venv\Scripts\python -m pytest tests/ -q `
    --ignore=tests/test_bias_engine.py `
    --ignore=tests/test_consensus.py `
    --ignore=tests/test_phase3.py `
    --ignore=tests/test_strategy_index.py
```

Expect: ~588 passing, a small number of pre-existing failures in tests
that reference retired tiers. Treat these as known noise unless they
spike upward (i.e., a new failure appears that wasn't there before).

---

## 7. Install NSSM service

Automated:

```powershell
# Admin PowerShell
.\scripts\setup.ps1
```

Manual:

```powershell
nssm install BTCBiasEngine "C:\Trading\btc-bias-engine\venv\Scripts\python.exe" `
    "C:\Trading\btc-bias-engine\run_copy_engine.py"
nssm set BTCBiasEngine AppDirectory "C:\Trading\btc-bias-engine"
nssm set BTCBiasEngine AppStdout "C:\Trading\btc-bias-engine\data\engine_history.log"
nssm set BTCBiasEngine AppStderr "C:\Trading\btc-bias-engine\data\engine_history.log"
nssm set BTCBiasEngine AppRotateFiles 1
nssm set BTCBiasEngine AppRotateBytes 50000000
```

Start it:

```powershell
nssm start BTCBiasEngine
nssm status BTCBiasEngine        # expected: SERVICE_RUNNING
```

Tail logs:

```powershell
Get-Content data\engine_history.log -Tail 50 -Wait
```

---

## 8. Validate paper-mode behavior

Run for ≥1 hour. Watch for these expected log patterns:

| Pattern | Meaning |
|---|---|
| `CopyEngine: new Poly window` | Window flip detected |
| `SCALP MARKET FILL: SIDE Nx @ Mc IOC (ask=Mc+Nc)` | SCALP fired and filled |
| `SCALP BTC-TRAIL: ... retrace=$X >= trail=$Y → exit` | BTC-trail fired |
| `SCALP REVERSAL-SCORE: ... → INVERT` | Trail exit → flip decision |
| `SCALP INVERSE FILL: SIDE Nx @ Mc IOC` | Inverse re-entry filled |
| `SCALP PRE-EXPIRY: ...` / `SCALP EXIT (PRE-EXPIRY)` | Time-cap exit |
| `SYNC RECLAIM SKIP-DIRECTION: ... DIRECTION-tier ticker` | SCALP fill correctly registered, SYNC RECLAIM skipping (good) |
| `WINDOW-CAP RISK: tier=...` | $15 risk cap fired (expected after 5-6 flips) |

**Watch for ABSENCE of**:

- `OVERSELL-DETECTED` — should never appear in normal operation
- `STUCK-RESIDUAL` — should never appear
- `PROTECTIVE cancel ... aborting cycle` repeated more than ~3×/sec for
  more than a few seconds — that's the cancel-replace paralysis loop
  Fixes A-D shipped to prevent. If you see it, file a P0.
- `SCALP MARKET SKIP: already on _open_position` — Fix C diagnostic.
  Means STARTUP RECOVERY routed an old position to legacy state. Should
  be impossible after Fix A (`53aaf55`); if it appears, double-check
  `_direction_active.json` is being read at boot.

---

## 9. Pre-live checklist

Before flipping `PAPER_TRADING = False`:

- [ ] Test suite mostly green (small known noise OK)
- [ ] Service running cleanly for ≥1 hour in paper mode, multiple
      window flips observed
- [ ] Kalshi balance confirmed via direct API call (not just dashboard)
- [ ] `user_config.py` matches Section 1 of this doc
- [ ] At least one full SCALP cycle observed in paper:
      `SCALP MARKET FILL → SCALP BTC-TRAIL → SCALP REVERSAL-SCORE →
      SCALP INVERSE FILL` (or the position rode to PRE-EXPIRY cleanly)
- [ ] Boot log shows the 5 fixes are wired:
  - `STARTUP RECOVERY: ... routing to _direction_position` (Fix A)
    appears if you boot mid-window with an existing position
  - No `PROTECTIVE cancel ... aborting cycle` infinite-loop spam (Fix D)
  - No collision crashes between recovery and SCALP MARKET (Fixes B/C/E)
- [ ] `SAFETY_OVERSELL_HARDENING = True` and `MAX_RISK_PER_WINDOW_DOLLARS
      = 15.0` (the load-bearing safety net)
- [ ] You have a way to stop the service quickly:
      `nssm stop BTCBiasEngine`

When ready:

```powershell
notepad user_config.py    # set PAPER_TRADING = False
nssm restart BTCBiasEngine
Get-Content data\engine_history.log -Tail 100 -Wait
```

Watch the next 2-3 windows. Verify:

- `SCALP MARKET FILL` (real) is followed by `SYNC RECLAIM SKIP-DIRECTION`
  on the very next reconcile cycle (proves the fill registered in
  `_direction_position`, not the legacy `_open_position`)
- Trail exits show the dynamic-trail value: `trail=$25` near strike,
  larger deep ITM
- Inverse re-entry fires (`SCALP INVERSE FILL`) when the
  REVERSAL-SCORE total exceeds 0.5

---

## 10. Common pitfalls

### "The engine isn't firing"

SCALP fires once per window (`SCALP_PLACE_MAX_AGE_S = 30s` — must
fire in the first 30s of a fresh window). If the engine boots
mid-window, it skips the place-attempt and waits for the next window
flip. Expected behavior — don't mistake it for a bug.

### "I see `SCALP INVERSE NOFILL: NO IOC@Xc — no liquidity`"

The opposite-side ask was 0 (no offers in the book at any reasonable
price). The flip silently fails and the per-window lock blocks further
entries on this ticker. This is a market-structure constraint, not a
bug — Kalshi's 15-min ticker books are thin.

### "I see `WINDOW-CAP RISK: tier=SCALP-INV cost=$X.XX + committed=$Y.YY > cap`"

Expected after 5-6 flips per window. The $15/window cap fired and
refused another inverse leg. This is the safety net working as
designed.

### "Engine restarted and now I have two state objects on the same ticker"

This was the 2026-05-09 paralysis bug. STARTUP RECOVERY adopted into
legacy `_open_position`, then SCALP MARKET fired into
`_direction_position` on the same ticker, then `_maintain_protective_order`
spam-looped trying to cancel a 404'd order forever.

Five fixes shipped (commit `96c6bb2` + `53aaf55`). Verify they're in
your tree:

```powershell
git log --oneline | Select-String -Pattern "Fix [A-E]|paralysis|count_filled"
```

You should see `53aaf55`, `96c6bb2`, `13a9b8f` at minimum. If any are
missing, `git pull` before going live.

### "I see `OVERSELL-DETECTED`"

The 2026-04-22 catastrophe signature. The residual reconciler will
auto-flatten on detect, but **investigate the root cause** — likely
candidates: a cancel-then-place race surfacing in a new path, or a
position-count miscalculation. Read `_reconcile_residual_position`
source for context. File a P0.

### "Service is RUNNING but logs are stale"

The engine froze. Force restart:

```powershell
nssm restart BTCBiasEngine
```

If repeated, file a P0. The engine should never freeze — Fixes A-E
were designed to make this unreachable.

### "Credentials worked yesterday but now fail"

Two common causes:

1. Kalshi rotated keys — check kalshi.com/account/profile
2. The service is running from a different working directory than your
   shell. Confirm with: `nssm get BTCBiasEngine AppDirectory`

### "I want to investigate without touching live"

Set `PAPER_TRADING = True` and `nssm restart BTCBiasEngine`. The engine
will log everything but route fills through `paper_trader.py` instead
of Kalshi. Real balance is untouched.

---

## 11. Operational gotchas (ALL versions)

### `_uc()` caches user_config at module import

Editing `user_config.py` while the engine is running has **no effect**
until restart. After any config change:

```powershell
nssm restart BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
# verify the new value appears in the next FIRE log line
```

### Position state lives on TWO objects

- `self._direction_position` — SCALP fills (current architecture).
  Trail-aware exit layer (`_scalp_fire_exit` etc.) manages this.
- `self._open_position` — legacy fixed-TP path. SYNC RECLAIM may adopt
  orphans here. Should rarely matter post-Fix-A, but exists for
  safety-net reasons.

If you see logs referencing `_open_position` for a ticker you expected
to be SCALP-managed, that's a Fix-A regression — investigate.

### Manual trades are invisible to engine state

`MANUAL_FILLS_CAPTURE_ENABLED = False`. The manual-fills poller LOGS
user trades from the Kalshi UI but does NOT add them to engine state.
This means:

- You can manually trade alongside engine without conflict
- Engine BAL checks may briefly read low during your settlement
  collateral periods (use confirm-with-2nd-fetch BAL protocol below)

### Confirm-with-2nd-fetch BAL protocol

Kalshi shows transient low BAL during settlement. **Always confirm a
catastrophic BAL drop with a second fetch ~10s later before stopping
the engine.** A genuine drop reads the same on both fetches; a
settlement artifact recovers within a few seconds.

### Race-condition in IOC fills

By the time a 50ms-old book read becomes an actual order, the offer
side may have been swept. `SCALP MARKET NOFILL` at slip=5c sometimes
means "depth was there 50ms ago, gone now" — not "no offer in price
range." `SCALP_REENTRY_SLIP_C` is the dial: raising it improves fill
rate at the cost of slippage.

---

## 12. Direct Kalshi state check

If engine logs and Kalshi disagree, query Kalshi truth directly:

```powershell
.\venv\Scripts\python -c "
import asyncio, os, sys; sys.path.insert(0, '.')
from kalshi_client import KalshiClient
async def main():
    with open('credentials/kalshi.env') as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k,v = line.strip().split('=',1)
                os.environ[k.strip()] = v.strip()
    pem = open(os.environ['KALSHI_PRIVATE_KEY_PATH']).read()
    async with KalshiClient(os.environ['KALSHI_API_KEY'], pem) as c:
        bal = await c.get_balance()
        print(f'BAL: \${bal.balance/100:.2f} | Portfolio: \${bal.portfolio_value/100:.2f}')
        positions = await c.get_positions()
        for p in positions or []:
            count = int(p.get('position', 0))
            if count != 0:
                print(f'  {p.get(\"ticker\", \"?\")[-15:]}: {count} ({p.get(\"market_exposure_dollars\", \"?\")})')
asyncio.run(main())
"
```

---

## 13. Where to ask

Authoritative sources, in order of trust:

1. **The code itself** — `polymarket_copy_engine.py`, `kalshi_client.py`
2. **`CLAUDE.md`** (root) — operator-facing summary (may lag the code by
   one or two commits; if they disagree, code wins)
3. **Recent git commits** — chronological; recent overrides older
4. **This document** — set up + paper validation + pre-live checklist
5. **`to-do/MONITORING_*.md`** — behavioral observations from live sessions
6. **`MEMORY.md`** (in `~/.claude/projects/.../memory/`) — prior catastrophe
   lessons (oversell, unauthorized strategies, balance tracking)

**Do NOT trust:**

- `AGENTS.md` (pre-strategic-reset, deprecated)
- Anything in `_archive/`, `docs/archive/`
- Older `to-do/PLAN_*.md` files dated before 2026-05-04
- Older `RESEARCH_*.md` files describing TA_FORCED / FVG / DIRECTION as
  live (all retired)
- `CURRENT_STATE_MAP.md` if its date predates the most recent commit
  (frequently stale)

---

## 14. First trade observation (live)

The first time the engine fires live after flipping
`PAPER_TRADING = False`:

1. Verify `nssm status BTCBiasEngine` returns `SERVICE_RUNNING`
2. Watch for `SCALP MARKET FILL: ...` log line
3. On the very next reconcile cycle (~30-60s later), watch for
   `SYNC RECLAIM SKIP-DIRECTION: ... is a DIRECTION-tier ticker —
   holds to settlement, no reclaim/exit logic applies` — this proves
   the fill registered in `_direction_position` (post-Fix-A)
4. Check the Kalshi web UI for the position — engine state and Kalshi
   truth must match
5. After exit (BTC-TRAIL / TRAIL / PRE-EXPIRY), verify:
   - `SCALP EXIT (...): sold Nx @ Mc` log line
   - `RESIDUAL-CLEAN: ... reason=...` confirms flat
   - Balance changed by the expected amount
6. If REVERSAL-SCORE → INVERT fired, watch for `SCALP INVERSE FILL`
   on the opposite side

The engine has a known P&L attribution gap — `SELL TIER FILLED` and
`SCALP EXIT` log lines don't always reconcile cleanly with balance
changes due to settlement timing. **Always check actual Kalshi balance
via the API call in Section 12, not the engine's reported P&L**, to
know real performance.

---

## 15. Emergency shutdown

If anything looks catastrophically wrong:

```powershell
nssm stop BTCBiasEngine
```

Then check actual Kalshi state via Section 12. If you have residual
positions on Kalshi that the engine didn't close, manually flatten
them via the Kalshi web UI. Investigate before restarting.

To revoke the engine's API access entirely (without changing your
bankroll):

1. Log into Kalshi at kalshi.com/account/profile
2. Rotate / delete the API key the engine is using
3. The next engine cycle will fail to authenticate and the service
   will log auth errors but won't trade

To re-enable: generate a new API key, update `credentials/kalshi.env`,
restart.
