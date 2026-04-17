"""
TAAPI.IO client — indicadores técnicos reales desde Binance.

Plan Free: 5,000 llamadas/día
Documentación: https://taapi.io/indicators/

Indicadores usados:
  - ichimoku  → Tenkan, Kijun, SenkouA, SenkouB, Chikou
  - rsi       → RSI value
  - ema       → EMA value
  - macd      → MACD line, Signal, Histogram

Rate limiting automático: máx 1 req/s en free plan.
Cache de 4 minutos: en 5-min slots no necesitas más de 1 llamada por slot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TAAPI_BASE = "https://api.taapi.io"
_CACHE_TTL = 240   # segundos — renueva cada 4 min (cabe en slot de 5 min)
_MIN_INTERVAL = 1.2  # segundos entre requests (free plan ~1 req/s)


class TaapiClient:
    """
    Cliente async para TAAPI.IO.

    Uso:
        client = TaapiClient(secret=os.getenv("TAAPI_SECRET"))
        ichidata = await client.ichimoku("BTC/USDT", "5m")
        tenkan = ichidata["valueTenkanSen"]
    """

    def __init__(self, secret: Optional[str] = None):
        self.secret = secret or os.getenv("TAAPI_SECRET", "")
        self._cache: Dict[str, tuple] = {}   # key → (timestamp, data)
        self._last_req: float = 0.0

    # ── Rate limiting ─────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        """Garantiza mínimo _MIN_INTERVAL entre requests."""
        elapsed = time.monotonic() - self._last_req
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        self._last_req = time.monotonic()

    # ── Cache ─────────────────────────────────────────────────────────────

    def _cache_get(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.monotonic() - ts < _CACHE_TTL:
                return data
        return None

    def _cache_set(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.monotonic(), data)

    # ── HTTP helper ───────────────────────────────────────────────────────

    async def _get(self, endpoint: str, params: Dict) -> Dict:
        import httpx
        params["secret"] = self.secret
        url = f"{TAAPI_BASE}/{endpoint}"
        await self._throttle()
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    # ── Indicadores ───────────────────────────────────────────────────────

    async def ichimoku(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "5m",
        exchange: str = "binance",
    ) -> Optional[Dict[str, float]]:
        """
        Ichimoku Cloud completo.

        Returns dict con:
          valueTenkanSen     — Tenkan-sen (línea de conversión, 9 periodos)
          valueKijunSen      — Kijun-sen  (línea base, 26 periodos)
          valueSenkouSpanA   — Senkou Span A (borde nube 1)
          valueSenkouSpanB   — Senkou Span B (borde nube 2, 52 periodos)
          valueChikouSpan    — Chikou Span (lagging span, 26 barras atrás)

        Ejemplo de señal:
          price > max(SpanA, SpanB)  AND  Tenkan > Kijun  → bullish
          Chikou > price_26_ago                           → confirmado
        """
        key = f"ichimoku:{exchange}:{symbol}:{interval}"
        cached = self._cache_get(key)
        if cached:
            logger.debug(f"[taapi] ichimoku cache hit — {symbol} {interval}")
            return cached

        try:
            raw = await self._get("ichimoku", {
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
            })
            # Normalizar nombres de campo al estándar interno
            # TAAPI devuelve: conversion, base, spanA/B, currentSpanA/B, laggingSpanA/B
            data = {
                "tenkan":       raw.get("conversion", 0),
                "kijun":        raw.get("base", 0),
                "senkou_a":     raw.get("currentSpanA", 0),   # cloud actual
                "senkou_b":     raw.get("currentSpanB", 0),   # cloud actual
                "senkou_a_fut": raw.get("spanA", 0),          # cloud +26 barras
                "senkou_b_fut": raw.get("spanB", 0),
                "lagging_a":    raw.get("laggingSpanA", 0),   # cloud hace 26 barras
                "lagging_b":    raw.get("laggingSpanB", 0),
            }
            self._cache_set(key, data)
            logger.info(
                f"[taapi] ichimoku {symbol} {interval} — "
                f"T={data['tenkan']:.2f} K={data['kijun']:.2f} "
                f"cloud=[{min(data['senkou_a'],data['senkou_b']):.2f}"
                f"–{max(data['senkou_a'],data['senkou_b']):.2f}]"
            )
            return data
        except Exception as e:
            logger.error(f"[taapi] ichimoku error: {e}")
            return None

    async def rsi(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "5m",
        period: int = 14,
        exchange: str = "binance",
    ) -> Optional[float]:
        """RSI value (0-100)."""
        key = f"rsi:{exchange}:{symbol}:{interval}:{period}"
        cached = self._cache_get(key)
        if cached:
            return cached.get("value")
        try:
            data = await self._get("rsi", {
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "period": period,
            })
            self._cache_set(key, data)
            return data.get("value")
        except Exception as e:
            logger.error(f"[taapi] rsi error: {e}")
            return None

    async def ema(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "5m",
        period: int = 50,
        exchange: str = "binance",
    ) -> Optional[float]:
        """EMA value."""
        key = f"ema:{exchange}:{symbol}:{interval}:{period}"
        cached = self._cache_get(key)
        if cached:
            return cached.get("value")
        try:
            data = await self._get("ema", {
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "period": period,
            })
            self._cache_set(key, data)
            return data.get("value")
        except Exception as e:
            logger.error(f"[taapi] ema error: {e}")
            return None

    async def macd(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "5m",
        exchange: str = "binance",
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Optional[Dict[str, float]]:
        """
        MACD. Returns dict:
          valueMACD        — MACD line
          valueMACDSignal  — Signal line
          valueMACDHist    — Histogram
        """
        key = f"macd:{exchange}:{symbol}:{interval}"
        cached = self._cache_get(key)
        if cached:
            return cached
        try:
            data = await self._get("macd", {
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "optInFastPeriod": fast,
                "optInSlowPeriod": slow,
                "optInSignalPeriod": signal,
            })
            self._cache_set(key, data)
            return data
        except Exception as e:
            logger.error(f"[taapi] macd error: {e}")
            return None

    async def bulk(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "5m",
        exchange: str = "binance",
        indicators: list = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Bulk request — múltiples indicadores en 1 llamada (ahorra rate limit).
        Solo disponible en planes pagos de TAAPI. En Free usa calls individuales.

        indicators: lista de dicts, ej:
          [{"indicator": "ichimoku"}, {"indicator": "rsi", "period": 14}]
        """
        key = f"bulk:{exchange}:{symbol}:{interval}"
        cached = self._cache_get(key)
        if cached:
            return cached
        try:
            import httpx
            payload = {
                "secret": self.secret,
                "construct": {
                    "exchange": exchange,
                    "symbol": symbol,
                    "interval": interval,
                    "indicators": indicators or [{"indicator": "ichimoku"}],
                }
            }
            await self._throttle()
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(f"{TAAPI_BASE}/bulk", json=payload)
                resp.raise_for_status()
                data = resp.json()
            self._cache_set(key, data)
            return data
        except Exception as e:
            logger.error(f"[taapi] bulk error: {e}")
            return None


# ── Singleton global para reusar en estrategias ────────────────────────────

_instance: Optional[TaapiClient] = None


def get_taapi_client() -> TaapiClient:
    """Devuelve el cliente singleton (inicializado con TAAPI_SECRET del env)."""
    global _instance
    if _instance is None:
        secret = os.getenv("TAAPI_SECRET", "")
        if not secret:
            logger.warning("[taapi] TAAPI_SECRET no configurado en .env")
        _instance = TaapiClient(secret=secret)
    return _instance
