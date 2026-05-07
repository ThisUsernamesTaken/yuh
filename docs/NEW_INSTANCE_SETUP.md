# New Instance Setup — for AI agents and humans

**Last updated**: 2026-05-06 PT (DIRECTION refactor + improvements)
**Reading prerequisite**: `CLAUDE.md` (root). Read it first to understand
what the engine *does* before installing it.

This guide walks through cleanly setting up the BTC Bias Engine on a new
Windows machine, validating it works, and flipping it to live trading. It
exists because `README.md` got stale and confused fresh AI agents about
which strategy is live.

The engine has **one live entry strategy** as of 2026-05-06:

- **`DIRECTION`** — Sign-aligned distance + momentum, IOC at ask+slippage,
  hold to settlement. Conviction-multiplier sizing (0.7-2.0×).
  - Pure module: `direction_strategy.py` (decision + multiplier math)
  - Engine handler: `_direction_tick` in `polymarket_copy_engine.py`
  - **Position state: `self._direction_position` (NOT `_open_position`)**
    — this is the architectural fix that prevents 20+ legacy exit
    paths from hijacking DIRECTION fills.

Backtest evidence (`scripts/backtest_strategy_comparison.py`, n=197
settled markets): DIRECTION 0.10%/$10 thresholds win on alpha density:
90.5% win rate / +$3.93 mean per trade / +$331 corpus / 1.1% top-win-skew
(extremely robust; not single-trade-skewed). Beat 8 alternative strategies
(cheap-underdog, mean-reversion, BB-model-edge, momentum-only, etc.).

`PAPER_FVG_LIVE_MODE`, `BB_PURE`, `BB_TREND`, `BB_MOMENTUM`, `TA_FORCED`,
`SR_FADE`, `SCALP_DCA`, `SNIPER`, `WALLET_COPY`, `ATM_REVERSION`,
`ARB_DETECTOR`, `MICRO_PULLBACK`, `TP_LAYERED` are all retired/feature-
flagged-off. **Do not re-enable them without explicit user direction** —
they obscure DIRECTION signal and several have historical catastrophe
records (see `MEMORY.md`). FVG-tier specifically had a stale-cache
premature-close bug that caused a -19% loss on first live fill.

**OPERATIONAL GOTCHA — `_uc()` caches config at module import.** Editing
`user_config.py` while the engine is running has NO effect until restart.
After any config change, run `nssm restart BTCBiasEngine` and verify the
new value appears in the next FIRE log line. See `CLAUDE.md` for full
details.

---

## 0. Prerequisites

- **Windows 10 / 11** (the engine is Windows-only because it relies on NSSM)
- **Python 3.11+** on PATH
- **Admin PowerShell** for NSSM service install/management
- **Kalshi account** with API credentials:
  - `KALSHI_API_KEY` — UUID
  - `KALSHI_PRIVATE_KEY_PATH` — RSA private key in PEM format
- **Funded Kalshi balance** (start with $20-50 max — the engine assumes
  small bankrolls)
