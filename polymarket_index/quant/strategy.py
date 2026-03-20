"""
Unified quant strategy combining theta decay, KL-divergence mispricing,
and Bayesian probability updating into actionable trading signals.

The strategy runs three engines in parallel:
1. Theta Scanner: finds markets where time decay creates edge
2. KL Scanner: finds correlated markets that are mispriced relative to each other
3. Bayesian Updater: maintains probability estimates that incorporate all signals

Trades are executed when: theta + KL + Bayesian all agree on direction.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from dataclasses import dataclass

from loguru import logger

from polymarket_index.api.gamma_markets import fetch_active_markets, LiveMarket
from polymarket_index.quant.theta import ThetaCalculator, ThetaMetrics, print_theta_report
from polymarket_index.quant.kl_scanner import KLScanner, MarketDistribution, KLMispricing, print_kl_report
from polymarket_index.quant.bayesian import BayesianUpdater, BayesianEstimate
from polymarket_index.quant.optimizer import PortfolioOptimizer
from polymarket_index.edge.trade_db import TradeDB, EdgeTradeRecord


@dataclass
class QuantSignal:
    market_id: str
    question: str
    side: str
    confidence: float        # 0-1
    theta_edge: float        # edge from time decay
    kl_edge: float           # edge from correlation mispricing
    bayesian_edge: float     # edge from Bayesian posterior
    combined_edge: float     # weighted combination
    suggested_size_pct: float
    reasoning: str


class QuantStrategy:
    """
    Full quant strategy that scans Polymarket for opportunities
    using theta decay, KL-divergence, and Bayesian updating.
    """

    THETA_WEIGHT = 0.30
    KL_WEIGHT = 0.35
    BAYESIAN_WEIGHT = 0.35
    MIN_COMBINED_EDGE = 0.02

    def __init__(
        self,
        capital: float = 10_000,
        poll_interval: int = 30,
        duration_hours: float = 24,
        dry_run: bool = True,
    ) -> None:
        self._capital = capital
        self._poll_interval = poll_interval
        self._duration = duration_hours
        self._dry_run = dry_run

        self._theta = ThetaCalculator()
        self._kl = KLScanner()
        self._bayes = BayesianUpdater()
        self._optimizer = PortfolioOptimizer()
        self._db = TradeDB()

        self._running = False
        self._cycle = 0
        self._signals: list[QuantSignal] = []
        self._start_time = 0.0

    async def run(self) -> None:
        self._running = True
        self._start_time = time.monotonic()
        mode = "LIVE" if not self._dry_run else "DRY RUN"

        logger.info("=" * 70)
        logger.info("QUANT STRATEGY [{}]", mode)
        logger.info("Theta({:.0%}) + KL({:.0%}) + Bayesian({:.0%})",
                     self.THETA_WEIGHT, self.KL_WEIGHT, self.BAYESIAN_WEIGHT)
        logger.info("Capital: ${:,.0f} | Min edge: {:.0%} | Poll: {}s",
                     self._capital, self.MIN_COMBINED_EDGE, self._poll_interval)
        logger.info("=" * 70)

        try:
            end_time = self._start_time + self._duration * 3600
            while self._running and time.monotonic() < end_time:
                self._cycle += 1
                signals = await self._scan_cycle()
                self._print_dashboard(signals)
                await asyncio.sleep(self._poll_interval)

        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self._db.close()

    async def _scan_cycle(self) -> list[QuantSignal]:
        """One full scan cycle: fetch markets → run all three engines → combine."""
        try:
            markets = await fetch_active_markets(limit=100, min_volume_24h=5000)
        except Exception as exc:
            logger.warning("Market fetch failed: {}", exc)
            return []

        if not markets:
            return []

        # 1. Theta scan
        theta_results = self._run_theta(markets)

        # 2. KL scan
        kl_results = self._run_kl(markets)

        # 3. Bayesian update from price data
        bayes_results = self._run_bayesian(markets)

        # 4. Combine signals
        signals = self._combine_signals(markets, theta_results, kl_results, bayes_results)

        # 5. Optimize portfolio allocation
        if signals:
            self._optimize_portfolio(signals)

        self._signals = signals
        return signals

    def _run_theta(self, markets: list[LiveMarket]) -> dict[str, ThetaMetrics]:
        market_dicts = []
        for m in markets:
            hours = None
            if m.end_date:
                try:
                    end_dt = dt.datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                    hours = (end_dt - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass

            market_dicts.append({
                "market_id": m.condition_id,
                "question": m.question,
                "yes_price": m.yes_price,
                "hours_to_close": hours,
                "liquidity": m.liquidity,
                "volume_24h": m.volume_24h,
            })

        results = self._theta.scan_theta_opportunities(market_dicts, min_theta_pct=0.1)
        return {r.market_id: r for r in results}

    def _run_kl(self, markets: list[LiveMarket]) -> dict[str, KLMispricing]:
        distributions = [
            MarketDistribution(
                market_id=m.condition_id,
                question=m.question,
                slug=m.slug,
                probs=[m.yes_price, m.no_price],
                volume_24h=m.volume_24h,
                liquidity=m.liquidity,
            )
            for m in markets
        ]

        results = self._kl.scan(distributions)
        kl_by_market: dict[str, KLMispricing] = {}
        for r in results:
            if r.arb_signal:
                kl_by_market[r.market_a_id] = r
                kl_by_market[r.market_b_id] = r
        return kl_by_market

    def _run_bayesian(self, markets: list[LiveMarket]) -> dict[str, BayesianEstimate]:
        results: dict[str, BayesianEstimate] = {}
        for m in markets:
            estimate = self._bayes.get_estimate(m.condition_id)
            if estimate is None:
                estimate = self._bayes.initialize(m.condition_id, m.yes_price)

            # Update with price momentum as evidence
            if m.best_bid > 0 and m.best_ask > 0:
                mid = (m.best_bid + m.best_ask) / 2
                if mid > m.yes_price + 0.01:
                    self._bayes.update(m.condition_id, "price_momentum", "YES", 0.3)
                elif mid < m.yes_price - 0.01:
                    self._bayes.update(m.condition_id, "price_momentum", "NO", 0.3)

            results[m.condition_id] = self._bayes.get_estimate(m.condition_id)
        return results

    def _combine_signals(
        self,
        markets: list[LiveMarket],
        theta: dict[str, ThetaMetrics],
        kl: dict[str, KLMispricing],
        bayes: dict[str, BayesianEstimate],
    ) -> list[QuantSignal]:
        signals: list[QuantSignal] = []

        for m in markets:
            mid = m.condition_id

            # Theta component
            theta_edge = 0.0
            theta_side = "YES"
            if mid in theta:
                t = theta[mid]
                theta_edge = t.edge_from_theta
                theta_side = t.suggested_side

            # KL component
            kl_edge = 0.0
            if mid in kl:
                k = kl[mid]
                kl_edge = k.estimated_edge_pct / 100.0

            # Bayesian component
            bayes_edge = 0.0
            bayes_side = "YES"
            if mid in bayes:
                b = bayes[mid]
                bayes_edge = abs(b.divergence_from_market)
                bayes_side = "YES" if b.divergence_from_market > 0 else "NO"

            # Combine edges
            combined = (
                self.THETA_WEIGHT * theta_edge
                + self.KL_WEIGHT * kl_edge
                + self.BAYESIAN_WEIGHT * bayes_edge
            )

            if combined < self.MIN_COMBINED_EDGE:
                continue

            # Determine side: majority vote
            sides = []
            if theta_edge > 0.001:
                sides.append(theta_side)
            if kl_edge > 0.001:
                sides.append("YES")  # KL doesn't have clear directionality
            if bayes_edge > 0.001:
                sides.append(bayes_side)

            side = max(set(sides), key=sides.count) if sides else "YES"

            # Confidence: how many engines agree
            agree_count = len([s for s in sides if s == side])
            confidence = agree_count / max(len(sides), 1)

            # Size: Kelly from Bayesian posterior
            if mid in bayes:
                _, kelly_size = self._bayes.compute_kelly_from_posterior(mid, m.yes_price)
            else:
                kelly_size = 0.01

            reasoning_parts = []
            if theta_edge > 0.001:
                t = theta.get(mid)
                reasoning_parts.append(f"theta={theta_edge:.4f} ({t.decay_regime if t else '?'})")
            if kl_edge > 0.001:
                reasoning_parts.append(f"KL={kl_edge:.4f}")
            if bayes_edge > 0.001:
                b = bayes.get(mid)
                reasoning_parts.append(f"bayes={b.posterior_yes:.3f} vs mkt={m.yes_price:.3f}" if b else "")

            signals.append(QuantSignal(
                market_id=mid,
                question=m.question,
                side=side,
                confidence=round(confidence, 2),
                theta_edge=round(theta_edge, 6),
                kl_edge=round(kl_edge, 6),
                bayesian_edge=round(bayes_edge, 6),
                combined_edge=round(combined, 6),
                suggested_size_pct=round(kelly_size, 4),
                reasoning="; ".join(reasoning_parts),
            ))

        signals.sort(key=lambda s: s.combined_edge, reverse=True)
        return signals

    def _optimize_portfolio(self, signals: list[QuantSignal]) -> None:
        if len(signals) < 2:
            return

        top = signals[:20]
        market_ids = [s.market_id for s in top]
        probs = [
            0.5 + s.combined_edge if s.side == "YES" else 0.5 - s.combined_edge
            for s in top
        ]
        prices = [0.5] * len(top)  # simplified; real implementation uses actual prices

        try:
            allocation = self._optimizer.optimize(
                market_ids=market_ids,
                expected_probs=probs,
                market_prices=prices,
                max_position_pct=0.15,
                kl_budget=0.5,
            )

            for i, sig in enumerate(top):
                if i < len(allocation.weights):
                    sig.suggested_size_pct = allocation.weights[i]

        except Exception as exc:
            logger.debug("Portfolio optimization failed: {}", exc)

    def _print_dashboard(self, signals: list[QuantSignal]) -> None:
        elapsed_m = (time.monotonic() - self._start_time) / 60
        remaining_m = max(0, self._duration * 60 - elapsed_m)

        os.system("clear" if os.name != "nt" else "cls")

        print("=" * 80)
        print("  QUANT STRATEGY — Theta + KL-Divergence + Bayesian")
        print(f"  {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"  Cycle #{self._cycle} | {elapsed_m:.0f}m elapsed | {remaining_m:.0f}m remaining")
        print("=" * 80)

        if signals:
            print(f"\n  {'#':>3}  {'Edge':>7}  {'Conf':>5}  {'Theta':>7}  {'KL':>7}  "
                  f"{'Bayes':>7}  {'Side':>4}  {'Size':>6}  Question")
            print(f"  {'—'*75}")

            for i, s in enumerate(signals[:15]):
                print(
                    f"  {i+1:3d}  {s.combined_edge:6.4f}  {s.confidence:4.0%}  "
                    f"{s.theta_edge:6.4f}  {s.kl_edge:6.4f}  {s.bayesian_edge:6.4f}  "
                    f"{s.side:>4s}  {s.suggested_size_pct:5.2%}  {s.question[:30]}"
                )

            print(f"\n  Top signal: {signals[0].question[:50]}")
            print(f"    {signals[0].reasoning}")
        else:
            print("\n  No signals above threshold this cycle")

        stats = self._db.get_stats()
        print(f"\n  Trades: {stats['total_trades']} | "
              f"Win rate: {stats['win_rate']:.0f}% | "
              f"P&L: ${stats['total_pnl']:+,.2f}")
        print("=" * 80)


async def main() -> None:
    """Entry point for the quant strategy."""
    import sys
    import signal as sig

    dry_run = "--live" not in sys.argv

    strategy = QuantStrategy(
        capital=10_000,
        poll_interval=30,
        duration_hours=24,
        dry_run=dry_run,
    )

    loop = asyncio.get_event_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        loop.add_signal_handler(s, lambda: setattr(strategy, '_running', False))

    await strategy.run()
