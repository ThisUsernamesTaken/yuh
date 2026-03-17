# kalshi_client.py — Kalshi API client for BTC binary contract execution
#
# Production endpoint: https://api.elections.kalshi.com/trade-api/v2
# Auth: RSA-PSS signature per request (NOT a simple bearer token).
#
# Required env vars:
#   KALSHI_API_KEY          — your API key ID (UUID from Kalshi account)
#   KALSHI_PRIVATE_KEY      — PEM-encoded RSA private key (full content)
#   KALSHI_PRIVATE_KEY_PATH — path to PEM file (alternative to KALSHI_PRIVATE_KEY)
#
# Target contract series:
#   KXBTC15M — "Bitcoin price up down" — YES resolves if BTC closes >= open
#               for a 15-minute window.  One market open at a time.
#               CALL signal → buy YES side
#               PUT  signal → buy NO  side

import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class KalshiContract:
    """A single Kalshi binary contract."""
    ticker: str             # e.g. "KXBTC15M-26MAR130245-45"
    title: str
    status: str             # "open", "closed", "settled"
    yes_ask: float          # YES ask price (0.0–1.0 dollars)
    yes_bid: float          # YES bid price
    no_ask: float           # NO ask price
    no_bid: float
    volume: int             # Total contracts traded
    expiry_ts: int          # Unix ms when contract resolves
    result: Optional[str]   # "yes" / "no" / None (pending)

    @property
    def minutes_to_expiry(self) -> float:
        now_ms = int(time.time() * 1000)
        return max(0.0, (self.expiry_ts - now_ms) / 60_000)

    @property
    def is_tradeable(self) -> bool:
        return self.status in ("open", "active") and self.minutes_to_expiry > 0.5


@dataclass
class KalshiOrder:
    """A placed Kalshi order."""
    order_id: str
    ticker: str
    side: str               # "yes" or "no"
    count: int              # Number of contracts
    price: int              # Price in cents (1-99)
    status: str             # "resting", "executed", "canceled"
    filled_count: int
    average_price: Optional[float]
    created_ts: int         # Unix ms


@dataclass
class KalshiBalance:
    balance: int            # Available balance in cents
    portfolio_value: int    # Total portfolio value in cents


