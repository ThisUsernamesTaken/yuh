# =============================================================================
# user_config.py â€” REVERTED TO PROFITABLE VERSION
# =============================================================================
# Matched to the zip version that took the account to $150+.
# No sniper, no sell ladder, no price action, no flip reentry.
# =============================================================================

ENGINE_DIR = r"C:\Trading\btc-bias-engine"

# â”€â”€ Capital & Risk â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Percentage-based sizing â€” caps scale with balance instead of fixed dollars.
# SIZING_BALANCE_FRACTION = normal per-entry allocation (% of balance).
# SIZING_MAX_FRACTION     = absolute ceiling (% of balance) after boosts/DCA.
#   Example at $100 balance: entry ~$30, max any one position ~$50.
#   Example at $500 balance: entry ~$150, max any one position ~$250.
# SIZING_MAX_DOLLARS kept only as non-binding fallback (set very high) so
# nothing ever trips the legacy fixed-dollar ceiling under normal growth.
SIZING_BALANCE_FRACTION = 0.10      # 2026-04-29 retune (was 0.30): tighter base
                                     # sizing aligned with parallel-terminal
                                     # post-incident config. Kelly path still
                                     # primary; this is the legacy fallback.
SIZING_MAX_FRACTION     = 0.08      # 2026-05-01 safety retune (was 0.20):
                                     # absolute ceiling per position. Worst-
                                     # case loss bounded at 8% of balance.
SIZING_MAX_DOLLARS      = 10_000.0  # fallback only (effectively unbinding)
MIN_BALANCE_TO_TRADE = 2.00
DAILY_LOSS_LIMIT = 15.00            # 2026-04-29 retune (was 500.00): static
                                     # fallback ONLY. Live limit is dynamic
                                     # via DAILY_LOSS_FRACTION below — recom-
                                     # puted as balance × frac on every check.
DAILY_LOSS_FRACTION = 0.20          # 2026-04-29 NEW: live daily-loss limit
                                     # = balance × 0.20. At $76 balance →
                                     # $15.20 limit; at $500 → $100 limit.
                                     # Set to 0.0 to fall back to static
                                     # DAILY_LOSS_LIMIT.

# ── Contract caps (2026-04-29 retune — balance-proportional) ──────────────
# Static SIZING_HARD_CAP_CONTRACTS_DAY/_NIGHT are FALLBACK ONLY now. Live
# caps come from SIZING_CAP_BALANCE_FRAC_DAY/_NIGHT × _LIVE_BALANCE_DOLLARS,
# computed on every _get_sizing_cap() call. Set the FRAC values to 0.0
# to fall back to the static numbers.
#
# Worked examples (balance × FRAC = ct cap):
#   $76 × 0.30 = 22ct day,   $76 × 0.10 = 7ct night
#   $200 × 0.30 = 60ct,      $200 × 0.10 = 20ct
#   $500 × 0.30 = 150ct,     $500 × 0.10 = 50ct
#   $1000 × 0.30 = 300ct,    $1000 × 0.10 = 100ct
SIZING_HARD_CAP_CONTRACTS_DAY   = 15   # 2026-05-03 PoC: bumped from 8 → 15.
                                       # At $40 bankroll, 0.10 Kelly cap × $40 = $4
                                       # max bet → ~10-25ct depending on entry
                                       # price. Need at least 15ct hard cap to
                                       # let cheap-side entries (5-15c) actually
                                       # size meaningfully.
SIZING_HARD_CAP_CONTRACTS_NIGHT = 10   # 2026-04-29 (was 100): static fallback
SIZING_CAP_BALANCE_FRAC_DAY     = 0.30 # 2026-04-29 NEW: live day-cap fraction
SIZING_CAP_BALANCE_FRAC_NIGHT   = 0.10 # 2026-04-29 NEW: live night-cap fraction

# ── TA_FORCED minimum fill floor (Claude 2026-04-27) ────────────────────────
# Today's data: 1ct dribbler trades at 77c, 91c entries netted +$0.01-$0.07 each.
# Useful as paper observability, useless as real P&L. The 12ct@62c trade was the
# real contributor (+$3.96). Skip trades whose final size is below this floor
# rather than fire 1ct insurance lottery tickets that flood the notification
# channel. Set to 0 to disable the floor (legacy behavior).
TA_FORCED_MIN_FILL_CONTRACTS = 5

# ── Mid-session no-trade zone (Claude 2026-04-27) ───────────────────────────
# User directive after -$36 ghost-fill loss day: contrarian entries past
# minute 7 are bad — time premium has decayed enough that the FVG/BB fair
# value has insufficient runway to mean-revert before expiry. The two big
# losers today (14ct YES @ 43c at minute 12 → -$6.10, 71ct YES @ 49c at
# minute 7:44 → -$29.64) both fired in this dead zone.
#
# Block ALL new entries from session-minute FROM through (UNTIL - 1).
# Position MANAGEMENT (TP, stop, flatten, residual reconcile) keeps
# running — the gate only blocks NEW orders.
#
# Final 3 minutes (UNTIL through 15) reserved for LATE_DOMINANT tier:
# momentum-aligned entry on whichever side BTC is currently on, gated on
# volume-profile confidence + 0.05% strike distance.
SESSION_NO_TRADE_MIN_FROM  = 7    # session minute (inclusive) where block starts
SESSION_NO_TRADE_MIN_UNTIL = 12   # session minute (exclusive) where block ends
SESSION_NO_TRADE_ZONE_ENABLED = True  # kill switch

# ── LATE_DOMINANT tier (Claude 2026-04-27) ──────────────────────────────────
# Final-3-minute momentum-aligned entry. Direction = side BTC is currently
# on (BTC > strike → YES; BTC < strike → NO). Only fires when:
#   1. session minute >= SESSION_NO_TRADE_MIN_UNTIL (12)
#   2. minutes_to_expiry >= 1 (1-min buffer before settlement cliff)
#   3. |BTC - strike| / BTC >= LATE_DOMINANT_DIST_FLOOR (0.05% = ~$36 @ $72k)
#   4. BtcVolumeTracker not stale; ≥ 30 prints in window
#   5. side bid in [10, 55]
#   6. confidence composite >= LATE_DOMINANT_MIN_CONF
LATE_DOMINANT_ENABLED            = False  # 2026-05-01 09:20 PT: DISABLED. Legacy momentum-scalp path. Same chaos pattern as TA_FORCED. BB_PURE-only mode.
                                            # alongside TA_FORCED after forensic
                                            # confirmed the 1164ct event was user
                                            # manual trading. Safety reconciler
                                            # now distinguishes manual vs engine.
LATE_DOMINANT_DIST_FLOOR         = 0.0005  # 0.05% of BTC price
# 2026-04-27 evening tuning after first 3 live signals: at minute 12+ with BTC
# decisively past strike, the winning side is ALWAYS priced 80-99c (in-the-money
# carry). Original 10-55c band caught only the LOSING side — opposite of the
# "side about to close" thesis. Restructured around the carry-trade math:
# pay 80-96c, scalp the 4-20c remaining premium on a near-certain settlement.
# Threshold raised to 0.75 because at 90c entry the EV math demands
# P(win) > ~90%, which only the highest-conviction tape conditions deliver.
LATE_DOMINANT_MIN_CONF           = 0.75    # was 0.65 — raised for carry-trade EV
LATE_DOMINANT_MIN_VOL_PRINTS     = 30      # min trade prints in tracker window
LATE_DOMINANT_SIZE_MIN_CT        = 5       # min contracts when conf >= threshold
# 2026-04-28 evening: day/night cap pair to match TA_FORCED. During US active
# hours, scale up to 50ct on full conviction; overnight stay at 30ct.
LATE_DOMINANT_SIZE_MAX_CT_DAY    = 50      # max contracts at conf == 1.0 (day)
LATE_DOMINANT_SIZE_MAX_CT_NIGHT  = 10      # 2026-04-29 retune (was 30)
LATE_DOMINANT_SIZE_MAX_CT        = 30      # legacy single-knob (back-compat)
LATE_DOMINANT_MIN_ENTRY_CENTS    = 80      # was 10 — winning side is in-the-money
LATE_DOMINANT_MAX_ENTRY_CENTS    = 96      # was 55 — need ≥4c upside vs fees
# Confidence composite weights (must sum to 1.0)
LATE_DOMINANT_W_DISTANCE   = 0.25
LATE_DOMINANT_W_CUM_SHARE  = 0.20
LATE_DOMINANT_W_AGGRESSOR  = 0.20
LATE_DOMINANT_W_POC        = 0.15
LATE_DOMINANT_W_VWAP       = 0.10
LATE_DOMINANT_W_VELOCITY   = 0.10

# ── LATE_DOMINANT stop loss (Claude 2026-04-27) ─────────────────────────────
# Real stop, position-truth based. Unlike the disabled SCALP HARD STOP (which
# fired on mean-reversion sessions and burned recoverable losses), LATE_DOMINANT
# is a momentum entry with at most 3 minutes of expiry runway — there's no
# time for mean-reversion to bail us out. If price moves through the strike
# against our side and the bid drops STOP_CENTS below entry, get out.
#
# Uses get_positions() for truth count rather than the engine's tracked count
# so a residual / ghost position still gets fully flattened.
LATE_DOMINANT_STOP_ENABLED   = True
LATE_DOMINANT_STOP_CENTS     = 8     # bid drop below entry → flatten

# â”€â”€ Take Profit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TAKE_PROFIT_CENTS = 8

# â”€â”€ Entry Price Bands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MIN_ENTRY_CENTS = 35                # Lowered â€” microstructure pressure validates entries
MIN_ENTRY_CENTS_NO = 35
MAX_ENTRY_CENTS = 75                # DATA: 50-64c=58% WR, 65-79c=63% WR. 80c+=tiny sample.
# A flat NO-side cap was CONSIDERED 2026-04-18 after trade 2236 (-$49.85
# NO @ 62c) but rejected: the engine's existing 2026-04-14 comment shows
# NO 65-74c is the only profitable NO band (+$2.89 on 10 trades), while
# NO 55-64c is the worst (-$20.63). A flat 59c cap blocks the good band
# and allows the bad one. Trade 2236 was an INVERTED entry â€” killed by
# TA_INVERSION_ENABLED=False above, which is the correct fix.

# â”€â”€ Signal Thresholds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MIN_DIVERGENCE = 0.01
MIN_SMART_WALLETS = 1
MIN_FLOW_CONVICTION = 0.50
STOP_LOSS_CENTS = 8
STOP_LOSS_CENTS_PRIMARY_WIDE = 8
STOP_LOSS_CENTS_HIGH_ENTRY = 5
HIGH_ENTRY_STOP_THRESHOLD = 83
STOP_GRACE_PERIOD_S = 15

# â”€â”€ Mimic â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MIMIC_ENABLED = False
MIMIC_MIN_WALLET_WR = 0.70

# â”€â”€ Trading Schedule â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DIVERGENCE_LADDER_ENABLED = True
# 2026-04-30 user directive: "disable overnight 10 PM - 7 AM EST".
# Hours are interpreted as ET (US/Eastern, auto-handles EST/EDT).
# 22-06 inclusive = 10 PM through 6:59 AM ET = 9 hours blocked.
# Position management (TPs, stops, flattens) still runs — only NEW
# entries are blocked during these hours.
BLOCKED_HOURS = {22, 23, 0, 1, 2, 3, 4, 5, 6}

# â”€â”€ TA Forced â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 2026-04-22: TA_FORCED evaluator MUST stay True â€” it's the function that
# runs microstructure.update(), regime_clf.update(), and contract_sr.update()
# every cycle. Disabling TA_FORCED_ENABLED silences the entire data pipeline
# and starves SR-FADE of the btc_5m_move + level evidence it needs.
TA_FORCED_ENABLED = True
# NEW: entry-only kill switch. TA_FORCED tier runs its evaluator and feeds
# the data pipeline, but will NOT produce an entry signal. This disables the
# tier's trades without breaking downstream evaluators.
# 2026-04-27 12:35 PT: re-enabled per user directive. TA_FORCED IS the FVG /
# Brownian-Bridge engine (whitepaper §4).
# 2026-04-27 ~20:10 PT — HALTED per user. Two losing trades today (-$6.10 14ct,
# -$29.64 71ct) traced to GHOST/SYNC RECONCILE bug: engine logs 9ct entry,
# Kalshi accumulates 71ct via SCALP DCA retries / partial fill races. Engine
# count diverges from Kalshi truth by 5-10x by window expiry.
# 2026-04-28 — RE-ENABLED. GHOST fix shipped: SCALP DCA is now strictly single-
# shot (sets _scalp_dca_fired=True immediately after place_order, cancels any
# resting limit on unfilled). Combined with the new mid-session no-trade zone
# (min 7-12) and the 5ct min-fill floor, the failure modes that produced the
# 71ct ghost are blocked. Watch for SCALP DCA logs and any GHOST IGNORED /
# SYNC RECONCILE adopting unexpected counts.
# 2026-04-28 07:52 PT — DISABLED AGAIN. Trade 5 (NO 30x @ 56c) closed via TP
# ladder + ORPHAN CATCH at 07:48:25 with logged P&L_CORRECTION =$-0.47. Then
# 3 minutes later (07:51:51) ESCROW LEAK -$996.19 appeared with no engine-
# tracked position. SYNC RESIDUAL FLATTEN at 07:51:56 found 1164 NO contracts
# at avg 5.5c (~$64 cost) — the engine lost track of a massive position
# accumulation. Possible cause: TP ladder cancel-then-replace race after
# window-lock left orphan resting orders that filled at low prices.
# 2026-04-28 11:15 PT — RE-ENABLED. Forensic confirmed the 1164ct + 1221ct
# events were USER MANUAL TRADES, not engine oversells. Safety reconciler
# now has manual-detection gates (size > 150ct OR untouched ticker OR
# kalshi_count > 1.5x engine's known fill → leave alone). 13/13 manual-TP
# tests pass; 11/11 reconciler tests pass. Re-enabling TA_FORCED entries.
TA_FORCED_ENTRY_ENABLED = False  # 2026-05-05 PT 21:55: RE-DISABLED.
                                  # Was re-enabled earlier today as portfolio
                                  # mode (with DIRECTION) but that was scope
                                  # creep — operator asked for conviction-
                                  # based sizing, NOT a strategy re-enable.
                                  # Reverting to DIRECTION-only with the
                                  # actual conviction multiplier built. Keep
                                  # TA_FORCED off until a separate decision
                                  # is made about portfolio composition.
                                  # Prior comment block follows for context.
                                  # ──────────────────────────────────────
                                  # 2026-05-05 PT: RE-ENABLED.
                                  # Empirical realized P&L 04-21 to 05-01:
                                  # 81 trades, 67.9% win rate, +$611 net
                                  # (kalshi_trades.strategy_name='TA_FORCED').
                                  # Account peak $1,332 on 04-28 ridden by
                                  # this strategy. The 05-01 catastrophe
                                  # came from DCA stack + TIERED_TP, not
                                  # the entry signal itself. Loss-adders
                                  # are confirmed off:
                                  #   SCALP_DCA_ENABLED        = False
                                  #   TP_LAYERED_ENABLED       = False
                                  #   MICRO_PULLBACK_ENABLED   = False
                                  #   SNIPER_ENABLED           = False
                                  #   WALLET_COPY_ENABLED      = False
                                  # Sizing uses fixed-fraction (8% day,
                                  # 2% night) capped at 15ct day / 5ct
                                  # night. Runs in portfolio with
                                  # DIRECTION_STRATEGY (different alpha,
                                  # one-trade-per-window lock applies).
                                  # 2026-05-01 PRIOR DISABLE: TA_FORCED was
                                  # the source of major losses morning
                                  # -$249 + afternoon -$83 — both via DCA
                                  # stack overrun, NOT the entry signal.

# ── DOMINANT-DIRECTION gate tuning (Claude 2026-04-28) ──────────────────────
# Gate 1 of 4 in the DOMINANT filter (polymarket_copy_engine.py:~7421).
# Required BTC movement over 5min in the FVG direction. Was hardcoded $30
# which produced a 91% rejection rate against valid FVG signals — most often
# when btc5m=$0 in mean-reverting sessions (the engine's design target).
# 30d data showed 1-4ct trades (those that survived this filter + damper)
# had 79% WR and +$30 EV/trade — the signal works when it fires, but it
# barely fires. Lowered to 15 to roughly double pass-through while keeping
# the "BTC must be moving with us" guard against trades into pure noise.
# Watch DOMINANT-SKIP/SHADOW-EDGE/TA_FORCED FILL ratios after the change;
# expect skip-rate to drop from ~91% toward ~70-80%.
DOMINANT_BTC_5M_THRESHOLD = 15.0

# ── Manual-fills capture (Claude 2026-04-28) ────────────────────────────────
# Polls Kalshi /portfolio/fills periodically. Any fill whose order_id is NOT
# in the engine's known engine_order_ids gets snapshotted into manual_fills
# with full engine-state context (BTC, book, FVG, pressure, regime, etc.) so
# we can analyze user manual-trade patterns and reproduce the alpha
# algorithmically. Independent of engine entries — runs even when
# TA_FORCED_ENTRY_ENABLED and LATE_DOMINANT_ENABLED are False.
MANUAL_FILLS_CAPTURE_ENABLED  = True
MANUAL_FILLS_POLL_INTERVAL_S  = 8.0     # poll every 8s; ring buffer aligns
                                          # state to actual fill_time so rapid
                                          # scalps under poll-interval still
                                          # get accurate context (within ~0.8s)

