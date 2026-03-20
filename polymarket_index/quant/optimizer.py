"""
Portfolio optimization with KL-divergence constraints using CVXPY.

Solves for the optimal allocation across prediction market positions
that minimizes KL-divergence from a target distribution while
respecting risk constraints.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import cvxpy as cp
from loguru import logger


@dataclass
class OptimalAllocation:
    market_ids: list[str]
    weights: list[float]
    expected_return: float
    risk: float
    kl_from_target: float
    sharpe: float


class PortfolioOptimizer:
    """
    Optimizes prediction market portfolio using convex optimization.

    Objective: maximize expected return subject to:
    1. KL-divergence from target (uniform or custom) below threshold
    2. Position size limits
    3. Total allocation = 100% of deployed capital
    4. Minimum diversification constraint
    """

    def optimize(
        self,
        market_ids: list[str],
        expected_probs: list[float],    # our estimated P(YES wins)
        market_prices: list[float],     # current market price (YES)
        max_position_pct: float = 0.15,
        kl_budget: float = 0.5,
        target_weights: list[float] | None = None,
    ) -> OptimalAllocation:
        """
        Solve for optimal portfolio weights.

        Args:
            expected_probs: our probability estimates for each market
            market_prices: current YES prices on Polymarket
            max_position_pct: max allocation to any single market
            kl_budget: max KL-divergence from target distribution
            target_weights: target allocation (default: equal-weight)
        """
        n = len(market_ids)
        if n == 0:
            return OptimalAllocation([], [], 0, 0, 0, 0)

        # Decision variable: portfolio weights
        w = cp.Variable(n)

        # Expected edge per market: prob_true - price_market
        edges = np.array(expected_probs) - np.array(market_prices)

        # Expected return = sum(w_i * edge_i)
        expected_return = edges @ w

        # Target distribution (default: equal weight)
        if target_weights is None:
            theta = np.ones(n) / n
        else:
            theta = np.array(target_weights)
            theta = theta / theta.sum()

        # Constraints
        constraints = [
            cp.sum(w) == 1,       # fully invested
            w >= 0.01 / n,        # minimum allocation (small epsilon)
            w <= max_position_pct, # max per position
        ]

        # KL-divergence constraint: D_KL(w || theta) <= budget
        # Using the CVXPY kl_div atom: sum(kl_div(w_i, theta_i))
        kl_constraint = cp.sum(cp.kl_div(w, theta)) <= kl_budget
        constraints.append(kl_constraint)

        # Objective: maximize expected return
        objective = cp.Maximize(expected_return)

        problem = cp.Problem(objective, constraints)
        try:
            problem.solve(solver=cp.SCS, verbose=False)
        except Exception:
            try:
                problem.solve(solver=cp.CLARABEL, verbose=False)
            except Exception as exc:
                logger.warning("Optimization failed: {}", exc)
                # Fallback: equal weight
                w_fallback = np.ones(n) / n
                return OptimalAllocation(
                    market_ids=market_ids,
                    weights=w_fallback.tolist(),
                    expected_return=float(edges @ w_fallback),
                    risk=0,
                    kl_from_target=0,
                    sharpe=0,
                )

        if problem.status not in ("optimal", "optimal_inaccurate"):
            logger.warning("Optimization status: {}", problem.status)
            w_fallback = np.ones(n) / n
            return OptimalAllocation(
                market_ids=market_ids,
                weights=w_fallback.tolist(),
                expected_return=float(edges @ w_fallback),
                risk=0,
                kl_from_target=0,
                sharpe=0,
            )

        optimal_weights = w.value
        optimal_weights = np.clip(optimal_weights, 0, 1)
        optimal_weights /= optimal_weights.sum()

        # Compute KL from target
        kl_val = float(np.sum(
            optimal_weights * np.log(np.clip(optimal_weights / theta, 1e-10, 1e10))
        ))

        # Risk estimate: weighted variance of edges
        risk = float(np.sqrt(np.sum(optimal_weights ** 2 * edges ** 2)))
        exp_ret = float(edges @ optimal_weights)
        sharpe = exp_ret / risk if risk > 0 else 0

        result = OptimalAllocation(
            market_ids=market_ids,
            weights=[round(float(x), 4) for x in optimal_weights],
            expected_return=round(exp_ret, 4),
            risk=round(risk, 4),
            kl_from_target=round(kl_val, 4),
            sharpe=round(sharpe, 2),
        )

        logger.info(
            "Optimized portfolio: {} markets, E[R]={:.2%}, Sharpe={:.2f}, KL={:.4f}",
            n, exp_ret, sharpe, kl_val,
        )

        return result

    def optimize_hedge_portfolio(
        self,
        long_market_ids: list[str],
        long_probs: list[float],
        long_prices: list[float],
        short_market_ids: list[str],
        short_probs: list[float],
        short_prices: list[float],
        capital: float = 100_000,
    ) -> dict:
        """
        Build a hedged long/short portfolio across correlated markets.
        Long the underpriced side, short the overpriced side.
        """
        all_ids = long_market_ids + short_market_ids
        all_probs = long_probs + [1 - p for p in short_probs]
        all_prices = long_prices + [1 - p for p in short_prices]

        allocation = self.optimize(
            market_ids=all_ids,
            expected_probs=all_probs,
            market_prices=all_prices,
            max_position_pct=0.20,
            kl_budget=1.0,
        )

        positions = []
        for i, (mid, weight) in enumerate(zip(all_ids, allocation.weights)):
            size = capital * weight
            if i < len(long_market_ids):
                side = "YES"
                entry_price = long_prices[i]
            else:
                side = "NO"
                idx = i - len(long_market_ids)
                entry_price = 1 - short_prices[idx]

            positions.append({
                "market_id": mid,
                "side": side,
                "weight": weight,
                "size_usdc": round(size, 2),
                "entry_price": entry_price,
                "expected_edge": round(all_probs[i] - all_prices[i], 4),
            })

        return {
            "positions": positions,
            "total_capital": capital,
            "expected_return": allocation.expected_return,
            "sharpe": allocation.sharpe,
            "kl_divergence": allocation.kl_from_target,
        }
