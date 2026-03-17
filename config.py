# config.py — All constants for the BTC Multi-Timeframe Bias Engine

# Indicator lengths
FAST_EMA_LEN: int = 5
SLOW_EMA_LEN: int = 13
RSI_LEN: int = 7
VOL_AVG_LEN: int = 20
SCORE_SMOOTH_LEN: int = 2

# Scoring weights
CYCLE_RETURN_W: float = 120.0
EMA_SPREAD_W: float = 200.0
RSI_BIAS_W: float = 25.0
CANDLE_PRESSURE_W: float = 15.0
REL_VOL_W: float = 10.0

# Thresholds
ENTRY_THRESHOLD: float = 10.0       # Minimum confidence to emit signal
DECISION_START_BAR: int = 3         # Earliest bar in window to begin scoring

# Contract window
CYCLE_TIMEFRAME: str = "15m"        # Default contract window timeframe

# Contract parameters (Kalshi)
PAYOUT_ON_WIN: float = 1.0          # Win multiple
LOSS_ON_LOSE: float = 1.0           # Loss multiple
MAX_PCT_EQUITY: float = 2.0         # Max % of equity at 100% confidence
STAKE_CAP: float = 100.0            # Hard dollar cap per trade

# Timeframe weights (Fourier framing)
TF_WEIGHTS: dict[str, float] = {
    "1m":  0.10,
    "3m":  0.15,
    "5m":  0.20,
    "15m": 0.30,
    "1h":  0.25,
}

# After-hours weights (UTC 21–08): reduce 1h influence.
# The 1h TF updates only once per hour, creating a persistent stale directional
# bias during low-liquidity sessions. Redistributing its weight to 5m/15m lets
# short-term momentum be more decisive when the 1h may be lagging reality.
AFTER_HRS_TF_WEIGHTS: dict[str, float] = {
    "1m":  0.10,
    "3m":  0.15,
    "5m":  0.28,   # +0.08
    "15m": 0.37,   # +0.07
    "1h":  0.10,   # -0.15
}

# Timeframe durations in minutes
TF_MINUTES: dict[str, int] = {
    "1m":  1,
    "3m":  3,
    "5m":  5,
    "15m": 15,
    "1h":  60,
}

# Confidence buckets
BUCKET_THRESHOLDS: list[int] = [25, 50, 75]  # Upper bounds for buckets 0,1,2; bucket 3 is >= 75

# Binance.US WebSocket (binance.com is geo-blocked for US users)
BINANCE_WS_URL: str = "wss://stream.binance.us:9443/ws/btcusdt@kline_1m"

# SQLite paths
SIGNALS_DB_PATH: str = "data/signals.db"
TRADES_DB_PATH: str = "data/trades.db"

# Risk management
DAILY_LOSS_LIMIT: float = 500.0     # Hard stop for the day in dollars
STARTING_EQUITY: float = 24.13     # Starting equity — sync'd from Kalshi 2026-03-13; overridden at runtime by balance sync

# Signal quality floor — minimum raw signal confidence before WR floor applies.
# Prevents noise trades where the regime WR (63-66%) does all the work and the
# actual signal is near-zero (e.g. the 0.10% confidence CALL that still traded).
MIN_RAW_SIGNAL_CONFIDENCE: float = 15.0

# Contract execution filters
MIN_MINUTES_REMAINING: float = 4.0  # Don't enter if < 4 min left in window
MAX_SPREAD_CENTS: float = 20.0      # Skip contracts with bid-ask spread > 20¢ (illiquid)
MIN_CONTRACT_VOLUME: int = 25       # Skip contracts with fewer than 25 total trades (illiquid)

# Strategy Index
STRATEGY_INDEX_ENABLED: bool = True
BLOCK_LOW_SAMPLE_CELLS: bool = True
LOW_SAMPLE_THRESHOLD: int = 30

