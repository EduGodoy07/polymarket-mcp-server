"""
Built-in example strategies.

These are reference implementations — not production-ready edges.
Write your own by subclassing BaseStrategy in a separate file.

Available:
  OrderbookSpreadStrategy  — trades when spread is tight + RSI extreme
  DivergenceScalpStrategy  — trades on cross-source BTC price divergence
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Type

from .strategy import BaseStrategy, StrategyAPI, Side
from . import indicators

logger = logging.getLogger(__name__)


class OrderbookSpreadStrategy(BaseStrategy):
    """
    Enters when:
    1. Order book spread < 0.03 (tight = good fill)
    2. RSI < 35 (oversold YES → expect bounce → buy YES)
       or RSI > 65 (overbought YES → buy NO)
    3. At least 60 seconds remaining in the window

    Exit: take-profit at +15% from entry, stop-loss at -8%
    """

    name = "orderbook_spread"

    def __init__(self, rsi_period: int = 10, size_usd: float = 15.0):
        self.rsi_period = rsi_period
        self.size_usd = size_usd
        self._rsi = indicators.RollingRSI(period=rsi_period)
        self._price_history = []

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        # Warm up RSI with early price observations
        await self._warmup(api)

        if api.seconds_remaining < 60:
            logger.info(f"[{self.name}] Less than 60s remaining — skipping entry")
            return

        ob = await api.orderbook(depth=5)
        rsi_val = self._rsi.value
        spread = ob.spread

        if spread is None or rsi_val is None:
            logger.info(f"[{self.name}] Insufficient data (spread={spread}, rsi={rsi_val})")
            return

        logger.info(f"[{self.name}] spread={spread:.4f} RSI={rsi_val:.1f}")

        if spread > 0.03:
            logger.info(f"[{self.name}] Spread too wide ({spread:.4f}) — no trade")
            return

        entry_price = await api.price()

        if rsi_val < 35 and ob.best_ask:
            # Buy YES — expect price to recover
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 0.92)
                api.set_take_profit(entry_price * 1.15)
                logger.info(f"[{self.name}] Entered YES @ {ob.best_ask:.4f}")

        elif rsi_val > 65 and ob.best_ask:
            # Buy NO — expect YES price to fall
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 1.08)
                api.set_take_profit(entry_price * 0.85)
                logger.info(f"[{self.name}] Entered NO @ market")
        else:
            logger.info(f"[{self.name}] No signal — RSI neutral ({rsi_val:.1f})")

    async def _warmup(self, api: StrategyAPI) -> None:
        """Collect 15 price ticks over ~15 seconds to seed the RSI."""
        for _ in range(15):
            price = await api.price()
            self._rsi.update(price)
            self._price_history.append(price)
            await asyncio.sleep(1.0)


class DivergenceScalpStrategy(BaseStrategy):
    """
    Uses cross-source BTC divergence as a leading signal.

    When Binance price leads Coinbase upward by >0.1%:
      → buy YES (price-to-beat likely to be exceeded)
    When Binance leads downward:
      → buy NO

    Exits at +12% profit or -6% stop-loss.
    """

    name = "divergence_scalp"

    def __init__(self, size_usd: float = 15.0, min_divergence: float = 0.001):
        self.size_usd = size_usd
        self.min_divergence = min_divergence

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        # Get divergence signal from price feed (attached to api slot)
        divergence = await self._get_divergence(api)

        if divergence is None:
            logger.info(f"[{self.name}] No divergence data available")
            return

        if abs(divergence) < self.min_divergence:
            logger.info(
                f"[{self.name}] Divergence {divergence:.4f} below threshold "
                f"{self.min_divergence:.4f}"
            )
            return

        ob = await api.orderbook(depth=3)
        entry_price = ob.mid or 0.5

        if divergence > 0:
            # Binance leading up → buy YES
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 0.94)
                api.set_take_profit(entry_price * 1.12)
                logger.info(
                    f"[{self.name}] YES entry — divergence={divergence:.4f} "
                    f"@ {ob.best_ask:.4f}"
                )
        else:
            # Binance leading down → buy NO
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 1.06)
                api.set_take_profit(entry_price * 0.88)
                logger.info(
                    f"[{self.name}] NO entry — divergence={divergence:.4f}"
                )

    async def _get_divergence(self, api: StrategyAPI) -> Optional[float]:
        """
        Read latest divergence from the price feed if attached to the slot.
        Returns positive for bullish (Binance > Coinbase), negative for bearish.
        """
        try:
            slot = api._slot
            if hasattr(slot, "_engine") and slot._engine and slot._engine.price_feeds:
                feed = slot._engine.price_feeds
                binance = feed.price_by_source("binance")
                coinbase = feed.price_by_source("coinbase")
                if binance and coinbase:
                    avg = (binance + coinbase) / 2
                    return (binance - coinbase) / avg
        except Exception:
            pass
        return None


# ── Registry ─────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "orderbook_spread": OrderbookSpreadStrategy,
    "divergence_scalp": DivergenceScalpStrategy,
}


def get_strategy_class(name: str) -> Type[BaseStrategy]:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def register_strategy(name: str, cls: Type[BaseStrategy]) -> None:
    """Register a custom strategy class by name."""
    _REGISTRY[name] = cls
    logger.info(f"Strategy registered: {name} → {cls.__name__}")


def list_strategies() -> Dict[str, str]:
    return {name: cls.__doc__.split("\n")[1].strip() for name, cls in _REGISTRY.items()}
