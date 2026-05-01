# RESEARCH — Optimal Limit Sell Prices for Kalshi Binary Contracts — Do Not Implement

Generated: 2026-04-08  
Source: `btc-bias-engine/data/trades.db` — `kalshi_trades` table  
Column key: `limit_price` = entry price in cents, `pnl` = total trade P&L in dollars, `count` = contracts

---

## Section 1: Data Overview

| Metric | Value |
|--------|-------|
| Total trades in DB | 1,734 |
| Trades with resolved P&L | 1,703 |
| Date range | 2026-03-13 → 2026-04-08 (~26 days) |
| Hold-to-expiry wins (`won`) | 726 |
| Hold-to-expiry losses (`lost`) | 603 |
| Early exits — better than holding (`exited_win`) | 93 |
| Early exits — worse than holding (`exited_loss`) | 66 |
| Catastrophic exits (extreme/nobounce/stopped/mtf) | 71 |
| Unfilled / reconciled / expired pending | 144 |

**On the current TP setup (from CLAUDE.md):**  
The engine places two passive limit sell orders on every fill:
- Shallow half: `entry × 1.15` (e.g., 45¢ → 51.75¢ → rounds to 52¢)
- Deep half: `entry × 1.20` (e.g., 45¢ → 54¢)

These are resting Kalshi book orders. Only 93 of 819 winning-outcome trades (11.4%) were `exited_win` — the vast majority held to expiry. This means the 15-20% TPs rarely fill, likely because 15-minute binary contracts don't spend time at intermediate prices on winning trades; they tend to jump at resolution.

**Critical clarification on `exited_win` status:** This does NOT mean "sold above entry for a profit." It means "this early exit beat holding to expiry." Many `exited_win` rows have negative per-contract P&L — the system sold below entry, but the contract ultimately resolved wrong, so the early sell avoided a larger loss. True take-profit exits (positive per-contract P&L) are a subset of `exited_win`.

---

## Section 2: Raw Data Tables

### 2.1 Exit Type Breakdown

| Status | Count | Avg P&L (¢/trade) | Total P&L |
|--------|-------|-------------------|-----------|
| `won` | 726 | +76.3¢ | +$553.85 |
| `lost` | 603 | −83.6¢ | −$504.14 |
| `exited_win` | 93 | +46.1¢ | +$42.88 |
| `exited_loss` | 66 | −159.3¢ | −$105.12 |
| `reconciled_settled` | 65 | 0.0¢ | $0.00 |
| `reconciled_unknown` | 61 | −413.8¢ | −$252.41 |
| `extreme_exit` | 32 | −437.0¢ | −$139.85 |
| `nobounce_exit` | 24 | −297.6¢ | −$71.42 |
| `expired_pending` | 17 | −412.2¢ | −$70.07 |
| `stopped` | 12 | −279.9¢ | −$33.59 |
| `mtf_reversal` | 3 | −457.7¢ | −$13.73 |

**Context:** The catastrophic exits (extreme/nobounce/stopped/mtf) — 71 trades averaging −350¢ — account for $258.59 in losses. This dwarfs any TP optimization. These are the primary P&L bleed.

### 2.2 P&L Distribution of All Positive-P&L Trades

| Profit Range | Count | Avg P&L |
|-------------|-------|---------|
| 1–3¢ | 8 | 2.5¢ |
| 4–6¢ | 17 | 5.5¢ |
| 7–10¢ | 19 | 9.7¢ |
| 11–15¢ | 17 | 12.4¢ |
| 16–25¢ | 50 | 20.4¢ |
| **26¢+** | **654** | **112.1¢** |

The heavy right tail (654 trades at 26¢+) is overwhelmingly `won` hold-to-expiry trades. For single-contract YES at 40¢ entry winning = 60¢ profit, multi-contract trades multiply this. The 1–25¢ range represents mainly early exits on 1-contract trades.

### 2.3 Hold-to-Expiry EV by Entry Band and Side

Win rate counts both `won` and `exited_win`. EV = win_rate% − avg_entry_cents (simplified before fees).