# ── Cross-side arbitrage detector (Claude 2026-04-28) ───────────────────────
# Scans active Kalshi ticker every ARB_POLL_INTERVAL_S; when yes_ask + no_ask
# is below threshold (after fee accounting), fires hedge buy on both sides.
# Each completed pair guarantees ~`100 - sum_ask - fee_winning_side` per ct.
# Algorithmic version of the user's manual hedge play that netted +$29.40 on
# 2389 pairs today.
ARB_DETECTOR_ENABLED          = False   # 2026-05-02 reset: was True for
                                        # observability. TA_FORCED is now
                                        # disabled so the regime-input use
                                        # case is gone. Detector logs were
                                        # noise. Re-enable for research only.
                                        # Trades are gated separately via
                                        # ARB_TRADES_ENABLED below (=False
                                        # currently, after extensive live
                                        # testing showed REST API latency
                                        # can't beat colocated HFT for
                                        # ARB execution).
ARB_TRADES_ENABLED            = False   # When False, _check_arb_opportunity
                                        # runs the full classification +
                                        # SCAN pipeline but skips actual
                                        # order placement. Keeps the data
                                        # feed alive without trading.

# ── Terminal-wallet copy engine (Claude 2026-04-29 per user) ──────────────
# Mirrors Polymarket BTC up/down buys from a curated 95%+ WR wallet list
# onto Kalshi at bid+1c. Runs in parallel with the FVG / TA_FORCED tier;
# does NOT share gating logic, so a wallet fire bypasses pressure /
# DOMINANT / RSI checks (the wallet's selectivity is the filter).
#
# Generation pipeline:
#   1. Run scripts/identify_terminal_wallets.py  → data/terminal_wallets.json
#   2. terminal_copy.py loads that file at startup, refreshes hourly
#   3. Polymarket /trades polled every WALLET_COPY_POLL_S seconds
#   4. New BUY by tracked wallet at >=WALLET_COPY_MIN_POLY_PRICE → fire
#
# Live observability runs even when WALLET_COPY_TRADES_ENABLED=False —
# we get the SIGNAL log lines without placing orders. Flip TRADES_ENABLED
# to True to actually mirror.
WALLET_COPY_ENGINE_ENABLED    = False   # 2026-04-29 reverted: copy engine
                                        # disabled, return to TA_FORCED primary
WALLET_COPY_TRADES_ENABLED    = False   # Defense in depth — even if engine
                                        # flag flips back on, no live orders
WALLET_COPY_POLL_S            = 2.5     # Polymarket /trades poll interval
WALLET_COPY_RELOAD_S          = 3600.0  # Wallet pool refresh interval
WALLET_COPY_MIN_POLY_PRICE    = 0.93    # Skip buys below this price (only
                                        # late-window certainty fires)
WALLET_COPY_MIN_POLY_SIZE     = 5.0     # Skip dust trades on Polymarket
WALLET_COPY_MIN_SESSION_AGE_S = 360     # 2026-04-29 per user: lowered from
                                        # 600s to 360s. Kalshi's price
                                        # discovery is faster than Polymarket's
                                        # ("locked out from lack of contracts
                                        # for sale" at minute 10). Polymarket
                                        # wallets enter at "minute two of
                                        # five" (= 60% time remaining); the
                                        # Kalshi-equivalent entry on a 15m
                                        # window is at minute 6 (= 9 min left).
# ── Wallet-quality runtime filter (Claude 2026-04-29 per user) ─────────────
# The terminal_wallets.json file is generated with WR>=0.95 / trades>=5.
# These runtime knobs let us tighten further without re-running the scanner:
WALLET_COPY_MIN_WR            = 1.0     # User directive: "any single wallet
                                        # with a 100% win rate". 1.0 = strict.
WALLET_COPY_MIN_TRADES        = 10      # Min sample size to filter out
                                        # 5/5 lucky-streak noise. Top
                                        # candidates (Pricey-Operator 50/50,
                                        # Stiff-Pocketwatch 26/26) all clear.
# ── Flat 95c limit (Claude 2026-04-29 per user) ───────────────────────────
# Replaces dynamic bid+1+walk with a simple flat limit price. Kalshi's
# matching engine handles all four cases:
#   - ask < 95: fills at actual ask (= we get cheaper than our limit)
#   - ask = 95: fills as taker
#   - ask > 95: rests as maker; fills only if a dip touches 95
#   - no asks:  rests, no fill, no harm
# Wallet ceiling preserves at least MIN_EDGE_C below the wallet's
# entry — if their poly price is too low to support 95c with edge,
# we skip rather than overpay.
WALLET_COPY_FLAT_LIMIT_C      = 95      # Per user: "set a limit buy for
                                        # 95 cents" 5 min before close
WALLET_COPY_MIN_EDGE_C        = 2       # Required cents of edge below
                                        # the wallet's poly entry price
WALLET_COPY_DOLLAR_SIZE       = 20.0    # USD risk per copy (per user
                                        # 2026-04-29: "test with $20 at
                                        # a time"). Sizing computed as
                                        # round($20 * 100 / entry_price).
                                        # At 90c entry → 22ct, at 50c → 40ct.
WALLET_COPY_MAX_CONTRACTS     = 200     # Hard ceiling regardless of dollar
                                        # math (= cap on cheap entries)
WALLET_COPY_MAX_ENTRY_C       = 99      # Never pay above this many c
WALLET_COPY_MAX_PER_DAY       = 8       # Bound daily copy attempts

# ── Per-tier pricing strategy (Claude 2026-04-29 per user) ────────────────
# The 100% WR wallet pool splits into three populations by volume-weighted
# avg entry price (= total_notional_usd / total_size, computed at load):
#
#   CERTAINTY  (avg_price >= 0.85)   — late-window high-conviction (e.g.
#                                     Pricey-Operator 50/50 @ ~0.95)
#   MID        (0.50 <= ap < 0.85)   — mid-window (Confused-Dead 23/23 @ 0.73)
#   CONTRARIAN (avg_price < 0.50)    — bet on reversal (Instructive-Flat
#                                     13/13 Down @ 0.49 @ $608/trade)
#
# The pricing strategy DIFFERS per tier:
#   - CERTAINTY: pay best_ask up to MAX_ENTRY_C (the move is mostly
#     resolved; we accept the small remaining premium for the high
#     hit rate).
#   - MID / CONTRARIAN: cap entry at min(best_ask, poly_price + premium).
#     If Kalshi has already run past the wallet's entry price by more
#     than the premium, the alpha is gone — skip rather than chase.
#
# Per-tier minimum poly price floor too: contrarians enter at 0.46 — the
# global 0.93 floor would filter them out entirely.
WALLET_COPY_TIER_PRICING_ENABLED = True

# Tier thresholds (volume-weighted avg_price)
WALLET_COPY_TIER_CERTAINTY_MIN  = 0.85   # ap >= this → CERTAINTY
WALLET_COPY_TIER_MID_MIN        = 0.50   # 0.50 <= ap < 0.85 → MID
                                          # ap < 0.50 → CONTRARIAN

# Per-tier max premium above the wallet's poly_price (in cents)
# CERTAINTY uses MAX_ENTRY_C globally (no per-trade poly cap), tracked
# as None below to mean "no relative cap".
WALLET_COPY_CERTAINTY_PREMIUM_C  = None  # No relative cap — just MAX_ENTRY_C
WALLET_COPY_MID_PREMIUM_C        = 10    # Pay up to poly_price+10c
WALLET_COPY_CONTRARIAN_PREMIUM_C = 5     # Pay up to poly_price+5c only

# Per-tier minimum poly price floor (signal qualification)
WALLET_COPY_CERTAINTY_MIN_POLY  = 0.93   # = global default
WALLET_COPY_MID_MIN_POLY        = 0.50   # at least their natural range
WALLET_COPY_CONTRARIAN_MIN_POLY = 0.30   # avoid extreme dust

# ── Microstructure-aware gating (Claude 2026-04-30) ────────────────────────
# Five-component upgrade to entry/exit decision-making, designed after the
# 2026-04-30 hold-to-expiry incident exposed two systemic weaknesses:
#   1. Engine catches falling knives — places maker at high limit, gets
#      adverse-selected by retail dumping
#   2. Bid-touch stops fire on single-tick wicks from one panicking
#      participant, selling the bottom of normal chop
#
# All five components are FLAG-GATED OFF by default. Phased enablement:
#   Phase 1 (data): KALSHI_TAPE_ENABLED → records ms-resolution trade tape,
#                                          no behavior change
#   Phase 2 (stops): STOP_REQUIRES_PERSISTENCE → bid touch alone no longer
#                                                 fires stop, requires
#                                                 duration + volume confirmation
#   Phase 3 (entry): ENTRY_FLOW_GATE_ENABLED + BOOK_DENSITY_GATE_ENABLED
#                                                 → block entry if adverse
#                                                 flow / thin support
#   Phase 4 (architecture): PROTECTIVE_ORDER_MODE → replaces bid-check stop
#                                                    with always-resting
#                                                    sell at TP/SL price
#
# Gate decisions are written to data/trades.db `gate_decisions` table for
# post-hoc analysis (correlate pass/block with subsequent outcomes).

# ── Phase 1: Trade tape ────────────────────────────────────────────────
KALSHI_TAPE_ENABLED              = True   # 2026-04-30 PM: live activation —
                                          # paper testing is inaccurate; real
                                          # execution behavior is the signal
KALSHI_TAPE_RETENTION_S          = 120.0  # Per-ticker trade history kept

# ── Phase 2: Stop persistence ──────────────────────────────────────────
STOP_REQUIRES_PERSISTENCE        = True
STOP_PERSISTENCE_SECONDS_DEFAULT = 5      # Bid must be ≤ trigger for ≥ this long
STOP_PERSISTENCE_MIN_VOLUME_CT   = 10     # Trades at-or-below trigger in window
STOP_PERSISTENCE_VOLUME_WINDOW_S = 10     # Window for volume confirmation
STOP_DEFER_ON_THICK_BID          = True   # If thick bid below trigger, defer stop
STOP_DEFER_THICK_BID_VOLUME_CT   = 100    # Threshold for "thick" support

# ── Phase 3a: Entry flow gate ──────────────────────────────────────────
# 2026-05-03 Option A: DISABLED. This gate blocked entries when the
# OPPOSITE side had strong taker flow — which is exactly the condition
# of momentum chasers piling into the wrong side, creating the cheap
# entry the user manually trades. We were filtering OUT our own alpha.
ENTRY_FLOW_GATE_ENABLED          = False
ENTRY_FLOW_WINDOW_S              = 5.0    # Recent flow window
ENTRY_FLOW_BASELINE_S            = 15.0   # Baseline for deceleration check
ENTRY_FLOW_MAX_ADVERSE_SHARE     = 0.60   # Block if adverse flow > 60%
ENTRY_FLOW_MAX_ADVERSE_VEL_CPS   = 2.0    # Block if mid moving > 2c/s against
ENTRY_FLOW_REQUIRE_DECEL         = True   # Require adverse-side deceleration
                                          # OR same-side dominance

# ── Phase 3b: Book density gate ────────────────────────────────────────
# 2026-05-03 Option A: DISABLED. Same reasoning — opp-side density
# means chasers built the wrong-side book, creating mispricing on the
# thin side. Engine should fade INTO that, not refuse entry.
BOOK_DENSITY_GATE_ENABLED        = False
BOOK_DENSITY_DEPTH_LEVELS        = 5      # Top N price levels to sum
BOOK_DENSITY_MIN_SAME_SIDE       = 50     # Min ct on our bid stack
BOOK_DENSITY_MAX_OPP_DOMINANCE   = 4.0    # Block if opp_density > X * same_density

# ── Phase 4: Protective-order mode ─────────────────────────────────────
PROTECTIVE_ORDER_MODE            = True   # Replaces bid-check stop entirely
PROTECTIVE_TP_OFFSET_C           = 5      # TP price = entry + this
PROTECTIVE_SL_OFFSET_C           = 5      # SL price = entry - this
                                          # 2026-05-01 PT: tightened from
                                          # 8 → 5 per user. MFE/MAE data:
                                          # successful trades had 1-2c
                                          # adverse drawdown; losers had
                                          # 8-47c. 5c trigger catches
                                          # losers ~3c earlier with
                                          # cross-spread execution.
# Mid-trade BTC velocity SL (2026-05-01): force SL state regardless of
# bid if BTC is moving sharply against our position. Catches "BTC just
# reversed" moment before contract bid catches up.
PROTECTIVE_MID_TRADE_ADVERSE_VEL  = 20.0   # 2026-05-02 strategic reset
                                           # (was 10): doubled to reduce
                                           # false-fires on routine market
                                           # noise. Trade 10 today fired
                                           # SL 15+ times in 6 min at the
                                           # 10 threshold — costing $11.
PROTECTIVE_MID_TRADE_PERSIST_COUNT = 3     # 2026-05-02 strategic reset:
                                           # require N consecutive polls
                                           # showing adverse vel before
                                           # forcing SL. ~2-3 sec sustained.
                                           # Defends against single-poll
                                           # spikes on a $78k underlying.

# Trailing TP for BB_PURE (2026-05-01): re-enabled with smart arming.
# Trail engages ONLY when bid exceeds the FVG-close target (the
# preflight TP price). Initial floor = FVG-close target. As bid moves
# higher, floor ratchets up (bid - BB_PURE_TRAIL_DISTANCE_C). If bid
# pulls back below floor, fires trail exit.
# Worst case: trail floor = FVG-close target → exit at planned profit.
# Best case: bid runs to settlement/100c → ride the move.
BB_PURE_TRAIL_DISTANCE_C          = 3      # cents below current bid
                                           # for trail floor (give back
                                           # at most 3c from peak)
BB_PURE_MFE_TRAIL_THRESHOLD_C     = 8      # Arm dynamic MFE trail after
                                           # the trade has moved +8c.
BB_PURE_MFE_TRAIL_RATIO           = 0.4    # Give back max(base trail,
                                           # 40% of MFE) once armed.
PROTECTIVE_REPLAN_DEBOUNCE_S     = 2.0    # Min seconds between cancel+replace
PROTECTIVE_PRE_EXPIRY_FORCE_S    = 60     # Force market sell when remaining < this

# ── Session-minute strictness multipliers ──────────────────────────────
# Phase 3 + Phase 2 read these to scale thresholds per session minute.
# Each tuple is (session_min_start, session_min_end, strictness_factor).
# Factor 1.0 = normal; >1.0 = stricter; <1.0 = looser.
SESSION_STRICTNESS_BUCKETS       = [
    (0,  3,  1.50),   # 0-3 min: very strict (high uncertainty)
    (3,  10, 1.00),   # 3-10 min: standard
    (10, 13, 0.80),   # 10-13 min: looser (LATE_DOMINANT zone)
    (13, 15, 0.60),   # 13-15 min: pre-expiry (gates softer to allow exits)
]
SESSION_STOP_PERSIST_BUCKETS_S   = [
    (0,  3,  8),      # 0-3 min: 8s stop persistence
    (3,  10, 5),      # 3-10 min: 5s
    (10, 13, 3),      # 10-13 min: 3s
    (13, 15, 1),      # 13-15 min: 1s + force flatten path
]

# ── BB_PURE_MODE — fair-value-anchored signal (Claude 2026-04-30 PM) ──────
# Founding-philosophy implementation. When ON, replaces the entire
# composite-signal cascade (TA_FORCED + LATE_DOMINANT + ATM_REVERSION_DISCOUNT)
# with pure Brownian-Bridge mispricing logic:
#   1. Read BB model's fair_value vs market mid
#   2. If |edge| ≥ MIN_EDGE_PP, side = underpriced side
#   3. Kelly-size on implied edge (capped at KELLY_MAX_FRAC)
#   4. Microstructure gates (entry flow, density) still gate execution
#   5. Protective-order mode handles exit (TP/SL anchored to fair value)
#
# DEFAULT OFF. Session-1 ships only the math + method definition. Session 2
# wires it into the cascade. Session 3 flips the flag for live testing.
BB_PURE_MODE                     = False  # 2026-05-04 PT 19:55: KILLED.
                                          # Backtest verdict: -$0.024/trade.
                                          # The "$42 → $78 today" was a user
                                          # deposit, NOT engine earnings —
                                          # corrected. Without that data point
                                          # we have no evidence BB_PURE makes
                                          # money. BB_TREND lives inside this
                                          # code path so this also disables
                                          # BB_TREND (+$0.88/day projected,
                                          # acceptable loss for the night).
                                          # Tonight's strategy: FVG tier-aware
                                          # paper-shadow only. No live trades.
                                          # Tomorrow: wire FVG live, flip
                                          # PAPER_FVG_LIVE_MODE=True.
BB_PURE_MIN_EDGE_PP              = 8.0    # Min mispricing pp to fire (static)
# ─── BB_TREND mode (2026-05-03) ─────────────────────────────────────────
# Sibling alpha to mean-reversion: when BTC has drifted firmly in one
# direction (>= TREND_MIN_STRIKE_DIST_PCT from strike), the contract is
# correctly priced as "very likely YES/NO" — but BB still sees additional
# pp-edge from precise fair-value math. Trades WITH the trend only:
#   - Signal direction (sig.side) must match BTC's side relative to strike
#   - 5-min momentum (btc_move_300s) must NOT be strongly reverting
#     against the trend (capped by MAX_REVERSAL_DOLLARS)
# Mutex with mean-reversion via per-window ticker lock — only one trade
# per window regardless of which regime fires.
#
# Counterfactual analysis on 2026-05-03 showed all 6 settled windows we
# observed went YES, but mean-reversion gate (0.04% strike-dist) blocked
# every entry. Trend mode would have caught those wins.
#
# DEFAULT OFF until backtested. Caller responsibility: review backtest
# results before flipping. See scripts/backtest_dual_regime.py.
BB_TREND_MODE_ENABLED            = True    # 2026-05-03 15:31 PT: ENABLED for live testing
                                           # (cross-engine interference cleared,
                                           # BTC firmly above strike for hours)