@dataclass
class KalshiOrderBook:
    """Full order book depth for a single Kalshi contract.

    Kalshi reports two bid stacks:
      yes_bids — buyers willing to purchase YES (sorted price descending)
      no_bids  — buyers willing to purchase NO  (sorted price descending)

    Key relationships:
      best YES ask = 100 - best_no_bid   (buying YES costs this many cents)
      best YES bid = best_yes_bid        (selling YES earns this many cents)
      spread = best_yes_ask - best_yes_bid
    """
    ticker: str
    fetched_at_ms: int
    yes_bids: list          # [(price_cents: int, quantity: int), ...] descending
    no_bids: list           # [(price_cents: int, quantity: int), ...] descending

    # ── Derived best prices ───────────────────────────────────────────────

    @property
    def best_yes_bid(self) -> int:
        """Highest price (cents) a buyer will pay for YES — we can sell here."""
        return self.yes_bids[0][0] if self.yes_bids else 0

    @property
    def best_yes_ask(self) -> int:
        """Lowest price (cents) to buy YES — derived from best NO bid."""
        return (100 - self.no_bids[0][0]) if self.no_bids else 99

    @property
    def best_no_bid(self) -> int:
        """Highest price (cents) a buyer will pay for NO — we can sell here."""
        return self.no_bids[0][0] if self.no_bids else 0

    @property
    def best_no_ask(self) -> int:
        """Lowest price (cents) to buy NO — derived from best YES bid."""
        return (100 - self.yes_bids[0][0]) if self.yes_bids else 99

    @property
    def spread_cents(self) -> int:
        """YES bid-ask spread in cents."""
        return max(0, self.best_yes_ask - self.best_yes_bid)

    @property
    def mid_cents(self) -> float:
        """Mid-market price in cents."""
        return (self.best_yes_bid + self.best_yes_ask) / 2.0

    @property
    def book_age_ms(self) -> int:
        """Milliseconds since this book was fetched from the exchange."""
        return int(time.time() * 1000) - self.fetched_at_ms

    @property
    def top_yes_qty(self) -> int:
        """Top-of-book YES bid quantity (demand for YES)."""
        return self.yes_bids[0][1] if self.yes_bids else 0

    @property
    def top_no_qty(self) -> int:
        """Top-of-book NO bid quantity (supply of YES / demand for NO)."""
        return self.no_bids[0][1] if self.no_bids else 0

    @property
    def imbalance(self) -> float:
        """Signed top-of-book imbalance: +1 = all YES demand, -1 = all NO demand.
        Positive is bullish for YES (price pressure upward).
        """
        total = self.top_yes_qty + self.top_no_qty
        if total == 0:
            return 0.0
        return (self.top_yes_qty - self.top_no_qty) / total

    @property
    def microprice_cents(self) -> float:
        """Imbalance-weighted midprice. Closer to ask when YES demand dominates.
        Formula: (ask * yes_qty + bid * no_qty) / (yes_qty + no_qty)
        """
        v_b, v_a = self.top_yes_qty, self.top_no_qty
        total = v_b + v_a
        if total == 0:
            return self.mid_cents
        return (self.best_yes_ask * v_b + self.best_yes_bid * v_a) / total

    # ── Depth helpers ─────────────────────────────────────────────────────

    def liquidity_within(self, side: str, n_cents: int) -> int:
        """Total contracts available within n_cents of the best price on side.

        side: "yes" (buying YES) or "no" (buying NO).
        For buying YES we walk the no_bids stack (they are our sellers);
        for buying NO we walk the yes_bids stack.
        """
        if side == "yes":
            # Walking no_bids descending: best YES ask = 100 - no_bids[0][0]
            floor_no_bid = self.best_no_bid - n_cents
            return sum(qty for price, qty in self.no_bids if price >= floor_no_bid)
        else:
            # Walking yes_bids descending: best NO ask = 100 - yes_bids[0][0]
            floor_yes_bid = self.best_yes_bid - n_cents
            return sum(qty for price, qty in self.yes_bids if price >= floor_yes_bid)

    def fill_cost(self, side: str, count: int) -> tuple:
        """Estimate average fill price (cents) and total cost for market-buying `count` contracts.

        Returns (avg_price_cents, total_cost_cents, filled_count).
        filled_count may be less than count if the book is too thin.
        """
        stack = self.no_bids if side == "yes" else self.yes_bids
        remaining = count
        total_cost = 0
        filled = 0
        for price, qty in stack:
            effective_price = (100 - price) if side == "yes" else (100 - price)
            take = min(remaining, qty)
            total_cost += take * effective_price
            filled += take
            remaining -= take
            if remaining == 0:
                break
        avg = (total_cost / filled) if filled > 0 else 0
        return avg, total_cost, filled

    def total_yes_liquidity(self) -> int:
        """Total YES-side buy contracts in the book (qty of all YES bids)."""
        return sum(qty for _, qty in self.yes_bids)

    def total_no_liquidity(self) -> int:
        """Total NO-side buy contracts in the book (qty of all NO bids)."""
        return sum(qty for _, qty in self.no_bids)


# ─────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────

