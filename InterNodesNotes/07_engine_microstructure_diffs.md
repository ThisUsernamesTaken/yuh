# Step 7: `polymarket_copy_engine.py` — microstructure gates + BB_PURE wiring

This is the largest change. Apply by:
1. Looking at `REFERENCE_polymarket_copy_engine.py` (full primary file in
   this directory) for exact line numbers + surrounding context
2. Or grep your own engine for the markers below to find where each
   change goes

The primary's engine has line numbers ~Apr 30 PM. Yours may differ — use
markers, not line numbers.

---

## Change 1: Engine init — wire `KalshiTape` into WS

**Marker to find** in your `polymarket_copy_engine.py`:
```python
        # ── Start Kalshi WebSocket for real-time orderbook data ──
        try:
            from kalshi_ws import KalshiWebSocket
            self._kalshi_ws = KalshiWebSocket(
```

**Replace this entire startup block with:**

```python
        # ── Start Kalshi WebSocket for real-time orderbook data ──
        # 2026-04-30: also init the millisecond KalshiTape and wire trade
        # events into it so the new gating layer has data to read.
        try:
            from kalshi_tape import KalshiTape
            self._kalshi_tape = KalshiTape(
                retention_s=float(_uc("KALSHI_TAPE_RETENTION_S", 120.0)),
            )
        except Exception as _tape_e:
            logger.warning("KalshiTape: init failed: %s", _tape_e)
            self._kalshi_tape = None

        def _tape_on_trade(ticker, trade_msg):
            """WS callback: push trade into both the existing flow tracker
            (already handled inside KalshiWebSocket) and our ms-resolution tape."""
            try:
                if self._kalshi_tape is None:
                    return
                side = str(trade_msg.get("taker_side",
                                          trade_msg.get("side", ""))).lower()
                if side not in ("yes", "no"):
                    return
                count_raw = trade_msg.get("count", trade_msg.get("count_fp", 0))
                count = int(float(count_raw)) if count_raw else 0
                if count <= 0:
                    return
                px_raw = (trade_msg.get("yes_price_dollars_fp")
                          or trade_msg.get("yes_price")
                          or trade_msg.get("price", "0.5"))
                px_f = float(str(px_raw))
                yes_price_cents = round(px_f * 100) if px_f < 1.0 else int(px_f)
                self._kalshi_tape.record_trade(ticker, side, count, yes_price_cents)
            except Exception:
                pass

        def _tape_on_mid(ticker, mid_cents):
            try:
                if self._kalshi_tape is not None:
                    self._kalshi_tape.record_mid(ticker, int(mid_cents))
            except Exception:
                pass
            logger.debug("KalshiWS mid: %s=%dc", str(ticker)[-15:], int(mid_cents))

        try:
            from kalshi_ws import KalshiWebSocket
            self._kalshi_ws = KalshiWebSocket(
                key_id=self._client._key_id,
                private_key_pem=self._client._private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
                demo=self._client._base == self._client.DEMO_BASE,
                on_mid_update=_tape_on_mid,
                on_trade=_tape_on_trade,
            )
            asyncio.create_task(self._kalshi_ws.connect())
            logger.info("KalshiWS: websocket task started")
        except Exception as e:
            logger.warning("KalshiWS: failed to start websocket, falling back to REST: %s", e)
            self._kalshi_ws = None
```

---

## Change 2: New methods on `PolymarketCopyEngine` class

Insert all of the following after `_check_daily_loss_limit` (search for
`def _check_daily_loss_limit` to find the right place). These are 8 new
methods — copy them verbatim.

See `REFERENCE_polymarket_copy_engine.py` lines ~8700-8990 for exact
positioning, but they can go anywhere after `_check_daily_loss_limit` and
before any caller.

