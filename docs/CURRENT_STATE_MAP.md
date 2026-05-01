# Current State Map

This is the fast onboarding map for the repo as it exists now.

## Active vs legacy

### Active live path

- [run_copy_engine.py](/C:/Trading/btc-bias-engine/run_copy_engine.py:1)
- [polymarket_copy_engine.py](/C:/Trading/btc-bias-engine/polymarket_copy_engine.py:503)
- [user_config.py](/C:/Trading/btc-bias-engine/user_config.py:1)
- [kalshi_client.py](/C:/Trading/btc-bias-engine/kalshi_client.py:1)
- [kalshi_ws.py](/C:/Trading/btc-bias-engine/kalshi_ws.py:1)
- [price_feed.py](/C:/Trading/btc-bias-engine/price_feed.py:1)
- [microstructure.py](/C:/Trading/btc-bias-engine/microstructure.py:1)
- [contract_sr.py](/C:/Trading/btc-bias-engine/contract_sr.py:1)
- [regime.py](/C:/Trading/btc-bias-engine/regime.py:1)
- [edge_sizer.py](/C:/Trading/btc-bias-engine/edge_sizer.py:1)
- [signal_logger.py](/C:/Trading/btc-bias-engine/signal_logger.py:1)

### Active but supporting

- [ta_module.py](/C:/Trading/btc-bias-engine/ta_module.py:1)
- [mtf_scorer.py](/C:/Trading/btc-bias-engine/mtf_scorer.py:1)
- [tf_analyzer.py](/C:/Trading/btc-bias-engine/tf_analyzer.py:1)
- [indicators.py](/C:/Trading/btc-bias-engine/indicators.py:1)
- [models.py](/C:/Trading/btc-bias-engine/models.py:1)
- [shadow_edge.py](/C:/Trading/btc-bias-engine/shadow_edge.py:1)

### Feature-flagged

- [paper_trader.py](/C:/Trading/btc-bias-engine/paper_trader.py:1)
- [sniper.py](/C:/Trading/btc-bias-engine/sniper.py:1)
- [sniper_signals.py](/C:/Trading/btc-bias-engine/sniper_signals.py:1)

### Historical / not current truth

- `_archive/`
- `docs/archive/`
- deleted legacy top-level files shown by `git status`

## Signal reality today

- `SR_FADE` is the sole live entry tier.
- `TA_FORCED` still runs evaluator/data-pipeline work, but live entry is off via `TA_FORCED_ENTRY_ENABLED = False`.
- Wallet-copy tiers are disabled.
- `SNIPER` is disabled.

## Config switches worth checking first

Open [user_config.py](/C:/Trading/btc-bias-engine/user_config.py:1) and confirm:

- `PAPER_TRADING`
- `TA_FORCED_ENABLED`
- `TA_FORCED_ENTRY_ENABLED`
- `SR_FADE_ENABLED`
- `WALLET_COPY_ENABLED`
- `SNIPER_ENABLED`
- `SAFETY_OVERSELL_HARDENING`
- `MAKER_ONLY`

These tell you more about live behavior than older docs do.

## Recent live changes already present in code

As of 2026-04-23:

- `SR_FADE_ENTRY_CAP_MIN = 7`
- `SR_FADE_ADVERSE_VEL_CENTS = 5`

Both are already present in [user_config.py](/C:/Trading/btc-bias-engine/user_config.py:276).

## Tests that cover the newest safety logic

- [tests/test_sr_fade_gates.py](/C:/Trading/btc-bias-engine/tests/test_sr_fade_gates.py:1)
- [tests/test_residual_reconciler.py](/C:/Trading/btc-bias-engine/tests/test_residual_reconciler.py:1)
- [tests/test_contract_sr.py](/C:/Trading/btc-bias-engine/tests/test_contract_sr.py:1)
- [tests/test_regime_damper.py](/C:/Trading/btc-bias-engine/tests/test_regime_damper.py:1)

If we touch modern live behavior, these are the first tests worth reading and running.

## Separate toolchains

### Operational scripts

`scripts/` contains useful ops and forensic tooling, especially:

- [scripts/reconcile_pnl.py](/C:/Trading/btc-bias-engine/scripts/reconcile_pnl.py:1)
- [scripts/post_session_diagnostic.py](/C:/Trading/btc-bias-engine/scripts/post_session_diagnostic.py:1)
- [scripts/attribution.py](/C:/Trading/btc-bias-engine/scripts/attribution.py:1)

### Paper / replay research

`paper_engine/` is a separate analysis toolchain, not the live execution path.

### Planning / notes

`to-do/` and much of `docs/` are useful context, but they are not authoritative for live behavior unless they match code and config.
