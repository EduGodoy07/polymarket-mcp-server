"""
Run comparative backtest across all registered strategies.

Usage:
    venv/bin/python run_backtest_all.py

Fetches 30 days of BTCUSDT 5m candles (once), then tests every strategy
and prints a ranked leaderboard.
"""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.WARNING,  # suppress per-candle noise; show only summary
    format="%(levelname)s %(name)s: %(message)s",
)
# Show our own progress lines
logging.getLogger("__main__").setLevel(logging.INFO)
log = logging.getLogger(__name__)

sys.path.insert(0, "src")

from polymarket_mcp.engine.backtester import Backtester, fetch_candles
from polymarket_mcp.engine.strategies import _REGISTRY

SYMBOL    = "BTCUSDT"
INTERVAL  = "5m"
DAYS      = 30
BUDGET    = 20.0
CAPITAL   = 500.0

# Strategies to skip (need live data only)
SKIP = {"divergence_scalp"}


async def main():
    print(f"\n{'='*65}")
    print(f"  Backtest — {SYMBOL} {INTERVAL}  |  {DAYS} days  |  ${CAPITAL} capital")
    print(f"{'='*65}\n")

    print("Fetching candles from Binance...", flush=True)
    candles = fetch_candles(symbol=SYMBOL, interval=INTERVAL, days=DAYS)
    print(f"  {len(candles)} candles loaded ({candles[0].open_time.date()} → {candles[-1].close_time.date()})\n")

    rows = []
    for name, cls in _REGISTRY.items():
        if name in SKIP:
            print(f"  [{name}] SKIP (live data only)")
            continue

        print(f"  [{name}] running...", end="", flush=True)
        bt = Backtester(
            strategy_class=cls,
            budget_per_slot=BUDGET,
            initial_capital=CAPITAL,
        )
        results = await bt.run(candles=candles, symbol=SYMBOL, interval=INTERVAL)
        s = results.summary()
        rows.append(s)
        print(
            f"  slots={s['traded_slots']:>5}  "
            f"WR={s['win_rate']*100:>5.1f}%  "
            f"PnL={s['total_pnl']:>+10.2f}  "
            f"DD={s['max_drawdown_pct']*100:>5.1f}%  "
            f"PF={s['profit_factor']:>5.2f}"
        )

    # Sort by total_pnl descending
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    print(f"\n{'='*65}")
    print(f"  LEADERBOARD")
    print(f"{'='*65}")
    print(f"  {'#':<3} {'Strategy':<18} {'Slots':>6} {'Win%':>6} {'PnL':>12} {'MaxDD':>7} {'PFactor':>8}")
    print(f"  {'-'*62}")
    for i, r in enumerate(rows):
        medal = medals[i] if i < len(medals) else f" {i+1}"
        print(
            f"  {medal} {r['strategy']:<18} "
            f"{r['traded_slots']:>6}  "
            f"{r['win_rate']*100:>5.1f}%  "
            f"${r['total_pnl']:>+10.2f}  "
            f"{r['max_drawdown_pct']*100:>5.1f}%  "
            f"{r['profit_factor']:>7.2f}"
        )
    print(f"{'='*65}\n")


if __name__ == "__main__":
    asyncio.run(main())