```python
    # ════════════════════════════════════════════════════════════════════
    # Microstructure-aware gating (2026-04-30)
    # ════════════════════════════════════════════════════════════════════

    def _session_minute(self) -> float:
        """Current session minute (0.0–15.0). Returns 0 if window not open."""
        ws = float(getattr(self, "_window_start_time", 0) or 0)
        if ws <= 0:
            return 0.0
        return max(0.0, (time.time() - ws) / 60.0)

    def _session_strictness(self) -> float:
        """Per-session-minute strictness multiplier (>1.0 = stricter)."""
        sm = self._session_minute()
        for lo, hi, factor in _uc("SESSION_STRICTNESS_BUCKETS",
                                  [(0, 15, 1.0)]):
            if lo <= sm < hi:
                return float(factor)
        return 1.0

    def _session_stop_persistence_s(self) -> float:
        """Per-session-minute stop persistence requirement in seconds."""
        sm = self._session_minute()
        for lo, hi, secs in _uc("SESSION_STOP_PERSIST_BUCKETS_S",
                                [(0, 15, 5)]):
            if lo <= sm < hi:
                return float(secs)
        return float(_uc("STOP_PERSISTENCE_SECONDS_DEFAULT", 5))

    def _evaluate_entry_filter(
        self, ticker: str, side: str, entry_px: int,
    ) -> tuple[bool, str, dict]:
        """Composite entry-pass/block decision. Reads tape + book density.

        Returns (allow: bool, reason: str, snapshot: dict).
        """
        # [SEE REFERENCE_polymarket_copy_engine.py for full body — ~95 lines]
        # Key logic:
        #   - flow_on = ENTRY_FLOW_GATE_ENABLED
        #   - density_on = BOOK_DENSITY_GATE_ENABLED
        #   - tape.flow(ticker, 5.0): adverse_share if > 0.6 → block
        #     UNLESS opposing-side decelerating (exhaustion)
        #   - tape.velocity_cps: if mid moving > 2c/s adverse → block
        #   - book.density: if same-side < 50ct or opp_dom > 4× → block
        ...

    def _evaluate_stop_persistence(
        self, pos: dict, side: str, trigger_px: int, current_bid: int,
    ) -> tuple[str, str, dict]:
        """Decide whether to FIRE the stop, DEFER it, or no-op.

        Returns (action, reason, snapshot) where action is:
          "passthrough" — gate disabled OR conditions met (legacy fires)
          "defer"       — wait, persistence/volume/density says no
        """
        # [SEE REFERENCE_polymarket_copy_engine.py for full body — ~90 lines]
        # CRITICAL early returns (BUG-FIX 2026-04-30 PM):
        #   if current_bid <= 0:
        #       return "passthrough", "no_bid_data", snapshot  # dead book
        #   if bid_seconds > 300.0:
        #       return "passthrough", "defer_max_exceeded", snapshot
        ...
```

**For the FULL bodies of `_evaluate_entry_filter` and
`_evaluate_stop_persistence`, copy verbatim from
`REFERENCE_polymarket_copy_engine.py`** — search for the marker lines in
that file:
- `def _evaluate_entry_filter(` → ~line 8775
- `def _evaluate_stop_persistence(` → ~line 8869

---

## Change 3: New `_maintain_protective_order()` method

Place just before `_cancel_tp_order()` (search for that marker).

Full body in `REFERENCE_polymarket_copy_engine.py` — search for
`async def _maintain_protective_order(self) -> bool:`.

Key behavior:
- Returns `True` if protective layer fully owns the position (caller
  should mark `pos["_protective_active"] = True`)
- Returns `False` when mode disabled OR position not yet visible on
  Kalshi (cache lag still in flight)
- Maintains a single resting Kalshi-side limit sell at TP or SL price
- Re-pegs on state flip / count change with 2s debounce
- Pre-expiry < 60s remaining → force market sell @ 1¢

---

## Change 4: New `_evaluate_bb_pure_signal()` + `_execute_bb_pure_signal()` methods

Place these right before `_session_minute()` (after the gating methods).

Full bodies in `REFERENCE_polymarket_copy_engine.py`:
- `async def _evaluate_bb_pure_signal(self):` → ~line 8755
- `async def _execute_bb_pure_signal(self, sig)` → ~line 8856

Key behavior:
- `_evaluate_bb_pure_signal()` reads BB model fair_value vs market mid,
  builds config from `BB_PURE_*` knobs, calls `bb_pure.evaluate()`,
  returns `BBSignal | None`
- `_execute_bb_pure_signal(sig)` runs entry filter gate, places order at
  `best_yes_ask` (or `best_no_ask`), capped at `suggested_entry_cents+2c`
  slippage; sets `_open_position` so protective mode handles exit

---

## Change 5: Wire BB_PURE into the signal cascade

**Find:**
```python
        # ── Signal cascade (priority order — matches profitable version) ──
        # ONE TRADE PER SESSION. After any exit, window is locked.
        if getattr(self, '_window_locked', False):
            self._publish_state()
            return

        # Wallet tiers DISABLED — data shows they override contract mid direction
```

