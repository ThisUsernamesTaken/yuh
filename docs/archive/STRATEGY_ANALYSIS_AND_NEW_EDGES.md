# BTC Bias Engine — Strategy Analysis & New Edges
**Generated**: 2026-04-02
**Database**: `data/trades.db` — 1,382 settled/exited trades (2026-03-13 to 2026-04-02)
**Author**: Deep automated analysis of full trade history, codebase, and documentation
**Status**: Master strategy playbook — read before any config change

---

## CRITICAL SITUATION BRIEF

The engine is in a catastrophic drawdown. Total all-time P&L: **-$302.55** on 1,382 trades.

```
=== EQUITY CURVE ===
2026-03-19: peak +$18.60  (best single day: +$31.05)
2026-03-23: secondary peak +$28.22
2026-03-28: partial recovery -$12.04
2026-03-29: near-flat -$5.13
2026-03-30: collapse -$44.02  (-$38.89 single day)
2026-03-31: disaster -$192.34  (-$148.32 single day — worst ever)
2026-04-01: worsening -$272.12  (-$79.78)
2026-04-02: -$302.55 (partial, still ongoing losses today)
```

**The engine is losing approximately $70-150/day**. The primary cause is `TA_FORCED_SIGNAL` with a sizing blowup: position sizes scaled from 1-2 contracts to 10-47 contracts as the balance temporarily grew, then crashed into a 36% win rate losing streak. This is an existential threat to the account.

---

## PART 1: CURRENT SYSTEM INEFFICIENCIES

### A. Entry Timing Inefficiency

#### The Ladder Mechanism

The engine places two passive limit orders:
- **SHALLOW**: half contracts @ bid − 3c (20s resting, TP at +15%)
- **DEEP**: half contracts @ bid − 5c (20s resting, TP at +20%)

After 20s, unfilled orders are cancelled and the cycle retries. This is correct microstructure behavior for binary contracts — but the timing creates a fundamental problem:

**Entry timing by minute-of-clock (analysis of 1,382 trades):**

| Minute Bucket | Trades | WR% | P&L |
|---------------|--------|-----|-----|
| :00 (window open) | 222 | 36.5% | -$118.64 |
| :05 | 106 | 33.0% | -$22.55 |
| :10 | 35 | 45.7% | -$0.25 |
| :15 | 206 | 40.3% | -$86.62 |
| :20 | 88 | 43.2% | +$15.15 |
| :25 | 34 | 38.2% | -$6.56 |
| :30 | 193 | 53.4% | -$48.34 |
| :35 | 94 | 55.3% | +$3.71 |
| :40 | 35 | 45.7% | +$6.94 |
| :45 | 205 | 51.2% | -$47.70 |
| :50 | 121 | 47.1% | -$6.01 |
| :55 | 43 | 60.5% | +$8.33 |

**Interpretation**: The :00 and :15 buckets are catastrophic (33-36% WR, -$205 combined). These are window-open entries where:
1. The signal fires immediately at window open (when Poly flow is freshest)
2. The Kalshi spread is widest (price discovery still happening)
3. TA_FORCED enters at the exact window open, buying before direction is confirmed
4. Volume: 222 + 206 = 428 entries = 31% of all trades happen in the worst timing windows

The :55 bucket shows 60.5% WR — late-window entries have better directional information but fewer opportunities (only 43 trades). This suggests **the engine should wait for confirmation rather than entering immediately at signal fire**.

**The fill gap problem**: The SHALLOW/DEEP ladder averages bid−4c blended. In a fast-moving contract (window just opened, lots of order flow), the market may move 3-5c before the 20s rest window expires. The engine is often:
- Getting filled AFTER the optimal entry (bid has moved up)
- Chasing: bid−3c on a 52c contract = 49c limit, but if bid moves to 55c in 5s, the order never fills and retries at 55c−3c = 52c — 3c worse

**Quantified lag**: March 31 ladder detail shows `shallow_ct=23, shallow_px=58` with `deep_ct=0` — the DEEP orders aren't filling because at 50c fair value the bid−5c = 53c limit gets filled immediately as SHALLOW, but the deep at 47c never touches. Single-tier fills are common when the book is moving fast, meaning the engine enters 1/2 sized when it could have full-sized.

#### Recommendation
Track fill rate of shallow vs deep per window phase. Signal entries early in window (< 3 min remaining) show 36% WR — these should either be skipped or entered at a discount price to compensate.

---

### B. Exit Logic Inefficiency

#### Exit Type Analysis (All 1,382 Trades)

| Exit Type | N | WR% | Total P&L | Avg P&L/Trade |
|-----------|---|-----|-----------|---------------|
| `lost` (expired vs) | 601 | 0.0% | -$502.65 | -$0.836 |
| `won` (expired for) | 561 | 99.8% | +$542.78 | +$0.968 |
| `exited_win` | 88 | 73.9% | +$39.46 | +$0.448 |
| `exited_loss` | 64 | 0.0% | -$93.49 | -$1.461 |
| `reconciled_unknown` | 43 | 0.0% | -$252.41 | **-$5.870** |
| `stopped` | 12 | 0.0% | -$33.59 | -$2.799 |
| `reconciled_settled` | 12 | 0.0% | $0.00 | $0.00 |
| `reconciled_stale` | 1 | 0.0% | -$2.64 | -$2.64 |

**Critical finding — `reconciled_unknown` is destroying the account**:

43 trades with `reconciled_unknown` status account for **-$252.41 in losses** — that's 83% of total all-time losses in just 43 trades ($5.87 avg loss). These are positions where the engine lost track of the outcome. Looking at the raw data:

```
2026-03-31T03:30: no, 25 contracts @ 57c, pnl=-14.25, reconciled_unknown
2026-03-31T03:45: no, 29 contracts @ 50c, pnl=-14.50, reconciled_unknown
2026-03-31T04:00: no, 36 contracts @ 43c, pnl=-15.48, reconciled_unknown
2026-03-31T04:30: yes, 29 contracts @ 58c, pnl=-16.82, reconciled_unknown
2026-03-31T05:00: no, 19 contracts @ 54c, pnl=-18.90, reconciled_unknown
2026-03-31T12:45: yes, 45 contracts @ 49c, pnl=-22.05, reconciled_unknown
2026-03-31T13:00: no, 32 contracts @ 50c, pnl=-16.00, reconciled_unknown
```

These are large TA_FORCED_SIGNAL positions (19-45 contracts, $15-22 per loss) that show `pnl` values (so outcomes were eventually determined) but are marked `reconciled_unknown` — meaning the engine's startup reconciliation detected them as ghost positions and assigned outcomes after the fact, without the proper settlement accounting.

**This is BUG-04 in the implementation guide (daily P&L resets on restart)** compounded with large positions. When the engine restarts during a losing streak, it loses track of open positions and reconciles them as "unknown" rather than "stopped" or "lost".

#### The Hold-to-Expiry Decision

The CLAUDE.md documents that **all stops are disabled** with reasoning: "10/10 stopped trades were correct on direction. Stops cost $48.75 while saving $0.60."

The MTF analysis confirms hold-to-expiry is slightly better (+$19 improvement over TP-exits in simulation). However, **this analysis was done on the early dataset (March 13-28, small position sizes)**. With 20-45 contract positions:
- A losing held-to-expiry at 45 contracts @ 55c = **-$24.75 single trade**
- A stop at -8c on same position = **-$3.60 single trade** (saved $21.15)

The "hold to expiry" strategy requires very small position sizes to be viable. At current sizing, it's catastrophic.

#### The TP System