BB_TREND_MIN_STRIKE_DIST_PCT     = 0.0015  # require >= 0.15% from strike
BB_TREND_MAX_REVERSAL_DOLLARS    = 50.0    # block if 5min move reverts >$50 against trend
BB_TREND_MAX_ENTRY_CENTS         = 75      # higher cap than mean-rev (55c)
# Min entry for trend mode (2026-05-03 backtest finding):
# Cheap trend entries (30-59c) had 0/5 hit rate → -$18.52 in backtest.
# Expensive trend entries (60-75c) had 6/7 hit rate → +$9.09. The
# profitable subset is at high entry prices where the trend is already
# well-established. Setting MIN at 60c captures the profitable subset
# only.
BB_TREND_MIN_ENTRY_CENTS         = 60
# Post-failure cooldown (2026-05-03): when place_order is rejected by
# Kalshi (e.g., post_only_cross from book moving between eval and
# Kalshi-time), back off this ticker for N seconds so we don't spam
# the same failing order at every signal cycle (~3-4× per second).
BB_PURE_POST_FAIL_COOLDOWN_S     = 5.0
# ── Fee-aware dynamic edge threshold (2026-05-03) ────────────────────────
# When enabled, replaces the static BB_PURE_MIN_EDGE_PP floor with a
# per-entry-price threshold derived from Kalshi fees:
#     fee_per_contract  ≈ 0.07 × P × (1−P) × 100¢   (P in [0,1])
#     breakeven_edge_pp = 2 × fee  (round-trip)
#     effective_floor   = max(FEE_AWARE_FLOOR_PP, FEE_AWARE_MULT × breakeven)
# Result: looser at cheap entries (15c→3.6pp at K=2), tighter at 50c (7pp).
# Off by default — flip on only after backtest validates fire-rate vs PnL.
BB_PURE_FEE_AWARE_EDGE_ENABLED   = False  # default off
BB_PURE_FEE_AWARE_EDGE_MULT      = 2.0    # K = safety multiplier on breakeven
BB_PURE_FEE_AWARE_EDGE_FLOOR_PP  = 4.0    # absolute pp floor regardless of P
BB_PURE_MAX_ENTRY_CENTS          = 55     # 2026-05-02 Phase 0.1.5 (was 70):
                                          # data-driven cap. Tonight's
                                          # clean wins: 48c, 51c. Today's
                                          # earlier wins: 26c, 30c, 32c.
                                          # Today/yesterday's biggest
                                          # losers (-$36, -$56, fade
                                          # trades): 55-65c entries. The
                                          # asymmetric payoff math says
                                          # cheap entries dominate; mid-
                                          # priced entries (56-70c) are
                                          # where the engine reliably
                                          # loses money. Cap at 55 keeps
                                          # the cheap-side bias without
                                          # forcing 35c (which the alpha
                                          # table didn't have data for).
BB_PURE_MIN_ENTRY_CENTS          = 5      # Don't fire on dust prices
BB_PURE_MIN_TIME_REMAINING_S     = 60.0   # No new entries within last minute
# 2026-05-03 PoC sizing: bumped Kelly cap from 0.05 to 0.10 to allow
# meaningful trade sizes at $40 bankroll. Per-trade max ~$4 cost.
BB_PURE_KELLY_FRACTION           = 0.25   # Quarter-Kelly base (matches existing
                                          # KELLY_FRACTION default)
BB_PURE_KELLY_MAX_FRAC           = 0.10   # 2026-05-03 PoC: bumped from 0.05
                                          # back to 0.10. At $40 BAL, max
                                          # trade = $4 cost. Engine needs
                                          # this to overcome fees and produce
                                          # measurable per-trade alpha. Will
                                          # scale up further as BAL grows.
                                          # 2026-05-02 strategic reset
                                          # (was 0.10): halved while shaking
                                          # out the new architecture. Goal is one
                                          # clean session of close-handler
                                          # logs before re-raising. At $200
                                          # bankroll = $10 max per trade.
                                          # (was 0.30): cap any BB_PURE
                                          # position at 10% of bankroll.
BB_PURE_VOLATILITY_LOOKBACK      = 15     # Mirrors founding-doc default
# Fair-value-anchored protective-order targets (used in Session 2 refactor)
BB_PURE_TP_EDGE_PP               = 5.0    # TP when fair value crosses
                                          # entry_implied_pp + this
BB_PURE_SL_EDGE_PP               = 8.0    # SL when fair value drops below
                                          # entry_implied_pp - this

# Preflight TP — placed at fill time, anchored to FVG closing (2026-05-01)
# Target = fair value for our side − BB_PURE_TP_INSIDE_FAIR_C, bounded by
# [entry+MIN_CENTS, entry+MAX_CENTS].
#   - Big edge (e.g. 40pp): TP wide, captures most of the implied move
#   - Small edge (e.g. 8pp): TP tight, takes certain wins early
# Sized to philosophy: when market reaches fair, the BB_PURE thesis is
# fully played out. Wider TPs lower per-trade fill rate but raise expected
# value (proven on 2026-04-30 PM session — flat 5c TPs left $30+ on the
# table per high-edge entry).
BB_PURE_TP_INSIDE_FAIR_C         = 1      # Sell this many cents inside fair
                                          # for fill probability (0 = at fair)
BB_PURE_TP_MIN_CENTS             = 4      # Floor — never TP tighter than this
BB_PURE_TP_MAX_CENTS             = 30     # Cap — never TP wider than this
                                          # (defensive against extreme edges)

# Conviction-tier sizing (2026-05-01 per user). When edge AND fair-side
# extremity both clear thresholds, raise Kelly cap. The hard ceiling is
# liquidity (visible book depth) — the engine's per-window contract cap
# still applies. Tighter TP cap on bigger size: with size already large
# we don't need to be greedy on per-contract gain; tighter TP raises
# fill probability so trades close in-window instead of riding to expiry.
BB_PURE_KELLY_TIER2_MIN_EDGE_PP      = 25.0  # Tier 2 edge floor
BB_PURE_KELLY_TIER2_MIN_FAIR_EXTREME = 85.0  # Tier 2 fair-extremity floor
BB_PURE_KELLY_TIER2_MAX_FRAC         = 0.06  # Tier 2 Kelly cap INVERTED
                                              # 2026-05-01 late PT. Was 0.50;
                                              # data showed 5 Tier-2 trades
                                              # today netted -\$522 with 0%
                                              # hit rate. Model overshoots
                                              # fair after fast BTC moves;
                                              # 'high confidence' actually
                                              # means 'just chased a move'.
                                              # Shrinking the cap reduces
                                              # exposure when model is least
                                              # reliable.
BB_PURE_KELLY_TIER3_MIN_EDGE_PP      = 40.0  # Tier 3 edge floor
BB_PURE_KELLY_TIER3_MIN_FAIR_EXTREME = 95.0  # Tier 3 fair-extremity floor
BB_PURE_KELLY_TIER3_MAX_FRAC         = 0.03  # Tier 3 Kelly cap INVERTED.
                                              # Was 0.75; data showed 4
                                              # Tier-3 trades today netted
                                              # -\$275 (with -\$252 single
                                              # catastrophe). Model is
                                              # WORST at extremes (fair
                                              # >= 95c or <= 5c). Cap is
                                              # now smaller than Tier 1.
BB_PURE_TP_MAX_CENTS_TIER2           = 20    # Tier 2 TP cap (tighter)
BB_PURE_TP_MAX_CENTS_TIER3           = 12    # Tier 3 TP cap (tightest)

# Per-window fire cap (2026-05-01). Live observation: 1-2 BB_PURE fires
# per window has been profitable; the third fire crosses into gamble
# territory because of cumulative edge decay and accumulating risk.
BB_PURE_MAX_FIRES_PER_WINDOW         = 99    # 2026-05-01 PT: lifted from 2.
                                              # User directive: any number of
                                              # entries OK as long as we have
                                              # 7+ min runway and the new SL
                                              # path actually executes losers.
                                              # NOTE: per-ticker is still
                                              # capped via _entered_tickers_this_window
                                              # (= MAX_TRADES_PER_SESSION_TICKER)
                                              # — this counts ALL fires across
                                              # all tickers within a window.
# 2026-05-02 user directive after live test: "limit to one trade per session"
# where session = one 15-min window = one ticker. Once the engine has any fill
# on a ticker, no further engine entries on that ticker until the window flips
# (auto-resets per ticker). The engine remains continuously active across all
# windows. Lock is enforced at the BB_PURE preflight site via
# _entered_tickers_this_window membership check.
MAX_TRADES_PER_SESSION_TICKER        = 1     # Hard cap: one engine entry per
                                              # 15-min ticker / window. Engine
                                              # runs continuously; lock auto-
                                              # resets on window flip. User
                                              # manual trades are not affected.
BB_PURE_HARD_MIN_TIME_S              = 420.0 # 7 minutes (was 60s). After
                                              # this point in the window
                                              # there isn't enough runway
                                              # for FVG-close TPs to fill;
                                              # late-session entries are
                                              # exposure to pin risk.

# Pre-expiry consolidation (2026-05-01): single owner of the close in
# the final N seconds. Cancels all resting orders for the open ticker,
# places ONE limit-sell at the bid, locks out other paths until window
# rotates. Targets the "last 5 minutes chaos" pattern.
PRE_EXPIRY_CONSOLIDATE_S             = 90.0  # Take ownership at this
                                              # remaining-seconds threshold

# Orphan-position auto-flatten watchdog (2026-05-01). Background task
# polls Kalshi truth directly (NOT engine state) every N seconds; any
# non-zero position not from the active BB_PURE ticker AND not from a
# recent placement gets market-sold immediately. Targets the side-flip
# pattern where engine sells against stale state and Kalshi atomically
# opens an opposite-side short.
ORPHAN_FLATTEN_ENABLED               = False  # 2026-05-03 15:35 PT: DISABLED.
                                              # Was incorrectly flattening the
                                              # user's manual trades (treated
                                              # as "untracked positions").
                                              # Re-enable only after adding a
                                              # user-position-whitelist or
                                              # session-tag detection so it
                                              # only flattens positions the
                                              # engine itself opened.
ORPHAN_FLATTEN_POLL_S                = 3.0   # Poll interval (seconds —
                                              # tightened from 5s for
                                              # faster orphan detection)
ORPHAN_FLATTEN_RECENT_S              = 90.0  # 2026-05-02 evening: RESTORED
                                              # after live observed bug:
                                              # FLAT-CONFIRMED falsely cleared
                                              # state on Kalshi blip, then
                                              # orphan-flatten saw position
                                              # re-appear and crossed at
                                              # bid-5c locking $2.50 loss.
                                              # The c044d04 "remove recency
                                              # protection" commit went too
                                              # far. Now: tickers with
                                              # _recent_placement_tickers
                                              # entry within this window are
                                              # SKIPPED by orphan-flatten.
ORPHAN_FLATTEN_OFFSET_C              = 1     # 2026-05-02 strategic reset:
                                              # was 5c. We're 0.005% of
                                              # book — bid-1 fills cleanly
                                              # without paying 5c slippage.
                                              # 5c was justified when the
                                              # narrative was "race the
                                              # market"; user reframe
                                              # confirmed that's wrong at
                                              # our size. bid-1 is plenty
                                              # aggressive enough to fill.

# Entry-timing filter (2026-05-01): reject BB_PURE entries when the
# buy side is at an extreme of the recent mid range. The model's fair
# value lags BTC moves — when YES has just spiked, fair hasn't caught
# up yet, and the engine sees "huge edge" because mid is at a local
# high. Today's -$252 loss on -1330-30: bought YES @ 66c at top of
# range, BTC reversed, YES collapsed to 8c.
BB_PURE_RANGE_LOOKBACK_S             = 180.0  # Scan last 3 min of mids
BB_PURE_RANGE_BLOCK_PCT              = 0.80   # Block buys above this
                                              # percentile of recent range
                                              # (0.80 = top 20% blocked)
BB_PURE_RANGE_MIN_SAMPLES            = 10     # Need at least N mids in
                                              # the lookback to gate

# BTC velocity gate (2026-05-01): block BB_PURE entries when BTC spot
# is actively moving against the FVG position. Per user diagnosis after
# watching trades all day: contract price is pegged to BTC, so when BTC
# moves down, YES drops; entering YES (bullish bet) while BTC is
# dropping means we're chasing a moving market. The model's fair value
# lags BTC by seconds. Wait for BTC to stabilize before entering.
BB_PURE_BTC_ADVERSE_VEL              = 5.0    # $/s threshold. If BTC
                                              # vel ≤ -5 $/s and we want
                                              # YES, skip. If vel ≥ +5
                                              # and we want NO, skip.
                                              # tick_velocity window is
                                              # the price_feed default
                                              # (~30s typically).

# BTC stability gate (2026-05-01 evening): require BTC to be in a tight
# range over the last N seconds before firing. MFE/MAE analysis showed
# successful entries had ~1-2c adverse drawdown; failed entries drew
# down 8-47c. The latter happened during BTC trends, not chop. Wait
# for chop = wait for the trend to exhaust.
BB_PURE_BTC_STABILITY_WINDOW_S       = 30.0   # Window to scan
BB_PURE_BTC_STABILITY_MAX_RANGE      = 30.0   # Max BTC range ($) in
                                              # the window. If exceeded,
                                              # BTC is trending; skip.
# 2026-05-03 Option A: DISABLE the momentum-following gates that were
# blocking BB_PURE's mean-reversion thesis. User confirmed their actual
# profitable strategy is counter-trend (buy cheap side after BTC moves),
# but these gates assumed BTC velocity continues — explicitly blocking
# entry into the counter-trend opportunity. See full diagnosis in
# session notes 2026-05-03 16:00 PT.
BB_PURE_BTC_STABILITY_GATE_ENABLED   = False  # was the BTC-RANGE-BLOCK trigger
BB_PURE_BTC_VELOCITY_GATE_ENABLED    = False  # was the BTC-ADVERSE-BLOCK

# 2026-05-02 Phase 0.1.6 — ASYMMETRIC vol gate by trend orientation.
# Tonight's loser pattern: -$5 fade trade fired NO at 24c right after BTC
# ripped 200+ in 2 min. Both contract sides got volatile, but the
# direction matters — fading WITH the prevailing trend (= classic mean-
# reversion fade) is the high-risk case. The previous symmetric cap
# (30) treated all volatility the same. New asymmetric design:
#   - Counter-trend (mean-reversion fade): stricter (default 20) — only
#     fire if trend is visibly exhausting.
#   - With-trend (rare for BB_PURE): looser (default 50) — vol is in our
#     favor.
#   - Neutral (|trend_change| ≤ dead zone): use the legacy default cap.
BB_PURE_TREND_WINDOW_S               = 300.0  # 5-min trend lookback
BB_PURE_TREND_DEAD_ZONE_USD          = 20.0   # |change| ≤ this = neutral
BB_PURE_VOL_STRICT_COUNTER_TREND     = 20.0   # tighter cap when fading
                                              # against the trend
BB_PURE_VOL_LOOSE_WITH_TREND         = 50.0   # looser cap when going
                                              # with the trend (rare for
                                              # BB_PURE)

# 2026-05-02 Phase 8 — TAPE PRESSURE / ABSORPTION DETECTION.
# Encodes the user's manual-trading edge: sustained large buys on the
# side OPPOSITE to BTC's recent direction = institutional absorption =
# high-conviction signal that retracement is coming. Decision logic in
# tape_pressure.py.
#
# SHADOW mode = compute and log the decision but don't gate yet. We need
# a few sessions of data to validate the thresholds before turning the
# gate on. Once enabled, BB_PURE entries on the side BEING ABSORBED AGAINST
# will be blocked.
BB_PURE_TAPE_SHADOW_ENABLED          = False  # 2026-05-02 reset: was True.
                                              # Polluted logs (2000+ lines
                                              # per window). Re-enable when
                                              # validating signal vs P&L.
BB_PURE_TAPE_GATE_ENABLED            = False  # actively block on "block"
                                              # decision (start False —
                                              # shadow first)
BB_PURE_TAPE_WINDOW_MIN_S            = 0.0    # start of session-age window
BB_PURE_TAPE_WINDOW_MAX_S            = 300.0  # 5 min — only count buys
                                              # in first N seconds of
                                              # session. User: most trades
                                              # happen in first 3-5 min
BB_PURE_TAPE_LARGE_BUY_USD           = 100.0  # min $ to count toward
                                              # large_count
BB_PURE_TAPE_RELIABLE_BUY_USD        = 200.0  # higher conviction
                                              # threshold (per user)
BB_PURE_TAPE_INVERSE_MIN_USD         = 200.0  # min $ on inverse-trend
                                              # side to flag absorption
BB_PURE_TAPE_DOMINANCE_RATIO         = 2.0    # inverse-side $ must be
                                              # ≥ N× with-trend $
BB_PURE_TAPE_MIN_CONSISTENCY         = 3      # ≥ N large buys on inverse
                                              # side (sustained, not a
                                              # single dump)
BB_PURE_TAPE_BTC_DEAD_ZONE_USD       = 20.0   # |BTC 5m change| ≤ this
                                              # → no trend, no decision