# Position scale bounds
MIN_POSITION_SCALE: float = 0.25
MAX_POSITION_SCALE: float = 2.0

# Execution quality gates
MAX_QUOTE_AGE_MS: int = 2000   # Reject if Kalshi quote is > 2s stale at submit time

# HFT Engine — fast order-book-driven loop running in parallel with candle engine
HFT_ENABLED: bool = True
HFT_POLL_INTERVAL: float = 4.0           # Seconds between order book polls
HFT_MIN_MINUTES_REMAINING: float = 2.5  # Don't open new positions this close to expiry

# Arbitrage (YES + NO of same contract)
HFT_ARB_MIN_EDGE_CENTS: float = 2.0     # Min guaranteed profit to trigger arb
HFT_ARB_MAX_CONTRACTS: int = 5          # Max contracts per arb opportunity

# Scalping — edge uses calibrated expected_wr (not raw signal.confidence)
# gross_edge = expected_wr_cents - market_ask_cents
# net_edge   = gross_edge - spread_cents  (conservative full-spread friction proxy)
HFT_SCALP_GROSS_EDGE_FLOOR: float = 5.0   # Min gross edge to consider entry at all
HFT_SCALP_NET_EDGE_FLOOR: float = 2.0     # Min net edge after friction — entry gate
HFT_SCALP_TARGET_CENTS: float = 8.0       # Take-profit: exit when up this many cents
HFT_SCALP_STOP_CENTS: float = 8.0         # Stop-loss: exit when down this many cents
# When exiting a scalp on take-profit, start the limit sell above the current bid
# to attempt a better fill before the normal step-down retry loop runs.
HFT_TP_LIMIT_PREMIUM_CENTS: int = 4       # Extra cents above bid when exiting on take_profit
HFT_SCALP_ENTRY_TIMEOUT: float = 30.0     # Cancel unfilled entry limit after N seconds
HFT_SCALP_MAX_PER_WINDOW: int = 6         # Max scalp fills per 15m window
HFT_SCALP_MIN_ENTRY_CENTS: int = 25       # Don't enter when contract is already >75% priced one way
HFT_SCALP_MAX_ENTRY_CENTS: int = 75       # Mirror — avoid near-certain contracts on either side
# Regime gate for HFT scalping.
# False = allow all regimes — the edge floor (gross_edge >= 5¢) is the real quality gate.
# The expected_wr already incorporates regime via the WR table; blocking by regime is
# redundant and kills 88% of evaluations (VOLATILE + UNKNOWN = 2000+ missed opportunities).
HFT_REQUIRE_FAVORABLE_REGIME: bool = False

# Coordination between HFT engine and main engine
HFT_ALLOW_OPPOSE_MAIN: bool = False       # Allow HFT to take opposite side of active main position
HFT_MAX_ADDITIVE_CONTRACTS: int = 2       # Max total contracts in same direction (main + HFT combined)

# Microstructure controls — per research paper recommendations
HFT_MAX_BOOK_AGE_MS: int = 1500           # Block orders if REST poll is older than this
HFT_MAX_REPRICES_PER_ORDER: int = 3       # Cancel entry without retry after this many price steps
HFT_TIGHTEN_INVENTORY_MINUTES: float = 5.0 # Require elevated net edge below this minutes-to-expiry
HFT_MIN_DEPTH_CONTRACTS: int = 10         # Min contracts within 2¢ on opposite side before entry
HFT_USE_IMBALANCE_GATE: bool = True       # Block entries when top-of-book imbalance strongly opposes
HFT_IMBALANCE_OPPOSE_THRESHOLD: float = -0.60  # Block YES entry if imbalance < this (−0.60 = 80/20 NO dominance)
HFT_POST_FILL_DRIFT_SECONDS: list = [1, 3, 10] # Capture book price at these offsets after fill confirmation