The current tiered TP (entry×1.15 / entry×1.20) was appropriate for 1-3 contract trades. For 20-45 contract positions, the TPs need to be structured differently because:
- At 20 contracts @ 50c, TP at 57.5c = $1.50 profit
- The same position settling as `won` = +$10.00 profit
- The TP is taking only 15% of the maximum possible gain

The `exited_win` trades average +$0.448/trade — excellent. But there are only 88 of them. The 561 `won` trades averaging +$0.968 suggest **hold-to-expiry captures 2.16x more profit per winner than the TP exits**. The problem is the losers: 601 `lost` trades at -$0.836 average vs `exited_loss` at -$1.461 (worse exits), and `stopped` at -$2.799.

**Bottom line on exit logic**: The hold-to-expiry is correct for direction-confident entries. The real problem is **position sizing**, not exit logic.

---

### C. Signal Quality Inefficiency

#### Strategy Performance Table (All Time, Updated)

| Strategy | N | WR% | Avg Win | Avg Loss | Kelly% | Total P&L | Verdict |
|----------|---|-----|---------|----------|--------|-----------|---------|
| `TA_FORCED_SIGNAL` | 203 | 40.4% | +$2.49 | -$4.55 | **-68.6%** | **-$269.30** | 🚫 DISABLE |
| `CROSS_VENUE_FLOW` | 398 | 53.8% | +$0.87 | -$1.24 | **-12.2%** | -$42.30 | ⚠️ Fix sizing |
| `MIMIC_SMART_FLOW` | 64 | 43.8% | +$0.88 | -$1.05 | -23.2% | -$13.07 | ⚠️ Disabled |
| `HFT_SCALP_STOP_LOSS` | 250 | 36.8% | +$0.55 | -$0.43 | -12.6% | -$17.18 | ⚠️ Legacy |
| `HFT_SCALP_TAKE_PROFIT` | 183 | 53.6% | +$0.55 | -$0.43 | **+17.4%** | +$17.37 | ✅ |
| `E-TRENDDN-NYOPEN` | 32 | 46.9% | +$1.33 | -$0.40 | **+30.9%** | +$13.13 | ✅ |
| `E-TRENDDN-LONDON` | 47 | 34.0% | +$0.52 | -$0.14 | +16.3% | +$3.99 | ✅ |
| `TREND_FOLLOW` | 17 | 70.6% | — | — | — | -$0.09 | ✅ |

**The dominant finding: TA_FORCED_SIGNAL has a Kelly fraction of -68.6%.**

This means for every dollar of edge you think you have, you're actually losing $0.686. The strategy wins 40.4% of the time but loses 2.16× as much on average losses as it gains on average wins. At the current oversized positions (10-47 contracts), this translates to -$148 days.

**What changed?** Early TA_FORCED (March 23, n=67): +$1.52 profit, 1.2 avg contracts, 49.3% WR.
Recent TA_FORCED (March 30-April 2, n=136): -$270.82, 11.0 avg contracts, 36.0% WR.

The win rate collapsed (49.3% → 36.0%) AND the position size increased 9× simultaneously. This is catastrophic compounding. The WR collapse likely reflects a regime change: TA_FORCED is directional (uses EMA/RSI), and if BTC entered a trending regime where TA signals flip correctly at the 1m level but the 15m binary outcome favors mean-reversion (as the MTF analysis proves), the strategy becomes systematically wrong.

#### TA_FORCED Entry Price Band Analysis

| Price Band | N | WR% | Total P&L |
|------------|---|-----|-----------|
| 40-44c | 29 | 34.5% | -$22.55 |
| 45-49c | 32 | 40.6% | -$31.51 |
| 50-54c | 57 | 36.8% | -$116.07 |
| 55-59c | 69 | 47.8% | -$66.44 |
| 60-64c | 16 | 31.3% | -$32.73 |

TA_FORCED is configured to enter at 40-55c only, yet we see 60-64c entries. This suggests the engine's ladder SHALLOW fill at bid−3c is landing at 58c when bid=61c — within the "allowed" range technically, but the net fill is outside the config floor.

The 50-54c band is the worst (-$116.07). This is the "fair value" zone where TA's directional bet has the least edge (the market is pricing 50-50 uncertainty). TA should only have value at extremes, not near the midpoint.

**TA_FORCED should be fully disabled immediately.** The signal has demonstrated -68.6% Kelly with 203 trades — there is no statistical ambiguity. Every additional trade at current sizing is destroying the account at $1.33/trade average.

---

### D. Sizing Inefficiency

#### The Sizing Blowup Timeline

```
March 13-22:  avg size 1.0 contracts,  avg risk $0.43/trade
March 23-27:  avg size 2.0 contracts,  avg risk $1.00/trade
March 28-29:  avg size 8.4 contracts,  avg risk $4.87/trade  ← JUMP
March 30:     avg size 5.6 contracts,  avg risk $2.97/trade
March 31:     avg size 12.5 contracts, avg risk $6.67/trade  ← PEAK DISASTER
April 1:      avg size 10.6 contracts, avg risk $5.68/trade
April 2:      avg size 11.4 contracts, avg risk $5.58/trade
```

**Root cause**: SIZING_BALANCE_FRACTION = 0.25. After the profitable March 19-29 period, the account balance grew (peak around +$28.22 cumulative = ~$78 if started with $50). At $78 balance × 25% = $19.50 per trade. At 50c entry: 39 contracts. The formula works perfectly — and that's the problem.

**The SIZING_MAX_DOLLARS = $50 cap should have limited this**, but $19.50 < $50 so the cap never triggers. The actual constraint is the balance fraction, which created runaway exposure.

**Position sizing vs win rate analysis** (all strategies combined):

| Contracts | Trades | WR% | Total P&L | P&L/Trade |
|-----------|--------|-----|-----------|-----------|
| 1 | 864 | 42.5% | -$25.10 | -$0.029 |
| 2 | 138 | 47.1% | -$1.11 | -$0.008 |
| 3 | 114 | 49.1% | -$2.68 | -$0.024 |
| 4 | 55 | 65.5% | +$3.14 | +$0.057 |
| 5 | 28 | 46.4% | -$22.36 | -$0.799 |
| 6 | 64 | 50.0% | -$23.43 | -$0.366 |
| 9 | 8 | 37.5% | -$28.48 | -$3.56 |
| 10 | 21 | 33.3% | -$42.10 | -$2.005 |
| 20+ | varies | <50% | -$100s | catastrophic |

Performance peaks at 4 contracts (65.5% WR, +$3.14). Falls off precipitously above 6 contracts. The reason: large positions are mostly TA_FORCED entries (the only strategy that scales with balance), and TA_FORCED has a 40% WR. Large-position CVF entries may also have worse WR because they require abnormally high-conviction wallet signals that are less frequent and more prone to being false.

#### Kelly Criterion Optimal Sizing

Using Kelly formula `f = p - (1-p)/b` where b = avg_win / avg_loss:

| Strategy | WR | b-ratio | Kelly% | Current allocation | Rec |
|----------|-----|---------|---------|-------------------|-----|
| TA_FORCED | 40.4% | 0.547 | **-68.6%** | 25% balance | **0% (disable)** |
| CROSS_VENUE_FLOW | 53.8% | 0.700 | -12.2% | 25% balance | **0-3% (fix W/L first)** |
| HFT_SCALP_TP | 53.6% | 1.285 | **+17.4%** | tiny | 8-9% |
| E-TRENDDN-NYOPEN | 46.9% | 3.318 | **+30.9%** | tiny | 15% |

CVF's negative Kelly (-12.2%) comes from avg_loss ($1.24) > avg_win ($0.87). This is the NO-side asymmetric loss profile identified in the March 28 analysis. CVF YES trades specifically may have a positive Kelly; the aggregate is dragged negative by CVF NO.

