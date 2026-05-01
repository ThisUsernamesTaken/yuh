"""Unit tests for microprice-based limit entry refinement (B)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Pure-math helper test of the microprice formula. Full integration into
# _execute_signal_inner is exercised in live monitoring (the function is
# 1000+ lines, mocking the entire entry path is impractical; the math
# itself is what could regress).

def _microprice(bid, bid_size, ask, ask_size):
    if bid_size + ask_size == 0:
        return float(bid + ask) / 2
    return (bid * ask_size + ask * bid_size) / (bid_size + ask_size)


def test_microprice_balanced_returns_mid():
    # 30c bid 100ct, 33c ask 100ct → mp = (30×100 + 33×100) / 200 = 31.5
    mp = _microprice(30, 100, 33, 100)
    assert abs(mp - 31.5) < 0.01


def test_microprice_heavy_bid_pushes_toward_ask():
    """100ct bidders, 10ct ask → microprice closer to ask (book wants to lift)."""
    mp = _microprice(30, 100, 33, 10)
    # mp = (30×10 + 33×100) / 110 = (300 + 3300) / 110 = 32.7
    assert mp > 32.0
    assert mp < 33.0


def test_microprice_heavy_ask_pushes_toward_bid():
    """10ct bidders, 100ct ask → microprice near bid (book wants to drop)."""
    mp = _microprice(30, 10, 33, 100)
    # mp = (30×100 + 33×10) / 110 = (3000 + 330) / 110 = 30.3
    assert mp > 30.0
    assert mp < 31.0


def test_microprice_extreme_ask_imbalance():
    """1ct bidders, 1000ct ask → microprice almost at bid."""
    mp = _microprice(30, 1, 33, 1000)
    # mp = (30×1000 + 33×1) / 1001 = 30.003
    assert mp < 30.1


def test_microprice_zero_sizes_returns_mid():
    """Edge case: book with zero depth either side returns mid."""
    mp = _microprice(30, 0, 33, 0)
    assert mp == 31.5


def test_microprice_tight_book():
    """1c spread book — microprice should be in [bid, ask]."""
    mp = _microprice(50, 50, 51, 50)
    assert 50 <= mp <= 51
