"""
Bayesian probability updater for prediction markets.

Uses Beta-Binomial conjugate model to update our probability estimate
as new evidence arrives (trades, news, price movements).

The key advantage over frequentist approaches: we start with the
market's prior and update as we observe signals, giving us a posterior
that naturally shrinks toward the prior when evidence is weak and
diverges when evidence is strong.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats
from loguru import logger


@dataclass
class BayesianEstimate:
    market_id: str
    prior_yes: float          # market-implied prior
    posterior_yes: float       # our updated estimate
    posterior_no: float
    alpha: float              # Beta distribution alpha (YES evidence)
    beta_param: float         # Beta distribution beta (NO evidence)
    credible_interval: tuple[float, float]  # 90% CI
    evidence_strength: float  # 0-1, how much evidence we have
    divergence_from_market: float  # posterior - market price
    confidence: float         # how confident we are (CI width)


class BayesianUpdater:
    """
    Maintains and updates Bayesian probability estimates for markets.

    Model: Beta(α, β) prior → observe evidence → Beta(α', β') posterior
    - α = pseudo-count of YES evidence
    - β = pseudo-count of NO evidence
    - Prior: set from market price, e.g., market at 60% → Beta(6, 4)

    Evidence sources:
    - Top wallet trades (strong signal)
    - Price momentum (moderate signal)
    - Volume spikes (weak signal)
    - External data (crypto prices, polls, etc.)
    """

    PRIOR_STRENGTH = 10  # equivalent sample size for market prior

    def __init__(self) -> None:
        self._states: dict[str, dict] = {}  # market_id → {alpha, beta, ...}

    def initialize(self, market_id: str, market_price: float) -> BayesianEstimate:
        """
        Initialize a Beta prior from the market price.
        market_price = 0.6 → Beta(6, 4) with strength=10
        """
        alpha = market_price * self.PRIOR_STRENGTH
        beta_param = (1 - market_price) * self.PRIOR_STRENGTH

        self._states[market_id] = {
            "alpha": alpha,
            "beta": beta_param,
            "observations": 0,
            "prior_price": market_price,
        }

        return self._compute_estimate(market_id)

    def update(
        self,
        market_id: str,
        signal_type: str,
        signal_direction: str,
        signal_strength: float = 1.0,
    ) -> BayesianEstimate:
        """
        Update the posterior with new evidence.

        signal_type: "wallet_trade", "price_momentum", "volume_spike",
                     "external_data", "news"
        signal_direction: "YES" or "NO"
        signal_strength: 0-1, how strong the signal is
        """
        if market_id not in self._states:
            raise ValueError(f"Market {market_id} not initialized")

        state = self._states[market_id]

        # Different signal types have different evidence weights
        weights = {
            "wallet_trade": 2.0,
            "price_momentum": 1.0,
            "volume_spike": 0.5,
            "external_data": 1.5,
            "news": 1.0,
            "theta_decay": 0.3,
        }

        weight = weights.get(signal_type, 1.0) * signal_strength

        if signal_direction.upper() in ("YES", "BUY"):
            state["alpha"] += weight
        else:
            state["beta"] += weight

        state["observations"] += 1

        return self._compute_estimate(market_id)

    def bulk_update(
        self,
        market_id: str,
        yes_signals: int,
        no_signals: int,
        weight_per_signal: float = 1.0,
    ) -> BayesianEstimate:
        """Update with multiple observations at once."""
        if market_id not in self._states:
            raise ValueError(f"Market {market_id} not initialized")

        state = self._states[market_id]
        state["alpha"] += yes_signals * weight_per_signal
        state["beta"] += no_signals * weight_per_signal
        state["observations"] += yes_signals + no_signals

        return self._compute_estimate(market_id)

    def get_estimate(self, market_id: str) -> BayesianEstimate | None:
        if market_id not in self._states:
            return None
        return self._compute_estimate(market_id)

    def _compute_estimate(self, market_id: str) -> BayesianEstimate:
        state = self._states[market_id]
        alpha = state["alpha"]
        beta_param = state["beta"]
        prior_price = state["prior_price"]

        dist = stats.beta(alpha, beta_param)
        posterior_yes = float(dist.mean())
        posterior_no = 1 - posterior_yes

        # 90% credible interval
        ci_low, ci_high = dist.ppf(0.05), dist.ppf(0.95)

        # Evidence strength: how much our posterior has been influenced
        total_evidence = alpha + beta_param - self.PRIOR_STRENGTH
        evidence_strength = min(1.0, total_evidence / 20.0)

        # Confidence: inversely proportional to CI width
        ci_width = ci_high - ci_low
        confidence = max(0.0, 1.0 - ci_width)

        return BayesianEstimate(
            market_id=market_id,
            prior_yes=prior_price,
            posterior_yes=round(posterior_yes, 4),
            posterior_no=round(posterior_no, 4),
            alpha=round(alpha, 2),
            beta_param=round(beta_param, 2),
            credible_interval=(round(ci_low, 4), round(ci_high, 4)),
            evidence_strength=round(evidence_strength, 3),
            divergence_from_market=round(posterior_yes - prior_price, 4),
            confidence=round(confidence, 3),
        )

    def compute_kelly_from_posterior(
        self,
        market_id: str,
        market_price: float,
    ) -> tuple[str, float]:
        """
        Compute half-Kelly bet size using the Bayesian posterior.
        Returns (side, fraction_of_bankroll).
        """
        estimate = self.get_estimate(market_id)
        if estimate is None:
            return "NONE", 0.0

        p = estimate.posterior_yes

        # Check if YES is underpriced
        if p > market_price:
            side = "YES"
            edge = p - market_price
            odds = 1.0 / market_price - 1.0
        elif (1 - p) > (1 - market_price):
            side = "NO"
            edge = (1 - p) - (1 - market_price)
            odds = 1.0 / (1 - market_price) - 1.0
        else:
            return "NONE", 0.0

        if odds <= 0:
            return "NONE", 0.0

        # Full Kelly: f* = (p * odds - (1-p)) / odds
        kelly = (edge * (1 + odds)) / odds
        half_kelly = kelly * 0.5

        # Scale by confidence
        half_kelly *= estimate.confidence

        return side, max(0.0, min(0.10, half_kelly))
