# New Instance Setup — for AI agents and humans

**Last updated**: 2026-05-03 PT (post commit ca1e347)
**Reading prerequisite**: `CLAUDE.md` (root). Read it first to understand
what the engine *does* before installing it.

This guide walks through cleanly setting up the BTC Bias Engine on a new
Windows machine, validating it works, and flipping it to live trading. It
exists because `README.md` got stale and confused fresh AI agents about
which strategy is live.

The engine has **three live entry strategies** as of ca1e347:
- `BB_PURE` — Brownian-Bridge mispricing (mean-reversion in mean-rev zone)
- `BB_TREND` — With-trend bets when BTC is firmly past strike (≥0.15%)
- `BB_MOMENTUM` — Sustained directional BTC velocity entries

All three are gated behind a per-window ticker lock — one entry per
ticker per window across all three strategies.

A five-flag alignment classification stack is also active:
- A1 `BB_PURE_ALIGNMENT_GATE_ENABLED` — blocks contrarian BB fires
- A2 `BB_PURE_ALIGNMENT_FALLBACK_TIER_ENABLED` — adds with-trend fallback
- B1 `BB_PURE_POSITION_ALIGN_ENABLED` — overrides strike-dist block
  when position-aligned (captures 0.04-0.15% no-man's-land setups)
- B3 `BB_PURE_LOSS_STREAK_COOLDOWN_ENABLED` — auto-tightens after losses
- B4 `BB_PURE_FINAL_MIN_RELAX_ENABLED` — relaxes time-floor for aligned

TA_FORCED, SR_FADE, SCALP_DCA, wallet-copy, sniper, and ATM_REVERSION
are all retired/feature-flagged-off. Don't re-enable them without reading
the relevant `to-do/` postmortems.

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
2. `bb_pure.py` — the entire trading thesis (pure math, ~190 lines)
3. `tests/test_bb_pure.py` — pins down each decision rule
4. `user_config.py` — every live-behavior knob in one file

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

Expect: ~545 passing (HEAD ca1e347). 3 pre-existing failures
(2 in `test_late_dominant.py`, 1 in `test_sr_fade_gates.py`) unrelated
to current strategies. The 4 ignored test files reference modules
deleted long ago — safe to skip.

The pure-module test files most worth reading first:
- `tests/test_bb_pure.py` — BB_PURE math + sizing
- `tests/test_bb_pure_alignment.py` — alignment classifier + position-
  vs-strike override + fallback signal synthesis
- `tests/test_bb_momentum.py` — directional-velocity entries
- `tests/test_protective_math.py` — MFE-aware trail logic

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

With `PAPER_TRADING=True` and `BB_PURE_MODE=True`, watch for these log
patterns over 1-2 windows (~30 min):

| Pattern | Meaning |
|---|---|
| `CopyEngine: new Poly window` | Window flip detected |
| `BB_PURE STRIKE-DIST-BLOCK` | Strategic gate blocking (BTC too far from strike) |
| `BB_PURE BTC-RANGE-BLOCK` | Asymmetric vol gate blocking |
| `BB_PURE GATE-BLOCK` | Microstructure gate blocking |
| `BB_PURE FIRE: SIDE Nx @ Mc` | Engine attempted a paper entry |
| `BB_PURE FILL: ...` | Paper entry filled |
| `RESIDUAL-CLEAN: ... (flat)` | Residual reconciler confirmed flat post-close |

If you see no `BB_PURE FIRE` for 1-2 hours, that's normal — the strategic
gates filter aggressively. Long quiet stretches are expected behavior, not
a bug. Look for absence of:

- `OVERSELL-DETECTED`
- `STUCK-RESIDUAL`
- `REENTRY-BLOCK` (tripped — expected to *never* fire in normal operation)
- Anything labeled `ERROR` repeatedly

---

## 7. Pre-live checklist

Before flipping `PAPER_TRADING=False`:

- [ ] Test suite passing (445/447 expected, 2 pre-existing failures OK)
- [ ] Service has been running cleanly for ≥1 hour in paper mode
- [ ] Kalshi balance verified via direct API call (not just dashboard)
- [ ] `user_config.py` reviewed end-to-end against current `CLAUDE.md`
- [ ] Sizing knobs match your bankroll appetite:
  - `BB_PURE_KELLY_FRACTION` (default 0.25 — quarter-Kelly base)
  - `BB_PURE_KELLY_MAX_FRAC` (default 0.05 — 5% bankroll cap per trade)
  - `SIZING_HARD_CAP_CONTRACTS_DAY` (default 8)
  - `DAILY_LOSS_FRACTION` (default 0.20 — auto-halt at 20% bankroll loss)
- [ ] Strategic gates enabled:
  - `BB_PURE_STRIKE_DISTANCE_GATE_ENABLED = True`
  - `BB_PURE_TRADING_HOURS_GATE_ENABLED = True`
  - `BB_PURE_BAL_GATE_ENABLED = True`
- [ ] Retired tiers still off:
  - `TA_FORCED_ENTRY_ENABLED = False`
  - `SR_FADE_ENABLED = False`
  - `SCALP_DCA_ENABLED = False`
  - `SNIPER_ENABLED = False`
  - `WALLET_COPY_*` (all four) = False
- [ ] You have a way to stop the service quickly: `nssm stop BTCBiasEngine`

When ready:

```powershell
notepad user_config.py
# Set PAPER_TRADING = False
nssm restart BTCBiasEngine
```

Watch the next 2-3 windows closely. Tail the log; verify any `BB_PURE FIRE`
events also produce a `BB_PURE FILL` and a clean close.

---

## 8. Common pitfalls

### "The engine isn't firing"

Default behavior. Strategic gates only allow entries when:
- BTC is within ±0.04% of strike (gate firing aggressively when BTC trends)
- Time-of-day is between 06:00 and 22:00 PT (no overnight)
- BB-fair vs market-mid edge ≥ 8pp
- BTC velocity is not strongly counter to the proposed side
- `MAX_TRADES_PER_SESSION_TICKER = 1` is satisfied (one trade per 15-min window)
- BAL ≥ 2× projected entry cost

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

---

## 9. Where to ask

This codebase has been worked through multiple AI sessions. Authoritative
sources, in order of trust:

1. The code itself (esp. `bb_pure.py`, `polymarket_copy_engine.py`)
2. `CLAUDE.md` (current architecture)
3. Recent git commits (chronological — recent commits override older ones)
4. This document
5. `to-do/LIVE_SESSION_NOTES_*.md` for behavioral observations
6. `to-do/PLAN_*.md` for previously-planned (sometimes stale) work

**Do NOT trust:**
- `AGENTS.md` (pre-strategic-reset, deprecated)
- Anything in `_archive/`, `docs/archive/`
- Older `to-do/PLAN_*.md` files dated before the 2026-05-02 reset

---

## 10. First trade observation

The first time the engine fires live:
1. Verify `nssm status BTCBiasEngine` returns `SERVICE_RUNNING`
2. Check the Kalshi web UI for the position (engine and Kalshi truth must match)
3. Watch `BB_PURE FILL`, `PROTECTIVE`, and either `RESIDUAL-CLEAN` or
   `PREFLIGHT-TP` events
4. After settlement, verify balance changed by the expected amount

The engine has a known P&L attribution gap: PREFLIGHT-TP fills don't
trigger the close-handler, so `kalshi_trades` rows can be missing close
data. **Always check actual balance, not the engine's reported P&L.**
