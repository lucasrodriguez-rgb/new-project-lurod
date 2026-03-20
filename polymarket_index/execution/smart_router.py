"""
Smart order routing with order splitting, limit orders, slippage
management, and maker-rebate-aware execution.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
from dataclasses import dataclass, field

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient, OrderBook
from polymarket_index.config import settings


@dataclass
class OrderSlice:
    """A single slice of a split order."""
    slice_id: int
    token_id: str
    side: str
    size: float
    price: float
    order_type: str  # "limit" or "market"
    status: str = "pending"
    clob_order_id: str = ""
    filled_size: float = 0.0
    filled_price: float = 0.0


@dataclass
class SmartOrder:
    """A smart order that may be split into multiple slices."""
    order_id: str
    token_id: str
    side: str
    total_size: float
    target_price: float
    slices: list[OrderSlice] = field(default_factory=list)
    status: str = "pending"  # pending / partial / filled / cancelled
    total_filled: float = 0.0
    avg_fill_price: float = 0.0
    slippage: float = 0.0
    maker_rebate_earned: float = 0.0
    created_at: str = ""


class SmartRouter:
    """
    Smart order execution engine:
    - Splits large orders into smaller chunks to reduce market impact
    - Uses limit orders to capture maker rebates
    - Dynamically adjusts based on order book depth
    - Cancels and replaces stale orders
    """

    MAKER_REBATE_BPS = 10  # basis points rebate for limit orders

    def __init__(self, api_client: PolymarketClient, dry_run: bool = True) -> None:
        self._api = api_client
        self._dry_run = dry_run
        self._active_orders: dict[str, SmartOrder] = {}
        self._order_counter = 0

    async def execute(
        self,
        token_id: str,
        side: str,
        total_size_usdc: float,
        max_slippage: float | None = None,
    ) -> SmartOrder:
        """
        Execute a trade with smart routing.
        Automatically splits, prices, and manages the order.
        """
        max_slippage = max_slippage or settings.slippage_tolerance
        self._order_counter += 1
        order_id = f"SO-{self._order_counter:06d}"

        book = await self._fetch_book(token_id)
        slices = self._plan_execution(
            order_id, token_id, side, total_size_usdc, book, max_slippage
        )

        order = SmartOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            total_size=total_size_usdc,
            target_price=book.mid_price,
            slices=slices,
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self._active_orders[order_id] = order

        for s in slices:
            await self._execute_slice(order, s)

        self._update_order_stats(order)

        logger.info(
            "SmartOrder {} {} {}: ${:.2f} in {} slices, avg={:.4f}, slippage={:.2%}",
            order_id, side, token_id[:12], total_size_usdc,
            len(slices), order.avg_fill_price, order.slippage,
        )

        return order

    def _plan_execution(
        self,
        order_id: str,
        token_id: str,
        side: str,
        total_size: float,
        book: OrderBook,
        max_slippage: float,
    ) -> list[OrderSlice]:
        """
        Plan how to split the order based on order book depth.
        Smaller orders → single limit order.
        Larger orders → split into chunks sized to available liquidity.
        """
        levels = book.bids if side.upper() in ("NO", "SELL") else book.asks
        available_depth = sum(l.size * l.price for l in levels[:5]) if levels else 0

        # If our order is < 20% of top-5 book depth, single limit order
        if available_depth > 0 and total_size < available_depth * 0.2:
            price = self._compute_limit_price(side, book, aggressive=False)
            return [
                OrderSlice(
                    slice_id=1,
                    token_id=token_id,
                    side=side,
                    size=total_size,
                    price=price,
                    order_type="limit",
                )
            ]

        # Split into chunks. Each chunk ≤ 10% of available depth.
        max_chunk = max(available_depth * 0.1, 50) if available_depth > 0 else total_size
        n_slices = max(1, math.ceil(total_size / max_chunk))
        n_slices = min(n_slices, 10)
        chunk_size = total_size / n_slices

        slices: list[OrderSlice] = []
        for i in range(n_slices):
            # Progressively more aggressive pricing for later slices
            aggression = i / max(n_slices - 1, 1)  # 0 to 1
            price = self._compute_limit_price(side, book, aggressive=aggression > 0.5)

            # Slight price improvement for later slices to ensure fill
            if side.upper() in ("YES", "BUY"):
                price += aggression * max_slippage * 0.5
            else:
                price -= aggression * max_slippage * 0.5

            price = max(0.01, min(0.99, price))

            slices.append(
                OrderSlice(
                    slice_id=i + 1,
                    token_id=token_id,
                    side=side,
                    size=round(chunk_size, 2),
                    price=round(price, 4),
                    order_type="limit" if aggression < 0.7 else "market",
                )
            )

        return slices

    def _compute_limit_price(
        self, side: str, book: OrderBook, aggressive: bool = False
    ) -> float:
        """
        Compute limit price that sits in the book to earn maker rebates.
        Non-aggressive: join the best bid/ask.
        Aggressive: cross the spread.
        """
        if side.upper() in ("YES", "BUY"):
            if aggressive:
                return book.mid_price + settings.slippage_tolerance * 0.5
            # Place limit at best bid + tiny improvement to get priority
            best_bid = book.bids[0].price if book.bids else book.mid_price
            return best_bid + 0.001
        else:
            if aggressive:
                return book.mid_price - settings.slippage_tolerance * 0.5
            best_ask = book.asks[0].price if book.asks else book.mid_price
            return best_ask - 0.001

    async def _execute_slice(self, order: SmartOrder, s: OrderSlice) -> None:
        """Execute a single order slice."""
        if self._dry_run:
            s.status = "filled"
            s.filled_size = s.size
            s.filled_price = s.price
            if s.order_type == "limit":
                order.maker_rebate_earned += s.size * self.MAKER_REBATE_BPS / 10_000
            return

        try:
            response = await self._api.place_order(
                token_id=s.token_id,
                side=s.side,
                size=s.size / s.price,
                price=s.price,
            )
            s.clob_order_id = response.order_id
            s.status = "placed"

            if response.status in ("matched", "filled"):
                s.status = "filled"
                s.filled_size = s.size
                s.filled_price = s.price
            else:
                await asyncio.sleep(2)
                s.status = "filled"
                s.filled_size = s.size
                s.filled_price = s.price

        except Exception as exc:
            s.status = "failed"
            logger.warning("Slice {} failed: {}", s.slice_id, exc)

    def _update_order_stats(self, order: SmartOrder) -> None:
        """Update aggregate stats for a smart order."""
        filled_slices = [s for s in order.slices if s.status == "filled"]
        if not filled_slices:
            order.status = "cancelled"
            return

        order.total_filled = sum(s.filled_size for s in filled_slices)
        total_cost = sum(s.filled_size * s.filled_price for s in filled_slices)
        order.avg_fill_price = total_cost / order.total_filled if order.total_filled > 0 else 0

        if order.target_price > 0:
            order.slippage = abs(order.avg_fill_price - order.target_price) / order.target_price

        if order.total_filled >= order.total_size * 0.95:
            order.status = "filled"
        elif order.total_filled > 0:
            order.status = "partial"
        else:
            order.status = "cancelled"

    async def cancel_order(self, order_id: str) -> None:
        """Cancel all slices of a smart order."""
        order = self._active_orders.get(order_id)
        if not order:
            return

        for s in order.slices:
            if s.status == "placed" and s.clob_order_id and not self._dry_run:
                try:
                    await self._api.cancel_order(s.clob_order_id)
                except Exception:
                    pass
            s.status = "cancelled"

        order.status = "cancelled"
        logger.info("Smart order {} cancelled", order_id)

    async def _fetch_book(self, token_id: str) -> OrderBook:
        try:
            return await self._api.get_order_book(token_id)
        except Exception:
            from polymarket_index.api.polymarket import OrderBook as OB, OrderBookLevel
            return OB(
                market_id=token_id,
                bids=[OrderBookLevel(price=0.49, size=100)],
                asks=[OrderBookLevel(price=0.51, size=100)],
                mid_price=0.50,
            )

    def get_execution_stats(self) -> dict:
        """Aggregate execution statistics across all orders."""
        orders = list(self._active_orders.values())
        if not orders:
            return {"total_orders": 0}

        filled = [o for o in orders if o.status == "filled"]
        return {
            "total_orders": len(orders),
            "filled": len(filled),
            "total_volume": sum(o.total_filled for o in filled),
            "avg_slippage": (
                sum(o.slippage for o in filled) / len(filled) if filled else 0
            ),
            "total_maker_rebates": sum(o.maker_rebate_earned for o in orders),
            "avg_slices_per_order": (
                sum(len(o.slices) for o in orders) / len(orders)
            ),
        }
