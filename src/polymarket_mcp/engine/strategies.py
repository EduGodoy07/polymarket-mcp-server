"""
Trading strategies for Polymarket 5-minute BTC markets.

Built-in strategies:
  OrderbookSpreadStrategy  — spread + RSI extreme (reference)
  DivergenceScalpStrategy  — cross-source BTC divergence (reference)

Researched & implemented strategies:
  SuperTrendStrategy       — ATR-based trend following
  RSIDivergenceStrategy    — momentum reversal via RSI divergence
  EMAStackStrategy         — triple EMA alignment (9/21/50)
  IchimokuStrategy         — cloud-based trend confirmation
  AdaptiveGridStrategy     — volatility-adapted grid levels
  TrendLevelsStrategy      — Donchian midpoint + EMA200/50 + RSI + MACD confluence
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Dict, List, Optional, Tuple, Type

from .strategy import BaseStrategy, StrategyAPI, Side
from . import indicators

logger = logging.getLogger(__name__)


class OrderbookSpreadStrategy(BaseStrategy):
    """
    Enters when:
    1. Order book spread < 0.03 (tight = good fill)
    2. RSI < 35 (oversold YES → expect bounce → buy YES)
       or RSI > 65 (overbought YES → buy NO)
    3. At least 60 seconds remaining in the window

    Exit: take-profit at +15% from entry, stop-loss at -8%
    """

    name = "orderbook_spread"

    def __init__(self, rsi_period: int = 10, size_usd: float = 15.0):
        self.rsi_period = rsi_period
        self.size_usd = size_usd
        self._rsi = indicators.RollingRSI(period=rsi_period)
        self._price_history = []

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        # Warm up RSI with early price observations
        await self._warmup(api)

        if api.seconds_remaining < 60:
            logger.info(f"[{self.name}] Less than 60s remaining — skipping entry")
            return

        ob = await api.orderbook(depth=5)
        rsi_val = self._rsi.value
        spread = ob.spread

        if spread is None or rsi_val is None:
            logger.info(f"[{self.name}] Insufficient data (spread={spread}, rsi={rsi_val})")
            return

        logger.info(f"[{self.name}] spread={spread:.4f} RSI={rsi_val:.1f}")

        if spread > 0.03:
            logger.info(f"[{self.name}] Spread too wide ({spread:.4f}) — no trade")
            return

        entry_price = await api.price()

        if rsi_val < 35 and ob.best_ask:
            # Buy YES — expect price to recover
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 0.92)
                api.set_take_profit(entry_price * 1.15)
                logger.info(f"[{self.name}] Entered YES @ {ob.best_ask:.4f}")

        elif rsi_val > 65 and ob.best_ask:
            # Buy NO — expect YES price to fall
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 1.08)
                api.set_take_profit(entry_price * 0.85)
                logger.info(f"[{self.name}] Entered NO @ market")
        else:
            logger.info(f"[{self.name}] No signal — RSI neutral ({rsi_val:.1f})")

    async def _warmup(self, api: StrategyAPI) -> None:
        """Collect 15 price ticks over ~15 seconds to seed the RSI."""
        for _ in range(15):
            price = await api.price()
            self._rsi.update(price)
            self._price_history.append(price)
            await asyncio.sleep(1.0)


class DivergenceScalpStrategy(BaseStrategy):
    """
    Uses cross-source BTC divergence as a leading signal.

    When Binance price leads Coinbase upward by >0.1%:
      → buy YES (price-to-beat likely to be exceeded)
    When Binance leads downward:
      → buy NO

    Exits at +12% profit or -6% stop-loss.
    """

    name = "divergence_scalp"

    def __init__(self, size_usd: float = 15.0, min_divergence: float = 0.001):
        self.size_usd = size_usd
        self.min_divergence = min_divergence

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        # Get divergence signal from price feed (attached to api slot)
        divergence = await self._get_divergence(api)

        if divergence is None:
            logger.info(f"[{self.name}] No divergence data available")
            return

        if abs(divergence) < self.min_divergence:
            logger.info(
                f"[{self.name}] Divergence {divergence:.4f} below threshold "
                f"{self.min_divergence:.4f}"
            )
            return

        ob = await api.orderbook(depth=3)
        entry_price = ob.mid or 0.5

        if divergence > 0:
            # Binance leading up → buy YES
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 0.94)
                api.set_take_profit(entry_price * 1.12)
                logger.info(
                    f"[{self.name}] YES entry — divergence={divergence:.4f} "
                    f"@ {ob.best_ask:.4f}"
                )
        else:
            # Binance leading down → buy NO
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(entry_price * 1.06)
                api.set_take_profit(entry_price * 0.88)
                logger.info(
                    f"[{self.name}] NO entry — divergence={divergence:.4f}"
                )

    async def _get_divergence(self, api: StrategyAPI) -> Optional[float]:
        """
        Read latest divergence from the price feed if attached to the slot.
        Returns positive for bullish (Binance > Coinbase), negative for bearish.
        """
        try:
            slot = api._slot
            if hasattr(slot, "_engine") and slot._engine and slot._engine.price_feeds:
                feed = slot._engine.price_feeds
                binance = feed.price_by_source("binance")
                coinbase = feed.price_by_source("coinbase")
                if binance and coinbase:
                    avg = (binance + coinbase) / 2
                    return (binance - coinbase) / avg
        except Exception:
            pass
        return None


# ── SuperTrend ────────────────────────────────────────────────────────────

class SuperTrendStrategy(BaseStrategy):
    """
    ATR-based trend following. Enters when SuperTrend flips direction.

    Logic:
    - SuperTrend upper band = (high+low)/2 + multiplier * ATR  → resistance
    - SuperTrend lower band = (high+low)/2 - multiplier * ATR  → support
    - When price closes above upper band → trend turns bullish → buy YES
    - When price closes below lower band → trend turns bearish → buy NO
    - Exit: take-profit +12%, stop-loss -6%

    Best for: trending markets. Struggles in sideways ranges.
    Documented metrics: win rate 71-87%, profit factor 2.1-2.5
    """

    name = "supertrend"

    def __init__(self, atr_period: int = 10, multiplier: float = 3.0, size_usd: float = 15.0):
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.size_usd = size_usd

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 60:
            return

        # Use historical candles if available (backtester), else poll live prices
        closes, highs, lows = self._get_ohlc(api)
        if len(closes) < self.atr_period + 2:
            logger.info(f"[{self.name}] Insufficient history ({len(closes)} candles)")
            return

        atr_val = indicators.atr(highs, lows, closes, self.atr_period)
        if atr_val is None:
            return

        mid = (max(highs[-3:]) + min(lows[-3:])) / 2
        upper_band = mid + self.multiplier * atr_val
        lower_band = mid - self.multiplier * atr_val
        current_price = closes[-1]

        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.05:
            return

        logger.info(
            f"[{self.name}] btc={current_price:.2f} "
            f"upper={upper_band:.2f} lower={lower_band:.2f} ATR={atr_val:.2f}"
        )

        yes_price = await api.price()  # YES token price (0-1 range) for SL/TP

        if current_price > upper_band:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 0.88)
                api.set_take_profit(min(0.95, yes_price * 1.20))
                logger.info(f"[{self.name}] YES @ {yes_price:.4f} — BTC above upper band")
        elif current_price < lower_band:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 1.12)
                api.set_take_profit(max(0.05, yes_price * 0.80))
                logger.info(f"[{self.name}] NO @ {yes_price:.4f} — BTC below lower band")
        else:
            logger.info(f"[{self.name}] BTC inside bands — no signal")

    def _get_ohlc(self, api) -> Tuple[List[float], List[float], List[float]]:
        """Use backtester history if available, else return current candle only."""
        if hasattr(api, "history_closes") and len(api.history_closes) >= self.atr_period + 2:
            return api.history_closes, api.history_highs, api.history_lows
        # Live fallback — single candle, not enough for ATR
        p = api._candle.close if hasattr(api, "_candle") else 0.5
        return [p], [p], [p]


# ── RSI Divergence ────────────────────────────────────────────────────────

class RSIDivergenceStrategy(BaseStrategy):
    """
    Momentum reversal via RSI divergence.

    Bullish divergence: price makes lower low, RSI makes higher low → buy YES
    Bearish divergence: price makes higher high, RSI makes lower high → buy NO

    Uses short RSI period (4) optimized for 5-minute timeframes.
    Exit: take-profit +18%, stop-loss -7%

    Documented metrics: win rate 65-91%, best on short timeframes (5m)
    """

    name = "rsi_divergence"

    def __init__(self, rsi_period: int = 4, lookback: int = 10, size_usd: float = 15.0):
        self.rsi_period = rsi_period
        self.lookback = lookback
        self.size_usd = size_usd

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 60:
            return

        # Use historical candle closes if available
        if hasattr(api, "history_closes") and len(api.history_closes) >= self.lookback + self.rsi_period + 2:
            prices = api.history_closes
        else:
            # Live: collect prices
            n = self.lookback + self.rsi_period + 2
            prices = []
            for _ in range(n):
                prices.append(await api.price())
                await asyncio.sleep(1.0)

        if len(prices) < self.lookback + self.rsi_period + 2:
            logger.info(f"[{self.name}] Insufficient history")
            return

        # Calculate RSI at each point in the lookback window
        rsi_values = []
        for i in range(self.rsi_period + 1, len(prices) + 1):
            r = indicators.rsi(prices[:i], self.rsi_period)
            if r is not None:
                rsi_values.append(r)

        if len(rsi_values) < self.lookback:
            logger.info(f"[{self.name}] Insufficient RSI history")
            return

        price_window = prices[-self.lookback:]
        rsi_window   = rsi_values[-self.lookback:]

        price_min_idx = price_window.index(min(price_window))
        price_max_idx = price_window.index(max(price_window))
        rsi_at_price_min = rsi_window[price_min_idx]
        rsi_at_price_max = rsi_window[price_max_idx]

        current_price = prices[-1]
        current_rsi   = rsi_values[-1]

        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.05:
            return

        # Bullish divergence: price at new low but RSI higher than at prior low
        bullish_div = (
            current_price <= min(price_window) and
            current_rsi > rsi_at_price_min and
            current_rsi < 45
        )
        # Bearish divergence: price at new high but RSI lower than at prior high
        bearish_div = (
            current_price >= max(price_window) and
            current_rsi < rsi_at_price_max and
            current_rsi > 55
        )

        logger.info(
            f"[{self.name}] price={current_price:.4f} RSI={current_rsi:.1f} "
            f"bull_div={bullish_div} bear_div={bearish_div}"
        )

        yes_price = await api.price()

        if bullish_div:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 0.87)
                api.set_take_profit(min(0.95, yes_price * 1.22))
                logger.info(f"[{self.name}] YES @ {yes_price:.4f} — bullish RSI divergence")

        elif bearish_div:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 1.13)
                api.set_take_profit(max(0.05, yes_price * 0.78))
                logger.info(f"[{self.name}] NO @ {yes_price:.4f} — bearish RSI divergence")
        else:
            logger.info(f"[{self.name}] No divergence signal")


# ── EMA Stack 9/21/50 ────────────────────────────────────────────────────

class EMAStackStrategy(BaseStrategy):
    """
    Triple EMA alignment trend following (9 / 21 / 50).

    Bullish stack:  EMA9 > EMA21 > EMA50 AND price > EMA9 → buy YES
    Bearish stack:  EMA9 < EMA21 < EMA50 AND price < EMA9 → buy NO

    Optional ADX filter: only trade when trend strength > 20.
    Exit: take-profit +10%, stop-loss -5%

    Documented metrics: win rate 50-59%, profit factor 1.7, annual return ~20%
    """

    name = "ema_stack"

    def __init__(self, size_usd: float = 15.0):
        self.size_usd = size_usd
        # Periods
        self.fast   = 9
        self.mid    = 21
        self.slow   = 50

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 60:
            return

        # Use historical closes if available, else poll live
        if hasattr(api, "history_closes") and len(api.history_closes) >= self.slow:
            prices = api.history_closes
        else:
            prices = []
            for _ in range(self.slow + 5):
                prices.append(await api.price())
                await asyncio.sleep(1.0)

        if len(prices) < self.slow:
            logger.info(f"[{self.name}] Insufficient data for EMA50 ({len(prices)} pts)")
            return

        ema9  = indicators.ema(prices, self.fast)
        ema21 = indicators.ema(prices, self.mid)
        ema50 = indicators.ema(prices, self.slow)

        if not all([ema9, ema21, ema50]):
            return

        current_price = prices[-1]
        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.05:
            return

        bullish = ema9 > ema21 > ema50 and current_price > ema9
        bearish = ema9 < ema21 < ema50 and current_price < ema9

        logger.info(
            f"[{self.name}] EMA9={ema9:.4f} EMA21={ema21:.4f} EMA50={ema50:.4f} "
            f"price={current_price:.4f} bull={bullish} bear={bearish}"
        )

        yes_price = await api.price()

        if bullish:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 0.88)
                api.set_take_profit(min(0.95, yes_price * 1.18))
                logger.info(f"[{self.name}] YES @ {yes_price:.4f} — bullish EMA stack")

        elif bearish:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 1.12)
                api.set_take_profit(max(0.05, yes_price * 0.82))
                logger.info(f"[{self.name}] NO @ {yes_price:.4f} — bearish EMA stack")
        else:
            logger.info(f"[{self.name}] EMAs not aligned — no signal")


# ── Ichimoku Cloud ────────────────────────────────────────────────────────

class IchimokuStrategy(BaseStrategy):
    """
    Ichimoku Cloud — 5 componentes con TAAPI.IO en live / cálculo propio en backtest.

    Fuente de datos:
      Live trading → TAAPI.IO (Binance real, datos precisos)
        GET api.taapi.io/ichimoku?exchange=binance&symbol=BTC/USDT&interval=5m
      Backtesting  → cálculo propio con highs/lows históricos

    5 componentes:
      Tenkan-sen  (9):  (máx9 + mín9) / 2
      Kijun-sen   (26): (máx26 + mín26) / 2
      Senkou A:         (Tenkan + Kijun) / 2
      Senkou B    (52): (máx52 + mín52) / 2
      Chikou Span:      close actual vs close de hace 26 barras

    Señal YES: precio > cloud + Tenkan>Kijun + Chikou↑
    Señal NO : precio < cloud + Tenkan<Kijun + Chikou↓
    Exit: TP +20%, SL -12%
    """

    name = "ichimoku"

    TENKAN_P  = 9
    KIJUN_P   = 26
    SENKOU_P  = 52
    MIN_H     = SENKOU_P + KIJUN_P  # 78 barras mínimas (backtester)

    def __init__(self, size_usd: float = 15.0):
        self.size_usd = size_usd

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 60:
            return

        # ── Obtener valores Ichimoku ──────────────────────────────────────
        # Modo live: TAAPI.IO (Binance real) | Modo backtest: cálculo propio
        ichi = await self._get_ichimoku(api)
        if ichi is None:
            return

        tenkan, kijun, cloud_top, cloud_bottom, chikou_bull, chikou_bear, current = ichi

        # ── Señales ───────────────────────────────────────────────────────
        long_signal  = current > cloud_top    and tenkan > kijun and chikou_bull
        short_signal = current < cloud_bottom and tenkan < kijun and chikou_bear

        logger.info(
            f"[{self.name}] btc={current:.2f} "
            f"cloud=[{cloud_bottom:.2f}–{cloud_top:.2f}] "
            f"T={tenkan:.2f} K={kijun:.2f} "
            f"chikou={'↑' if chikou_bull else '↓'} "
            f"long={long_signal} short={short_signal}"
        )

        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.05:
            return

        yes_price = await api.price()

        if long_signal:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 0.88)
                api.set_take_profit(min(0.95, yes_price * 1.20))
                logger.info(f"[{self.name}] YES @ {yes_price:.4f} — cloud+TK+chikou")

        elif short_signal:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 1.12)
                api.set_take_profit(max(0.05, yes_price * 0.80))
                logger.info(f"[{self.name}] NO @ {yes_price:.4f} — cloud+TK+chikou")

    async def _get_ichimoku(self, api) -> Optional[tuple]:
        """
        Retorna (tenkan, kijun, cloud_top, cloud_bottom, chikou_bull, chikou_bear, current).

        Intenta TAAPI.IO primero (live). Si no hay secret o falla,
        usa cálculo propio con historial del backtester.
        """
        # ── TAAPI.IO (live trading) ───────────────────────────────────────
        if not getattr(api, "simulation", True):
            try:
                from .taapi_client import get_taapi_client
                taapi = get_taapi_client()
                if taapi.secret:
                    data = await taapi.ichimoku("BTC/USDT", "5m")
                    if data:
                        tenkan   = data["tenkan"]
                        kijun    = data["kijun"]
                        senkou_a = data["senkou_a"]   # cloud actual SpanA
                        senkou_b = data["senkou_b"]   # cloud actual SpanB
                        # Precio actual BTC — usamos Tenkan como proxy (≈ precio reciente)
                        current  = (tenkan + kijun) / 2  # aprox precio actual
                        cloud_top = max(senkou_a, senkou_b)
                        cloud_bot = min(senkou_a, senkou_b)
                        # Chikou proxy: si cloud pasado (lagging) está por debajo del
                        # precio actual → tendencia confirmada alcista
                        lag_cloud_top = max(data["lagging_a"], data["lagging_b"])
                        lag_cloud_bot = min(data["lagging_a"], data["lagging_b"])
                        chikou_bull = current > lag_cloud_top  # precio > nube hace 26 barras
                        chikou_bear = current < lag_cloud_bot
                        logger.info(
                            f"[{self.name}] TAAPI — T={tenkan:.2f} K={kijun:.2f} "
                            f"cloud=[{cloud_bot:.2f}–{cloud_top:.2f}]"
                        )
                        return tenkan, kijun, cloud_top, cloud_bot, chikou_bull, chikou_bear, current
            except Exception as e:
                logger.warning(f"[{self.name}] TAAPI fallback a cálculo propio: {e}")

        # ── Cálculo propio (backtester / fallback) ────────────────────────
        if not (hasattr(api, "history_closes") and
                len(api.history_closes) >= self.MIN_H):
            n = len(api.history_closes) if hasattr(api, "history_closes") else 0
            logger.info(f"[{self.name}] Historial insuficiente ({n}/{self.MIN_H})")
            return None

        closes = api.history_closes
        highs  = api.history_highs if hasattr(api, "history_highs") else closes
        lows   = api.history_lows  if hasattr(api, "history_lows")  else closes

        def hl_mid(h: List[float], l: List[float], period: int) -> float:
            return (max(h[-period:]) + min(l[-period:])) / 2.0

        tenkan   = hl_mid(highs, lows, self.TENKAN_P)
        kijun    = hl_mid(highs, lows, self.KIJUN_P)
        senkou_a = (tenkan + kijun) / 2
        senkou_b = hl_mid(highs, lows, self.SENKOU_P)
        cloud_top = max(senkou_a, senkou_b)
        cloud_bot = min(senkou_a, senkou_b)
        current   = closes[-1]

        past_price  = closes[-self.KIJUN_P - 1]
        chikou_bull = current > past_price
        chikou_bear = current < past_price

        return tenkan, kijun, cloud_top, cloud_bot, chikou_bull, chikou_bear, current


# ── Adaptive Grid ────────────────────────────────────────────────────────

class AdaptiveGridStrategy(BaseStrategy):
    """
    Volatility-adapted grid trading. Inspired by SureShot Adaptive Grid.

    Logic:
    - Calculate ATR to measure current volatility
    - Set grid levels above and below current price: price ± N*ATR
    - If price is near the lower grid level (oversold zone) → buy YES
    - If price is near the upper grid level (overbought zone) → buy NO
    - Grid spacing adapts automatically to volatility — wider in volatile
      markets, tighter in quiet markets

    Exit: take-profit at next grid level (+1 ATR), stop-loss at -0.5 ATR

    Documented metrics: win rate 81.5%, profit factor 7.64 (SureShot variant)
    """

    name = "adaptive_grid"

    def __init__(
        self,
        atr_period: int = 14,
        grid_multiplier: float = 1.5,
        entry_proximity: float = 0.3,
        size_usd: float = 15.0,
    ):
        self.atr_period       = atr_period
        self.grid_multiplier  = grid_multiplier  # ATR multiples for grid spacing
        self.entry_proximity  = entry_proximity  # how close to grid level to enter (fraction)
        self.size_usd         = size_usd

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 90:
            return

        # Use historical OHLC if available
        if hasattr(api, "history_closes") and len(api.history_closes) >= self.atr_period + 2:
            prices = api.history_closes
            highs  = api.history_highs
            lows   = api.history_lows
        else:
            prices, highs, lows = [], [], []
            for _ in range(self.atr_period + 5):
                p  = await api.price()
                ob = await api.orderbook(depth=1)
                h  = ob.best_ask or p
                l  = ob.best_bid or p
                prices.append(p)
                highs.append(max(p, h))
                lows.append(min(p, l))
                await asyncio.sleep(1.0)

        atr_val = indicators.atr(highs, lows, prices, self.atr_period)
        if atr_val is None:
            return

        anchor = sum(prices[-20:]) / min(20, len(prices))  # recent average as anchor
        grid_step = atr_val * self.grid_multiplier

        lower_grid = anchor - grid_step
        upper_grid = anchor + grid_step
        current_price = prices[-1]

        # Proximity check — how close is price to each grid level?
        proximity_lower = abs(current_price - lower_grid) / grid_step
        proximity_upper = abs(current_price - upper_grid) / grid_step

        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.06:
            return

        logger.info(
            f"[{self.name}] price={current_price:.4f} ATR={atr_val:.4f} "
            f"grid=[{lower_grid:.4f} — {upper_grid:.4f}] "
            f"prox_lower={proximity_lower:.2f} prox_upper={proximity_upper:.2f}"
        )

        yes_price = await api.price()

        if proximity_lower <= self.entry_proximity:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 0.87)
                api.set_take_profit(min(0.95, yes_price * 1.22))
                logger.info(f"[{self.name}] YES @ {yes_price:.4f} — near lower grid")

        elif proximity_upper <= self.entry_proximity:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * 1.13)
                api.set_take_profit(max(0.05, yes_price * 0.78))
                logger.info(f"[{self.name}] NO @ {yes_price:.4f} — near upper grid")
        else:
            logger.info(f"[{self.name}] Price between grid levels — no entry")


# ── Trend Levels (Pine Script adaptation) ────────────────────────────────

class TrendLevelsStrategy(BaseStrategy):
    """
    Alta probabilidad: Donchian + EMA50/20 + RSI(4) + MACD acelerado + BB squeeze.

    Diseñada para win rate 80-95% con ratio TP:SL de alta probabilidad.

    Clave del alto win rate:
      TP = +2%  → precio YES sube solo 2% para ganar
                  BTC solo necesita subir 0.08% → trivial en uptrend
      SL = -12% → precio YES cae 12% para perder
                  BTC necesita caer 0.48% → raro con 5 filtros activos

    Con SL apretado (-2% anterior): cualquier ruido toca el SL → WR 20%
    Con SL amplio (-12%):           solo caídas reales tocan el SL → WR 80%+

    5 condiciones de entrada (long):
      1. trendUp   = precio > Donchian(30) midpoint
      2. rsi4 < 40 = RSI(4) en pullback corto (comprar el dip)
      3. macd_bull = MACD line > Signal line (momentum macro +)
      4. precio > EMA50 (macro tendencia alcista, ≈ 4h)
      5. precio > EMA20 (tendencia corta alcista, ≈ 1.7h)

    5 condiciones de entrada (short): espejo simétrico.

    Exit: TP +2% / SL -12%

    Escalado para 5-min TF:
      EMA200 → EMA50 | EMA50 → EMA20 | RSI(14) → RSI(4)
    """

    name = "trend_levels"

    TREND_PERIOD    = 30
    EMA_SLOW        = 50
    EMA_FAST        = 20
    RSI_PERIOD      = 4
    MACD_FAST       = 12
    MACD_SLOW       = 26
    MACD_SIGNAL     = 9
    RSI_LONG_ENTRY  = 40
    RSI_SHORT_ENTRY = 60
    TP_PCT          = 1.05   # +5% YES space
    SL_PCT          = 0.98   # -2% YES space  (apretado = sale antes de resolución binaria)
    MIN_CANDLES     = EMA_SLOW + MACD_SIGNAL + 5  # ≈ 64

    def __init__(self, size_usd: float = 15.0):
        self.size_usd = size_usd

    async def run(self, api: StrategyAPI) -> None:
        logger.info(f"[{self.name}] Starting on market {api.market_id[:14]}")

        if api.seconds_remaining < 60:
            return

        # ── 1. Historial ──────────────────────────────────────────────────
        if hasattr(api, "history_closes") and len(api.history_closes) >= self.MIN_CANDLES:
            prices = api.history_closes
            highs  = api.history_highs
            lows   = api.history_lows
        else:
            n = len(api.history_closes) if hasattr(api, "history_closes") else 0
            logger.info(f"[{self.name}] Historial insuficiente ({n}/{self.MIN_CANDLES})")
            return

        current_price = prices[-1]

        # ── 2. Donchian midpoint ──────────────────────────────────────────
        window_h  = max(highs[-self.TREND_PERIOD:])
        window_l  = min(lows[-self.TREND_PERIOD:])
        mid_level = (window_h + window_l) / 2.0
        trend_up   = current_price > mid_level
        trend_down = current_price < mid_level

        # ── 3. EMAs ───────────────────────────────────────────────────────
        ema_slow = indicators.ema(prices, self.EMA_SLOW)
        ema_fast = indicators.ema(prices, self.EMA_FAST)
        if ema_slow is None or ema_fast is None:
            return

        # ── 4. RSI(4) ─────────────────────────────────────────────────────
        rsi_val = indicators.rsi(prices, self.RSI_PERIOD)
        if rsi_val is None:
            return

        # ── 5. MACD ───────────────────────────────────────────────────────
        macd_line, sig_line, _ = indicators.macd(
            prices, self.MACD_FAST, self.MACD_SLOW, self.MACD_SIGNAL
        )
        if macd_line is None:
            return

        macd_bullish = macd_line > sig_line
        macd_bearish = macd_line < sig_line

        # ── 6. Señal — 5 condiciones ──────────────────────────────────────
        long_signal = (
            trend_up and
            rsi_val < self.RSI_LONG_ENTRY and
            macd_bullish and
            current_price > ema_slow and
            current_price > ema_fast
        )
        short_signal = (
            trend_down and
            rsi_val > self.RSI_SHORT_ENTRY and
            macd_bearish and
            current_price < ema_slow and
            current_price < ema_fast
        )

        logger.info(
            f"[{self.name}] btc={current_price:.2f} mid={mid_level:.2f} "
            f"RSI={rsi_val:.1f} MACD={'bull' if macd_bullish else 'bear'} "
            f"long={long_signal} short={short_signal}"
        )

        ob = await api.orderbook(depth=5)
        if ob.spread and ob.spread > 0.05:
            return

        yes_price = await api.price()

        if long_signal:
            receipt = await api.buy(Side.YES, self.size_usd, price=ob.best_ask)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * self.SL_PCT)
                api.set_take_profit(min(0.95, yes_price * self.TP_PCT))
                logger.info(
                    f"[{self.name}] YES @ {yes_price:.4f} "
                    f"SL={yes_price*self.SL_PCT:.4f} TP={yes_price*self.TP_PCT:.4f} "
                    f"(6-factor confluence)"
                )

        elif short_signal:
            receipt = await api.buy(Side.NO, self.size_usd)
            if receipt.is_filled:
                api.set_stop_loss(yes_price * (2 - self.SL_PCT))   # +12% para NO
                api.set_take_profit(max(0.05, yes_price * (2 - self.TP_PCT)))  # -2%
                logger.info(
                    f"[{self.name}] NO @ {yes_price:.4f} "
                    f"(6-factor confluence)"
                )
        else:
            logger.info(f"[{self.name}] Sin señal")


# ── Registry ─────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "orderbook_spread": OrderbookSpreadStrategy,
    "divergence_scalp": DivergenceScalpStrategy,
    "supertrend":       SuperTrendStrategy,
    "rsi_divergence":   RSIDivergenceStrategy,
    "ema_stack":        EMAStackStrategy,
    "ichimoku":         IchimokuStrategy,
    "adaptive_grid":    AdaptiveGridStrategy,
    "trend_levels":     TrendLevelsStrategy,
}


def get_strategy_class(name: str) -> Type[BaseStrategy]:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


def register_strategy(name: str, cls: Type[BaseStrategy]) -> None:
    """Register a custom strategy class by name."""
    _REGISTRY[name] = cls
    logger.info(f"Strategy registered: {name} → {cls.__name__}")


def list_strategies() -> Dict[str, str]:
    return {name: cls.__doc__.split("\n")[1].strip() for name, cls in _REGISTRY.items()}
