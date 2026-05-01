# Claude Brief: ATM Reversion Performance Upgrade

Date: 2026-04-25
Owner context: Codex + Claude collaboration on `C:\Trading\btc-bias-engine`

This brief supersedes the earlier initial ATM handoff where values conflict. The earlier handoff was directionally right, but some defaults are now stale after the fee-corrected sweep and entry-quality study.

## Current State

ATM Reversion is implemented as a paper-only tier:

- `atm_reversion.py`
- `scripts/atm_reversion_backtest.py`
- `scripts/atm_reversion_sweep.py`
- `tests/test_atm_reversion.py`
- `polymarket_copy_engine.py` paper wiring
- `user_config.py` has `ATM_REVERSION_ENABLED = True` and `ATM_REVERSION_PAPER_ONLY = True`

Live orders are not placed by ATM. SR_FADE live behavior is unchanged.

The current production-candidate config is:

```python
ATM_MAX_STRIKE_DIST_PCT = 0.030
ATM_STOP_STRIKE_DIST_PCT = 0.080
ATM_DISCOUNT_MAX_ENTRY_CENTS = 35
ATM_DISCOUNT_MIN_FAIR_CENTS = 47.0
ATM_DISCOUNT_MIN_EDGE_CENTS = 8.0
ATM_TARGET_CENTS = 49
ATM_PROFIT_TARGET_CENTS = 8
ATM_BIAS_ENABLED = False
ATM_FORCE_EXIT_AGE_S = 840
```

## Important Clarification On PnL

The 30-day backtest PnL is not "start with $100 and compound."

It is fixed-size, per-contract replay:

```text
Immediate ask entry, 1 contract/trade:
n=1202
WR=59.9%
gross=+6742c
fee-adjusted net=+3863.8c = +$38.64
maxDD=610.7c = $6.11

Fixed 100ct equivalent:
net=+$3,863.80
maxDD=$610.70
```

For a real `$100` account, fixed `100ct` is too large. The historical drawdown would exceed account equity. Safer validation sizing is fixed `5-20ct`, or a tiny balance fraction with a hard contract cap.

## Proven Backtest Shape

Data source:

- `data/kalshi_external_backtest.db`
- Official Kalshi 1-minute candles
- BTC 1-minute candles
- Entry modeled at executable same-side ask
- Exit modeled at same-side bid or settlement
- Fees modeled as Kalshi taker fee on both legs: `7 * p * (1-p)` cents/contract

Fee-corrected production candidate:

```text
DISCOUNT-only, immediate ask:
n=1202
WR=59.9%
avg gross=+5.61c/contract
avg net=+3.21c/contract
total net=+3863.8c at 1ct
maxDD=610.7c at 1ct
```

Bias mode looked attractive by win rate but not by net:

```text
BIAS-only:
n=1170
WR=71.5%
avg net=+2.07c

Combined discount+bias:
n=1482
WR=68.8%
avg net=+2.38c

Combined sub-bucket:
discount: n=743, WR=62.3%, avg net=+4.79c
bias:     n=739, WR=75.2%, avg net=-0.04c
```

Conclusion: keep `ATM_BIAS_ENABLED = False`.

## New Finding: Entry Mechanism Is Leaving Edge On The Table

Current backtest/paper ATM enters at the first qualifying executable ask. That does not mean it enters the local bottom of the up/down contract.

Using intraminute Kalshi OHLC from `raw_json`:

```text
Immediate ask entry quality:
Average overpay vs best same-side ask in next 1m:  7.21c
Average overpay vs best same-side ask in next 3m: 11.53c
Within 2c of next-3m local bottom: 14.9%
Overpay >=5c vs next-3m local bottom: 76.4%
```

But blindly waiting for pullbacks hurt because it skipped fast repricers.

Variant replay results:

```text
immediate_ask:
n=1202, missed=0, WR=59.9%, net=+3863.8c, avg_net=+3.21c, maxDD=610.7c

maker_bid_wait_1m:
n=1116, missed=86, WR=59.4%, net=+4438.7c, avg_net=+3.98c, maxDD=392.7c

maker_bid_wait_3m:
n=1149, missed=53, WR=58.9%, net=+4374.3c, avg_net=+3.81c, maxDD=421.1c

pullback_2c_wait3m:
n=1106, missed=96, WR=56.5%, net=+3790.1c, avg_net=+3.43c, maxDD=435.5c

pullback_5c_wait3m:
n=944, missed=258, WR=51.5%, net=+3212.2c, avg_net=+3.40c, maxDD=423.4c
```

Best simple next hypothesis: **maker at current bid, wait up to 60 seconds**. It improves net and drawdown in replay, but live queue priority may reduce fills. This must be paper-tested before live promotion.

## Claude Task 1: Add Entry-Mode Backtest Support

Enhance `scripts/atm_reversion_backtest.py` so entry mode is a first-class argument rather than one-off analysis code.

Add:

```text
--entry-mode immediate_ask | maker_bid_wait | pullback_wait
--entry-wait-s 60
--entry-pullback-c 2
```

Implementation notes:

- `immediate_ask`: current behavior.
- `maker_bid_wait`: at signal time, set intended entry to same-side bid; fill if subsequent intraminute same-side ask low reaches that bid within wait window.
- `pullback_wait`: set intended entry to `signal_ask - pullback_c`; fill if subsequent intraminute same-side ask low reaches that price within wait window.
- If not filled, skip that ticker. Preserve one trade per ticker.
- Use candle `raw_json` OHLC for low/high where available.
- For NO side:
  - NO ask = `100 - yes_bid`
  - NO ask low occurs when YES bid high is highest, so `no_ask_low = 100 - yes_bid_high`
  - NO bid = `100 - yes_ask`