# 2026-05-03 Option C — ALIGNMENT GATE + ALIGNMENT-FALLBACK TIER.
# User insight: BB_PURE's profitable edge is counter-trend (buy the cheap
# side after BTC moves against it, hold for mean-reversion bounce). But
# when no clear contrarian setup exists, sitting out costs opportunity —
# default to a small WITH-TREND bet at the aligned-side cheap price.
#
# Two independent flags, both default OFF (opt-in deployment):
#   _GATE_ENABLED:          when ON, BB_PURE BLOCKS contrarian fires
#                           (cheap side opposes BTC trend) unless a
#                           reversal-confirmation gate is satisfied.
#                           Aligned fires pass through unchanged.
#                           NOTE: the reversal-confirmation gate (e.g.
#                           3c-bounce) is NOT yet wired — when this flag
#                           is True, contrarian is hard-blocked. Wire
#                           the confirmation gate separately and gate
#                           the block on (not confirmation_ok).
#   _FALLBACK_TIER_ENABLED: when ON, on cycles where BB_PURE found no
#                           qualifying mispricing, the engine fires a
#                           small with-trend bet on the aligned side
#                           (cheap-side cap, fixed-fraction sizing, no
#                           Kelly math). Independent of the gate above.
#
# Both flags default OFF: shipping the code, not the behavior. Flip to
# True per the deploy review checklist (paper validate → live).
# 2026-05-03 PT 20:30 — A1/A2 ACTIVATED. Both alignment features were
# shipped default-OFF earlier; user reviewed today's tape and authorized
# flipping ON for live validation. Engine behavior change minimal on
# today's tape (most fires were alignment=neutral) but enables future
# protection.
BB_PURE_ALIGNMENT_GATE_ENABLED            = True   # 2026-05-03 ACTIVATED
BB_PURE_ALIGNMENT_FALLBACK_TIER_ENABLED   = True   # 2026-05-03 ACTIVATED
BB_PURE_ALIGNMENT_BTC_DEAD_ZONE_USD       = 20.0   # |BTC 5m change| ≤ this
                                                   # = no clear trend → no
                                                   # alignment classification
                                                   # (mirrors tape dead zone)
# Alignment-fallback tier sizing/price gates (only used when
# _FALLBACK_TIER_ENABLED=True). Conservative by design — this is a
# "default action" bet, not a thesis bet, so it sizes smaller than
# BB_PURE's regular Kelly path.
BB_PURE_ALIGNMENT_FALLBACK_MAX_ENTRY_CENTS  = 55    # cheap-side cap, mirrors
                                                    # BB_PURE_MAX_ENTRY_CENTS
BB_PURE_ALIGNMENT_FALLBACK_MIN_ENTRY_CENTS  = 5     # don't fire on dust
BB_PURE_ALIGNMENT_FALLBACK_MIN_TIME_S       = 90.0  # min seconds-to-expiry
                                                    # (stricter than 60s
                                                    # BB_PURE default — no
                                                    # edge to lean on)
BB_PURE_ALIGNMENT_FALLBACK_KELLY_FRACTION   = 0.10  # fixed fraction (no Kelly)
BB_PURE_ALIGNMENT_FALLBACK_KELLY_MAX_FRAC   = 0.05  # absolute ceiling

# 2026-05-03 PT 20:30 — POSITION-VS-STRIKE ALIGNMENT (option B1).
# When BB_PURE's strike-distance gate would block (BTC in 0.04-0.15%
# no-man's-land), check whether BB's cheap side aligns with where BTC
# IS relative to strike. If aligned (e.g., BTC>strike & BB picks YES),
# allow the entry through with regime tag POSITION_ALIGNED. Captures
# sustained-trend setups that the velocity-based classifier misses.
# 2026-05-03 PT 20:54 ACTIVATED per user "approved all flips" directive.
BB_PURE_POSITION_ALIGN_ENABLED          = True    # 2026-05-03 ACTIVATED
BB_PURE_POSITION_ALIGN_DEADBAND_USD     = 5.0     # |BTC - strike| ≤ this
                                                  # = neutral, no override
BB_PURE_POSITION_ALIGN_MAX_DIST_PCT     = 0.0015  # only override up to 0.15%
                                                  # (above that, BB_TREND
                                                  # zone takes over instead)
# 2026-05-03 PT 22:08 — Option A: dedicated entry-cents bounds for
# POSITION_ALIGNED regime. Originally inherited mean-rev's 55c cap,
# but POSITION_ALIGNED is structurally a trend setup (BTC firmly
# past strike, contract drifting toward 100c). Today's window
# 1:00-1:15 ET ENTRY-CAP-BLOCK at 59c (above 55c cap) on a setup
# structurally identical to Trade #7 (which won +$1.01 at 62c
# entry under TREND regime cap of 75c) — same R/R profile, just
# blocked by inherited mean-rev cap. Raising to 70c (between
# mean-rev's 55c and BB_TREND's 75c) to capture these setups.
# Min still 5c — cheap-side filter for noise.
BB_PURE_POSITION_ALIGN_MAX_ENTRY_CENTS  = 70      # was 55 (inherited)
BB_PURE_POSITION_ALIGN_MIN_ENTRY_CENTS  = 5       # cheap-side floor

# 2026-05-03 PT 20:30 — LOSS-STREAK COOLDOWN (option B3).
# After N consecutive losses on BB_PURE, tighten min_edge_pp by a
# multiplier for the next entry. Releases on next win or after K windows.
# Win/loss determined by entry_cents vs current_bid at close time
# (approximate; B2 will refine when shipped).
# 2026-05-03 PT 20:54 ACTIVATED per user "approved all flips" directive.
BB_PURE_LOSS_STREAK_COOLDOWN_ENABLED    = True    # 2026-05-03 ACTIVATED
BB_PURE_LOSS_STREAK_THRESHOLD           = 2       # tighten after N losses
BB_PURE_LOSS_STREAK_EDGE_MULT           = 1.5     # 8pp × 1.5 = 12pp during
                                                  # cooldown
BB_PURE_LOSS_STREAK_RELEASE_WINDOWS     = 3       # auto-release after N
                                                  # windows w/o a fire

# 2026-05-03 PT 20:30 — FINAL-MINUTE RELAX FOR ALIGNED SETUPS (option B4).
# When alignment classifier returns "aligned" (BB cheap side matches
# BTC trend), relax the min-time-remaining floor. Settlement-cliff
# trades on aligned setups have near-determined outcomes and are
# the safer subset of late-window entries.
# 2026-05-03 PT 20:54 ACTIVATED per user "approved all flips" directive.
BB_PURE_FINAL_MIN_RELAX_ENABLED         = True    # 2026-05-03 ACTIVATED
BB_PURE_FINAL_MIN_RELAX_S               = 30.0    # relaxed floor for aligned
                                                  # setups (vs 60s default)

# Bump KalshiTape retention to 5 min so the absorption window has
# enough trade history. Default in kalshi_tape.py is 120s.
KALSHI_TAPE_RETENTION_S              = 360.0  # 6 min — covers 5-min
                                              # window with buffer

# 2026-05-02 Phase 8b — TAPE EXIT-PRESSURE (massive opposite flow = exit).
# User: "massive volume to the opposite direction is also an indicator to
# exit." Symmetric to the entry absorption signal but on a 30s reactive
# window. When smart money aggresses against our side, bail before the
# bid catches up.
BB_PURE_TAPE_EXIT_SHADOW_ENABLED     = False  # 2026-05-04 PT 16:30:
                                              # POST-CATASTROPHE KILL-SWITCH.
                                              # Was re-enabled in dispatch
                                              # bundle that drained $68. See
                                              # to-do/CATASTROPHE_2026_05_04.md.
BB_PURE_TAPE_EXIT_GATE_ENABLED       = False  # 2026-05-04 PT 16:30: KILLED
                                              # alongside the shadow flag.
BB_PURE_TAPE_EXIT_WINDOW_S           = 30.0   # how recent the spike must be
BB_PURE_TAPE_EXIT_MASSIVE_USD        = 300.0  # min $ on opposite side to
                                              # call it "massive"
BB_PURE_TAPE_EXIT_DOM_RATIO          = 3.0    # opp $ ≥ N × our $ (one-
                                              # sided flow)
BB_PURE_TAPE_EXIT_MIN_LARGE          = 2      # ≥ N large opposite buys
                                              # (sustained, not single)

# 2026-05-02 cache-lag race fix.
# Live observed today (13:32-13:34 PT): BB_PURE bought 40ct YES,
# protective sell closed at +profit but the order_id wasn't tracked
# ("MANUAL FILL" label). 47s later the engine still thought +40 YES,
# fired MID-TRADE-SL, placed a new sell-yes that Kalshi treated as a
# new SHORT YES (synthetic LONG NO 40ct) since YES inventory was 0.
# User had to manually flatten the synthetic NO position.
#
# Fix: in _maintain_protective_order, if Kalshi positions API EXPLICITLY
# returns 0 AND fill_age >= this window, treat the position as closed.
# Clear engine state and stop placing sells. The window must be long
# enough to cover Kalshi's BUY-side propagation lag (don't want to
# falsely-clear right after entry) but short enough to detect close
# events from untracked sells.
PROTECTIVE_FLAT_CONFIRM_S            = 30.0   # seconds past fill_time
                                              # before trusting Kalshi=0
# 2026-05-02 evening hardening: live observed 18:16 PT a single Kalshi=0
# blip caused FLAT-CONFIRMED to clear state, then orphan-flatten saw the
# (still-real) position re-appear and crossed at bid-5c — locked $2.50
# loss on a healthy trade. Require N consecutive 0-readings spaced over
# M seconds before clearing; single blips no longer trip it.
PROTECTIVE_FLAT_CONFIRM_COUNT        = 3      # require N consecutive 0s
PROTECTIVE_FLAT_CONFIRM_SPAN_S       = 6.0    # AND span across this many s

# ─────────────────────────────────────────────────────────────────────────
# 2026-05-02 STRATEGIC RESET GATES
# ─────────────────────────────────────────────────────────────────────────
# These gates encode user-validated alpha conditions. Apply BEFORE the older
# microstructure gates — if market context is wrong, no signal matters.

# Strike-distance gate: only fire when |BTC - strike| / BTC < threshold.
# User insight (2026-05-02): "Market confidence is far less speculative when
# < ±0.04% of strike". At BTC=$78k, that's ~$31. Inside that band gamma is
# high and prices are meaningful; outside, the contract is mostly settled
# (probability near 0 or 1) and book noise dominates.
BB_PURE_STRIKE_DISTANCE_GATE_ENABLED = True
BB_PURE_MAX_STRIKE_DIST_PCT          = 0.0004  # 0.04% (~$31 at $78k BTC)

# Time-of-day gate: pause during overnight hours where the tape is "ruthless"
# per user manual-trading observation 2026-05-02. Default 06:00–22:00 PT
# (= 13:00–05:00 UTC). Window covers US/EU active hours.
# 2026-05-03 PT 22:21 — DISABLED per user "let the engine trade"
# directive after Option A ship. Engine now eligible for overnight
# fires. Re-enable if night losses materialize.
BB_PURE_TRADING_HOURS_GATE_ENABLED   = False   # 2026-05-03 ALL-HOURS
BB_PURE_TRADING_HOUR_START_PT        = 6
BB_PURE_TRADING_HOUR_END_PT          = 22
BB_PURE_PT_UTC_OFFSET_H              = -7.0    # PDT (summer); -8 in winter

# Entry execution mode (2026-05-02 strategic reset).
# Old "taker_ask" pattern: place limit-buy at best_ask with post_only=False.
# Live observed 60% NOFILL rate because limit-at-ask is NOT a true taker —
# it's a limit that crosses only if Kalshi still sees ask ≤ our price by
# processing time (WS book lag often invalidates this).
#
# New default "maker_bid_plus_1": place limit-buy at bid+1 with post_only=True.
# We're 0.005% of session volume — we're a passive maker; counterparties
# come to us. Pair with the existing 8s stale-entry cancel for cleanup.
#
# Modes:
#   "maker_bid_plus_1" (default) — passive maker at bid+1
#   "taker_ask"        — legacy aggressive taker (use for fallback testing)
BB_PURE_ENTRY_MODE                   = "maker_bid_plus_1"

# Pre-fire balance gate (2026-05-03). Block new BB_PURE entries when
# live BAL doesn't have headroom to cover (entry_cost × multiplier).
# Defends against the "insufficient_balance" cascade observed 2026-05-02:
# once BAL drained, ORPHAN-FLATTEN couldn't unwind a wayward phantom
# position via cross-spread sell, and the position rode to expiry.
# 2x multiplier means: if entry costs $5, require BAL ≥ $10. Leaves
# headroom for cleanup at adverse prices.
BB_PURE_BAL_GATE_ENABLED             = True
# Pre-fire EXIT-LIQUIDITY gate (2026-05-03):
# Refuse entry if the exit-side bid book has insufficient depth at
# acceptable exit prices. Live regression observed 2026-05-03 11:30 PT:
# entered 7 YES @ 51c on a ticker with no exit liquidity, both
# PROTECTIVE [SL] attempts rested without filling, position rode to
# settlement and lost full $3.57 entry cost. This gate refuses entries
# on illiquid books at entry time.
BB_PURE_LIQUIDITY_GATE_ENABLED        = True
BB_PURE_MIN_EXIT_BID_DEPTH_CONTRACTS  = 1     # require >= entry_size or this floor
BB_PURE_MAX_EXIT_LOSS_CENTS           = 25    # exit must be available within 25c of entry

# ─── BB_MOMENTUM (2026-05-03) ───────────────────────────────────────────
# Directional-trend strategy. Sibling of BB_PURE — runs after BB_PURE in
# the cascade. Per-window mutex via _entered_tickers_this_window prevents
# both from firing on the same ticker.
#
# Detection: btc_move_300s + btc_move_30s same direction, magnitude
#            above thresholds. Buys WITH the trend at cheap-side prices.
# Exits:     STRIKE-CROSS (BTC structurally crosses against us) +
#            MFE-TRAIL (lock profits after seeing trigger cents above
#            entry, exit on first trail-cents retracement).
#
# Backtest on 273 settled tickers: +$19.86 with combined exits vs
# +$9.99 hold-to-settle baseline. MFE-TRAIL exits hit 81-91%.
#
# Default OFF — flip to True to enable live.
BB_MOMENTUM_ENABLED                  = False  # 2026-05-05: disabled to leave
                                              # FVG-tier-aware as the sole
                                              # live signal. User directive:
                                              # "We should be running either
                                              # tier one or two of the FVG
                                              # engine you back tested.
                                              # nothing else makes money
                                              # sense like that data did".
BB_MOMENTUM_MIN_BTC_MOVE_300S        = 30.0   # require >=$30 over 5min
BB_MOMENTUM_MIN_BTC_MOVE_30S         = 10.0   # confirmation: >=$10 in last 30s
BB_MOMENTUM_REQUIRE_SAME_DIRECTION   = True   # 30s + 300s must agree
BB_MOMENTUM_MAX_ENTRY_CENTS          = 50     # only buy when our-side mid <= 50c
BB_MOMENTUM_MIN_ENTRY_CENTS          = 5
BB_MOMENTUM_MIN_TIME_REMAINING_S     = 90.0
BB_MOMENTUM_KELLY_FRACTION           = 0.20   # fixed-fractional sizing
BB_MOMENTUM_KELLY_MAX_FRAC           = 0.05
# Post-entry exit signals
BB_MOMENTUM_STRIKE_CROSS_EXIT_ENABLED = True
BB_MOMENTUM_MFE_TRAIL_ENABLED         = True
BB_MOMENTUM_MFE_TRIGGER_CENTS         = 3     # +3c profit before trailing
BB_MOMENTUM_MFE_TRAIL_CENTS           = 1     # exit on 1c retracement
BB_PURE_BAL_HEADROOM_MULT            = 2.0

# 2026-05-02 BB_PURE entry stale-cancel timeout.
# Live observed 15:04:21 PT: BB_PURE FIRE 42ct YES @ 26c (taker).
# By the time Kalshi processed our taker order at 26c, the ask had
# moved up — the unfilled portion sat as a maker bid at 26c. The
# market then went past 26c twice over the next several seconds
# without our order matching, and the engine just left it resting
# until it filled at a stale price minutes later.
#
# Fix: schedule a one-shot cancel task after BB_PURE FIRE NOFILL.
# After this many seconds, if the order is still resting (not
# filled, not in any terminal state), cancel it. The RECLAIM path
# still handles late-fills if the order does match within this
# window. Session-lock stays set (one-trade-per-session honored).
BB_PURE_ENTRY_NOFILL_TIMEOUT_S       = 8.0    # cancel unfilled BB_PURE
                                              # entry after N seconds

