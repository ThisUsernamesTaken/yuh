# whale_monitor.py — Real-time BTC whale wallet monitoring via mempool.space WebSocket
#
# Connects to wss://mempool.space/api/v1/ws and subscribes to known exchange
# wallet addresses via the "track-addresses" action. Receives instant push
# notifications when BTC enters or leaves these addresses — including
# UNCONFIRMED mempool transactions (before they hit a block).
#
# Signal thesis for 15-minute BTC contracts:
#   - Large BTC INFLOW to exchange  = imminent sell pressure = BEARISH
#   - Large BTC OUTFLOW from exchange = accumulation/withdrawal = BULLISH
#   - Detection in mempool gives ~10-60 second head start before spot impact
#
# Publishes WhaleFlowState into a shared ref dict, consumed by the
# cross-venue flow engine as a third signal layer.

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SATS_PER_BTC = 100_000_000


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

MEMPOOL_WS_URL = "wss://mempool.space/api/v1/ws"
MEMPOOL_REST_URL = "https://mempool.space/api"

# Minimum BTC per transaction to count as whale activity
WHALE_MIN_BTC = 5.0

# Rolling window for whale flow calculation
WHALE_WINDOW_S = 900.0  # 15 minutes

# WebSocket reconnect
WS_RECONNECT_DELAY_S = 5.0
WS_PING_INTERVAL_S = 25.0

# Known exchange wallet addresses (BTC mainnet).
# Sources: OXT, Arkham, walletexplorer.com, on-chain clustering
#
# These are DEPOSIT/HOT wallets — when BTC flows IN, someone is depositing to sell.
# Cold storage wallets are less useful (scheduled treasury moves, not urgent sells).
EXCHANGE_WALLETS: dict[str, list[str]] = {
    "binance": [
        "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
        "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j",
        "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3",
        "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",
        "bc1qnkf7u8m5nc2mqzgahfrq39lrmaqt6kgr7sp05q",
    ],
    "coinbase": [
        "3Kzh9qAqVWQhEsfQz7zEQL1EuSx5tyNLNS",
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        "bc1q4c8n5t00jmj8temxdgcc3t32nkg2wjwz24lywv",
    ],
    "kraken": [
        "bc1qr4dl5wa7kl8yu792dceg9z5knl2gkn220lk7a9",
        "bc1q5shngadp2gf3tupnmfkdr7clkfeeqtyevagh5a",
    ],
    "bitfinex": [
        "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",
        "3JzbGFkJPaHpBMbMbXfqGPKBQ4AGJsQqho",
    ],
    "gemini": [
        "bc1qpx0q4wlxgncz3snltxe0hj47m3yg335avqglgz",
    ],
    "okx": [
        "bc1q2s3rjwvam9dt2ftt4sqxqjf3twav0gdx0k0q2etjz886yr73vrzsmzfxxm",
    ],
}

# Large known whale/fund wallets (non-exchange).
# Movement FROM these to exchanges is an even stronger sell signal.
WHALE_WALLETS: dict[str, list[str]] = {
    # Add known whale addresses here as discovered
    # "grayscale": ["bc1q..."],
    # "microstrategy": ["bc1q..."],
}


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class WhaleTransaction:
    """A detected large BTC transfer involving a known wallet."""
    txid: str
    exchange: str               # "binance", "coinbase", etc.
    direction: str              # "inflow" (bearish) or "outflow" (bullish)
    btc_amount: float
    detected_at: float          # time.time() when we saw it
    confirmed: bool = False     # True if in a block, False if mempool-only
    source_label: str = ""      # "exchange" or "whale"