| Entry Band | Side | N | Win Rate | Avg Entry | EV/contract (hold) |
|-----------|------|---|----------|-----------|-------------------|
| `<40¢` | NO | 223 | 31.8% | 27.3¢ | **+4.6¢** |
| `<40¢` | YES | 180 | 29.4% | 26.3¢ | **+3.2¢** |
| `40–49¢` | NO | 200 | 48.0% | 45.2¢ | **+2.8¢** |
| `40–49¢` | YES | 157 | 52.2% | 44.8¢ | **+7.4¢** |
| `50–59¢` | NO | 268 | 56.0% | 54.2¢ | **+1.7¢** |
| `50–59¢` | YES | 235 | 63.4% | 54.3¢ | **+9.1¢** |
| `60–69¢` | NO | 75 | 66.7% | 63.0¢ | **+3.7¢** |
| `60–69¢` | YES | 90 | 68.9% | 63.6¢ | **+5.3¢** |
| `70¢+` | NO | 48 | 75.0% | 78.7¢ | **−3.7¢** ← only negative band |
| `70¢+` | YES | 83 | 84.3% | 79.7¢ | **+4.6¢** |

**Key finding:** Every band has positive hold EV except 70¢+ NO (−3.7¢/contract). The market prices NO contracts at 70¢+ (implying 70%+ win probability) but they only actually win 75% of the time — very close. However, losing 78.7¢ 25% of the time beats winning 21.3¢ 75% of the time when you're paying 78.7¢.

### 2.4 Hold-to-Expiry Winners — Per-Contract Profit on Wins Only

| Entry Band | Side | N (wins) | Avg Entry | Avg Profit/contract |
|-----------|------|---------|-----------|---------------------|
| `<40¢` | NO | 34 | 32.8¢ | **67.3¢** |
| `<40¢` | YES | 39 | 31.7¢ | **66.2¢** |
| `40–49¢` | NO | 82 | 45.5¢ | **43.4¢** |
| `40–49¢` | YES | 71 | 44.9¢ | **47.3¢** |
| `50–59¢` | NO | 141 | 54.3¢ | **28.3¢** |
| `50–59¢` | YES | 143 | 54.6¢ | **33.7¢** |
| `60–69¢` | NO | 48 | 63.2¢ | **10.9¢** |
| `60–69¢` | YES | 62 | 63.6¢ | **21.3¢** |
| `70¢+` | NO | 36 | 78.9¢ | **14.4¢** |
| `70¢+` | YES | 70 | 79.7¢ | **14.6¢** |

**Note on 60–69¢ NO:** Avg entry 63.2¢, avg win profit only 10.9¢. Max possible win = 100 − 63 = 37¢. The 10.9¢ average means a significant portion of "won" trades in this band had heavy multi-contract sizing that bled fees, or there's a mix of sizes dragging the average down. This band wins 66.7% but the 33.3% losses wipe more than the wins build.

### 2.5 True Take-Profit Exits (sold above entry) — Individual Trade Detail

Filtering `exited_win` to rows where `pnl/count > 0` (genuinely sold at profit):

**NO contracts entering at 40–49¢** (9 true TPs):

| Entry | Exit Price (implied) | Profit/contract |
|-------|---------------------|----------------|
| 41¢ | 64¢ | +23¢ |
| 42¢ | 77¢ | +35¢ |
| 43¢ | 49¢ | +6¢ |
| 44¢ | 95¢ | +51¢ |
| 45¢ | 46¢ | +1¢ |
| 47¢ | 51¢ | +4¢ |
| 49¢ | 51¢ | +2¢ |
| 49¢ | 58¢ | +9¢ |
| 49¢ | 75¢ | +26¢ |

Range: 46–95¢. Average implied exit: **~63¢**. Profit range: 1–51¢/contract.

**YES contracts entering at 40–49¢** (5 true TPs):

