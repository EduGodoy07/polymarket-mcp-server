"""
Market Slot Lifecycle for Polymarket 5-minute BTC markets.

Each 5-minute window is one MarketSlot. The lifecycle is:
  WAITING → RUNNING → COMPLETED (or FAILED)

The engine always targets a FUTURE slot (at least one ahead of the current
window) so the strategy is ready the moment the window opens — "early-bird".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .strategy import BaseStrategy, StrategyAPI

logger = logging.getLogger(__name__)

SLOT_DURATION_SECONDS = 5 * 60  # 5 minutes


class SlotState(str, Enum):
    WAITING = "WAITING"    # Subscribed, waiting for slot to open
    RUNNING = "RUNNING"    # Inside the 5-minute window, strategy executing
    COMPLETED = "COMPLETED"  # Window closed, PnL computed
    FAILED = "FAILED"      # Unrecoverable error


@dataclass
class SlotPnL:
    market_id: str
    strategy_name: str
    simulation: bool
    start_time: datetime
    end_time: datetime
    total_bought_usd: float = 0.0
    total_sold_usd: float = 0.0
    resolved_value_usd: float = 0.0  # value of remaining position at resolution
    fee_usd: float = 0.0

    @property
    def realized_pnl(self) -> float:
        return self.total_sold_usd - self.total_bought_usd

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.resolved_value_usd - self.fee_usd

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "strategy": self.strategy_name,
            "simulation": self.simulation,
            "start": self.start_time.isoformat(),
            "end": self.end_time.isoformat(),
            "bought_usd": round(self.total_bought_usd, 4),
            "sold_usd": round(self.total_sold_usd, 4),
            "resolved_value_usd": round(self.resolved_value_usd, 4),
            "fee_usd": round(self.fee_usd, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
        }


class MarketSlot:
    """
    Represents one 5-minute prediction market window.

    The engine creates slots in advance (early-bird). Each slot runs exactly
    one strategy instance during its RUNNING state.
    """

    def __init__(
        self,
        market_id: str,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        price_to_beat: Optional[float],
        open_time: datetime,
        close_time: datetime,
        strategy: "BaseStrategy",
        api: "StrategyAPI",
        simulation: bool = False,
    ):
        self.market_id = market_id
        self.condition_id = condition_id
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.price_to_beat = price_to_beat
        self.open_time = open_time
        self.close_time = close_time
        self.strategy = strategy
        self.api = api
        self.simulation = simulation

        self.state = SlotState.WAITING
        self.pnl: Optional[SlotPnL] = None
        self.error: Optional[str] = None

        # Stop-loss / take-profit levels set by strategy
        self.stop_loss_price: Optional[float] = None
        self.take_profit_price: Optional[float] = None

        self._task: Optional[asyncio.Task] = None
        self._started_at: Optional[datetime] = None
        self._ended_at: Optional[datetime] = None

        logger.info(
            f"MarketSlot created: {market_id[:16]}  "
            f"opens={open_time.strftime('%H:%M:%S')}  "
            f"closes={close_time.strftime('%H:%M:%S')}  "
            f"strategy={strategy.name}  sim={simulation}"
        )

    # ── Timing helpers ───────────────────────────────────────────────────

    @property
    def seconds_until_open(self) -> float:
        now = datetime.now(timezone.utc)
        delta = (self.open_time - now).total_seconds()
        return max(0.0, delta)

    @property
    def seconds_remaining(self) -> float:
        now = datetime.now(timezone.utc)
        delta = (self.close_time - now).total_seconds()
        return max(0.0, delta)

    @property
    def is_open(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.open_time <= now <= self.close_time

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Begin the lifecycle. Waits until the slot opens, then runs the strategy.
        This is meant to be called as an asyncio Task.
        """
        try:
            await self._wait_for_open()
            await self._run()
        except asyncio.CancelledError:
            logger.warning(f"Slot {self.market_id[:12]} cancelled")
            self.state = SlotState.FAILED
            self.error = "Cancelled"
        except Exception as exc:
            logger.exception(f"Slot {self.market_id[:12]} failed: {exc}")
            self.state = SlotState.FAILED
            self.error = str(exc)
        finally:
            await self._end()

    async def _wait_for_open(self) -> None:
        """Sleep until the slot opens (early-bird: pre-registers before open)."""
        wait = self.seconds_until_open
        if wait > 0:
            logger.info(
                f"[{self.market_id[:12]}] Waiting {wait:.1f}s for slot to open..."
            )
            await asyncio.sleep(wait)
        logger.info(f"[{self.market_id[:12]}] Slot OPEN — strategy starting")

    async def _run(self) -> None:
        """Execute the strategy with a hard timeout at close_time."""
        self.state = SlotState.RUNNING
        self._started_at = datetime.now(timezone.utc)

        timeout = self.seconds_remaining
        logger.info(
            f"[{self.market_id[:12]}] Running strategy '{self.strategy.name}' "
            f"(timeout={timeout:.1f}s)"
        )

        try:
            await asyncio.wait_for(
                self.strategy.run(self.api),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Strategy ran to end of window — call on_market_end hook
            logger.info(
                f"[{self.market_id[:12]}] Window closed, calling on_market_end"
            )
            await self.strategy.on_market_end(self.api)

    async def _end(self) -> None:
        """Finalize slot: compute PnL and transition to COMPLETED."""
        self._ended_at = datetime.now(timezone.utc)

        if self.state != SlotState.FAILED:
            self.state = SlotState.COMPLETED

        # Build PnL summary from receipts
        receipts = self.api.receipts
        bought = sum(
            r.filled_size_usd for r in receipts
            if not getattr(r, "is_sell", False)
        )
        sold = sum(
            r.filled_size_usd for r in receipts
            if getattr(r, "is_sell", False)
        )

        self.pnl = SlotPnL(
            market_id=self.market_id,
            strategy_name=self.strategy.name,
            simulation=self.simulation,
            start_time=self._started_at or datetime.now(timezone.utc),
            end_time=self._ended_at,
            total_bought_usd=bought,
            total_sold_usd=sold,
        )

        logger.info(
            f"[{self.market_id[:12]}] Slot COMPLETED — "
            f"PnL: ${self.pnl.total_pnl:+.4f} | "
            f"bought=${bought:.2f} sold=${sold:.2f} | "
            f"state={self.state.value}"
        )

    def cancel(self) -> None:
        """Cancel this slot's running task (emergency stop)."""
        if self._task and not self._task.done():
            self._task.cancel()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "state": self.state.value,
            "strategy": self.strategy.name,
            "simulation": self.simulation,
            "price_to_beat": self.price_to_beat,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "seconds_remaining": round(self.seconds_remaining, 1),
            "stop_loss": self.stop_loss_price,
            "take_profit": self.take_profit_price,
            "pnl": self.pnl.to_dict() if self.pnl else None,
            "error": self.error,
        }