@dataclass
class WhaleFlowState:
    """Aggregated whale flow state for the cross-venue engine."""
    # Transaction counts in rolling window
    inflow_count: int = 0
    outflow_count: int = 0
    total_count: int = 0

    # BTC amounts in rolling window
    inflow_btc: float = 0.0     # Total BTC flowing INTO exchanges (bearish)
    outflow_btc: float = 0.0    # Total BTC flowing OUT of exchanges (bullish)
    net_flow_btc: float = 0.0   # Positive = net inflow (bearish)

    # Derived signal
    whale_bias: float = 0.0     # -1.0 (strong outflow/bullish) to +1.0 (strong inflow/bearish)
    signal_strength: float = 0.0  # 0.0-1.0, based on volume

    # Recent notable transactions
    recent_txs: list = field(default_factory=list)

    # Freshness
    last_tx_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_stale(self) -> bool:
        """No whale activity in the last 5 minutes."""
        return (time.time() - self.last_tx_at) > 300 if self.last_tx_at > 0 else True

    @property
    def has_signal(self) -> bool:
        """Has enough whale data to be actionable."""
        return self.total_count > 0 and abs(self.whale_bias) > 0.1


# ─────────────────────────────────────────────
# Monitor
# ─────────────────────────────────────────────

class WhaleMonitor:
    """Real-time BTC whale wallet monitor via mempool.space WebSocket.

    Publishes WhaleFlowState into whale_ref["state"].

    Usage:
        whale_ref = {"state": None}
        monitor = WhaleMonitor(whale_ref)
        asyncio.create_task(monitor.run())
    """

    def __init__(self, whale_ref: dict) -> None:
        self._ref = whale_ref
        self._state = WhaleFlowState()

        # Transaction history (rolling window)
        self._txs: deque[WhaleTransaction] = deque()
        self._known_txids: set[str] = set()

        # Build reverse lookup: address → (exchange_name, source_type)
        self._watched_addresses: dict[str, tuple[str, str]] = {}
        for exchange, addrs in EXCHANGE_WALLETS.items():
            for addr in addrs:
                self._watched_addresses[addr] = (exchange, "exchange")
        for label, addrs in WHALE_WALLETS.items():
            for addr in addrs:
                self._watched_addresses[addr] = (label, "whale")

        self._all_addresses = list(self._watched_addresses.keys())

    async def run(self) -> None:
        """Main loop — connects to WebSocket, reconnects on failure."""
        logger.info(
            "WhaleMonitor starting — tracking %d addresses across %d exchanges",
            len(self._all_addresses),
            len(EXCHANGE_WALLETS) + len(WHALE_WALLETS),
        )
        while True:
            try:
                await self._run_websocket()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("WhaleMonitor WebSocket error: %s — reconnecting in %ds",
                               e, WS_RECONNECT_DELAY_S)
            await asyncio.sleep(WS_RECONNECT_DELAY_S)

    async def _run_websocket(self) -> None:
        """Connect to mempool.space WS and subscribe to tracked addresses."""
        import websockets

        async with websockets.connect(
            MEMPOOL_WS_URL,
            ping_interval=WS_PING_INTERVAL_S,
        ) as ws:
            logger.info("WhaleMonitor: WebSocket connected to mempool.space")

            # Subscribe to address tracking
            # mempool.space supports "track-addresses" with a map of address → label
            track_msg = json.dumps({
                "action": "want",
                "data": ["blocks", "stats"],
            })
            await ws.send(track_msg)

            # Track each address individually (mempool.space format)
            # The WS supports "track-address" for single address monitoring
            # For multiple addresses, we use sequential track-address calls
            for addr in self._all_addresses:
                msg = json.dumps({"track-address": addr})
                await ws.send(msg)
                await asyncio.sleep(0.05)  # Don't flood

            logger.info("WhaleMonitor: subscribed to %d addresses", len(self._all_addresses))

            # Also start a background REST poller for addresses not well-served by WS
            poll_task = asyncio.create_task(self._rest_poll_loop())

            try:
                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                        await self._handle_ws_message(data)
                    except json.JSONDecodeError:
                        continue
            finally:
                poll_task.cancel()

    async def _handle_ws_message(self, data: dict) -> None:
        """Process a WebSocket message from mempool.space."""
        # Address transaction notification:
        # {"address-transactions": {"address": "bc1q...", "transactions": [...]}}
        addr_txs = data.get("address-transactions")
        if addr_txs:
            address = addr_txs.get("address", "")
            transactions = addr_txs.get("transactions", [])
            if address in self._watched_addresses:
                exchange, source = self._watched_addresses[address]
                for tx in transactions:
                    self._process_transaction(tx, address, exchange, source)
                self._recompute_state()
            return

        # Multi-address format: {"multi-address-transactions": {...}}
        multi = data.get("multi-address-transactions")
        if multi and isinstance(multi, dict):
            for address, transactions in multi.items():
                if address in self._watched_addresses:
                    exchange, source = self._watched_addresses[address]
                    for tx in (transactions if isinstance(transactions, list) else []):
                        self._process_transaction(tx, address, exchange, source)
            self._recompute_state()
            return

    def _process_transaction(
        self,
        tx: dict,
        watched_address: str,
        exchange: str,
        source: str,
    ) -> None:
        """Classify and record a transaction involving a watched address."""
        txid = tx.get("txid", "")
        if not txid or txid in self._known_txids:
            return

        direction, btc_amount = self._classify_tx(tx, watched_address, source)
        if direction is None or btc_amount < WHALE_MIN_BTC:
            return

        # Check if confirmed or mempool
        status = tx.get("status", {})
        confirmed = bool(status.get("confirmed", False))

        whale_tx = WhaleTransaction(
            txid=txid,
            exchange=exchange,
            direction=direction,
            btc_amount=btc_amount,
            detected_at=time.time(),
            confirmed=confirmed,
            source_label=source,
        )

        self._txs.append(whale_tx)
        self._known_txids.add(txid)

        logger.info(
            "WHALE %s: %s %s %.2f BTC (%s) tx=%s%s",
            "ALERT" if btc_amount >= 50 else "DETECT",
            exchange.upper(),
            direction.upper(),
            btc_amount,
            "confirmed" if confirmed else "MEMPOOL",
            txid[:16],
            f" [from {source}]" if source == "whale" else "",
        )

    def _classify_tx(
        self,
        tx: dict,
        watched_address: str,
        source: str,
    ) -> tuple[Optional[str], float]:
        """Classify a transaction as inflow or outflow relative to a watched address.

        For EXCHANGE wallets:
          - BTC flowing IN  = deposit to sell = BEARISH (inflow)
          - BTC flowing OUT = withdrawal       = BULLISH (outflow)

        For WHALE wallets:
          - BTC flowing OUT = whale moving coins (possibly to exchange) = BEARISH
          - BTC flowing IN  = whale accumulating = BULLISH
        """
        vin = tx.get("vin", [])
        vout = tx.get("vout", [])

        is_sender = False
        is_receiver = False
        received_sats = 0
        sent_sats = 0

        for inp in vin:
            prevout = inp.get("prevout", {})
            addr = prevout.get("scriptpubkey_address", "")
            if addr == watched_address:
                is_sender = True
                sent_sats += prevout.get("value", 0)

        for out in vout:
            addr = out.get("scriptpubkey_address", "")
            if addr == watched_address:
                is_receiver = True
                received_sats += out.get("value", 0)

        if source == "whale":
            # For whale wallets: outflow = bearish, inflow = bullish
            if is_sender and not is_receiver:
                return "inflow", sent_sats / SATS_PER_BTC  # Whale selling → bearish
            elif is_receiver and not is_sender:
                return "outflow", received_sats / SATS_PER_BTC  # Whale accumulating → bullish
        else:
            # For exchange wallets: inflow = bearish, outflow = bullish
            if is_receiver and not is_sender:
                return "inflow", received_sats / SATS_PER_BTC
            elif is_sender and not is_receiver:
                return "outflow", sent_sats / SATS_PER_BTC

        # Both sender and receiver (change output / consolidation)
        if is_sender and is_receiver:
            net = received_sats - sent_sats
            if abs(net) < WHALE_MIN_BTC * SATS_PER_BTC:
                return None, 0.0
            if net > 0:
                return "inflow", net / SATS_PER_BTC
            else:
                return "outflow", abs(net) / SATS_PER_BTC

        return None, 0.0

    # ── REST polling fallback ────────────────────────────────────────────

    async def _rest_poll_loop(self) -> None:
        """Fallback REST polling for addresses that WS might miss.

        Runs less frequently than the WS — just catches any gaps.
        """
        import ssl as _ssl
        try:
            import certifi
            get_ssl_context = lambda: _ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            get_ssl_context = _ssl.create_default_context
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(ssl=get_ssl_context()),
        ) as session:
            while True:
                try:
                    await self._rest_poll_all(session)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("WhaleMonitor REST poll error: %s", e)
                await asyncio.sleep(60.0)  # Every 60 seconds

    async def _rest_poll_all(self, session: aiohttp.ClientSession) -> None:
        """Poll mempool transactions for all watched addresses."""
        changed = False
        for addr, (exchange, source) in self._watched_addresses.items():
            try:
                url = f"{MEMPOOL_REST_URL}/address/{addr}/txs/mempool"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    txs = await resp.json()

                if isinstance(txs, list):
                    for tx in txs:
                        txid = tx.get("txid", "")
                        if txid and txid not in self._known_txids:
                            self._process_transaction(tx, addr, exchange, source)
                            changed = True
            except Exception:
                pass
            await asyncio.sleep(1.0)  # Rate limit

        if changed:
            self._recompute_state()

    # ── State computation ────────────────────────────────────────────────

    def _recompute_state(self) -> None:
        """Recompute whale flow state from rolling window."""
        now = time.time()
        cutoff = now - WHALE_WINDOW_S

        # Prune old transactions
        while self._txs and self._txs[0].detected_at < cutoff:
            self._txs.popleft()

        # Prune known txids periodically
        if len(self._known_txids) > 10000:
            recent_ids = {tx.txid for tx in self._txs}
            self._known_txids = recent_ids

        # Aggregate
        inflow_btc = 0.0
        outflow_btc = 0.0
        inflow_count = 0
        outflow_count = 0

        for tx in self._txs:
            if tx.direction == "inflow":
                inflow_btc += tx.btc_amount
                inflow_count += 1
            else:
                outflow_btc += tx.btc_amount
                outflow_count += 1

        total = inflow_btc + outflow_btc
        net = inflow_btc - outflow_btc  # positive = net inflow = bearish

        # Whale bias: -1.0 (strong outflow/bullish) to +1.0 (strong inflow/bearish)
        if total > 0:
            raw_bias = net / total  # -1 to +1
        else:
            raw_bias = 0.0

        # Signal strength: scales with total volume (more BTC moving = higher confidence)
        # Tanh scaling: 50 BTC → 0.46, 100 BTC → 0.76, 200 BTC → 0.96
        strength = abs(raw_bias) * min(1.0, math.tanh(total / 100.0))

        # Build recent tx list (last 5 for logging)
        recent = list(self._txs)[-5:]

        self._state = WhaleFlowState(
            inflow_count=inflow_count,
            outflow_count=outflow_count,
            total_count=inflow_count + outflow_count,
            inflow_btc=inflow_btc,
            outflow_btc=outflow_btc,
            net_flow_btc=net,
            whale_bias=raw_bias,
            signal_strength=strength,
            recent_txs=recent,
            last_tx_at=self._txs[-1].detected_at if self._txs else 0,
            updated_at=now,
        )

        self._ref["state"] = self._state

        if self._txs:
            logger.debug(
                "WhaleMonitor state: txs=%d in=%.1f BTC out=%.1f BTC net=%.1f bias=%.2f str=%.2f",
                len(self._txs), inflow_btc, outflow_btc, net, raw_bias, strength,
            )