# ── Post-close residual sweep (Claude 2026-04-29 per user) ────────────────
# After every TA_FORCED / LATE_DOMINANT / SR_FADE close, poll Kalshi
# positions every POST_CLOSE_RESIDUAL_POLL_S seconds for
# POST_CLOSE_RESIDUAL_DURATION_S seconds. If a fractional fill lands
# late (= residual appears 1-10s after the engine flagged the close),
# market-sell it via the existing _reconcile_residual_position handler.
# Async background task — doesn't block the close path.
POST_CLOSE_RESIDUAL_SWEEP_ENABLED = True
POST_CLOSE_RESIDUAL_DURATION_S    = 30.0
POST_CLOSE_RESIDUAL_POLL_S        = 2.5
ARB_POLL_INTERVAL_S           = 0.5     # scan every 0.5s
ARB_MAX_SUM_CENTS             = 97      # only fire when sum_ask < this
ARB_MIN_NET_CENTS_PER_PAIR    = 1.0     # require ≥1c net per pair after fees
ARB_FRACTION_OF_BALANCE       = 0.10    # commit 10% of balance per arb
ARB_MAX_CONTRACTS             = 200     # absolute cap per leg
ARB_MIN_CONTRACTS             = 10      # don't fire below this size (overhead)
ARB_MIN_TOP_DEPTH             = 5       # require ≥5ct visible depth on each side
ARB_MIN_BALANCE               = 5.0     # don't fire if balance < $5
ARB_TICKER_COOLDOWN_S         = 30.0    # wait 30s between arbs on same ticker
# ── ARB time-cutoff + cancel-after-cycle (per user, 2026-04-28 incident) ───
# Forensic: 18:09 PT — last 5 ARB cycles on -26APR282115-15 fired at minute
# 7-9 of session, accumulated 173 stale resting YES bids that filled async
# as YES crashed 29c→2c, ending in a −$60 bag-hold at expiry. User: "if it
# had stopped by the 7th minute it would have been profitable". Default 6.0
# guarantees no NEW arb cycles past minute 6 of a 15-min session, leaving
# 9 min of post-cutoff safety to flatten any lingering inventory.
ARB_TIME_CUTOFF_MIN           = 6.0     # no new arb cycles past this many min
                                        # into the 15-min window
ARB_FILL_GRACE_S              = 0.25    # post-place_order grace before
                                        # cancelling unfilled remainders;
                                        # gives same-tick maker fills a chance
                                        # to land before we nuke the rest order
ARB_MIN_UNWIND_DEPTH          = 5       # both sides must have ≥this many ct
                                        # in their TOP-3 BID stack so a partial
                                        # fill can actually be flattened. Skip
                                        # ticker if either side's bid is thin —
                                        # prevents bag-hold accumulation when
                                        # yes_bids dict is empty (post-crash).

# ── ARB session-type classifier (Claude 2026-04-28, per user) ──────────────
# At the first ARB scan of each window, the engine reads the book once and
# classifies the session as BILATERAL / DIRECTIONAL / DECIDED. Only
# BILATERAL sessions get ARB cycles; the others hand off to FVG / TA_FORCED.
#
# Forensic refinement (2026-04-28 19:30 PT): the original tight thresholds
# (yes_ask in [25, 75], sum in [95, 99]) would have rejected 5 of the 6
# profitable cycles in the "perfect" stretch on -26APR282115-15 — those
# fired at sum=72-95c, well below the [95, 99] band.
#
# The actual discriminator between PROFITABLE and BAG-HOLD wasn't price
# range or sum band — it was BID-STACK DEPTH. Profitable stretch had
# yes_bids fat AND no_bids fat (40+61 ct on snapshot). Bag-hold scenarios
# had one side near-empty (post-crash YES bids alive, NO bids gone, or the
# inverse on tonight's one-sided opens).
#
# Defaults below: only BID DEPTH gates BILATERAL classification. Price
# range and sum band kept as VERY permissive (1-99c, 0-99c) so they don't
# block real opportunities; if you want to tighten in production, raise
# these thresholds. The per-ticker stop-loss halt + rebalance-verify
# remain belt-and-suspenders if conditions degrade mid-window.
ARB_SESSION_DECIDED_LOW       = 1       # only block at extreme deep ITM/OTM
ARB_SESSION_DECIDED_HIGH      = 99      # — never trigger DECIDED in practice
ARB_SESSION_BILATERAL_ASK_LO  = 1       # virtually no ask-price gate
ARB_SESSION_BILATERAL_ASK_HI  = 99
ARB_SESSION_BILATERAL_MIN_DEPTH = 10    # THE actual gate: both bid stacks
                                        # need ≥10ct top-3 to support unwind
ARB_SESSION_BILATERAL_SUM_LO  = 0       # no lower bound on sum
ARB_SESSION_BILATERAL_SUM_HI  = 200     # no upper bound (effective): the
                                        # ARB_MAX_SUM_CENTS=97 threshold
                                        # already blocks non-arb sums at
                                        # the cycle level. The sum upper
                                        # bound here would just lock the
                                        # session as DIRECTIONAL on books
                                        # that open at sum=100-102 (= the
                                        # no-arb floor) and later drop
                                        # below threshold. Keep it open.

# ── ARB rebalance v4: complete-pair via opposite-side buy (per user 2026-04-28) ──
# When ARB legs fill asymmetrically, we COMPLETE the synthetic-flat pair by
# buying the missing leg, instead of selling the over-leg. On Kalshi binary
# contracts holding 1 YES + 1 NO = guaranteed $1 at settlement, so buying
# NO is mathematically equivalent to selling YES — but hits a different
# liquidity pool (no_asks vs yes_bids). Often more reliable when the bid
# stack we'd sell into is empty.
#
# Logic:
#   max_buy_price = 100 - original_leg_entry - ARB_REBALANCE_MIN_PROFIT_C
#   IOC buy at max_buy_price → Kalshi fills at best available ask up to ceiling
#   If filled: synthetic flat, +ARB_REBALANCE_MIN_PROFIT_C minimum profit/pair locked
#   If not filled: hold the over-leg (positive-EV), halt ticker for window
ARB_REBALANCE_MIN_PROFIT_C    = 1       # require ≥this many c profit per pair
                                        # to attempt the rebalance buy. Higher
                                        # = stricter (skip more cycles), lower
                                        # = take any pair-completion that fits.
                                        # 1c default = "any positive arb is worth
                                        # completing".

# ── ARB rebalance v5 (Claude 2026-04-28 per user) ─────────────────────────
# Phase 1 of pair-completion: rest a post_only BUY at bid + offset, wait
# for an active seller to cross our bid. This catches different liquidity
# flow than racing HFT for the ASK with an IOC.
# Phase 2 fallback: IOC buy at max_buy_price ceiling (the v4 path) if
# maker timeout expires.
ARB_REBALANCE_BUY_MAKER_OFFSET_C  = 1   # post_only bid at best_bid+this many c
                                        # (capped at best_ask-1 to avoid cross)
ARB_REBALANCE_BUY_MAKER_TIMEOUT_S = 5.0 # seconds to wait for seller to cross

# ── Microprice-aware limit entries (Claude 2026-04-28, B) ───────────────────
# When MICROPRICE_ENTRY_ENABLED, the engine uses depth-weighted microprice to
# refine maker-only entry pricing. If top-of-book is bid=30×100, ask=33×20
# (heavy buy interest), microprice = 30×20+33×100 / 120 = 32.5c — book wants
# to lift. Placing entry at 32c (above plain bid 30, still below ask 33) gets
# us a better fill price without paying the spread.
# Bounded by ask-1 (post_only-safe) and never reduces below ADAPTIVE-BID's
# output (microprice can only INCREASE entry, not decrease).
MICROPRICE_ENTRY_ENABLED      = True

# ── Book-depth ring buffer + wall consumption (Claude 2026-04-28, C) ────────
# Snapshots top-3 depth on each side every BOOK_DEPTH_SNAPSHOT_INTERVAL_S
# into a per-ticker ring (600 entries = ~10 min retention at 1s/tick).
# `_detect_wall_consumption(ticker, side, lookback_s)` returns the rate of
# ask-stack consumption — a verdict of AGGRESSIVE_BUY (>= 30 ct/s
# consumption) signals heavy directional flow on that side.
#
# Wired into LATE_DOMINANT confidence as a 7th component (weight 0.10) —
# fires when our entry side's ask is being aggressively consumed.
BOOK_DEPTH_SNAPSHOTS_ENABLED  = True
BOOK_DEPTH_SNAPSHOT_INTERVAL_S = 1.0
LATE_DOMINANT_W_WALL          = 0.10
LATE_DOMINANT_WALL_LOOKBACK_S = 10.0

# ── Late dominant weight rebalance (now sums to 1.0 with wall component) ────
# Old: d=0.25 c=0.20 a=0.20 poc=0.15 vwap=0.10 vel=0.10 → 1.00
# New: d=0.20 c=0.20 a=0.20 poc=0.10 vwap=0.10 vel=0.10 wall=0.10 → 1.00
LATE_DOMINANT_W_DISTANCE      = 0.20    # was 0.25 — shifted to wall
LATE_DOMINANT_W_POC           = 0.10    # was 0.15 — shifted to wall

# ── Maker → Taker escalation (Claude 2026-04-28, per user) ─────────────────
# When a post_only maker entry doesn't fill within MAKER_TAKER_TIMEOUT_S AND
# the book has moved against us by ≥ MAKER_TAKER_MOVE_THRESHOLD_C, the engine
# cancels the resting maker and places a taker at the current ask. Fixes
# the user's complaint about "no fills" when the book outruns post_only.
#
# Trade-off: pays the spread + 7% taker fee on these fills. Off-by-default
# would lose the alpha; on-by-default takes the slippage hit when needed.
# User approved 2026-04-28 evening: "yes" + "tick velocity at millisecond
# time frame" (the latter is a follow-up).
MAKER_TAKER_ESCALATION_ENABLED = True
MAKER_TAKER_TIMEOUT_S          = 5.0    # how long to wait for maker fill
MAKER_TAKER_MOVE_THRESHOLD_C   = 3      # min book move before we escalate
# 2026-04-28 (Claude #6): ms-level fast-path. If BTC velocity over the last
# 500ms exceeds this $/sec threshold IN THE ADVERSE DIRECTION (i.e. against
# the side we just entered), don't wait the full timeout — cancel the maker
# and escalate to taker NOW. Saves 3-4s of slip on flash moves.
# Calibrated for KXBTC15M: $25/sec = $7.50 over 300ms = real momentum.
MAKER_TAKER_MS_VEL_THRESHOLD   = 25.0

# ── Manual-trade TP autoplacer (Claude 2026-04-28) ──────────────────────────
# After capturing a USER MANUAL BUY fill, place a limit SELL (TP) at a
# % of remaining upside above entry. Lets the user enter directionally and
# the engine handle disciplined exits.
#
# Math: upside = 100 - entry; tp_offset = clamp(upside * PCT, MIN, MAX);
#       tp_price = min(95, entry + tp_offset).
#
# Each manual fill gets exactly one TP (idempotent via fill_id). The TP's
# order_id is added to engine_order_ids by the place_order wrapper, so the
# fills poller does NOT re-snapshot the TP as a new manual fill.
#
# OFF by default — hedge plays (e.g. buy YES at 30c + NO at 66c for arb)
# would be ruined by auto-TPs on both sides. Flip ON only for sessions
# where you want the engine to manage exits on directional entries.
MANUAL_TP_ENABLED             = True   # 2026-04-28: flipped ON per user
                                          # ("we just need a take profit")
# 2026-04-28 evening: switched math from % of remaining-upside to % of capital
# deployed. User clarified: target is 10% of $ entered, not 30% of upside.
# Math: tp_offset_cents = round(entry_cents * MANUAL_TP_PCT_OF_ENTRY).
#   30c @ 30ct entry ($9 cap) → +3c @ 33c TP → +$0.90 (10%)
#   50c @ 100ct entry ($50)   → +5c @ 55c TP → +$5.00 (10%)
#   65c @ 30ct entry ($19.50) → +6.5c → +6c → 71c TP → +$1.80 (~9%)
# For early-session inversion scalps where 3-5c bounces are common.
MANUAL_TP_PCT_OF_ENTRY        = 0.15    # 15% of entry price (was 0.10 —
                                        # bumped 2026-05-01 PT per user:
                                        # +5c TPs were closing winners
                                        # too early; manual scalps had
                                        # more room to run)
MANUAL_TP_MIN_OFFSET_CENTS    = 2       # at least 2c above entry (covers fees)
MANUAL_TP_MAX_OFFSET_CENTS    = 20      # cap absolute offset (was 15 —
                                        # bumped 2026-05-01 PT to allow
                                        # higher-conviction TPs on deep
                                        # mid entries like 50-65c)
MANUAL_TP_MAX_PRICE_CENTS     = 95      # never sell above 95c (Kalshi cap)
MANUAL_TP_MIN_ENTRY_CENTS     = 5       # don't TP fills below this — too cheap, may be a probe
MANUAL_TP_MAX_ENTRY_CENTS     = 92      # above this, no room for profit after fees
MANUAL_TP_SKIP_IF_HEDGED      = True    # skip TP if other side has open position
                                          # on this ticker (hedge protection)
# Legacy alternative — 30% of remaining upside (engine-style ride). Kept for
# fallback; not currently active. Ignore unless MANUAL_TP_PCT_OF_ENTRY <= 0.
MANUAL_TP_PCT_OF_UPSIDE       = 0.30

# ── TA_FORCED fixed-fraction sizing (Claude 2026-04-28) ─────────────────────
# Overrides the Kelly + regime damper + conviction multiplier + boost paths
# with a deterministic % of balance per entry, capped at MAX_CONTRACTS.
#
# Rationale: 30d data shows damper × DD shrinks size aggressively in
# CHAOTIC/hard_dd, producing 1-4ct trades that win 79% with +$30 EV/trade
# — but the EV is wasted on tiny size. Larger trades (50-99ct) lost in that
# sample because they happened in "good" regimes where the damper relaxed
# but signals were weaker. With the relaxed DOMINANT gate now letting more
# FVG signals through, we want to size them CONSISTENTLY so we can validate
# signal quality on a uniform distribution, then iterate.
#
# Example math at $1064 balance, 5% fraction, 30ct cap:
#   entry @ 45c: target=$53.20, raw=118ct, capped=30ct, risk=$13.50/trade
#   entry @ 75c: target=$53.20, raw=70ct, capped=30ct, risk=$22.50/trade
#   entry @ 95c: target=$53.20, raw=56ct, capped=30ct, risk=$28.50/trade
# Worst-case full loss at 30ct@95c = -$28.50 = 2.7% of balance.
TA_FORCED_FIXED_FRACTION_ENABLED = True
# 2026-04-28 evening: time-of-day sizing per user. Bigger caps during
# active US hours (8 AM - 9 PM local), conservative overnight when book
# liquidity thins and slippage compounds. Local time = engine host clock
# (PT for the original install). The legacy single-knob constants below
# are kept for back-compat but the day/night pair takes precedence.
TA_FORCED_DAY_HOURS_START        = 8      # local hour [inclusive] day starts
TA_FORCED_DAY_HOURS_END          = 21     # local hour [exclusive] day ends (9 PM)
TA_FORCED_FIXED_FRACTION_DAY     = 0.08   # 8% of balance during day hours
TA_FORCED_FIXED_FRACTION_NIGHT   = 0.02   # 2026-04-29 retune (was 0.05)
TA_FORCED_FIXED_MAX_CONTRACTS_DAY   = 15  # 2026-04-29 retune (was 50)
TA_FORCED_FIXED_MAX_CONTRACTS_NIGHT = 5   # 2026-04-29 retune (was 30)
# Legacy/back-compat knobs — only used if the day/night ones aren't set.
TA_FORCED_FIXED_FRACTION         = 0.05
TA_FORCED_FIXED_MAX_CONTRACTS    = 30

# ── TA_FORCED stop loss (Claude 2026-04-28, per user) ──────────────────────
# Mirrors the LATE_DOMINANT_STOP pattern. When the side bid drops STOP_CENTS
# below the original entry, market-sell the truth count. Position-truth via
# get_positions() so a residual / ghost still gets fully flattened.
#
# Why now: with the day-hours size-up to 50ct, full-loss exposure on a deep-
# ITM entry (e.g. 30ct YES @ 77c = $23) bounded to ~$4 by the 8c stop. EV
# math at 65% WR / +6c avg TP / 50ct flips from -$6.80/trade (no stop) to
# +$0.55/trade (8c stop). Critical to maintain positive EV at bigger size.
#
# User noted: "8c makes sense especially because we're bidding into entry"
# — the maker-only entry path means every fill is at-or-below where you'd
# pay if market-buying, so the 8c trigger represents a real adverse move
# rather than just spread normalization.
TA_FORCED_STOP_ENABLED           = True
TA_FORCED_STOP_CENTS             = 8      # bid drop below original entry → flatten

# ── Hybrid stop trigger (Claude 2026-04-30, per parallel-terminal forensic) ──
# Parallel terminal lost $7.41 on YES @ 57c → expiry-zero because:
#   yes_ask CRASHED 39c → 8c (sellers desperate)
#   yes_bid stayed STICKY near 60c (stale resting orders)
#   implied bid (= 100 − no_ask=37) = 63c — also above trigger
#   stop never fired; contract settled $0.
#
# Hybrid trigger fires the stop on ANY of:
#   (a) bid       ≤ entry − STOP_CENTS    (current behavior, conservative)
#   (b) mid       ≤ entry − STOP_CENTS    (catches sticky-bid case)
#   (c) pre-expiry safety: minutes_remaining < N AND mid ≤ entry − M
#
# (a) and (b) share STOP_CENTS. (c) uses a tighter threshold near expiry
# so dying contracts get exited before settlement-cliff. Applied to both
# LATE_DOMINANT_STOP and TA_FORCED_STOP.
STOP_USE_MID_TRIGGER            = True   # Trigger (b): mid-based fallback
STOP_PREEXPIRY_MIN_REMAINING    = 3      # Trigger (c): minutes-to-expiry threshold
STOP_PREEXPIRY_MID_DROP_C       = 5      # Trigger (c): mid drop below entry

# ── Patch #7: Pre-expiry forced flatten (Claude 2026-04-30) ────────────────
# Last-line-of-defense backstop. When <PRE_EXPIRY_FLATTEN_SECONDS remain,
# regardless of bid/mid/fair triggers, force market sell at price=1¢ to
# guarantee fill. Prevents held-to-expiry-at-$0 losses from any stop bug,
# cache-lag false-zero, sticky book, or unforeseen failure mode.
PRE_EXPIRY_FLATTEN_ENABLED      = True
PRE_EXPIRY_FLATTEN_SECONDS      = 180.0  # 3 min before expiry

