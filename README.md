# BTC Binary Contract Trading Engine

Automated trading engine for **Kalshi KXBTC15M** (15-minute BTC up/down binary contracts).

The engine watches BTC price impulses on Coinbase, order-book pressure on Kalshi, and taker flow in real time. When the microstructure shows the market being forced in a direction that also disagrees with a Brownian-Bridge fair-value model (FVG signal), it enters on the corresponding Kalshi contract with maker-only limit orders and holds to expiry.

> **Source of truth for system behavior**: [`CLAUDE.md`](CLAUDE.md). This README is a quickstart; CLAUDE.md documents the live signal logic, sizing, and file map.

---

## How it works (current, 2026-04-21)

1. **Microstructure pressure** (`microstructure.py`) — continuously scores BTC impulse, Kalshi book imbalance, taker flow momentum, and Kalshi repricing lag into a single `PressureScore`.
2. **FVG filter** — a Brownian Bridge prob engine in `price_feed.py` computes a per-second fair value. FVG ≥ 8c from the session baseline arms an entry.
3. **DOMINANT-DIRECTION gate** — four AND conditions before any entry: BTC 5-min trend ≥ $30 in FVG direction, pressure score sign + confidence ≥ 0.55 agreeing, RSI not contrarian, FVG magnitude ≥ 10c.
4. **Maker-only entry** — single limit at the current bid with `post_only=True`. Kalshi rejects the order if it would cross the spread.
5. **Kelly sizing** — contract count computed from model edge; aggressive 0.65 Kelly fraction. High MTF confluence doubles base size.
6. **Hold to expiry** — no mid-session stops (disabled after data showed 10/10 stopped trades were directionally correct). TP rests at the ask; contract settles at 0c or 100c at the window boundary.

Polymarket smart-wallet copying is **disabled** as of 2026-04-21. Code paths are intact; flip `WALLET_COPY_ENABLED` in `user_config.py` to re-enable.

---

## Prerequisites

- **Windows 10/11** with **NSSM** (installed by setup script) — primary supported platform
- **Python 3.11+**
- **Kalshi account** with API access (UUID + RSA private-key PEM) and a funded balance
- ~$2 minimum balance to trade (configurable via `MIN_BALANCE_TO_TRADE`)

---

## Install (Windows)

```powershell
# Admin PowerShell
cd C:\Trading\btc-bias-engine
.\scripts\setup.ps1       # creates venv, installs deps, prompts for credentials, registers NSSM service
```

The setup script:
1. Verifies Python 3.11+
2. Creates `venv/`, installs `requirements.txt`
3. Prompts for Kalshi API UUID + PEM path; writes `credentials/kalshi.env`
4. Registers **BTCBiasEngine** with NSSM pointing at `run_copy_engine.py`
5. Starts the service

---

## Configuration

All dials live in `user_config.py` (copy from `user_config.example.py` on first run). Key current defaults:

```python
ENGINE_DIR = r"C:\Trading\btc-bias-engine"   # change if installed elsewhere

# Sizing — Kelly is primary, aggressive
KELLY_ENABLED = True
KELLY_FRACTION = 0.65          # between half and three-quarter Kelly
KELLY_MAX_FRAC = 0.80          # monster edges push 80% of balance
SIZING_MAX_FRACTION = 0.75     # absolute ceiling per position
SIZING_HARD_CAP_CONTRACTS_DAY = 150
MTF_SIZE_MULTIPLIER_HIGH = 2.0

# Risk
DAILY_LOSS_LIMIT = 100.00
MIN_BALANCE_TO_TRADE = 2.00

# Entry bands
MIN_ENTRY_CENTS = 35
MAX_ENTRY_CENTS = 75

# Features
MAKER_ONLY = True
TA_FORCED_ENABLED = True
MTF_ENABLED = True
WALLET_COPY_ENABLED = False
WALLET_SCORING_ENABLED = False
SNIPER_ENABLED = False         # drained account $172→$8 while "off" — do not flip without reading CLAUDE.md
```

