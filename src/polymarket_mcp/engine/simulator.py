"""
Paper Wallet simulation for Polymarket 5-minute markets.

Mirrors real trading conditions without spending real USDC:
- Tracks paper balance
- Simulates limit order fills against current orderbook
- Applies on-chain settlement delay (tokens not immediately re-sellable)
- Tracks open positions and unrealized PnL
- Simulates partial fills when liquidity is thin
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .strategy import (
    Side, OrderReceipt, OrderResult, PositionSnapshot,
    OrderbookSnapshot, OrderbookLevel
)
from .lifecycle import MarketSlot

logger = logging.getLogger(__name__)

# Simulated on-chain settlement delay before tokens can be re-sold
SETTLEMENT_DELAY_SECONDS = 12.0  # ~1 Polygon block

# Simulated taker fee (0.5% of trade value, matching Polymarket's fee)
TAKER_FEE_RATE = 0.005


@dataclass
class SimulatedPosition:
    market_id: str
    side: Side
    token_id: str
    shares: float          # number of conditional tokens held
    entry_price: float     # average fill price
    size_usd: float        # USDC spent
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    settlement_ready_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
                                 + timedelta(seconds=SETTLEMENT_DELAY_SECONDS)
    )

    @property
    def is_settled(self) -> bool:
        return datetime.now(timezone.utc) >= self.settlement_ready_at

    def current_value(self, current_price: float) -> float:
        return self.shares * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        return self.current_value(current_price) - self.size_usd


class PaperWallet:
    """
    Simulated wallet that intercepts StrategyAPI order calls.

    Plugged into TradingEngine as execute_fn when simulation=True.
    """

    def __init__(self, initial_balance: float = 500.0):
        self.balance = initial_balance
        self._initial_balance = initial_balance
        self._positions: Dict[str, SimulatedPosition] = {}  # market_id → position
        self._order_log: List[Dict[str, Any]] = []
        self._total_fees = 0.0

        logger.info(f"PaperWallet initialized — balance: ${initial_balance:.2f}")

    # ── Core execute function (injected into StrategyAPI) ─────────────────

    async def execute_order(
        self,
        slot: MarketSlot,
        side: Side,
        size_usd: float,
        price: Optional[float],
        order_type: str,
        is_sell: bool = False,
    ) -> OrderReceipt:
        """
        Simulate an order fill. Called instead of PolymarketClient.post_order.
        """
        order_id = str(uuid.uuid4())[:8]

        if is_sell:
            return await self._simulate_sell(slot, side, size_usd, price, order_id)
        else:
            return await self._simulate_buy(slot, side, size_usd, price, order_id)

    # ── Buy simulation ───────────────────────────────────────────────────

    async def _simulate_buy(
        self,
        slot: MarketSlot,
        side: Side,
        size_usd: float,
        price: Optional[float],
        order_id: str,
    ) -> OrderReceipt:
        # Fetch real orderbook for fill simulation
        ob = await self._fetch_orderbook(slot, side)
        fill_price = price or (ob.best_ask or 0.5)

        # Check balance
        if size_usd > self.balance:
            logger.warning(
                f"[SIM] Insufficient balance: need ${size_usd:.2f}, "
                f"have ${self.balance:.2f}"
            )
            return OrderReceipt(
                order_id=order_id, side=side, price=fill_price,
                size_usd=size_usd, result=OrderResult.REJECTED,
                filled_size_usd=0.0, error="Insufficient paper balance"
            )

        # Simulate partial fill based on ask liquidity at this price
        available_liquidity = sum(
            lvl.price * lvl.size
            for lvl in ob.asks
            if lvl.price <= fill_price + 0.01
        )
        filled_usd = min(size_usd, available_liquidity) if available_liquidity > 0 else size_usd

        # FOK: all or nothing
        if order_type == "FOK" and filled_usd < size_usd * 0.99:
            return OrderReceipt(
                order_id=order_id, side=side, price=fill_price,
                size_usd=size_usd, result=OrderResult.CANCELLED,
                filled_size_usd=0.0, error="FOK: insufficient liquidity"
            )

        shares = filled_usd / fill_price
        fee = filled_usd * TAKER_FEE_RATE
        total_cost = filled_usd + fee

        self.balance -= total_cost
        self._total_fees += fee

        # Update position
        token_id = slot.yes_token_id if side == Side.YES else slot.no_token_id
        key = f"{slot.market_id}:{side.value}"

        if key in self._positions:
            existing = self._positions[key]
            total_shares = existing.shares + shares
            avg_price = (existing.size_usd + filled_usd) / total_shares
            existing.shares = total_shares
            existing.entry_price = avg_price
            existing.size_usd += filled_usd
        else:
            self._positions[key] = SimulatedPosition(
                market_id=slot.market_id,
                side=side,
                token_id=token_id,
                shares=shares,
                entry_price=fill_price,
                size_usd=filled_usd,
            )

        result = OrderResult.FILLED if filled_usd >= size_usd * 0.99 else OrderResult.PARTIAL

        self._log_order(
            order_id=order_id, action="BUY", side=side, slot=slot,
            price=fill_price, size_usd=filled_usd, fee=fee, result=result
        )

        logger.info(
            f"[SIM BUY] {side.value} ${filled_usd:.2f} @ {fill_price:.3f} "
            f"| fee=${fee:.3f} | balance=${self.balance:.2f}"
        )

        return OrderReceipt(
            order_id=order_id, side=side, price=fill_price,
            size_usd=size_usd, result=result,
            filled_size_usd=filled_usd
        )

    # ── Sell simulation ───────────────────────────────────────────────────

    async def _simulate_sell(
        self,
        slot: MarketSlot,
        side: Side,
        size_usd: float,
        price: Optional[float],
        order_id: str,
    ) -> OrderReceipt:
        key = f"{slot.market_id}:{side.value}"
        position = self._positions.get(key)

        if not position:
            return OrderReceipt(
                order_id=order_id, side=side, price=price or 0.5,
                size_usd=size_usd, result=OrderResult.REJECTED,
                filled_size_usd=0.0, error="No position to sell"
            )

        # Check settlement delay
        if not position.is_settled:
            wait_s = (position.settlement_ready_at - datetime.now(timezone.utc)).total_seconds()
            logger.info(f"[SIM] Waiting {wait_s:.1f}s for settlement...")
            await asyncio.sleep(wait_s)

        ob = await self._fetch_orderbook(slot, side)
        fill_price = price or (ob.best_bid or 0.5)

        shares_to_sell = min(position.shares, size_usd / fill_price)
        proceeds_usd = shares_to_sell * fill_price
        fee = proceeds_usd * TAKER_FEE_RATE
        net_proceeds = proceeds_usd - fee

        self.balance += net_proceeds
        self._total_fees += fee

        position.shares -= shares_to_sell
        position.size_usd -= shares_to_sell * position.entry_price
        if position.shares < 0.001:
            del self._positions[key]

        self._log_order(
            order_id=order_id, action="SELL", side=side, slot=slot,
            price=fill_price, size_usd=proceeds_usd, fee=fee,
            result=OrderResult.FILLED
        )

        logger.info(
            f"[SIM SELL] {side.value} {shares_to_sell:.4f} shares @ {fill_price:.3f} "
            f"| proceeds=${net_proceeds:.2f} | balance=${self.balance:.2f}"
        )

        receipt = OrderReceipt(
            order_id=order_id, side=side, price=fill_price,
            size_usd=proceeds_usd, result=OrderResult.FILLED,
            filled_size_usd=proceeds_usd
        )
        receipt.is_sell = True
        return receipt

    # ── Orderbook helper ──────────────────────────────────────────────────

    async def _fetch_orderbook(self, slot: MarketSlot, side: Side) -> OrderbookSnapshot:
        """Attempt to get live orderbook; fall back to synthetic if unavailable."""
        token_id = slot.yes_token_id if side == Side.YES else slot.no_token_id
        try:
            # Try to use the slot's API to get real orderbook data
            if hasattr(slot, "api") and slot.api:
                return await slot.api.orderbook()
        except Exception:
            pass

        # Synthetic fallback: thin book around 0.5
        return OrderbookSnapshot(
            token_id=token_id,
            bids=[
                OrderbookLevel(0.49, 100.0),
                OrderbookLevel(0.48, 150.0),
            ],
            asks=[
                OrderbookLevel(0.51, 100.0),
                OrderbookLevel(0.52, 150.0),
            ],
            timestamp=datetime.now(timezone.utc),
        )

    # ── Status & reporting ────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        total_pnl = self.balance - self._initial_balance
        return {
            "balance": round(self.balance, 4),
            "initial_balance": self._initial_balance,
            "total_pnl": round(total_pnl, 4),
            "total_pnl_pct": round(total_pnl / self._initial_balance * 100, 2),
            "total_fees_paid": round(self._total_fees, 4),
            "open_positions": len(self._positions),
            "positions": [
                {
                    "market": pos.market_id[:16],
                    "side": pos.side.value,
                    "shares": round(pos.shares, 4),
                    "entry_price": round(pos.entry_price, 4),
                    "size_usd": round(pos.size_usd, 4),
                    "settled": pos.is_settled,
                }
                for pos in self._positions.values()
            ],
            "total_trades": len(self._order_log),
        }

    def _log_order(
        self,
        order_id: str,
        action: str,
        side: Side,
        slot: MarketSlot,
        price: float,
        size_usd: float,
        fee: float,
        result: OrderResult,
    ) -> None:
        self._order_log.append({
            "order_id": order_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "side": side.value,
            "market_id": slot.market_id,
            "price": round(price, 4),
            "size_usd": round(size_usd, 4),
            "fee": round(fee, 4),
            "result": result.value,
            "balance_after": round(self.balance, 4),
        })
