from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.config import settings
from polymarket_index.db.models import Signal, async_session
from polymarket_index.db import queries


@dataclass
class RawSignal:
    wallet_address: str
    market_id: str
    side: str
    size_usdc: float
    price: float
    timestamp: dt.datetime
    wallet_rank: int | None
    trade_id: str


class SignalDetector:
    """
    Polls tracked top-wallet trade activity and detects new trades
    that haven't been seen before (not in DB).
    """

    def __init__(self, api_client: PolymarketClient) -> None:
        self._api = api_client
        self._seen_trade_ids: dict[str, set[str]] = {}

    async def initialize(self) -> None:
        """Pre-load known trade IDs for all indexed wallets."""
        async with async_session() as session:
            indexed = await queries.get_indexed_wallets(session)
            for wallet in indexed:
                trade_ids = await queries.get_trade_ids_for_wallet(
                    session, wallet.address
                )
                self._seen_trade_ids[wallet.address] = trade_ids

        logger.info(
            "Signal detector initialized — tracking {} wallets",
            len(self._seen_trade_ids),
        )

    async def poll_for_signals(self) -> list[RawSignal]:
        """
        Check all indexed wallets for new trades since last poll.
        Returns a list of raw signals for newly detected trades.
        """
        async with async_session() as session:
            indexed = await queries.get_indexed_wallets(session)

        if not indexed:
            return []

        signals: list[RawSignal] = []
        sem = asyncio.Semaphore(5)

        async def _poll_wallet(wallet) -> list[RawSignal]:
            async with sem:
                return await self._detect_new_trades(
                    wallet.address, wallet.rank
                )

        tasks = [_poll_wallet(w) for w in indexed]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning("Signal poll error: {}", result)
            else:
                signals.extend(result)

        if signals:
            logger.info("Detected {} raw signals from {} wallets", len(signals), len(indexed))

        return signals

    async def _detect_new_trades(
        self,
        address: str,
        wallet_rank: int | None,
    ) -> list[RawSignal]:
        """Fetch recent trades for a wallet and return any unseen ones."""
        try:
            recent_trades = await self._api.get_trades(address, limit=20)
        except Exception as exc:
            logger.debug("Failed to fetch trades for {}: {}", address[:10], exc)
            return []

        seen = self._seen_trade_ids.get(address, set())
        new_signals: list[RawSignal] = []

        for trade in recent_trades:
            if trade.id in seen:
                continue

            seen.add(trade.id)

            ts = _parse_timestamp(trade.timestamp)
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()

            signal = RawSignal(
                wallet_address=address,
                market_id=trade.market_id,
                side=trade.side,
                size_usdc=trade.size * trade.price,
                price=trade.price,
                timestamp=ts,
                wallet_rank=wallet_rank,
                trade_id=trade.id,
            )
            new_signals.append(signal)

            logger.debug(
                "New trade: wallet={} market={} side={} size=${:.0f} age={:.0f}s",
                address[:10],
                trade.market_id[:12],
                trade.side,
                signal.size_usdc,
                age,
            )

        self._seen_trade_ids[address] = seen
        return new_signals

    async def persist_signal(self, raw: RawSignal) -> Signal:
        """Store a raw signal in the database and return the DB object."""
        signal = Signal(
            wallet_address=raw.wallet_address,
            market_id=raw.market_id,
            side=raw.side,
            size=raw.size_usdc,
            price=raw.price,
            wallet_rank=raw.wallet_rank,
            detected_at=dt.datetime.now(dt.timezone.utc),
        )

        async with async_session() as session:
            async with session.begin():
                signal = await queries.insert_signal(session, signal)

        return signal

    def refresh_tracked_wallets(self, addresses: list[str]) -> None:
        """Update the set of wallets being actively tracked."""
        current = set(self._seen_trade_ids.keys())
        new_addrs = set(addresses)

        for addr in new_addrs - current:
            self._seen_trade_ids[addr] = set()

        for addr in current - new_addrs:
            del self._seen_trade_ids[addr]


def _parse_timestamp(ts: str) -> dt.datetime:
    if not ts:
        return dt.datetime.now(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return dt.datetime.now(dt.timezone.utc)
