"""
External odds feeds from Kalshi, The Odds API (sportsbooks),
and other prediction markets for cross-platform arbitrage.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp
from loguru import logger

from polymarket_index.config import settings

ODDS_API_URL = "https://api.the-odds-api.com/v4"


@dataclass
class ExternalOdds:
    source: str  # "kalshi", "draftkings", "fanduel", etc.
    event_name: str
    outcome: str
    price: float  # implied probability (0-1)
    raw_odds: str  # original format
    timestamp: float


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_price: float
    no_price: float
    volume: int
    status: str


class KalshiClient:
    """
    Kalshi prediction market API client.
    Public endpoints only — no auth needed for market data.
    """
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_markets(self, limit: int = 100, status: str = "open") -> list[KalshiMarket]:
        session = await self._get_session()
        try:
            url = f"{self.BASE_URL}/markets"
            params = {"limit": limit, "status": status}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.debug("Kalshi API returned {}", resp.status)
                    return []

                data = await resp.json()
                markets = data.get("markets", [])

                return [
                    KalshiMarket(
                        ticker=m.get("ticker", ""),
                        title=m.get("title", m.get("subtitle", "")),
                        yes_price=m.get("yes_ask", 0) / 100.0 if m.get("yes_ask") else 0.5,
                        no_price=m.get("no_ask", 0) / 100.0 if m.get("no_ask") else 0.5,
                        volume=m.get("volume", 0),
                        status=m.get("status", ""),
                    )
                    for m in markets
                ]
        except Exception as exc:
            logger.debug("Kalshi fetch failed: {}", exc)
            return []

    async def search_markets(self, query: str) -> list[KalshiMarket]:
        session = await self._get_session()
        try:
            url = f"{self.BASE_URL}/markets"
            params = {"limit": 50, "status": "open"}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                markets = data.get("markets", [])
                query_lower = query.lower()
                return [
                    KalshiMarket(
                        ticker=m.get("ticker", ""),
                        title=m.get("title", ""),
                        yes_price=m.get("yes_ask", 50) / 100.0,
                        no_price=m.get("no_ask", 50) / 100.0,
                        volume=m.get("volume", 0),
                        status=m.get("status", ""),
                    )
                    for m in markets
                    if query_lower in m.get("title", "").lower()
                ]
        except Exception:
            return []


class SportsOddsClient:
    """
    Fetches sports odds from The Odds API.
    Supports DraftKings, FanDuel, BetMGM, etc.
    Free tier: 500 requests/month.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_odds(
        self,
        sport: str = "basketball_nba",
        regions: str = "us",
        markets: str = "h2h",
    ) -> list[ExternalOdds]:
        if not self._api_key:
            return []

        session = await self._get_session()
        try:
            url = f"{ODDS_API_URL}/sports/{sport}/odds"
            params = {
                "apiKey": self._api_key,
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
            }
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                odds_list: list[ExternalOdds] = []

                for event in data:
                    event_name = f"{event.get('home_team', '')} vs {event.get('away_team', '')}"
                    for bookmaker in event.get("bookmakers", []):
                        source = bookmaker.get("key", "unknown")
                        for market in bookmaker.get("markets", []):
                            for outcome in market.get("outcomes", []):
                                decimal_odds = outcome.get("price", 0)
                                if decimal_odds > 0:
                                    implied_prob = 1.0 / decimal_odds
                                    odds_list.append(
                                        ExternalOdds(
                                            source=source,
                                            event_name=event_name,
                                            outcome=outcome.get("name", ""),
                                            price=round(implied_prob, 4),
                                            raw_odds=str(decimal_odds),
                                            timestamp=time.time(),
                                        )
                                    )

                return odds_list

        except Exception as exc:
            logger.debug("Odds API fetch failed: {}", exc)
            return []

    async def get_available_sports(self) -> list[dict]:
        if not self._api_key:
            return []
        session = await self._get_session()
        try:
            url = f"{ODDS_API_URL}/sports"
            async with session.get(url, params={"apiKey": self._api_key}) as resp:
                if resp.status != 200:
                    return []
                return await resp.json()
        except Exception:
            return []
