"""
Theta (time decay) model for prediction markets.

In prediction markets, theta works differently than options:
- As resolution approaches, prices converge to 0 or 1
- Markets trading near 50% have the highest theta (most uncertainty to resolve)
- Markets near 0% or 100% have low theta (outcome is ~known)
- Theta accelerates non-linearly as expiry approaches

The strategy: SELL overpriced uncertainty. When a market is priced at
(say) 30% YES with 48 hours to close, and our model says the true prob
is 25%, the 5% edge compounds with theta as time passes — the market
will converge faster to the true value as resolution nears.

Key insight: prediction market theta is analogous to selling options
premium. We want to be short gamma (sell uncertainty) in markets where
we have an edge on the true probability.
"""
from __future__ import annotations

import math
import datetime as dt
from dataclasses import dataclass

import numpy as np
from loguru import logger


@dataclass
class ThetaMetrics:
    market_id: str
    question: str
    yes_price: float
    hours_to_close: float
    theta_daily: float        # daily time decay in price units
    theta_hourly: float       # hourly decay
    theta_pct: float          # decay as % of current price per day
    implied_vol: float        # implied volatility of the market
    certainty_rate: float     # how fast the market is converging (0-1)
    decay_regime: str         # "slow", "accelerating", "terminal"
    edge_from_theta: float    # exploitable edge from theta alone
    suggested_side: str       # YES or NO
    suggested_size_pct: float # suggested position size