| Entry | Exit Price (implied) | Profit/contract |
|-------|---------------------|----------------|
| 46¢ | 49¢ | +3¢ |
| 47¢ | 49¢ | +2¢ |
| 48¢ | 64¢ | +16¢ |
| 49¢ | 67¢ | +18¢ |
| 49¢ | 57¢ | +8¢ |

Range: 49–67¢. Average implied exit: **~57¢**. Profit range: 2–18¢/contract.

**YES contracts entering at 50–59¢** (3 true TPs):

| Entry | Exit Price (implied) | Profit/contract |
|-------|---------------------|----------------|
| 50¢ | 79¢ | +29¢ |
| 51¢ | 77¢ | +26¢ |
| 57¢ | 82¢ | +25¢ |

Average implied exit: **~79¢**. Profit: 25–29¢/contract.

**NO contracts entering at 50–59¢** (2 true TPs):

| Entry | Exit Price (implied) | Profit/contract |
|-------|---------------------|----------------|
| 52¢ | 64¢ | +12¢ |
| 53¢ | 73¢ | +20¢ |

**`<40¢` entries** — 37 NO and 14 YES true TPs, mostly exits in the 60–105¢ range:

| Side | Avg Entry | Avg Profit/contract | Avg Implied Exit |
|------|-----------|--------------------|--------------------|
| NO | 24.2¢ | +53.6¢ | **77.9¢** |
| YES | 24.4¢ | +58.1¢ | **82.5¢** |

### 2.6 MFE / High-Water-Mark Data

**None.** The `kalshi_trades` schema contains no columns tracking maximum favorable excursion (MFE), peak contract price, or high-water mark during a trade's lifetime. This is the most significant gap for TP optimization.

---

## Section 3: Analysis — Optimal Limit Sell by Entry Band

### Framing the Problem

For a passive limit sell order on Kalshi:
- You buy YES (or NO) at entry price E¢
- You post a limit sell at price T¢ (your take-profit target)
- **The TP fills ONLY if** another buyer is willing to pay T¢ for the contract before expiry
- If nobody buys at T¢ before expiry, the contract settles to 0 or 100¢

For 15-minute BTC binary contracts, intra-contract liquidity is thin. The data shows that 88.6% of all winning trades held to expiry — the current 15/20% TPs simply don't fill on most winning trades. This strongly suggests that for entries in the 40–80¢ range, the contracts jump to near-100¢ quickly when heading for a win, and most TP orders at lower prices don't get picked up.

The exception is `<40¢` entries (speculative longs), where 51 true TPs were observed. These contracts swing more dramatically — a BTC contract priced at 24¢ can move to 80¢ on a strong candle, creating genuine liquidity.

### The Current TP Math (entry × 1.15 / 1.20)

| Entry | Shallow TP (×1.15) | Deep TP (×1.20) | Hold Win | TP Leaves on Table |
|-------|-------------------|----------------|----------|-------------------|
| 30¢ | 34.5¢ (+4.5¢) | 36¢ (+6¢) | 70¢ | **64¢** |
| 40¢ | 46¢ (+6¢) | 48¢ (+8¢) | 60¢ | **52¢** |
| 45¢ | 51.75¢ (+6.75¢) | 54¢ (+9¢) | 55¢ | **46¢** |
| 50¢ | 57.5¢ (+7.5¢) | 60¢ (+10¢) | 50¢ | **40¢** |
| 55¢ | 63.25¢ (+8.25¢) | 66¢ (+11¢) | 45¢ | **34¢** |
| 60¢ | 69¢ (+9¢) | 72¢ (+12¢) | 40¢ | **28¢** |

The current TPs capture 12–17% of the maximum possible win profit. They are rightly described as "passive" — they don't materially change the P&L profile unless the market happens to be offered at those tight levels.

### Per-Band Recommendation Logic

**Key constraint:** Without MFE data, we cannot compute "probability the price reaches X before expiry." The following recommendations are based on (a) observed true TP exits, (b) hold EV, and (c) the structural properties of 15-minute binary contracts.

---

### Entry Band: `<40¢`

**Win rate:** 29–32% | **Hold EV:** +3.2–4.6¢/contract | **Avg hold win:** 66–67¢/contract

