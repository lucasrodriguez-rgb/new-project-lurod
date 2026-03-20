from __future__ import annotations

import datetime as dt

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.config import settings
from polymarket_index.db.models import Order as OrderModel, async_session
from polymarket_index.db import queries
from polymarket_index.executor.trade_builder import CopyOrder


class OrderManager:
    """
    Places and manages orders via the Polymarket CLOB API.
    Handles order lifecycle: pending → placed → filled/cancelled/failed.
    """

    def __init__(self, api_client: PolymarketClient, dry_run: bool = True) -> None:
        self._api = api_client
        self._dry_run = dry_run
        self._pending_orders: list[OrderModel] = []

    async def submit_order(self, copy_order: CopyOrder) -> OrderModel:
        """
        Submit a copy-trade order. In dry_run mode, the order is logged
        and stored in DB but not actually placed on the CLOB.
        """
        now = dt.datetime.now(dt.timezone.utc)

        db_order = OrderModel(
            signal_id=copy_order.signal_id,
            market_id=copy_order.market_id,
            side=copy_order.side,
            size=copy_order.size,
            price=copy_order.price,
            status="pending",
        )

        async with async_session() as session:
            async with session.begin():
                db_order = await queries.insert_order(session, db_order)
                order_id = db_order.id

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would place order: {} {:.4f} @ ${:.4f} on {} (order #{})",
                copy_order.side,
                copy_order.size,
                copy_order.price,
                copy_order.market_id[:12],
                order_id,
            )
            await self._update_status(order_id, "dry_run", placed_at=now)
            return db_order

        try:
            response = await self._api.place_order(
                token_id=copy_order.market_id,
                side=copy_order.side,
                size=copy_order.size,
                price=copy_order.price,
            )

            logger.info(
                "Order placed: {} {} @ {} on {} — CLOB ID: {} status: {}",
                copy_order.side,
                copy_order.size,
                copy_order.price,
                copy_order.market_id[:12],
                response.order_id,
                response.status,
            )

            status = "placed" if response.status in ("live", "open", "matched") else response.status
            await self._update_status(order_id, status, placed_at=now)

        except Exception as exc:
            logger.error("Order placement failed: {}", exc)
            await self._update_status(order_id, "failed")

        return db_order

    async def check_open_orders(self) -> list[OrderModel]:
        """
        Check status of all placed (open) orders and update accordingly.
        Returns list of orders whose status changed.
        """
        updated: list[OrderModel] = []

        async with async_session() as session:
            from sqlalchemy import select
            stmt = select(OrderModel).where(OrderModel.status == "placed")
            result = await session.execute(stmt)
            open_orders = list(result.scalars().all())

        for order in open_orders:
            try:
                book = await self._api.get_order_book(order.market_id)
                if book.mid_price > 0:
                    updated.append(order)
            except Exception as exc:
                logger.debug("Failed to check order {}: {}", order.id, exc)

        return updated

    async def cancel_stale_orders(self, max_age_minutes: int = 30) -> int:
        """Cancel orders that have been open too long without filling."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=max_age_minutes)
        cancelled = 0

        async with async_session() as session:
            from sqlalchemy import select
            stmt = select(OrderModel).where(
                OrderModel.status == "placed",
                OrderModel.placed_at < cutoff,
            )
            result = await session.execute(stmt)
            stale_orders = list(result.scalars().all())

        for order in stale_orders:
            if not self._dry_run:
                try:
                    await self._api.cancel_order(str(order.id))
                except Exception as exc:
                    logger.warning("Failed to cancel order {}: {}", order.id, exc)
                    continue

            await self._update_status(order.id, "cancelled")
            cancelled += 1

        if cancelled:
            logger.info("Cancelled {} stale orders", cancelled)
        return cancelled

    async def get_portfolio_summary(self) -> dict:
        """Return a summary of all orders by status."""
        async with async_session() as session:
            from sqlalchemy import select, func
            stmt = (
                select(OrderModel.status, func.count(OrderModel.id), func.sum(OrderModel.size * OrderModel.price))
                .group_by(OrderModel.status)
            )
            result = await session.execute(stmt)
            rows = result.all()

        return {
            status: {"count": count, "total_value": float(value or 0)}
            for status, count, value in rows
        }

    async def _update_status(
        self,
        order_id: int,
        status: str,
        placed_at: dt.datetime | None = None,
        filled_at: dt.datetime | None = None,
    ) -> None:
        async with async_session() as session:
            async with session.begin():
                from sqlalchemy import update
                values: dict = {"status": status}
                if placed_at:
                    values["placed_at"] = placed_at
                if filled_at:
                    values["filled_at"] = filled_at
                stmt = update(OrderModel).where(OrderModel.id == order_id).values(**values)
                await session.execute(stmt)
