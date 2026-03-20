"""
Auto-detect active, tradeable markets from the Polymarket CLOB and Gamma APIs.
Filters by volume, liquidity, spread, and time-to-resolution.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient, Market, OrderBook
from polymarket_index.config import settings


@dataclass
class ActiveMarket:
    condition_id: str
    question: str
    slug: str
    volume: float
    liquidity: float
    close_time: dt.datetime | None
    hours_to_close: float | None
    yes_price: float
    no_price: float
    spread: float
    mid_price: float
    category: str
    token_ids: list[str] = field(default_factory=list)


class MarketScanner:
    """Scans Polymarket for active, liquid markets with tight spreads."""

    def __init__(self, api_client: PolymarketClient) -> None:
        self._api = api_client
        self._market_cache: dict[str, ActiveMarket] = {}

    async def scan(
        self,
        min_volume: float | None = None,
        min_liquidity: float | None = None,
        max_spread: float = 0.15,
        min_hours_to_close: float = 1.0,
        category_filter: str | None = None,
    ) -> list[ActiveMarket]:
        min_volume = min_volume or settings.min_market_volume
        min_liquidity = min_liquidity or settings.min_market_liquidity

        logger.info("Scanning for active markets (vol>${:.0f}, liq>${:.0f})...",
                     min_volume, min_liquidity)

        # Fetch top markets by volume (not ALL markets — too slow)
        all_markets: list[Market] = []
        for offset in range(0, 500, 100):
            batch = await self._api.get_markets(limit=100, offset=offset, active=True)
            all_markets.extend(batch)
            if len(batch) < 100:
                break
        logger.info("Fetched {} markets from Gamma API", len(all_markets))

        now = dt.datetime.now(dt.timezone.utc)
        candidates: list[ActiveMarket] = []

        for m in all_markets:
            if m.closed or not m.active:
                continue
            if m.volume < min_volume and m.liquidity < min_liquidity:
                continue
            if category_filter and m.category.lower() != category_filter.lower():
                continue

            close_dt = None
            hours_left = None
            if m.close_time:
                try:
                    close_dt = dt.datetime.fromisoformat(m.close_time.replace("Z", "+00:00"))
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if hours_left < min_hours_to_close:
                        continue
                except (ValueError, TypeError):
                    pass

            yes_price = m.outcome_prices.get("Yes", m.outcome_prices.get("yes", 0.5))
            no_price = m.outcome_prices.get("No", m.outcome_prices.get("no", 0.5))

            if yes_price <= 0.01 or yes_price >= 0.99:
                continue

            spread = abs(1.0 - yes_price - no_price)
            mid = (yes_price + (1.0 - no_price)) / 2.0

            if spread > max_spread:
                continue

            active = ActiveMarket(
                condition_id=m.id,
                question=m.question,
                slug=m.slug,
                volume=m.volume,
                liquidity=m.liquidity,
                close_time=close_dt,
                hours_to_close=hours_left,
                yes_price=yes_price,
                no_price=no_price,
                spread=spread,
                mid_price=mid,
                category=m.category,
            )
            candidates.append(active)
            self._market_cache[m.id] = active

        candidates.sort(key=lambda x: x.volume, reverse=True)
        logger.info("Found {} tradeable markets", len(candidates))
        return candidates

    async def scan_crypto_markets(self) -> list[ActiveMarket]:
        """Find crypto price prediction markets (BTC, ETH, SOL, XRP up/down)."""
        all_markets = await self.scan(min_volume=1000, min_liquidity=500)
        crypto_keywords = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana",
                           "xrp", "crypto", "updown"]
        return [
            m for m in all_markets
            if any(kw in m.slug.lower() or kw in m.question.lower() for kw in crypto_keywords)
        ]

    async def get_order_book_depth(self, token_id: str) -> dict:
        """Fetch order book and compute depth metrics."""
        try:
            book = await self._api.get_order_book(token_id)
            bid_depth = sum(l.size * l.price for l in book.bids[:10])
            ask_depth = sum(l.size * l.price for l in book.asks[:10])
            return {
                "mid_price": book.mid_price,
                "best_bid": book.bids[0].price if book.bids else 0,
                "best_ask": book.asks[0].price if book.asks else 1,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "spread": (book.asks[0].price - book.bids[0].price) if book.bids and book.asks else 1,
            }
        except Exception as exc:
            logger.debug("Order book fetch failed for {}: {}", token_id[:12], exc)
            return {"mid_price": 0, "best_bid": 0, "best_ask": 1, "bid_depth": 0, "ask_depth": 0, "spread": 1}

    def get_cached_market(self, condition_id: str) -> ActiveMarket | None:
        return self._market_cache.get(condition_id)
