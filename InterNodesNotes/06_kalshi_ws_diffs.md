# Step 6: `kalshi_ws.py` modifications

Add 4 helper methods to the `LocalOrderBook` class for orderbook density
analysis. These are pure read-only methods, no behavior change.

---

## Change: Append helpers inside `LocalOrderBook` class

**Find:**
```python
    @property
    def is_ready(self) -> bool:
        return self._initialized

    @staticmethod
    def _parse_level(level) -> tuple[int, int]:
```

**Replace with:**
```python
    @property
    def is_ready(self) -> bool:
        return self._initialized

    # ── Density / depth helpers (2026-04-30) ────────────────────────────
    # Used by entry-flow gate + stop-persistence gate to evaluate how
    # well-supported a price level is. Thin density at trigger price =
    # real risk of falling-knife move. Thick density = noise filter.

    def density(self, side: str, levels: int = 5) -> dict:
        """Return depth of the bid stack for `side` (top N price levels).

        side: "yes" or "no" — which bid stack to read.
        Returns {top_volume, level_count, max_level_volume, levels: [(px, qty), ...]}.
        Levels are sorted highest-price-first (top of book first).
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        if not book:
            return {"top_volume": 0, "level_count": 0,
                    "max_level_volume": 0, "levels": []}
        sorted_levels = sorted(book.items(), key=lambda kv: -kv[0])[:max(1, levels)]
        top_volume = sum(qty for _, qty in sorted_levels)
        max_level_vol = max((qty for _, qty in sorted_levels), default=0)
        return {
            "top_volume": top_volume,
            "level_count": len(sorted_levels),
            "max_level_volume": max_level_vol,
            "levels": sorted_levels,
        }

    def volume_at_or_below(self, side: str, threshold_cents: int) -> int:
        """Sum bid quantity for `side` at prices <= threshold_cents.

        For a YES position with stop trigger at 24c, this answers:
          "How much resting YES-bid volume is there at 24c or below?"
        Thick volume below trigger = real support, defer stop. Thin = real
        risk, fire stop. Used by stop-persistence gate.
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        return sum(qty for px, qty in book.items() if px <= threshold_cents)

    def volume_at_or_above(self, side: str, threshold_cents: int) -> int:
        """Sum bid quantity for `side` at prices >= threshold_cents.

        Used to evaluate same-side bid wall at-or-above entry — strong
        wall above entry on our side = TP feasibility for protective-order
        mode.
        """
        side_norm = (side or "").lower()
        book = self.yes_bids if side_norm == "yes" else self.no_bids
        return sum(qty for px, qty in book.items() if px >= threshold_cents)

    def imbalance_ratio(self, levels: int = 5) -> float:
        """Top-N depth ratio of YES bids vs NO bids. Range [0, 1].

        > 0.5 = more YES-buyers (bullish bias)
        < 0.5 = more NO-buyers (bearish bias)
        = 0.5 = balanced.
        """
        yd = self.density("yes", levels)["top_volume"]
        nd = self.density("no", levels)["top_volume"]
        total = yd + nd
        if total <= 0:
            return 0.5
        return yd / total

    @staticmethod
    def _parse_level(level) -> tuple[int, int]:
```

---

## Verification

```python
import kalshi_ws
ws_book = kalshi_ws.LocalOrderBook()
ws_book.apply_snapshot({'yes': [['0.32', 100], ['0.30', 200], ['0.28', 50]], 'no': [['0.65', 500], ['0.62', 100]]})
print(ws_book.density('yes', 5))
# Expected: {'top_volume': 350, 'level_count': 3, 'max_level_volume': 200, 'levels': [(32, 100), (30, 200), (28, 50)]}
print(ws_book.imbalance_ratio())
# Expected: ~0.368 (350 / (350 + 600))
```
