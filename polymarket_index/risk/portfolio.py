"""
Correlation-aware portfolio optimization and dynamic Kelly sizing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from loguru import logger


@dataclass
class PositionSuggestion:
    market_id: str
    side: str
    suggested_size_pct: float
    kelly_fraction: float
    correlation_penalty: float
    final_size_pct: float
    reasoning: str


class PortfolioOptimizer:
    """
    Optimizes position sizing using:
    1. Half-Kelly criterion based on edge
    2. Correlation penalty for related positions
    3. Concentration limits per category
    """

    MAX_KELLY = 0.10  # never more than 10% of portfolio on one trade
    CORRELATION_CATEGORIES = {
        "nba": ["nba", "basketball"],
        "nfl": ["nfl", "football"],
        "mlb": ["mlb", "baseball"],
        "soccer": ["soccer", "football", "epl", "ucl", "champions"],
        "crypto": ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "crypto"],
        "politics": ["president", "election", "trump", "biden", "congress", "senate"],
        "esports": ["cs2", "lol", "dota", "valorant"],
    }

    def compute_size(
        self,
        market_id: str,
        market_slug: str,
        side: str,
        estimated_prob: float,
        market_price: float,
        portfolio_value: float,
        open_positions: list[dict],
    ) -> PositionSuggestion:
        """Compute optimal position size with Kelly + correlation adjustment."""

        # 1. Kelly criterion
        kelly = self._half_kelly(estimated_prob, market_price)

        # 2. Correlation penalty
        categories = self._detect_categories(market_slug)
        corr_penalty = self._correlation_penalty(categories, open_positions)

        # 3. Concentration check
        adjusted_kelly = kelly * (1.0 - corr_penalty)
        adjusted_kelly = min(adjusted_kelly, self.MAX_KELLY)

        final_pct = max(adjusted_kelly, 0.002)  # minimum 0.2%

        reasoning_parts = [f"kelly={kelly:.3f}"]
        if corr_penalty > 0:
            reasoning_parts.append(f"corr_penalty={corr_penalty:.2f}")
        reasoning_parts.append(f"final={final_pct:.3f}")

        return PositionSuggestion(
            market_id=market_id,
            side=side,
            suggested_size_pct=round(kelly, 4),
            kelly_fraction=round(kelly, 4),
            correlation_penalty=round(corr_penalty, 3),
            final_size_pct=round(final_pct, 4),
            reasoning=", ".join(reasoning_parts),
        )

    def _half_kelly(self, prob: float, price: float) -> float:
        """Half-Kelly criterion for binary outcome betting."""
        if price <= 0 or price >= 1 or prob <= 0 or prob >= 1:
            return 0.0

        b = (1.0 / price) - 1.0  # decimal odds - 1
        q = 1.0 - prob

        kelly = (b * prob - q) / b if b > 0 else 0
        half_kelly = kelly * 0.5

        return max(0.0, min(self.MAX_KELLY, half_kelly))

    def _detect_categories(self, slug: str) -> list[str]:
        """Detect which correlation categories a market belongs to."""
        slug_lower = slug.lower()
        matched = []
        for category, keywords in self.CORRELATION_CATEGORIES.items():
            if any(kw in slug_lower for kw in keywords):
                matched.append(category)
        return matched

    def _correlation_penalty(
        self, categories: list[str], open_positions: list[dict]
    ) -> float:
        """
        Calculate penalty based on how many correlated positions we already hold.
        More positions in the same category → higher penalty.
        """
        if not categories or not open_positions:
            return 0.0

        max_penalty = 0.0
        for cat in categories:
            keywords = self.CORRELATION_CATEGORIES.get(cat, [])
            correlated_count = sum(
                1 for p in open_positions
                if any(kw in p.get("market_slug", "").lower() for kw in keywords)
            )

            if correlated_count == 0:
                continue
            elif correlated_count <= 2:
                penalty = 0.15 * correlated_count
            elif correlated_count <= 4:
                penalty = 0.30 + 0.15 * (correlated_count - 2)
            else:
                penalty = 0.70

            max_penalty = max(max_penalty, penalty)

        return min(max_penalty, 0.80)

    def get_portfolio_breakdown(self, positions: list[dict]) -> dict:
        """Analyze current portfolio by category."""
        breakdown: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usdc": 0.0})

        for p in positions:
            slug = p.get("market_slug", "")
            cats = self._detect_categories(slug)
            if not cats:
                cats = ["other"]
            for cat in cats:
                breakdown[cat]["count"] += 1
                breakdown[cat]["total_usdc"] += p.get("size_usdc", 0)

        return dict(breakdown)
