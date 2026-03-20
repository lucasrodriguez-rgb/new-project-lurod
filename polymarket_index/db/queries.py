from __future__ import annotations

import datetime as dt
import json
from typing import Sequence

from sqlalchemy import select, update, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_index.db.models import (
    Wallet,
    Trade,
    Signal,
    Order,
    LeaderboardSnapshot,
    async_session,
)


async def get_session() -> AsyncSession:
    return async_session()


# ── Wallet queries ──────────────────────────────────────────────────────


async def upsert_wallet(
    session: AsyncSession,
    address: str,
    source: str = "manual",
    **kwargs: object,
) -> Wallet:
    result = await session.get(Wallet, address.lower())
    if result is None:
        result = Wallet(address=address.lower(), source=source, **kwargs)
        session.add(result)
    else:
        for k, v in kwargs.items():
            setattr(result, k, v)
    await session.flush()
    return result


async def get_wallet(session: AsyncSession, address: str) -> Wallet | None:
    return await session.get(Wallet, address.lower())


async def get_all_wallets(session: AsyncSession) -> Sequence[Wallet]:
    result = await session.execute(select(Wallet).order_by(Wallet.current_score.desc()))
    return result.scalars().all()


async def get_indexed_wallets(session: AsyncSession) -> Sequence[Wallet]:
    result = await session.execute(
        select(Wallet)
        .where(Wallet.in_index.is_(True))
        .order_by(Wallet.rank.asc())
    )
    return result.scalars().all()


async def update_wallet_score(
    session: AsyncSession,
    address: str,
    score: float,
    rank: int | None = None,
    in_index: bool = False,
    index_entered_at: dt.datetime | None = None,
) -> None:
    stmt = (
        update(Wallet)
        .where(Wallet.address == address.lower())
        .values(
            current_score=score,
            rank=rank,
            in_index=in_index,
            index_entered_at=index_entered_at,
        )
    )
    await session.execute(stmt)


async def get_wallet_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(Wallet.address)))
    return result.scalar_one()


# ── Trade queries ───────────────────────────────────────────────────────


async def insert_trade(session: AsyncSession, trade: Trade) -> None:
    existing = await session.get(Trade, trade.id)
    if existing is None:
        session.add(trade)
        await session.flush()


async def bulk_insert_trades(session: AsyncSession, trades: list[Trade]) -> int:
    inserted = 0
    for trade in trades:
        existing = await session.get(Trade, trade.id)
        if existing is None:
            session.add(trade)
            inserted += 1
    if inserted:
        await session.flush()
    return inserted


async def get_wallet_trades(
    session: AsyncSession,
    address: str,
    since: dt.datetime | None = None,
) -> Sequence[Trade]:
    stmt = select(Trade).where(Trade.wallet_address == address.lower())
    if since:
        stmt = stmt.where(Trade.timestamp >= since)
    stmt = stmt.order_by(Trade.timestamp.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_latest_trade_id(session: AsyncSession, address: str) -> str | None:
    stmt = (
        select(Trade.id)
        .where(Trade.wallet_address == address.lower())
        .order_by(Trade.timestamp.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_trade_ids_for_wallet(
    session: AsyncSession, address: str
) -> set[str]:
    stmt = select(Trade.id).where(Trade.wallet_address == address.lower())
    result = await session.execute(stmt)
    return {row for row in result.scalars().all()}


async def count_wallets_with_position(
    session: AsyncSession,
    market_id: str,
    side: str,
    indexed_addresses: list[str],
) -> int:
    stmt = select(func.count(func.distinct(Trade.wallet_address))).where(
        and_(
            Trade.market_id == market_id,
            Trade.side == side,
            Trade.wallet_address.in_(indexed_addresses),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one()


# ── Signal queries ──────────────────────────────────────────────────────


async def insert_signal(session: AsyncSession, signal: Signal) -> Signal:
    session.add(signal)
    await session.flush()
    return signal


async def update_signal_validity(
    session: AsyncSession,
    signal_id: int,
    is_valid: bool,
    rejection_reason: str | None = None,
) -> None:
    stmt = (
        update(Signal)
        .where(Signal.id == signal_id)
        .values(is_valid=is_valid, rejection_reason=rejection_reason)
    )
    await session.execute(stmt)


# ── Order queries ───────────────────────────────────────────────────────


async def insert_order(session: AsyncSession, order: Order) -> Order:
    session.add(order)
    await session.flush()
    return order


async def update_order_status(
    session: AsyncSession,
    order_id: int,
    status: str,
    filled_at: dt.datetime | None = None,
) -> None:
    values: dict = {"status": status}
    if filled_at:
        values["filled_at"] = filled_at
    stmt = update(Order).where(Order.id == order_id).values(**values)
    await session.execute(stmt)


# ── Leaderboard snapshots ──────────────────────────────────────────────


async def save_leaderboard_snapshot(
    session: AsyncSession,
    wallets: list[dict],
) -> LeaderboardSnapshot:
    snapshot = LeaderboardSnapshot(
        snapshot_time=dt.datetime.now(dt.timezone.utc),
        top_wallets_json=json.dumps(wallets),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def get_latest_snapshot(
    session: AsyncSession,
) -> LeaderboardSnapshot | None:
    stmt = (
        select(LeaderboardSnapshot)
        .order_by(LeaderboardSnapshot.snapshot_time.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
