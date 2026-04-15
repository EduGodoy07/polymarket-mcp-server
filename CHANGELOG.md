# Changelog

All notable changes to the Polymarket MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0] - 2026-04-15

### 🤖 Autonomous Trading Engine

Major new module: `src/polymarket_mcp/engine/` — a complete autonomous trading engine for Polymarket 5-minute BTC prediction markets, implementing the "early-bird" lifecycle pattern.

#### Added — Engine Core
- **`engine/strategy.py`** — `BaseStrategy` abstract class + `StrategyAPI` injectable interface. Write strategies by subclassing `BaseStrategy` and implementing `run(api)`. API provides `buy()`, `sell()`, `orderbook()`, `price()`, `position()`, `set_stop_loss()`, `set_take_profit()`.
- **`engine/lifecycle.py`** — `MarketSlot` with `WAITING → RUNNING → COMPLETED` lifecycle. Slots are created in advance (early-bird) and execute exactly one strategy per 5-minute window. Includes `SlotPnL` dataclass with realized/resolved PnL tracking.
- **`engine/engine.py`** — `TradingEngine` orchestrator. Discovers upcoming BTC 5-minute markets via Gamma API, launches slots as asyncio tasks, aggregates PnL, supports concurrent slot limits.

#### Added — Simulation
- **`engine/simulator.py`** — `PaperWallet` for zero-risk strategy testing. Simulates: limit order fills vs real orderbook liquidity, Polygon on-chain settlement delay (12s), partial fills for thin books, FOK all-or-nothing semantics, taker fee (0.5%).

#### Added — Risk Management
- **`engine/position_manager.py`** — `PositionManager` background monitor. Polls open slot prices every 2s, triggers `on_stop_loss_hit` / `on_take_profit_hit` hooks on strategy, records `RiskEvent` objects.

#### Added — Price Feeds
- **`engine/price_feeds.py`** — `MultiSourcePriceFeed`. Connects simultaneously to Binance WebSocket trade stream, Coinbase Advanced Trade WebSocket, and Chainlink BTC/USD aggregator on Polygon (via JSON-RPC). Detects cross-source price divergences as leading indicators. Divergence threshold configurable (default 0.1%).

#### Added — Indicators
- **`engine/indicators.py`** — Pure-Python (no pandas/numpy) technical indicators: `rsi()`, `atr()`, `ema()`, `vwap()`, `bollinger()`, `divergence_score()`. Includes `RollingRSI` and `RollingATR` for incremental updates. `signal_strength()` composite scorer combining RSI extremes, spread tightness, and cross-source divergence.

#### Added — Logging & Charts
- **`engine/pnl_logger.py`** — `PnLLogger`. Writes structured JSONL trade logs per session. `generate_charts()` produces self-contained interactive HTML charts (Plotly CDN) per slot showing buy/sell events. `generate_session_chart()` produces cumulative PnL curve with slot bar chart. Charts saved to `~/.polymarket_engine/logs/charts/`.

#### Added — Backtesting
- **`engine/backtester.py`** — `Backtester` + `fetch_candles()`. Downloads BTC/USDT 5m OHLCV from Binance public REST API (no auth). Maps each candle to a Polymarket slot (open = price_to_beat, YES wins if close > open). Runs strategy with `BacktestStrategyAPI` — synthetic orderbook from OHLC, deterministic price path simulation, stop-loss/take-profit enforcement. Computes: `win_rate`, `total_pnl`, `avg_pnl`, `max_drawdown`, `sharpe_ratio`, `profit_factor`, `best/worst_slot`.

#### Added — Built-in Strategies
- **`engine/strategies.py`** — Two reference strategies:
  - `OrderbookSpreadStrategy` — enters when spread < 3% + RSI extreme (< 35 or > 65). Stop-loss -8%, take-profit +15%.
  - `DivergenceScalpStrategy` — trades on cross-source Binance/Coinbase divergence > 0.1%. Stop-loss ±6%, take-profit ±12%.
  - `register_strategy()` — register custom strategies by name.

#### Added — MCP Tools (8 new tools)
- **`start_engine`** — start autonomous engine (simulation or live)
- **`stop_engine`** — graceful shutdown with session summary
- **`engine_status`** — active slots, strategy, total PnL, paper balance
- **`engine_pnl_history`** — per-slot PnL breakdown (paginated)
- **`paper_wallet_status`** — balance, positions, fees, settlement state
- **`generate_charts`** — produce HTML charts, returns file paths
- **`price_feed_status`** — consensus BTC price + recent divergences
- **`run_backtest`** — backtest strategy on historical Binance OHLCV with full metrics

#### Changed
- `server.py` — routes 8 new engine tool calls, wires `polymarket_client` into engine tools on init, tool count updated 45 → 53
- `tools/__init__.py` — exports `engine_tools` module

---

## [0.1.0] - 2025-01-10

### 🎉 Initial Public Release

The first public release of Polymarket MCP Server - a complete AI-powered trading platform for Polymarket prediction markets.

### Added