class KalshiClient:
    """Async Kalshi API client with RSA-PSS request signing.

    Kalshi API v2 requires every authenticated request to be signed with
    an RSA private key.  The signature is computed over:
        {timestamp_ms}{HTTP_METHOD}{/path/without/query}

    Usage:
        client = KalshiClient(key_id="uuid...", private_key_pem="-----BEGIN...")
        async with client:
            balance = await client.get_balance()
            contracts = await client.find_btc_contracts()
            order = await client.place_order(ticker, side="yes", count=1, price=65)
    """

    PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
    DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_PATH_PREFIX = "/trade-api/v2"
    DEMO_PATH_PREFIX = "/trade-api/v2"

    def __init__(
        self,
        key_id: str,
        private_key_pem: str,
        demo: bool = False,
    ) -> None:
        """
        Args:
            key_id:          Kalshi API key ID (UUID from account settings).
            private_key_pem: PEM-encoded RSA private key string.
            demo:            If True, use the Kalshi demo environment.
        """
        self._key_id = key_id
        pem_bytes = (
            private_key_pem.encode()
            if isinstance(private_key_pem, str)
            else private_key_pem
        )
        self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        self._base = self.DEMO_BASE if demo else self.PROD_BASE
        self._path_prefix = self.DEMO_PATH_PREFIX if demo else self.PROD_PATH_PREFIX
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "KalshiClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self

    async def __aexit__(self, *args) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ── Signing ──────────────────────────────────────────────────────────

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS sign a request. Path must NOT include query parameters."""
        path_no_query = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        # Signature must cover the full path including /trade-api/v2 prefix
        full_path = self._path_prefix + path
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, full_path),
            "Content-Type": "application/json",
        }

    # ── Internal request helper ──────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make a signed API request. Returns parsed JSON body."""
        if self._session is None:
            raise RuntimeError("KalshiClient must be used as async context manager")

        url = f"{self._base}{path}"
        headers = self._auth_headers(method.upper(), path)
        async with self._session.request(
            method, url, headers=headers, **kwargs
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status not in (200, 201):
                raise KalshiAPIError(resp.status, path, body)
            return body

    # ── Account ──────────────────────────────────────────────────────────

    async def get_balance(self) -> KalshiBalance:
        """Fetch current account balance."""
        data = await self._request("GET", "/portfolio/balance")
        return KalshiBalance(
            balance=data.get("balance", 0),
            portfolio_value=data.get("portfolio_value", 0),
        )

    # ── Contract discovery ────────────────────────────────────────────────

    async def find_btc_contracts(
        self,
        window_minutes: int = 15,
        min_minutes_remaining: float = 2.0,
    ) -> list[KalshiContract]:
        """Find open KXBTC15M contracts with sufficient time remaining.

        KXBTC15M is a rolling 15-minute BTC up/down market.  There is
        normally only one market open at a time.  YES resolves if the 15-min
        close price >= open price.

        Args:
            window_minutes:        Target window duration (kept for API compat).
            min_minutes_remaining: Skip contracts expiring in less than this.

        Returns:
            List of tradeable KalshiContracts, sorted by expiry ascending.
        """
        data = await self._request(
            "GET", "/markets",
            params={
                "series_ticker": "KXBTC15M",
                "limit": 100,
            }
        )

        now_ms = int(time.time() * 1000)
        contracts: list[KalshiContract] = []
        for m in data.get("markets", []):
            close_time = m.get("close_time", "")
            open_time = m.get("open_time", "")
            if not close_time or not open_time:
                continue

            expiry_ts = _parse_iso_to_ms(close_time)
            open_ts = _parse_iso_to_ms(open_time)
            minutes_left = (expiry_ts - now_ms) / 60_000

            # Only consider markets that are currently open (open_time <= now < close_time)
            if open_ts > now_ms:
                continue
            if minutes_left < min_minutes_remaining:
                continue

            contracts.append(KalshiContract(
                ticker=m["ticker"],
                title=m.get("title", ""),
                status=m.get("status", ""),
                yes_ask=float(m.get("yes_ask_dollars", "0.5")),
                yes_bid=float(m.get("yes_bid_dollars", "0.5")),
                no_ask=float(m.get("no_ask_dollars", "0.5")),
                no_bid=float(m.get("no_bid_dollars", "0.5")),
                volume=int(float(m.get("volume_fp", m.get("volume", 0)))),
                expiry_ts=expiry_ts,
                result=m.get("result") or None,
            ))

        contracts.sort(key=lambda c: c.expiry_ts)
        return [c for c in contracts if c.is_tradeable]

    async def get_contract(self, ticker: str) -> KalshiContract:
        """Fetch a specific contract by ticker."""
        data = await self._request("GET", f"/markets/{ticker}")
        m = data["market"]
        return KalshiContract(
            ticker=m["ticker"],
            title=m.get("title", ""),
            status=m.get("status", ""),
            yes_ask=float(m.get("yes_ask_dollars", "0.5")),
            yes_bid=float(m.get("yes_bid_dollars", "0.5")),
            no_ask=float(m.get("no_ask_dollars", "0.5")),
            no_bid=float(m.get("no_bid_dollars", "0.5")),
            volume=int(float(m.get("volume_fp", m.get("volume", 0)))),
            expiry_ts=_parse_iso_to_ms(m.get("close_time", "")),
            result=m.get("result") or None,
        )

    async def get_orderbook(self, ticker: str) -> KalshiOrderBook:
        """Fetch full order book depth for a contract.

        Returns a KalshiOrderBook with yes_bids and no_bids sorted price-descending.
        Each level is (price_cents: int, quantity: int).
        """
        data = await self._request("GET", f"/markets/{ticker}/orderbook")

        # Kalshi v2 uses "orderbook_fp" with "yes_dollars"/"no_dollars" (dollar floats).
        # Older / demo responses may use "orderbook" with "yes"/"no" (cent ints).
        book = data.get("orderbook_fp") or data.get("orderbook", {})
        yes_key = "yes_dollars" if "yes_dollars" in book else "yes"
        no_key  = "no_dollars"  if "no_dollars"  in book else "no"

        def parse_levels(raw: list, dollar_prices: bool) -> list:
            """Parse [[price, qty], ...] into [(price_cents, qty), ...] sorted descending."""
            levels = []
            for level in (raw or []):
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    price_raw, qty = level[0], level[1]
                else:
                    continue
                price_cents = round(float(price_raw) * 100) if dollar_prices else int(price_raw)
                levels.append((price_cents, int(float(qty))))
            levels.sort(key=lambda x: x[0], reverse=True)
            return [(p, q) for p, q in levels if q > 0]

        dollar_prices = yes_key == "yes_dollars"
        return KalshiOrderBook(
            ticker=ticker,
            fetched_at_ms=int(time.time() * 1000),
            yes_bids=parse_levels(book.get(yes_key, []), dollar_prices),
            no_bids=parse_levels(book.get(no_key, []), dollar_prices),
        )

    # ── Orders ────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,          # "yes" (CALL) or "no" (PUT)
        count: int,
        price: Optional[int] = None,  # Cents (1-99). Required for limit orders; ignored for market.
        order_type: str = "limit",
        action: str = "buy",  # "buy" to open, "sell" to close a position
    ) -> KalshiOrder:
        """Place or close a KXBTC15M order.

        CALL signal → action="buy", side="yes", order_type="market"
        PUT  signal → action="buy", side="no",  order_type="market"
        Reversal exit  → action="sell", order_type="market"
        Take-profit    → action="sell", order_type="limit", price=ask_cents

        Args:
            ticker:     Market ticker (e.g. "KXBTC15M-26MAR130245-45").
            side:       "yes" or "no".
            count:      Number of contracts.
            price:      Limit price in cents. Required for limit orders.
            order_type: "limit" or "market".
            action:     "buy" (open) or "sell" (close).
        """
        payload: dict = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }
        if order_type == "limit":
            payload["yes_price" if side == "yes" else "no_price"] = price

        data = await self._request("POST", "/portfolio/orders", json=payload)
        return _parse_order(data["order"])

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Returns True if successful."""
        try:
            await self._request("DELETE", f"/portfolio/orders/{order_id}")
            return True
        except KalshiAPIError as e:
            logger.warning("Cancel order %s failed: %s", order_id, e)
            return False

    async def get_order(self, order_id: str) -> KalshiOrder:
        """Fetch current status of an order."""
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return _parse_order(data["order"])

    async def get_positions(self) -> list[dict]:
        """Fetch all open positions."""
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _parse_iso_to_ms(iso: str) -> int:
    """Parse an ISO datetime string to Unix milliseconds."""
    if not iso:
        return 0
    iso = iso.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return 0


def _parse_order(o: dict) -> KalshiOrder:
    # Kalshi API v2 uses fill_count_fp (string) instead of filled_count
    fill_count = int(float(o.get("fill_count_fp", o.get("filled_count", 0)) or 0))
    # Average fill price in cents, derived from taker + maker fill costs
    taker_cost = float(o.get("taker_fill_cost_dollars", 0) or 0)
    maker_cost = float(o.get("maker_fill_cost_dollars", 0) or 0)
    total_cost = taker_cost + maker_cost
    avg_price_cents = (total_cost / fill_count * 100) if fill_count > 0 else None
    # Limit price: use dollars field (returned as string) then convert to cents
    side = o.get("side", "")
    price_dollars = float(
        o.get("no_price_dollars" if side == "no" else "yes_price_dollars", "0") or 0
    )
    return KalshiOrder(
        order_id=o.get("order_id", ""),
        ticker=o.get("ticker", ""),
        side=side,
        count=int(float(o.get("initial_count_fp", o.get("count", 0)) or 0)),
        price=round(price_dollars * 100),
        status=o.get("status", ""),
        filled_count=fill_count,
        average_price=avg_price_cents,
        created_ts=_parse_iso_to_ms(o.get("created_time", "")),
    )


class KalshiAPIError(Exception):
    """Raised when the Kalshi API returns a non-2xx status."""
    def __init__(self, status: int, path: str, body: dict) -> None:
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"Kalshi API {status} on {path}: {body}")
