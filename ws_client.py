# ws_client.py — Binance WebSocket client with auto-reconnect

import asyncio
import json
import logging
from collections.abc import Callable, Awaitable
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import BINANCE_WS_URL
from models import Candle

logger = logging.getLogger(__name__)

# Type alias for the candle callback
CandleCallback = Callable[[Candle], Awaitable[None]]


class BinanceWSClient:
    """Binance WebSocket client for btcusdt@kline_1m.

    Only processes closed 1m candles (k.x == true). Emits Candle objects
    via the provided async callback. Auto-reconnects on disconnection.
    """

    def __init__(
        self,
        callback: CandleCallback,
        url: str = BINANCE_WS_URL,
        reconnect_delay: float = 3.0,
        max_reconnect_delay: float = 60.0,
    ) -> None:
        """
        Args:
            callback:            Async function called with each closed Candle.
            url:                 WebSocket URL.
            reconnect_delay:     Initial wait before reconnect attempt (seconds).
            max_reconnect_delay: Cap on exponential backoff (seconds).
        """
        self._callback = callback
        self._url = url
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._running = False
        self._candles_received: int = 0
        self._errors: int = 0

    async def start(self) -> None:
        """Start the WebSocket loop. Runs until stop() is called."""
        self._running = True
        delay = self._reconnect_delay
        logger.info("BinanceWSClient starting. URL: %s", self._url)

        while self._running:
            try:
                await self._connect_and_consume()
                delay = self._reconnect_delay  # Reset on clean exit
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                self._errors += 1
                if not self._running:
                    break
                logger.warning(
                    "WebSocket disconnected (%s). Reconnecting in %.1fs...", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
            except Exception as exc:
                self._errors += 1
                logger.error("Unexpected WebSocket error: %s", exc, exc_info=True)
                if not self._running:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

        logger.info("BinanceWSClient stopped.")

    async def _connect_and_consume(self) -> None:
        """Open connection and process messages until disconnection."""
        async with websockets.connect(self._url, ping_interval=20, ping_timeout=10) as ws:
            logger.info("WebSocket connected.")
            async for raw in ws:
                if not self._running:
                    break
                await self._handle_message(raw)

    async def _handle_message(self, raw: str) -> None:
        """Parse a raw WebSocket message and invoke callback for closed candles."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse message: %s", raw[:200])
            return

        kline = msg.get("k")
        if kline is None:
            return

        # Only process closed candles
        if not kline.get("x", False):
            return

        candle = Candle(
            timestamp=int(kline["t"]),       # Open time in ms
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
            closed=True,
        )

        self._candles_received += 1

        try:
            await self._callback(candle)
        except Exception as exc:
            logger.error("Callback error for candle at %d: %s", candle.timestamp, exc, exc_info=True)

    def stop(self) -> None:
        """Signal the client to stop after the current message."""
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "candles_received": self._candles_received,
            "errors": self._errors,
        }
