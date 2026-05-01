    async def _sniper_check(self) -> None:
        """Pre-fire sniper. Two phases:
        Phase 1 (T-3s to T-1.5s): Read settling mid, lock side, size position
        Phase 2 (T+0s onwards): Hammer order every 0.5s until filled
        """
        window_s = 15 * 60
        now = time.time()
        boundary = int(now // window_s) * window_s + window_s
        time_to_boundary = boundary - now

        # ── Phase 1: Lock at T-3s ──
        if 1.5 < time_to_boundary <= 3.0 and not getattr(self, '_sniper_locked', False):
            tape = self._kalshi_tape
            settling_mid = tape.mid_price_cents if tape.updated_at > 0 else 50

            if settling_mid >= 60:
                side = "yes"
            elif settling_mid <= 40:
                side = "no"
            else:
                self._sniper_locked = True
                self._sniper_side = None
                logger.info("CopyEngine SNIPER: settling mid=%dc (undecided), skip", settling_mid)
                return

            try:
                bal = await self._client.get_balance()
                balance = bal.balance / 100.0
            except Exception:
                balance = 100.0

            budget = min(balance * SIZING_BALANCE_FRACTION, SIZING_MAX_DOLLARS)
            num_cts = min(int(budget / 0.51), 100)

            self._sniper_locked = True
            self._sniper_side = side
            self._sniper_num = num_cts
            self._sniper_boundary = boundary
            self._sniper_attempts = 0

            logger.warning(
                "CopyEngine SNIPER LOCKED: %s %dx @ 51c | mid=%dc | T-%.1fs",
                side.upper(), num_cts, settling_mid, time_to_boundary,
            )
            return

        # ── Phase 2: Hammer at T+0 ──
        if time_to_boundary > 0:
            return  # Not yet
        if not getattr(self, '_sniper_side', None):
            return  # No lock

        side = self._sniper_side
        num_cts = getattr(self, '_sniper_num', 10)
        locked_b = getattr(self, '_sniper_boundary', 0)
        attempts = getattr(self, '_sniper_attempts', 0)

        # Timeout after 8s
        if now - locked_b > 8:
            logger.info("CopyEngine SNIPER: timeout after %d attempts", attempts)
            self._sniper_side = None
            self._sniper_locked = False
            return

        if self._open_position is not None:
            self._sniper_side = None
            return

        # Reset window state on first attempt
        if attempts == 0:
            self._window_locked = False
            self._pa_entered_this_window = False
            self._trades_this_window = 0

        self._sniper_attempts = attempts + 1

        # Discover contract
        try:
            contracts = await self._client.find_btc_contracts(
                window_minutes=15, min_minutes_remaining=12.0,
            )
        except Exception:
            contracts = None

        if not contracts:
            return  # Will retry next poll (0.5s)

        ticker = contracts[0].ticker

        # Place order
        try:
            order = await self._client.place_order(
                ticker=ticker, side=side, price=51, count=num_cts,
            )
        except Exception as e:
            logger.debug("CopyEngine SNIPER attempt %d: %s", attempts, e)
            return  # Retry next poll

        filled = order.filled_count if order else 0

        # Cancel unfilled remainder
        if order and order.order_id and filled < num_cts:
            try:
                await self._client.cancel_order(order.order_id)
            except Exception:
                pass

        if filled == 0:
            return  # Retry next poll

        # ── FILLED ──
        fill_px = int(order.average_price) if order.average_price else 51
        logger.warning(
            "CopyEngine SNIPER FILLED: %s %dx @ %dc ($%.2f) | attempt %d | %.1fs after boundary",
            side.upper(), filled, fill_px, filled * fill_px / 100.0,
            attempts, now - locked_b,
        )

        # Place +5c sell
        tp_price = fill_px + 5
        tp_ids = []
        try:
            tp = await self._client.place_order(
                ticker=ticker, side=side, price=tp_price, count=filled, action="sell",
            )
            if tp and tp.order_id:
                tp_ids.append(tp.order_id)
                logger.info("CopyEngine SNIPER SELL: %dx @ %dc (+5c)", filled, tp_price)
        except Exception as e:
            logger.error("CopyEngine SNIPER sell failed: %s", e)

        self._open_position = {
            "order_id": order.order_id,
            "side": side,
            "entry_cents": fill_px,
            "ticker": ticker,
            "tier": "SNIPER",
            "count": filled,
            "fill_time": time.time(),
            "entry_conviction": 0.9,
            "entry_wallets": 0,
            "entry_wallet_count_at_last_scale": 0,
            "high_water_bid": fill_px,
            "had_flow_at_entry": False,
            "tiers_in": {"SNIPER"},
            "entry_elite_wallets": set(),
            "_initial_fill_count": filled,
            "shallow_filled": filled,
            "shallow_price": fill_px,
            "deep_filled": 0,
            "deep_price": fill_px,
            "signal_wallet_names": [],
            "tp_order_id": tp_ids[0] if tp_ids else None,
            "tp_order_ids": tp_ids,
            "_resting_buy_ids": [],
            "tp_price": tp_price,
        }
        self._window_start_time = time.time()
        self._window_locked = True
        self._trades_this_window += 1
        self._sniper_side = None
        self._sniper_locked = False

        # Reset lock for next boundary cycle
        prev_boundary = int(now // window_s) * window_s
        if getattr(self, '_sniper_boundary', 0) < prev_boundary:
            self._sniper_locked = False
            self._sniper_side = None