**What happened:** The system executed 51 true TPs here, achieving average exits of **77.9¢ (NO)** and **82.5¢ (YES)**. This is the most liquid TP zone — speculative contracts swing dramatically.

**Why TP makes sense here:** A <40¢ contract implies the market expects this outcome ≤40% of the time. When the price spikes toward 80¢+, that's a temporary signal shift. Rather than betting on whether the shift holds to expiry, selling into the spike locks in a profit equivalent to two hold-to-expiry losses being offset by one win.

**Math example (YES at 28¢):**
- Hold EV = 29.4% × 72¢ − 70.6% × 28¢ = 21.2 − 19.8 = **+1.4¢/contract** (thin)
- TP at 82¢ captures 54¢ per contract
- If price reaches 82¢ on 40% of eventual winners: TP EV ≈ 0.294 × 0.40 × 54¢ ≈ **+6.4¢/contract**
- Hold EV is lower — TP wins if it fires even occasionally

**Recommendation: Post limit sell at 78–85¢**

Specifically: 80¢ as a round number is the center of mass for observed exits. Going above 85¢ risks not filling; going below 75¢ captures less than 48¢ which doesn't compensate for the 71% losing trades.

---

### Entry Band: `40–49¢`

**Win rate:** 48–52% | **Hold EV:** +2.8–7.4¢/contract | **Avg hold win:** 43–47¢/contract

**What happened:** Very few true TPs (9 NO, 5 YES). Observed exits ranged widely from 46¢ to 95¢. The 95¢ exit (NO at 44¢) and 77¢ exit (NO at 42¢) show these contracts DO reach high prices occasionally. But the majority of NO exits were at 46–58¢ (tiny profits).

**The YES 40–49¢ picture:** The highest exit was 67¢ (YES at 49¢). YES near-50¢ contracts have the best hold EV in any band (+7.4¢/contract). This is a band where holding is genuinely competitive with a TP.

**Math example (YES at 45¢):**
- Hold EV = +7.2¢/contract
- TP at 65¢: captures 20¢. To beat hold EV, needs to fire >36% of the time among winning trades.
- TP at 70¢: captures 25¢. Needs to fire >29% of the time.
- Without MFE, we estimate: mid-range entries in winner contracts probably pass through 65¢ often (contracts headed to 100¢ from 45¢ will pass 65¢). If they do, 65–70¢ TPs are clearly superior to holding.

**Critical issue:** The 93 exited_win trades (all historical) include both genuine TPs and damage-limitation exits. The true TP count for 40–49¢ is tiny (14 total). The current system's ×1.15/×1.20 TPs (roughly 52¢/54¢ for 45¢ entry) are capturing 7–9¢ when the real opportunity is 20–50¢.

**Recommendation: Post limit sell at 68–72¢**

This captures 23–27¢ from a 45¢ entry (vs. 7–9¢ currently). It's above the noise range where tiny accidental fills happen (46–51¢) and below the speculative spike zone of 80¢+. The handful of observed exits at 64–77¢ confirm this range is reachable.

---

### Entry Band: `50–59¢`

**Win rate:** 56–63% | **Hold EV:** +1.7–9.1¢/contract | **Avg hold win:** 28–34¢/contract

**YES 50–59¢ is the highest EV band overall (+9.1¢/contract).** The win rate (63.4%) is well above the implied probability (~54¢ entry = 54% implied), showing persistent edge.

**What happened:** Only 3 YES true TPs (exits at 77–82¢) and 2 NO true TPs (exits at 64–73¢). Very sparse data.

The 3 YES exits at 77–82¢ captured 25–29¢ each — close to what holding to expiry delivers (33.7¢). This suggests the 50–59¢ YES band, when it moves in your favor, often moves strongly and early, making high TPs achievable.

**Recommendation: YES 50–59¢ → post limit sell at 78–82¢; NO 50–59¢ → post limit sell at 73–78¢**

