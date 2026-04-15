"""
Strategy base class and StrategyAPI for Polymarket 5-minute BTC markets.

Every custom strategy must subclass BaseStrategy and implement run().
The StrategyAPI is injected at runtime — strategies never touch the client directly.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .lifecycle import MarketSlot

logger = logging.getLogger(__name__)


class Side(str, Enum):
    YES = "YES"  # UP / above Price to Beat
    NO = "NO"    # DOWN / below Price to Beat


class OrderResult(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"


@dataclass
class OrderReceipt:
    order_id: str
    side: Side
    price: float
    size_usd: float
    result: OrderResult
    filled_size_usd: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None

    @property
    def is_filled(self) -> bool:
        return self.result == OrderResult.FILLED

    @property
    def fill_ratio(self) -> float:
        return self.filled_size_usd / self.size_usd if self.size_usd > 0 else 0.0


@dataclass
class PositionSnapshot:
    side: Side
    entry_price: float
    size_usd: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class OrderbookSnapshot:
    token_id: str
    bids: List[OrderbookLevel]
    asks: List[OrderbookLevel]
    timestamp: datetime

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    def depth(self, side: str, levels: int = 5) -> float:
        """Total liquidity in USD for the given side up to `levels` price levels."""
        book = self.bids if side == "bid" else self.asks
        return sum(lvl.price * lvl.size for lvl in book[:levels])


class StrategyAPI:
    """
    Injected interface that strategies use to interact with the market.

    In simulation mode the underlying calls are intercepted by PaperWallet.
    In production they go through the real PolymarketClient.
    """

    def __init__(
        self,
        slot: "MarketSlot",
        execute_order_fn: Callable,
        cancel_order_fn: Callable,
        get_orderbook_fn: Callable,
        get_price_fn: Callable,
        get_position_fn: Callable,
        set_stop_loss_fn: Callable,
        set_take_profit_fn: Callable,
        simulation: bool = False,
    ):
        self._slot = slot
        self._execute_order = execute_order_fn
        self._cancel_order = cancel_order_fn
        self._get_orderbook = get_orderbook_fn
        self._get_price = get_price_fn
        self._get_position = get_position_fn
        self._set_stop_loss = set_stop_loss_fn
        self._set_take_profit = set_take_profit_fn
        self.simulation = simulation

        self._receipts: List[OrderReceipt] = []

    # ── Core trading calls ──────────────────────────────────────────────

    async def buy(
        self,
        side: Side,
        size_usd: float,
        price: Optional[float] = None,  # None = market order
        order_type: str = "GTC",
    ) -> OrderReceipt:
        """
        Place a buy order.

        Args:
            side: YES (UP) or NO (DOWN)
            size_usd: Amount in USDC
            price: Limit price 0-1. None places at best ask (market-like).
            order_type: GTC | FOK | FAK
        """
        receipt = await self._execute_order(
            slot=self._slot,
            side=side,
            size_usd=size_usd,
            price=price,
            order_type=order_type,
        )
        self._receipts.append(receipt)
        logger.info(
            f"[{self._slot.market_id[:12]}] BUY {side.value} ${size_usd:.2f} "
            f"@ {price or 'MARKET'} → {receipt.result.value}"
        )
        return receipt

    async def sell(
        self,
        side: Side,
        size_usd: float,
        price: Optional[float] = None,
        order_type: str = "GTC",
    ) -> OrderReceipt:
        """Sell (exit) an existing position."""
        receipt = await self._execute_order(
            slot=self._slot,
            side=side,
            size_usd=size_usd,
            price=price,
            order_type=order_type,
            is_sell=True,
        )
        self._receipts.append(receipt)
        logger.info(
            f"[{self._slot.market_id[:12]}] SELL {side.value} ${size_usd:.2f} "
            f"@ {price or 'MARKET'} → {receipt.result.value}"
        )
        return receipt

    async def cancel(self, order_id: str) -> bool:
        return await self._cancel_order(order_id)

    # ── Market data ─────────────────────────────────────────────────────

    async def orderbook(self, depth: int = 10) -> OrderbookSnapshot:
        return await self._get_orderbook(self._slot, depth)

    async def price(self) -> float:
        """Current mid price for the YES token (0-1)."""
        return await self._get_price(self._slot)

    async def position(self) -> Optional[PositionSnapshot]:
        """Current open position for this market slot, or None."""
        return await self._get_position(self._slot)

    # ── Risk controls ───────────────────────────────────────────────────

    def set_stop_loss(self, price: float) -> None:
        """
        Register a stop-loss price. PositionManager will auto-exit if
        price drops below this level.
        """
        self._set_stop_loss(self._slot, price)

    def set_take_profit(self, price: float) -> None:
        """Register a take-profit price for auto-exit."""
        self._set_take_profit(self._slot, price)

    # ── Slot information ────────────────────────────────────────────────

    @property
    def market_id(self) -> str:
        return self._slot.market_id

    @property
    def price_to_beat(self) -> Optional[float]:
        return self._slot.price_to_beat

    @property
    def seconds_remaining(self) -> float:
        return self._slot.seconds_remaining

    @property
    def is_simulation(self) -> bool:
        return self.simulation

    # ── History ─────────────────────────────────────────────────────────

    @property
    def receipts(self) -> List[OrderReceipt]:
        return list(self._receipts)


class BaseStrategy(ABC):
    """
    Abstract base for all trading strategies.

    Subclass this and implement run(). The engine injects a StrategyAPI
    instance — strategies must not import or use PolymarketClient directly.

    Example
    -------
    class MyStrategy(BaseStrategy):
        name = "my_strategy"

        async def run(self, api: StrategyAPI) -> None:
            ob = await api.orderbook()
            if ob.spread and ob.spread < 0.02:
                await api.buy(Side.YES, size_usd=10.0)
                api.set_stop_loss(0.40)
                api.set_take_profit(0.65)
    """

    name: str = "base"

    @abstractmethod
    async def run(self, api: StrategyAPI) -> None:
        """
        Main strategy logic. Called once when the market slot enters RUN state.
        This coroutine can use await freely — it runs in the slot's lifecycle task.
        """
        ...

    async def on_stop_loss_hit(self, api: StrategyAPI, price: float) -> None:
        """Called when stop-loss triggers. Default: sell everything."""
        pos = await api.position()
        if pos:
            await api.sell(pos.side, pos.size_usd)

    async def on_take_profit_hit(self, api: StrategyAPI, price: float) -> None:
        """Called when take-profit triggers. Default: sell everything."""
        pos = await api.position()
        if pos:
            await api.sell(pos.side, pos.size_usd)

    async def on_market_end(self, api: StrategyAPI) -> None:
        """Called just before market resolves. Default: do nothing (let it settle)."""
        pass

    def __repr__(self) -> str:
        return f"<Strategy:{self.name}>"
