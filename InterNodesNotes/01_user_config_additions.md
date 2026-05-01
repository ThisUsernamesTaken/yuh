# Step 1: `user_config.py` additions

Append the following block to the END of `user_config.py` on the
secondary machine. Default values shown match the primary.

These knobs do nothing until referenced by code in steps 6-7.

---

```python
# ── Microstructure-aware gating (Claude 2026-04-30) ────────────────────────
# Five-component upgrade to entry/exit decision-making, designed after the
# 2026-04-30 hold-to-expiry incident exposed two systemic weaknesses:
#   1. Engine catches falling knives — places maker at high limit, gets
#      adverse-selected by retail dumping
#   2. Bid-touch stops fire on single-tick wicks from one panicking
#      participant, selling the bottom of normal chop
#
# Per user direction (paper testing inaccurate, execution is the signal),
# all flags activated together rather than phased.

# ── Phase 1: Trade tape ────────────────────────────────────────────────
KALSHI_TAPE_ENABLED              = True
KALSHI_TAPE_RETENTION_S          = 120.0  # Per-ticker trade history kept

# ── Phase 2: Stop persistence ──────────────────────────────────────────
STOP_REQUIRES_PERSISTENCE        = True
STOP_PERSISTENCE_SECONDS_DEFAULT = 5      # Bid must be ≤ trigger for ≥ this long
STOP_PERSISTENCE_MIN_VOLUME_CT   = 10     # Trades at-or-below trigger in window
STOP_PERSISTENCE_VOLUME_WINDOW_S = 10     # Window for volume confirmation
STOP_DEFER_ON_THICK_BID          = True   # If thick bid below trigger, defer stop
STOP_DEFER_THICK_BID_VOLUME_CT   = 100    # Threshold for "thick" support

# ── Phase 3a: Entry flow gate ──────────────────────────────────────────
ENTRY_FLOW_GATE_ENABLED          = True
ENTRY_FLOW_WINDOW_S              = 5.0    # Recent flow window
ENTRY_FLOW_BASELINE_S            = 15.0   # Baseline for deceleration check
ENTRY_FLOW_MAX_ADVERSE_SHARE     = 0.60   # Block if adverse flow > 60%
ENTRY_FLOW_MAX_ADVERSE_VEL_CPS   = 2.0    # Block if mid moving > 2c/s against
ENTRY_FLOW_REQUIRE_DECEL         = True   # Require adverse-side deceleration
                                          # OR same-side dominance

# ── Phase 3b: Book density gate ────────────────────────────────────────
BOOK_DENSITY_GATE_ENABLED        = True
BOOK_DENSITY_DEPTH_LEVELS        = 5      # Top N price levels to sum
BOOK_DENSITY_MIN_SAME_SIDE       = 50     # Min ct on our bid stack
BOOK_DENSITY_MAX_OPP_DOMINANCE   = 4.0    # Block if opp_density > X * same_density

# ── Phase 4: Protective-order mode ─────────────────────────────────────
PROTECTIVE_ORDER_MODE            = True   # Replaces bid-check stop entirely
PROTECTIVE_TP_OFFSET_C           = 5      # TP price = entry + this
PROTECTIVE_SL_OFFSET_C           = 8      # SL price = entry - this
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
BB_PURE_MODE                     = True   # Master switch
BB_PURE_MIN_EDGE_PP              = 8.0    # Min mispricing pp to fire
BB_PURE_MAX_ENTRY_CENTS          = 70     # Don't enter above this price
BB_PURE_MIN_ENTRY_CENTS          = 5      # Don't fire on dust prices
BB_PURE_MIN_TIME_REMAINING_S     = 60.0   # No new entries within last minute
BB_PURE_KELLY_FRACTION           = 0.25   # Quarter-Kelly base
BB_PURE_KELLY_MAX_FRAC           = 0.15   # Hard cap on bankroll % per position
BB_PURE_VOLATILITY_LOOKBACK      = 15     # Mirrors founding-doc default
# Fair-value-anchored protective-order targets (used in Session 3)
BB_PURE_TP_EDGE_PP               = 5.0    # TP when fair value crosses
                                          # entry_implied_pp + this
BB_PURE_SL_EDGE_PP               = 8.0    # SL when fair value drops below
                                          # entry_implied_pp - this
```

---

## Verification

After saving:

```bash
python -c "import user_config as uc; print('BB_PURE_MODE:', uc.BB_PURE_MODE); print('PROTECTIVE_ORDER_MODE:', uc.PROTECTIVE_ORDER_MODE); print('KALSHI_TAPE_ENABLED:', uc.KALSHI_TAPE_ENABLED)"
```

Expected output:

```
BB_PURE_MODE: True
PROTECTIVE_ORDER_MODE: True
KALSHI_TAPE_ENABLED: True
```

If `KALSHI_DEMO=true` on secondary, you may want all gates `False`
during shake-down then enable individually. Adjust at your discretion.
