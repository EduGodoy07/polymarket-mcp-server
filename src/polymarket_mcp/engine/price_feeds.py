"""
Multi-source BTC price feed aggregator.

Consumes BTC/USDT prices from three independent sources simultaneously:
- Binance WebSocket (wss://stream.binance.com)
- Coinbase WebSocket (wss://advanced-trade-ws.coinbase.com)
- Chainlink on-chain feed (via Polygon RPC, same oracle Polymarket uses)

Key signal: cross-source divergence. When Binance and Coinbase diverge
significantly from each other or from Chainlink, it often signals
a large order being absorbed on one venue before it propagates — a
leading indicator for short-term BTC direction.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# Divergence threshold that triggers a signal (as fraction of price, e.g. 0.001 = 0.1%)
DIVERGENCE_SIGNAL_THRESHOLD = 0.001

# How many seconds of prices to keep in the rolling window
PRICE_HISTORY_SECONDS = 60

# Binance BTC/USDT trade stream
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
# Coinbase Advanced Trade WS
COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
# Polygon RPC for Chainlink
POLYGON_RPC_URL = "https://polygon-rpc.com"
# Chainlink BTC/USD feed on Polygon
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"


@dataclass
class PriceTick:
    source: str
    price: float
    timestamp: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


@dataclass
class DivergenceSignal:
    source_a: str
    source_b: str
    price_a: float
    price_b: float
    divergence_pct: float
    direction: str  # "up" | "down" — which source is leading
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_bullish(self) -> bool:
        """Binance leading above Coinbase → bullish (buy YES)."""
        return self.direction == "up"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_a": self.source_a,
            "source_b": self.source_b,
            "price_a": round(self.price_a, 2),
            "price_b": round(self.price_b, 2),
            "divergence_pct": round(self.divergence_pct * 100, 4),
            "direction": self.direction,
            "timestamp": self.timestamp.isoformat(),
        }


class MultiSourcePriceFeed:
    """
    Aggregates BTC prices from Binance, Coinbase, and Chainlink.

    Usage
    -----
    feed = MultiSourcePriceFeed()
    await feed.start()

    price = feed.consensus_price()
    signal = feed.latest_divergence_signal()

    # Register a callback for divergence alerts
    feed.on_divergence(my_callback)
    """

    def __init__(self, divergence_threshold: float = DIVERGENCE_SIGNAL_THRESHOLD):
        self.divergence_threshold = divergence_threshold
        self._latest: Dict[str, PriceTick] = {}  # source → latest tick
        self._history: Dict[str, Deque[PriceTick]] = {
            "binance": deque(maxlen=500),
            "coinbase": deque(maxlen=500),
            "chainlink": deque(maxlen=500),
        }
        self._divergence_signals: Deque[DivergenceSignal] = deque(maxlen=100)
        self._callbacks: List[Callable] = []

        self._tasks: List[asyncio.Task] = []
        self._running = False

        logger.info("MultiSourcePriceFeed initialized")

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._binance_stream()),
            asyncio.create_task(self._coinbase_stream()),
            asyncio.create_task(self._chainlink_poll()),
        ]
        logger.info("MultiSourcePriceFeed started (Binance + Coinbase + Chainlink)")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("MultiSourcePriceFeed stopped")

    def on_divergence(self, callback: Callable[[DivergenceSignal], None]) -> None:
        """Register a callback invoked whenever a divergence signal is detected."""
        self._callbacks.append(callback)

    def consensus_price(self) -> Optional[float]:
        """Median BTC price across all sources with fresh data (<10s old)."""
        fresh = [
            t.price for t in self._latest.values()
            if t.age_seconds < 10
        ]
        if not fresh:
            return None
        fresh.sort()
        mid = len(fresh) // 2
        return fresh[mid] if len(fresh) % 2 == 1 else (fresh[mid - 1] + fresh[mid]) / 2

    def price_by_source(self, source: str) -> Optional[float]:
        tick = self._latest.get(source)
        return tick.price if tick and tick.age_seconds < 15 else None

    def latest_divergence_signal(self) -> Optional[DivergenceSignal]:
        return self._divergence_signals[-1] if self._divergence_signals else None

    def recent_divergences(self, n: int = 5) -> List[DivergenceSignal]:
        return list(self._divergence_signals)[-n:]

    def get_status(self) -> Dict[str, Any]:
        return {
            "consensus_price": self.consensus_price(),
            "sources": {
                source: {
                    "price": round(t.price, 2),
                    "age_seconds": round(t.age_seconds, 1),
                    "stale": t.age_seconds > 10,
                }
                for source, t in self._latest.items()
            },
            "divergence_count": len(self._divergence_signals),
            "latest_divergence": (
                self._divergence_signals[-1].to_dict()
                if self._divergence_signals else None
            ),
        }

    def get_price_history(self, source: str, n: int = 60) -> List[Dict[str, Any]]:
        hist = self._history.get(source, deque())
        return [
            {"price": round(t.price, 2), "ts": t.timestamp}
            for t in list(hist)[-n:]
        ]

    # ── Internal feed handlers ─────────────────────────────────────────────

    def _update_price(self, source: str, price: float) -> None:
        tick = PriceTick(source=source, price=price)
        self._latest[source] = tick
        self._history[source].append(tick)
        self._check_divergence()

    def _check_divergence(self) -> None:
        """Compare each pair of sources for significant divergence."""
        sources = {
            s: t for s, t in self._latest.items() if t.age_seconds < 10
        }
        if len(sources) < 2:
            return

        pairs = [
            ("binance", "coinbase"),
            ("binance", "chainlink"),
            ("coinbase", "chainlink"),
        ]

        for a, b in pairs:
            ta = sources.get(a)
            tb = sources.get(b)
            if not ta or not tb:
                continue

            diff = abs(ta.price - tb.price)
            avg = (ta.price + tb.price) / 2
            div_pct = diff / avg

            if div_pct >= self.divergence_threshold:
                direction = "up" if ta.price > tb.price else "down"
                signal = DivergenceSignal(
                    source_a=a, source_b=b,
                    price_a=ta.price, price_b=tb.price,
                    divergence_pct=div_pct,
                    direction=direction,
                )
                self._divergence_signals.append(signal)
                logger.info(
                    f"[DIVERGENCE] {a}={ta.price:.2f} vs {b}={tb.price:.2f} "
                    f"({div_pct*100:.3f}%) direction={direction}"
                )
                for cb in self._callbacks:
                    try:
                        cb(signal)
                    except Exception as exc:
                        logger.error(f"Divergence callback error: {exc}")

    # ── Binance WebSocket ──────────────────────────────────────────────────

    async def _binance_stream(self) -> None:
        import websockets
        while self._running:
            try:
                logger.info("Connecting to Binance trade stream...")
                async with websockets.connect(BINANCE_WS_URL) as ws:
                    logger.info("Binance stream connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            price = float(data["p"])  # trade price
                            self._update_price("binance", price)
                        except (KeyError, ValueError, json.JSONDecodeError):
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Binance stream error: {exc} — reconnecting in 3s")
                await asyncio.sleep(3)

    # ── Coinbase WebSocket ─────────────────────────────────────────────────

    async def _coinbase_stream(self) -> None:
        import websockets
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "market_trades",
        })
        while self._running:
            try:
                logger.info("Connecting to Coinbase trade stream...")
                async with websockets.connect(COINBASE_WS_URL) as ws:
                    await ws.send(subscribe_msg)
                    logger.info("Coinbase stream connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            # Coinbase Advanced Trade format
                            events = data.get("events", [])
                            for event in events:
                                for trade in event.get("trades", []):
                                    price = float(trade["price"])
                                    self._update_price("coinbase", price)
                        except (KeyError, ValueError, json.JSONDecodeError):
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Coinbase stream error: {exc} — reconnecting in 3s")
                await asyncio.sleep(3)

    # ── Chainlink polling ──────────────────────────────────────────────────

    async def _chainlink_poll(self) -> None:
        """Poll Chainlink BTC/USD aggregator on Polygon via JSON-RPC."""
        # latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
        # answer is price * 10^8
        get_latest = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{
                "to": CHAINLINK_BTC_USD,
                "data": "0xfeaf968c",  # latestRoundData() selector
            }, "latest"],
        })

        import aiohttp
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    while self._running:
                        try:
                            async with session.post(
                                POLYGON_RPC_URL,
                                data=get_latest,
                                headers={"Content-Type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=5),
                            ) as resp:
                                data = await resp.json()
                                result_hex = data.get("result", "0x")
                                if result_hex and result_hex != "0x":
                                    # answer is the 2nd 32-byte word (offset 32)
                                    raw = bytes.fromhex(result_hex[2:])
                                    if len(raw) >= 64:
                                        answer = int.from_bytes(raw[32:64], "big")
                                        price = answer / 1e8
                                        if 10_000 < price < 500_000:
                                            self._update_price("chainlink", price)
                        except Exception as exc:
                            logger.debug(f"Chainlink poll error: {exc}")

                        await asyncio.sleep(15)  # Chainlink updates ~every 15-30s

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"Chainlink session error: {exc} — retrying in 30s")
                await asyncio.sleep(30)