Acceptance criteria:

- Default `--entry-mode immediate_ask` reproduces the current production result within rounding.
- `--entry-mode maker_bid_wait --entry-wait-s 60` reports approximately:
  - `n ~= 1116`
  - `avg_net ~= +3.98c`
  - `maxDD ~= 392.7c`
- Summary must include missed/skipped setup count.

## Claude Task 2: Add Live Paper Entry-Mode Shadowing

Do not change live execution yet.

Add paper-only tracking fields for ATM so we can compare actual book behavior:

```text
entry_mode
signal_yes_bid
signal_yes_ask
signal_side_bid
signal_side_ask
intended_entry_c
filled_simulated
sim_fill_delay_s
min_same_side_ask_60s
min_same_side_ask_180s
max_same_side_bid_60s
max_same_side_bid_180s
```

Goal: answer whether real-time WS books confirm the 1-minute candle replay.

Recommended implementation:

- Keep the existing paper `immediate_ask` trade path as baseline.
- Add a parallel shadow candidate for `maker_bid_wait_60s`.
- The shadow candidate should not block the baseline paper trade.
- For every ATM signal, track same-side ask/bid for 180 seconds or until ticker rolls.
- Persist signal-level rows even when the maker simulation does not fill.

Suggested new table:

```sql
CREATE TABLE IF NOT EXISTS atm_reversion_entry_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    strategy_label TEXT,
    side TEXT,
    signal_ts REAL,
    signal_age_s REAL,
    signal_btc REAL,
    strike REAL,
    strike_dist_pct REAL,
    signal_yes_bid INTEGER,
    signal_yes_ask INTEGER,
    signal_side_bid INTEGER,
    signal_side_ask INTEGER,
    intended_entry_c INTEGER,
    entry_mode TEXT,
    filled_simulated INTEGER DEFAULT 0,
    sim_fill_ts REAL,
    sim_fill_delay_s REAL,
    min_same_side_ask_60s INTEGER,
    min_same_side_ask_180s INTEGER,
    max_same_side_bid_60s INTEGER,
    max_same_side_bid_180s INTEGER,
    final_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
```

Acceptance criteria:

- No real orders.
- No interference with SR_FADE.
- One shadow signal row per ATM signal per ticker.
- After a few windows, this query should work:

```sql
SELECT entry_mode,
       COUNT(*) signals,
       SUM(filled_simulated) fills,
       ROUND(AVG(sim_fill_delay_s), 1) avg_fill_delay,
       ROUND(AVG(signal_side_ask - min_same_side_ask_60s), 2) avg_60s_overpay
FROM atm_reversion_entry_shadow
GROUP BY entry_mode;
```

## Claude Task 3: Size The Engine Like A $100 Account, Not A Fantasy 100ct Book

Add a sizing backtest mode or separate script for fixed-dollar/balance sizing.

Needed outputs:

```text
start_balance_usd
fixed_contracts
fractional_balance_mode
final_balance
max_drawdown
ruin_count / min_balance
largest_position_cost
```

Initial safety posture:

```python
ATM_FIXED_SIZE_CONTRACTS = 5  # paper/live validation
ATM_MAX_SIZE_CONTRACTS = 20
ATM_MAX_BALANCE_FRAC = 0.10
```

Do not promote 100ct sizing from the current backtest. The `100ct` number is a scale illustration, not an account-management recommendation.

## Claude Task 4: Keep HQ Bull Separate

Do not merge the high-win-rate bullish filter into the baseline ATM engine.

It is a separate tiny-sample specialist:

```text
YES only
entry 33-35c
age 4-12 min
strike distance 0.0025%-0.015%
Kalshi volume >= 15000
```

Strict 10-trade version had 100% WR, but this is sample-collapse territory. Useful as a paper sub-tier only:

```python
ATM_HQ_BULL_ENABLED = False  # paper first if implemented
```

## Do Not Do Yet

- Do not enable live ATM orders.
- Do not enable bias mode.
- Do not raise size based on the fixed 100ct PnL display.
- Do not optimize for 100% win rate with tiny sample buckets.
- Do not remove the `0.080` strike escape stop; removing it did not improve net.

## Recommended Immediate Build Order

1. Add entry-mode support to `scripts/atm_reversion_backtest.py`.
2. Verify `maker_bid_wait_60s` result in the formal script.
3. Add live paper entry-shadow table to compare immediate ask vs maker bid wait.
4. Run engine for paper accumulation.
5. Re-run after 24h:

```sql
SELECT entry_mode,
       COUNT(*) signals,
       SUM(filled_simulated) fills,
       ROUND(AVG(signal_side_ask - min_same_side_ask_60s), 2) avg_60s_overpay
FROM atm_reversion_entry_shadow
GROUP BY entry_mode;
```

6. Only if live paper confirms replay, add a future live execution mode:

```python
ATM_ENTRY_MODE = "maker_bid_wait"
ATM_ENTRY_WAIT_S = 60
ATM_ENTRY_FALLBACK_TAKER = False
```

## Bottom Line

ATM Reversion likely has a real dislocation edge, but current immediate-ask entry is crude. The highest-value improvement is not another signal filter. It is entry mechanics:

```text
signal remains ATM discount
entry changes from "pay first ask" to "rest at current bid briefly"
exit remains +8c or 49c
stop remains strike escape 0.080%
size remains small until live paper confirms fill mechanics
```

