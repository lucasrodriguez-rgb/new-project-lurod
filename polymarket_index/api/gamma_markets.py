"""
Polymarket Gamma API — fetch active markets with implied probabilities,
sorted by 24h volume. No auth needed.

Usage:
    python3 -m polymarket_index.api.gamma_markets
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import aiohttp
from loguru import logger

GAMMA_URL = "https://gamma-api.polymarket.com"


@dataclass
class LiveMarket:
    condition_id: str
    question: str
    slug: str
    yes_price: float
    no_price: float
    best_bid: float
    best_ask: float
    spread: float
    volume_24h: float
    volume_total: float
    liquidity: float
    end_date: str
    category: str
    token_id_yes: str
    token_id_no: str
    active: bool
    implied_prob_yes: float
    implied_prob_no: float

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.best_bid and self.best_ask else self.yes_price


async def fetch_active_markets(
    limit: int = 100,
    sort_by: str = "volume24hr",
    category: str | None = None,
    min_volume_24h: float = 0,
    min_liquidity: float = 0,
) -> list[LiveMarket]:
    """
    Fetch all active markets from the Gamma API, sorted by 24h volume.
    Returns typed LiveMarket objects with implied probabilities.
    """
    all_markets: list[LiveMarket] = []
    offset = 0
    page_size = min(limit, 100)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while len(all_markets) < limit:
            params: dict[str, Any] = {
                "closed": "false",
                "active": "true",
                "limit": page_size,
                "offset": offset,
                "order": sort_by,
                "ascending": "false",
            }

            async with session.get(f"{GAMMA_URL}/markets", params=params) as resp:
                if resp.status != 200:
                    logger.warning("Gamma API returned {}", resp.status)
                    break

                data = await resp.json()
                if not data:
                    break

                for m in data:
                    market = _parse_market(m)
                    if market is None:
                        continue

                    if market.volume_24h < min_volume_24h:
                        continue
                    if market.liquidity < min_liquidity:
                        continue
                    if category and category.lower() not in market.slug.lower():
                        continue

                    all_markets.append(market)

                    if len(all_markets) >= limit:
                        break

                if len(data) < page_size:
                    break
                offset += page_size

    return all_markets


async def fetch_market_by_slug(slug: str) -> LiveMarket | None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(f"{GAMMA_URL}/markets", params={"slug": slug}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not data:
                return None
            return _parse_market(data[0] if isinstance(data, list) else data)


async def fetch_position_history(wallet_address: str, limit: int = 100) -> list[dict]:
    """Pull position history from the Data API for a wallet address."""
    url = "https://data-api.polymarket.com/activity"
    params = {"user": wallet_address, "limit": limit, "type": "TRADE",
              "sortBy": "TIMESTAMP", "sortDirection": "DESC"}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data if isinstance(data, list) else []


def _parse_market(m: dict) -> LiveMarket | None:
    """Parse a raw Gamma API market dict into a LiveMarket."""
    try:
        prices_raw = m.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw or []

        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)

        token_ids_raw = m.get("clobTokenIds", "[]")
        if isinstance(token_ids_raw, str):
            token_ids = json.loads(token_ids_raw)
        else:
            token_ids = token_ids_raw or []

        best_bid = float(m.get("bestBid") or 0)
        best_ask = float(m.get("bestAsk") or 0)

        return LiveMarket(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            yes_price=yes_price,
            no_price=no_price,
            best_bid=best_bid,
            best_ask=best_ask if best_ask > 0 else yes_price,
            spread=float(m.get("spread") or 0),
            volume_24h=float(m.get("volume24hr") or 0),
            volume_total=float(m.get("volumeNum") or m.get("volume") or 0),
            liquidity=float(m.get("liquidityNum") or m.get("liquidity") or 0),
            end_date=m.get("endDate") or m.get("endDateIso") or "",
            category=m.get("slug", "").split("-")[0] if m.get("slug") else "",
            token_id_yes=str(token_ids[0]) if len(token_ids) > 0 else "",
            token_id_no=str(token_ids[1]) if len(token_ids) > 1 else "",
            active=m.get("active", False),
            implied_prob_yes=yes_price,
            implied_prob_no=no_price,
        )
    except Exception as exc:
        logger.debug("Failed to parse market: {}", exc)
        return None


def print_markets(markets: list[LiveMarket], top_n: int = 25) -> None:
    """Pretty-print a list of markets."""
    print(f"\n{'='*90}")
    print(f"  POLYMARKET — TOP {min(top_n, len(markets))} ACTIVE MARKETS BY 24H VOLUME")
    print(f"{'='*90}")
    print(f"  {'#':>3}  {'YES':>6}  {'NO':>6}  {'Spread':>6}  {'24h Vol':>12}  {'Liquidity':>10}  Question")
    print(f"  {'—'*84}")

    for i, m in enumerate(markets[:top_n]):
        vol_str = f"${m.volume_24h:>10,.0f}" if m.volume_24h > 0 else "     —"
        liq_str = f"${m.liquidity:>8,.0f}" if m.liquidity > 0 else "   —"
        print(
            f"  {i+1:3d}  {m.yes_price:5.1%}  {m.no_price:5.1%}  "
            f"{m.spread:5.3f}  {vol_str}  {liq_str}  {m.question[:40]}"
        )

    total_vol = sum(m.volume_24h for m in markets[:top_n])
    total_liq = sum(m.liquidity for m in markets[:top_n])
    print(f"  {'—'*84}")
    print(f"  Total 24h volume: ${total_vol:,.0f}   Total liquidity: ${total_liq:,.0f}")
    print(f"{'='*90}\n")


async def main() -> None:
    markets = await fetch_active_markets(limit=50, min_volume_24h=1000)
    print_markets(markets, top_n=25)


if __name__ == "__main__":
    asyncio.run(main())
