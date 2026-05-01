# =============================================================================
# user_config.py — Your personal trading configuration
# =============================================================================
# Copy this file and edit the values below to match your setup.
# The engine loads this at startup and applies your overrides.
#
# QUICK START:
#   1. Copy:    cp user_config.example.py user_config.py
#   2. Edit:    Fill in your values below
#   3. Run:     python main.py
# =============================================================================

# ── Installation Path ───────────────────────────────────────────────────────
# Where you cloned/installed the engine. Data files (DBs, logs) go under
# this path in a "data/" subfolder.
# Windows:  r"C:\Trading\btc-bias-engine"
# Linux:    "/home/you/btc-bias-engine"
# Mac:      "/Users/you/btc-bias-engine"
ENGINE_DIR = r"C:\Trading\btc-bias-engine"


# ── Capital & Risk ──────────────────────────────────────────────────────────
# Balance-based sizing: risk a % of your available balance per trade.
# Example: $50 balance × 20% = $10 risk → at 50c ask = 20 contracts
SIZING_BALANCE_FRACTION = 0.30      # 30% — tighter TPs mean less risk per contract, can size bigger
SIZING_MAX_DOLLARS = 50.00          # Cap at $50 per entry

# Hard floor: engine stops trading if your Kalshi balance drops below this.
MIN_BALANCE_TO_TRADE = 2.00         # dollars

# Maximum dollar loss per day before the engine auto-halts.
DAILY_LOSS_LIMIT = 35.00            # Raised — single trades risk $5-10, old $15 limit tripped on one ghost adoption

# ── Take Profit ────────────────────────────────────────────────────────────
# After each fill, a limit sell is placed at entry + N cents.
# Range 3-6c. Higher = more profit per win, lower = fills more often.
TAKE_PROFIT_CENTS = 5               # Sell at entry + 5c (e.g. buy 50c → sell 55c)


# ── Entry Price Bands ───────────────────────────────────────────────────────
# Binary contracts on Kalshi trade from $0.01 to $0.99.
# These set the min/max price (in cents) the engine will pay.
#
# Backtested sweet spot: 40-55 cents = 83% win rate (16-trade sample).
# Below 15c = too speculative.  Above 55c = overpaying, 33% WR.
MIN_ENTRY_CENTS = 40                # 40c floor — below this is too speculative
MIN_ENTRY_CENTS_NO = 40             # Same for NO.
MAX_ENTRY_CENTS = 55                # 42-55c proven range. 50c was too restrictive — cut trade count in half.


# ── Signal Thresholds ───────────────────────────────────────────────────────
# Minimum divergence between Polymarket smart money and Kalshi pricing.
# Higher = fewer but higher-quality trades.  Lower = more trades, noisier.
MIN_DIVERGENCE = 0.01               # Near-zero — trust wallet signal even when Kalshi agrees

# Minimum smart wallets trading in the same direction before the engine acts.
MIN_SMART_WALLETS = 1               # Incremental: enter on 1st wallet, scale in as more confirm

# Minimum WR-weighted conviction on one side (0.0 - 1.0)
MIN_FLOW_CONVICTION = 0.50          # 50% — lower barrier, let wallet quality filter handle conviction

# Stop loss: exit if position drops this many cents from high-water mark.
# Uses a TRAILING stop — tracks highest bid since entry, exits on drop.
# Wider = more room to recover.  Tighter = cuts losses faster.
STOP_LOSS_CENTS = 8                 # DATA: -8c stop optimal. Turns -$37 lifetime into +$127.
STOP_LOSS_CENTS_PRIMARY_WIDE = 8    # Same for all tiers.
STOP_LOSS_CENTS_HIGH_ENTRY = 5      # Tight stop for entries >83c. Max payout is only 17c — can't afford 8c drawdown.
HIGH_ENTRY_STOP_THRESHOLD = 83      # Entry price (cents) above which tight stop applies.
STOP_GRACE_PERIOD_S = 15            # seconds — brief grace for order to settle


# ── Mimic Tier (optional) ──────────────────────────────────────────────────
# The mimic tier takes small positions following single high-WR wallets
# even when the full signal doesn't fire.  Set False to disable.
# DATA: MIMIC_SMART_FLOW is -$13.07 total (-$0.204/trade) on 64 trades — worst risk-adjusted strategy.
MIMIC_ENABLED = False

