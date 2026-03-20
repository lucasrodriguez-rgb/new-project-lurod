"""
Fetches and caches historical Polymarket data for backtesting.
Uses only the public Gamma API — no API keys required.
"""
from __future__ import annotations

import asyncio
import json
import datetime as dt
from pathlib import Path
from dataclasses import dataclass, asdict

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient, TradeRecord, Market


DATA_DIR = Path("backtest_data")


@dataclass
class WalletHistory:
    address: str
    trades: list[dict]
    fetched_at: str
    trade_count: int


@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    slug: str
    volume: float
    liquidity: float
    close_time: str | None
    outcome_prices: dict[str, float]
    fetched_at: str


class DataCollector:
    """
    Fetches historical trade data from the Polymarket Gamma API and
    caches it locally as JSON so you only hit the API once.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._api = PolymarketClient()
        self._cache_dir = cache_dir or DATA_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / "wallets").mkdir(exist_ok=True)
        (self._cache_dir / "markets").mkdir(exist_ok=True)

    async def close(self) -> None:
        await self._api.close()

    def _wallet_cache_path(self, address: str) -> Path:
        return self._cache_dir / "wallets" / f"{address.lower()}.json"

    def _market_cache_path(self, market_id: str) -> Path:
        safe_id = market_id.replace("/", "_")[:64]
        return self._cache_dir / "markets" / f"{safe_id}.json"

    # ── Wallet trade history ────────────────────────────────────────────

    async def fetch_wallet_trades(
        self, address: str, force_refresh: bool = False
    ) -> WalletHistory:
        cache_path = self._wallet_cache_path(address)

        if cache_path.exists() and not force_refresh:
            data = json.loads(cache_path.read_text())
            logger.debug(
                "Loaded {} trades for {} from cache",
                data["trade_count"],
                address[:10],
            )
            return WalletHistory(**data)

        logger.info("Fetching trade history for {}...", address[:10])
        trades = await self._api.get_all_trades(address)

        history = WalletHistory(
            address=address.lower(),
            trades=[_trade_to_dict(t) for t in trades],
            fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            trade_count=len(trades),
        )

        cache_path.write_text(json.dumps(asdict(history), indent=2))
        logger.info("Cached {} trades for {}", len(trades), address[:10])
        return history

    async def fetch_multiple_wallets(
        self,
        addresses: list[str],
        force_refresh: bool = False,
    ) -> dict[str, WalletHistory]:
        results: dict[str, WalletHistory] = {}
        sem = asyncio.Semaphore(3)

        async def _fetch(addr: str) -> None:
            async with sem:
                try:
                    results[addr] = await self.fetch_wallet_trades(
                        addr, force_refresh=force_refresh
                    )
                except Exception as exc:
                    logger.error("Failed to fetch {}: {}", addr[:10], exc)

        await asyncio.gather(*[_fetch(a) for a in addresses])
        return results

    # ── Discover wallets from leaderboard ───────────────────────────────

    async def discover_top_wallets(self, limit: int = 100) -> list[str]:
        cache_path = self._cache_dir / "leaderboard.json"

        if cache_path.exists():
            data = json.loads(cache_path.read_text())
            age_hours = (
                dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(data["fetched_at"])
            ).total_seconds() / 3600
            if age_hours < 24:
                logger.info(
                    "Using cached leaderboard ({} wallets, {:.1f}h old)",
                    len(data["addresses"]),
                    age_hours,
                )
                return data["addresses"]

        logger.info("Fetching leaderboard...")
        addresses = await self._api.scrape_leaderboard(limit=limit)

        cache_path.write_text(
            json.dumps(
                {
                    "addresses": addresses,
                    "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
                indent=2,
            )
        )
        logger.info("Cached {} leaderboard wallets", len(addresses))
        return addresses

    # ── Market data ─────────────────────────────────────────────────────

    async def fetch_market(
        self, market_id: str, force_refresh: bool = False
    ) -> MarketSnapshot | None:
        cache_path = self._market_cache_path(market_id)

        if cache_path.exists() and not force_refresh:
            data = json.loads(cache_path.read_text())
            return MarketSnapshot(**data)

        market = await self._api.get_market_by_id(market_id)
        if market is None:
            return None

        snapshot = MarketSnapshot(
            market_id=market.id,
            question=market.question,
            slug=market.slug,
            volume=market.volume,
            liquidity=market.liquidity,
            close_time=market.close_time,
            outcome_prices=market.outcome_prices,
            fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

        cache_path.write_text(json.dumps(asdict(snapshot), indent=2))
        return snapshot

    # ── Full data pull ──────────────────────────────────────────────────

    async def collect_full_dataset(
        self,
        wallet_addresses: list[str] | None = None,
        leaderboard_limit: int = 50,
        force_refresh: bool = False,
    ) -> dict:
        """
        One-command data pull: discover wallets, fetch all trades, cache everything.
        Returns summary stats.
        """
        if wallet_addresses:
            addresses = wallet_addresses
        else:
            addresses = await self.discover_top_wallets(limit=leaderboard_limit)

        if not addresses:
            logger.warning("No wallets to collect data for")
            return {"wallets": 0, "total_trades": 0}

        histories = await self.fetch_multiple_wallets(
            addresses, force_refresh=force_refresh
        )

        total_trades = sum(h.trade_count for h in histories.values())
        logger.info(
            "Data collection complete: {} wallets, {} total trades",
            len(histories),
            total_trades,
        )

        return {
            "wallets": len(histories),
            "total_trades": total_trades,
            "addresses": list(histories.keys()),
        }


def _trade_to_dict(t: TradeRecord) -> dict:
    return {
        "id": t.id,
        "market_id": t.market_id,
        "market_slug": t.market_slug,
        "side": t.side,
        "size": t.size,
        "price": t.price,
        "timestamp": t.timestamp,
        "outcome": t.outcome,
    }