**Replace with:**
```python
        # ── Signal cascade (priority order — matches profitable version) ──
        # ONE TRADE PER SESSION. After any exit, window is locked.
        if getattr(self, '_window_locked', False):
            self._publish_state()
            return

        # ═══════════════════════════════════════════════════════════════════
        # BB_PURE_MODE — fair-value-anchored signal (Session 2 wiring)
        # ───────────────────────────────────────────────────────────────────
        # When BB_PURE_MODE=True, this evaluator runs FIRST and BYPASSES the
        # existing composite cascade (SR_FADE / LATE_DOMINANT / TA_FORCED).
        # If it returns a signal, the BB_PURE execution path runs directly.
        # The existing cascade is skipped entirely — founding philosophy:
        # the BB model IS the signal; everything else is execution quality.
        # ═══════════════════════════════════════════════════════════════════
        if not signal and bool(_uc("BB_PURE_MODE", False)):
            try:
                bb_sig = await self._evaluate_bb_pure_signal()
                if bb_sig is not None:
                    await self._execute_bb_pure_signal(bb_sig)
                    self._publish_state()
                    return  # BB_PURE took the session — skip composite cascade
            except Exception:
                logger.exception("CopyEngine BB_PURE evaluator/execute error")

        # Wallet tiers DISABLED — data shows they override contract mid direction
```

---

## Change 6: Wire entry filter into TA_FORCED entry placement

**Find:**
```python
                logger.warning(
                    "CopyEngine %s %s ENTRY: %s %s %dx @ %dc ($%.2f) | %s",
                    tier_tag,
                    "MAKER" if _maker_only else "MARKET",
                    signal.kalshi_side.upper(), contract.ticker[:25],
                    mkt_ct, _entry_px, dollar_risk, _market_tag,
                )
                order = await self._client.place_order(
```

**Replace with:**
```python
                # 2026-04-30 entry filter gate (microstructure-aware).
                _gate_allow, _gate_reason, _gate_snap = self._evaluate_entry_filter(
                    contract.ticker, signal.kalshi_side, _entry_px,
                )
                _gate_snap.update({
                    "ticker": contract.ticker,
                    "session_min": _gate_snap.get("session_min", 0.0),
                    "entry_cents": _entry_px,
                    "position_count": mkt_ct,
                })
                try:
                    if self._signal_logger:
                        await self._signal_logger.log_gate_decision(
                            gate="entry_flow",
                            decision="pass" if _gate_allow else "block",
                            reason=_gate_reason,
                            **_gate_snap,
                        )
                except Exception:
                    pass
                if not _gate_allow:
                    logger.warning(
                        "CopyEngine ENTRY-GATE BLOCKED: %s %s @ %dc | reason=%s",
                        signal.kalshi_side.upper(),
                        contract.ticker[-15:], _entry_px, _gate_reason,
                    )
                    return  # caller proceeds to next signal
                logger.warning(
                    "CopyEngine %s %s ENTRY: %s %s %dx @ %dc ($%.2f) | %s",
                    tier_tag,
                    "MAKER" if _maker_only else "MARKET",
                    signal.kalshi_side.upper(), contract.ticker[:25],
                    mkt_ct, _entry_px, dollar_risk, _market_tag,
                )
                order = await self._client.place_order(
```

---

## Change 7: Wire stop persistence into TA_FORCED stop block

**Find** (the TA_FORCED stop trigger block):
```python
                        if _bid_trig_ta or _mid_trig_ta or _fair_trig_ta or _pre_trig_ta:
                            _trig_parts_ta = []
                            if _bid_trig_ta: _trig_parts_ta.append("bid")
                            if _mid_trig_ta: _trig_parts_ta.append("mid")
                            if _fair_trig_ta: _trig_parts_ta.append("fair")
                            if _pre_trig_ta: _trig_parts_ta.append("pre_expiry")
                            _which_ta = "+".join(_trig_parts_ta) or "?"
```

**Insert immediately after this block** (BEFORE the `pos["_ta_stopped"] = True` that follows):
```python
                            # 2026-04-30 stop persistence gate. Pre-expiry
                            # trigger exempt — that path is the safety net.
                            if not _pre_trig_ta:
                                _stp_action, _stp_reason, _stp_snap = self._evaluate_stop_persistence(
                                    pos, _side_ta, _trigger_px_ta, _bid_ta,
                                )
                                try:
                                    if self._signal_logger:
                                        await self._signal_logger.log_gate_decision(
                                            gate="stop_persistence",
                                            decision="fire" if _stp_action == "passthrough" else _stp_action,
                                            reason=_stp_reason,
                                            **_stp_snap,
                                        )
                                except Exception:
                                    pass
                                if _stp_action == "defer":
                                    _last_def_p = float(pos.get("_stop_persistence_log_ts", 0) or 0)
                                    if time.time() - _last_def_p > 5.0:
                                        logger.info(
                                            "CopyEngine TA_FORCED STOP DEFER (persistence): %s "
                                            "trigger=%dc bid=%dc reason=%s",
                                            pos["ticker"][-15:], _trigger_px_ta,
                                            _bid_ta, _stp_reason,
                                        )
                                        pos["_stop_persistence_log_ts"] = time.time()
                                    return  # don't fire stop this cycle
```

