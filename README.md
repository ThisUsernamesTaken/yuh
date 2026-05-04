# BTC Bias Engine

Automated trading engine for **Kalshi KXBTC15M** (15-minute BTC up/down
binary contracts). Uses a Brownian-Bridge fair-value model to detect
mispricings and enters via maker-bid limit orders.

> **Source of truth for system behavior**: [`CLAUDE.md`](CLAUDE.md).
> **Setup walkthrough for new machines / new AI agents**: [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md).
> This README is a one-page orientation; the docs above are authoritative.

---

## Current state (2026-05-03 PT, HEAD ca1e347)

- **Live strategies (3)**: `BB_PURE` (mean-reversion mispricing),
  `BB_TREND` (with-trend when BTC firmly past strike), `BB_MOMENTUM`
  (sustained directional BTC velocity). All gated behind a per-window
  ticker lock — one entry per ticker per window across all three.
- **Alignment classification stack (active)**:
  - **A1** `BB_PURE_ALIGNMENT_GATE_ENABLED` — blocks contrarian fires
    (BB cheap side opposes BTC 5-min trend)
  - **A2** `BB_PURE_ALIGNMENT_FALLBACK_TIER_ENABLED` — fires aligned
    bet when no contrarian setup AND BTC has clear trend
  - **B1** `BB_PURE_POSITION_ALIGN_ENABLED` — overrides strike-distance
    block when BB cheap side is aligned with BTC's position vs strike
  - **B3** `BB_PURE_LOSS_STREAK_COOLDOWN_ENABLED` — tightens edge floor
    after consecutive losses
  - **B4** `BB_PURE_FINAL_MIN_RELAX_ENABLED` — relaxes 60s time floor
    for aligned settlement-cliff setups
- **Retired** (feature-flagged off): TA_FORCED, SR_FADE, SCALP_DCA,
  SNIPER, ATM_REVERSION, wallet-copy. Don't re-enable without reading
  the relevant `to-do/` postmortem.
- **One trade per 15-min window**, capped at quarter-Kelly with 5-10%
  bankroll-per-trade ceiling depending on conviction tier.

---

## How the engine works

```
BB model fair_yes_cents (price_feed.prob_engine)
  vs market_mid_cents (Kalshi WS)
  → edge_pp = fair − market
  → if |edge_pp| ≥ BB_PURE_MIN_EDGE_PP (8 default):
       side = underpriced side
       contracts = quarter-Kelly × balance
       fire ONE entry per ticker per window
```

Strategic gates in front of the BB math:
- **Strike-distance**: |BTC − strike| / BTC < 0.04% (gamma is high near strike)
- **Trading hours**: 06:00–22:00 PT (no overnight)
- **Asymmetric vol cap**: counter-trend entries blocked if BTC moved >$20 in 30s
- **Pre-fire BAL gate**: require live balance ≥ 2× projected entry cost
- **Per-window ticker lock**: one entry per ticker, persisted to disk

See [`CLAUDE.md`](CLAUDE.md) for the full lifecycle.

---

## Implementing the repo (quickstart, Windows)

