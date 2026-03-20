"""
Live paper trading CLI.

Usage:
    # Run for 24 hours tracking top 500 wallets
    python3 -m polymarket_index.live --wallets 500 --duration 24

    # Aggressive mode: bigger positions, faster polling
    python3 -m polymarket_index.live --wallets 500 --capital 50000 --trade-pct 0.02 --poll 15

    # Resume a previous session
    python3 -m polymarket_index.live --resume

    # Quick test: 100 wallets, 1 hour
    python3 -m polymarket_index.live --wallets 100 --duration 1
"""
import argparse
import asyncio
import signal
import sys

from loguru import logger

from polymarket_index.live.paper_trader import LivePaperTrader


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
        "logs/live_paper_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket Live Paper Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m polymarket_index.live --wallets 500 --duration 24
  python3 -m polymarket_index.live --wallets 100 --capital 50000 --trade-pct 0.02
  python3 -m polymarket_index.live --resume
        """,
    )

    parser.add_argument("--wallets", type=int, default=500, help="Number of leaderboard wallets to track (default: 500)")
    parser.add_argument("--top-n", type=int, default=100, help="Copy trades from top N scored wallets (default: 100)")
    parser.add_argument("--capital", type=float, default=10_000, help="Starting virtual capital in USDC (default: 10000)")
    parser.add_argument("--poll", type=int, default=15, help="Poll interval in seconds (default: 15)")
    parser.add_argument("--duration", type=float, default=24, help="Run duration in hours (default: 24)")
    parser.add_argument("--trade-pct", type=float, default=0.01, help="Base trade size as fraction of portfolio (default: 0.01)")
    parser.add_argument("--min-price", type=float, default=0.03, help="Min price to copy (default: 0.03)")
    parser.add_argument("--max-price", type=float, default=0.98, help="Max price to copy (default: 0.98)")
    parser.add_argument("--min-score", type=float, default=30.0, help="Min wallet score to consider (default: 30)")
    parser.add_argument("--resume", action="store_true", help="Resume from previous snapshot")

    args = parser.parse_args()
    configure_logging()

    trader = LivePaperTrader(
        num_wallets=args.wallets,
        top_n_copy=args.top_n,
        initial_capital=args.capital,
        poll_interval=args.poll,
        duration_hours=args.duration,
        base_trade_pct=args.trade_pct,
        min_price=args.min_price,
        max_price=args.max_price,
        min_wallet_score=args.min_score,
        resume=args.resume,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown() -> None:
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
