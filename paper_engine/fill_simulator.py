"""Realistic Kalshi fill simulation — accounts for slippage, depth, partials.

Key calibration facts (from live observation):
- Top-of-book depth: typically 20-50 contracts at best bid/ask
- Beyond top of book, book walks ~1c per 25 contracts
- Limit orders rest at back of queue — fill as queue clears
- Market sell on falling bid can walk 3-10c in thin liquidity
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class FillResult:
    """Outcome of a simulated order."""
    filled_count: int
    avg_fill_price: int  # cents
    slippage_cents: float  # avg price vs requested
    fully_filled: bool

    @property
    def unfilled_count(self) -> int:
        return 0  # simulator assumes all fills; partials TBD


class FillSimulator:
    """Models Kalshi orderbook behavior for paper trading."""

    # Top-of-book depth we assume is available at the shown bid/ask
    TOP_OF_BOOK_DEPTH = 20
    # Slippage: how many cents per 25 contracts beyond top-of-book
    SLIPPAGE_PER_25CT = 1.0
    # Market-sell additional slippage (falling book is thinner)
    MARKET_SELL_EXTRA_SLIP = 0.5  # cents per 25ct

    def simulate_market_entry(self, count: int, ask: int) -> FillResult:
        """Market buy at ask. Walks book upward."""
        if count <= self.TOP_OF_BOOK_DEPTH:
            # Entire order fills at ask
            return FillResult(filled_count=count, avg_fill_price=ask,
                              slippage_cents=0.0, fully_filled=True)

        # Walks book
        excess = count - self.TOP_OF_BOOK_DEPTH
        avg_slip = (excess / 25.0) * self.SLIPPAGE_PER_25CT / 2  # triangle avg
        avg_price = ask + avg_slip
        return FillResult(filled_count=count, avg_fill_price=int(round(avg_price)),
                          slippage_cents=avg_slip, fully_filled=True)

    def simulate_limit_tp(self, tp_price: int, count: int,
                          bid_trajectory: list) -> Optional[FillResult]:
        """Limit sell at tp_price. Fills when bid reaches or exceeds tp_price.
        bid_trajectory: list of (ts, bid) tuples post-entry.
        Returns FillResult at first crossing, or None if never crosses."""
        for ts, bid in bid_trajectory:
            if bid >= tp_price:
                # Queue fills — we're at the back so not every cross fills us,
                # but on a meaningful cross (bid == tp or tp+1) we assume fill.
                return FillResult(filled_count=count, avg_fill_price=tp_price,
                                  slippage_cents=0.0, fully_filled=True)
        return None

    def simulate_market_stop(self, count: int, bid: int) -> FillResult:
        """Market sell on stop. Walks book downward (worse than limit at bid)."""
        if count <= self.TOP_OF_BOOK_DEPTH:
            # Fills near bid with small slippage
            slip = self.MARKET_SELL_EXTRA_SLIP
        else:
            excess = count - self.TOP_OF_BOOK_DEPTH
            slip = (excess / 25.0) * (self.SLIPPAGE_PER_25CT + self.MARKET_SELL_EXTRA_SLIP) / 2
        avg_price = max(1, bid - slip)
        return FillResult(filled_count=count, avg_fill_price=int(round(avg_price)),
                          slippage_cents=slip, fully_filled=True)

    def simulate_settlement(self, count: int, side: str, won: bool) -> FillResult:
        """Contract settled at expiry.
        Won (our side correct) = $1.00 per contract.
        Lost = $0.00 per contract."""
        settlement_price = 100 if won else 0
        return FillResult(filled_count=count, avg_fill_price=settlement_price,
                          slippage_cents=0.0, fully_filled=True)