The full walkthrough with credential setup, paper-mode smoke testing,
and pre-live checklist is in
[`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md). Short version:

### 1. Prerequisites

- **Windows 10/11** (NSSM-managed Windows service)
- **Python 3.11+** on PATH
- **Admin PowerShell** for NSSM install
- **Kalshi account** with API credentials (UUID API key + RSA PEM private key)
- **Funded Kalshi balance** ($20-50 to start; engine assumes small bankrolls)

### 2. Clone and install

```powershell
cd C:\Trading
git clone https://github.com/ThisUsernamesTaken/yuh.git btc-bias-engine
cd btc-bias-engine
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Credentials

```powershell
mkdir credentials -ErrorAction SilentlyContinue
notepad credentials\kalshi.env
```

Contents (replace placeholders):

```
KALSHI_API_KEY=your-uuid-here
KALSHI_PRIVATE_KEY_PATH=C:\Trading\btc-bias-engine\credentials\kalshi_key.pem
EXECUTE_TRADES=true
KALSHI_DEMO=false
```

Save your RSA PEM key to that path. Confirm `credentials/` is gitignored:

```powershell
git check-ignore credentials\kalshi.env  # should print credentials/kalshi.env
```

### 4. Force paper mode for first run

```powershell
notepad user_config.py   # ensure PAPER_TRADING = True
```

### 5. Smoke test credentials + tests

```powershell
.\venv\Scripts\python -m pytest tests/ -q `
    --ignore=tests/test_bias_engine.py `
    --ignore=tests/test_consensus.py `
    --ignore=tests/test_phase3.py `
    --ignore=tests/test_strategy_index.py
# expect ~545 passed, 3 pre-existing failures unrelated to current strategy
```

### 6. Install + start the NSSM service

```powershell
# from Admin PowerShell
.\scripts\setup.ps1
nssm start BTCBiasEngine
nssm status BTCBiasEngine                          # expect SERVICE_RUNNING
Get-Content data\engine_history.log -Tail 50 -Wait
```

### 7. Validate paper-mode behavior, then flip live

Watch for `BB_PURE FIRE`, `BB_PURE FILL`, `RESIDUAL-CLEAN` log patterns
over 1-2 windows (~30 min). Once paper looks healthy and you've
reviewed the pre-live checklist in `docs/NEW_INSTANCE_SETUP.md`, flip
`PAPER_TRADING = False` in `user_config.py` and `nssm restart BTCBiasEngine`.

---

## Service management

```powershell
nssm status BTCBiasEngine
nssm restart BTCBiasEngine                          # needs Admin
nssm stop BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
```

Direct Kalshi state check:

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
        flat = all(int(p.get('position',0)) == 0 for p in positions or [])
        print(f'Position: {\"FLAT\" if flat else \"NOT FLAT\"}')
asyncio.run(main())
"
```

---

## Critical config (live values, see `user_config.py`)

```python
PAPER_TRADING                        = False
BB_PURE_MODE                         = True
BB_PURE_MIN_EDGE_PP                  = 8.0
BB_PURE_MAX_ENTRY_CENTS              = 55       # cheap-side bias
BB_PURE_KELLY_FRACTION               = 0.25     # quarter-Kelly base
BB_PURE_KELLY_MAX_FRAC               = 0.05     # 5% bankroll ceiling
SIZING_HARD_CAP_CONTRACTS_DAY        = 8
DAILY_LOSS_FRACTION                  = 0.20     # auto-halt at 20% loss
MAX_TRADES_PER_SESSION_TICKER        = 1        # one trade per window
BB_PURE_STRIKE_DISTANCE_GATE_ENABLED = True
BB_PURE_TRADING_HOURS_GATE_ENABLED   = True
BB_PURE_BAL_GATE_ENABLED             = True
```

`CLAUDE.md` has the complete config inventory with rationale for every
default.

---

## Test suite

```powershell
.\venv\Scripts\python -m pytest tests/ -q `
    --ignore=tests/test_bias_engine.py `
    --ignore=tests/test_consensus.py `
    --ignore=tests/test_phase3.py `
    --ignore=tests/test_strategy_index.py
```

Expected: ~545 passing as of HEAD ca1e347. 3 pre-existing failures
(2 in `test_late_dominant.py`, 1 in `test_sr_fade_gates.py`) unrelated
to current strategy. The 4 ignored test files reference modules deleted
long ago — safe to delete in a future cleanup commit.

Net new tests since fcf9008: 41 covering alignment classification,
position-vs-strike override, loss-streak cooldown, final-minute relax,
and fallback-signal synthesis.

---

## Risk disclaimer

Real money goes on Kalshi. Start with a small balance ($20-50). The engine
auto-halts at `DAILY_LOSS_FRACTION` (20% bankroll loss), but nothing
prevents you from losing up to that amount in a bad session before the
halt fires.

The engine is calibrated for **bankrolls under $200** with quarter-Kelly
sizing. Larger bankrolls require re-tuning the sizing knobs — see
`BB_PURE_KELLY_*` in `user_config.py`.
