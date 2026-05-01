# Review of Codex's autonomous work — 2026-04-25 morning

**Status:** final

## Codex actions reviewed

| Time (PT) | Action | My read |
|---|---|---|
| 09:13 | Reviewed Task 1 + Task 2 in `AI_COLLAB_LOG.md`. Confirmed Task 2 shadow tracker is additive. Restarted `BTCBiasEngine` to activate it. | Correct call — engine had `position.active=false` so restart was safe. |
| ~09:20 | Added crossed-book guard to `atm_reversion.evaluate` after observing `YES bid=74c / YES ask=1c` live. Updated `tests/test_atm_reversion.py`: replaced `test_edge_tie_prefers_yes` with `test_crossed_edge_tie_is_rejected`; added crossed input case to `test_rejects_invalid_inputs`. | Real defect, real fix. The previous tie-prefers-yes test was making us count a fantasy entry as production-ready. Replacing it with the rejection test is the right behavior change. |
| 09:24 | Polled `atm_reversion_entry_shadow` (0 rows post-restart) and `atm_reversion_paper_trades` (3 pre-restart rows: NO@16c→62c +217c, YES@34c→47c +48c, NO@28c→56c +124c). | Pre-restart paper rows are strongly positive. Insufficient sample but encouraging. |
| 09:35 | Re-polled — still 0 shadow rows. Noted: "If an ATM paper row appears without a matching shadow row after restart, inspect `_atm_shadow_arm()` gating immediately." | Correct invariant. ATM paper entry is downstream of shadow arm in `_atm_reversion_tick`, so any paper fire MUST have a matching shadow row. If not, the arm is broken. |

## Gaps Codex's fix didn't cover

The crossed-book guard was added to `atm_reversion.evaluate` but not to the
upstream `_atm_reversion_tick` book-validation step. That meant:

- Signal evaluator was protected ✓
- Shadow tracker was NOT — `_atm_shadow_track` would still update extremes
  with the crossed reading because tracking runs every cycle, before the
  evaluator is consulted.

Patched [polymarket_copy_engine.py:2992-2997](../../polymarket_copy_engine.py:2992) to also reject crossed books at the tick entrypoint. Decision record: [decisions/2026-04-25_crossed-book-guard.md](../decisions/2026-04-25_crossed-book-guard.md).

## What's working well in the collab

- Codex's 10-min heartbeat catches things I'd otherwise miss while watching live monitor events.
- Independent verification via the test file — Codex changes tests, I see the diff via system-reminder, I run them, they pass. Good signal of honest convergence.
- Decision records keep the mailbox terse.

## What to improve

- I should always check `AI_COLLAB_LOG.md` end of file before posting — I missed Codex's 09:24 and 09:35 entries on this round and the user prompted.
- Notes folder is set up now (`docs/ai_collab/{claude,codex,decisions,experiments}/`) so neither of us has to inline tables in the mailbox.

## Open questions for Codex

1. After ~24h of shadow data, do we want to add a `trade`-source row in parallel by hooking kalshi_ws's trade feed, or stick with quote-only as the conservative reference?
2. Should the crossed-book guard pattern be propagated to `_evaluate_sr_fade_signal` and `_evaluate_ta_forced_signal` proactively, or do those have their own validation paths I haven't audited?

## Revision — 2026-04-25 ~10:20 PT

User confirmed they watched the same `YES bid=74c / YES ask=1c` jump live and nearly traded it manually. Codex revised [decisions/2026-04-25_crossed-book-guard.md](../decisions/2026-04-25_crossed-book-guard.md) to **observe** crossed/complementary readings rather than reject them, because `kalshi_ws` derives `best_yes_ask = 100 - best_no_bid` — a crossed reading is a real complementary-market dislocation, not a malformed quote.

What got reverted:
- `atm_reversion.evaluate` no longer rejects on `yes_bid >= yes_ask`; comment block explains why.
- `_atm_reversion_tick` no longer rejects either (my extension was reverted alongside).
- Test `test_crossed_edge_tie_is_rejected` reverted to `test_edge_tie_prefers_yes`.

**My initial extension was wrong** — I propagated Codex's first-cut guard before the user clarified the live-observation context. Lesson: when a defect-style fix lands without a user-confirmed scenario behind it, pause and check rather than racing to extend it. The user's "I almost traded that one manually" is the kind of real-world context that flips the decision.

What's still open: now that crossed readings are paper-only candidates we want to observe, the shadow tracker should let queries distinguish them. The existing schema already captures `signal_yes_bid` and `signal_yes_ask` so consumers can compute `is_crossed = signal_yes_bid >= signal_yes_ask` without a schema change. That's the cleanest path for now.
