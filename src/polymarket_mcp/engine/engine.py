"""
TradingEngine — orchestrates MarketSlots for Polymarket 5-minute BTC markets.

Responsibilities:
- Discover upcoming 5-minute BTC market slots via Gamma API
- Create MarketSlot instances with the chosen strategy (early-bird)
- Launch lifecycle tasks and track their state
- Aggregate PnL across all completed slots
- Support both simulation and live modes
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Type

from .lifecycle import MarketSlot, SlotState, SlotPnL
from .strategy import BaseStrategy, StrategyAPI, OrderReceipt, OrderResult, Side
from .strategy import OrderbookSnapshot, OrderbookLevel, PositionSnapshot

logger = logging.getLogger(__name__)

# How far ahead (seconds) to look for the next slot
LOOKAHEAD_SECONDS = 120
# How often (seconds) to poll for new upcoming slots
POLL_INTERVAL = 30
# Duration of each 5-minute market slot
SLOT_DURATION_SECONDS = 300


class EngineState(str):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"


class TradingEngine:
    """
    Autonomous trading engine for Polymarket 5-minute prediction markets.

    Usage
    -----
    engine = TradingEngine(
        client=polymarket_client,
        strategy_class=MyStrategy,
        simulation=True,
        budget_per_slot=20.0,
    )
    await engine.start()
    # ... later ...
    await engine.stop()
    """

    def __init__(
        self,
        client,                          # PolymarketClient
        strategy_class: Type[BaseStrategy],
        simulation: bool = True,
        budget_per_slot: float = 20.0,
        max_concurrent_slots: int = 1,
        price_feeds=None,                # MultiSourcePriceFeed (optional)
        pnl_logger=None,                 # PnLLogger (optional)
        position_manager=None,           # PositionManager (optional)
    ):
        self.client = client
        self.strategy_class = strategy_class
        self.simulation = simulation
        self.budget_per_slot = budget_per_slot
        self.max_concurrent_slots = max_concurrent_slots
        self.price_feeds = price_feeds
        self.pnl_logger = pnl_logger
        self.position_manager = position_manager

        self.state = EngineState.IDLE
        self._active_slots: Dict[str, MarketSlot] = {}   # market_id → slot
        self._completed_slots: List[MarketSlot] = []
        self._known_market_ids: set = set()

        self._main_task: Optional[asyncio.Task] = None
        self._simulator = None  # set when simulation=True

        logger.info(
            f"TradingEngine initialized — strategy={strategy_class.name}  "
            f"simulation={simulation}  budget=${budget_per_slot}/slot"
        )

    # ── Public API ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the engine loop."""
        if self.state != EngineState.IDLE:
            raise RuntimeError(f"Engine already in state {self.state}")

        if self.simulation:
            from .simulator import PaperWallet
            self._simulator = PaperWallet(initial_balance=500.0)
            logger.info("Simulation mode: PaperWallet initialized with $500")

        self.state = EngineState.RUNNING
        self._main_task = asyncio.create_task(self._main_loop())
        logger.info("TradingEngine started")

    async def stop(self) -> None:
        """Gracefully stop the engine."""
        logger.info("TradingEngine stopping...")
        self.state = EngineState.STOPPING

        # Cancel all active slot tasks
        for slot in list(self._active_slots.values()):
            slot.cancel()

        if self._main_task:
            self._main_task.cancel()
            try:
                await asyncio.wait_for(self._main_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self.state = EngineState.IDLE
        logger.info("TradingEngine stopped")

    def get_status(self) -> Dict[str, Any]:
        """Return current engine status as a serializable dict."""
        total_pnl = sum(
            s.pnl.total_pnl for s in self._completed_slots if s.pnl
        )
        return {
            "state": self.state,
            "strategy": self.strategy_class.name,
            "simulation": self.simulation,
            "budget_per_slot": self.budget_per_slot,
            "active_slots": [s.to_dict() for s in self._active_slots.values()],
            "completed_count": len(self._completed_slots),
            "total_pnl_usd": round(total_pnl, 4),
            "paper_balance": (
                round(self._simulator.balance, 2) if self._simulator else None
            ),
        }

    def get_pnl_history(self) -> List[Dict[str, Any]]:
        return [
            s.pnl.to_dict() for s in self._completed_slots if s.pnl
        ]

    # ── Main loop ────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        logger.info("Engine main loop running")
        while self.state == EngineState.RUNNING:
            try:
                await self._discover_and_launch()
                await self._reap_completed()
            except Exception as exc:
                logger.error(f"Engine loop error: {exc}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)
        logger.info("Engine main loop exited")

    async def _discover_and_launch(self) -> None:
        """Find upcoming slots and launch them if under concurrent limit."""
        if len(self._active_slots) >= self.max_concurrent_slots:
            return

        upcoming = await self._fetch_upcoming_slots()
        for slot_info in upcoming:
            market_id = slot_info["market_id"]
            if market_id in self._known_market_ids:
                continue
            if len(self._active_slots) >= self.max_concurrent_slots:
                break

            slot = self._build_slot(slot_info)
            if slot is None:
                continue

            self._known_market_ids.add(market_id)
            self._active_slots[market_id] = slot
            task = asyncio.create_task(slot.start())
            slot._task = task
            task.add_done_callback(lambda t, mid=market_id: self._on_slot_done(mid))
            logger.info(f"Launched slot for market {market_id[:16]}")

    async def _reap_completed(self) -> None:
        """Move finished slots from active to completed and log PnL."""
        done = [
            mid for mid, slot in self._active_slots.items()
            if slot.state in (SlotState.COMPLETED, SlotState.FAILED)
        ]
        for mid in done:
            slot = self._active_slots.pop(mid)
            self._completed_slots.append(slot)

            if slot.pnl and self.pnl_logger:
                self.pnl_logger.record(slot.pnl, slot.api.receipts)

    def _on_slot_done(self, market_id: str) -> None:
        logger.debug(f"Slot task done: {market_id[:12]}")

    # ── Slot construction ────────────────────────────────────────────────

    def _build_slot(self, slot_info: Dict[str, Any]) -> Optional[MarketSlot]:
        """Build a MarketSlot from discovered market info."""
        try:
            strategy = self.strategy_class()

            if self.simulation and self._simulator:
                execute_fn = self._simulator.execute_order
            else:
                execute_fn = self._real_execute_order

            api = StrategyAPI(
                slot=None,  # filled in below after slot creation
                execute_order_fn=execute_fn,
                cancel_order_fn=self._cancel_order,
                get_orderbook_fn=self._get_orderbook,
                get_price_fn=self._get_price,
                get_position_fn=self._get_position,
                set_stop_loss_fn=self._set_stop_loss,
                set_take_profit_fn=self._set_take_profit,
                simulation=self.simulation,
            )

            slot = MarketSlot(
                market_id=slot_info["market_id"],
                condition_id=slot_info["condition_id"],
                yes_token_id=slot_info["yes_token_id"],
                no_token_id=slot_info["no_token_id"],
                price_to_beat=slot_info.get("price_to_beat"),
                open_time=slot_info["open_time"],
                close_time=slot_info["close_time"],
                strategy=strategy,
                api=api,
                simulation=self.simulation,
            )

            # Wire the slot reference into the api
            api._slot = slot

            # Register with position manager if present
            if self.position_manager:
                self.position_manager.register_slot(slot)

            return slot

        except Exception as exc:
            logger.error(f"Failed to build slot: {exc}", exc_info=True)
            return None

    # ── Market discovery ─────────────────────────────────────────────────

    async def _fetch_upcoming_slots(self) -> List[Dict[str, Any]]:
        """
        Fetch upcoming 5-minute BTC markets from Gamma API.
        Returns a list of slot_info dicts.
        """
        try:
            # Query Gamma API for active BTC 5-minute markets
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"tag": "Crypto", "active": "true", "closed": "false", "limit": 20},
                )
                resp.raise_for_status()
                raw = resp.json()
            markets = raw if isinstance(raw, list) else raw.get("data", [])
            # Filter to BTC-related markets
            markets = [
                m for m in markets
                if "btc" in (m.get("question") or "").lower()
                or "bitcoin" in (m.get("question") or "").lower()
            ]

            slots = []
            now = datetime.now(timezone.utc)

            for market in markets:
                # Filter: only 5-minute markets not yet started
                end_date_str = market.get("end_date_iso") or market.get("endDateIso")
                if not end_date_str:
                    continue

                try:
                    close_time = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                    # Ensure timezone-aware
                    if close_time.tzinfo is None:
                        close_time = close_time.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

                # Only future markets within lookahead window
                seconds_to_close = (close_time - now).total_seconds()
                if seconds_to_close < 0 or seconds_to_close > SLOT_DURATION_SECONDS + LOOKAHEAD_SECONDS:
                    continue

                # Infer open time (5 min before close)
                open_time = close_time - timedelta(seconds=SLOT_DURATION_SECONDS)

                # Extract tokens
                tokens = market.get("tokens", [])
                yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), None)
                no_token = next((t for t in tokens if t.get("outcome") == "No"), None)

                if not yes_token or not no_token:
                    continue

                slots.append({
                    "market_id": market.get("condition_id", market.get("id", "")),
                    "condition_id": market.get("condition_id", ""),
                    "yes_token_id": yes_token["token_id"],
                    "no_token_id": no_token["token_id"],
                    "price_to_beat": market.get("price_to_beat"),
                    "open_time": open_time,
                    "close_time": close_time,
                })

            return slots

        except Exception as exc:
            logger.error(f"Failed to fetch upcoming slots: {exc}", exc_info=True)
            return []

    # ── Order execution helpers ──────────────────────────────────────────

    async def _real_execute_order(
        self,
        slot: MarketSlot,
        side: Side,
        size_usd: float,
        price: Optional[float],
        order_type: str,
        is_sell: bool = False,
    ) -> OrderReceipt:
        """Execute a real order via PolymarketClient."""
        token_id = slot.yes_token_id if side == Side.YES else slot.no_token_id
        clob_side = "SELL" if is_sell else "BUY"

        try:
            # Fetch current best price if not specified
            if price is None:
                ob = await self.client.get_orderbook(token_id)
                asks = ob.get("asks", [])
                bids = ob.get("bids", [])
                if is_sell:
                    price = float(bids[0]["price"]) if bids else 0.5
                else:
                    price = float(asks[0]["price"]) if asks else 0.5

            size_shares = size_usd / price
            response = await self.client.post_order(
                token_id=token_id,
                price=price,
                size=size_shares,
                side=clob_side,
                order_type=order_type,
            )

            return OrderReceipt(
                order_id=response.get("orderID", "unknown"),
                side=side,
                price=price,
                size_usd=size_usd,
                result=OrderResult.FILLED,
                filled_size_usd=size_usd,
            )

        except Exception as exc:
            logger.error(f"Order execution failed: {exc}")
            return OrderReceipt(
                order_id="error",
                side=side,
                price=price or 0.0,
                size_usd=size_usd,
                result=OrderResult.REJECTED,
                filled_size_usd=0.0,
                error=str(exc),
            )

    async def _cancel_order(self, order_id: str) -> bool:
        try:
            await self.client.cancel_order(order_id)
            return True
        except Exception:
            return False

    async def _get_orderbook(self, slot: MarketSlot, depth: int = 10) -> OrderbookSnapshot:
        ob = await self.client.get_orderbook(slot.yes_token_id)
        bids = [OrderbookLevel(float(b["price"]), float(b["size"])) for b in ob.get("bids", [])[:depth]]
        asks = [OrderbookLevel(float(a["price"]), float(a["size"])) for a in ob.get("asks", [])[:depth]]
        return OrderbookSnapshot(
            token_id=slot.yes_token_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(timezone.utc),
        )

    async def _get_price(self, slot: MarketSlot) -> float:
        ob = await self._get_orderbook(slot)
        return ob.mid or 0.5

    async def _get_position(self, slot: MarketSlot) -> Optional[PositionSnapshot]:
        try:
            positions = await self.client.get_positions()
            for pos in positions:
                if pos.get("asset") in (slot.yes_token_id, slot.no_token_id):
                    current = await self._get_price(slot)
                    entry = float(pos.get("entry_price", current))
                    size = float(pos.get("value", 0))
                    pnl = (current - entry) * size / entry if entry > 0 else 0.0
                    pnl_pct = pnl / size * 100 if size > 0 else 0.0
                    return PositionSnapshot(
                        side=Side.YES if pos.get("asset") == slot.yes_token_id else Side.NO,
                        entry_price=entry,
                        size_usd=size,
                        current_price=current,
                        unrealized_pnl=pnl,
                        unrealized_pnl_pct=pnl_pct,
                    )
        except Exception as exc:
            logger.error(f"Failed to get position: {exc}")
        return None

    def _set_stop_loss(self, slot: MarketSlot, price: float) -> None:
        slot.stop_loss_price = price
        if self.position_manager:
            self.position_manager.update_stop_loss(slot.market_id, price)
        logger.info(f"[{slot.market_id[:12]}] Stop-loss set @ {price:.3f}")

    def _set_take_profit(self, slot: MarketSlot, price: float) -> None:
        slot.take_profit_price = price
        if self.position_manager:
            self.position_manager.update_take_profit(slot.market_id, price)
        logger.info(f"[{slot.market_id[:12]}] Take-profit set @ {price:.3f}")