# Dynamic position sizing
# Automatically scales MAX_PCT_EQUITY up as the rolling win rate is confirmed.
# Tiers are evaluated top-down; first match wins.
# Each tier is (min_settled_trades, min_win_rate, max_pct_equity_pct).
DYNAMIC_SIZING_ENABLED: bool = True
DYNAMIC_SIZING_LOOKBACK: int = 20          # Number of most-recent settled trades to evaluate
DYNAMIC_SIZING_FLOOR_PCT: float = 2.0      # Fallback when no tier is matched
DYNAMIC_SIZING_TIERS: list = [
    (30, 0.65, 8.0),   # 30+ settled trades, WR ≥ 65%  → 8%
    (30, 0.58, 5.0),   # 30+ settled trades, WR ≥ 58%  → 5%
    (20, 0.55, 3.5),   # 20+ settled trades, WR ≥ 55%  → 3.5%
]

# Early exit / arbitration
# When the consensus signal strongly reverses against an open position, sell it
# early at the current bid rather than holding to binary expiry.
EARLY_EXIT_TF_THRESHOLD: int = 4      # TFs opposing position needed to trigger reversal exit
EARLY_EXIT_MIN_MINUTES: float = 3.0  # Don't exit if < 3 min to expiry — just hold
EARLY_EXIT_MIN_SAVINGS_CENTS: float = 5.0  # Skip reversal exit if it saves < 5¢ (not worth friction)
TAKE_PROFIT_CENTS: float = 12.0      # Lock in gains when contract moves ≥12¢ in our favor

# Take-profit holding — post a resting limit sell above the current ask on TP trigger.
# When a position first hits TAKE_PROFIT_CENTS profit, a limit sell is posted at
# ask + TP_LIMIT_PREMIUM_CENTS instead of an immediate force-sell. The hold is
# unwound when:
#   1. The limit sell fills (best outcome — captured higher price)
#   2. Profit reaches TP_HARD_CEILING_CENTS (lock in, don't hold further)
#   3. TP_HOLD_TIMEOUT_MINUTES elapses without a fill (cancel limit, force-sell at ask)
#   4. A reversal signal fires (cancel limit, reversal exit logic takes over)
TP_USE_LIMIT_HOLD: bool = True
TP_LIMIT_PREMIUM_CENTS: float = 6.0       # Post limit this far above ask at TP trigger
TP_HARD_CEILING_CENTS: float = 30.0       # Force-sell immediately when profit reaches this
TP_HOLD_TIMEOUT_MINUTES: float = 3.0      # Cancel limit and force-sell after this long

ENABLE_REENTRY: bool = True           # After a reversal exit, re-enter the opposite side
REENTRY_MIN_MINUTES: float = 5.0     # Min minutes remaining for a re-entry to be worthwhile

# Strong reversal / aggressive exit
# When ALL TFs unanimously oppose the position (massive correction), skip the 2s
# fill wait and sell immediately at bid - STRONG_REVERSAL_AGGRESSION_CENTS to
# guarantee execution rather than risk the price gapping further against us.
STRONG_REVERSAL_TF_THRESHOLD: int = 5        # All TFs reversed — "massive correction"
STRONG_REVERSAL_AGGRESSION_CENTS: int = 3    # Sell this many cents below bid for certain fill

# ── Polymarket cross-venue signal fusion ──────────────────────────────────────
# Connects to Polymarket's 5-minute BTC Up/Down markets as an external signal
# layer. When Polymarket reprices faster than Kalshi, this creates a window where
# passive Kalshi limit orders can be posted at favorable prices.
# Set POLY_ENABLED=False to disable entirely and run as before.

POLY_ENABLED: bool = True
POLY_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_REST_BASE: str = "https://clob.polymarket.com"
POLY_GAMMA_BASE: str = "https://gamma-api.polymarket.com"

