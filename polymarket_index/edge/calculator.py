"""
Edge calculator — compares Polymarket odds against estimated true
probability to find positive expected value (EV) opportunities.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from loguru import logger

from polymarket_index.edge.market_scanner import ActiveMarket
from polymarket_index.edge.price_feeds import PriceFeed, PriceSnapshot
from polymarket_index.config import settings


@dataclass
class EdgeSignal:
    market_id: str
    market_question: str
    market_slug: str
    side: str  # YES or NO
    market_price: float  # what Polymarket is offering
    estimated_prob: float  # our calculated probability
    edge: float  # estimated_prob - market_price (for YES)
    ev_pct: float  # expected value as percentage
    confidence: str  # low / medium / high
    reasoning: str
    size_suggestion: float


class EdgeCalculator:
    """
    Calculates trading edge by comparing Polymarket prices
    to estimated true probabilities from external data.
    """

    def __init__(self, price_feed: PriceFeed) -> None:
        self._prices = price_feed

    async def evaluate_market(self, market: ActiveMarket) -> EdgeSignal | None:
        """Evaluate a market for edge. Returns signal if EV > threshold."""
        slug = market.slug.lower()
        question = market.question.lower()

        if self._is_crypto_market(slug, question):
            return await self._evaluate_crypto_market(market)

        return self._evaluate_generic_market(market)

    def _is_crypto_market(self, slug: str, question: str) -> bool:
        crypto_terms = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana",
                        "xrp", "crypto", "updown"]
        return any(t in slug or t in question for t in crypto_terms)

    async def _evaluate_crypto_market(self, market: ActiveMarket) -> EdgeSignal | None:
        """
        For crypto up/down markets: estimate probability from real price data.
        Uses Binance price position within 24h range + momentum.
        """
        slug = market.slug.lower()
        symbol = self._detect_crypto_symbol(slug, market.question.lower())
        if not symbol:
            return None

        price_data = await self._prices.get_price_at_percentile(symbol)
        if not price_data or price_data.get("price", 0) <= 0:
            return None

        position = price_data["position"]
        bias = price_data["bias"]
        volatility = price_data["volatility"]
        change_pct = price_data["change_pct"]

        is_up_market = any(kw in slug for kw in ["up", "higher", "above", "over"])
        is_down_market = any(kw in slug for kw in ["down", "lower", "below", "under"])

        if is_up_market:
            # Probability of going up: higher if price has momentum up
            # and is not already at the top of range
            base_prob = 0.50
            momentum_adj = change_pct * 0.01  # +1% change -> +1% prob
            position_adj = (0.5 - position) * 0.1  # mean reversion factor
            est_prob = max(0.05, min(0.95, base_prob + momentum_adj + position_adj))
            market_price = market.yes_price
            side = "YES"
        elif is_down_market:
            base_prob = 0.50
            momentum_adj = -change_pct * 0.01
            position_adj = (position - 0.5) * 0.1
            est_prob = max(0.05, min(0.95, base_prob + momentum_adj + position_adj))
            market_price = market.yes_price
            side = "YES"
        else:
            # Generic crypto mention — check if YES is underpriced
            est_prob = 0.50
            market_price = market.yes_price
            side = "YES" if market.yes_price < 0.50 else "NO"
            if side == "NO":
                market_price = market.no_price
                est_prob = 1.0 - est_prob

        edge = est_prob - market_price
        ev_pct = edge / market_price if market_price > 0 else 0

        # If YES is overpriced, flip to NO
        if edge < -settings.min_ev_threshold and side == "YES":
            side = "NO"
            market_price = market.no_price
            est_prob = 1.0 - est_prob
            edge = est_prob - market_price
            ev_pct = edge / market_price if market_price > 0 else 0

        if ev_pct < settings.min_ev_threshold:
            return None

        confidence = "high" if abs(edge) > 0.10 else ("medium" if abs(edge) > 0.05 else "low")

        reasoning = (
            f"{symbol} ${price_data['price']:,.0f} ({change_pct:+.1f}% 24h), "
            f"range position {position:.0%}, bias={bias}, "
            f"est_prob={est_prob:.1%} vs market={market_price:.1%}"
        )

        size = self._compute_kelly_size(est_prob, market_price)

        return EdgeSignal(
            market_id=market.condition_id,
            market_question=market.question,
            market_slug=market.slug,
            side=side,
            market_price=market_price,
            estimated_prob=round(est_prob, 4),
            edge=round(edge, 4),
            ev_pct=round(ev_pct, 4),
            confidence=confidence,
            reasoning=reasoning,
            size_suggestion=round(size, 2),
        )

    def _evaluate_generic_market(self, market: ActiveMarket) -> EdgeSignal | None:
        """
        For non-crypto markets: look for mispriced odds based on
        consensus signals (when many top wallets agree, the market
        may be slow to adjust).
        """
        yes_price = market.yes_price
        no_price = market.no_price

        # Simple spread-based edge: if spread is wide, the mid is more accurate
        # than either side. Trade the side further from mid.
        mid = (yes_price + (1.0 - no_price)) / 2.0
        yes_edge = mid - yes_price
        no_edge = (1.0 - mid) - no_price

        if abs(yes_edge) > abs(no_edge) and yes_edge > settings.min_ev_threshold:
            side = "YES"
            edge = yes_edge
            market_price = yes_price
            est_prob = mid
        elif no_edge > settings.min_ev_threshold:
            side = "NO"
            edge = no_edge
            market_price = no_price
            est_prob = 1.0 - mid
        else:
            return None

        ev_pct = edge / market_price if market_price > 0 else 0
        if ev_pct < settings.min_ev_threshold:
            return None

        confidence = "low"
        reasoning = f"spread edge: mid={mid:.3f}, {side} price={market_price:.3f}, edge={edge:.3f}"
        size = self._compute_kelly_size(est_prob, market_price) * 0.5  # half-size for generic

        return EdgeSignal(
            market_id=market.condition_id,
            market_question=market.question,
            market_slug=market.slug,
            side=side,
            market_price=market_price,
            estimated_prob=round(est_prob, 4),
            edge=round(edge, 4),
            ev_pct=round(ev_pct, 4),
            confidence=confidence,
            reasoning=reasoning,
            size_suggestion=round(size, 2),
        )

    def _detect_crypto_symbol(self, slug: str, question: str) -> str | None:
        text = slug + " " + question
        if "btc" in text or "bitcoin" in text:
            return "BTCUSDT"
        if "eth" in text or "ethereum" in text:
            return "ETHUSDT"
        if "sol" in text or "solana" in text:
            return "SOLUSDT"
        if "xrp" in text:
            return "XRPUSDT"
        return None

    def _compute_kelly_size(self, prob: float, odds_price: float) -> float:
        """
        Half-Kelly criterion for position sizing.
        Returns fraction of portfolio to bet.
        """
        if odds_price <= 0 or odds_price >= 1 or prob <= 0:
            return 0.0
        decimal_odds = 1.0 / odds_price
        b = decimal_odds - 1.0
        q = 1.0 - prob
        kelly = (b * prob - q) / b if b > 0 else 0
        half_kelly = max(0, kelly * 0.5)
        return min(half_kelly, settings.base_trade_pct * 3)
