# settings/user.py — User-adjustable trading parameters
#
# Every value here overrides the matching constant in config.py.
# You only need to uncomment and change the settings you want to adjust.
# Leave a setting commented out to keep the default from config.py.
#
# After editing this file, restart the engine:
#   cd C:\Trading\btc-bias-engine
#   .\scripts\reinstall_service.ps1


# ── Capital & Risk ────────────────────────────────────────────────────────────

# Your current account balance. The engine syncs this from Kalshi at startup,
# but you can set a manual override here if the sync is unavailable.
# STARTING_EQUITY: float = 24.13       # dollars

# Maximum dollar loss allowed in a single day before the engine halts.
# DAILY_LOSS_LIMIT: float = 500.0      # dollars

# Maximum percent of equity to risk on a single main-engine trade at 100%
# confidence. Actual risk scales linearly with confidence (50% conf → 50% of this).
# Example: 2.0 means risk up to 2% of equity = $0.48 on a $24 account.
# MAX_PCT_EQUITY: float = 2.0          # percent

# Hard dollar cap per trade regardless of MAX_PCT_EQUITY or equity size.
# Prevents a large equity balance from making single bets too big.
# STAKE_CAP: float = 100.0             # dollars


# ── HFT Scalping ─────────────────────────────────────────────────────────────

# Maximum number of scalp fills allowed per 15-minute window.
# Lower = more conservative. Raise if you want more research data.
# HFT_SCALP_MAX_PER_WINDOW: int = 6

# Take-profit target: exit when the position has gained this many cents.
# HFT_SCALP_TARGET_CENTS: float = 15.0

# Stop-loss: exit when the position has lost this many cents.
# HFT_SCALP_STOP_CENTS: float = 6.0

# Entry price band. Only enter contracts priced between these values.
# Contracts below MIN are too near-certain to have edge.
# Contracts above MAX have collapsing win rate (tested: 0% above 60¢).
# HFT_SCALP_MIN_ENTRY_CENTS: int = 25
# HFT_SCALP_MAX_ENTRY_CENTS: int = 50

# Trailing stop: activates once profit exceeds ACTIVATE, then trails
# DISTANCE cents below the position's high-water mark.
# HFT_TRAIL_ACTIVATE_CENTS: float = 10.0
# HFT_TRAIL_DISTANCE_CENTS: float = 6.0


# ── Flip Behavior ─────────────────────────────────────────────────────────────

# After a signal-reversal exit, immediately attempt entry in the opposite
# direction (the "flip"). Set False to disable and just exit cleanly.
# HFT_FLIP_ENABLED: bool = True

# Seconds to wait after exit before trying the flip entry.
# A brief pause prevents chasing a single-tick noise spike.
# HFT_FLIP_COOLDOWN_SECONDS: float = 1.5


# ── Trading Hours ─────────────────────────────────────────────────────────────

# UTC hours to block all trading. Empty set = trade 24/7.
# Add hours back if post-analysis shows consistent losses in specific windows.
# Example: frozenset({14, 19, 20, 21}) to block known bad hours only.
# BAD_UTC_HOURS: frozenset = frozenset()


# ── Dynamic Position Sizing ───────────────────────────────────────────────────

# Sizing tiers: (min_settled_trades, min_win_rate, max_pct_equity).
# Engine evaluates top-down; first match wins. Falls back to DYNAMIC_SIZING_FLOOR_PCT.
# Raise thresholds if you want to be more conservative before scaling up.
# DYNAMIC_SIZING_TIERS: list = [
#     (30, 0.65, 8.0),   # 30+ trades, WR >= 65%  -> 8% per trade
#     (30, 0.58, 5.0),   # 30+ trades, WR >= 58%  -> 5% per trade
#     (20, 0.55, 3.5),   # 20+ trades, WR >= 55%  -> 3.5% per trade
# ]
# DYNAMIC_SIZING_FLOOR_PCT: float = 2.0   # fallback when no tier matches


# ── Main Engine Entry Filters ─────────────────────────────────────────────────

# Minimum raw signal confidence (0–100) before the engine evaluates a trade.
# Raise to filter out weaker signals; lower to see more entries in the log.
# MIN_RAW_SIGNAL_CONFIDENCE: float = 15.0

# Minimum minutes remaining in the 15m window to enter a new main-engine trade.
# Raise to avoid late-window entries with little time to be right.
# MIN_MINUTES_REMAINING: float = 4.0
