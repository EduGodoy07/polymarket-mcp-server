"""
Technical indicators for strategy signal generation.

All computations are pure functions that operate on price lists —
no external deps (no pandas/numpy). Strategies import these to
filter entry signals.

Available:
- rsi(prices, period=14)     → 0-100 float
- atr(highs, lows, closes, period=14)  → float
- ema(prices, period)        → float
- vwap(prices, volumes)      → float
- bollinger(prices, period, std_dev)  → (upper, mid, lower)
- divergence_score(price_a, price_b) → float (cross-source spread %)
"""
from __future__ import annotations

import math
from collections import deque
from typing import Deque, List, Optional, Tuple


# ── RSI ────────────────────────────────────────────────────────────────────

def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Relative Strength Index.

    Args:
        prices: List of closing prices (oldest first), min length = period + 1
        period: RSI period (default 14)

    Returns:
        RSI value 0-100, or None if insufficient data
    """
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Initial averages
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for remaining periods
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── ATR ────────────────────────────────────────────────────────────────────

def atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Optional[float]:
    """
    Average True Range.

    Args:
        highs, lows, closes: Equal-length price lists (oldest first)
        period: ATR period

    Returns:
        ATR value or None if insufficient data
    """
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None

    true_ranges = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    # Initial ATR = simple average
    current_atr = sum(true_ranges[:period]) / period

    # Wilder smoothing
    for tr in true_ranges[period:]:
        current_atr = (current_atr * (period - 1) + tr) / period

    return current_atr


# ── EMA ────────────────────────────────────────────────────────────────────

def ema(prices: List[float], period: int) -> Optional[float]:
    """
    Exponential Moving Average (most recent value).

    Args:
        prices: Price list (oldest first)
        period: EMA period

    Returns:
        Latest EMA value or None if insufficient data
    """
    if len(prices) < period:
        return None

    k = 2.0 / (period + 1)
    value = sum(prices[:period]) / period  # seed with SMA
    for price in prices[period:]:
        value = price * k + value * (1 - k)
    return value


# ── VWAP ───────────────────────────────────────────────────────────────────

def vwap(prices: List[float], volumes: List[float]) -> Optional[float]:
    """
    Volume-Weighted Average Price.

    Args:
        prices: Price list
        volumes: Corresponding volume list

    Returns:
        VWAP value or None if empty
    """
    if not prices or len(prices) != len(volumes):
        return None
    total_vol = sum(volumes)
    if total_vol == 0:
        return None
    return sum(p * v for p, v in zip(prices, volumes)) / total_vol


# ── Bollinger Bands ────────────────────────────────────────────────────────

def bollinger(
    prices: List[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> Optional[Tuple[float, float, float]]:
    """
    Bollinger Bands.

    Args:
        prices: Price list (oldest first)
        period: MA period
        std_dev: Number of standard deviations

    Returns:
        (upper, middle, lower) or None if insufficient data
    """
    if len(prices) < period:
        return None

    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    sigma = math.sqrt(variance)

    return (middle + std_dev * sigma, middle, middle - std_dev * sigma)


# ── Cross-source divergence ────────────────────────────────────────────────

def divergence_score(price_a: float, price_b: float) -> float:
    """
    Compute relative spread between two price sources.

    Returns:
        Fraction [0, 1] — e.g. 0.001 = 0.1% divergence
    """
    if price_a <= 0 or price_b <= 0:
        return 0.0
    avg = (price_a + price_b) / 2
    return abs(price_a - price_b) / avg


# ── Rolling indicator helper ───────────────────────────────────────────────

class RollingRSI:
    """
    Incremental RSI that updates with each new price tick.
    More efficient than recomputing from full history.
    """

    def __init__(self, period: int = 14):
        self.period = period
        self._prices: Deque[float] = deque(maxlen=period * 3)
        self._value: Optional[float] = None

    def update(self, price: float) -> Optional[float]:
        self._prices.append(price)
        if len(self._prices) >= self.period + 1:
            self._value = rsi(list(self._prices), self.period)
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value

    @property
    def is_overbought(self) -> bool:
        return self._value is not None and self._value > 70

    @property
    def is_oversold(self) -> bool:
        return self._value is not None and self._value < 30


class RollingATR:
    """Incremental ATR tracker."""

    def __init__(self, period: int = 14):
        self.period = period
        self._highs: Deque[float] = deque(maxlen=period * 3)
        self._lows: Deque[float] = deque(maxlen=period * 3)
        self._closes: Deque[float] = deque(maxlen=period * 3)
        self._value: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        if len(self._closes) >= self.period + 1:
            self._value = atr(
                list(self._highs),
                list(self._lows),
                list(self._closes),
                self.period,
            )
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value


# ── Strategy signal helpers ────────────────────────────────────────────────

def signal_strength(
    rsi_value: Optional[float],
    atr_value: Optional[float],
    spread: Optional[float],
    divergence: float = 0.0,
) -> float:
    """
    Composite signal score from 0 (no edge) to 1 (strong edge).

    Combines:
    - RSI extremes (oversold/overbought)
    - ATR (volatility context)
    - Order book spread (tight = better fills)
    - Cross-source divergence (leading indicator)

    Returns:
        Float 0-1. Trade only when score > threshold (e.g. 0.6)
    """
    score = 0.0
    components = 0

    if rsi_value is not None:
        # Score high when RSI is at extremes
        if rsi_value <= 30:
            score += (30 - rsi_value) / 30  # max 1.0 at RSI=0
        elif rsi_value >= 70:
            score += (rsi_value - 70) / 30  # max 1.0 at RSI=100
        else:
            score += 0.0
        components += 1

    if spread is not None:
        # Tight spread = easier fill, higher score
        spread_score = max(0.0, 1.0 - spread / 0.10)  # 0 spread → 1.0; 10% → 0.0
        score += spread_score
        components += 1

    if divergence > 0:
        # Any divergence is a positive signal
        div_score = min(1.0, divergence / 0.005)  # 0.5% divergence → max score
        score += div_score
        components += 1

    return score / components if components > 0 else 0.0
