# Trading Engine Guide

Complete reference for the `polymarket_mcp.engine` module — autonomous 5-minute BTC prediction market trading.

---

## Overview

The engine implements the **"early-bird"** pattern: subscribe to the *next* 5-minute Polymarket slot before it opens, so your strategy is ready the moment the window begins.

```
Backtest → Paper Wallet → Live
```

Each 5-minute BTC market window is one `MarketSlot`. The engine discovers upcoming slots, creates them with your chosen strategy, and runs the full lifecycle autonomously.

---

## Quick Start

### 1. Backtest first

```
run_backtest(
  strategy="orderbook_spread",
  days=30,
  budget_per_slot=20,
  initial_capital=500
)
```

Look for:
- **Win rate > 52%**
- **Sharpe ratio > 1.0**
- **Max drawdown < 15%**
- **Profit factor > 1.3**

### 2. Paper wallet (simulation)

```
start_engine(
  strategy="orderbook_spread",
  simulation=true,
  budget_per_slot=20
)
```

Monitor for 50+ slots. If profitable:

```
engine_status
paper_wallet_status
engine_pnl_history
generate_charts
```

### 3. Go live

```
start_engine(
  strategy="orderbook_spread",
  simulation=false,
  budget_per_slot=20
)
```

Requires `POLYGON_PRIVATE_KEY` and `POLYMARKET_API_KEY` in `.env`.

---

## Writing a Custom Strategy

Subclass `BaseStrategy` and implement `run(api)`.

```python
from polymarket_mcp.engine import BaseStrategy, StrategyAPI, Side
from polymarket_mcp.engine import indicators

class MyStrategy(BaseStrategy):
    name = "my_strategy"          # must be unique

    async def run(self, api: StrategyAPI) -> None:
        # 1. Collect data
        prices = []
        for _ in range(15):
            prices.append(await api.price())
            import asyncio; await asyncio.sleep(1)

        # 2. Compute indicators
        rsi_val = indicators.rsi(prices, period=10)
        ob = await api.orderbook(depth=5)

        # 3. Entry signal
        if rsi_val is None or ob.spread is None:
            return
        if ob.spread > 0.03:
            return                # too wide, skip

        if rsi_val < 35 and ob.best_ask:
            # Oversold → buy YES
            receipt = await api.buy(Side.YES, size_usd=15.0, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(receipt.price * 0.92)
                api.set_take_profit(receipt.price * 1.15)

        elif rsi_val > 65:
            # Overbought → buy NO
            receipt = await api.buy(Side.NO, size_usd=15.0)
            if receipt.is_filled:
                api.set_stop_loss(0.60)   # YES above 0.60 = stop
                api.set_take_profit(0.30) # YES below 0.30 = profit

    async def on_stop_loss_hit(self, api: StrategyAPI, price: float) -> None:
        """Called automatically by PositionManager when stop-loss triggers."""
        pos = await api.position()
        if pos:
            await api.sell(pos.side, pos.size_usd)

    async def on_take_profit_hit(self, api: StrategyAPI, price: float) -> None:
        pos = await api.position()
        if pos:
            await api.sell(pos.side, pos.size_usd)
```

### Register and use it

```python
from polymarket_mcp.engine.strategies import register_strategy
register_strategy("my_strategy", MyStrategy)
```

Then in Claude:
```
start_engine(strategy="my_strategy", simulation=true)
```

---

## StrategyAPI Reference

| Method | Description |
|--------|-------------|
| `await api.buy(side, size_usd, price=None)` | Place buy order. `price=None` → best ask |
| `await api.sell(side, size_usd, price=None)` | Exit position |
| `await api.cancel(order_id)` | Cancel pending order |
| `await api.orderbook(depth=10)` | `OrderbookSnapshot` with bids/asks |
| `await api.price()` | Current YES mid price (0–1) |
| `await api.position()` | `PositionSnapshot` or `None` |
| `api.set_stop_loss(price)` | Register stop-loss (auto-executed) |
| `api.set_take_profit(price)` | Register take-profit (auto-executed) |
| `api.market_id` | Current market condition ID |
| `api.price_to_beat` | BTC price that determines YES/NO |
| `api.seconds_remaining` | Seconds left in this window |
| `api.is_simulation` | `True` if paper wallet mode |
| `api.receipts` | All `OrderReceipt` objects this slot |

### OrderbookSnapshot

```python
ob = await api.orderbook()
ob.best_bid    # float or None
ob.best_ask    # float or None
ob.mid         # (bid + ask) / 2
ob.spread      # ask - bid
ob.depth("bid", levels=5)  # total USD liquidity
```

---

## Indicators

