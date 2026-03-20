from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.config import settings
from polymarket_index.db.models import async_session
from polymarket_index.db import queries
from polymarket_index.signals.detector import RawSignal


@dataclass
class CopyOrder:
    """Ready-to-submit order constructed from a validated signal."""

    market_id: str
    side: str
    size: float  # USDC
    price: float
    signal_wallet: str
    signal_id: int | None
    reason: str


class TradeBuilder:
    """
    Constructs copy-trade orders from validated signals with position-sizing
    logic that accounts for wallet rank, agreement among top wallets,
    and market diversity.
    """

    def __init__(self, api_client: PolymarketClient) -> None:
        self._api = api_client

    async def build_order(
        self,
        signal: RawSignal,
        signal_db_id: int | None = None,
    ) -> CopyOrder | None:
        """
        Build a copy-trade order from a validated signal.
        Returns None if order cannot be constructed.
        """
        portfolio = settings.portfolio_value_usdc
        trade_pct = await self._determine_size_pct(signal)
        raw_size = portfolio * trade_pct

        if raw_size < 1.0:
            logger.debug("Trade size too small (${:.2f}), skipping", raw_size)
            return None

        price = await self._determine_price(signal)
        if price is None or price <= 0 or price >= 1.0:
            logger.debug(
                "Invalid price ({}) for market {}, skipping",
                price,
                signal.market_id[:12],
            )
            return None

        size_shares = raw_size / price

        reason_parts = [f"base={settings.base_trade_pct:.1%}"]
        if trade_pct != settings.base_trade_pct:
            reason_parts.append(f"adjusted={trade_pct:.1%}")

        order = CopyOrder(
            market_id=signal.market_id,
            side=signal.side,
            size=round(size_shares, 4),
            price=round(price, 4),
            signal_wallet=signal.wallet_address,
            signal_id=signal_db_id,
            reason=", ".join(reason_parts),
        )

        logger.info(
            "Built copy order: {} {} shares @ ${:.4f} on {} (${:.2f} USDC) [{}]",
            order.side,
            order.size,
            order.price,
            order.market_id[:12],
            raw_size,
            order.reason,
        )
        return order

    async def _determine_size_pct(self, signal: RawSignal) -> float:
        """
        Determine position size as a fraction of portfolio:
        - Base: 1% (configurable)
        - Boost to 2% if wallet is top-10 AND multiple top wallets agree
        - Reduce to 0.5% if market diversity is low
        """
        base = settings.base_trade_pct

        is_top_rank = (
            signal.wallet_rank is not None
            and signal.wallet_rank <= settings.top_wallet_boost_rank
        )

        agreement_count = await self._count_agreeing_wallets(
            signal.market_id, signal.side
        )
        has_agreement = agreement_count >= 2

        if is_top_rank and has_agreement:
            return settings.boosted_trade_pct

        diversity = await self._check_portfolio_diversity(signal.market_id)
        if diversity < 0.3:
            return settings.reduced_trade_pct

        return base

    async def _count_agreeing_wallets(
        self, market_id: str, side: str
    ) -> int:
        """Count how many indexed wallets have recently traded the same side."""
        async with async_session() as session:
            indexed = await queries.get_indexed_wallets(session)
            indexed_addrs = [w.address for w in indexed]
            return await queries.count_wallets_with_position(
                session, market_id, side, indexed_addrs
            )

    async def _check_portfolio_diversity(self, market_id: str) -> float:
        """
        Check how diverse our own copy-trade portfolio is.
        Returns a 0-1 score. Low values mean we're concentrated.
        """
        async with async_session() as session:
            from sqlalchemy import select, func
            from polymarket_index.db.models import Order

            stmt = (
                select(Order.market_id, func.count(Order.id))
                .where(Order.status.in_(["placed", "filled"]))
                .group_by(Order.market_id)
            )
            result = await session.execute(stmt)
            rows = result.all()

        if not rows:
            return 1.0

        total = sum(count for _, count in rows)
        fracs = [count / total for _, count in rows]
        hhi = sum(f * f for f in fracs)
        return 1.0 - hhi

    async def _determine_price(self, signal: RawSignal) -> float | None:
        """
        Get execution price: order book mid + small offset for slippage.
        Falls back to signal price if order book is unavailable.
        """
        try:
            book = await self._api.get_order_book(signal.market_id)
            if book.mid_price > 0:
                if signal.side.upper() == "YES":
                    return book.mid_price + settings.slippage_tolerance
                else:
                    return book.mid_price - settings.slippage_tolerance
        except Exception as exc:
            logger.debug("Order book fetch failed: {}", exc)

        return signal.price
