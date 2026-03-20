"""
EV-based trader — executes trades when expected value exceeds threshold.
Dry-run by default, --live flag for real order placement.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass

from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient
from polymarket_index.edge.market_scanner import MarketScanner, ActiveMarket
from polymarket_index.edge.price_feeds import PriceFeed
from polymarket_index.edge.calculator import EdgeCalculator, EdgeSignal
from polymarket_index.edge.trade_db import TradeDB, EdgeTradeRecord
from polymarket_index.config import settings


@dataclass
class PortfolioState:
    cash: float
    total_value: float
    open_positions: list[dict]
    realized_pnl: float = 0.0


class EVTrader:
    """
    Scans markets for edge, calculates EV, and executes trades
    when EV > threshold. Dry-run by default.
    """

    def __init__(
        self,
        live: bool = False,
        capital: float = 10_000.0,
        ev_threshold: float | None = None,
        poll_interval: int | None = None,
        duration_hours: float = 24.0,
        telegram_callback=None,
    ) -> None:
        self._live = live
        self._ev_threshold = ev_threshold or settings.min_ev_threshold
        self._poll_interval = poll_interval or settings.edge_poll_interval_seconds
        self._duration = duration_hours
        self._telegram = telegram_callback

        self._api = PolymarketClient()
        self._prices = PriceFeed()
        self._scanner = MarketScanner(self._api)
        self._calculator = EdgeCalculator(self._prices)
        self._db = TradeDB()

        self._portfolio = PortfolioState(
            cash=capital, total_value=capital, open_positions=[]
        )
        self._initial_capital = capital
        self._running = False
        self._cycle_count = 0
        self._start_time = 0.0
        self._signals_found = 0
        self._trades_executed = 0

    async def run(self) -> None:
        self._running = True
        self._start_time = time.monotonic()
        mode = "LIVE" if self._live else "DRY RUN"

        logger.info("=" * 70)
        logger.info("EV TRADER [{}]", mode)
        logger.info("Capital: ${:,.0f} | EV threshold: {:.0%} | Poll: {}s",
                     self._initial_capital, self._ev_threshold, self._poll_interval)
        logger.info("=" * 70)

        if self._telegram:
            await self._telegram(
                f"🚀 EV Trader started [{mode}]\n"
                f"Capital: ${self._initial_capital:,.0f}\n"
                f"EV threshold: {self._ev_threshold:.0%}\n"
                f"Duration: {self._duration}h"
            )

        try:
            end_time = self._start_time + self._duration * 3600
            while self._running and time.monotonic() < end_time:
                self._cycle_count += 1
                await self._scan_cycle()
                self._print_status()

                self._db.save_pnl_snapshot(
                    self._portfolio.total_value, self._portfolio.cash,
                    self._portfolio.realized_pnl,
                    self._portfolio.total_value - self._portfolio.cash - self._initial_capital,
                    len(self._portfolio.open_positions),
                    self._db.get_stats()["closed"], self._db.get_stats()["win_rate"],
                )

                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(self._poll_interval, remaining))

        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            self._print_final()
            await self._api.close()
            await self._prices.close()
            self._db.close()

    async def _scan_cycle(self) -> None:
        """One full scan cycle: find markets → calculate edge → trade."""
        try:
            markets = await self._scanner.scan(
                min_volume=settings.min_market_volume,
                min_liquidity=settings.min_order_book_depth,
                max_spread=0.20,
            )
        except Exception as exc:
            logger.warning("Market scan failed: {}", exc)
            return

        signals: list[EdgeSignal] = []
        sem = asyncio.Semaphore(5)

        async def _eval(m: ActiveMarket) -> None:
            async with sem:
                try:
                    sig = await self._calculator.evaluate_market(m)
                    if sig:
                        signals.append(sig)
                except Exception:
                    pass

        await asyncio.gather(*[_eval(m) for m in markets[:100]])

        signals.sort(key=lambda s: s.ev_pct, reverse=True)
        self._signals_found += len(signals)

        for sig in signals[:5]:
            await self._execute_signal(sig)

    async def _execute_signal(self, sig: EdgeSignal) -> None:
        """Execute a trade for a positive-EV signal."""
        size_frac = min(sig.size_suggestion, settings.base_trade_pct * 3)
        size_usdc = self._portfolio.cash * size_frac
        size_usdc = min(size_usdc, settings.max_position_per_market)
        size_usdc = max(size_usdc, 5.0)

        if size_usdc > self._portfolio.cash:
            return

        # Check if we already have a position on this market
        existing = [p for p in self._portfolio.open_positions if p["market_id"] == sig.market_id]
        if len(existing) >= 2:
            return

        mode = "live" if self._live else "dry_run"
        now = dt.datetime.now(dt.timezone.utc).isoformat()

        record = EdgeTradeRecord(
            id=None,
            timestamp=now,
            market_id=sig.market_id,
            market_question=sig.market_question[:100],
            side=sig.side,
            size_usdc=round(size_usdc, 2),
            entry_price=sig.market_price,
            exit_price=None,
            pnl=None,
            ev_pct=sig.ev_pct,
            edge=sig.edge,
            confidence=sig.confidence,
            status="open",
            mode=mode,
            reasoning=sig.reasoning,
        )

        trade_id = self._db.insert_trade(record)
        self._portfolio.cash -= size_usdc
        self._portfolio.open_positions.append({
            "trade_id": trade_id,
            "market_id": sig.market_id,
            "side": sig.side,
            "size_usdc": size_usdc,
            "entry_price": sig.market_price,
        })
        self._trades_executed += 1

        if self._live:
            try:
                await self._api.place_order(
                    token_id=sig.market_id,
                    side=sig.side,
                    size=size_usdc / sig.market_price,
                    price=sig.market_price,
                )
                logger.info("LIVE ORDER placed: {} {} ${:.2f} @ {:.3f} EV={:.1%}",
                            sig.side, sig.market_slug[:25], size_usdc, sig.market_price, sig.ev_pct)
            except Exception as exc:
                logger.error("Order failed: {}", exc)
                self._db.cancel_trade(trade_id)
                self._portfolio.cash += size_usdc
                self._portfolio.open_positions = [
                    p for p in self._portfolio.open_positions if p["trade_id"] != trade_id
                ]
                return
        else:
            logger.info(
                "[DRY] {} {} ${:.2f} @ {:.3f} | EV={:.1%} edge={:.3f} | {}",
                sig.side, sig.market_slug[:25], size_usdc, sig.market_price,
                sig.ev_pct, sig.edge, sig.confidence,
            )

        if self._telegram:
            await self._telegram(
                f"{'🔴 LIVE' if self._live else '📝 DRY'} TRADE\n"
                f"{sig.side} {sig.market_question[:50]}\n"
                f"${size_usdc:.2f} @ {sig.market_price:.3f}\n"
                f"EV: {sig.ev_pct:.1%} | Edge: {sig.edge:.3f} | {sig.confidence}\n"
                f"{sig.reasoning[:100]}"
            )

    def _print_status(self) -> None:
        elapsed = time.monotonic() - self._start_time
        elapsed_m = elapsed / 60
        stats = self._db.get_stats()

        os.system("clear" if os.name != "nt" else "cls")

        mode = "\033[91mLIVE\033[0m" if self._live else "\033[92mDRY RUN\033[0m"
        pnl = self._portfolio.total_value - self._initial_capital
        pnl_c = "\033[92m" if pnl >= 0 else "\033[91m"
        r = "\033[0m"

        print("=" * 70)
        print(f"  EV TRADER [{mode}]  |  "
              f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} UTC  |  "
              f"Cycle #{self._cycle_count}")
        print("=" * 70)
        print(f"  {pnl_c}P&L: ${pnl:+,.2f}{r}  |  "
              f"Value: ${self._portfolio.total_value:,.2f}  |  "
              f"Cash: ${self._portfolio.cash:,.2f}")
        print(f"  Trades: {stats['total_trades']} ({stats['open']} open, {stats['closed']} closed)  |  "
              f"Win Rate: {stats['win_rate']:.0f}% ({stats['wins']}W/{stats['losses']}L)")
        print(f"  Signals found: {self._signals_found}  |  "
              f"Executed: {self._trades_executed}  |  "
              f"Running: {elapsed_m:.0f}m")

        recent = self._db.get_recent_trades(10)
        if recent:
            print(f"\n  --- RECENT TRADES ---")
            for t in recent:
                ev_s = f"EV={t.ev_pct:.1%}"
                if t.status == "open":
                    print(f"  [OPEN] {t.side:3s} ${t.size_usdc:>7.2f} @ {t.entry_price:.3f}  "
                          f"{ev_s}  {t.confidence}  {t.market_question[:35]}")
                elif t.pnl is not None:
                    c = "\033[92m" if t.pnl > 0 else "\033[91m"
                    print(f"  {c}[{'WIN ' if t.pnl > 0 else 'LOSS'}]{r} {t.side:3s} "
                          f"${t.size_usdc:>7.2f}  {c}${t.pnl:>+8.2f}{r}  "
                          f"{ev_s}  {t.market_question[:30]}")

        prices = asyncio.get_event_loop().run_until_complete(self._prices.get_all_prices()) if False else {}
        print(f"\n  EV threshold: {self._ev_threshold:.0%}  |  "
              f"Mode: {'LIVE' if self._live else 'dry-run'}")
        print("=" * 70)

    def _print_final(self) -> None:
        stats = self._db.get_stats()
        elapsed_h = (time.monotonic() - self._start_time) / 3600

        print("\n" + "=" * 70)
        print("  EV TRADER — FINAL REPORT")
        print("=" * 70)
        print(f"  Duration:     {elapsed_h:.2f}h")
        print(f"  Total trades: {stats['total_trades']}")
        print(f"  Win rate:     {stats['win_rate']:.1f}%")
        print(f"  Total P&L:    ${stats['total_pnl']:+,.2f}")
        print(f"  Avg win:      ${stats['avg_win']:+,.2f}")
        print(f"  Avg loss:     ${stats['avg_loss']:+,.2f}")
        print(f"  Signals:      {self._signals_found}")
        print(f"  DB:           {self._db._path}")
        print("=" * 70)
