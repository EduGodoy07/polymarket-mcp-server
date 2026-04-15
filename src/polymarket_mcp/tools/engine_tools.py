"""
MCP tools for controlling the TradingEngine from Claude.

Exposes 8 tools:
  start_engine         — start autonomous trading (sim or live)
  stop_engine          — graceful shutdown
  engine_status        — current state, active slots, PnL
  engine_pnl_history   — all completed slot results
  paper_wallet_status  — simulation wallet balance + positions
  generate_charts      — produce HTML charts from session logs
  price_feed_status    — multi-source BTC price + divergences
  run_backtest         — backtest a strategy on historical Binance OHLCV data
"""
import logging
from typing import Any, Dict, List, Optional

import mcp.types as types

logger = logging.getLogger(__name__)

# Global engine instance (set by server.py on first start_engine call)
_engine = None
_price_feed = None
_pnl_logger = None


def get_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="start_engine",
            description=(
                "Start the autonomous Polymarket BTC trading engine. "
                "Discovers upcoming 5-minute BTC markets and trades them with the chosen strategy. "
                "Use simulation=true (default) for paper trading. "
                "Requires POLYGON_PRIVATE_KEY configured for live mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["orderbook_spread", "divergence_scalp"],
                        "default": "orderbook_spread",
                        "description": "Strategy to use"
                    },
                    "simulation": {
                        "type": "boolean",
                        "default": True,
                        "description": "true=paper wallet, false=real USDC"
                    },
                    "budget_per_slot": {
                        "type": "number",
                        "default": 20.0,
                        "description": "Max USDC to risk per 5-minute window"
                    },
                    "enable_price_feeds": {
                        "type": "boolean",
                        "default": True,
                        "description": "Connect Binance+Coinbase+Chainlink feeds"
                    },
                },
                "required": []
            }
        ),
        types.Tool(
            name="stop_engine",
            description="Gracefully stop the trading engine and cancel any active slots.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        types.Tool(
            name="engine_status",
            description=(
                "Get the current state of the trading engine: "
                "active slots, strategy, total PnL, and paper wallet balance."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        types.Tool(
            name="engine_pnl_history",
            description="Get the PnL breakdown for all completed market slots this session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max number of slots to return"
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="paper_wallet_status",
            description="Get paper wallet balance, open positions, and trade statistics.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        types.Tool(
            name="generate_charts",
            description=(
                "Generate interactive HTML charts from this session's trade logs. "
                "Returns file paths to the created charts."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        types.Tool(
            name="price_feed_status",
            description=(
                "Get current BTC prices from all sources (Binance, Coinbase, Chainlink), "
                "consensus price, and recent divergence signals."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        types.Tool(
            name="run_backtest",
            description=(
                "Backtest a trading strategy against historical BTC/USDT 5-minute candles "
                "from Binance (free public API, no auth needed). "
                "Returns win rate, total PnL, Sharpe ratio, max drawdown, and per-slot breakdown. "
                "Use this to validate a strategy before running it with real or paper money."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["orderbook_spread", "divergence_scalp"],
                        "default": "orderbook_spread",
                        "description": "Strategy to backtest"
                    },
                    "days": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 365,
                        "description": "Days of historical data to use"
                    },
                    "budget_per_slot": {
                        "type": "number",
                        "default": 20.0,
                        "description": "Simulated USDC budget per 5-minute slot"
                    },
                    "initial_capital": {
                        "type": "number",
                        "default": 500.0,
                        "description": "Starting capital for drawdown calculations"
                    },
                    "symbol": {
                        "type": "string",
                        "default": "BTCUSDT",
                        "description": "Binance trading pair (e.g. BTCUSDT, ETHUSDT)"
                    },
                    "show_slots": {
                        "type": "integer",
                        "default": 10,
                        "description": "Number of recent slots to include in the breakdown"
                    },
                },
                "required": []
            }
        ),
    ]


async def handle_tool_call(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    global _engine, _price_feed, _pnl_logger

    try:
        if name == "start_engine":
            return await _start_engine(arguments)
        elif name == "stop_engine":
            return await _stop_engine()
        elif name == "engine_status":
            return _engine_status()
        elif name == "engine_pnl_history":
            return _pnl_history(arguments)
        elif name == "paper_wallet_status":
            return _paper_wallet_status()
        elif name == "generate_charts":
            return await _generate_charts()
        elif name == "price_feed_status":
            return _price_feed_status()
        elif name == "run_backtest":
            return await _run_backtest(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        logger.error(f"engine_tools error [{name}]: {exc}", exc_info=True)
        return [types.TextContent(type="text", text=f"Error: {exc}")]


# ── Tool handlers ─────────────────────────────────────────────────────────

async def _start_engine(args: Dict[str, Any]) -> List[types.TextContent]:
    global _engine, _price_feed, _pnl_logger

    if _engine and _engine.state == "RUNNING":
        return [types.TextContent(type="text", text="Engine already running. Use engine_status.")]

    simulation = args.get("simulation", True)
    budget = float(args.get("budget_per_slot", 20.0))
    strategy_name = args.get("strategy", "orderbook_spread")
    enable_feeds = args.get("enable_price_feeds", True)

    # Import here to avoid circular imports at module load time
    from ..engine import TradingEngine, PositionManager, MultiSourcePriceFeed, PnLLogger
    from ..engine.strategies import get_strategy_class

    strategy_class = get_strategy_class(strategy_name)

    _pnl_logger = PnLLogger(strategy_name=strategy_name)

    position_manager = PositionManager(pnl_logger=_pnl_logger)
    await position_manager.start()

    if enable_feeds:
        _price_feed = MultiSourcePriceFeed()
        await _price_feed.start()
    else:
        _price_feed = None

    # We need the client — it's initialized in server.py
    # Engine tools get the client via _get_client()
    client = _get_client()
    if client is None and not simulation:
        return [types.TextContent(
            type="text",
            text="No Polymarket client available. Configure credentials or use simulation=true."
        )]

    _engine = TradingEngine(
        client=client,
        strategy_class=strategy_class,
        simulation=simulation,
        budget_per_slot=budget,
        price_feeds=_price_feed,
        pnl_logger=_pnl_logger,
        position_manager=position_manager,
    )
    await _engine.start()

    mode = "SIMULATION (paper wallet)" if simulation else "LIVE (real USDC)"
    return [types.TextContent(type="text", text=(
        f"Trading Engine started\n"
        f"{'='*40}\n"
        f"Mode:     {mode}\n"
        f"Strategy: {strategy_name}\n"
        f"Budget:   ${budget:.2f} per slot\n"
        f"Feeds:    {'Binance+Coinbase+Chainlink' if enable_feeds else 'disabled'}\n\n"
        f"The engine is now watching for upcoming BTC 5-minute markets.\n"
        f"Use engine_status to monitor progress."
    ))]


async def _stop_engine() -> List[types.TextContent]:
    global _engine, _price_feed

    if _engine is None:
        return [types.TextContent(type="text", text="Engine not running.")]

    await _engine.stop()

    if _price_feed:
        await _price_feed.stop()
        _price_feed = None

    summary = _pnl_logger.session_summary() if _pnl_logger else {}

    return [types.TextContent(type="text", text=(
        f"Engine stopped.\n\n"
        f"Session summary:\n"
        + "\n".join(f"  {k}: {v}" for k, v in summary.items())
    ))]


def _engine_status() -> List[types.TextContent]:
    if _engine is None:
        return [types.TextContent(type="text", text="Engine not running. Use start_engine.")]

    s = _engine.get_status()
    active = s["active_slots"]
    lines = [
        f"Engine Status",
        f"{'='*40}",
        f"State:        {s['state']}",
        f"Strategy:     {s['strategy']}",
        f"Mode:         {'simulation' if s['simulation'] else 'live'}",
        f"Budget/slot:  ${s['budget_per_slot']:.2f}",
        f"Completed:    {s['completed_count']} slots",
        f"Total PnL:    ${s['total_pnl_usd']:+.4f}",
    ]
    if s.get("paper_balance") is not None:
        lines.append(f"Paper wallet: ${s['paper_balance']:.2f}")

    if active:
        lines.append(f"\nActive Slots ({len(active)}):")
        for slot in active:
            lines.append(
                f"  {slot['market_id'][:14]}  "
                f"state={slot['state']}  "
                f"remaining={slot['seconds_remaining']:.0f}s  "
                f"strategy={slot['strategy']}"
            )
    else:
        lines.append("\nNo active slots (waiting for next market window)")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _pnl_history(args: Dict[str, Any]) -> List[types.TextContent]:
    if _engine is None:
        return [types.TextContent(type="text", text="Engine not running.")]

    limit = args.get("limit", 20)
    history = _engine.get_pnl_history()[-limit:]

    if not history:
        return [types.TextContent(type="text", text="No completed slots yet.")]

    total = sum(h["total_pnl"] for h in history)
    lines = [f"PnL History (last {len(history)} slots)\n{'='*40}"]
    for h in history:
        pnl = h["total_pnl"]
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"  {h['market_id'][:14]}  "
            f"PnL={sign}${pnl:.4f}  "
            f"bought=${h['bought_usd']:.2f}  "
            f"sold=${h['sold_usd']:.2f}"
        )
    lines.append(f"\nTotal: ${total:+.4f}")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _paper_wallet_status() -> List[types.TextContent]:
    if _engine is None or _engine._simulator is None:
        return [types.TextContent(type="text", text="No paper wallet (engine not running in simulation mode).")]

    s = _engine._simulator.get_status()
    lines = [
        f"Paper Wallet",
        f"{'='*40}",
        f"Balance:      ${s['balance']:.4f}",
        f"Initial:      ${s['initial_balance']:.2f}",
        f"Total PnL:    ${s['total_pnl']:+.4f} ({s['total_pnl_pct']:+.2f}%)",
        f"Fees paid:    ${s['total_fees_paid']:.4f}",
        f"Trades:       {s['total_trades']}",
        f"Open pos.:    {s['open_positions']}",
    ]
    if s["positions"]:
        lines.append("\nOpen Positions:")
        for pos in s["positions"]:
            lines.append(
                f"  {pos['side']} {pos['shares']:.4f} shares @ "
                f"{pos['entry_price']:.4f}  "
                f"settled={'yes' if pos['settled'] else 'pending'}"
            )
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _generate_charts() -> List[types.TextContent]:
    if _pnl_logger is None:
        return [types.TextContent(type="text", text="No session logger active.")]

    paths = _pnl_logger.generate_charts()
    session_path = _pnl_logger.generate_session_chart()

    all_paths = paths + ([session_path] if session_path else [])
    if not all_paths:
        return [types.TextContent(type="text", text="No completed slots to chart yet.")]

    lines = [f"Generated {len(all_paths)} chart(s):"]
    for p in all_paths:
        lines.append(f"  {p}")
    lines.append("\nOpen any .html file in your browser to view interactively.")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _price_feed_status() -> List[types.TextContent]:
    if _price_feed is None:
        return [types.TextContent(type="text", text="Price feeds not running. Start engine with enable_price_feeds=true.")]

    s = _price_feed.get_status()
    lines = [
        f"BTC Price Feeds",
        f"{'='*40}",
        f"Consensus:  ${s['consensus_price']:,.2f}" if s['consensus_price'] else "Consensus:  (waiting for data)",
    ]
    for source, info in s["sources"].items():
        stale = " [STALE]" if info["stale"] else ""
        lines.append(f"  {source:12s}  ${info['price']:,.2f}  ({info['age_seconds']:.1f}s ago){stale}")

    if s["latest_divergence"]:
        d = s["latest_divergence"]
        lines += [
            f"\nLatest Divergence:",
            f"  {d['source_a']} vs {d['source_b']}: {d['divergence_pct']:.4f}%",
            f"  direction={d['direction']}  at {d['timestamp']}",
        ]

    lines.append(f"\nTotal divergence signals: {s['divergence_count']}")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _run_backtest(args: Dict[str, Any]) -> List[types.TextContent]:
    strategy_name = args.get("strategy", "orderbook_spread")
    days = int(args.get("days", 30))
    budget = float(args.get("budget_per_slot", 20.0))
    capital = float(args.get("initial_capital", 500.0))
    symbol = args.get("symbol", "BTCUSDT")
    show_slots = int(args.get("show_slots", 10))

    from ..engine.backtester import Backtester
    from ..engine.strategies import get_strategy_class

    strategy_class = get_strategy_class(strategy_name)

    bt = Backtester(
        strategy_class=strategy_class,
        budget_per_slot=budget,
        initial_capital=capital,
    )

    try:
        results = await bt.run(symbol=symbol, interval="5m", days=days)
    except RuntimeError as exc:
        return [types.TextContent(type="text", text=f"Backtest failed: {exc}")]

    s = results.summary()
    pnl_sign = "+" if s["total_pnl"] >= 0 else ""
    cap_sign = "+" if s["total_pnl"] >= 0 else ""

    lines = [
        f"Backtest Results — {strategy_name}",
        f"{'='*50}",
        f"Period:         {s['period']}",
        f"Symbol:         {symbol} 5m candles",
        f"Total slots:    {s['total_slots']} ({s['traded_slots']} traded)",
        f"",
        f"── Performance ──────────────────────────────",
        f"Initial capital: ${s['initial_capital']:.2f}",
        f"Final capital:   ${s['final_capital']:.2f}  ({cap_sign}{s['total_pnl_pct']:.2f}%)",
        f"Total PnL:       ${pnl_sign}{s['total_pnl']:.4f}",
        f"Avg PnL/slot:    ${s['avg_pnl']:+.4f}",
        f"Best slot:       ${s['best_slot']:+.4f}",
        f"Worst slot:      ${s['worst_slot']:+.4f}",
        f"",
        f"── Risk ─────────────────────────────────────",
        f"Win rate:        {s['win_rate']:.1f}%",
        f"Sharpe ratio:    {s['sharpe_ratio']:.3f}",
        f"Max drawdown:    {s['max_drawdown_pct']:.2f}%",
        f"Profit factor:   {s['profit_factor']:.3f}",
        f"Total fees:      ${s['total_fees']:.4f}",
    ]

    breakdown = results.slot_breakdown(show_slots)
    if breakdown:
        lines += [f"", f"── Last {len(breakdown)} slots ───────────────────────"]
        for slot in breakdown:
            marker = "✓" if slot["correct"] else "✗"
            pnl_str = f"${slot['pnl']:+.4f}"
            lines.append(
                f"  {marker} {slot['open_time'][:16]}  "
                f"BTC {slot['direction']:4s}  "
                f"outcome={slot['outcome']}  "
                f"PnL={pnl_str:>10}  "
                f"trades={slot['trades']}"
            )

    # Verdict
    lines += [""]
    if s["sharpe_ratio"] > 1.0 and s["win_rate"] > 52 and s["total_pnl"] > 0:
        lines.append("Verdict: PROMISING — consider running in paper wallet mode.")
    elif s["total_pnl"] > 0:
        lines.append("Verdict: MARGINALLY PROFITABLE — needs more data or refinement.")
    else:
        lines.append("Verdict: NOT PROFITABLE in this period — refine strategy before live trading.")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ── Client access ─────────────────────────────────────────────────────────

_client_ref = None

def set_client(client) -> None:
    global _client_ref
    _client_ref = client

def _get_client():
    return _client_ref
