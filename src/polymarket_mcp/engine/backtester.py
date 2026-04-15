"""
Backtesting engine for Polymarket 5-minute BTC strategies.

Downloads historical BTC/USDT 5-minute OHLCV candles from Binance's
public REST API (no auth required) and simulates each candle as a
Polymarket 5-minute prediction market slot.

How each candle maps to a Polymarket slot:
  - price_to_beat  = candle open price
  - YES wins       = close > open  (BTC went up)
  - NO wins        = close <= open (BTC went down/flat)
  - YES token price evolves 0→1 mapping the BTC price move within the candle
  - Strategy gets a synthetic StrategyAPI with:
      • orderbook()  — tight synthetic book around current simulated price
      • price()      — current YES token price (0-1 normalized from OHLC)
      • buy/sell     — paper fills, no network calls

Metrics computed:
  win_rate, total_pnl, avg_pnl, max_drawdown, sharpe_ratio,
  profit_factor, best/worst slot, per-slot breakdown
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Type

from .strategy import (
    BaseStrategy, StrategyAPI, Side,
    OrderReceipt, OrderResult, PositionSnapshot,
    OrderbookSnapshot, OrderbookLevel
)
from .lifecycle import MarketSlot, SlotState, SLOT_DURATION_SECONDS
from .simulator import TAKER_FEE_RATE

logger = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_KLINES_PER_REQUEST = 1000


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class Candle:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def direction(self) -> str:
        return "up" if self.close > self.open else "down"

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low

    @property
    def yes_wins(self) -> bool:
        return self.close > self.open


@dataclass
class BacktestSlotResult:
    candle: Candle
    strategy_name: str
    receipts: List[OrderReceipt]
    outcome: str       # "YES" | "NO" | "FLAT"
    pnl: float
    bought_usd: float
    sold_usd: float
    resolved_value: float
    correct: bool      # strategy bet on the right side

    def to_dict(self) -> Dict[str, Any]:
        return {
            "open_time": self.candle.open_time.isoformat(),
            "open": round(self.candle.open, 2),
            "close": round(self.candle.close, 2),
            "direction": self.candle.direction,
            "outcome": self.outcome,
            "correct": self.correct,
            "pnl": round(self.pnl, 4),
            "bought": round(self.bought_usd, 4),
            "sold": round(self.sold_usd, 4),
            "trades": len(self.receipts),
        }


@dataclass
class BacktestResults:
    strategy_name: str
    symbol: str
    interval: str
    start: datetime
    end: datetime
    initial_capital: float
    slots: List[BacktestSlotResult] = field(default_factory=list)

    # Computed metrics (call .compute() after adding slots)
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    best_slot: float = 0.0
    worst_slot: float = 0.0
    total_fees: float = 0.0
    traded_slots: int = 0  # slots where strategy actually placed orders

    def compute(self) -> "BacktestResults":
        """Calculate all aggregate metrics from slot results."""
        if not self.slots:
            return self

        pnls = [s.pnl for s in self.slots]
        traded = [s for s in self.slots if s.receipts]

        self.total_pnl = sum(pnls)
        self.avg_pnl = self.total_pnl / len(pnls)
        self.best_slot = max(pnls)
        self.worst_slot = min(pnls)
        self.traded_slots = len(traded)
        self.total_fees = sum(
            s.bought_usd * TAKER_FEE_RATE + s.sold_usd * TAKER_FEE_RATE
            for s in self.slots
        )

        wins = [s for s in traded if s.pnl > 0]
        self.win_rate = len(wins) / len(traded) * 100 if traded else 0.0

        # Max drawdown (on cumulative PnL curve)
        cumulative = []
        running = self.initial_capital
        peak = running
        max_dd = 0.0
        for pnl in pnls:
            running += pnl
            peak = max(peak, running)
            dd = (peak - running) / peak * 100
            max_dd = max(max_dd, dd)
            cumulative.append(running)
        self.max_drawdown = max_dd

        # Sharpe ratio (annualized, assuming ~288 five-min slots per day)
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            std = math.sqrt(variance) if variance > 0 else 0.0
            daily_slots = 288
            annualized_factor = math.sqrt(365 * daily_slots)
            self.sharpe_ratio = (mean / std * annualized_factor) if std > 0 else 0.0

        # Profit factor = gross profit / gross loss
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        self.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        return self

    def summary(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "symbol": self.symbol,
            "period": f"{self.start.date()} → {self.end.date()}",
            "total_slots": len(self.slots),
            "traded_slots": self.traded_slots,
            "initial_capital": self.initial_capital,
            "final_capital": round(self.initial_capital + self.total_pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
            "total_pnl_pct": round(self.total_pnl / self.initial_capital * 100, 2),
            "win_rate": round(self.win_rate, 1),
            "avg_pnl": round(self.avg_pnl, 4),
            "best_slot": round(self.best_slot, 4),
            "worst_slot": round(self.worst_slot, 4),
            "max_drawdown_pct": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "profit_factor": round(self.profit_factor, 3),
            "total_fees": round(self.total_fees, 4),
        }

    def slot_breakdown(self, n: int = 20) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.slots[-n:]]


# ── Data fetching ──────────────────────────────────────────────────────────

def fetch_candles(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    days: int = 30,
    end_time: Optional[datetime] = None,
) -> List[Candle]:
    """
    Fetch historical OHLCV candles from Binance public REST API.
    No authentication required.

    Args:
        symbol: Trading pair (default BTCUSDT)
        interval: Candle interval: 1m, 5m, 15m, 1h, etc.
        days: Number of days of history to fetch
        end_time: End of range (default: now)

    Returns:
        List of Candle objects sorted oldest-first
    """
    end_dt = end_time or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_candles = []
    current_start = start_ms

    logger.info(
        f"Fetching {days}d of {interval} {symbol} candles from Binance..."
    )

    while current_start < end_ms:
        params = (
            f"symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}"
            f"&limit={MAX_KLINES_PER_REQUEST}"
        )
        url = f"{BINANCE_KLINES_URL}?{params}"

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            raise RuntimeError(f"Binance API error: {exc}") from exc

        if not data:
            break

        for k in data:
            candle = Candle(
                open_time=datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                close_time=datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
            )
            all_candles.append(candle)

        # Advance past last candle's open time
        current_start = data[-1][0] + 1

        if len(data) < MAX_KLINES_PER_REQUEST:
            break

        time.sleep(0.2)  # polite rate limiting

    logger.info(f"Fetched {len(all_candles)} candles ({days}d of {interval} {symbol})")
    return all_candles


# ── Synthetic StrategyAPI ─────────────────────────────────────────────────

class BacktestStrategyAPI(StrategyAPI):
    """
    Synthetic StrategyAPI backed by a single OHLCV candle.

    Simulates price movement within the candle using a path interpolated
    from open→close with noise proportional to the high-low range.
    """

    def __init__(self, candle: Candle, budget: float, slot):
        self._candle = candle
        self._budget = budget
        self._balance = budget
        self._receipts: List[OrderReceipt] = []
        self._position: Optional[PositionSnapshot] = None
        self._position_side: Optional[Side] = None
        self._position_shares: float = 0.0
        self._position_entry: float = 0.0
        self._position_cost: float = 0.0
        self._stop_loss: Optional[float] = None
        self._take_profit: Optional[float] = None
        self._slot = slot
        self._sim_step = 0
        self._sim_prices = self._build_price_path()
        self.simulation = True

    def _build_price_path(self) -> List[float]:
        """
        Build a synthetic YES token price path (0-1) for this candle.

        Methodology:
          - YES token price = probability BTC closes above open
          - Starts near 0.50 (50/50 at open)
          - Moves toward ~0.80 if close > open, ~0.20 if close < open
          - With noise proportional to (high - low) / open
        """
        steps = 60  # simulate 60 ticks over 5 minutes (~5s each)
        o = self._candle.open
        c = self._candle.close
        h = self._candle.high
        lo = self._candle.low

        # Final YES price maps the BTC move to probability space
        # If BTC went up 1%, YES ~ 0.75; down 1% ~ 0.25
        move_pct = (c - o) / o
        final_yes = 0.5 + move_pct * 25  # 1% BTC move → YES moves 0.25
        final_yes = max(0.05, min(0.95, final_yes))

        # Noise scale from ATR of the candle
        noise_scale = (h - lo) / o * 0.5

        path = [0.50]
        rng = random.Random(int(o * 1000) % (2**31))  # deterministic per candle

        for i in range(1, steps):
            t = i / (steps - 1)
            # Drift toward final_yes
            drift = (final_yes - path[-1]) * (2.0 / (steps - i + 1))
            noise = rng.gauss(0, noise_scale * 0.3)
            next_p = path[-1] + drift + noise
            next_p = max(0.02, min(0.98, next_p))
            path.append(next_p)

        return path

    def _current_yes_price(self) -> float:
        idx = min(self._sim_step, len(self._sim_prices) - 1)
        return self._sim_prices[idx]

    # ── StrategyAPI overrides ─────────────────────────────────────────────

    async def buy(self, side: Side, size_usd: float, price=None, order_type="GTC") -> OrderReceipt:
        import uuid
        order_id = str(uuid.uuid4())[:8]

        current = self._current_yes_price()
        fill_price = price if price else (current + 0.005)  # simulate spread
        fill_price = max(0.01, min(0.99, fill_price))

        effective_size = min(size_usd, self._balance)
        if effective_size < 0.01:
            receipt = OrderReceipt(
                order_id=order_id, side=side, price=fill_price,
                size_usd=size_usd, result=OrderResult.REJECTED,
                filled_size_usd=0.0, error="Insufficient backtest balance"
            )
            self._receipts.append(receipt)
            return receipt

        fee = effective_size * TAKER_FEE_RATE
        shares = effective_size / fill_price
        self._balance -= (effective_size + fee)

        self._position_side = side
        self._position_shares += shares
        self._position_entry = fill_price
        self._position_cost += effective_size

        receipt = OrderReceipt(
            order_id=order_id, side=side, price=fill_price,
            size_usd=effective_size, result=OrderResult.FILLED,
            filled_size_usd=effective_size,
        )
        self._receipts.append(receipt)
        return receipt

    async def sell(self, side: Side, size_usd: float, price=None, order_type="GTC") -> OrderReceipt:
        import uuid
        order_id = str(uuid.uuid4())[:8]

        if self._position_shares < 0.001:
            receipt = OrderReceipt(
                order_id=order_id, side=side, price=price or 0.5,
                size_usd=size_usd, result=OrderResult.REJECTED,
                filled_size_usd=0.0, error="No position"
            )
            self._receipts.append(receipt)
            return receipt

        current = self._current_yes_price()
        fill_price = price if price else (current - 0.005)
        fill_price = max(0.01, min(0.99, fill_price))

        shares_to_sell = min(self._position_shares, size_usd / fill_price)
        proceeds = shares_to_sell * fill_price
        fee = proceeds * TAKER_FEE_RATE
        self._balance += (proceeds - fee)
        self._position_shares -= shares_to_sell

        receipt = OrderReceipt(
            order_id=order_id, side=side, price=fill_price,
            size_usd=proceeds, result=OrderResult.FILLED,
            filled_size_usd=proceeds,
        )
        receipt.is_sell = True
        self._receipts.append(receipt)
        return receipt

    async def orderbook(self, depth: int = 10) -> OrderbookSnapshot:
        p = self._current_yes_price()
        spread = 0.008
        bids = [OrderbookLevel(round(p - spread * (i + 1), 4), 100.0) for i in range(depth)]
        asks = [OrderbookLevel(round(p + spread * (i + 1), 4), 100.0) for i in range(depth)]
        return OrderbookSnapshot(
            token_id="backtest",
            bids=bids,
            asks=asks,
            timestamp=self._candle.open_time,
        )

    async def price(self) -> float:
        self._sim_step += 1  # advance simulation clock
        return self._current_yes_price()

    async def position(self) -> Optional[PositionSnapshot]:
        if self._position_shares < 0.001 or self._position_side is None:
            return None
        current = self._current_yes_price()
        entry = self._position_entry
        size = self._position_cost
        pnl = (current - entry) * self._position_shares
        pnl_pct = pnl / size * 100 if size > 0 else 0
        return PositionSnapshot(
            side=self._position_side,
            entry_price=entry,
            size_usd=size,
            current_price=current,
            unrealized_pnl=pnl,
            unrealized_pnl_pct=pnl_pct,
        )

    def set_stop_loss(self, price: float) -> None:
        self._stop_loss = price
        self._slot.stop_loss_price = price

    def set_take_profit(self, price: float) -> None:
        self._take_profit = price
        self._slot.take_profit_price = price

    @property
    def market_id(self) -> str:
        return f"backtest_{self._candle.open_time.strftime('%Y%m%d_%H%M')}"

    @property
    def price_to_beat(self) -> float:
        return self._candle.open

    @property
    def seconds_remaining(self) -> float:
        step_seconds = SLOT_DURATION_SECONDS / len(self._sim_prices)
        remaining_steps = max(0, len(self._sim_prices) - self._sim_step)
        return remaining_steps * step_seconds

    @property
    def receipts(self) -> List[OrderReceipt]:
        return list(self._receipts)

    def resolve(self) -> Tuple[float, str]:
        """
        Resolve this slot. Returns (pnl, outcome).

        Remaining open positions are settled at resolution price:
          YES wins (close > open) → YES shares = 1.0 USDC
          NO wins                 → NO shares = 1.0 USDC
          Losing shares = 0
        """
        outcome = "YES" if self._candle.yes_wins else "NO"

        resolved_value = 0.0
        if self._position_shares > 0.001 and self._position_side is not None:
            if self._position_side.value == outcome:
                # Winning side: each share = 1 USDC
                resolved_value = self._position_shares * 1.0
            else:
                # Losing side: shares = 0
                resolved_value = 0.0
            self._balance += resolved_value

        bought = sum(
            r.filled_size_usd for r in self._receipts
            if not getattr(r, "is_sell", False)
        )
        sold = sum(
            r.filled_size_usd for r in self._receipts
            if getattr(r, "is_sell", False)
        )
        pnl = (self._balance - (self._budget - bought)) + resolved_value - bought

        return pnl, outcome


# ── Backtest runner ────────────────────────────────────────────────────────

class Backtester:
    """
    Runs a BaseStrategy against historical Binance OHLCV data.

    Usage
    -----
    bt = Backtester(strategy_class=OrderbookSpreadStrategy)
    results = await bt.run(symbol="BTCUSDT", days=30, budget=500.0)
    print(results.summary())
    """

    def __init__(
        self,
        strategy_class: Type[BaseStrategy],
        budget_per_slot: float = 20.0,
        initial_capital: float = 500.0,
    ):
        self.strategy_class = strategy_class
        self.budget_per_slot = budget_per_slot
        self.initial_capital = initial_capital

    async def run(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        days: int = 30,
        end_time: Optional[datetime] = None,
        candles: Optional[List[Candle]] = None,
    ) -> BacktestResults:
        """
        Run the backtest.

        Args:
            symbol: Binance trading pair
            interval: Candle interval (should be 5m for Polymarket 5m markets)
            days: Days of history to use
            end_time: End of backtest window (default: now)
            candles: Pre-loaded candles (skips Binance fetch if provided)

        Returns:
            BacktestResults with full metrics
        """
        if candles is None:
            candles = fetch_candles(symbol=symbol, interval=interval, days=days, end_time=end_time)

        if not candles:
            raise ValueError("No candles fetched — check symbol/interval/dates")

        results = BacktestResults(
            strategy_name=self.strategy_class.name,
            symbol=symbol,
            interval=interval,
            start=candles[0].open_time,
            end=candles[-1].close_time,
            initial_capital=self.initial_capital,
        )

        logger.info(
            f"Backtesting '{self.strategy_class.name}' on {len(candles)} "
            f"{interval} {symbol} candles..."
        )

        for i, candle in enumerate(candles):
            slot_result = await self._run_candle(candle)
            results.slots.append(slot_result)

            if (i + 1) % 100 == 0:
                completed_pnl = sum(s.pnl for s in results.slots)
                logger.info(
                    f"  {i+1}/{len(candles)} candles processed "
                    f"| running PnL: ${completed_pnl:+.2f}"
                )

        results.compute()
        logger.info(
            f"Backtest complete: "
            f"PnL=${results.total_pnl:+.2f} "
            f"WR={results.win_rate:.1f}% "
            f"Sharpe={results.sharpe_ratio:.2f} "
            f"MaxDD={results.max_drawdown:.1f}%"
        )
        return results

    async def _run_candle(self, candle: Candle) -> BacktestSlotResult:
        """Simulate one 5-minute market slot against a single candle."""
        strategy = self.strategy_class()

        # Create a minimal synthetic slot (no network access)
        slot = _SyntheticSlot(candle)
        api = BacktestStrategyAPI(candle=candle, budget=self.budget_per_slot, slot=slot)
        slot.api = api

        # Run strategy with a tight timeout (no real async waits in backtest)
        try:
            await asyncio.wait_for(
                self._run_strategy(strategy, api),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.debug(f"Strategy error on {candle.open_time}: {exc}")

        # Apply stop-loss / take-profit against price path
        await self._check_risk_exits(strategy, api, slot)

        # Resolve the slot
        pnl, outcome = api.resolve()

        # Determine if the strategy bet correctly
        bought_side = None
        for r in api.receipts:
            if not getattr(r, "is_sell", False) and r.result == OrderResult.FILLED:
                bought_side = r.side
                break

        correct = (
            (bought_side == Side.YES and outcome == "YES") or
            (bought_side == Side.NO and outcome == "NO")
        ) if bought_side else False

        bought = sum(
            r.filled_size_usd for r in api.receipts
            if not getattr(r, "is_sell", False)
        )
        sold = sum(
            r.filled_size_usd for r in api.receipts
            if getattr(r, "is_sell", False)
        )

        return BacktestSlotResult(
            candle=candle,
            strategy_name=strategy.name,
            receipts=api.receipts,
            outcome=outcome,
            pnl=pnl,
            bought_usd=bought,
            sold_usd=sold,
            resolved_value=api._position_shares * (1.0 if correct else 0.0),
            correct=correct,
        )

    async def _run_strategy(self, strategy: BaseStrategy, api: BacktestStrategyAPI) -> None:
        """Run strategy.run() but replace asyncio.sleep calls with sim step advances."""
        # Monkey-patch asyncio.sleep to advance simulation instead of waiting
        original_sleep = asyncio.sleep

        async def sim_sleep(delay: float) -> None:
            steps = max(1, int(delay))
            api._sim_step = min(api._sim_step + steps, len(api._sim_prices) - 1)

        asyncio.sleep = sim_sleep
        try:
            await strategy.run(api)
        finally:
            asyncio.sleep = original_sleep

    async def _check_risk_exits(
        self,
        strategy: BaseStrategy,
        api: BacktestStrategyAPI,
        slot,
    ) -> None:
        """
        Scan the price path for stop-loss and take-profit hits.
        Executes the strategy's exit hook if triggered.
        """
        if api._position_shares < 0.001:
            return

        stop = api._stop_loss
        tp = api._take_profit

        if stop is None and tp is None:
            return

        for price in api._sim_prices[api._sim_step:]:
            if stop is not None and price <= stop:
                logger.debug(f"[BACKTEST] Stop-loss hit @ {price:.4f}")
                await strategy.on_stop_loss_hit(api, price)
                return
            if tp is not None and price >= tp:
                logger.debug(f"[BACKTEST] Take-profit hit @ {price:.4f}")
                await strategy.on_take_profit_hit(api, price)
                return


class _SyntheticSlot:
    """Minimal slot-like object for BacktestStrategyAPI."""
    def __init__(self, candle: Candle):
        self.market_id = f"bt_{candle.open_time.strftime('%Y%m%d_%H%M')}"
        self.yes_token_id = "bt_yes"
        self.no_token_id = "bt_no"
        self.price_to_beat = candle.open
        self.stop_loss_price: Optional[float] = None
        self.take_profit_price: Optional[float] = None
        self.state = SlotState.RUNNING
        self.api = None