# ── Patch #8: Fair-value stop trigger (Claude 2026-04-30) ──────────────────
# Third trigger added to bid+mid: BB model's fair value. Catches the case
# where both bid AND mid lie (deep crossed/illiquid book) but the engine's
# own probability model knows the contract is dying.
TA_FORCED_STOP_USE_FAIR         = True
LATE_DOMINANT_STOP_USE_FAIR     = True

# ── Patch #9: Aggressive 1¢ sell on stop fire (Claude 2026-04-30) ──────────
# Replace `bid - 1` sell price with hard 1¢ limit (post_only=False). The
# previous bid-1 could fail to fill on sticky/illiquid books. 1¢ guarantees
# the order crosses — whatever bidder is highest on the book takes us out.
# The decision to STOP has already been made, so accepting whatever bid
# exists is correct.
STOP_SELL_AT_PENNY               = False  # 2026-04-30 user: "limit sell, not unintelligent". Penny only fires in final 30s before expiry as last-resort fill guarantee.

# ── Patch #10: Don't trust single-zero from get_positions (Claude 2026-04-30) ──
# Local-only bug seen overnight: stop fires, get_positions() briefly returns
# zero (Kalshi cache lag), engine clears state, position re-appears, gets
# tagged as "user manual trade" by SYNC MANUAL-DETECTED gate. Fix: when stop
# fires within 60s of fill AND get_positions returns zero, retry up to N
# times before trusting the zero.
STOP_TRUST_ZERO_REQUIRES_RETRIES = 3
STOP_RETRY_INTERVAL_S           = 1.0

# ── TA_FORCED wall-consumption integration (Claude #3, 2026-04-28) ─────────
# The wall-consumption detector reads the per-ticker book-depth ring buffer
# (built from WS snapshots in _book_depth_history) and reports whether ask
# depth is being aggressively consumed (verdict=AGGRESSIVE_BUY) — i.e.
# market buyers lifting offers at speed.
#
# Two orthogonal levers wired into TA_FORCED:
#   VETO  — if the OPPOSITE side is being aggressively bought, smart money
#           disagrees. Block our entry. Conservative; defaults on.
#   BOOST — if OUR side is being aggressively bought, that's confirmation.
#           Multiply conviction by (1 + BOOST_PCT). Propagates into Kelly
#           / edge_sizer downstream.
#
# Lookback window: 10s aligns with how long top-of-book consumption events
# typically last on KXBTC15M. Shorter windows over-fit to spurious depth
# changes; longer windows lag the actual aggression.
TA_FORCED_WALL_CONSUMPTION_ENABLED = True
TA_FORCED_WALL_VETO              = True
TA_FORCED_WALL_BOOST             = True
TA_FORCED_WALL_LOOKBACK_S        = 10.0
TA_FORCED_WALL_BOOST_PCT         = 0.10   # +10% conviction on agg-buy confirm

# 2026-04-22: skip-first-window safety. Historically protected against stale
# resting orders on restart. In paper mode during active iteration we flip
# it off so restarts don't burn 15 min each. Keep True for live; False in paper.
STARTUP_SKIP_WINDOW = False
PRICE_ACTION_ENABLED = False        # OFF
# TA_INVERSION disabled 2026-04-18 based on post-rebuild data (35 trades):
#   ALIGNED  (TA agrees w/ entry side): 18 trades, 83% WR, +$37.94
#   INVERTED (TA opposes entry side):   17 trades, 41% WR, -$98.06
#   STRONG+INVERTED was the killer: 0/6 WR, -$88.88 across 6 trades.
#   Exits (peak_giveback, expired_pending) look broken because they fire
#   on inverted entries â€” the same exit logic is +$3.71 on ALIGNED trades.
#   The microstructure reads head-fakes; when TA disagrees, trend wins.
#   Counterfactual: same 35-trade tape with inversion off = +$37.94
#   instead of -$65.76 (WR 60% -> 83%, avg_loss -$8.12 -> -$1.26).
TA_INVERSION_ENABLED = False

# â”€â”€ MTF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MTF_ENABLED = True
MTF_SHADOW_MODE = False
MTF_MIN_CONFLUENCE = 0.3
MTF_SIZE_MULTIPLIER_HIGH = 2.0      # Was 1.5 â€” high confluence gets doubled (2026-04-21)
MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h"]
PRICE_FEED_SYMBOL = "btcusdt"
PRICE_FEED_WS_TIMEOUT_S = 30.0

# â”€â”€ Paper Trading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 2026-04-23 04:55 UTC: flipped to LIVE per user directive. User said
# "push it live" and authorized autonomous management during their absence.
# Guardrails in place: DAILY_LOSS_LIMIT=$25, SIZING_HARD_CAP=80ct,
# SR_FADE_MIN_EDGE_PP=5.0, L1 stability gate, residual reconciler,
# one-open-position mutex. Flip back to True if anything unexpected.
PAPER_TRADING = False  # 2026-05-01 09:00 PT: live with full BB_PURE stack
                       # Cumulative fixes since session start:
                       #   - Preflight TP at fill (FVG-close target)
                       #   - Strategy-aware protective_order maintain
                       #   - Atomic place-then-cancel on protective replan
                       #   - Count-poll + OVERRUN detection (3s, 6 polls)
                       #   - BB_PURE preserved across SYNC RECLAIM
                       #   - SCALP DCA gated off on BB_PURE
                       # Account at restart: $1441.77
PAPER_STARTING_BALANCE = 100.0
PAPER_SLIPPAGE_CENTS = 1

# â”€â”€ Advanced â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# MAX_TRADES_PER_WINDOW evolution:
#   was 1: -$215/day regression from counter-reset bug (2026-04-12)
#   was 3: partial uncap (2026-04-14), still clipped Wednesday-style stacking
#   was 999: full uncap (2026-04-17 rebuild). Wednesday had 352 fills/day
#     with multi-fire at extreme edge.
#   now 1 (2026-04-21): re-entry after TP erased profit + stacked losses. The
#     minute-level observation: once our thesis settles, re-entering the same
#     window chases noise. Belt-and-suspenders with _entered_tickers_this_window
#     ticker-lock set populated at fill time (see polymarket_copy_engine.py
#     lines ~7315 and ~5696) â€” the config cap binds first, ticker lock catches
#     any DOMINANT_UPGRADE / FLIP_INVERT path that bypasses the counter.
MAX_TRADES_PER_WINDOW = 1          # 1-per-window hard lock; ticker-lock belt-and-suspenders
TRADE_COOLDOWN_SECONDS = 3600      # 2026-04-29 retune (was 10): 1-hour
                                    # spacing between fills. Combines with
                                    # MAX_TRADES_PER_WINDOW=1 for hard
                                    # rate-limiting.
MIN_MINUTES_REMAINING = 2.0
MAX_MINUTES_REMAINING = 15.0
VWAP_FALLBACK_MINUTES = 10          # Enter without VWAP after 10 min (no pullback = trending)

# â”€â”€ Features that DON'T EXIST in profitable version â€” disabled â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SELL_LADDER_ENABLED = False         # Did not exist in profitable zip
FLIP_REENTRY_ENABLED = False        # Did not exist in profitable zip
SNIPER_ENABLED = False              # Disabled â€” drained account $172â†’$8 while "off"

# â”€â”€ Wallet copying (2026-04-21: disabled â€” microstructure is primary) â”€â”€â”€â”€â”€â”€â”€
# User directive: "keep the smart wallets out of it, they're probably scalping
# or waiting til the last minute to enter on most likely outcome."
# With microstructure pressure live-gating entries (DOMINANT-DIRECTION gate),
# wallet signals are a throttle / lagging indicator. Disable polling overhead
# and keep the signal cascade on its microstructure+TA_FORCED path.
# Left as a switch so we can A/B it later if we want.
WALLET_COPY_ENABLED = False
WALLET_SCORING_ENABLED = False      # Skip hourly Polymarket wallet rescore

# â”€â”€ Maker-only execution (2026-04-20) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Kalshi fee schedule: 0% maker rebate, ~7% taker on BTC markets.
# Forensic review: paying the ask on "market entry" paths was burning 3-5c/trade
# in hidden slippage on top of the fee. Maker-only flips that: enter at the bid
# with post_only=True (Kalshi rejects if the price would cross), accept that
# some entries will miss, and let TP limits rest at/above the ask as usual.
# Stops have been killed (scalp_stop, peak_giveback) so there's no code path
# that WANTS to cross the spread on exit either.
MAKER_ONLY = True

# â”€â”€ Kelly sizing (2026-04-20, replaces fixed SIZING_BALANCE_FRACTION) â”€â”€â”€â”€â”€â”€â”€
# KELLY_ENABLED: when True, contract count is computed from model-edge instead
# of a flat 30% of balance. Formula:
#     f* = (b*p - q) / b   where p=model_prob, q=1-p, b=(1-mkt)/mkt
#     size_contracts = balance * (f* * KELLY_FRACTION) / market_price
# KELLY_FRACTION = 0.25 is quarter-Kelly â€” Octagon's half-Kelly convention
# dialed down once more because our edge estimates are noisy (Brownian Bridge
# on 15-min BTC). Clamped to [KELLY_MIN_FRAC, KELLY_MAX_FRAC] of balance so a
# single high-conviction trade can't blow up the account.
KELLY_ENABLED = True
# Aggressive sizing (2026-04-21): user directive was "throw precautions to the
# wind on entry sizing â€” sizing on good trades is how we made $1k." Bumped
# fraction from 0.25 (quarter-Kelly) to 0.65 (between half and three-quarter
# Kelly). At this fraction, monster-edge trades push toward the 80% ceiling,
# mediocre trades stay small (so losses on noise are small, wins on alpha are
# big). Combined with 150-contract cap and 2.0x MTF boost, a high-conviction
# entry at $100 balance sizes ~$60; at $300 balance ~$180.
KELLY_FRACTION = 0.25               # 2026-04-29 retune (was 0.65): quarter-
                                     # Kelly. The 0.65 setting compounded with
                                     # the cap-bypass on the GHOST race and
                                     # produced 53ct positions on $100 base.
KELLY_MIN_FRAC = 0.02               # 2026-04-29 retune (was 0.03): floor
KELLY_MAX_FRAC = 0.15               # 2026-04-29 retune (was 0.80): monster-
                                     # edge cap. Even maximum-conviction trades
                                     # capped at 15% of balance pre-cap.
KELLY_MIN_EDGE_PP = 2.0             # unchanged — still skip sub-noise entries

# â”€â”€ Phase 2: regime Ã— drawdown sizing damper (2026-04-21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Multiplies the raw sizing output (Kelly OR edge_sizer) by
#   regime_factor Ã— drawdown_factor
# using the "aggressive" ladder defined in regime.py:
#   regime:   STRUCTURED 1.10 / CHOP 0.60 / CHAOTIC 0.25
#   drawdown: FLAT       1.00 / SOFT_DD 0.55 / HARD_DD 0.25
# Worst case (CHAOTIC+HARD_DD) = 0.06Ã—. Best case (STRUCTURED+FLAT) = 1.10Ã—.
# DOMINANT gate is UNCHANGED â€” this is sizing only.
REGIME_DAMPER_ENABLED = True
# Floor after damping. When damped contracts < this, the entry is SKIPPED
# (not floored to 1) so CHAOTIC+HARD_DD really means "stop trading".
REGIME_DAMPER_MIN_CONTRACTS = 1

# â”€â”€ Phase 3: shadow edge model (2026-04-21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Emits CopyEngine SHADOW-EDGE: ... log lines alongside DOMINANT decisions.
# LOGS ONLY, no orders, no sizing influence. Weights in shadow_edge.py.
SHADOW_EDGE_ENABLED = True

# 2026-04-23 15:59 UTC: HALTED after first Phase-N live trade lost âˆ’$57.
# Root cause: adaptive-hold's vel30s read is STALE during a held position.
# The SR-FADE evaluator returns early when _open_position is not None, which
# skips the `_sr_mid_buf.append` inside it. So during the 4-min hold, vel30s
# stayed at +0c even while YES crashed from 50c â†’ 1c. Engine flew blind.
# FIX SHIPPED 2026-04-23: buffer append moved into _flow_iteration per-cycle
# path (after contract_sr.update), so vel30s stays fresh during held
# positions. Adaptive-hold logic now sees the real tape.
#
# Re-enabled 2026-04-23 20:30 UTC by user directive ("LET IT TRADE").
# Halt at 19:43 was triggered after -$107 cumulative loss run. Since then:
#   - vwap_flip_exit SR_FADE guard (fixed the -$37 bug)
#   - HOLD_EXPIRY inversion-abort SR_FADE guard
#   - MFE-lock bid sanity check (15c band vs tape mid)
#   - SR_FADE_BTC_CONTINUATION_VETO per Codex spec (blocks entries when
#     BTC is still pushing against the fade on both 5s and 30s horizons)
#   - btc_5s/30s/300s logged in ladder_detail for next-session calibration
# 168/168 tests pass. Running with all fixes active.
SR_FADE_ENABLED = False  # 2026-04-27 12:35 PT user directive: "no more SR_FADE". FVG/Brownian-Bridge engine (TA_FORCED tier) is the new live primary. Reversion ladder updated in docs/ai_collab/decisions/2026-04-27_strategy-reversion-ladder.md.

# â”€â”€ Phase 5.1: adaptive bid (2026-04-21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# When pressure.confidence >= ADAPTIVE_BID_CONFIDENCE_MIN and pressure
# direction agrees with our side, bid at best_bid + 1c (still post_only).
# Kalshi rejects if that would cross; we fall back to best_bid in that case.
ADAPTIVE_BID_ENABLED = True
ADAPTIVE_BID_CONFIDENCE_MIN = 0.75

# â”€â”€ Phase 5.2: micro-pullback entry (2026-04-21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DISABLED 2026-04-22: safety review after oversell incident. Adds state
# complexity that made forensics harder; re-enable after A2â€“A5 are validated.
MICRO_PULLBACK_ENABLED = False
MICRO_PULLBACK_WAIT_S = 10
MICRO_PULLBACK_CENTS = 2
MICRO_PULLBACK_SKIP_IF_FVG_WIDENS = True

# â”€â”€ Phase 5.3: layered take-profit (2026-04-21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DISABLED 2026-04-22: prime suspect in the oversell-to-NO incident. Layered
# legs interact badly with DCA rebuild â€” cancel-then-place race at
# polymarket_copy_engine.py:10685 can leave un-cancelled stretch legs live
# while fresh TPs are placed, creating sell-volume > held-volume â†’ short.
# Re-enable only after A2â€“A5 are live and residual reconciler confirms zero
# OVERSELL-DETECTED events in a full session.
TP_LAYERED_ENABLED = False
TP_LAYER_BASE_FRACTION = 0.60
TP_LAYER_STRETCH_CENTS = 5

# 2026-05-02 Phase 0.1.3: hard kill on SCALP DCA when BB_PURE-only.
# Live test 01:42 PT: SCALP DCA fired on a phantom position (orphan-flatten
# residue with strategy_name=TA_FORCED) on a locked ticker, adding 60ct NO
# @ 2c. The strategy_name-based gate ("not BB_PURE and not SR_FADE")
# passed because the phantom didn't carry BB_PURE labels. Hard global kill
# is the simplest fix while we're BB_PURE-only.
SCALP_DCA_ENABLED = False

# â”€â”€ Safety: oversell hardening umbrella (A6, 2026-04-22) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Master kill-switch for A2 (bool-returning cancel), A3 (gated DCA rebuild),
# A4 (double-gated ticker lock), and A5 (residual reconciler). Default on.
# Flip False ONLY if the new code path misbehaves; reverts to pre-safety
# behavior without requiring a code deploy.
SAFETY_OVERSELL_HARDENING = True

# â”€â”€ Phase C: contract-native S/R detector (2026-04-22) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Observation-only until SR_FADE (Phase F) ships. When enabled, the engine
# feeds Kalshi mid ticks to a per-ticker detector that tracks dwells at each
# cent level. Levels are surfaced via nearest_support/nearest_resistance APIs.
CONTRACT_SR_ENABLED = True

# â”€â”€ Phase E: cheap-leverage sizing multiplier (2026-04-22) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# When the final entry price is cheap AND conviction is high AND the regime
# damper is not punitive, scale size up. This formalizes the "big + cheap +
# thin margin + conviction" profile from the $1k winning day.
# Applied AFTER the Phase 2 regime damper in _apply_regime_damper output path.
CHEAP_LEVERAGE_ENABLED = False         # Settlement truth: cheap 10-35c SR_FADE bucket is the largest loss bucket; disable until revalidated.
CHEAP_LEVERAGE_MAX_PRICE = 35           # only fires when entry <= 35c
CHEAP_LEVERAGE_MIN_CONVICTION = 0.70    # pressure.confidence or signal conviction
CHEAP_LEVERAGE_MULT = 1.75              # multiply contracts when gate hit