Edit and restart: `nssm restart BTCBiasEngine`.

---

## Service management

```powershell
nssm status BTCBiasEngine
nssm restart BTCBiasEngine                          # needs Admin
nssm stop BTCBiasEngine
Get-Content data\engine_history.log -Tail 50 -Wait  # live logs
```

---

## Monitor

```powershell
python launcher.pyw --monitor      # or just run: python monitor.py
```

Reads `data/dashboard_state.json` (refreshed every few seconds by the engine). Shows pressure gauge, DOMINANT-gate factor breakdown, current window, open position, balance.

---

## Useful log lines

```
CopyEngine DOMINANT-SKIP: YES not dominant-direction [btc5m=$+0] — April-15 profile required
CopyEngine SHADOW-ARM / SHADOW-FIRE / SHADOW-EXPIRE        # measurement instrument, not real orders
CopyEngine KELLY SIZING: f*=0.XX size=Xct                  # sizing decision
CopyEngine scorer: disabled via WALLET_SCORING_ENABLED=False
CopyEngine flow: starting with 0 wallets …                 # normal — TA_FORCED doesn't need wallets
BALANCE SNAPSHOT: $XX.XX | window_change
```

---

## Quick PnL check

```powershell
.\venv\Scripts\python -c "
import sqlite3
c = sqlite3.connect('data/trades.db').cursor()
c.execute('''SELECT DATE(placed_at), COUNT(*),
    SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2)
    FROM kalshi_trades WHERE status NOT IN (\"pending\",\"unfilled\")
    GROUP BY DATE(placed_at) ORDER BY 1 DESC LIMIT 7''')
for d, n, w, p in c.fetchall():
    print(f'{d}  {n:3d} trades  WR={w/n*100:3.0f}%  \${p:+.2f}')
"
```

For authoritative PnL (balance-change truth, not `kalshi_trades.pnl` which can drift), see `scripts/reconcile_pnl.py`.

---

## Switching accounts

```powershell
cd C:\Trading\btc-bias-engine
.\deploy\swap-credentials.ps1
# prompts for new UUID + new PEM path; updates NSSM env and restarts
```

---

## Reinstall after code update

```powershell
.\scripts\reinstall_service.ps1   # preserves credentials
```

---

## Transfer to a new machine

1. Copy the whole `btc-bias-engine/` folder (or unzip a fresh `deploy/btc-bias-engine-deploy.zip`).
2. Edit `user_config.py::ENGINE_DIR` if installing somewhere other than `C:\Trading\btc-bias-engine`.
3. Run `.\scripts\setup.ps1` — it will create `venv/`, prompt for credentials, and register the NSSM service.
4. Engine skips the in-progress 15-min window on first start and trades the next one.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Service won't start | Check `data\engine_history.log` — almost always a missing credential or wrong PEM path. |
| `DOMINANT-SKIP` spam with `btc5m=$+0` | Normal — BTC is flat; the gate is correctly holding fire. |
| `starting with 0 wallets` warning | Expected with `WALLET_SCORING_ENABLED=False`. Not an error. |
| Dashboard stale | Service hung. Check `nssm status BTCBiasEngine`; restart if `SERVICE_RUNNING` but dashboard file >30s old. |
| Ghost positions after restart | `CopyEngine STARTUP: cancelled N ghost resting orders` — auto-recovery. If a position was live, `POSITION SYNC` adopts it. |
| `kalshi_trades.pnl` disagrees with balance | Known — use `python scripts/reconcile_pnl.py --apply` to rewrite from `settlement_ledger`. |

---

## Risk disclaimer

Real money goes on Kalshi. Start with a small balance. The engine will halt at `DAILY_LOSS_LIMIT`, but nothing prevents you from losing up to that amount in a bad session.
