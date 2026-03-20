"""
EV-based edge trader CLI.

Usage:
    # Dry-run mode (default) — scans for edge, simulates trades
    python3 -m polymarket_index.edge

    # Live mode — places real orders via CLOB API
    python3 -m polymarket_index.edge --live

    # Customize
    python3 -m polymarket_index.edge --capital 50000 --ev 0.05 --poll 15 --duration 48
"""
import argparse
import asyncio
import signal
import sys

from loguru import logger

from polymarket_index.edge.ev_trader import EVTrader
from polymarket_index.bot.telegram_bot import create_alert_callback


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        level="INFO",
    )
    logger.add(
        "logs/ev_trader_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="14 days",
        level="DEBUG",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket EV Edge Trader")
    parser.add_argument("--live", action="store_true", help="Enable live trading (real orders)")
    parser.add_argument("--capital", type=float, default=10_000, help="Starting capital (default: 10000)")
    parser.add_argument("--ev", type=float, default=0.03, help="Min EV threshold (default: 0.03 = 3%%)")
    parser.add_argument("--poll", type=int, default=10, help="Poll interval seconds (default: 10)")
    parser.add_argument("--duration", type=float, default=24, help="Run duration hours (default: 24)")
    args = parser.parse_args()

    configure_logging()

    if args.live:
        logger.warning("=" * 50)
        logger.warning("LIVE MODE — REAL MONEY WILL BE USED")
        logger.warning("=" * 50)

    telegram = create_alert_callback()

    trader = EVTrader(
        live=args.live,
        capital=args.capital,
        ev_threshold=args.ev,
        poll_interval=args.poll,
        duration_hours=args.duration,
        telegram_callback=telegram,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown():
        trader._running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        loop.run_until_complete(trader.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
