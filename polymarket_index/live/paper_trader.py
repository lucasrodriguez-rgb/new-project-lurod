"""
Live paper trading engine.

Runs continuously, polling top wallets for new trades every 30s,
scoring wallets on the fly, and simulating copy-trades in a virtual portfolio.
Prints a live dashboard to the terminal.

Usage:
    python3 -m polymarket_index.live.paper_trader \
        --wallets 500 --capital 10000 --poll 30 --duration 24
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from polymarket_index.api.polymarket import PolymarketClient, TradeRecord
from polymarket_index.live.paper_portfolio import (
    PaperPortfolio,
    PaperPosition,
    TradeSignal,
)

SNAPSHOT_PATH = Path("live_paper_trading.json")


class LivePaperTrader:
    """
    Continuously polls Polymarket for new trades from top wallets
    and simulates a copy-trading portfolio in real time.
    """

    def __init__(
        self,
        num_wallets: int = 500,
        top_n_copy: int = 100,
        initial_capital: float = 10_000.0,
        poll_interval: int = 15,
        duration_hours: float = 24.0,
        base_trade_pct: float = 0.01,
        min_price: float = 0.03,
        max_price: float = 0.98,
        min_wallet_score: float = 30.0,
        resume: bool = False,
    ) -> None:
        self._api = PolymarketClient()
        self._num_wallets = num_wallets
        self._top_n = top_n_copy
        self._poll_interval = poll_interval
        self._duration = duration_hours
        self._base_trade_pct = base_trade_pct
        self._min_price = min_price
        self._max_price = max_price
        self._min_score = min_wallet_score

        if resume and SNAPSHOT_PATH.exists():
            self._portfolio = PaperPortfolio.load_snapshot(SNAPSHOT_PATH)
            logger.info("Resumed portfolio: ${:.2f} value", self._portfolio.total_value)
        else:
            self._portfolio = PaperPortfolio(initial_capital=initial_capital, cash=initial_capital)

        self._wallet_addresses: list[str] = []
        self._wallet_trades: dict[str, list[dict]] = {}
        self._wallet_scores: dict[str, dict] = {}
        self._seen_tx_hashes: set[str] = set()
        self._market_cache: dict[str, dict] = {}
        self._cycle_count = 0
        self._start_time = time.monotonic()
        self._running = False

    async def run(self) -> None:
        self._running = True
        self._start_time = time.monotonic()

        logger.info("=" * 70)
        logger.info("LIVE PAPER TRADER")
        logger.info("Capital: ${:,.0f} | Wallets: {} | Top-N: {} | Poll: {}s | Duration: {}h",
                     self._portfolio.initial_capital, self._num_wallets,
                     self._top_n, self._poll_interval, self._duration)
        logger.info("=" * 70)

        try:
            await self._initialize()

            end_time = self._start_time + self._duration * 3600
            while self._running and time.monotonic() < end_time:
                self._cycle_count += 1
                await self._poll_cycle()
                self._print_dashboard()
                self._portfolio.save_snapshot(SNAPSHOT_PATH)

                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(self._poll_interval, remaining))

        except asyncio.CancelledError:
            logger.info("Cancelled")
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self._print_final_report()
            self._portfolio.save_snapshot(SNAPSHOT_PATH)
            await self._api.close()

    async def _initialize(self) -> None:
        logger.info("Discovering active wallets from multiple leaderboard categories...")

        all_addresses: set[str] = set()
        for period in ("DAY", "WEEK", "MONTH", "ALL"):
            for order_by in ("PNL", "VOL"):
                try:
                    batch = await self._api.scrape_leaderboard(
                        limit=min(self._num_wallets, 50),
                        time_period=period,
                        order_by=order_by,
                    )
                    all_addresses.update(batch)
                    logger.info("  {}/{}: {} wallets", period, order_by, len(batch))
                except Exception as exc:
                    logger.debug("Leaderboard {}/{} failed: {}", period, order_by, exc)

        if len(all_addresses) < self._num_wallets:
            try:
                extra = await self._api.scrape_leaderboard(
                    limit=self._num_wallets, order_by="PNL"
                )
                all_addresses.update(extra)
            except Exception:
                pass

        self._wallet_addresses = list(all_addresses)
        logger.info("Total unique wallets discovered: {}", len(self._wallet_addresses))

        if not self._wallet_addresses:
            raise RuntimeError("No wallets found on leaderboard")

        logger.info("Loading recent trades for scoring (keeping seen window small)...")
        sem = asyncio.Semaphore(8)
        loaded = 0

        async def _load(addr: str) -> None:
            nonlocal loaded
            async with sem:
                try:
                    trades = await self._api.get_trades(addr, limit=100, offset=0)
                    self._wallet_trades[addr] = [self._trade_to_dict(t) for t in trades]
                    # Only mark the LAST 3 trades as seen — everything else can trigger copies
                    for t in trades[:3]:
                        if t.id:
                            self._seen_tx_hashes.add(t.id)
                    loaded += 1
                    if loaded % 50 == 0:
                        logger.info("  loaded {}/{} wallets...", loaded, len(self._wallet_addresses))
                except Exception as exc:
                    logger.debug("Failed to load {}: {}", addr[:10], exc)

        await asyncio.gather(*[_load(a) for a in self._wallet_addresses])
        logger.info("Loaded {} wallets ({} seen hashes — kept small for more signals)",
                     len(self._wallet_trades), len(self._seen_tx_hashes))

        self._score_all_wallets()
        top = sorted(self._wallet_scores.values(), key=lambda w: w["score"], reverse=True)[:10]
        logger.info("Top 10 scored wallets:")
        for i, w in enumerate(top):
            logger.info("  {:2d}. {}  score={:.1f}  roi={:+.1%}  wr={:.0%}  trades={}",
                         i + 1, w["address"][:12], w["score"],
                         w["roi"], w["win_rate"], w["trade_count"])

        logger.info("Initialization complete — polling ALL {} wallets every {}s",
                     len(self._wallet_addresses), self._poll_interval)

    async def _poll_cycle(self) -> None:
        all_scored = sorted(
            self._wallet_scores.values(),
            key=lambda w: w["score"],
            reverse=True,
        )
        rank_map = {w["address"]: i + 1 for i, w in enumerate(all_scored)}

        # Poll ALL tracked wallets, not just top N
        poll_addrs = self._wallet_addresses

        sem = asyncio.Semaphore(10)
        new_trades: list[tuple[str, TradeRecord, int]] = []

        async def _check(addr: str) -> None:
            async with sem:
                try:
                    recent = await self._api.get_trades(addr, limit=5, offset=0)
                    for t in recent:
                        if t.id and t.id not in self._seen_tx_hashes:
                            self._seen_tx_hashes.add(t.id)
                            rank = rank_map.get(addr, len(all_scored))
                            new_trades.append((addr, t, rank))

                            if addr in self._wallet_trades:
                                self._wallet_trades[addr].insert(0, self._trade_to_dict(t))
                except Exception:
                    pass

        await asyncio.gather(*[_check(a) for a in poll_addrs])

        for wallet_addr, trade, rank in new_trades:
            await self._process_signal(wallet_addr, trade, rank)

        await self._update_open_positions()

        if self._cycle_count % 60 == 0:
            self._score_all_wallets()

    async def _process_signal(
        self, wallet: str, trade: TradeRecord, rank: int
    ) -> None:
        now_str = dt.datetime.now(dt.timezone.utc).isoformat()
        title = trade.market_slug or trade.market_id[:20]

        size_usdc = trade.size if trade.size > 0 else 0

        skip_reason = self._validate_signal(trade, rank)

        signal = TradeSignal(
            timestamp=now_str,
            wallet=wallet[:12],
            wallet_rank=rank,
            market_id=trade.market_id,
            market_title=title,
            side=trade.side,
            size_usdc=round(size_usdc, 2),
            price=trade.price,
            action="SKIP" if skip_reason else "COPY",
            skip_reason=skip_reason,
        )
        self._portfolio.log_signal(signal)

        if skip_reason:
            return

        trade_pct = self._base_trade_pct
        if rank <= 5:
            trade_pct *= 3.0
        elif rank <= 15:
            trade_pct *= 2.0
        elif rank <= 30:
            trade_pct *= 1.5

        copy_size = self._portfolio.total_value * trade_pct
        copy_size = min(copy_size, self._portfolio.cash * 0.15)
        copy_size = max(copy_size, 2.0)

        pos = self._portfolio.open_position(
            market_id=trade.market_id,
            market_title=title,
            side=trade.side,
            price=trade.price,
            size_usdc=copy_size,
            source_wallet=wallet[:12],
            source_rank=rank,
        )

        if pos:
            logger.info(
                "COPY #{} | rank={:2d} {} {} ${:.2f} @ {:.3f} | {}",
                pos.id, rank, trade.side, pos.market_title[:30],
                copy_size, trade.price, wallet[:10],
            )

    def _validate_signal(self, trade: TradeRecord, rank: int) -> str:
        if trade.price <= 0.02:
            return f"price too low ({trade.price:.3f})"
        if trade.price >= 0.99:
            return f"price too high ({trade.price:.3f})"

        same_market_same_side = sum(
            1 for p in self._portfolio.open_positions
            if p.market_id == trade.market_id and p.side == trade.side
        )
        if same_market_same_side >= 5:
            return f"already have {same_market_same_side} same-side positions"

        if len(self._portfolio.open_positions) >= 200:
            return "max open positions (200)"

        if self._portfolio.cash < 2.0:
            return "insufficient cash"

        return ""

    async def _update_open_positions(self) -> None:
        """Check if any markets have resolved and close positions."""
        if not self._portfolio.open_positions:
            return

        market_ids = list({p.market_id for p in self._portfolio.open_positions})

        for mid in market_ids[:3]:
            try:
                market = await self._api.get_market_by_id(mid)
                if market is None:
                    continue

                if market.closed and market.outcome_prices:
                    yes_price = market.outcome_prices.get("Yes", market.outcome_prices.get("yes", 0))
                    no_price = market.outcome_prices.get("No", market.outcome_prices.get("no", 0))

                    for pos in list(self._portfolio.open_positions):
                        if pos.market_id != mid:
                            continue

                        exit_price = None
                        if yes_price > 0.9:
                            exit_price = 1.0 if pos.side.upper() in ("YES", "BUY") else 0.0
                        elif no_price > 0.9:
                            exit_price = 0.0 if pos.side.upper() in ("YES", "BUY") else 1.0
                        else:
                            for _, price in market.outcome_prices.items():
                                if price > 0.9:
                                    exit_price = 1.0 if pos.side.upper() in ("YES", "BUY") else 0.0
                                    break

                        if exit_price is not None:
                            closed = self._portfolio.close_position(pos.id, exit_price)
                            if closed:
                                icon = "W" if closed.won else "L"
                                logger.info(
                                    "RESOLVED [{}] {} | {} ${:.2f} -> ${:+.2f} ({:+.1f}%)",
                                    icon, closed.market_title[:30], closed.side,
                                    closed.size_usdc, closed.pnl, closed.pnl_pct,
                                )
                else:
                    yes_p = market.outcome_prices.get("Yes", market.outcome_prices.get("yes", 0))
                    if yes_p > 0:
                        for pos in self._portfolio.open_positions:
                            if pos.market_id == mid:
                                p = yes_p if pos.side.upper() in ("YES", "BUY") else (1.0 - yes_p)
                                self._portfolio.update_position_price(mid, p)
            except Exception:
                pass

    def _score_all_wallets(self) -> None:
        self._wallet_scores.clear()
        for addr, trades in self._wallet_trades.items():
            if len(trades) < 5:
                continue
            score_data = self._score_wallet(addr, trades)
            if score_data:
                self._wallet_scores[addr] = score_data

    def _score_wallet(self, addr: str, trades: list[dict]) -> dict | None:
        total_capital = 0.0
        total_pnl = 0.0
        wins = 0
        resolved = 0
        markets: set[str] = set()
        daily_pnl: dict[str, float] = {}

        for t in trades:
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            if size <= 0 or price <= 0:
                continue

            capital = size
            total_capital += capital
            markets.add(t.get("market_id", ""))

            side = t.get("side", "").upper()
            outcome = t.get("outcome")
            if not outcome:
                continue

            outcome_upper = outcome.upper().strip()
            won = None
            if side in ("YES", "BUY"):
                if outcome_upper == "YES":
                    won = True
                elif outcome_upper == "NO":
                    won = False
            elif side in ("NO", "SELL"):
                if outcome_upper == "NO":
                    won = True
                elif outcome_upper == "YES":
                    won = False

            if won is None:
                continue

            resolved += 1
            ts_str = t.get("timestamp", "")[:10]
            if won:
                pnl = (capital / price - capital) if price > 0 else 0
                total_pnl += pnl
                wins += 1
                daily_pnl[ts_str] = daily_pnl.get(ts_str, 0) + pnl
            else:
                total_pnl -= capital
                daily_pnl[ts_str] = daily_pnl.get(ts_str, 0) - capital

        if total_capital <= 0 or resolved < 3:
            return None

        roi = total_pnl / total_capital
        win_rate = wins / resolved if resolved > 0 else 0

        if len(daily_pnl) >= 2:
            vals = list(daily_pnl.values())
            mean_r = float(np.mean(vals))
            std_r = float(np.std(vals, ddof=1))
            sharpe = (mean_r / std_r * math.sqrt(365)) if std_r > 0 else 0
        else:
            sharpe = 0

        n_markets = len(markets)
        n_trades = len(trades)
        if n_trades > 0 and n_markets > 0:
            counts: dict[str, int] = {}
            for t in trades:
                m = t.get("market_id", "")
                counts[m] = counts.get(m, 0) + 1
            hhi = sum((c / n_trades) ** 2 for c in counts.values())
            diversity = 1.0 - hhi
        else:
            diversity = 0

        def _norm(v: float, lo: float, hi: float) -> float:
            if hi <= lo:
                return 50.0
            return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))

        score = (
            0.35 * _norm(roi, -0.5, 2.0)
            + 0.25 * _norm(sharpe, -1.0, 5.0)
            + 0.20 * _norm(win_rate, 0.3, 0.85)
            + 0.10 * _norm(math.log10(max(total_capital, 1)), 2.0, 6.0)
            + 0.10 * _norm(diversity, 0.0, 0.9)
        )

        return {
            "address": addr,
            "score": round(score, 2),
            "roi": round(roi, 4),
            "win_rate": round(win_rate, 4),
            "sharpe": round(sharpe, 2),
            "volume": round(total_capital, 2),
            "diversity": round(diversity, 3),
            "trade_count": n_trades,
            "resolved": resolved,
        }

    def _print_dashboard(self) -> None:
        p = self._portfolio
        elapsed = time.monotonic() - self._start_time
        elapsed_h = elapsed / 3600
        remaining_h = max(0, self._duration - elapsed_h)

        os.system("clear" if os.name != "nt" else "cls")

        pnl_sign = "+" if p.total_pnl >= 0 else ""
        rpnl_sign = "+" if p.realized_pnl >= 0 else ""

        print("=" * 70)
        print("  POLYMARKET LIVE PAPER TRADER")
        print("=" * 70)
        print(f"  Started: {p.started_at[:19]}Z")
        print(f"  Running: {elapsed_h:.1f}h / {self._duration:.0f}h ({remaining_h:.1f}h left)")
        print(f"  Cycle:   #{self._cycle_count} (every {self._poll_interval}s)")
        print(f"  Wallets: {len(self._wallet_scores)} scored / {len(self._wallet_addresses)} tracked")
        print()
        print(f"  {'='*50}")
        print(f"  PORTFOLIO")
        print(f"  {'='*50}")
        print(f"  Initial:      ${p.initial_capital:>12,.2f}")
        print(f"  Current:      ${p.total_value:>12,.2f}  ({pnl_sign}{p.total_pnl_pct:.2f}%)")
        print(f"  Cash:         ${p.cash:>12,.2f}")
        print(f"  Unrealized:   ${p.total_pnl - p.realized_pnl:>12,.2f}")
        print(f"  Realized:     ${rpnl_sign}{p.realized_pnl:>11,.2f}")
        print()
        print(f"  {'='*50}")
        print(f"  TRADING STATS")
        print(f"  {'='*50}")
        print(f"  Open Positions: {len(p.open_positions)}")
        print(f"  Closed Trades:  {len(p.closed_positions)}")
        print(f"  Win Rate:       {p.win_rate:.1f}% ({p.win_count}W / {p.loss_count}L)")
        print(f"  Profit Factor:  {p.profit_factor:.2f}")
        print(f"  Signals Seen:   {len(p.signals_log)}")
        copied = sum(1 for s in p.signals_log if s.action == "COPY")
        skipped = sum(1 for s in p.signals_log if s.action == "SKIP")
        print(f"  Copied/Skipped: {copied} / {skipped}")

        if p.open_positions:
            print()
            print(f"  {'='*50}")
            print(f"  OPEN POSITIONS (top 10)")
            print(f"  {'='*50}")
            sorted_pos = sorted(p.open_positions, key=lambda x: abs(x.unrealized_pnl), reverse=True)
            for pos in sorted_pos[:10]:
                upnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
                print(
                    f"  {pos.side:3s} ${pos.size_usdc:>7.2f} @ {pos.entry_price:.3f} "
                    f"→ {upnl_sign}${pos.unrealized_pnl:.2f}  "
                    f"| {pos.market_title[:35]}"
                )

        recent_closed = p.closed_positions[-5:]
        if recent_closed:
            print()
            print(f"  {'='*50}")
            print(f"  RECENT TRADES")
            print(f"  {'='*50}")
            for c in reversed(recent_closed):
                icon = "W" if c.won else "L"
                print(
                    f"  [{icon}] {c.side:3s} ${c.size_usdc:>7.2f} "
                    f"→ ${c.pnl:+.2f} ({c.pnl_pct:+.1f}%) "
                    f"| {c.market_title[:30]}"
                )

        recent_signals = [s for s in p.signals_log[-10:] if s.action == "COPY"]
        if recent_signals:
            print()
            print(f"  {'='*50}")
            print(f"  RECENT SIGNALS")
            print(f"  {'='*50}")
            for s in reversed(recent_signals[-5:]):
                print(
                    f"  [{s.action}] rank={s.wallet_rank:2d} {s.side:3s} "
                    f"@ {s.price:.3f} ${s.size_usdc:.0f} "
                    f"| {s.market_title[:30]}"
                )

        print()
        print(f"  Snapshot: {SNAPSHOT_PATH}")
        print("=" * 70)

    def _print_final_report(self) -> None:
        p = self._portfolio
        elapsed_h = (time.monotonic() - self._start_time) / 3600

        print("\n" + "=" * 70)
        print("  FINAL REPORT")
        print("=" * 70)
        print(f"  Duration:        {elapsed_h:.2f} hours")
        print(f"  Initial Capital: ${p.initial_capital:,.2f}")
        print(f"  Final Value:     ${p.total_value:,.2f}")
        pnl_sign = "+" if p.total_pnl >= 0 else ""
        print(f"  Total P&L:       {pnl_sign}${p.total_pnl:,.2f} ({pnl_sign}{p.total_pnl_pct:.2f}%)")
        print(f"  Realized P&L:    ${p.realized_pnl:,.2f}")
        print(f"  Trades:          {len(p.closed_positions)}")
        print(f"  Win Rate:        {p.win_rate:.1f}%")
        print(f"  Profit Factor:   {p.profit_factor:.2f}")
        print(f"  Signals:         {len(p.signals_log)} total")
        print(f"  Snapshot saved:  {SNAPSHOT_PATH}")
        print("=" * 70)

    @staticmethod
    def _trade_to_dict(t: TradeRecord) -> dict:
        return {
            "id": t.id,
            "market_id": t.market_id,
            "market_slug": t.market_slug,
            "side": t.side,
            "size": t.size,
            "price": t.price,
            "timestamp": t.timestamp,
            "outcome": t.outcome,
        }
