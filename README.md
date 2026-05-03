# BTC Bias Engine

Automated trading engine for **Kalshi KXBTC15M** (15-minute BTC up/down
binary contracts). Uses a Brownian-Bridge fair-value model to detect
mispricings and enters via maker-bid limit orders.

> **Source of truth for system behavior**: [`CLAUDE.md`](CLAUDE.md).
> **Setup walkthrough for new machines / new AI agents**: [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md).
> This README is a one-page orientation; the docs above are authoritative.

---

## Current state (2026-05-03)

- **Live strategy**: `BB_PURE` only (Brownian-Bridge mispricing).
- **Retired** (feature-flagged off): TA_FORCED, SR_FADE, SCALP_DCA,
  SNIPER, ATM_REVERSION, wallet-copy. Don't re-enable without reading the
  relevant `to-do/` postmortem.
- **One trade per 15-min window**, capped at quarter-Kelly with 5%
  bankroll-per-trade ceiling.

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

## Quickstart (Windows)

```powershell
git clone https://github.com/ThisUsernamesTaken/yuh.git C:\Trading\btc-bias-engine
cd C:\Trading\btc-bias-engine
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Set credentials in credentials/kalshi.env (see docs/NEW_INSTANCE_SETUP.md)
.\scripts\setup.ps1   # registers NSSM service
nssm start BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait
```

For first-run validation, paper-mode smoke testing, and the pre-live
checklist, see [`docs/NEW_INSTANCE_SETUP.md`](docs/NEW_INSTANCE_SETUP.md).

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

Expected: 445 passing, 2 pre-existing `test_late_dominant.py` failures
unrelated to current strategy. The 4 ignored test files reference modules
deleted long ago — safe to delete in a future cleanup commit.

---

## Risk disclaimer

Real money goes on Kalshi. Start with a small balance ($20-50). The engine
auto-halts at `DAILY_LOSS_FRACTION` (20% bankroll loss), but nothing
prevents you from losing up to that amount in a bad session before the
halt fires.

The engine is calibrated for **bankrolls under $200** with quarter-Kelly
sizing. Larger bankrolls require re-tuning the sizing knobs — see
`BB_PURE_KELLY_*` in `user_config.py`.