For YES specifically: the high hold EV (+9.1¢) means you don't NEED to TP early. The TP at 78¢+ only fires when the move is dramatic — it's a bonus, not the core profit mechanism. Setting it lower (65¢) would truncate many winners unnecessarily.

For NO: hold EV is thinner (+1.7¢/contract). A TP at 73–78¢ (capturing 19–24¢) is more clearly superior to the thin hold edge.

---

### Entry Band: `60–69¢`

**Win rate:** 67–69% | **Hold EV:** +3.7–5.3¢/contract | **Avg hold win:** 10.9–21.3¢/contract

**What happened:** Zero true TP exits observed. Complete absence of data. Recommendations below are pure EV math.

**Key issue — NO 60–69¢:** Avg entry 63¢, 66.7% win rate, avg hold win only 10.9¢/contract. Max possible win = 37¢. The 10.9¢ average suggests this band has much higher multi-contract sizing, compressing per-contract returns. The 33.3% loss rate costs 63¢/contract. EV is barely positive (hold EV +3.7¢). A TP at 82¢ capturing 19¢ would require firing only 19% of the time to beat hold EV — likely achievable on winning trades that move strongly.

**YES 60–69¢:** More profitable to hold (21.3¢ avg win). A TP at 84–88¢ captures 21–25¢, approximately matching the hold win average. Setting it this high means it only fires on dramatic moves, preserving most of the hold-to-expiry upside.

**Recommendation: YES 60–69¢ → post limit sell at 84–88¢; NO 60–69¢ → post limit sell at 82–85¢**

---

### Entry Band: `70¢+`

**YES win rate:** 84.3% | **Hold EV:** +4.6¢/contract | **Avg hold win:** 14.6¢/contract  
**NO win rate:** 75.0% | **Hold EV:** −3.7¢/contract | **Avg hold win:** 14.4¢/contract

**YES 70¢+:** Strong hold. Max win = ~20¢/contract (100 − 80). A TP at 90¢+ captures ~10¢, giving up half the max profit. Hold EV is solid. **Hold to expiry, or set TP at 90–95¢ only for capital recycling.**

**NO 70¢+:** This is the only band with negative hold EV. You're paying 78.7¢ for a contract that wins 75% (not the 78.7% the price implies). Every hold-to-expiry loss costs ~79¢ while wins only earn ~21¢. A TP at 88¢ captures only 9¢ — still better than the −3.7¢ hold EV if it fires frequently enough, but the real answer is: **avoid buying NO at 70¢+ unless you have strong conviction the market has priced it wrong.**

**Recommendation: YES 70¢+ → hold to expiry (or TP at 90–95¢); NO 70¢+ → post limit sell at 87–92¢, and reconsider entering this band**

---

## Section 4: Quick Reference Table

### Limit Sell Price by Entry Band

| If you buy YES at… | Post limit sell at | Expected profit captured | vs. current ×1.15 TP |
|--------------------|-------------------|--------------------------|----------------------|
| <35¢ | **80–85¢** | 46–51¢/contract | vs. 4–5¢ currently |
| 35–39¢ | **78–83¢** | 40–46¢/contract | vs. 5–6¢ currently |
| 40–44¢ | **68–72¢** | 25–30¢/contract | vs. 6–7¢ currently |
| 45–49¢ | **68–72¢** | 20–25¢/contract | vs. 7–8¢ currently |
| 50–54¢ | **78–82¢** | 25–30¢/contract | vs. 7–8¢ currently |
| 55–59¢ | **78–82¢** | 20–25¢/contract | vs. 8–9¢ currently |
| 60–64¢ | **84–88¢** | 22–26¢/contract | vs. 9–10¢ currently |
| 65–69¢ | **84–88¢** | 18–22¢/contract | vs. 10–11¢ currently |
| 70¢+ | **hold / 90–95¢** | 20–25¢/contract | vs. 10–11¢ currently |