# â”€â”€ Phase F: SR_FADE signal tier (2026-04-22) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Enters when live mid is within SR_FADE_TOUCH_CENTS of a detected S/R level
# with strength >= SR_FADE_MIN_STRENGTH, regime != CHAOTIC, and no other
# tier has fired in this window. Exit target is percentage-based.
# 2026-04-23 08:25: see earlier halt block at line ~198 â€” DO NOT set True
# here; the canonical assignment is the False at line 198. Left commented
# to preserve the historical intent of enabling the tier in Phase F.
# SR_FADE_ENABLED = True  # duplicate assignment removed 2026-04-23
SR_FADE_TOUCH_CENTS = 3                 # within 3c of detected level
# K1 (2026-04-23): 1.5h low-vol period yielded 0 SR-FADE fires. Relaxed to
# 0.25 to build up sample size for statistical validation. If this produces
# too many poor-quality fires, dynamic sizing will naturally shrink them
# (low confidence â†’ low fraction) so the risk of each weak trade is small.
SR_FADE_MIN_STRENGTH = 0.25
SR_FADE_EXIT_PCT = 0.20                 # target: entry Ã— (1 + 0.20) for base
SR_FADE_EXIT_MIN_CENTS = 5              # floor on target (don't exit at <5c profit)
SR_FADE_TIME_CAP_MIN = 10               # market-sell at minute 10 if still open
# Post-live tuning 2026-04-23 (second session): late-session contrarian
# entries are dangerous. 19:30 ET window traded NO @ 30c at minute 8:04,
# flattened at minute 10:00 on a mild 2c/30s adverse move. By minute 8
# the directional move is baked in and the fade has no runway to work
# before the soft cap. Tighten entry gate from 10 min â†’ 7 min so entries
# must fire within the first half of the window, leaving â‰¥8 min of
# runway before the soft-cap adaptive-hold kicks in.
SR_FADE_ENTRY_CAP_MIN = 7               # block NEW entries past minute 7
SR_FADE_MAX_ENTRY_PRICE = 55            # was 45 â€” raised to catch mid-range fades (2026-04-23)
SR_FADE_MIN_ENTRY_PRICE = 36            # settlement truth: cheap 10-35c bucket 11% WR / -$270.89; mid 36-55c is less bad and the only bucket to revalidate first

# J1 (2026-04-22): dynamic SR-FADE sizing scaled by observed confidence.
# Replaces the generic Kelly/edge_sizer path for SR_FADE tier. The earlier
# 21:47:57 trade got 26ct at 32c ($8.32, 8% of balance) with edge=+66.7pp
# and strength=0.60 â€” under-sized given the setup. This formula treats
# confidence as a composite of observed signals and lets strong setups push
# deeper into the balance.
#   fraction = SR_FADE_SIZE_MIN + (SR_FADE_SIZE_MAX - SR_FADE_SIZE_MIN) * confidence
# where confidence = weighted sum of normalized level-strength, edge_pp,
# and pressure.confidence. Regime damper still applies AFTER this fraction
# so hard_dd or CHAOTIC cuts size defensively.
SR_FADE_DYNAMIC_SIZING = True
SR_FADE_SIZE_MIN_FRAC = 0.02            # validation mode: keep weak fires tiny while rebuilt labels/veto data accumulate
SR_FADE_SIZE_MAX_FRAC = 0.15            # validation mode until 20-30 clean post-veto fills prove edge under settlement truth
SR_FADE_EDGE_SATURATION_PP = 30.0       # +30pp edge contributes max to confidence
SR_FADE_STRENGTH_SATURATION = 0.80      # strength 0.80+ contributes max
# Component weights (must sum to ~1.0)
# Phase N4 (2026-04-23): rebalanced weights with velocity_quality term.
# Sum must = 1.0. A still-accelerating tape now shrinks size materially
# even on high-strength / high-edge setups (which was the signature of
# today's two losers).
SR_FADE_W_STRENGTH = 0.30               # was 0.35 â€” contract_sr level evidence
SR_FADE_W_EDGE = 0.40                   # was 0.50 â€” BB model mispricing
SR_FADE_W_PRESSURE = 0.10               # was 0.15 â€” microstructure pressure confidence
SR_FADE_W_VELOCITY = 0.20               # N4: deceleration quality

# â”€â”€ P1-1 (2026-04-23): oriented pressure in SR_FADE sizing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Attribution on 12 reconciled SR_FADE trades showed the pressure-confidence
# sizing term was miscalibrated: strong trend pushing AGAINST the fade
# increased size. Disagree/high-conf bucket was 33% WR, -$54.47 on 3 trades.
# The fix orients the pressure component to the fade direction:
#   agree    (ps and buy-side same sign)    â†’ full p_conf
#   neutral  (|ps| <= NEUTRAL_BAND)          â†’ 0.5 Ã— p_conf
#   disagree (ps opposes buy-side)           â†’ DISAGREE_MULT Ã— p_conf
# Default DISAGREE_MULT=0 zeros the term when pressure fights the fade.
# Cheap-leverage multiplier also stands down when orient=disagree, so we
# can't supersize a fade a confident tape is pushing against.
SR_FADE_PRESSURE_NEUTRAL_BAND = 0.02    # |pressure_score| <= this = neutral
SR_FADE_PRESSURE_DISAGREE_MULT = 0.0    # 0 = zero the term on disagree

# â”€â”€ MFE-LOCK (2026-04-23, per Codex review after live burst-vs-expiry data)
# Ship-narrow rule to monetize the SR_FADE snapback before it round-trips.
# Live evidence 2026-04-23: YES 38 @ 39c hit +10c MFE, resting TPs missed,
# reverted, settled -$14.82. MFE analysis on 6 trades showed median
# time-to-MFE of 2.5 min and $167 in realized opportunity lost to giveback.
# Rule: arm after unrealized profit >= ARM_CENTS (executable bid, not mid).
# Then track peak. Exit on giveback >= max(GIVEBACK_MIN, GIVEBACK_PCT*peak).
# Only SR_FADE. Only pre-soft-cap (adaptive time-cap owns post-soft-cap).
SR_FADE_MFE_LOCK_ENABLED = True
SR_FADE_MFE_LOCK_ARM_CENTS = 5          # arm after +5c unrealized profit
SR_FADE_MFE_LOCK_GIVEBACK_MIN_CENTS = 3 # floor on giveback trigger
SR_FADE_MFE_LOCK_GIVEBACK_PCT = 0.50    # also trigger at 50% of peak
SR_FADE_MFE_LOCK_BID_SANITY_CENTS = 15  # ignore WS bid if far from REST tape same-side mid
SR_FADE_EXCURSION_TRACKER_ENABLED = True
SR_FADE_EXCURSION_TRACKER_INTERVAL_S = 5 # log executable bid/profit every 5s while SR_FADE is open

# â”€â”€ BTC continuation veto (2026-04-23 per Codex review after trade 6) â”€â”€â”€â”€â”€â”€
# Trade 6 today: NO 210 @ 34c, +66pp edge, cheap entry, tape "stable" on
# contract mid â€” but BTC was driving NO toward 0 the whole 14-min hold.
# Codex's diagnosis: high BB edge is garbage if BTC continuation dominates.
# This veto blocks new SR_FADE candidates when all three conditions hit:
#   (1) BTC 30s and 5s moves agree in sign
#   (2) That sign is against the fade side (NO fades a down-BTC; YES up-BTC)
#   (3) |btc_30s|>=VETO_30S AND |btc_5s|>=VETO_5S
# Optionally require pressure orientation != "agree" to allow tape-supported
# fades through even during mild BTC continuation.
# Provisional thresholds before calibration: 20 USD 30s, 5 USD 5s.
SR_FADE_BTC_CONTINUATION_VETO_ENABLED = True
SR_FADE_BTC_VETO_30S_USD = 20.0
SR_FADE_BTC_VETO_5S_USD = 5.0
# Codex P2 (2026-04-23): config name clarified. When True, the veto applies
# whenever pressure is NOT agree (i.e., "neutral" or "disagree"). When False,
# the veto does not consult pressure at all and blocks purely on BTC signals.
# Pressure=agree always bypasses the veto because a fade with pressure
# backing it is a higher-quality setup even during BTC continuation.
SR_FADE_BTC_VETO_BYPASS_ON_PRESSURE_AGREE = True
# Back-compat alias (old name) â€” still read by the evaluator so in-flight
# overrides don't silently stop working; will be removed next cleanup.
SR_FADE_BTC_VETO_REQUIRE_PRESSURE_DISAGREE = True

# â”€â”€ Phase N gates (2026-04-23 post-live tuning) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# N1: velocity/deceleration gate. Replaces the old L1 stability check. We
# require mid displacement to be DECREASING across three nested windows
# AND the last 5s to show near-stabilization. Rejects signals that arrive
# mid-leg of a directional move â€” the catch-a-falling-knife signature
# behind today's YES 120ct @ 34c â†’ 4c and NO 180ct @ 32c â†’ 1c losses.
SR_FADE_USE_VELOCITY_GATE = True
SR_FADE_VELOCITY_WINDOWS_S = (30, 15, 5)
SR_FADE_MAX_RECENT_VEL_CENTS = 2

# N2: adaptive time-cap. At minute SR_FADE_TIME_CAP_MIN (10) we now
# consult the 30s signed velocity on OUR side price. If moving toward
# profit, hold up to SR_FADE_MAX_HOLD_MIN. If moving adversely by
# SR_FADE_ADVERSE_VEL_CENTS+, market-sell immediately. Flat = hold to
# SR_FADE_MAX_HOLD_MIN then flatten. 1-min buffer before expiry is
# non-negotiable.
SR_FADE_ADAPTIVE_TIME_CAP = True
SR_FADE_MAX_HOLD_MIN = 14
# 2026-04-23 retune: 2c/30s is noise-level (4bp/s); flattened us at 21c on
# NO 210 @ 30c just before the price bounced to 36c (+6c target) where our
# TP would have filled. Raising to 5c (10bp/s) so only a clearly directional
# move triggers the adaptive-flatten â€” random walk wiggles hold to hard cap.
SR_FADE_ADVERSE_VEL_CENTS = 5

# N3: opposite-side edge block. M2 rejects when OUR-side edge is below
# +5pp. N3 adds a second rejection: if the BB model has strong edge on
# the OPPOSITE side (our fade is against a screaming model signal), skip.
# Today's two losers both had opposite-side edge â‰ˆ +25â€“30pp â€” signals we
# should never have taken. 10pp is conservative; can tighten later.
SR_FADE_OPPOSITE_EDGE_BLOCK_PP = 10.0
# G2 (2026-04-22): max slippage between signal-gen mid and exec-time ask.
# If current ask exceeds signal.kalshi_mid_cents + this cents, abort the
# entry â€” the fade thesis is broken if market ran through the level.
SR_FADE_MAX_SLIP_CENTS = 2

# H3 (2026-04-22, post-live-monitoring): directional-move gate.
# SR-FADE fires only when BTC has moved >= SR_FADE_MIN_BTC_MOVE in 5min,
# and we buy the LOSING side (opposite of BTC direction). This matches
# the user's manual alpha: "BTC has clearly moved up, the cheap NO side
# is now priced for the current BTC but will appreciate on the inevitable
# retrace/shake-out". Without this gate SR-FADE fired on pure level
# touches regardless of setup, catching noise.
SR_FADE_MIN_BTC_MOVE = 10.0          # $ BTC must have moved in 5min to arm

# L1 (2026-04-23): stability gate. Prevents entering when BTC is still
# accelerating through the fade setup (observed 03:23 -$9 loss: entered NO
# at 12c while YES was still rallying from 80â†’88). Require the YES mid to
# have moved â‰¤STABILITY_MAX_CENTS over the last STABILITY_WINDOW_S seconds
# before firing. If the tape is calm (fade has peaked / started reverting),
# the setup is economical. If still moving, we're catching a falling knife.
SR_FADE_STABILITY_WINDOW_S = 10
SR_FADE_STABILITY_MAX_CENTS = 4

# M1 (2026-04-23): mutex mode â€” "flat" allows re-entry within the same window
# after a clean exit (no overlap with open position). Prior "window" mode
# locked out for the full 15 min even after a 30s TP close, leaving alpha
# on the table. "flat" is the user-preferred setting post-safety-layer landing.
SR_FADE_MUTEX_MODE = "flat"             # "flat" | "window"
SR_FADE_POST_EXIT_COOLDOWN_S = 60       # 60s cooldown after position closes to prevent instant re-fire on same level

# M2 (2026-04-23 pre-live): hard edge floor. 04:45 trade fired with +2.8pp
# edge + 0.27 strength and lost $5.92 when YES ran to expiry. Reject any
# SR-FADE setup where model-implied edge is below this threshold. Weak edges
# don't survive slippage + retrace variance.
SR_FADE_MIN_EDGE_PP = 5.0

# H4 (2026-04-22): looser SR-FADE strength/touch requirements so the tier
# can fire early in session. Prior: strength>=0.55 + within 2c â€” too tight
# to fire in minutes 1-5 when dwell evidence is thin. Relaxing unlocks
# the user's "early entry" window while seeded priors (G4 floor) provide
# the level candidates.
# Overrides earlier defaults where these same names were set tighter.

# â”€â”€ ATM Reversion / Strike-Pin Reversion (Codex handoff 2026-04-25) â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Buy the side priced under fair value when BTC is within ATM_MAX_STRIKE_DIST_PCT
# of the contract strike, harvest repricing via limit exit at +profit_target_c
# above entry or target_c absolute (whichever fires first).
#
# 30-day backtest (1098 fills, 64.3% WR, +4.27c gross) and parameter sweep
# (1202 fills, 59.9% WR, +5.61c gross / +3.21c net after Kalshi taker fees both
# legs) per scripts/atm_reversion_sweep.py output. Bias-mode entries proven
# breakeven-after-fees in the same backtest â€” disabled here.
#
# Paper-only on first deployment. ATM_REVERSION_ENABLED=False AND
# ATM_REVERSION_PAPER_ONLY=True (belt + suspenders). Live promotion only after
# â‰¥7 days of paper validation matches the backtest expectancy under settlement
# truth.
ATM_REVERSION_ENABLED = False                  # 2026-05-02 reset: was True.
                                                # Paper-only sim was generating
                                                # 1000s of log lines per session
                                                # without informing the live
                                                # BB_PURE strategy. Re-enable
                                                # for research only.
ATM_REVERSION_PAPER_ONLY = True                # belt+suspenders: even if ENABLED flips True, live entries blocked until this is False
ATM_BIAS_ENABLED = False                       # bias-mode entries (proven flat-after-fees) â€” leave OFF
ATM_ONE_TRADE_PER_TICKER = True                # don't re-enter same window after exit
ATM_FIXED_SIZE_CONTRACTS = 5                   # paper-only sizing: small fixed lot. No Kelly. No cheap-leverage.
ATM_MUTEX_WITH_SR_FADE = True                  # if SR_FADE has fired/holding this window, ATM stands down

# Entry gate â€” sweep-validated production candidate (top of avg_net_c ranking)
ATM_MAX_STRIKE_DIST_PCT = 0.030                # only fire when |BTC âˆ’ strike| / strike â‰¤ 0.030 %
ATM_DISCOUNT_MAX_ENTRY_CENTS = 35              # side ask must be â‰¤ 35c
ATM_DISCOUNT_MIN_FAIR_CENTS = 47.0             # 50c default fair, allow 47+ as ATM
ATM_DISCOUNT_MIN_EDGE_CENTS = 8.0              # fair âˆ’ ask â‰¥ 8c

# Bias-mode knobs â€” kept for completeness; do nothing while ATM_BIAS_ENABLED=False
ATM_BIAS_MAX_ENTRY_CENTS = 62
ATM_BIAS_MIN_EDGE_CENTS = 1.0

# Exit logic â€” first match wins inside atm_reversion.evaluate_exit()
ATM_TARGET_CENTS = 49                          # absolute exit at 49c bid
ATM_PROFIT_TARGET_CENTS = 8                    # OR exit at entry + 8c bid
ATM_STOP_STRIKE_DIST_PCT = 0.080               # BTC escaped â†’ flatten
ATM_FORCE_EXIT_AGE_S = 840                     # 14 min hard cap (1-min buffer before settle)

# â”€â”€ ATM_ONLY live mode (Codex 2026-04-25 audit-grade live promotion plan) â”€â”€
# When LIVE_STRATEGY_MODE == "ATM_ONLY":
#   - Legacy live entries (SR_FADE, TA_FORCED) are blocked at _execute_signal_inner.
#   - Existing legacy positions can still be managed (no new entries, but exits
#     and residual reconciliation continue).
#   - ATM Reversion is the only tier that can place new live orders, and only
#     if ATM_LIVE_ENABLED is also True.
# ATM_LIVE_ENABLED defaults to False so the user does the explicit flip after
# the audit. ATM live execution path is fully separate from SR_FADE â€” no
# Kelly, no cheap-leverage, no DCA, no TP ladder, no orphan opposite-side
# residual sweep. Position cleanup uses Kalshi's authoritative position API
# and only flattens the held side that actually shows non-zero count.
LIVE_STRATEGY_MODE = "LEGACY"                # 2026-04-27 12:30 PT reverted from ATM_ONLY (per reversion-ladder decision record)
ATM_LIVE_ENABLED = False                       # disabled with revert; ATM stays paper-only until FVG/BB paper validates next-best primary
ATM_LIVE_MAX_CONTRACTS = 5                     # absolute hard cap per fill
ATM_LIVE_MAX_NOTIONAL_CENTS = 250              # $2.50 absolute notional cap
ATM_LIVE_REQUIRE_PAPER_SIGNAL = True           # must pass atm_reversion.evaluate first
ATM_LIVE_ENTRY_MODE = "maker_or_exact_ask_test"  # initial fill-verification mode
ATM_LIVE_EXIT_MODE = "same_as_paper"           # uses atm_reversion.evaluate_exit
ATM_LIVE_DISABLE_BIAS = True                   # belt+suspenders: no bias mode live
ATM_LIVE_EXIT_RETRY_COOLDOWN_S = 2.0           # throttle unfilled live exits; avoid order spam
# Market-sell fallback (Claude 2026-04-26 after the 10:21 PT 250-orders-in-2.5min storm).
# After this many failed limit-exit cycles on the same position, escalate
# to a market sell. Guarantees exit even when the bid keeps drifting away
# from our limit price. Pure safety net — does not change behavior on
# clean fills. Set ATM_LIVE_EXIT_MARKET_FALLBACK=False to disable
# escalation if the broker rejects market orders for any reason.
ATM_LIVE_EXIT_LIMIT_MAX_TRIES = 8              # ~16s of trying at 2s cooldown before market-sell
ATM_LIVE_EXIT_MARKET_FALLBACK = True

