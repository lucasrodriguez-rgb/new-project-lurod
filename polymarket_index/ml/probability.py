"""
Multi-source probability aggregator.
Combines ML model predictions, market prices, wallet consensus,
and external price feeds into a single probability estimate.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from loguru import logger

from polymarket_index.ml.model import ProbabilityModel
from polymarket_index.ml.features import FeatureExtractor, MarketFeatures
from polymarket_index.edge.market_scanner import ActiveMarket
from polymarket_index.edge.price_feeds import PriceFeed, PriceSnapshot


@dataclass
class ProbabilityEstimate:
    market_id: str
    market_question: str
    yes_probability: float
    confidence: float  # 0-1, how confident we are
    sources: dict  # breakdown by source
    market_price: float
    edge: float  # our estimate - market price


class ProbabilityAggregator:
    """
    Combines multiple probability sources with configurable weights:
    1. ML model prediction (trained on historical data)
    2. Market price (current Polymarket odds)
    3. Wallet consensus (what top traders think)
    4. External data (crypto prices, etc.)
    """

    def __init__(
        self,
        ml_model: ProbabilityModel | None = None,
        price_feed: PriceFeed | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._model = ml_model or ProbabilityModel()
        self._prices = price_feed or PriceFeed()
        self._extractor = FeatureExtractor()
        self._weights = weights or {
            "ml_model": 0.40,
            "market_price": 0.25,
            "wallet_consensus": 0.20,
            "external_data": 0.15,
        }

        if not self._model.is_trained:
            self._model.load()

    async def estimate(
        self,
        market: ActiveMarket,
        wallet_consensus: dict | None = None,
    ) -> ProbabilityEstimate:
        """
        Produce a weighted probability estimate from all sources.
        """
        sources: dict[str, float] = {}
        confidences: dict[str, float] = {}

        # 1. Market price as baseline
        sources["market_price"] = market.yes_price
        confidences["market_price"] = 0.7

        # 2. ML model prediction
        crypto_snap = None
        crypto_range = None
        slug = market.slug.lower()
        question = market.question.lower()

        symbol = None
        if "btc" in slug or "bitcoin" in question:
            symbol = "BTCUSDT"
        elif "eth" in slug or "ethereum" in question:
            symbol = "ETHUSDT"
        elif "sol" in slug or "solana" in question:
            symbol = "SOLUSDT"
        elif "xrp" in slug:
            symbol = "XRPUSDT"

        if symbol:
            crypto_snap = await self._prices.get_price(symbol)
            crypto_range = await self._prices.get_price_at_percentile(symbol)

        features = self._extractor.extract(
            market,
            crypto_snap=crypto_snap,
            crypto_range=crypto_range,
            wallet_consensus=wallet_consensus,
        )

        if self._model.is_trained:
            ml_prob = self._model.predict_proba(features)
            sources["ml_model"] = ml_prob
            confidences["ml_model"] = 0.8
        else:
            sources["ml_model"] = market.yes_price
            confidences["ml_model"] = 0.3

        # 3. Wallet consensus
        if wallet_consensus and wallet_consensus.get("count", 0) >= 3:
            sources["wallet_consensus"] = wallet_consensus.get("yes_fraction", 0.5)
            confidences["wallet_consensus"] = min(
                0.9, 0.5 + wallet_consensus["count"] * 0.05
            )
        else:
            sources["wallet_consensus"] = market.yes_price
            confidences["wallet_consensus"] = 0.2

        # 4. External data (crypto momentum)
        if crypto_range and crypto_range.get("price", 0) > 0:
            is_up_market = any(kw in slug for kw in ["up", "higher", "above"])
            change = crypto_range.get("change_pct", 0)
            position = crypto_range.get("position", 0.5)

            if is_up_market:
                ext_prob = 0.50 + change * 0.015 + (0.5 - position) * 0.08
            else:
                ext_prob = 0.50 - change * 0.015 + (position - 0.5) * 0.08

            sources["external_data"] = max(0.05, min(0.95, ext_prob))
            confidences["external_data"] = 0.6 if abs(change) > 2 else 0.4
        else:
            sources["external_data"] = market.yes_price
            confidences["external_data"] = 0.1

        # Weighted combination
        total_weight = 0.0
        weighted_sum = 0.0

        for source_name, prob in sources.items():
            w = self._weights.get(source_name, 0) * confidences.get(source_name, 0.5)
            weighted_sum += prob * w
            total_weight += w

        if total_weight > 0:
            final_prob = weighted_sum / total_weight
        else:
            final_prob = market.yes_price

        final_prob = max(0.02, min(0.98, final_prob))

        # Overall confidence
        agreement = 1.0 - np.std(list(sources.values())) * 2
        overall_confidence = max(0.1, min(1.0, agreement))

        edge = final_prob - market.yes_price

        return ProbabilityEstimate(
            market_id=market.condition_id,
            market_question=market.question,
            yes_probability=round(final_prob, 4),
            confidence=round(overall_confidence, 3),
            sources={k: round(v, 4) for k, v in sources.items()},
            market_price=market.yes_price,
            edge=round(edge, 4),
        )