```python
from polymarket_mcp.engine import indicators

# RSI (0-100)
rsi_val = indicators.rsi(prices, period=14)

# ATR
atr_val = indicators.atr(highs, lows, closes, period=14)

# EMA
ema_val = indicators.ema(prices, period=20)

# Bollinger Bands
upper, mid, lower = indicators.bollinger(prices, period=20, std_dev=2.0)

# Cross-source divergence
div = indicators.divergence_score(binance_price, coinbase_price)

# Composite entry signal (0-1)
score = indicators.signal_strength(
    rsi_value=rsi_val,
    atr_value=atr_val,
    spread=ob.spread,
    divergence=div,
)
# Trade only when score > 0.6
```

### Rolling indicators (incremental)

```python
from polymarket_mcp.engine.indicators import RollingRSI, RollingATR

rsi_tracker = RollingRSI(period=14)
while True:
    p = await api.price()
    rsi_val = rsi_tracker.update(p)
    if rsi_tracker.is_oversold:   # RSI < 30
        ...
```

---

## Multi-Source Price Feed

```python
from polymarket_mcp.engine import MultiSourcePriceFeed

feed = MultiSourcePriceFeed(divergence_threshold=0.001)
await feed.start()

# In strategy
binance = feed.price_by_source("binance")
coinbase = feed.price_by_source("coinbase")
chainlink = feed.price_by_source("chainlink")
consensus = feed.consensus_price()    # median

# Latest divergence signal
sig = feed.latest_divergence_signal()
if sig and sig.divergence_pct > 0.001:
    if sig.is_bullish:   # Binance leading up
        await api.buy(Side.YES, 15.0)
```

---

## Backtesting

### Via MCP tool

```
run_backtest(
  strategy="orderbook_spread",
  days=60,
  budget_per_slot=20,
  initial_capital=500,
  symbol="BTCUSDT"
)
```

### Programmatically

```python
from polymarket_mcp.engine.backtester import Backtester, fetch_candles
from polymarket_mcp.engine.strategies import OrderbookSpreadStrategy

# Pre-fetch candles (optional — avoids re-downloading)
candles = fetch_candles(symbol="BTCUSDT", interval="5m", days=60)

bt = Backtester(
    strategy_class=OrderbookSpreadStrategy,
    budget_per_slot=20.0,
    initial_capital=500.0,
)
results = await bt.run(candles=candles)
print(results.summary())
```

### Metrics explained

| Metric | Good value | Description |
|--------|-----------|-------------|
| `win_rate` | > 52% | % of traded slots with positive PnL |
| `sharpe_ratio` | > 1.0 | Risk-adjusted return (annualized) |
| `max_drawdown_pct` | < 15% | Largest peak-to-trough equity drop |
| `profit_factor` | > 1.3 | Gross profit / gross loss |
| `avg_pnl` | > 0 | Average PnL per slot |

---

## PnL Logger & Charts

Logs are written to `~/.polymarket_engine/logs/` as JSONL files.

```
generate_charts
```

Produces:
- One HTML per slot: price path, buy/sell markers, stop-loss line
- One HTML for session: cumulative PnL curve + bar chart per slot

Open any `.html` in a browser — no server needed (Plotly CDN).

---

## Module Architecture

```
engine/
├── strategy.py        BaseStrategy, StrategyAPI, Side, OrderReceipt
├── lifecycle.py       MarketSlot, SlotState, SlotPnL
├── engine.py          TradingEngine (orchestrator)
├── simulator.py       PaperWallet (simulation fills)
├── position_manager.py PositionManager (stop-loss/take-profit)
├── price_feeds.py     MultiSourcePriceFeed (Binance+Coinbase+Chainlink)
├── indicators.py      RSI, ATR, Bollinger, EMA, VWAP, signal_strength
├── pnl_logger.py      PnLLogger + chart generation
├── backtester.py      Backtester + fetch_candles
└── strategies.py      OrderbookSpreadStrategy, DivergenceScalpStrategy
```

---

## Safety Notes

- Always backtest before paper wallet, paper wallet before live
- The engine respects all existing `SafetyLimits` from your `.env`
- Simulation mode (`simulation=true`) never touches real USDC
- Stop-loss is your last line of defense — always set one
- The article's insight: **exit at profit, don't hold to resolution**
- Your edge window may only work during specific hours — add time filters

---

## Inspiration

Based on: [Building a Polymarket BTC Trading Engine with Claude](https://github.com/KaustubhPatange/polymarket-trade-engine)

Key concepts from the article:
- Order book is the source of truth
- No market orders — everything is a limit order on Polymarket
- On-chain settlement delay: tokens not immediately re-sellable after buy
- 5% of edge cases will lose big — stop-loss is mandatory
- Patterns change — backtest continuously, don't just set-and-forget