# Minimum win rate for a wallet to be mimicked.
MIMIC_MIN_WALLET_WR = 0.70         # 70% — only follow elite wallets


# ── Trading Schedule ────────────────────────────────────────────────────────
# UTC hours to block new signal entries.  Empty = trade 24/7.
# Position management always runs — only new entries are suppressed.
# DATA: 08-14 UTC (European session) = -$28.99 across 291 trades (41.6% WR).
#   Best hours: 17 UTC (+$15.29), 02 UTC (+$6.45), 15 UTC (+$11.70)
DIVERGENCE_LADDER_ENABLED = True     # Spread entries across 3 price levels when momentum opposes direction
BLOCKED_HOURS = set()                      # Trade 24/7


# ── TA Forced Tier (fallback when no Poly/Kalshi signal) ──────────────────
# When no smart-wallet flow or tape signal fires, the engine falls back to
# a technical analysis model (EMA spread, RSI, volume, cycle return, candle
# pressure) computed on 1m BTC candles from Binance.  This guarantees at
# least one trade per 15m window.
#
# The engine can INVERT the TA direction when broader signals disagree.
# If 2+ of {Poly flow, Kalshi tape, whale monitor} oppose the TA read,
# the trade is flipped and downsized.  If 1 opposes, it's just downsized.
TA_FORCED_ENABLED = False              # Legacy: replaced by PRICE_ACTION
PRICE_ACTION_ENABLED = True            # React to contract price action + trade flow

# When TA says one thing but the market says another, trust the market.
TA_INVERSION_ENABLED = True


# ── Multi-Timeframe Confluence (MTF) ────────────────────────────────────────
# Price-action confluence filter across 1m/5m/15m/1h timeframes.
# Start in SHADOW MODE — the score is logged but trades are never blocked.
# Validate via the mtf_score column in kalshi_trades for ~200 trades before
# setting MTF_SHADOW_MODE = False.
MTF_ENABLED = True              # True: start price feed + score every signal
MTF_SHADOW_MODE = False         # Live: inverted filter — opposing = contrarian boost, aligning = shrink
MTF_MIN_CONFLUENCE = 0.3        # Minimum |score| to consider aligned (live mode only)
MTF_SIZE_MULTIPLIER_HIGH = 1.5  # Size boost when score >= 0.6 (live mode only)
MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h"]   # Timeframes to track

# Binance WebSocket symbol for price feed
PRICE_FEED_SYMBOL = "btcusdt"

# WebSocket settings
PRICE_FEED_WS_TIMEOUT_S = 30.0  # Reconnect if no message for this many seconds


# ── Paper Trading (safe simulation mode — no real orders) ───────────────────
# Set PAPER_TRADING = True to run the engine with no real Kalshi orders.
# All fills are simulated; P&L is tracked in data/signals.db (paper_trades table).
# CRITICAL: While True, the engine will NEVER place a real order. Always verify
# this is False before going live.
PAPER_TRADING = False               # False = live trading. True = paper simulation only.
PAPER_STARTING_BALANCE = 100.0      # Virtual starting balance in dollars
PAPER_SLIPPAGE_CENTS = 1            # Cents of slippage added to simulated fills


# ── Advanced (usually don't need to change) ─────────────────────────────────
# Max trades per 15-minute contract window. 1 = safest (no double-entry).
MAX_TRADES_PER_WINDOW = 6           # Entry + flip-invert (sell + buy opposite + TP) needs headroom.

# Seconds between trades (cooldown).
TRADE_COOLDOWN_SECONDS = 10         # 10s cooldown — prevents entry spam

# Minimum/maximum minutes remaining on a Kalshi contract to enter.
MIN_MINUTES_REMAINING = 2.0         # 4min minimum. No stop losses = no risk of instant stop on late entries.
MAX_MINUTES_REMAINING = 15.0          # DATA: 20% of wallet trades arrive in first 30s. No reason to wait.

# ── Sell Ladder ────────────────────────────────────────────────────────────
# When True: places tiered sell orders at 3 levels (safe/standard/runner).
# When False: single limit sell at 75c (all-or-nothing).
SELL_LADDER_ENABLED = True

# When True: after a full sell ladder fill with 5+ min left, enters the
# opposite side at 15% sizing to ride the reversion. Conservative caps.
FLIP_REENTRY_ENABLED = True
