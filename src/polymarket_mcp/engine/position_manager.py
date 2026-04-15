"""
PositionManager — active exit signal monitoring for open market slots.

Runs a background monitoring loop that:
1. Polls current YES token price for each active slot
2. Triggers stop-loss if price drops below threshold
3. Triggers take-profit if price rises above threshold
4. Calls the strategy's on_stop_loss_hit / on_take_profit_hit hooks
5. Reports risk events to PnL logger if present

The article's key insight: "the goal is not to always hold shares until
the market resolves. As soon as you have appropriate profit, sell and exit."
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .lifecycle import MarketSlot, SlotState

logger = logging.getLogger(__name__)

# How often to check prices (seconds)
CHECK_INTERVAL = 2.0


@dataclass
class RiskEvent:
    kind: str   # "stop_loss" | "take_profit"
    market_id: str
    trigger_price: float
    current_price: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "market_id": self.market_id[:16],
            "trigger_price": round(self.trigger_price, 4),
            "current_price": round(self.current_price, 4),
            "timestamp": self.timestamp.isoformat(),
        }


class PositionManager:
    """
    Background monitor that enforces stop-loss and take-profit exits.

    Usage (wired in TradingEngine):
        pm = PositionManager(check_interval=2.0)
        await pm.start()
        pm.register_slot(slot)          # called by engine._build_slot
        pm.update_stop_loss(mid, 0.40)  # called by StrategyAPI.set_stop_loss
    """

    def __init__(self, check_interval: float = CHECK_INTERVAL, pnl_logger=None):
        self.check_interval = check_interval
        self.pnl_logger = pnl_logger

        self._slots: Dict[str, MarketSlot] = {}       # market_id → slot
        self._stop_losses: Dict[str, float] = {}      # market_id → price
        self._take_profits: Dict[str, float] = {}     # market_id → price
        self._triggered: Set[str] = set()             # market_ids already handled
        self._risk_events: List[RiskEvent] = []

        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("PositionManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("PositionManager stopped")

    def register_slot(self, slot: MarketSlot) -> None:
        self._slots[slot.market_id] = slot
        logger.debug(f"PositionManager: registered slot {slot.market_id[:12]}")

    def unregister_slot(self, market_id: str) -> None:
        self._slots.pop(market_id, None)
        self._stop_losses.pop(market_id, None)
        self._take_profits.pop(market_id, None)
        self._triggered.discard(market_id)

    def update_stop_loss(self, market_id: str, price: float) -> None:
        self._stop_losses[market_id] = price
        logger.info(f"Stop-loss set for {market_id[:12]}: {price:.4f}")

    def update_take_profit(self, market_id: str, price: float) -> None:
        self._take_profits[market_id] = price
        logger.info(f"Take-profit set for {market_id[:12]}: {price:.4f}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "monitored_slots": len(self._slots),
            "active_stop_losses": len(self._stop_losses),
            "active_take_profits": len(self._take_profits),
            "triggered_count": len(self._triggered),
            "recent_events": [e.to_dict() for e in self._risk_events[-10:]],
        }

    # ── Monitor loop ──────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        logger.info("PositionManager monitor loop running")
        while self._running:
            try:
                await self._check_all_slots()
            except Exception as exc:
                logger.error(f"PositionManager error: {exc}", exc_info=True)
            await asyncio.sleep(self.check_interval)
        logger.info("PositionManager monitor loop stopped")

    async def _check_all_slots(self) -> None:
        # Clean up completed/failed slots
        done = [
            mid for mid, slot in self._slots.items()
            if slot.state in (SlotState.COMPLETED, SlotState.FAILED)
        ]
        for mid in done:
            self.unregister_slot(mid)

        # Check each active running slot
        active = [
            (mid, slot)
            for mid, slot in self._slots.items()
            if slot.state == SlotState.RUNNING and mid not in self._triggered
        ]

        await asyncio.gather(
            *[self._check_slot(mid, slot) for mid, slot in active],
            return_exceptions=True,
        )

    async def _check_slot(self, market_id: str, slot: MarketSlot) -> None:
        try:
            current_price = await slot.api._get_price(slot)
        except Exception as exc:
            logger.debug(f"Could not fetch price for {market_id[:12]}: {exc}")
            return

        stop_loss = self._stop_losses.get(market_id)
        take_profit = self._take_profits.get(market_id)

        # Stop-loss check
        if stop_loss is not None and current_price <= stop_loss:
            logger.warning(
                f"[STOP-LOSS] {market_id[:12]} @ {current_price:.4f} "
                f"<= {stop_loss:.4f}"
            )
            await self._trigger_stop_loss(slot, current_price, stop_loss)
            return

        # Take-profit check
        if take_profit is not None and current_price >= take_profit:
            logger.info(
                f"[TAKE-PROFIT] {market_id[:12]} @ {current_price:.4f} "
                f">= {take_profit:.4f}"
            )
            await self._trigger_take_profit(slot, current_price, take_profit)

    async def _trigger_stop_loss(
        self, slot: MarketSlot, current_price: float, trigger_price: float
    ) -> None:
        self._triggered.add(slot.market_id)

        event = RiskEvent(
            kind="stop_loss",
            market_id=slot.market_id,
            trigger_price=trigger_price,
            current_price=current_price,
        )
        self._risk_events.append(event)

        if self.pnl_logger:
            self.pnl_logger.record_risk_event(event)

        try:
            await slot.strategy.on_stop_loss_hit(slot.api, current_price)
        except Exception as exc:
            logger.error(f"on_stop_loss_hit error: {exc}")

    async def _trigger_take_profit(
        self, slot: MarketSlot, current_price: float, trigger_price: float
    ) -> None:
        self._triggered.add(slot.market_id)

        event = RiskEvent(
            kind="take_profit",
            market_id=slot.market_id,
            trigger_price=trigger_price,
            current_price=current_price,
        )
        self._risk_events.append(event)

        if self.pnl_logger:
            self.pnl_logger.record_risk_event(event)

        try:
            await slot.strategy.on_take_profit_hit(slot.api, current_price)
        except Exception as exc:
            logger.error(f"on_take_profit_hit error: {exc}")