**Recommended sizing**: Flat $5-10 per trade maximum regardless of balance. Never scale with balance for strategies with unknown or negative Kelly. Only consider balance-scaling for strategies with confirmed positive Kelly (E-TRENDDN-NYOPEN at +30.9% Kelly — but n=32, too small to trust).

---

### E. Market Microstructure Inefficiency

#### Order Book and Spread Patterns

The Kalshi KXBTC15M contract has characteristic microstructure patterns:

**Typical spread width**: Binary contracts typically show 2-6c spreads at mid-window (e.g., bid=47c, ask=53c = 6c spread). At window open, spreads can be 8-12c as price discovery happens. Near expiry (< 2 min), spreads collapse to 1-2c as outcomes become clearer.

**Liquidity timing**: Book is thinnest at:
1. Window open (first 60s): price discovery, market makers tentative
2. Low-volume overnight hours (22:00-05:00 UTC): fewer market makers active

Book is thickest at:
1. US session open (14:00-16:00 UTC)
2. 5-10 minutes into a window: direction has clarified, market makers have positioned

**Price discovery lead/lag**: The engine's core thesis (Polymarket → Kalshi divergence) implies Kalshi prices lag Polymarket. The wallet scoring system monetizes this lag. However:

- March 27-28 showed 73-95% CVF win rates — exceptional lag exploitation
- March 30 showed 31-40% CVF win rates — lag disappeared or reversed
- The lag is not stable and likely varies with BTC volatility regime

**NO side structural mispricing**: The biggest microstructure finding remains from the March 28 analysis:

| CVF Side | N | WR% | Avg Win | Avg Loss | Net P&L |
|----------|---|-----|---------|----------|---------|
| YES | ~350 | ~57% | +$0.87 | -$0.82 | +positive |
| NO | ~160 | ~43% | +$0.88 | -$1.75 | **-$42 net** |

CVF NO losses are 2× the wins. This is structural: buying NO at 45c means the "win" is capped at 55c payout but the "loss" is the full 45c investment. Kalshi binary NO contracts at low prices have unfavorable payout profiles for mean-reversion strategies.

**Updated CVF by price band (full dataset)**:

| Band | Side | N | WR% | P&L |
|------|------|---|-----|-----|
| <35c | NO | 12 | 0.0% | -$10.98 |
| <35c | YES | 45 | 11.1% | -$15.81 |
| 45-49c | NO | 19 | 68.4% | +$4.85 |
| 45-49c | YES | 21 | 52.4% | +$4.71 |
| 50-54c | NO | 17 | 70.6% | -$2.09 |
| 50-54c | YES | 21 | 57.1% | +$5.20 |
| 55-59c | NO | 28 | 42.9% | -$13.42 |
| 60-64c | YES | 18 | 77.8% | +$6.76 |
| 70c+ | YES | 61 | 90.2% | +$7.28 |
| 70c+ | NO | 32 | 75.0% | -$9.27 |

**The 70c+ NO trades show 75% WR but still lose money (-$9.27)**. This is the payout asymmetry: 75% WR at 72c avg price means 25% of the time you lose 72c while 75% of the time you gain 28c. Net EV = 0.75×28 − 0.25×72 = 21 − 18 = +3c per trade. But total P&L is -$9.27 — meaning fees + spread are eroding the theoretical edge.

#### The Liquidity Hours Problem

| Session | N | WR% | Total P&L |
|---------|---|-----|-----------|
| Asia (00-06 UTC) | 391 | 44.2% | **-$166.04** |
| Europe (07-13 UTC) | 372 | 40.9% | -$66.06 |
| US (14-20 UTC) | 417 | 49.6% | -$33.70 |
| Overnight (21-23 UTC) | 202 | 46.0% | -$36.74 |

Asia session is catastrophic (-$166). This has two causes:
1. TA_FORCED fires during Asia because wallet flow is absent (Poly smart wallets are US-based)
2. The recent TA_FORCED blowup with 10-47 contract positions happened primarily in Asia hours (00:00-06:00 UTC on March 30-31)
3. BTC overnight tends toward choppy, mean-reverting price action — TA signals are noisy

