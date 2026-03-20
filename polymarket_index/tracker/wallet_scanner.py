from __future__ import annotations

import asyncio
import datetime as dt

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_index.api.polymarket import PolymarketClient, TradeRecord
from polymarket_index.api.onchain import PolygonReader
from polymarket_index.config import settings
from polymarket_index.db.models import Wallet, Trade, async_session
from polymarket_index.db import queries


class WalletScanner:
    """Discovers and ingests active Polymarket wallets from multiple sources."""

    def __init__(
        self,
        api_client: PolymarketClient,
        onchain_reader: PolygonReader,
    ) -> None:
        self._api = api_client
        self._onchain = onchain_reader

    async def seed_wallets(self) -> list[str]:
        """
        Seed wallet list from all available sources:
        1. Polymarket leaderboard
        2. On-chain activity scan
        3. Manual seed list from config
        """
        discovered: set[str] = set()

        leaderboard_task = self._seed_from_leaderboard()
        onchain_task = self._seed_from_onchain()
        manual_task = self._seed_from_config()

        results = await asyncio.gather(
            leaderboard_task, onchain_task, manual_task, return_exceptions=True
        )

        for i, result in enumerate(results):
            source = ["leaderboard", "onchain", "config"][i]
            if isinstance(result, Exception):
                logger.warning("Seed source '{}' failed: {}", source, result)
            else:
                logger.info(
                    "Seed source '{}' returned {} wallets", source, len(result)
                )
                discovered.update(result)

        async with async_session() as session:
            async with session.begin():
                for addr in discovered:
                    await queries.upsert_wallet(session, addr, source="seed")

        logger.info("Total unique wallets seeded: {}", len(discovered))
        return list(discovered)

    async def _seed_from_leaderboard(self) -> list[str]:
        try:
            addresses = await self._api.scrape_leaderboard(limit=200)
            return [a.lower() for a in addresses if a]
        except Exception as exc:
            logger.warning("Leaderboard seed failed: {}", exc)
            return []

    async def _seed_from_onchain(self) -> list[str]:
        try:
            return await self._onchain.extract_active_wallets(
                blocks_back=10_000, min_trade_count=5
            )
        except Exception as exc:
            logger.warning("On-chain seed failed: {}", exc)
            return []

    async def _seed_from_config(self) -> list[str]:
        return [a.lower() for a in settings.seed_wallets if a]

    async def ingest_wallet_trades(self, address: str) -> int:
        """
        Pull full trade history for a wallet and store in DB.
        Returns the number of new trades inserted.
        """
        address = address.lower()
        logger.debug("Ingesting trades for {}", address)

        trades = await self._api.get_all_trades(address)
        if not trades:
            return 0

        db_trades = [self._trade_record_to_model(address, t) for t in trades]

        async with async_session() as session:
            async with session.begin():
                inserted = await queries.bulk_insert_trades(session, db_trades)

                await queries.upsert_wallet(
                    session,
                    address,
                    last_active=dt.datetime.now(dt.timezone.utc),
                    total_trades=len(trades),
                )

        logger.info(
            "Wallet {} — {} total trades, {} new", address[:10], len(trades), inserted
        )
        return inserted

    async def ingest_all_wallets(self) -> dict[str, int]:
        """Ingest trades for all known wallets. Returns {address: new_trade_count}."""
        async with async_session() as session:
            wallets = await queries.get_all_wallets(session)

        results: dict[str, int] = {}
        sem = asyncio.Semaphore(5)

        async def _ingest(addr: str) -> None:
            async with sem:
                try:
                    count = await self.ingest_wallet_trades(addr)
                    results[addr] = count
                except Exception as exc:
                    logger.error("Failed to ingest {}: {}", addr[:10], exc)
                    results[addr] = 0

        tasks = [_ingest(w.address) for w in wallets]
        await asyncio.gather(*tasks)

        total_new = sum(results.values())
        logger.info(
            "Ingested {} wallets, {} new trades total", len(results), total_new
        )
        return results

    async def discover_new_wallets_from_recent_trades(self) -> list[str]:
        """
        Look at recent on-chain activity and add any newly discovered wallets.
        """
        try:
            active = await self._onchain.extract_active_wallets(
                blocks_back=2000, min_trade_count=3
            )
        except Exception as exc:
            logger.warning("On-chain discovery failed: {}", exc)
            return []

        new_wallets: list[str] = []
        async with async_session() as session:
            async with session.begin():
                for addr in active:
                    existing = await queries.get_wallet(session, addr)
                    if existing is None:
                        await queries.upsert_wallet(
                            session, addr, source="onchain_discovery"
                        )
                        new_wallets.append(addr)

        if new_wallets:
            logger.info("Discovered {} new wallets from on-chain scan", len(new_wallets))
        return new_wallets

    @staticmethod
    def _trade_record_to_model(wallet_address: str, t: TradeRecord) -> Trade:
        return Trade(
            id=t.id,
            wallet_address=wallet_address.lower(),
            market_id=t.market_id,
            market_slug=t.market_slug,
            side=t.side,
            size=t.size,
            price=t.price,
            timestamp=_parse_timestamp(t.timestamp),
            outcome=t.outcome,
        )


def _parse_timestamp(ts: str) -> dt.datetime:
    """Parse ISO-format timestamp, falling back to epoch on failure."""
    if not ts:
        return dt.datetime.now(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return dt.datetime.now(dt.timezone.utc)