#### Core Infrastructure
- Model Context Protocol (MCP) server implementation
- L1 authentication (Polygon wallet + EIP-712 signing)
- L2 authentication (API key + HMAC signatures)
- Auto-creation of API credentials
- Advanced token bucket rate limiter respecting all Polymarket API limits
- Configurable safety limits and risk management system
- Comprehensive error handling and logging

#### Market Discovery Tools (8 tools)
- `search_markets` - Search markets by keywords, slug, or filters
- `get_trending_markets` - Get markets with highest volume
- `filter_markets_by_category` - Filter by tags and categories
- `get_event_markets` - Get all markets for a specific event
- `get_featured_markets` - Get featured/promoted markets
- `get_closing_soon_markets` - Get markets closing within timeframe
- `get_sports_markets` - Get sports betting markets
- `get_crypto_markets` - Get cryptocurrency prediction markets

#### Market Analysis Tools (10 tools)
- `get_market_details` - Complete market information
- `get_current_price` - Current bid/ask prices
- `get_orderbook` - Full orderbook with depth
- `get_spread` - Calculate current spread
- `get_market_volume` - Volume statistics (24h, 7d, 30d)
- `get_liquidity` - Available liquidity in USD
- `get_price_history` - Historical price data
- `get_market_holders` - Top position holders
- `analyze_market_opportunity` - AI-powered analysis with recommendations
- `compare_markets` - Compare multiple markets side-by-side

#### Trading Tools (12 tools)
- `create_limit_order` - Create limit orders (GTC/GTD/FOK/FAK)
- `create_market_order` - Execute market orders
- `create_batch_orders` - Submit multiple orders efficiently
- `suggest_order_price` - AI-suggested optimal pricing
- `get_order_status` - Check specific order status
- `get_open_orders` - List all active orders
- `get_order_history` - Historical order data
- `cancel_order` - Cancel specific order
- `cancel_market_orders` - Cancel all orders in a market
- `cancel_all_orders` - Emergency cancel all orders
- `execute_smart_trade` - Natural language trading with intent parsing
- `rebalance_position` - Auto-adjust position to target size

#### Portfolio Management Tools (8 tools)
- `get_all_positions` - All user positions with filters
- `get_position_details` - Detailed position view
- `get_portfolio_value` - Total portfolio value calculation
- `get_pnl_summary` - Profit/loss overview
- `get_trade_history` - Historical trades with filters
- `get_activity_log` - On-chain activity tracking
- `analyze_portfolio_risk` - Risk assessment and scoring
- `suggest_portfolio_actions` - AI-powered optimization suggestions

#### Real-time Monitoring Tools (7 tools)
- `subscribe_market_prices` - Monitor price changes via WebSocket
- `subscribe_orderbook_updates` - Real-time orderbook updates
- `subscribe_user_orders` - User order status monitoring
- `subscribe_user_trades` - User trade execution alerts
- `subscribe_market_resolution` - Market resolution notifications
- `get_realtime_status` - WebSocket subscription status
- `unsubscribe_realtime` - Remove subscriptions

#### Safety & Risk Management
- Configurable order size limits
- Total portfolio exposure caps
- Per-market position limits
- Liquidity requirement validation
- Spread tolerance checks
- Confirmation thresholds for large orders
- Pre-trade safety validation

#### Infrastructure Features
- WebSocket manager with auto-reconnect
- Dual WebSocket connections (CLOB + Real-time)
- Token bucket rate limiting (all endpoint categories)
- HMAC authentication for WebSockets
- Event routing and notification system
- Subscription tracking and statistics

#### Testing
- Comprehensive test suite (1,900+ lines)
- Real API integration (NO MOCKS)
- Unit tests for all tools
- Integration tests for workflows
- Test runners and examples

#### Documentation
- Complete README with setup instructions
- Detailed SETUP_GUIDE.md
- Tools Reference (TOOLS_REFERENCE.md)
- Agent Integration Guide
- Trading Architecture documentation
- WebSocket Integration guide
- Usage examples and code samples
- CONTRIBUTING guidelines

### Technical Specifications

- **Python**: 3.10+
- **Total Lines of Code**: ~10,000+
- **Tools**: 45 comprehensive tools
- **API Integration**: CLOB API, Gamma API, Data API, WebSocket
- **Authentication**: L1 (EIP-712) + L2 (HMAC)
- **Rate Limiting**: Token bucket with exponential backoff
- **Dependencies**: MCP SDK, py-clob-client, websockets, eth-account, httpx, pydantic

### Credits

- **Created by**: Caio Vicentino
- **Communities**: Yield Hacker, Renda Cripto, Cultura Builder
- **Powered by**: Claude Code (Anthropic)

---

## [Unreleased]

### Planned Features
- CI/CD pipeline (GitHub Actions)
- Enhanced AI analysis tools
- Portfolio strategy templates
- Market alerts and notifications
- Performance analytics dashboard
- Multi-wallet support
- Advanced order types (trailing stop, OCO)
- Historical backtesting framework

---

## Release Notes Template

For future releases, use this template:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added
- New features

### Changed
- Changes to existing features

### Deprecated
- Features that will be removed

### Removed
- Removed features

### Fixed
- Bug fixes

### Security
- Security improvements
```

---

<div align="center">

**Maintained by Caio Vicentino and the Polymarket MCP community**

</div>