POLY_STALE_SIGNAL_MS: int = 2000          # Reject Polymarket state older than this
POLY_ENDGAME_SECONDS: float = 45.0        # Treat final 45s of Poly window as endgame
POLY_MIN_TOP_QTY: float = 50.0            # Min top-of-book USDC on Polymarket side
POLY_MAX_SPREAD: float = 0.06             # Max acceptable Polymarket spread (probability)
POLY_RECONNECT_DELAY: float = 5.0         # Seconds before reconnecting after WS failure

# Signal fusion weights — must sum to ~1.0 (renormalized internally)
# w_kalshi_microprice: weight on Kalshi's imbalance-weighted midprice
# w_kalshi_mid: weight on Kalshi's simple midprice
# w_poly_signal: weight on the translated Polymarket signal
FUSION_W_KALSHI_MICROPRICE: float = 0.35
FUSION_W_KALSHI_MID: float = 0.15
FUSION_W_POLY_SIGNAL: float = 0.35        # remaining 0.15 folds into microprice

# Minimum Polymarket conditions for a signal to be usable
FUSION_MIN_POLY_PROB_CHANGE_3S: float = 0.03   # ≥3% move in 3s to qualify as signal
FUSION_MIN_POLY_IMBALANCE: float = 0.20         # book must have ≥20% imbalance aligned to side

# Signal quality caps
FUSION_MAX_POLY_NOISE: float = 0.60             # Block if noise score above this
FUSION_MAX_SIGNAL_SHIFT_CENTS: float = 8.0      # Max Polymarket contribution ±8¢ from 50¢

# Entry threshold: minimum fused net edge to enter
FUSION_MIN_NET_EDGE_WITH_POLY: float = 1.5      # ¢ — lower than solo threshold because signal is better

# Already-repriced guard: if Kalshi ask is within this of fused fair value, skip entry
FUSION_ALREADY_REPRICED_THRESHOLD: float = 2.0  # ¢

# Translation layer coefficients
FUSION_POLY_ALPHA: float = 0.30   # velocity contribution (per probability unit/sec × 100¢)
FUSION_POLY_BETA: float  = 0.05   # imbalance contribution (per unit imbalance × 100¢)
FUSION_POLY_GAMMA: float = 0.05   # noise penalty (per unit noise × 100¢)
FUSION_POLY_DELTA: float = 0.10   # endgame penalty (fraction of 100¢ near expiry)

# ── Strategy 3 — Poly-Primary Directional Scalp ──────────────────────────────
# When no candle-engine signal is available, use Polymarket's 5m market as the
# primary signal source. Direction and fair value come entirely from Poly's live
# order book. Requires POLY_ENABLED=True and an active PolymarketStream.
#
# Design: fires in the elif branch of _tick() — only when Strategy 2 has no
# signal/decision from the candle engine. One strategy per tick, no double-update
# of the Poly feature history.

HFT_POLY_PRIMARY_ENABLED: bool = True
HFT_POLY_FAST_POLL_INTERVAL: float = 1.5     # Kalshi REST poll rate when Poly is active (vs 4.0s normal)
HFT_POLY_PRIMARY_NET_EDGE_FLOOR: float = 2.0 # Min fused net edge (¢) — conservative; no candle confirm
HFT_POLY_PRIMARY_MIN_PROB_CHANGE_3S: float = 0.04  # Min 3s Poly prob change; stricter than fusion gate
HFT_POLY_PRIMARY_MIN_IMBALANCE: float = 0.30  # Min aligned Poly order-book imbalance (side-adjusted)
HFT_POLY_PRIMARY_FEED_HEALTH_MIN: float = 0.60  # Min Poly feed health score
HFT_POLY_PRIMARY_MAX_NOISE: float = 0.40      # Max Poly noise score before rejecting
HFT_POLY_PRIMARY_MID_OFFSET: float = 0.02    # up_mid must be ≥ this far from 0.50 (directional clarity)
HFT_POLY_PRIMARY_MIN_KALSHI_MINUTES: float = 7.0  # Window correlation gate: Poly 5m ≈ Kalshi 15m early phase
