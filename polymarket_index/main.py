from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from polymarket_index.config import settings
from polymarket_index.db.models import init_db
from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.api.onchain import PolygonReader
from polymarket_index.tracker.wallet_scanner import WalletScanner
from polymarket_index.tracker.performance import PerformanceScorer
from polymarket_index.tracker.leaderboard import Leaderboard
from polymarket_index.signals.detector import SignalDetector
from polymarket_index.signals.validator import SignalValidator
from polymarket_index.executor.trade_builder import TradeBuilder
from polymarket_index.executor.order_manager import OrderManager


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        level="INFO",
    )
    logger.add(
        "logs/polymarket_index_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    )


class Orchestrator:
    """
    Main event loop that coordinates all subsystems:
    1. Startup: init DB, seed wallets, run initial scoring
    2. Every hour: rescore wallets, update leaderboard, snapshot
    3. Every 60s: poll → detect → validate → build → queue orders
    """

    def __init__(self, dry_run: bool = True) -> None:
        self._running = False
        self._dry_run = dry_run

        self._api = PolymarketClient()
        self._onchain = PolygonReader()
        self._scanner = WalletScanner(self._api, self._onchain)
        self._scorer = PerformanceScorer()
        self._leaderboard = Leaderboard(self._scorer)
        self._detector = SignalDetector(self._api)
        self._validator = SignalValidator(self._api)
        self._builder = TradeBuilder(self._api)
        self._orders = OrderManager(self._api, dry_run=dry_run)

    async def start(self) -> None:
        """Initialize all subsystems and start the main loop."""
        logger.info("=" * 60)
        logger.info("Polymarket Index — Starting up (dry_run={})", self._dry_run)
        logger.info("=" * 60)

        await init_db()
        logger.info("Database initialized")

        await self._startup_sequence()

        self._running = True
        logger.info("Main loop starting — poll every {}s, rescore every {}s",
                     settings.poll_interval_seconds,
                     settings.rescore_interval_seconds)

        poll_task = asyncio.create_task(self._poll_loop())
        rescore_task = asyncio.create_task(self._rescore_loop())
        order_mgmt_task = asyncio.create_task(self._order_management_loop())

        try:
            await asyncio.gather(poll_task, rescore_task, order_mgmt_task)
        except asyncio.CancelledError:
            logger.info("Main loop cancelled")
        finally:
            await self.shutdown()

    async def _startup_sequence(self) -> None:
        """Run one-time startup tasks."""
        logger.info("Seeding wallets...")
        wallets = await self._scanner.seed_wallets()
        logger.info("Seeded {} wallets", len(wallets))

        if wallets:
            logger.info("Ingesting initial trade history...")
            await self._scanner.ingest_all_wallets()

            logger.info("Running initial scoring...")
            await self._leaderboard.update()
            await self._leaderboard.snapshot()

        await self._detector.initialize()
        logger.info("Startup complete")

    async def _poll_loop(self) -> None:
        """Poll for new signals every poll_interval_seconds."""
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.error("Poll cycle error: {}", exc)

            await asyncio.sleep(settings.poll_interval_seconds)

    async def _poll_cycle(self) -> None:
        """Single poll cycle: detect → validate → build → submit."""
        raw_signals = await self._detector.poll_for_signals()
        if not raw_signals:
            return

        valid_count = 0
        for raw in raw_signals:
            signal_db = await self._detector.persist_signal(raw)
            result = await self._validator.validate(raw)

            from polymarket_index.db.models import async_session
            from polymarket_index.db import queries as q

            async with async_session() as session:
                async with session.begin():
                    await q.update_signal_validity(
                        session,
                        signal_db.id,
                        result.is_valid,
                        result.reason if not result.is_valid else None,
                    )

            if not result.is_valid:
                continue

            valid_count += 1
            order = await self._builder.build_order(raw, signal_db_id=signal_db.id)
            if order:
                await self._orders.submit_order(order)

        if valid_count:
            logger.info(
                "Poll cycle: {} raw signals → {} valid → orders submitted",
                len(raw_signals),
                valid_count,
            )

    async def _rescore_loop(self) -> None:
        """Re-score wallets and update leaderboard periodically."""
        while self._running:
            await asyncio.sleep(settings.rescore_interval_seconds)
            try:
                logger.info("Starting rescore cycle...")

                await self._scanner.discover_new_wallets_from_recent_trades()
                await self._scanner.ingest_all_wallets()

                events = await self._leaderboard.update()
                await self._leaderboard.flag_inactive_wallets()
                await self._leaderboard.snapshot()

                from polymarket_index.db.models import async_session
                from polymarket_index.db import queries as q
                async with async_session() as session:
                    indexed = await q.get_indexed_wallets(session)
                    addrs = [w.address for w in indexed]

                self._detector.refresh_tracked_wallets(addrs)

                logger.info(
                    "Rescore complete: {} events, {} wallets in index",
                    len(events),
                    len(addrs),
                )
            except Exception as exc:
                logger.error("Rescore cycle error: {}", exc)

    async def _order_management_loop(self) -> None:
        """Periodically check and manage open orders."""
        while self._running:
            await asyncio.sleep(settings.poll_interval_seconds * 5)
            try:
                await self._orders.cancel_stale_orders(max_age_minutes=30)
                summary = await self._orders.get_portfolio_summary()
                if summary:
                    logger.debug("Portfolio: {}", summary)
            except Exception as exc:
                logger.error("Order management error: {}", exc)

    async def shutdown(self) -> None:
        """Clean shutdown."""
        self._running = False
        logger.info("Shutting down...")
        await self._api.close()
        logger.info("Shutdown complete")


def main() -> None:
    configure_logging()

    dry_run = "--live" not in sys.argv
    if dry_run:
        logger.info("Running in DRY RUN mode (use --live to place real orders)")

    orchestrator = Orchestrator(dry_run=dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(orchestrator.shutdown()))

    try:
        loop.run_until_complete(orchestrator.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        loop.run_until_complete(orchestrator.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