# ── ATM live graduation auto-eval (Claude 2026-04-26) ───────────────────────
# After ATM_LIVE_GRADUATION_FILL_COUNT closes, engine emits one log line with
# per-contract avg net + verdict so we can decide whether to keep ATM as
# primary, hold for more data, or revert to LEGACY. Backtest target is +3.21c.
# Verdicts:
#   avg_net_per_ct >= ATM_LIVE_GRADUATION_HEALTHY_C       → HEALTHY  (keep)
#   avg_net_per_ct >= ATM_LIVE_GRADUATION_MARGINAL_C      → MARGINAL (review)
#   avg_net_per_ct <  ATM_LIVE_GRADUATION_MARGINAL_C      → UNDERPERFORMING (revert)
ATM_LIVE_GRADUATION_FILL_COUNT = 25
ATM_LIVE_GRADUATION_HEALTHY_C = 2.5
ATM_LIVE_GRADUATION_MARGINAL_C = 1.0
ATM_LIVE_POSITION_RECONCILE_GRACE_S = 60.0     # Kalshi positions can lag fills; do not block live state on early flat reads
ATM_LIVE_SKIP_CROSSED_BOOK = False             # 2026-04-26 12:15 PT: flipped after 7 clean-book live fills + Patch 1 market-fallback safety net. Crossed states were ~89% of signals; this lets them trade live. Reconcile-block + market-fallback are the safety. Flip True to revert.
ATM_LIVE_MAX_CLEAN_SPREAD_CENTS = 8            # skip live if same-side bid/ask spread is too wide for fill-test quality

# ── ATM session-probe fallback (2026-04-26) ────────────────────────────────
# Goal: materially increase sessions traded without lowering the proven
# discount gate. Backtest sanity check rejected "buy cheap side every window"
# (-2c to -3c/contract). The only simple fallback with positive replay was
# early momentum-side continuation: at ~60s, if BTC has moved >= $10 from
# session open, buy the side matching the move, but only on a clean book.
ATM_SESSION_PROBE_ENABLED = True
ATM_SESSION_PROBE_START_AGE_S = 60.0
ATM_SESSION_PROBE_END_AGE_S = 180.0
ATM_SESSION_PROBE_MIN_BTC_MOVE_USD = 10.0
ATM_SESSION_PROBE_MAX_ENTRY_CENTS = 55
ATM_SESSION_PROBE_MAX_SPREAD_CENTS = 8

# ─── MRC — Contract Momentum/Reversion/Covariance (2026-05-04) ──────
# Per-fill analyzer that watches the contract's own mid stream as a
# probability evolution and produces a TP multiplier + force-exit flag.
# Additive: any internal failure neutralizes outputs but never crashes
# the engine. See contract_momentum.py + RESEARCH_MOMENTUM_REVERSION_
# COVARIANCE.md. Disable by flipping MRC_ENABLED=False.
MRC_ENABLED                  = False  # 2026-05-04 PT 16:30: KILLED.
                                      # MRC FORCE-EXIT loop with max() truth
                                      # gate (not min) opened 143 NO contracts
                                      # via Kalshi sell-to-open semantics →
                                      # full account drain. Park until the
                                      # _safe_sell_count helper lands.
MRC_TP_MODULATION            = False  # 2026-05-04 PT 16:30: KILLED.
MRC_FORCE_EXIT               = False  # 2026-05-04 PT 16:30: KILLED — root
                                      # cause of catastrophic loss.
MRC_MIN_OBSERVATIONS         = 30     # warmup threshold (observations on the analyzer's buffer)
MRC_TP_PREMIUM_FLOOR_C       = 2      # absolute floor on scaled premium (entry + N cents)
MRC_FORCE_EXIT_MIN_PROFIT_C  = 2      # only force-exit when bid >= entry + N (profit gate)

# ─── Exit Management Enhancements (2026-05-04) ──────────────────────
# Five additive enhancements to BB_PURE's protective layer that consume
# already-running data feeds (S/R levels, wall consumption, Kalshi lag,
# tape exit pressure, MRC path signature) for sharper TP/SL decisions.
# Each is gated by its own flag, wrapped in try/except, and falls back
# silently to baseline behavior if the data path is cold.
SR_TP_CAP_ENABLED                = False  # 2026-05-04 PT 16:30: KILLED.
                                          # Untested live, parked with the
                                          # other dispatch flags.
SR_TP_CAP_MIN_STRENGTH           = 0.4   # min level_strength to consult
SR_TP_CAP_MIN_SAMPLES            = 30    # min samples_seen on the SR state before consulting
WALL_CONSUMPTION_EXIT_ENABLED    = False  # 2026-05-04 PT 16:30: KILLED.
WALL_CONSUMPTION_LOOKBACK_S      = 5.0   # lookback window for consumption rate
KALSHI_LAG_TP_ENABLED            = False  # 2026-05-04 PT 16:30: KILLED.
KALSHI_LAG_TP_THRESHOLD          = 0.3   # |kalshi_lag| above which we adjust
KALSHI_LAG_TP_WIDEN_MULT         = 1.15  # tp_target premium × this when lag favors hold
KALSHI_LAG_TP_TIGHTEN_MULT       = 0.85  # tp_target premium × this when lag closing
MRC_PATH_SIG_PROTECTIVE_ESC      = False  # 2026-05-04 PT 16:30: KILLED with MRC.

# ─── Three-Fix Bundle (2026-05-04) ──────────────────────────────────
# FIX 1: FLAT-CONFIRMED 5-second cache-lag recheck. After FLAT-CONFIRMED
# fires and clears engine state, schedule a one-shot async recheck of
# Kalshi positions. If the position re-appears (cache lag), RE-ADOPT
# instead of leaving it uncovered. Also triggers in SYNC MANUAL-DETECTED
# (over-fill) when in BB_PURE-only mode.
FLAT_CONFIRM_RECHECK_ENABLED     = False  # 2026-05-04 PT 16:30: KILLED.
                                          # The RE-ADOPT branch off this
                                          # path turned cache-lag glitches
                                          # into adopted phantom positions.
                                          # Restore "leave alone" until the
                                          # exposure-cap circuit-breaker is
                                          # in place.
FLAT_CONFIRM_RECHECK_DELAY_S     = 5.0
# FIX 2: BB_PURE BTC velocity asymmetry veto at entry. Blocks NO entries
# when BTC is moving up fast and YES entries when BTC is moving down
# fast — the dollar-velocity signal directly contradicts the entry
# thesis. Threshold is in $/s (tick_velocity is signed 30s BTC change).
BB_PURE_VELOCITY_VETO_ENABLED    = False  # 2026-05-04 PT 16:30: KILLED for
                                          # cleanliness with rest of dispatch
                                          # bundle. Re-enable individually
                                          # after paper validation.
BB_PURE_VELOCITY_VETO_THRESHOLD  = 2.0
# FIX 3: MRC warmup reduction (90s → 60s effective) is in
# contract_momentum.py (MIN_OBS = 20). MRC analyzer attachment in
# RECLAIM/RECONCILE paths gated below.
MRC_RECLAIM_ATTACH_ENABLED       = False  # 2026-05-04 PT 16:30: KILLED with MRC.

# ─── Dispatch flags that defaulted True in code ─────────────────────
# These were not declared in user_config.py originally — they got their
# default True from _uc("FLAG", True) calls in polymarket_copy_engine.py.
# Explicitly setting them False here makes the kill switch authoritative
# regardless of how _uc resolves missing names.
TP_TAKER_CONVERT_ENABLED         = True   # 2026-05-05 RE-ENABLED.
                                          # _safe_sell_count helper has
                                          # shipped (MIN-TRUTH gate inside
                                          # _place_capped_side_sell). Logic
                                          # extracted to pure helper
                                          # protective_math.decide_tp_placement
                                          # with 11/11 unit tests covering
                                          # boundaries (bid<tp / bid==tp /
                                          # bid>tp / SL-state / kill-switch /
                                          # zero/negative inputs). Trigger:
                                          # today's portfolio Trade 3 fired
                                          # 8 "post only cross" rejections
                                          # in 12s before the bid eased
                                          # enough to land a maker — taker-
                                          # convert eliminates that burst
                                          # entirely while still using post_
                                          # only as the default path.
PRE_EXPIRY_TAKER_ENABLED         = False  # 2026-05-04 PT 16:30: KILLED.
                                          # Re-enable individually after the
                                          # MIN-truth gate is in place.

# ─── Per-ticker exposure cap (2026-05-04 post-catastrophe) ─────────
# Hard upper bound on how much of the bankroll any single ticker can tie
# up across both YES and NO sides. Today's loss had 156 contracts × 44c
# = $68 on a $70 bankroll = 97% concentration on one expiry. The cap
# refuses any state change (entry, DCA, RECONCILE BACKFILL) that would
# push exposure above this fraction.
#
# 0.25 chosen so a 5%-Kelly entry can DCA up to 5x with cushion before
# tripping. Tighten to 0.15 for bankrolls > $200 if desired.
MAX_TICKER_EXPOSURE_FRAC         = 0.25
EXPOSURE_CAP_ENABLED             = True   # master switch for the cap
MRC_WARMUP_LOG_ENABLED           = True

# ─── FVG Tier Sizing (2026-05-04 post-OOS-validation) ──────────────────
# The OOS-validated 70/30 split (scripts/backtest_oos_level3.py) showed
# Tier 1 fill rate of 97.4% live (99% backtest), Tier 2 92.2% (93%
# backtest). See to-do/BACKTEST_2026_05_04.md for the full validation.
#
# THESE KNOBS ARE READ BY ``_fvg_tiering.py`` BUT NOT YET WIRED INTO THE
# LIVE ENGINE PATH (as of 2026-05-04 evening). The FVG paper sim
# continues to shadow-log via ``_paper_fvg_tick``. Live wiring + paper-
# mode validation is the next-session task.
#
# Tier definitions:
#   T1 (35% bankroll, TP+20c): late_300+ AND aligned AND |btc_dist| >= 0.10%
#   T2 (25% bankroll, TP+15c): late_300+ AND aligned (NOT in T1)
#   T3 (18% bankroll, TP+12c): late_300+ alone
#   T4 (10% bankroll, TP+12c): 180s <= age < 300s
#
# Refuse predicates (Tier 0):
#   - session_age_s < 180s
#   - counter-trend AND 30 <= entry_c <= 49
#   - |btc_5m_move| < $20 AND counter-trend
PAPER_FVG_ENABLED                = True   # paper-shadow logging (already
                                          # was shadow-logging before; this
                                          # makes it explicit. Live wiring
                                          # comes next session.)
PAPER_FVG_LIVE_MODE              = False  # 2026-05-05 06:27 PT EMERGENCY DISABLE.
                                          # Loss: $35.53 -> $28.80 (-$6.73)
                                          # via SYNC RECLAIM bug. FVG live
                                          # fills landed in _open_position=None
                                          # because FVG state is in
                                          # self._paper_fvg, not _open_position.
                                          # SYNC RECLAIM (BB_PURE-era path)
                                          # adopted the orphan, placed
                                          # BB_PURE tiered TP at 16c instead
                                          # of FVG tier_tp_price, position
                                          # bled to bid=8c. Re-enable only
                                          # after FVG live integrates with
                                          # _open_position machinery (so
                                          # SYNC RECLAIM stays out of FVG
                                          # positions). KEEP FALSE.
# PAPER_FVG_LIVE_MODE prior comment block (preserved for context):
# 2026-05-05 LIVE FLIP. Validated:
                                          # 23/23 tier classification tests
                                          # pass; 10/10 live-wiring tests
                                          # pass (test_fvg_live_wiring.py);
                                          # 6+ hrs paper-sim showed
                                          # T1/T2 fills matching OOS-validated
                                          # 97.4%/92.2%. Engine routes FVG
                                          # signals through real Kalshi
                                          # orders via _paper_fvg_live_entry
                                          # with full safety stack
                                          # (ticker-lock, MIN-TRUTH gate,
                                          # OVERSELL-GUARD, residual
                                          # reconciler, daily-loss halt).

# ── DIRECTION-FOLLOWING STRATEGY (2026-05-05) ──────────────────────────
# Buy whichever side BTC is moving when |dist|>=0.10% AND 5-min momentum
# agrees. Hold to settlement. OOS-validated: 84-132 trades, 69-90% win
# rate, +$2-4 mean P&L per trade, $50 BR -> $323 corpus.
# See direction_strategy.py + scripts/backtest_direction.py.
#
# This is the PRIMARY live strategy as of 2026-05-05, replacing the
# FVG-tier-aware path (PAPER_FVG_LIVE_MODE) which had structural bugs
# (FLAT-CONFIRMED missing, SYNC RECLAIM orphan adoption) and inferior
# economics (26% win rate vs 69-90% here).
DIRECTION_STRATEGY_ENABLED       = True    # 2026-05-05 LIVE FLIP. Validated:
                                           # 25/25 unit tests pass,
                                           # 615/617 full suite pass (2 pre-
                                           # existing fails unrelated).
                                           # Engine imports clean. Replaces
                                           # the broken FVG-tier path.
                                           # Fires real Kalshi IOC taker
                                           # orders when |dist|>=0.10% AND
                                           # 5min momentum agrees, holds to
                                           # settlement.
DIRECTION_DIST_THRESHOLD_PCT     = 0.0010  # 0.10% from strike (sweet spot
                                           # per OOS: 90.5% win rate)
DIRECTION_MOMENTUM_THRESHOLD_DOLLARS = 10  # $10 over 5 min minimum
DIRECTION_MAX_OFFSET_S           = 600     # entry only before minute 10
DIRECTION_CONTRACTS              = 5       # 2026-05-05 PT: REVERTED from 20.
                                           # The 20ct bump made one bad
                                           # trade (DIRECTION YES 20x @
                                           # 36c on -26MAY060030-30
                                           # settled NO at 21:29 PT) cost
                                           # -$7.53, 4× what 5ct would
                                           # have cost. Replaced static
                                           # bump with conviction-based
                                           # multiplier in
                                           # direction_strategy.conviction_multiplier
                                           # (0.7-2.0x scaled by dist from
                                           # strike + btc_5m momentum).
                                           # Base stays at 5; multiplier
                                           # scales up to 10ct on screaming
                                           # signals, down to 5ct floor.
DIRECTION_MIN_BANKROLL_X_COST    = 1.5     # require 1.5×cost in BAL
DIRECTION_DAILY_LOSS_HALT_FRAC   = 0.20    # halt at -20% from day-start
DIRECTION_POST_FAIL_COOLDOWN_S   = 5.0     # backoff after place_order fail
# 2026-05-06 evening (round 2): IOC taker with slippage tolerance.
# Maker mode (commit 9e4aef0) had structural fill problem on momentum
# strategy: maker bid only fills when market reverses = adverse selection.
# 0/1 fills in 25 min. Switched back to IOC, but at ask + slippage so
# we walk through the next depth tier when the ask is thin. 1c on a $0.40
# entry = 2.5% cost, well below the 5c TP target.
DIRECTION_TAKER_SLIPPAGE_C       = 2       # 2026-05-06 19:55: bumped 1->2.
                                           # 35 fires at slip=1c produced
                                           # 0 fills (ask depth at ask+1c
                                           # consistently below 5-10ct
                                           # requested size). Bumping to
                                           # walk through the next tier.
                                           # 2c on a 70c entry = 2.9% cost,
                                           # still below the 5c TP target.
# Legacy maker-mode knobs retained for rollback (currently inactive —
# fire path uses IOC at ask+slippage, not post_only at ask-offset).
# The pending-order state machine is dead code while in IOC mode.
DIRECTION_MAKER_TIMEOUT_S        = 60.0    # only used if maker mode resurrected
DIRECTION_MAKER_OFFSET_C         = 1       # only used if maker mode resurrected

# Tier sizing fractions (Level 3 — Half-Kelly, OOS-validated)
FVG_TIER_FRAC_T1                 = 0.35
FVG_TIER_FRAC_T2                 = 0.25
FVG_TIER_FRAC_T3                 = 0.18
FVG_TIER_FRAC_T4                 = 0.10
# Tier TP offsets (cents above entry)
FVG_TIER_TP_T1                   = 20
FVG_TIER_TP_T2                   = 15
FVG_TIER_TP_T3                   = 12
FVG_TIER_TP_T4                   = 12
# SL is uniform across tiers (8c, matches PROTECTIVE_SL_OFFSET_C)
FVG_TIER_SL_OFFSET               = 8
# Daily loss circuit breaker — halt FVG live for the day if this
# fraction of starting-day bankroll is lost.
FVG_DAILY_LOSS_HALT_FRAC         = 0.20
# When PAPER_FVG_LIVE_MODE flips to True, also raise the exposure cap
# to allow Tier 1's 35% slot. This knob lets us flip both flags
# atomically.
FVG_LIVE_MAX_TICKER_EXPOSURE_FRAC = 0.40   # only used when LIVE_MODE=True