class ThetaCalculator:
    """
    Computes theta (time decay) for Polymarket prediction markets.

    The model treats a binary market as a Brownian bridge — price starts
    at current level and must converge to 0 or 1 by expiry. The rate of
    convergence (theta) depends on:

    1. Distance from certainty: |price - 0.5| — closer to 50% = more decay
    2. Time remaining: less time = faster decay (1/sqrt(t) scaling)
    3. Market-implied volatility: derived from historical price movement
    4. Liquidity: thinner markets have noisier convergence
    """

    # Theta accelerates inside this window
    TERMINAL_HOURS = 24
    ACCELERATING_HOURS = 72

    def compute(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        hours_to_close: float,
        liquidity: float = 0,
        volume_24h: float = 0,
        historical_prices: list[float] | None = None,
    ) -> ThetaMetrics:
        """Compute theta metrics for a single market."""

        if hours_to_close <= 0:
            hours_to_close = 0.01

        # Implied volatility from price distance to endpoints
        # Markets at 50% have max vol, markets at 0/100% have min vol
        price_uncertainty = 4 * yes_price * (1 - yes_price)  # peaks at 0.5
        base_vol = math.sqrt(price_uncertainty)

        # Adjust vol with historical price movement if available
        if historical_prices and len(historical_prices) >= 5:
            returns = np.diff(historical_prices) / np.array(historical_prices[:-1])
            hist_vol = float(np.std(returns)) * math.sqrt(24)  # annualize to daily
            implied_vol = 0.6 * base_vol + 0.4 * hist_vol
        else:
            implied_vol = base_vol

        # Brownian bridge theta: d/dt of E[|B_t - target|]
        # For binary outcome: theta ∝ sigma / (2 * sqrt(T))
        # where T is time remaining and sigma is implied vol
        t_years = hours_to_close / (365.25 * 24)
        sqrt_t = math.sqrt(max(t_years, 1e-6))

        # Daily theta in price units
        # This represents how much the price is expected to move toward
        # its terminal value (0 or 1) per day due to time passage alone
        theta_daily = (implied_vol * price_uncertainty) / (2 * sqrt_t * 365.25)

        # Terminal acceleration: theta increases as sqrt(1/t)
        if hours_to_close < self.TERMINAL_HOURS:
            terminal_factor = math.sqrt(self.TERMINAL_HOURS / max(hours_to_close, 0.5))
            theta_daily *= terminal_factor
            decay_regime = "terminal"
        elif hours_to_close < self.ACCELERATING_HOURS:
            accel_factor = 1 + (self.ACCELERATING_HOURS - hours_to_close) / self.ACCELERATING_HOURS
            theta_daily *= accel_factor
            decay_regime = "accelerating"
        else:
            decay_regime = "slow"

        theta_hourly = theta_daily / 24
        theta_pct = (theta_daily / yes_price * 100) if yes_price > 0.01 else 0

        # Certainty rate: how quickly the market is resolving
        # High certainty = price near 0 or 1, theta is less useful
        certainty_rate = abs(2 * yes_price - 1)  # 0 at p=0.5, 1 at p=0 or p=1

        # Exploitable edge from theta:
        # If we're on the right side of the convergence, theta works for us.
        # The edge is proportional to theta * our confidence in the direction.
        # For the base case (no external signal), we assume the market is
        # slightly overpricing uncertainty, so we sell the side closer to 0.5
        if yes_price > 0.5:
            suggested_side = "YES"
            edge_from_theta = theta_daily * (certainty_rate * 0.5)
        else:
            suggested_side = "NO"
            edge_from_theta = theta_daily * (certainty_rate * 0.5)

        # Position sizing: larger in terminal regime, smaller in slow
        regime_multipliers = {"terminal": 2.0, "accelerating": 1.5, "slow": 1.0}
        base_size = 0.01
        suggested_size = base_size * regime_multipliers[decay_regime]
        # Reduce size for illiquid markets
        if liquidity > 0 and liquidity < 10_000:
            suggested_size *= 0.5

        return ThetaMetrics(
            market_id=market_id,
            question=question,
            yes_price=yes_price,
            hours_to_close=hours_to_close,
            theta_daily=round(theta_daily, 6),
            theta_hourly=round(theta_hourly, 6),
            theta_pct=round(theta_pct, 4),
            implied_vol=round(implied_vol, 4),
            certainty_rate=round(certainty_rate, 4),
            decay_regime=decay_regime,
            edge_from_theta=round(edge_from_theta, 6),
            suggested_side=suggested_side,
            suggested_size_pct=round(suggested_size, 4),
        )

    def scan_theta_opportunities(
        self,
        markets: list[dict],
        min_theta_pct: float = 0.5,
        max_hours: float = 168,
    ) -> list[ThetaMetrics]:
        """
        Scan a list of markets for theta-rich opportunities.
        Returns markets sorted by exploitable theta (highest first).
        """
        results: list[ThetaMetrics] = []

        for m in markets:
            hours = m.get("hours_to_close")
            if hours is None or hours <= 0 or hours > max_hours:
                continue

            yes_price = m.get("yes_price", 0.5)
            if yes_price < 0.03 or yes_price > 0.97:
                continue

            metrics = self.compute(
                market_id=m.get("market_id", ""),
                question=m.get("question", ""),
                yes_price=yes_price,
                hours_to_close=hours,
                liquidity=m.get("liquidity", 0),
                volume_24h=m.get("volume_24h", 0),
            )

            if metrics.theta_pct >= min_theta_pct:
                results.append(metrics)

        results.sort(key=lambda x: x.edge_from_theta, reverse=True)
        return results

    def compute_theta_pnl(
        self,
        entry_price: float,
        hours_held: float,
        total_hours_to_close: float,
        side: str = "YES",
    ) -> float:
        """
        Estimate P&L from theta alone over a holding period.
        Assumes price converges linearly toward the terminal value.
        """
        remaining_at_entry = total_hours_to_close
        remaining_at_exit = max(0, total_hours_to_close - hours_held)

        # Certainty increases as sqrt(time_elapsed / total_time)
        certainty_at_entry = math.sqrt(1 - remaining_at_entry / max(total_hours_to_close, 1))
        certainty_at_exit = math.sqrt(1 - remaining_at_exit / max(total_hours_to_close, 1))

        # If we're on the winning side, theta accrues as price moves toward 1
        # If losing side, theta hurts as price moves toward 0
        theta_accrual = certainty_at_exit - certainty_at_entry

        if side.upper() in ("YES", "BUY"):
            # Theta helps if price > 0.5 (converging to 1)
            directional = (entry_price - 0.5) * 2
        else:
            directional = (0.5 - entry_price) * 2

        return theta_accrual * directional * entry_price


def print_theta_report(metrics: list[ThetaMetrics], top_n: int = 15) -> None:
    """Pretty-print theta analysis."""
    print(f"\n{'='*95}")
    print(f"  THETA DECAY ANALYSIS — TOP {min(top_n, len(metrics))} OPPORTUNITIES")
    print(f"{'='*95}")
    print(f"  {'#':>3}  {'Price':>6}  {'Hours':>6}  {'Theta/d':>8}  {'Theta%':>7}  "
          f"{'ImplVol':>7}  {'Regime':>12}  {'Edge':>8}  {'Side':>4}  Question")
    print(f"  {'—'*90}")

    for i, m in enumerate(metrics[:top_n]):
        print(
            f"  {i+1:3d}  {m.yes_price:5.1%}  {m.hours_to_close:5.0f}h  "
            f"{m.theta_daily:7.4f}  {m.theta_pct:6.2f}%  "
            f"{m.implied_vol:6.4f}  {m.decay_regime:>12s}  "
            f"{m.edge_from_theta:7.5f}  {m.suggested_side:>4s}  {m.question[:30]}"
        )

    print(f"  {'—'*90}")
    print(f"{'='*95}\n")