- **NSSM** — installed automatically by `scripts/setup.ps1`, or grab from
  [nssm.cc](https://nssm.cc)

---

## 1. Clone and inspect

```powershell
cd C:\Trading
git clone https://github.com/ThisUsernamesTaken/yuh.git btc-bias-engine
cd btc-bias-engine
```

**Before installing**, read in this order:

1. `CLAUDE.md` — current architecture, lifecycle of a trade, knobs
2. `_fvg_tiering.py` — the entire trading thesis (pure tier math, ~230 lines)
3. `tests/test_fvg_tiering.py` — pins down each tier boundary
4. `polymarket_copy_engine.py:_paper_fvg_tick` — engine entry point for FVG
5. `polymarket_copy_engine.py:_paper_fvg_live_entry` — live execution path
6. `user_config.py` — every live-behavior knob in one file (FVG section
   at line ~2015)

If any of these contradict this guide, **the code wins**.

---

## 2. Python environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` is intentionally tiny (5 deps): `aiohttp`, `aiosqlite`,
`cryptography`, `psutil`, `websockets`. If any sub-dep fails to compile on
Windows, you probably need MSVC build tools — install via `winget install
Microsoft.VisualStudio.2022.BuildTools`.

---

## 3. Credentials

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

Save your PEM private key to the path you set above. Confirm permissions
restrict it to your user only.

**Never commit `credentials/`** — it's in `.gitignore`. Sanity check:

```powershell
git check-ignore credentials\kalshi.env
# expected: credentials/kalshi.env
```

---

## 4. First-run smoke test (PAPER MODE)

Before the engine touches a real Kalshi order, force paper mode:

```powershell
# Edit user_config.py and ensure:
# PAPER_TRADING = True
# PAPER_FVG_LIVE_MODE = False    # paper sim only
notepad user_config.py
```

Verify the credentials work:

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
asyncio.run(main())
"
```

If you see your balance, credentials are wired correctly.

Run the test suite:

```powershell
.\venv\Scripts\python -m pytest tests/ -q `
    --ignore=tests/test_bias_engine.py `
    --ignore=tests/test_consensus.py `
    --ignore=tests/test_phase3.py `
    --ignore=tests/test_strategy_index.py
```

Expect: **588 passing, 3 pre-existing failures**
(2 in `test_late_dominant.py`, 1 in `test_sr_fade_gates.py`) unrelated
to current strategy. The 4 ignored test files reference modules
deleted long ago — safe to skip.

The pure-module test files most worth reading first for FVG:

- `tests/test_fvg_tiering.py` — 23 tier-classification + sizing-math tests
- `tests/test_fvg_live_wiring.py` — 10 live-state-machine tests (entry,
  NOFILL timeout, late-fill, SL hit, time-exit, daily-loss halt,
  ticker-lock, place_order failure)
- `tests/test_residual_reconciler.py` — A5 oversell-guard
- `tests/test_place_capped_side_sell.py` — MIN-TRUTH + OVERSELL-GUARD

---

## 5. Install NSSM service

```powershell
# Admin PowerShell
.\scripts\setup.ps1
```

The setup script creates `venv/`, installs deps, prompts for credentials
if not yet set, and registers `BTCBiasEngine` as an NSSM service pointing
at `run_copy_engine.py`.

Manual NSSM install (if `setup.ps1` doesn't fit your environment):

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
nssm status BTCBiasEngine
# expected: SERVICE_RUNNING
```

Tail logs:

```powershell
Get-Content data\engine_history.log -Tail 50 -Wait
```

---

## 6. Validate paper-mode behavior

With `PAPER_TRADING=True`, `PAPER_FVG_ENABLED=True`, and
`PAPER_FVG_LIVE_MODE=False`, watch for these log patterns over 2-3
windows (~30-45 min):

| Pattern | Meaning |
|---|---|
| `CopyEngine: new Poly window` | Window flip detected |
| `PAPER FVG BASELINE: Nc from M samples` | First-90s baseline established |
| `PAPER FVG TIER-REFUSE: ...` | Tier 0 predicate caught the signal |
| `PAPER FVG ENTRY [TN]: SIDE Nx @ Mc` | Paper sim opened a position |
| `PAPER FVG CLOSE: ... pnl=$+/-X.XX` | Paper sim closed (TP / SL / time / trail) |

If you see no `PAPER FVG ENTRY` for 1-2 windows, that's normal — the tier-0
refuse predicates filter aggressively (early-window noise, counter-trend
mid-prices, flat BTC + counter alignment). Long quiet stretches are
expected behavior.

Look for ABSENCE of:

- `OVERSELL-DETECTED`
- `STUCK-RESIDUAL`
- `Anything labeled ERROR repeatedly`

---

## 7. Pre-live checklist

Before flipping `PAPER_FVG_LIVE_MODE = True`:

- [ ] Test suite passing (588/591 expected, 3 pre-existing failures OK)
- [ ] Service has been running cleanly for ≥1 hour in paper mode
- [ ] Kalshi balance verified via direct API call (not just dashboard)
- [ ] `user_config.py` reviewed end-to-end against current `CLAUDE.md`
- [ ] Tier sizing knobs match your bankroll appetite (defaults shown):
  - `FVG_TIER_FRAC_T1 = 0.35` (35% bankroll on highest-conviction signals)
  - `FVG_TIER_FRAC_T2 = 0.25`
  - `FVG_TIER_FRAC_T3 = 0.18`
  - `FVG_TIER_FRAC_T4 = 0.10`
  - `FVG_LIVE_MAX_TICKER_EXPOSURE_FRAC = 0.40` (40% hard cap)
  - `FVG_LIVE_MAX_CONTRACTS_CAP = 200` (defends against absurd bankrolls)
  - `FVG_DAILY_LOSS_HALT_FRAC = 0.20` (auto-halt at 20% bankroll loss/day)
  - `FVG_LIVE_MAX_ENTRY_CENTS = 75` (cheap-side bias)
- [ ] Retired strategies still off:
  - `BB_PURE_MODE = False`
  - `BB_MOMENTUM_ENABLED = False`
  - `TA_FORCED_ENTRY_ENABLED = False`
  - `SR_FADE_ENABLED = False`
  - `SCALP_DCA_ENABLED = False`
  - `SNIPER_ENABLED = False`
  - `WALLET_COPY_*` (all four) = False
  - `ATM_REVERSION_ENABLED = False`
  - `ARB_DETECTOR_ENABLED = False`
- [ ] Safety still on:
  - `SAFETY_OVERSELL_HARDENING = True`
  - `MAX_TRADES_PER_SESSION_TICKER = 1`
- [ ] You have a way to stop the service quickly: `nssm stop BTCBiasEngine`

When ready:

```powershell
notepad user_config.py
# Set PAPER_TRADING = False
# Set PAPER_FVG_LIVE_MODE = True
nssm restart BTCBiasEngine
```

Watch the next 2-3 windows closely. Tail the log; verify any
`PAPER FVG LIVE FIRE` events also produce a `PAPER FVG LIVE FILL` and a
clean `PAPER FVG LIVE CLOSE` (or a `PAPER FVG LIVE NOFILL CANCEL` if the
order didn't fill in 8s).

---

## 8. Common pitfalls

### "The engine isn't firing"

Default behavior. Tier-0 refuse predicates filter aggressively:

- `session_age_s < 180s` → refuse (early-window noise)
- counter-trend AND `30 ≤ entry_c ≤ 49` → refuse (worst-segment combo)
- `|btc_5m_move| < $20` AND counter-trend → refuse (no edge)

Plus the entry must clear:

- FVG threshold (5/8/12c depending on session age)
- Cheap-side cap: entry ≤ 75c (`FVG_LIVE_MAX_ENTRY_CENTS`)
- Per-window ticker lock: 1 trade per ticker per 15-min window
- Pre-fire balance gate: balance ≥ 1.5× projected cost

Under unfavorable conditions, the engine correctly stays out.

### "I see DOMINANT-SKIP / SHADOW-EDGE log spam"

Legacy retired-tier logs. They don't affect live behavior. Pruning them is
a future cleanup commit.

### "My credentials worked yesterday but now fail"

Two common causes:

1. Kalshi rotated keys — check kalshi.com/account/profile
2. The service is running from a different working directory than your
   shell. Confirm with: `nssm get BTCBiasEngine AppDirectory`

### "Service is RUNNING but logs are stale"

The engine froze. Force restart:

```powershell
nssm restart BTCBiasEngine
```

Then read recent logs for the cause. If repeated, file as a bug — engine
should never freeze; if it does, that's a P0.

### "I see `OVERSELL-DETECTED`"

This is the 2026-04-22 catastrophe signature — the engine tried to sell
more contracts than it actually held, which Kalshi auto-converts to
opening a position on the OPPOSITE side. The residual reconciler will
auto-flatten on detect, but **you should investigate the root cause**.
Likely candidates: a cancel-then-place race, or a position-count
miscalculation. Read `_reconcile_residual_position` source for context.

### "I see `PAPER FVG LIVE DAILY-LOSS-HALT`"

Day P&L hit -20%. The strategy halts for the rest of the day. To resume
manually, restart the engine — `live_day_start_balance` resets each day.
Investigate why before re-enabling.

---

## 9. Where to ask

This codebase has been worked through multiple AI sessions. Authoritative
sources, in order of trust:

1. The code itself (esp. `_fvg_tiering.py`, `polymarket_copy_engine.py`)
2. `CLAUDE.md` (current architecture)
3. Recent git commits (chronological — recent commits override older ones)
4. This document
5. `to-do/LIVE_SESSION_NOTES_*.md` for behavioral observations
6. `MEMORY.md` (in `~/.claude/projects/.../memory/`) for prior catastrophe lessons

**Do NOT trust:**

- `AGENTS.md` (pre-strategic-reset, deprecated)
- Anything in `_archive/`, `docs/archive/`
- Older `to-do/PLAN_*.md` files dated before 2026-05-04
- Comments referencing BB_PURE/BB_MOMENTUM/TA_FORCED as live (they're retired)

---

## 10. First trade observation

The first time the engine fires live:

1. Verify `nssm status BTCBiasEngine` returns `SERVICE_RUNNING`
2. Check the Kalshi web UI for the position (engine and Kalshi truth must match)
3. Watch `PAPER FVG LIVE FILL`, then either `PAPER FVG LIVE CLOSE` or
   `PAPER FVG LIVE EMERGENCY-EXIT`
4. After close, verify `RESIDUAL-CLEAN` log appears (residual reconciler
   confirms flat) — if not, expect `RESIDUAL: ... market-selling` instead
5. Verify balance changed by the expected amount via direct API call

The engine has a known P&L attribution gap: TP fills don't always trigger
the close-handler logging in real time. **Always check actual balance, not
the engine's reported P&L** to know real performance.

---

## 11. Tier behavior expectations

OOS validation summary (from `scripts/backtest_oos_level3.py`):

| Tier | Backtest fill | OOS fill | Edge / trade | Sizing |
|------|---------------|----------|--------------|--------|
| T1   | 99%           | 97.4%    | +$0.158      | 35% BR |
| T2   | 93%           | 92.2%    | +$0.085      | 25% BR |
| T3   | 91%           | 81.2%    | +$0.043      | 18% BR |
| T4   | 76%           | 75.0%    | +$0.018      | 10% BR |

Realistic expectations on a $35 starting bankroll:

- ~0-3 entries per day (tier-0 refuse predicates filter aggressively)
- ~$10-50/day flat-sized initially, scaling as bankroll grows
- Liquidity caps will limit compounding well below the simulated
  14000× growth — Kalshi 15m-tickers can absorb 50-100ct at T1 fills
  without significant slippage, but beyond that you'll see partial fills

If actual fill rate falls significantly below OOS expectations
(e.g., T1 < 90%), investigate before continuing — Kalshi market
microstructure may have shifted, requiring tier threshold revalidation.