| If you buy NO at… | Post limit sell at | Expected profit captured | Hold EV context |
|-------------------|-------------------|--------------------------|----------------|
| <35¢ | **78–83¢** | 44–49¢/contract | +4.6¢ hold EV |
| 35–39¢ | **78–83¢** | 40–45¢/contract | Same |
| 40–44¢ | **68–72¢** | 25–30¢/contract | +2.8¢ hold EV |
| 45–49¢ | **68–72¢** | 20–25¢/contract | Same |
| 50–54¢ | **73–78¢** | 20–25¢/contract | +1.7¢ hold EV |
| 55–59¢ | **73–78¢** | 18–22¢/contract | Same |
| 60–64¢ | **82–85¢** | 20–22¢/contract | +3.7¢ hold EV |
| 65–69¢ | **82–85¢** | 15–20¢/contract | Same |
| 70¢+ | **87–92¢** | 9–14¢/contract | **−3.7¢ hold EV — avoid or TP aggressively** |

---

## Section 5: Caveats and Sample Size Notes

### The MFE Gap — The Central Limitation

This analysis **cannot directly answer** the core question. To know whether "post TP at 72¢" is better than "post TP at 82¢" requires knowing: what fraction of winning trades pass through 72¢ vs. 82¢ during the 15-minute window? We have no MFE data.

Every recommendation above rests on:
1. What exits the system has **historically achieved** (actual fills at those prices)
2. Expected hold EV relative to TP profit capture
3. Structural intuition about how 15-minute binary contracts behave

A TP at 80¢ for a 45¢ entry might fill 0% or 40% of the time — both are plausible and we cannot distinguish.

**Priority action if implementing:** Add MFE logging. On every trade close, record the peak contract bid price observed during the trade's lifetime. With 200–300 trades of MFE data, the TP optimization becomes data-driven.

### Sample Size Warnings

| Entry Band / Side | True TP Count | Data Reliability |
|-------------------|--------------|-----------------|
| `<40¢` NO | 37 | Moderate — enough to see a pattern |
| `<40¢` YES | 14 | Weak but directionally informative |
| `40–49¢` NO | 9 | Weak — scattered exits |
| `40–49¢` YES | 5 | Very weak |
| `50–59¢` YES | 3 | Effectively no data |
| `50–59¢` NO | 2 | No data |
| `60–69¢` all | 0 | Pure EV math, no empirical exits |
| `70¢+` all | 0 | Pure EV math |

### Why the Current 15–20% TP Mostly Doesn't Fire

The 93 exited_win trades represent 11.4% of total winning outcomes. The ×1.15 TP for a 50¢ entry = 57.5¢. If a winning contract moves directly from 50¢ toward 100¢ at the 15-minute mark without spending time at 57¢ (no active buyers at 57¢ during the window), the TP sits unfilled and the position holds to expiry.

This suggests: either (a) most 15-minute contracts don't have enough intra-window price discovery for the TP to execute, or (b) the current TP prices are too tight to attract buyers (a 57¢ limit buy is a reasonable entry for a 50¢ contract — buyers might not materialize at +7¢). Going to 70–80¢ targets may actually be harder to fill on the same logic, but if the trade is going to win, the contract price eventually pushes there.

### The exited_loss Problem

66 `exited_loss` trades (−$105.12 total) exited early and would have been better off holding. These cases — where early sell hurt P&L — need separate analysis. They likely represent cases where the stop-loss or partial-exit logic triggered prematurely. **Fixing false early exits is higher priority than optimizing TP levels.**

### Fees

Kalshi charges ~7% of profit. On a 20¢/contract profit: ~1.4¢ fee. On a 60¢/contract profit: ~4.2¢ fee. Very small TPs (1–6¢) are largely eaten by fees — another reason the current ×1.15 TP is suboptimal for small-contract entries.

### Priority Order

Before implementing any TP change, the higher-impact items are:

1. **Stop catastrophic exits** — 71 trades × −350¢ average = −$258 in losses. Each catastrophic exit loses 4–5× a normal loss.
2. **Add MFE logging** — makes TP optimization data-driven rather than theoretical.
3. **Review `exited_loss` logic** — 66 trades that exited early and left P&L behind.
4. **Then optimize TP levels** using the ranges above.
