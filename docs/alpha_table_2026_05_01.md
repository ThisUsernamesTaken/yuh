# Alpha Extraction Report

- Generated UTC: 2026-05-02T05:14:53+00:00
- Since filter: `2026-04-29T00:00:00+00:00`
- Engine trades analyzed: 42
- Manual buy fills analyzed: 47

## Executive Summary

- Engine net: $-150.43 across 42 trades, WR 64.3%.
- Treat this as an entry/exit diagnostic, not account-truth P&L. Account truth remains Kalshi balance snapshots.
- Candidate rules below show retrospective deltas only; they are filters to validate, not proof of forward edge.

## Strategy Totals

| Strategy | N | WR | Net | Avg/trade |
|---|---:|---:|---:|---:|
| TA_FORCED_SIGNAL | 42 | 64.3% | $-150.43 | $-3.58 |

## Engine Buckets

### Entry Price

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| 36-50c | 7 | 57.1% | $-52.21 | $-7.46 | 1 |
| 51-65c | 17 | 64.7% | $-88.10 | $-5.18 | 2 |
| 66-80c | 13 | 84.6% | $-9.43 | $-0.73 | 0 |
| 81-99c | 5 | 20.0% | $-0.69 | $-0.14 | 0 |

### Gate Session Minute

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| unknown | 42 | 64.3% | $-150.43 | $-3.58 | 3 |

### Regime

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| chop | 25 | 72.0% | $-76.91 | $-3.08 | 2 |
| structured | 17 | 52.9% | $-73.52 | $-4.32 | 1 |

### Regime Volatility

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| 0.15-0.50 | 13 | 69.2% | $-36.88 | $-2.84 | 0 |
| 0.50-1.00 | 13 | 76.9% | $-49.60 | $-3.82 | 2 |
| 1.00-2.00 | 12 | 41.7% | $-58.28 | $-4.86 | 1 |
| <0.15 | 2 | 50.0% | $-8.29 | $-4.14 | 0 |
| >=2.00 | 2 | 100.0% | $2.62 | $1.31 | 0 |

### Raw Edge

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| 08-15pp | 8 | 62.5% | $6.11 | $0.76 | 0 |
| 15-25pp | 11 | 72.7% | $-20.86 | $-1.90 | 1 |
| 25-40pp | 10 | 70.0% | $-61.65 | $-6.17 | 1 |
| <8pp | 11 | 63.6% | $-8.91 | $-0.81 | 0 |
| >=40pp | 2 | 0.0% | $-65.12 | $-32.56 | 1 |

### BTC 5m Distance/Momentum

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| $100-200 | 6 | 66.7% | $3.06 | $0.51 | 0 |
| $25-50 | 12 | 58.3% | $-146.62 | $-12.22 | 3 |
| $50-100 | 15 | 60.0% | $15.89 | $1.06 | 0 |
| <$25 | 9 | 77.8% | $-22.76 | $-2.53 | 0 |

### UTC Weekday:Hour

| Bucket | N | WR | Net | Avg/trade | Cat losses |
|---|---:|---:|---:|---:|---:|
| 2:00Z | 2 | 100.0% | $2.10 | $1.05 | 0 |
| 2:01Z | 2 | 50.0% | $-58.88 | $-29.44 | 1 |
| 2:05Z | 1 | 100.0% | $2.27 | $2.27 | 0 |
| 2:06Z | 1 | 0.0% | $-17.70 | $-17.70 | 0 |
| 2:10Z | 1 | 100.0% | $1.60 | $1.60 | 0 |
| 2:11Z | 4 | 100.0% | $4.00 | $1.00 | 0 |
| 2:13Z | 2 | 50.0% | $-19.84 | $-9.92 | 0 |
| 2:14Z | 2 | 100.0% | $3.73 | $1.86 | 0 |
| 2:15Z | 2 | 50.0% | $6.25 | $3.12 | 0 |
| 2:16Z | 1 | 0.0% | $-31.01 | $-31.01 | 1 |
| 2:22Z | 1 | 0.0% | $-0.32 | $-0.32 | 0 |
| 2:23Z | 2 | 50.0% | $0.94 | $0.47 | 0 |
| 3:00Z | 1 | 100.0% | $1.89 | $1.89 | 0 |
| 3:01Z | 2 | 50.0% | $-49.81 | $-24.91 | 1 |
| 3:02Z | 1 | 100.0% | $1.50 | $1.50 | 0 |
| 3:04Z | 1 | 0.0% | $-7.90 | $-7.90 | 0 |
| 3:05Z | 2 | 50.0% | $-1.45 | $-0.72 | 0 |
| 3:09Z | 1 | 100.0% | $0.27 | $0.27 | 0 |
| 3:12Z | 2 | 50.0% | $-0.48 | $-0.24 | 0 |
| 3:13Z | 2 | 100.0% | $20.60 | $10.30 | 0 |
| 3:14Z | 3 | 66.7% | $3.59 | $1.20 | 0 |
| 3:15Z | 2 | 50.0% | $-7.37 | $-3.69 | 0 |
| 3:16Z | 1 | 0.0% | $-6.60 | $-6.60 | 0 |
| 4:00Z | 1 | 100.0% | $2.47 | $2.47 | 0 |
| 4:01Z | 1 | 100.0% | $1.12 | $1.12 | 0 |
| 4:02Z | 1 | 0.0% | $-1.40 | $-1.40 | 0 |

## Candidate Retrospective Filters

| Rule | Base N | Kept N | Retrospective Delta | Kept WR | Kept Net |
|---|---:|---:|---:|---:|---:|
| Keep entry <= 35c | 42 | 0 | $150.43 | n/a | $0.00 |
| Keep entry <= 50c | 42 | 7 | $98.22 | 57.1% | $-52.21 |
| Block entry 51-65c | 42 | 25 | $88.10 | 64.0% | $-62.33 |
| Block session minute >= 9 | 42 | 42 | $0.00 | 64.3% | $-150.43 |
| Block vol >= 1.0 | 42 | 28 | $55.66 | 71.4% | $-94.77 |

## MFE / MAE Coverage

- Trades with excursion fields: 26
- Positive-trade average MFE capture ratio: 10.6%
- Worst MAE: 1c

## Manual Fill Context

| Entry bucket | Fills | Contracts | Avg price | Settled PnL |
|---|---:|---:|---:|---:|
| 00-25c | 5 | 1716 | 9.0c | $0.00 |
| 36-50c | 30 | 2386 | 44.3c | $0.00 |
| 51-65c | 2 | 140 | 58.0c | $0.00 |
| 81-99c | 10 | 1139 | 98.0c | $0.00 |

## Next Implementation Decision

Use this report to decide whether Phase 2 cheap-side gating is backed by data.
If MFE coverage is missing or sparse, implement/backfill excursion tracking before changing trailing exits.