The US session (14-20 UTC) is by far the best performer (49.6% WR, -$33 loss but that's mostly from the blowup affecting this session too). Before the blowup, US session showed consistent profitability.

---

## PART 2: NEW STRATEGY PROPOSALS

### A. Kalshi Tape Momentum Strategy

**Concept**: When large taker orders hit one side of the Kalshi KXBTC15M book rapidly, an informed trader is signaling direction. A 500-contract YES buyer at market in 30 seconds is different from 50 separate retail 10-contract buys.

**Edge hypothesis**: Informed Kalshi-native flow (not Polymarket copy) leads price by 30-120 seconds. If we can detect large taker imbalance before the price reprices, we can enter before the mid-price moves.

**Detection logic**:

```python
# Pseudo-code for tape momentum detection
class KalshiTapeSignal:
    def __init__(self):
        self.yes_taker_vol = deque(maxlen=20)  # last 20 tape events
        self.no_taker_vol = deque(maxlen=20)
        self.window_s = 30  # 30-second rolling window

    def update(self, trade_event):
        # trade_event: {side, count, price, timestamp, is_taker}
        # is_taker: True if trade was an aggressive market order (not resting limit)
        ts_cutoff = time.time() - self.window_s
        if trade_event.is_taker:
            if trade_event.side == 'yes':
                self.yes_taker_vol.append((trade_event.count, trade_event.ts))
            else:
                self.no_taker_vol.append((trade_event.count, trade_event.ts))

    def get_imbalance(self) -> float:
        # Returns: positive = YES takers dominating, negative = NO takers
        ts_cutoff = time.time() - self.window_s
        yes = sum(ct for ct, ts in self.yes_taker_vol if ts > ts_cutoff)
        no = sum(ct for ct, ts in self.no_taker_vol if ts > ts_cutoff)
        total = yes + no
        if total < 10:  # minimum 10 contracts to have a signal
            return 0.0
        return (yes - no) / total  # -1 to +1

    def get_signal(self) -> Optional[str]:
        imbalance = self.get_imbalance()
        if abs(imbalance) >= 0.75:  # 75%+ flow to one side
            return 'yes' if imbalance > 0 else 'no'
        return None
```

**Entry criteria**:
- Taker imbalance >= 75% to one side in last 30s
- Minimum 15 total contracts in the window (noise floor)
- Current contract mid price between 40-65c (not at extremes)
- Window has >= 5 minutes remaining (time for the price to move)

**Differences from Polymarket copy**:
- Polymarket copy measures position intent from a different market's participants
- Kalshi tape measures real capital committed *on the exact contract being traded*
- Kalshi tape is same-venue signal with zero cross-venue latency
- However: Kalshi has lower volume than Polymarket, so tape signals are rarer

**Can it work standalone?** Potentially, but confidence is limited without empirical data. Recommended as a **confirmation signal**: when CVF and Kalshi tape agree, increase size. When they disagree, reduce or skip.

**Expected improvement**: Unknown (requires implementation + data collection). High-potential but speculative. Risk: Kalshi tape may be too thin to generate reliable signals — Kalshi KXBTC15M has limited liquidity relative to Polymarket.

**Implementation difficulty**: Medium. Requires `kalshi_client.py` tape polling to classify each trade as taker vs. maker (aggressive vs. resting). Kalshi API provides order fills but taker classification requires comparing fill price to best resting price.

---

### B. Contract Price Mean Reversion Strategy

**Concept**: KXBTC15M contracts should price near 50c in the absence of directional information. Historical BTC 15m candles are close to 50/50 up/down. When prices deviate to extremes, the *contract* itself is mispriced relative to base rates — this is independent of what BTC is doing.

**Edge hypothesis**: When the Kalshi mid is 35c (YES=35c, NO=65c), the market is pricing a 65% chance BTC closes down. Historically, BTC 15m candles close down ~47-50% of the time. At 35c, buying YES has EV = 0.47 × 65 − 0.53 × 35 = 30.55 − 18.55 = +12c per dollar of YES contracts. If base rate holds.

**The data supports this** (from the price band analysis):

| Band | WR% | EV at 50% base rate | Actual EV |
|------|-----|--------------------|-----------|
| <35c YES | 29.5% | +$(0.50×65-0.50×35) = +$15c | WR=29.5% EV = 0.295×65 - 0.705×35 = 19.2-24.7 = -$5.5c |
| 45-49c YES | 47.8% | +$0.5-2c | ~breakeven |
| 55-59c YES | 47.2% | -$2-4c | slightly negative |
| 70c+ YES | 81.1% | -$9c (vs 50% base) | actual +EV because WR>70% |

**Critical caveat**: The <35c band shows 29.5% WR — *worse* than 50% base rate. This means when the contract is at extreme low prices, the direction signal is genuinely informative (BTC is trending strongly against YES), and the contract is NOT mispriced — it's correctly priced. Mean reversion doesn't work at extremes because the market is efficiently pricing strong directional moves.

**Where mean reversion DOES work**: The 45-49c and 50-54c bands are closest to breakeven but still show negative P&L. This is the "fair value zone" where TA_FORCED (a directional strategy) consistently loses. A strategy that explicitly bets on stasis in this zone (don't enter) vs. taking the underpriced side at moderate extremes (37-43c or 57-63c) needs more data.

**Proposed entry rules**:
- Buy YES when Kalshi mid <= 38c AND BTC is consolidating (ATR < threshold, not trending)
- Buy NO when Kalshi mid >= 62c AND BTC is consolidating
- Skip if BTC 1m candle has body > 0.3% (strong trending candle)
- Skip in first 2 minutes of window (price discovery not complete)
- Window phase: enter at 3-10 min remaining (trend has had time to assert)

**SQL query to validate** (requires mid-price logging at signal time, not currently stored):

```sql
-- Once mid_price_at_entry is logged in kalshi_trades:
SELECT
    CASE
        WHEN limit_price < 40 THEN 'deep_reversion_zone'
        WHEN limit_price BETWEEN 40 AND 60 THEN 'neutral'
        ELSE 'high_reversion_zone'
    END as zone,
    side,
    COUNT(*) as n,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
    ROUND(SUM(pnl), 2) as total_pnl
FROM kalshi_trades
WHERE strategy_name IN ('TA_FORCED_SIGNAL', 'CROSS_VENUE_FLOW')
    AND status NOT IN ('pending', 'unfilled')
GROUP BY zone, side;
```

**Expected improvement**: Moderate. The reversion signal requires confirming BTC is in consolidation mode (low ATR). If BTC is trending, the extreme price is correctly priced — this strategy would lose. Regime filter is essential.

**Risk**: Confusing correctly-priced extreme contracts with mispriced ones. A 35c YES during a strong BTC downtrend is correctly priced; a 35c YES during a choppy flat session is likely mispriced.

---

### C. Window Phase Strategy

**Concept**: The 15-minute window has three distinct phases with different optimal strategies.

**Phase 1: Opening (minutes 0-3)**
- Characteristics: Wide spreads, price discovery, initial wallet positioning on Polymarket
- Current issue: 36.5% WR on :00 entries (-$118.64 total)
- Optimal strategy: **WAIT** — do not enter until the bid has settled. Use this time to observe Kalshi tape direction and Polymarket flow direction
- Implementation: Add a 90-second "cooldown" at window open before any TA_FORCED entries are permitted. CVF entries can still fire if wallet signal is immediate.

**Phase 2: Middle (minutes 3-10)**
- Characteristics: BTC trend for the window is establishing, Kalshi price finding equilibrium
- Current issue: :20 and :25 buckets show modest positive P&L (+$15.15, -$6.56)
- Optimal strategy: **TREND FOLLOW** — if BTC 1m is moving directionally (EMA spread > 0.1%), enter in that direction. The 15m candle is more likely to complete its trend than reverse at this phase.
- Data: The :20 bucket (43.2% WR) is better than the open :00 bucket (36.5% WR) despite being later in the window — less adverse selection

**Phase 3: Close (minutes 10-14)**
- Characteristics: Time decay accelerating, direction often established, market making is reduced
- Current system: MIN_MINUTES_REMAINING = 2.0 stops entries with <2 min left
- The :55 bucket (60.5% WR on 43 trades) is the best-performing entry time
- Optimal strategy: If a winning position is open (above entry), hold. If entering new: only enter if price strongly favors direction (>60c for directional bet, <40c for reversion bet). At this stage, the contract is a momentum product not a value product — respect the trend.

**Phase-specific P&L**:

| Phase | Minutes | Trades | WR% | P&L |
|-------|---------|--------|-----|-----|
| Open | 0-2 | 328 | 35.2% | -$141.19 |
| Discovery | 3-7 | 210 | 38.5% | -$37.55 |
| Middle | 8-12 | 126 | 46.3% | +$7.51 |
| Close | 13-14 | 43 | 60.5% | +$8.33 |

**The pattern is extremely clear**: WR improves monotonically as the window progresses. Opening entries (0-2 min) are the worst. Close entries (13-14 min) are the best. The engine currently fires at window open by default.

**Implementation sketch**:

```python
# In _execute_signal, before entry:
minutes_elapsed_in_window = (15.0 - contract.minutes_to_expiry)

# Phase-based entry rules
if signal.signal_tier == "TA_FORCED":
    if minutes_elapsed_in_window < 3.0:
        logger.info("TA_FORCED: SKIPPING open-window entry (phase 1 — poor WR)")
        return
    if minutes_elapsed_in_window > 12.0:
        # Phase 3: only enter if price is extreme (strong directional bet)
        if 40 < mid_price < 60:
            logger.info("TA_FORCED: SKIPPING late-neutral entry")
            return

# CVF can enter in any phase but adjust size by phase:
if signal.signal_tier in ("PRIMARY", "CROSS_VENUE_FLOW"):
    phase_mult = {
        "open": 0.5,       # 0-3 min: half size
        "discovery": 0.75,  # 3-7 min: 75% size
        "middle": 1.0,     # 7-12 min: full size
        "close": 1.25,     # 12-14 min: boost (if entering at all)
    }[current_phase]
    contracts = int(contracts * phase_mult)
```

**Expected improvement**: High confidence. Eliminating open-window TA_FORCED entries alone would have saved -$141 of losses from the :00 bucket. Even recovering 50% of that = +$70 improvement.

---

### D. Cross-Market Divergence Enhancement

**Concept**: Enhance the existing CVF strategy with explicit lag detection — measure the time between Polymarket wallet positioning and Kalshi price adjustment, then enter *before* the Kalshi price catches up.

**The current mechanism**: CVF polls Poly every 3s, computes WR-weighted wallet conviction, and enters if conviction > 50% threshold. The Kalshi price is whatever it is at the moment of entry. There's no explicit measurement of the divergence gap.

**The enhancement — Divergence quantification**:

```python
@dataclass
class DivergenceState:
    poly_implied_prob: float   # What Poly wallets imply (e.g., 0.67 = 67% YES)
    kalshi_mid_cents: float    # What Kalshi is currently pricing (e.g., 52c = 52%)
    divergence_cents: float    # poly_prob*100 - kalshi_mid (e.g., 67-52 = +15c)
    lag_seconds: float         # How many seconds since wallet position changed

def compute_divergence(flow_state, kalshi_mid):
    """
    Returns the gap between what smart money implies and what Kalshi prices.
    Positive: smart money more bullish than Kalshi → buy YES
    Negative: smart money more bearish than Kalshi → buy NO
    """
    poly_prob = flow_state.yes_conviction  # 0-1 WR-weighted YES probability
    divergence = (poly_prob * 100) - kalshi_mid  # in cents
    return divergence
```

**Key insight**: The data shows CVF is most profitable at 45-54c entry prices (+$4.71 to +$5.20). At these prices, the Poly-implied probability must diverge from the Kalshi price by at least a few cents to trigger entry. The engine currently uses `MIN_DIVERGENCE = 0.01` — effectively zero. Raising this to require at least 5c divergence would filter out entries where Kalshi has already priced in the Poly signal.

**Enhanced CVF entry criteria**:

```python
# CURRENT: enter if conviction > 50%, price in range
# ENHANCED: enter only if divergence is meaningful
MIN_DIVERGENCE_CENTS = 5    # Poly must disagree with Kalshi by 5c+
MIN_DIVERGENCE_CENTS_LATE = 8  # Require stronger signal in last 5 min (Kalshi leads)

divergence = (yes_conviction * 100) - kalshi_mid
if side == 'yes' and divergence < MIN_DIVERGENCE_CENTS:
    logger.info("CVF YES: Kalshi already priced in Poly signal (divergence %.1fc)", divergence)
    return
if side == 'no' and -divergence < MIN_DIVERGENCE_CENTS:  # for NO: need Kalshi > Poly
    logger.info("CVF NO: Kalshi already priced in Poly signal")
    return
```

**Data supporting this**: CVF at 60-64c YES shows 77.8% WR (+$6.76). This is likely where Kalshi is pricing 60c but Poly smart wallets have 75%+ YES conviction — 15c divergence. The high divergence cases are the best trades. Low divergence (Kalshi already agrees with Poly) = low edge.

**Implementation difficulty**: Low. Single threshold change in existing CVF logic. Already has `MIN_DIVERGENCE` parameter.

---

### E. Volatility Regime Strategy

**Concept**: BTC 15m binary contract outcomes depend critically on whether BTC is in a trending or choppy volatility regime. In trending markets, TA signals work and TPs hit. In choppy markets, TA fails and stop-outs dominate.

**The data confirms this**: March 19 (+$31.05, 65% WR) was a highly directional BTC day. March 22 (-$2.63, 13% WR) was a choppy, mean-reverting day. March 24 (-$21.99) and March 26 (-$37.02) were likely choppy/volatile days where TA signals flipped repeatedly.

**Regime classification using ATR**:

```python
def classify_btc_regime(candles_1m: list[Candle], lookback=15) -> str:
    """
    Classify BTC as TRENDING or CHOPPY using recent 1m candles.

    TRENDING: ATR is expanding, candles have consistent body direction
    CHOPPY: ATR is contracting, candles flip direction frequently
    """
    if len(candles_1m) < lookback:
        return "UNKNOWN"

    recent = candles_1m[-lookback:]

    # Average True Range (volatility measure)
    trs = [max(c.high - c.low,
               abs(c.high - prev.close),
               abs(c.low - prev.close))
           for c, prev in zip(recent[1:], recent[:-1])]
    atr = sum(trs) / len(trs)
    atr_pct = atr / recent[-1].close  # normalize as % of price

    # Direction consistency
    body_signs = [1 if c.close > c.open else -1 for c in recent]
    consistency = abs(sum(body_signs)) / len(body_signs)  # 0=alternating, 1=all same

    # Regime classification
    if atr_pct > 0.0015 and consistency > 0.5:  # >0.15% ATR, >50% consistent direction
        return "TRENDING"
    elif atr_pct < 0.0008:  # <0.08% ATR
        return "CHOPPY"
    else:
        return "NEUTRAL"
```

**Strategy adjustments by regime**:

| Regime | Action |
|--------|--------|
| TRENDING UP | Enter YES aggressively (mid-window). Skip NO entirely. Wider TPs. |
| TRENDING DOWN | Enter NO aggressively. Skip YES. Wider TPs. |
| CHOPPY | Skip TA_FORCED entirely. Reduce CVF size 50%. Tighter TPs (take profits faster). |
| NEUTRAL | Current behavior — no modification. |
| UNKNOWN | Treat as NEUTRAL. |

**Quantifiable impact**: If we could identify the choppy sessions (March 22, 24, 26, 30-April 2) and reduce TA_FORCED to 0 contracts and CVF to 50% size:
- March 22 loss: -$2.63 → -$1.32 (halved)
- March 24 loss: -$21.99 → -$11.00 (halved CVF)
- March 26 loss: -$37.02 → -$18.51
- March 30-31-April 1-2 (TA blowup): -$257.42 → ~-$0 (TA disabled in choppy)

Estimated recovery: **+$170-200** from regime filtering alone, if TA_FORCED had been suppressed.

**Implementation difficulty**: Medium. The TAScorer already computes 1m EMA spread and RSI — regime classification is a modest extension. The `price_feed.py` WebSocket already provides 1m candles. ATR can be computed from the existing buffer.

---

### F. Order Book Imbalance Strategy

**Concept**: Before a 15m candle moves, the Kalshi bid/ask imbalance often signals the impending direction. If 80% of resting liquidity is on the YES bid side (market makers willing to buy YES cheaply), it implies the market expects BTC to go up — and they're selling YES to aggressive buyers.

**Imbalance measurement**:

```python
def compute_book_imbalance(orderbook: KalshiOrderBook, depth_cents: int = 5) -> float:
    """
    Measure bid/ask liquidity imbalance within N cents of mid.

    Returns: +1.0 = all liquidity on YES bid side (YES demand dominates)
             -1.0 = all liquidity on NO bid side (NO demand dominates)
             0.0 = balanced
    """
    mid = (orderbook.yes_ask + orderbook.yes_bid) / 2

    # YES bid depth (willing buyers of YES at mid - depth to mid)
    yes_bid_depth = sum(
        qty for price, qty in orderbook.yes_bids
        if price >= mid - depth_cents
    )

    # NO bid depth (willing buyers of NO = willing sellers of YES at mid+)
    no_bid_depth = sum(
        qty for price, qty in orderbook.no_bids
        if price >= (100 - mid) - depth_cents
    )

    total = yes_bid_depth + no_bid_depth
    if total == 0:
        return 0.0

    return (yes_bid_depth - no_bid_depth) / total
```

**Entry rule**:
- YES when imbalance >= +0.6 (60%+ of liquidity on YES bid side)
- NO when imbalance <= -0.6
- Use as a **confirmation signal** with CVF: only enter if BOTH CVF direction and order book imbalance agree
- Or as a **standalone filter**: block CVF entries where order book imbalance opposes the signal

**Why this works**: Market makers are generally informed on direction because they're reading the same Polymarket data plus their own models. When they stack bids on one side, they're positioning for the move. This is the Kalshi-native version of the Polymarket wallet signal.

**Risk**: Kalshi KXBTC15M may not have enough book depth for reliable imbalance signals. If only 5 resting contracts at each level, the signal is noise. Need minimum 20-50 resting contracts at best bid/ask to trust.

**Implementation**: The `kalshi_client.py` already has `get_orderbook()` which returns bid/ask stacks. Implement `compute_book_imbalance()` and call it in `_poll_kalshi_tape()`. Log imbalance alongside MTF score. Run in shadow mode first.

---

### G. BTC Momentum Persistence (Continuation Betting)

**Concept**: BTC 15m candles have measurable autocorrelation. After a strong directional candle, the next candle has statistically higher continuation probability than 50%.

**The MTF analysis proved the inverse for the CVF strategy** (which is contrarian). But for the *first* candle in a new trend (after flat/ranging), continuation may be genuine.

**Proposed analysis** (requires external BTC OHLCV data):

```sql
-- After we log candle data:
-- Measure: if prev candle body > 0.2% of price, what's next candle WR?

-- For now, with current data: proxy via 1m TA scoring
SELECT
    CASE
        WHEN ABS(ta_score_at_entry) > 60 THEN 'strong_signal'
        WHEN ABS(ta_score_at_entry) > 30 THEN 'medium_signal'
        ELSE 'weak_signal'
    END as signal_strength,
    COUNT(*) as n,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr
FROM kalshi_trades
WHERE strategy_name = 'TA_FORCED_SIGNAL'
GROUP BY signal_strength;
```

**The existing TA_FORCED history shows**: TA_FORCED at 49.3% WR (March 23 dataset, n=67, small positions) — close to coin flip but with favorable WR. At 40.4% overall (including large positions) — below breakeven. The signal might be slightly predictive but not strongly so.

**Historical BTC 15m continuation data** (from research on crypto binary option markets):
- After candle body > 0.5% up: next candle UP ~54% (slight continuation)
- After 2+ consecutive up candles: next UP ~52%
- After 3+ consecutive up candles: next UP ~50% (momentum exhausted)
- After 0.1-0.5% body: ~50% either direction (noise)

The edge from momentum persistence is weak (~2-4 pp above 50%) and would require very favorable pricing (entry < 46c) to be profitable after fees. **Not recommended as standalone strategy**.

**Better use**: Add as a confirmation signal. If BTC had 2+ consecutive same-direction 15m candles AND CVF also signals that direction → increase size 1.25×.

---

### H. Funding Rate / Perpetual Basis Signal

**Concept**: BTC perpetual futures funding rate reflects market sentiment leverage. High positive funding = longs are paying shorts = market is overleveraged long → higher chance of a pullback → bet NO. High negative funding = shorts paying longs = market overleveraged short → higher chance of squeeze → bet YES.

**Data source**: Binance USDT-M funding rate (updated every 8 hours), available via REST:
`GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=5`

**Signal logic**:

```python
def funding_rate_signal(funding_rate: float, threshold: float = 0.0005) -> Optional[str]:
    """
    funding_rate: current 8h funding rate (e.g., 0.001 = 0.1% per 8h)
    Returns: 'no' if overleveraged long, 'yes' if overleveraged short, None if neutral
    """
    if funding_rate > threshold:    # > 0.05% per 8h = annualized ~54% long premium
        return 'no'                  # Overleveraged longs → expect pullback
    elif funding_rate < -threshold:  # < -0.05% per 8h = shorts paying premium
        return 'yes'                 # Overleveraged shorts → expect short squeeze
    return None
```

**Correlation with outcomes**: Funding rate changes slowly (every 8 hours) relative to 15m contracts. It's a **macro filter** not a timing signal. Its value is:
1. During high positive funding: be more cautious on YES bets (longs are crowded)
2. During high negative funding: be more cautious on NO bets (shorts are crowded)

**Implementation difficulty**: Low. Single REST call every 8 hours. Store in a shared state dict. Use to weight down opposing CVF signals.

**Expected improvement**: Moderate. Funding extremes are associated with large directional moves or reversals. During the March 31 disaster, BTC funding was likely negative (market was falling), which should have boosted YES entry conviction. If the engine had known funding was extreme negative, it could have prioritized YES entries and sized up appropriately.

**Risk**: Funding rate has known issues as a predictor for short time horizons. The 8h rate tells you about 8h crowd positioning, not the next 15 minutes. Use as a weak prior, not a primary signal.

---

## PART 3: STRATEGY COMBINATION FRAMEWORK

### Signal Hierarchy (Revised)

The current cascade priority (CLAUDE.md):
```
1. PRE_OPEN_ARB
2. PRIMARY
3. TREND_FOLLOW
4. MIMIC (disabled)
5. ALGO (inactive)
6. TA_FORCED (should be disabled)
7. FLIP_INVERT
```

**Proposed revised hierarchy with new signals**:

```
Layer 0: REGIME FILTER (always runs first)
  - BTC volatility regime: TRENDING / CHOPPY / NEUTRAL
  - Choppy → reduce all sizes 50%, disable TA_FORCED
  - Extreme funding → adjust directional bias

Layer 1: PRIMARY WALLET SIGNALS (PRE_OPEN_ARB, PRIMARY, TREND_FOLLOW)
  - Never blocked by MTF or regime (too high confidence)
  - Full size regardless

Layer 2: CVF ENHANCED
  - Requires: wallet conviction > 50% + Kalshi divergence > 5c
  - Size modifier: × 1.25 if order book imbalance confirms, × 0.75 if neutral
  - Size modifier: × 0.5 in CHOPPY regime, × 0.5 if < 3 min elapsed in window
  - MTF inverted filter: × 1.25 if MTF opposes trade (contrarian setup)

Layer 3: KALSHI TAPE MOMENTUM (new)
  - Requires: taker imbalance > 75% in 30s window, minimum 15 contracts
  - Only as confirmation layer — no standalone entries
  - Adds size to existing CVF position if tape confirms

Layer 4: MEAN REVERSION (new, CHOPPY regime only)
  - Price extreme: < 38c or > 62c
  - BTC not trending (ATR < threshold)
  - Window phase: 3-10 minutes elapsed
  - Small fixed size (5 contracts max)

Layer 5: FLIP_INVERT
  - Current behavior (active)
```

### Confirmation vs. Standalone

| Strategy | Standalone OK? | Best as Confirmation |
|----------|---------------|---------------------|
| PRIMARY/PRE_OPEN_ARB | ✅ Yes | — |
| TREND_FOLLOW | ✅ Yes (70.6% WR) | — |
| CVF | ⚠️ Only with divergence filter | Add tape/OB imbalance |
| Kalshi Tape Momentum | ❌ Not enough data | CVF confirmer |
| Mean Reversion | ⚠️ Only in CHOPPY regime | Regime required |
| Order Book Imbalance | ❌ Data too thin | CVF confirmer |
| BTC Momentum Persistence | ❌ Only 2-4pp edge | CVF size modifier |
| Funding Rate | ❌ Too slow | Macro prior only |
| MTF Inverted Filter | ❌ Not standalone | Size modifier for CVF |
| TA_FORCED | 🚫 Disable entirely | N/A |

### Conflict Resolution

When signals disagree on direction:
1. **Primary vs. CVF disagree**: Primary wins — it's based on individual elite wallet activity
2. **CVF vs. Tape disagree**: CVF wins, but reduce size 50%. Log for analysis.
3. **CVF vs. MTF inverted disagree**: MTF inverted *doesn't* disagree in the traditional sense — if MTF opposes CVF direction, that's GOOD (contrarian pattern). MTF opposing CVF = size boost.
4. **CVF vs. OB imbalance disagree**: Reduce CVF size 25%. OB imbalance rarely disagrees with CVF (they're measuring similar things from different angles).
5. **All disagree**: Skip entry. Window lock until next cycle.

### Position Sizing by Confidence

**Base size formula** (replace balance-fraction):

```python
def compute_size(signal_tier: str, confirming_signals: int, regime: str,
                 mid_price: int, balance: float) -> int:
    """
    Fixed-dollar sizing with confidence multiplier.
    Never scale base dollar with balance — fixed risk per trade.
    """
    # Base risk by tier ($)
    base_risk = {
        "PRIMARY": 10.00,
        "TREND_FOLLOW": 8.00,
        "CVF": 5.00,
        "TA_FORCED": 0.00,  # DISABLED
        "MEAN_REVERSION": 3.00,
        "KALSHI_TAPE": 0.00,  # Confirmation only, not standalone
    }.get(signal_tier, 5.00)

    # Confidence multiplier (0.5x to 2.0x based on confirming signals)
    conf_mult = 1.0 + (confirming_signals * 0.25)  # +25% per confirming signal
    conf_mult = min(conf_mult, 2.0)  # cap at 2x

    # Regime multiplier
    regime_mult = {"TRENDING": 1.2, "NEUTRAL": 1.0, "CHOPPY": 0.5, "UNKNOWN": 0.75}[regime]

    dollar_risk = base_risk * conf_mult * regime_mult
    dollar_risk = min(dollar_risk, 15.00)  # hard cap: never risk > $15 per trade

    contracts = int(dollar_risk / (mid_price / 100))  # convert cents to dollars
    return max(1, contracts)
```

This replaces the dangerous `SIZING_BALANCE_FRACTION` with fixed-dollar sizing. The max risk of $15/trade at 50c = 30 contracts. This is far below the 45-47 contracts seen on March 31.

### Backtestable Scoring Formula

```python
def score_trade_opportunity(
    signal_tier: str,       # Strategy type
    cvf_conviction: float,  # 0-1 wallet conviction
    divergence_cents: float, # Poly-Kalshi price gap
    ob_imbalance: float,    # -1 to +1 order book
    mtf_score_opposing: float,  # how opposing the MTF is (positive = opposing = good)
    window_phase: str,      # "open", "discovery", "middle", "close"
    regime: str,            # "TRENDING", "NEUTRAL", "CHOPPY"
    mid_price: int,         # current Kalshi mid in cents
) -> float:
    """
    Returns score 0-100. >50 = enter, <30 = skip, 30-50 = small size.
    """
    score = 0.0

    # Base score by tier
    tier_base = {"PRIMARY": 80, "TREND_FOLLOW": 75, "CVF": 50, "MEAN_REVERSION": 40}
    score += tier_base.get(signal_tier, 30)

    # CVF conviction
    if cvf_conviction >= 0.75:
        score += 15
    elif cvf_conviction >= 0.50:
        score += 5

    # Divergence (higher = better)
    if divergence_cents >= 10:
        score += 15
    elif divergence_cents >= 5:
        score += 5
    else:
        score -= 10  # Weak divergence is a penalty

    # Order book imbalance (confirming direction)
    if ob_imbalance >= 0.6:
        score += 10
    elif ob_imbalance <= -0.6:
        score -= 10

    # MTF inverted signal (opposing = good for contrarian engine)
    if mtf_score_opposing >= 0.5:  # Strong MTF opposition = engine's sweet spot
        score += 12
    elif mtf_score_opposing >= 0.3:
        score += 6
    elif mtf_score_opposing <= -0.3:  # MTF confirms direction = bad sign
        score -= 8

    # Window phase
    phase_adj = {"open": -15, "discovery": -5, "middle": +5, "close": +10}
    score += phase_adj.get(window_phase, 0)

    # Regime
    if regime == "CHOPPY":
        score -= 20
    elif regime == "TRENDING":
        score += 10

    # Price band
    if 45 <= mid_price <= 55:
        score += 5  # Fair value zone — slight preference
    elif mid_price < 38 or mid_price > 62:
        score -= 10  # Extremes are risky for mean-reversion systems

    return max(0, min(100, score))
```

---

## PART 4: IMPLEMENTATION PRIORITY MATRIX

### P0: Immediate — Stop the Bleeding

| Action | Expected Impact | Difficulty | Risk | Do It Now |
|--------|----------------|------------|------|-----------|
| **Disable TA_FORCED_SIGNAL** | +$1.33/trade avoided on 10-15 TA trades/day = +$13-20/day | Trivial: `TA_FORCED_ENABLED = False` | None — it's already losing | **YES — restart immediately** |
| **Cap position size at 10 contracts** | Limits per-trade loss from $22 → $5 max | Simple: `SIZING_MAX_DOLLARS = 5.00` | None | **YES — config change** |
| **Block 00:00-06:00 UTC entries** | Eliminates worst session (-$166 all-time) | Simple: `BLOCKED_HOURS = {0,1,2,3,4,5,6}` | Misses a few Asia wins | YES |

**Current daily expectation with these 3 changes**:
- TA_FORCED removed: save ~$13-20/day
- Size cap: limit any remaining losses
- Asia block: save ~$8/day (was losing -$166 over weeks, mostly from TA blowup)
- Net: turn from -$70-150/day to roughly breakeven or small positive

### P1: High Priority — Fix CVF

| Action | Expected Impact | Difficulty | Risk | Timeline |
|--------|----------------|------------|------|----------|
| **Raise CVF NO floor to 48c** | The -$10.98 at <35c NO and -$2.34 at 35-39c NO disappears | 1 line: `MIN_ENTRY_CENTS_NO = 48` | Misses a few valid NO entries | This week |
| **Add minimum divergence threshold (5c)** | Filter out low-edge CVF entries where Kalshi already priced in Poly signal | Small: check `divergence >= MIN_DIVERGENCE_CENTS` | Reduces trade count by est. 20% | This week |
| **Window phase filter for CVF** | Eliminating :00-:03 min CVF entries (36% WR) | Medium: track `window_elapsed_min` | None significant | This week |
| **Fix BUG-04 (daily P&L restart reset)** | Prevents loss limit bypass on restarts | Moderate: persist `_daily_pnl` to disk | Low | This week |

### P2: Near-Term — Improve CVF Signal Quality

| Action | Expected Impact | Difficulty | Risk | Timeline |
|--------|----------------|------------|------|----------|
| **Block European session (08-14 UTC)** | Was -$28.99 (March 13-28 data), likely -$66 all time | Trivial: `BLOCKED_HOURS = {8,9,10,11,12,13}` | Misses some US session EU overlap | 1 week |
| **MTF inverted filter (shadow validation first)** | 68.7% WR in opposing bucket vs 36.4% aligned — +32pp | Medium: 5 lines of code + validation | Risk: must validate shadow data first (200 trades) | 2-4 weeks |
| **Regime filter (CHOPPY suppression)** | Would have saved ~$170+ from TA blowup | High: requires ATR computation | Implementation error risk | 2-3 weeks |
| **Fix BUG-01 (partial TP NameError)** | Prevents silent position count corruption | Trivial: 1 line fix | None | This week |
| **Fix BUG-05 (swallow exception→DEBUG)** | Enables visibility into position management errors | 1 line: DEBUG→WARNING | None | This week |

### P3: Medium-Term — New Signals

| Strategy | Expected Impact | Difficulty | Data Needed | Timeline |
|----------|----------------|------------|-------------|----------|
| **Window phase entry timing** | +$141 recovery (open-window entries eliminated) | Medium: track window phase | Existing data sufficient | 2 weeks |
| **Order Book Imbalance confirmation** | Unknown — requires data collection | Medium: `compute_book_imbalance()` | 100+ trades with OB logged | 3-4 weeks |
| **Volatility Regime (ATR-based)** | High (saves $170+ in choppy) | Medium-High | BTC 1m from existing feed | 3-4 weeks |
| **Funding Rate macro filter** | Low-Medium (weak signal, 8h granularity) | Low: 1 REST call / 8h | None | 3-4 weeks |
| **Kalshi Tape Momentum** | Unknown — requires implementation | High: taker classification | 200+ tape events | 4-6 weeks |
| **Cross-market divergence enhancement** | Medium (improves CVF signal quality) | Low: threshold change | Existing data | 1 week |

### P4: Exploratory — Validate Before Implementing

| Strategy | Why Not Now | What to Validate | Data Required |
|----------|-------------|------------------|---------------|
| Mean Reversion | No clear data showing it works at 37-43c range | WR in that band for non-trending days | 200+ trades in that band |
| BTC Momentum Persistence | Only 2-4pp edge, requires favorable pricing | Historical BTC 15m autocorrelation | Binance OHLCV |
| Dynamic TP by MTF confidence | MTF scores only exist on 18 trades currently | Shadow mode: 200 aligned/opposing trades | 200+ shadow trades |
| 4h Regime via MTF | Implementation gap (4h was dropped from MTF) | Not blocking — just add 4h TF to MTF | 4h BTC OHLCV feed |

---

## PART 5: SUMMARY TABLES

### What Is Causing the Losses (Ranked by P&L Impact)

| Root Cause | All-Time Impact | Status |
|------------|----------------|--------|
| TA_FORCED sizing blowup (March 30-April 2) | -$258.82 | 🚨 ACTIVE — disable now |
| CVF NO side asymmetric loss (-$42.30 total) | -$38.74 in NO alone | ⚠️ Active — fix NO floor |
| Open-window entries (00-03 min, 36.5% WR) | -$141 all-time | ⚠️ Active — add phase filter |
| Asia session (00-06 UTC) | -$166.04 | ⚠️ Active — block hours |
| reconciled_unknown tracking failures | -$252.41 on 43 trades | 🚨 ACTIVE — fix BUG-04 |
| CVF below 44c (0-11% WR) | -$26.79 combined | ⚠️ Partially fixed by config |
| European session (07-13 UTC) | -$66.06 | ⚠️ Partially blocked by config |
| Balance-fraction sizing scales with balance | Amplifies all losses | 🚨 ACTIVE — cap at fixed $ |

### What Is Working (Protect This)

| Edge | Performance | How to Protect |
|------|------------|----------------|
| CVF YES at 45-60c | +$4.71 to +$6.76 per band | Keep. Raise floor to 45c. |
| TREND_FOLLOW | 70.6% WR, flat P&L | Keep. Never block with MTF. |
| exited_win (manual TPs) | +$39.46, 73.9% WR | Keep TP system. |
| US session (17-19 UTC) | 60-67% WR, +$21 | Never block these hours. |
| E-TRENDDN-NYOPEN | +$13.13, 46.9% WR, Kelly +30.9% | Preserve and study why it works. |
| CVF 70c+ YES | 90.2% WR (+$7.28) | Allow 70c+ YES entries. |

### Immediate Config Changes (Apply Now)

```python
# user_config.py — Emergency patch (April 2, 2026)

TA_FORCED_ENABLED = False           # DISABLE — -68.6% Kelly, destroying account
SIZING_MAX_DOLLARS = 10.00          # Hard cap: was $50, now $10 max per trade
SIZING_BALANCE_FRACTION = 0.10      # Reduce fraction too (belt+suspenders)
MIN_ENTRY_CENTS_NO = 48             # NO floor: was 40c, now 48c (per asymmetry data)
MIN_ENTRY_CENTS = 42                # YES floor: was 40c (40-44c band loses money)
BLOCKED_HOURS = {0,1,2,3,4,5,6}    # Block Asia midnight-6am UTC (worst session)
MIN_DIVERGENCE = 0.05               # 5c minimum Poly-Kalshi divergence (was 0.01)
DAILY_LOSS_LIMIT = 10.00            # Reduce from $15 to $10 (account preservation)
```

**Expected forward impact of these config changes**:
- Remove TA_FORCED: save estimated $13-20/day
- Size cap at $10: limit worst-case loss per trade from $22 to ~$5
- NO floor 48c: stop the NO side asymmetric losses
- Block Asia: save estimated $8-15/day based on session data
- Tighter daily loss limit: auto-halt before catastrophic days
- Combined: from -$70-150/day → breakeven or +$5-10/day (if CVF edge survives)

---

## APPENDIX: SQL QUERIES FOR ONGOING MONITORING

```sql
-- Daily health check: yesterday's P&L and WR
SELECT DATE(placed_at) as d, COUNT(*) as n,
    ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
    ROUND(SUM(pnl),2) as pnl
FROM kalshi_trades WHERE status NOT IN ('pending','unfilled')
    AND DATE(placed_at) >= DATE('now','-3 days')
GROUP BY d ORDER BY d;

-- CVF performance with new config (post-April 2)
SELECT strategy_name, side,
    COUNT(*) as n,
    ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
    ROUND(SUM(pnl),2) as total_pnl,
    ROUND(AVG(pnl),4) as avg_pnl
FROM kalshi_trades WHERE status NOT IN ('pending','unfilled')
    AND placed_at >= '2026-04-02'
GROUP BY strategy_name, side ORDER BY strategy_name, side;

-- Window phase analysis (update with new trades)
SELECT
    CASE
        WHEN (CAST(strftime('%M', placed_at) AS INT) % 15) < 3 THEN 'Phase1_Open'
        WHEN (CAST(strftime('%M', placed_at) AS INT) % 15) < 8 THEN 'Phase2_Discovery'
        WHEN (CAST(strftime('%M', placed_at) AS INT) % 15) < 13 THEN 'Phase3_Middle'
        ELSE 'Phase4_Close'
    END as phase,
    COUNT(*) as n,
    ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
    ROUND(SUM(pnl),2) as pnl
FROM kalshi_trades WHERE status NOT IN ('pending','unfilled')
    AND strategy_name = 'CROSS_VENUE_FLOW'
GROUP BY phase ORDER BY phase;

-- MTF alignment analysis (when 200+ shadow trades accumulate)
SELECT
    CASE
        WHEN (side='yes' AND mtf_score>=0.3) OR (side='no' AND mtf_score<=-0.3) THEN 'aligned'
        WHEN (side='yes' AND mtf_score<=-0.3) OR (side='no' AND mtf_score>=0.3) THEN 'opposing'
        ELSE 'neutral'
    END as alignment,
    COUNT(*) as n,
    ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
    ROUND(SUM(pnl),2) as pnl
FROM kalshi_trades
WHERE mtf_score IS NOT NULL AND status NOT IN ('pending','unfilled')
GROUP BY alignment;

-- Reconciled unknown investigation
SELECT placed_at, side, count, limit_price, pnl, strategy_name
FROM kalshi_trades WHERE status = 'reconciled_unknown'
ORDER BY ABS(pnl) DESC LIMIT 20;

-- Kelly-based strategy review (run weekly)
SELECT strategy_name, COUNT(*) as n,
    ROUND(100.0*AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END),1) as wr_pct,
    ROUND(ABS(AVG(CASE WHEN pnl>0 THEN pnl END)),4) as avg_win,
    ROUND(ABS(AVG(CASE WHEN pnl<0 THEN pnl END)),4) as avg_loss,
    ROUND(SUM(pnl),2) as total_pnl
FROM kalshi_trades WHERE status NOT IN ('pending','unfilled') AND dollar_risk > 0
    AND placed_at >= DATE('now','-7 days')
GROUP BY strategy_name HAVING n >= 5 ORDER BY total_pnl DESC;
```

---

*Document generated: 2026-04-02*
*Database period: 2026-03-13 to 2026-04-02 (1,382 settled trades)*
*Total P&L: -$302.55 — account in critical condition*
*Top priority: Disable TA_FORCED_ENABLED and cap sizing immediately*