---

## Change 8: Mirror the same persistence wiring into LATE_DOMINANT stop block

Find the analogous `if _bid_trig_ld or _mid_trig_ld or _fair_trig_ld or _pre_trig_ld:` block and insert the same pattern, replacing `_ta` with `_ld`, `_trigger_px_ta` with `_trigger_px`, `_side_ta` with `_side_ld`.

See `REFERENCE_polymarket_copy_engine.py` for the exact existing wiring.

---

## Change 9: Wire protective-order mode at top of `_manage_position`

**Find:**
```python
    async def _manage_position(self) -> None:
        """Smart flow-aware position management.

        Exit logic (priority order):
        0. Pre-expiry forced flatten (Patch #7, 2026-04-30)
```

**Replace docstring + add protective-order block:**
```python
    async def _manage_position(self) -> None:
        """Smart flow-aware position management.

        Exit logic (priority order):
        -1. Protective-order mode (Phase 4, 2026-04-30): when on, replaces
            bid-check stop with always-resting Kalshi-side TP/SL order.
        0. Pre-expiry forced flatten (Patch #7, 2026-04-30)
        ...
        """
        pos = self._open_position

        # ═══════════════════════════════════════════════════════════════════
        # PROTECTIVE-ORDER MODE (Phase 4, 2026-04-30)
        # ═══════════════════════════════════════════════════════════════════
        if pos is not None:
            try:
                _protective_owned = await self._maintain_protective_order()
                if _protective_owned:
                    pos["_protective_active"] = True
                else:
                    pos.pop("_protective_active", None)
            except Exception as _pe:
                logger.error("CopyEngine PROTECTIVE order error: %s", _pe)
```

---

## Change 10: Add `_protective_active` guard to bid-check stop blocks

**LATE_DOMINANT stop block** — find:
```python
        if (pos is not None
                and pos.get("strategy_name") == "LATE_DOMINANT"
                and not pos.get("_late_dom_stopped", False)
                and bool(_uc("LATE_DOMINANT_STOP_ENABLED", True))):
```

**Replace with:**
```python
        if (pos is not None
                and pos.get("strategy_name") == "LATE_DOMINANT"
                and not pos.get("_late_dom_stopped", False)
                and not pos.get("_protective_active", False)
                and bool(_uc("LATE_DOMINANT_STOP_ENABLED", True))):
```

**TA_FORCED stop block** — find:
```python
        if (pos is not None
                and pos.get("strategy_name") in ("TA_FORCED_SIGNAL", "TA_FORCED")
                and not pos.get("_ta_stopped", False)
                and bool(_uc("TA_FORCED_STOP_ENABLED", True))):
```

**Replace with:**
```python
        if (pos is not None
                and pos.get("strategy_name") in ("TA_FORCED_SIGNAL", "TA_FORCED")
                and not pos.get("_ta_stopped", False)
                and not pos.get("_protective_active", False)
                and bool(_uc("TA_FORCED_STOP_ENABLED", True))):
```

---

## Verification

After applying all 10 changes:

```bash
python -c "
import polymarket_copy_engine
src = open('polymarket_copy_engine.py').read()
markers = [
    '_evaluate_entry_filter',
    '_evaluate_stop_persistence',
    '_maintain_protective_order',
    '_evaluate_bb_pure_signal',
    '_execute_bb_pure_signal',
    'BB_PURE_MODE — fair-value-anchored signal',
    '_protective_active',
    'gate=\"entry_flow\"',
    'gate=\"stop_persistence\"',
    '_tape_on_trade',
    '_tape_on_mid',
    'no_bid_data',           # bug fix from step 8
    'defer_max_exceeded',    # bug fix from step 8
]
for m in markers:
    n = src.count(m)
    print(f'{m:<40} hits={n} {\"OK\" if n>0 else \"MISSING\"}')
"
```

All markers should show `hits >= 1`.
