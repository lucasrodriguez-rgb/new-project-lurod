from __future__ import annotations

import datetime as dt

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.config import settings
from polymarket_index.db.models import async_session
from polymarket_index.db import queries
from polymarket_index.signals.detector import RawSignal


class ValidationResult:
    __slots__ = ("is_valid", "reason")

    def __init__(self, is_valid: bool, reason: str = ""):
        self.is_valid = is_valid
        self.reason = reason

    def __repr__(self) -> str:
        status = "PASS" if self.is_valid else f"REJECT({self.reason})"
        return f"ValidationResult({status})"


class SignalValidator:
    """
    Filters raw signals through a series of validation checks.
    Only signals passing ALL checks are considered valid for copy-trading.
    """

    def __init__(self, api_client: PolymarketClient) -> None:
        self._api = api_client

    async def validate(self, signal: RawSignal) -> ValidationResult:
        """
        Run all validation checks on a raw signal.
        Returns immediately on the first failure.
        """
        checks = [
            ("signal_age", self._check_signal_age),
            ("market_status", self._check_market_status),
            ("liquidity", self._check_liquidity),
            ("time_to_close", self._check_time_to_close),
            ("price_staleness", self._check_price_staleness),
            ("crowding", self._check_crowding),
            ("wallet_tenure", self._check_wallet_tenure),
        ]

        for name, check_fn in checks:
            try:
                result = await check_fn(signal)
                if not result.is_valid:
                    logger.debug(
                        "Signal from {} on {} rejected by {}: {}",
                        signal.wallet_address[:10],
                        signal.market_id[:12],
                        name,
                        result.reason,
                    )
                    return result
            except Exception as exc:
                reason = f"check '{name}' raised: {exc}"
                logger.warning("Validation error: {}", reason)
                return ValidationResult(False, reason)

        logger.info(
            "Signal VALID: wallet={} market={} side={} ${:.0f}",
            signal.wallet_address[:10],
            signal.market_id[:12],
            signal.side,
            signal.size_usdc,
        )
        return ValidationResult(True)

    async def _check_signal_age(self, signal: RawSignal) -> ValidationResult:
        """Reject if signal is more than max_signal_age_seconds old."""
        age = (dt.datetime.now(dt.timezone.utc) - signal.timestamp).total_seconds()
        if age > settings.max_signal_age_seconds:
            return ValidationResult(
                False,
                f"signal too old ({age:.0f}s > {settings.max_signal_age_seconds}s)",
            )
        return ValidationResult(True)

    async def _check_market_status(self, signal: RawSignal) -> ValidationResult:
        """Reject if market is closed, paused, or not found."""
        market = await self._api.get_market_by_id(signal.market_id)
        if market is None:
            return ValidationResult(False, "market not found")
        if market.closed:
            return ValidationResult(False, "market is closed")
        if not market.active:
            return ValidationResult(False, "market is paused/inactive")
        return ValidationResult(True)

    async def _check_liquidity(self, signal: RawSignal) -> ValidationResult:
        """Reject if market liquidity is below threshold."""
        market = await self._api.get_market_by_id(signal.market_id)
        if market is None:
            return ValidationResult(False, "market not found for liquidity check")
        if market.liquidity < settings.min_market_liquidity:
            return ValidationResult(
                False,
                f"low liquidity (${market.liquidity:,.0f} < ${settings.min_market_liquidity:,.0f})",
            )
        return ValidationResult(True)

    async def _check_time_to_close(self, signal: RawSignal) -> ValidationResult:
        """Reject if market resolves within min_hours_to_close."""
        market = await self._api.get_market_by_id(signal.market_id)
        if market is None:
            return ValidationResult(False, "market not found for close-time check")
        if not market.close_time:
            return ValidationResult(True)

        try:
            close_dt = dt.datetime.fromisoformat(
                market.close_time.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            return ValidationResult(True)

        hours_left = (close_dt - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        if hours_left < settings.min_hours_to_close:
            return ValidationResult(
                False,
                f"market closes too soon ({hours_left:.1f}h < {settings.min_hours_to_close}h)",
            )
        return ValidationResult(True)

    async def _check_price_staleness(self, signal: RawSignal) -> ValidationResult:
        """Reject if signal price deviates too far from current order book mid."""
        try:
            order_book = await self._api.get_order_book(signal.market_id)
        except Exception:
            return ValidationResult(True)

        if order_book.mid_price <= 0:
            return ValidationResult(True)

        deviation = abs(signal.price - order_book.mid_price) / order_book.mid_price
        if deviation > settings.max_price_staleness_pct:
            return ValidationResult(
                False,
                f"price stale (deviation {deviation:.1%} > {settings.max_price_staleness_pct:.1%})",
            )
        return ValidationResult(True)

    async def _check_crowding(self, signal: RawSignal) -> ValidationResult:
        """Reject if too many top wallets already hold the same position."""
        async with async_session() as session:
            indexed = await queries.get_indexed_wallets(session)
            indexed_addrs = [w.address for w in indexed]

            count = await queries.count_wallets_with_position(
                session, signal.market_id, signal.side, indexed_addrs
            )

        if count >= settings.max_crowding_wallets:
            return ValidationResult(
                False,
                f"crowded ({count} top wallets already hold {signal.side} on this market)",
            )
        return ValidationResult(True)

    async def _check_wallet_tenure(self, signal: RawSignal) -> ValidationResult:
        """Reject if wallet has been in the index for less than min_hours_in_index."""
        async with async_session() as session:
            wallet = await queries.get_wallet(session, signal.wallet_address)

        if wallet is None:
            return ValidationResult(False, "wallet not found")

        if not wallet.in_index:
            return ValidationResult(False, "wallet not in index")

        if wallet.index_entered_at is None:
            return ValidationResult(False, "wallet index entry time unknown")

        entered = wallet.index_entered_at
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=dt.timezone.utc)

        hours_in = (dt.datetime.now(dt.timezone.utc) - entered).total_seconds() / 3600
        if hours_in < settings.min_hours_in_index:
            return ValidationResult(
                False,
                f"wallet too new in index ({hours_in:.1f}h < {settings.min_hours_in_index}h)",
            )
        return ValidationResult(True)
