"""
Live price feeds from Binance public API.
Provides real-time BTC, ETH, SOL, XRP prices for edge calculation
on crypto prediction markets.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp
from loguru import logger

from polymarket_index.config import settings

BINANCE_API = "https://api.binance.com/api/v3"
COINGECKO_API = "https://api.coingecko.com/api/v3"

COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}


@dataclass
class PriceSnapshot:
    symbol: str
    price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume_24h: float
    timestamp: float

    @property
    def base_asset(self) -> str:
        return self.symbol.replace("USDT", "").replace("USD", "")


class PriceFeed:
    """Fetches live crypto prices from Binance public API (no auth needed)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, PriceSnapshot] = {}
        self._cache_ttl = 5.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_price(self, symbol: str = "BTCUSDT") -> PriceSnapshot | None:
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached.timestamp) < self._cache_ttl:
            return cached

        snap = await self._try_binance(symbol)
        if snap is None:
            snap = await self._try_coingecko(symbol)
        if snap:
            self._cache[symbol] = snap
        return snap or cached

    async def _try_binance(self, symbol: str) -> PriceSnapshot | None:
        session = await self._get_session()
        try:
            url = f"{BINANCE_API}/ticker/24hr"
            async with session.get(url, params={"symbol": symbol}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return PriceSnapshot(
                    symbol=symbol,
                    price=float(data["lastPrice"]),
                    change_24h_pct=float(data["priceChangePercent"]),
                    high_24h=float(data["highPrice"]),
                    low_24h=float(data["lowPrice"]),
                    volume_24h=float(data["quoteVolume"]),
                    timestamp=time.time(),
                )
        except Exception:
            return None

    async def _try_coingecko(self, symbol: str) -> PriceSnapshot | None:
        cg_id = COINGECKO_IDS.get(symbol)
        if not cg_id:
            return None
        session = await self._get_session()
        try:
            url = f"{COINGECKO_API}/coins/{cg_id}"
            params = {"localization": "false", "tickers": "false",
                      "community_data": "false", "developer_data": "false"}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.debug("CoinGecko error for {}: {}", cg_id, resp.status)
                    return None
                data = await resp.json()
                md = data.get("market_data", {})
                price = md.get("current_price", {}).get("usd", 0)
                high = md.get("high_24h", {}).get("usd", price)
                low = md.get("low_24h", {}).get("usd", price)
                change = md.get("price_change_percentage_24h", 0)
                vol = md.get("total_volume", {}).get("usd", 0)
                return PriceSnapshot(
                    symbol=symbol,
                    price=float(price),
                    change_24h_pct=float(change or 0),
                    high_24h=float(high or price),
                    low_24h=float(low or price),
                    volume_24h=float(vol or 0),
                    timestamp=time.time(),
                )
        except Exception as exc:
            logger.debug("CoinGecko fetch failed for {}: {}", cg_id, exc)
            return None

    async def get_all_prices(self) -> dict[str, PriceSnapshot]:
        results: dict[str, PriceSnapshot] = {}
        tasks = [self.get_price(sym) for sym in settings.crypto_symbols]
        snapshots = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, snap in zip(settings.crypto_symbols, snapshots):
            if isinstance(snap, PriceSnapshot):
                results[sym] = snap

        return results

    async def get_btc_price(self) -> float:
        snap = await self.get_price("BTCUSDT")
        return snap.price if snap else 0.0

    async def get_price_at_percentile(
        self, symbol: str, hours: float = 24.0
    ) -> dict:
        """
        Estimate where current price sits within the 24h range.
        Returns dict with position (0=low, 1=high), direction bias, volatility.
        """
        snap = await self.get_price(symbol)
        if not snap or snap.high_24h <= snap.low_24h:
            return {"position": 0.5, "bias": "neutral", "volatility": 0}

        range_size = snap.high_24h - snap.low_24h
        position = (snap.price - snap.low_24h) / range_size
        volatility = range_size / snap.price

        if snap.change_24h_pct > 2:
            bias = "bullish"
        elif snap.change_24h_pct < -2:
            bias = "bearish"
        else:
            bias = "neutral"

        return {
            "position": round(position, 3),
            "bias": bias,
            "volatility": round(volatility, 4),
            "price": snap.price,
            "change_pct": snap.change_24h_pct,
        }
