"""
Polymarket Trading Engine — autonomous 5-minute BTC market engine.

Public surface:
  TradingEngine   — orchestrator
  BaseStrategy    — subclass to write custom strategies
  StrategyAPI     — injected into strategy.run()
  Side            — YES / NO
  PaperWallet     — simulation backend
  PositionManager — stop-loss / take-profit monitor
  MultiSourcePriceFeed — Binance + Coinbase + Chainlink
  PnLLogger       — structured logging + chart generation
  indicators      — RSI, ATR, Bollinger, signal_strength
"""
from .engine import TradingEngine
from .strategy import BaseStrategy, StrategyAPI, Side, OrderReceipt, OrderResult
from .lifecycle import MarketSlot, SlotState, SlotPnL
from .simulator import PaperWallet
from .position_manager import PositionManager
from .price_feeds import MultiSourcePriceFeed, DivergenceSignal
from .pnl_logger import PnLLogger
from .backtester import Backtester, BacktestResults, fetch_candles
from . import indicators

__all__ = [
    "Backtester",
    "BacktestResults",
    "fetch_candles",
    "TradingEngine",
    "BaseStrategy",
    "StrategyAPI",
    "Side",
    "OrderReceipt",
    "OrderResult",
    "MarketSlot",
    "SlotState",
    "SlotPnL",
    "PaperWallet",
    "PositionManager",
    "MultiSourcePriceFeed",
    "DivergenceSignal",
    "PnLLogger",
    "indicators",
]
