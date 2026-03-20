"""
CLI entry point for running backtests.

Usage:
    # Step 1: Collect data (only needs to run once, caches locally)
    python3 -m polymarket_index.backtest.runner collect

    # Step 2: Run backtest on cached data
    python3 -m polymarket_index.backtest.runner run

    # Or collect + run in one go:
    python3 -m polymarket_index.backtest.runner full

    # Use specific wallets:
    python3 -m polymarket_index.backtest.runner full --wallets 0xabc...,0xdef...

    # Customize parameters:
    python3 -m polymarket_index.backtest.runner run --capital 50000 --top-n 10 --lookback 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from polymarket_index.backtest.data_collector import DataCollector
from polymarket_index.backtest.engine import BacktestEngine, BacktestResult


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


async def cmd_collect(args: argparse.Namespace) -> None:
    """Fetch and cache historical data from Polymarket."""
    collector = DataCollector()

    try:
        wallets = args.wallets.split(",") if args.wallets else None
        result = await collector.collect_full_dataset(
            wallet_addresses=wallets,
            leaderboard_limit=args.leaderboard_limit,
            force_refresh=args.force_refresh,
        )
        print(f"\nData collected: {result['wallets']} wallets, {result['total_trades']} trades")
        print(f"Cached in: {Path('backtest_data').resolve()}")
    finally:
        await collector.close()


async def cmd_run(args: argparse.Namespace) -> None:
    """Run backtest on cached data."""
    data_dir = Path("backtest_data") / "wallets"

    if not data_dir.exists() or not list(data_dir.glob("*.json")):
        print("No cached data found. Run 'collect' first:")
        print("  python3 -m polymarket_index.backtest.runner collect")
        sys.exit(1)

    histories = {}
    for f in data_dir.glob("*.json"):
        data = json.loads(f.read_text())
        from polymarket_index.backtest.data_collector import WalletHistory
        histories[data["address"]] = WalletHistory(**data)

    print(f"Loaded {len(histories)} wallets from cache")

    engine = BacktestEngine(
        wallet_histories=histories,
        initial_capital=args.capital,
        scoring_lookback_days=args.lookback,
        top_n=args.top_n,
        base_trade_pct=args.trade_pct,
    )

    result = engine.run(start_date=args.start, end_date=args.end)
    _print_result(result)

    if args.output:
        output_path = Path(args.output)
        output_data = asdict(result)
        output_data.pop("daily_snapshots", None)
        output_path.write_text(json.dumps(output_data, indent=2, default=str))
        print(f"\nResults saved to: {output_path}")

    if args.csv:
        csv_path = Path(args.csv)
        _write_equity_csv(result, csv_path)
        print(f"Equity curve saved to: {csv_path}")


async def cmd_full(args: argparse.Namespace) -> None:
    """Collect data + run backtest in one step."""
    await cmd_collect(args)
    await cmd_run(args)


def _print_result(r: BacktestResult) -> None:
    """Print a formatted backtest report."""
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)

    print(f"\n  Period:          {r.start_date} → {r.end_date}")
    print(f"  Initial Capital: ${r.initial_capital:,.2f}")
    print(f"  Final Value:     ${r.final_value:,.2f}")

    print(f"\n  ┌─ Returns ─────────────────────────────")
    _pnl = r.final_value - r.initial_capital
    _sign = "+" if _pnl >= 0 else ""
    print(f"  │  Total Return:   {_sign}{r.total_return_pct:.2f}%  ({_sign}${_pnl:,.2f})")
    print(f"  │  Annualized:     {r.annualized_return_pct:+.2f}%")
    print(f"  │  Sharpe Ratio:   {r.sharpe_ratio:.2f}")
    print(f"  │  Max Drawdown:   -{r.max_drawdown_pct:.2f}%")

    print(f"  │")
    print(f"  ├─ Trading ─────────────────────────────")
    print(f"  │  Total Trades:   {r.total_copy_trades}")
    print(f"  │  Win Rate:       {r.win_rate:.1f}%")
    print(f"  │  Winners:        {r.winning_trades}")
    print(f"  │  Losers:         {r.losing_trades}")
    print(f"  │  Avg Win:        ${r.avg_win:,.2f}")
    print(f"  │  Avg Loss:       ${r.avg_loss:,.2f}")
    print(f"  │  Profit Factor:  {r.profit_factor:.2f}")

    print(f"  │")
    print(f"  ├─ Daily ───────────────────────────────")
    print(f"  │  Best Day:       ${r.best_day_pnl:+,.2f}")
    print(f"  │  Worst Day:      ${r.worst_day_pnl:+,.2f}")
    print(f"  │  Avg Daily P&L:  ${r.avg_daily_pnl:+,.2f}")

    if r.top_wallets_followed:
        print(f"  │")
        print(f"  └─ Top Wallets Followed ────────────────")
        for i, w in enumerate(r.top_wallets_followed[:10]):
            addr = w["address"][:10]
            print(
                f"     {i+1:2d}. {addr}...  score={w['score']:5.1f}  "
                f"roi={w['roi']:+.1%}  wr={w['win_rate']:.0%}  "
                f"trades={w['trade_count']}"
            )

    print("\n" + "=" * 60)


def _write_equity_csv(r: BacktestResult, path: Path) -> None:
    with open(path, "w") as f:
        f.write("date,portfolio_value,cash,open_positions,cumulative_pnl,daily_pnl\n")
        for s in r.daily_snapshots:
            f.write(
                f"{s.date},{s.portfolio_value:.2f},{s.cash:.2f},"
                f"{s.open_positions},{s.cumulative_pnl:.2f},{s.daily_pnl:.2f}\n"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Polymarket Index Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m polymarket_index.backtest.runner collect
  python3 -m polymarket_index.backtest.runner run --capital 50000 --top-n 10
  python3 -m polymarket_index.backtest.runner full --wallets 0xabc...,0xdef...
        """,
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # ── collect ──
    p_collect = sub.add_parser("collect", help="Fetch and cache historical data")
    p_collect.add_argument("--wallets", type=str, default="", help="Comma-separated wallet addresses")
    p_collect.add_argument("--leaderboard-limit", type=int, default=50, help="Number of leaderboard wallets to fetch")
    p_collect.add_argument("--force-refresh", action="store_true", help="Re-fetch even if cached")

    # ── run ──
    p_run = sub.add_parser("run", help="Run backtest on cached data")
    p_run.add_argument("--capital", type=float, default=10000, help="Starting capital in USDC")
    p_run.add_argument("--top-n", type=int, default=20, help="Number of top wallets to follow")
    p_run.add_argument("--lookback", type=int, default=30, help="Scoring lookback window in days")
    p_run.add_argument("--trade-pct", type=float, default=0.01, help="Base trade size as fraction of portfolio")
    p_run.add_argument("--start", type=str, default=None, help="Backtest start date (YYYY-MM-DD)")
    p_run.add_argument("--end", type=str, default=None, help="Backtest end date (YYYY-MM-DD)")
    p_run.add_argument("--output", type=str, default=None, help="Save results JSON to this path")
    p_run.add_argument("--csv", type=str, default=None, help="Save equity curve CSV to this path")

    # ── full ──
    p_full = sub.add_parser("full", help="Collect data + run backtest")
    p_full.add_argument("--wallets", type=str, default="", help="Comma-separated wallet addresses")
    p_full.add_argument("--leaderboard-limit", type=int, default=50)
    p_full.add_argument("--force-refresh", action="store_true")
    p_full.add_argument("--capital", type=float, default=10000)
    p_full.add_argument("--top-n", type=int, default=20)
    p_full.add_argument("--lookback", type=int, default=30)
    p_full.add_argument("--trade-pct", type=float, default=0.01)
    p_full.add_argument("--start", type=str, default=None)
    p_full.add_argument("--end", type=str, default=None)
    p_full.add_argument("--output", type=str, default=None)
    p_full.add_argument("--csv", type=str, default=None)

    return parser


def main() -> None:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "collect": cmd_collect,
        "run": cmd_run,
        "full": cmd_full,
    }

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
