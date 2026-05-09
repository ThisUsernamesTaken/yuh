# BTC Bias Engine — Evening Monitoring Session 2026-05-08

**Goal**: Observe engine behavior under the post-`56acf0b` config, take notes,
extract actionable insights without shipping more changes until we have data.

## Active config (post all 4 commits today)

```python
# Detection-based exits — DISABLED
DIRECTION_TRAIL_PHASE1-4_C            = 99 (disabled)
DIRECTION_WALL_EXIT_ENABLED           = False
DIRECTION_CONVERGENCE_TAKE_ENABLED    = False
DIRECTION_REVERSION_EXIT_ENABLED      = False
SCALP_TRAIL_*_C                       = 99 (disabled)
SCALP_BTC_TRAIL_DOLLARS               = 99999 (disabled)
SCALP_INVERSE_REENTRY_ENABLED         = False (legacy, replaced by below)

# Auto-TP machinery — DISABLED
SYNC_RECLAIM_AUTO_TP_ENABLED          = False
SCALP_TO_HOLD_UPGRADE_ENABLED         = False
SYNC_RECLAIM_HOLD_TO_SETTLE_ENABLED   = True   (kills PROTECTIVE [TP])

# Active mechanisms
INVERSE_REENTRY_ON_CLOSE_ENABLED      = True   (+4c IOC opp on close)
INVERSE_REENTRY_SLIP_C                = 4
INVERSE_REENTRY_CONTRACTS             = 5
SCALP_PRE_IOC_TICKER_LOCK_CHECK       = True   (kills retry-on-same-ticker bug)

# Threshold safeties (always)
DIRECTION_NEAR_CERTAIN_C              = 75    (TAKE-CEILING)
DIRECTION_MAX_LOSS_C                  = 20    (LOSS-CUT)
DIRECTION_FORCE_FLATTEN_S             = 60    (FORCE-FLATTEN)
SCALP_NEAR_CERTAIN_C                  = 90
SCALP_MAX_LOSS_C                      = 15
SCALP_PRE_EXPIRY_S                    = 60
```

## Baseline (start of monitoring window)

- **Wall clock**: 2026-05-08 20:48 PT
- **BAL**: $69.29
- **Position**: FLAT
- **Last engine restart**: 20:01:57 PT (commit 56acf0b)

## P&L since 56acf0b restart (20:01 PT)

| Time | Event | BAL | Δ |
|---|---|---|---|
| 20:01:57 | Restart | $66.01 | baseline |
| 20:48:39 | Now | $69.29 | **+$3.28 in 47 min** |

## Trade ledger — Window-by-window

### Window `-26MAY082315-15` (20:00-20:15 ET)
| Time | Action | Side | Qty | Price | Tier | Notes |
|---|---|---|---|---|---|---|
| 20:01:21 | SELL | YES | 5 | y=0.40 | (legacy carry) | Closed prior position |
| 20:01:29 | BUY | YES | 5 | y=0.38 | SCALP MARKET | Engine entry |
| 20:02:50 | — | — | — | — | PROTECTIVE [SL] | Stop-loss placed (bid dropped) |
| 20:02:52 | (multiple) | NO | 5+5 | n=0.71 | various | Closing complex |
| 20:03:00 | BUY | NO | 5 | n=0.71 | **INVERSE-REENTRY** | +4c IOC opposite on close — **FIRST USE OF MY NEW CODE, IT WORKED** |
| 20:03:06 | SKIP-REENTRY | — | — | — | LOCK | Further entries blocked (1-per-window) ✓ |
| 20:03:21 | SELL | YES | 10 | y=0.25 | settlement-ish | Position effectively closed |

### Window `-26MAY082330-30` (20:15-20:30 ET)
| Time | Action | Side | Qty | Price | Tier | Notes |
|---|---|---|---|---|---|---|
| 20:15:18 | (false NOFILL log) | NO | — | IOC@67c | SCALP MARKET | Actual fill at ask 62c |
| 20:15:18 | BUY | NO | 5 | n=0.62 | (Kalshi truth) | Real fill |
| 20:15:25 | SYNC RECLAIM | — | — | — | reclaim | "tagged _hold_to_settle=True (PROTECTIVE auto-TP will stand down)" — **MY NEW FIX WORKING** |

### Window `-26MAY082345-45` and `-26MAY090000-00`
| 20:30:17 | SCALP SKIP | — | — | — | _window_locked=True | **STARTUP-SKIP STUCK ON?** |
| 20:45:18 | SCALP SKIP | — | — | — | _window_locked=True | Same. Worth investigating. |

## Observations so far

### What's working
1. **INVERSE_REENTRY_ON_CLOSE fired correctly** — placed +4c above NO ask, filled at ask (depth available). Code path validated.
2. **SCALP_PRE_IOC_TICKER_LOCK_CHECK** — no duplicate entries on same ticker observed since 56acf0b.
3. **SYNC_RECLAIM_HOLD_TO_SETTLE_ENABLED** — `tagged _hold_to_settle=True` log line confirms PROTECTIVE auto-TP standing down.
4. **BAL net positive** — +$3.28 over 47 minutes.

### What needs investigation
1. **`_window_locked=True` persisting for 30+ minutes after restart** — engine should clear this on first window flip but didn't. Will miss ~2-3 windows of opportunities each restart.
2. **PROTECTIVE cancel returned False AND order still executed** errors at 20:02:55-59. Cancel-then-place race condition still exists somewhere.
3. **False NOFILL bug persists** — every SCALP MARKET still logs NOFILL when actually filled. Just cosmetic with the lock check, but still misleading.

### Potential actionable insights (preliminary, need more data)
- The +4c INVERSE_REENTRY filling at the ASK (lower than our limit) suggests we're often paying full slippage. Maybe try +2c.
- The 1-per-window lock (`SKIP-REENTRY` after INVERSE-REENTRY fires) means we get exactly ONE entry + ONE re-entry per window. Good safety, may limit upside.
- The startup-skip lock leaving multiple windows un-traded is a real cost. Need to fix or accept.

## Schedule
Next checkpoint: ~21:18 PT (30 min from baseline)
