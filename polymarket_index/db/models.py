from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    ForeignKey,
    Index,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from polymarket_index.config import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Wallet(Base):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    first_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_active: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_score: Mapped[float] = mapped_column(Float, default=0.0)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_index: Mapped[bool] = mapped_column(Boolean, default=False)
    index_entered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="manual")

    trades: Mapped[list[Trade]] = relationship(back_populates="wallet", lazy="selectin")

    __table_args__ = (
        Index("ix_wallets_score", "current_score"),
        Index("ix_wallets_in_index", "in_index"),
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(
        String(42), ForeignKey("wallets.address"), index=True
    )
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    market_slug: Mapped[str | None] = mapped_column(String(256), nullable=True)
    side: Mapped[str] = mapped_column(String(4))  # YES / NO
    size: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    outcome: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # win / loss / pending
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    wallet: Mapped[Wallet] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_trades_wallet_ts", "wallet_address", "timestamp"),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True)
    market_id: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(4))
    size: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    wallet_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    detected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    orders: Mapped[list[Order]] = relationship(back_populates="signal", lazy="selectin")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), index=True
    )
    market_id: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(4))
    size: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending / placed / filled / cancelled / failed
    placed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    signal: Mapped[Signal] = relationship(back_populates="orders")


class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_time: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    top_wallets_json: Mapped[str] = mapped_column(Text)


engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
