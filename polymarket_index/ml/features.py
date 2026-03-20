"""
Feature engineering for the ML probability model.
Extracts numerical features from market data, price feeds, wallet signals,
and order book state to predict true outcome probabilities.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np

from polymarket_index.edge.market_scanner import ActiveMarket
from polymarket_index.edge.price_feeds import PriceSnapshot


@dataclass
class MarketFeatures:
    market_id: str
    question: str

    # Price features
    yes_price: float = 0.5
    no_price: float = 0.5
    spread: float = 0.0
    mid_price: float = 0.5

    # Volume / liquidity
    log_volume: float = 0.0
    log_liquidity: float = 0.0
    volume_liquidity_ratio: float = 0.0

    # Time features
    hours_to_close: float = 0.0
    log_hours_to_close: float = 0.0
    is_closing_soon: float = 0.0  # 1 if < 24h

    # Order book features
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    book_imbalance: float = 0.0  # (bid - ask) / (bid + ask)
    book_spread: float = 0.0

    # Crypto price features (if applicable)
    crypto_price: float = 0.0
    crypto_change_24h: float = 0.0
    crypto_range_position: float = 0.5
    crypto_volatility: float = 0.0
    crypto_momentum: float = 0.0  # -1 bearish to +1 bullish

    # Category features
    is_crypto: float = 0.0
    is_sports: float = 0.0
    is_politics: float = 0.0

    # Wallet consensus
    top_wallet_agreement: float = 0.0  # fraction of top wallets on YES
    num_top_wallets_trading: int = 0

    # Implied features
    price_deviation_from_50: float = 0.0
    price_extremity: float = 0.0  # how far from 0.5

    def to_array(self) -> np.ndarray:
        return np.array([
            self.yes_price,
            self.spread,
            self.mid_price,
            self.log_volume,
            self.log_liquidity,
            self.volume_liquidity_ratio,
            self.log_hours_to_close,
            self.is_closing_soon,
            self.bid_depth,
            self.ask_depth,
            self.book_imbalance,
            self.book_spread,
            self.crypto_change_24h,
            self.crypto_range_position,
            self.crypto_volatility,
            self.crypto_momentum,
            self.is_crypto,
            self.is_sports,
            self.is_politics,
            self.top_wallet_agreement,
            self.num_top_wallets_trading,
            self.price_deviation_from_50,
            self.price_extremity,
        ], dtype=np.float64)

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "yes_price", "spread", "mid_price",
            "log_volume", "log_liquidity", "volume_liquidity_ratio",
            "log_hours_to_close", "is_closing_soon",
            "bid_depth", "ask_depth", "book_imbalance", "book_spread",
            "crypto_change_24h", "crypto_range_position",
            "crypto_volatility", "crypto_momentum",
            "is_crypto", "is_sports", "is_politics",
            "top_wallet_agreement", "num_top_wallets_trading",
            "price_deviation_from_50", "price_extremity",
        ]


class FeatureExtractor:
    """Builds feature vectors from market state for the ML model."""

    def extract(
        self,
        market: ActiveMarket,
        crypto_snap: PriceSnapshot | None = None,
        crypto_range: dict | None = None,
        book_data: dict | None = None,
        wallet_consensus: dict | None = None,
    ) -> MarketFeatures:
        f = MarketFeatures(
            market_id=market.condition_id,
            question=market.question,
        )

        # Price features
        f.yes_price = market.yes_price
        f.no_price = market.no_price
        f.spread = market.spread
        f.mid_price = market.mid_price
        f.price_deviation_from_50 = market.yes_price - 0.5
        f.price_extremity = abs(market.yes_price - 0.5)

        # Volume / liquidity
        f.log_volume = math.log10(max(market.volume, 1))
        f.log_liquidity = math.log10(max(market.liquidity, 1))
        f.volume_liquidity_ratio = (
            market.volume / market.liquidity if market.liquidity > 0 else 0
        )

        # Time features
        if market.hours_to_close is not None:
            f.hours_to_close = market.hours_to_close
            f.log_hours_to_close = math.log10(max(market.hours_to_close, 0.1))
            f.is_closing_soon = 1.0 if market.hours_to_close < 24 else 0.0

        # Order book
        if book_data:
            f.bid_depth = book_data.get("bid_depth", 0)
            f.ask_depth = book_data.get("ask_depth", 0)
            total_depth = f.bid_depth + f.ask_depth
            f.book_imbalance = (
                (f.bid_depth - f.ask_depth) / total_depth if total_depth > 0 else 0
            )
            f.book_spread = book_data.get("spread", 0)

        # Crypto
        slug = market.slug.lower()
        question = market.question.lower()
        if any(t in slug or t in question for t in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "crypto"]):
            f.is_crypto = 1.0
            if crypto_snap:
                f.crypto_price = crypto_snap.price
                f.crypto_change_24h = crypto_snap.change_24h_pct
            if crypto_range:
                f.crypto_range_position = crypto_range.get("position", 0.5)
                f.crypto_volatility = crypto_range.get("volatility", 0)
                change = crypto_range.get("change_pct", 0)
                f.crypto_momentum = max(-1, min(1, change / 10.0))

        # Category
        if any(t in slug or t in question for t in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "ufc"]):
            f.is_sports = 1.0
        if any(t in slug or t in question for t in ["president", "election", "trump", "biden", "congress", "senate"]):
            f.is_politics = 1.0

        # Wallet consensus
        if wallet_consensus:
            f.top_wallet_agreement = wallet_consensus.get("yes_fraction", 0.5)
            f.num_top_wallets_trading = wallet_consensus.get("count", 0)

        return f

    def extract_training_sample(
        self,
        trade_dict: dict,
        market_volume: float = 0,
        market_liquidity: float = 0,
    ) -> tuple[np.ndarray, float] | None:
        """
        Extract features from a historical trade for training.
        Returns (feature_vector, label) where label is 1.0 for YES win, 0.0 for NO win.
        """
        side = trade_dict.get("side", "").upper()
        outcome = trade_dict.get("outcome", "")
        price = float(trade_dict.get("price", 0))

        if not outcome or price <= 0:
            return None

        outcome_upper = outcome.upper().strip()
        if outcome_upper == "YES":
            label = 1.0
        elif outcome_upper == "NO":
            label = 0.0
        else:
            return None

        features = np.array([
            price,  # yes_price
            0.02,  # spread (estimated)
            price,  # mid_price
            math.log10(max(market_volume, 1)),
            math.log10(max(market_liquidity, 1)),
            0,  # vol/liq ratio
            3.0,  # log_hours_to_close (estimated)
            0,  # is_closing_soon
            0, 0, 0, 0,  # book features (unavailable in history)
            0, 0.5, 0, 0,  # crypto features
            0, 0, 0,  # category
            0.5, 0,  # wallet consensus
            price - 0.5,  # deviation
            abs(price - 0.5),  # extremity
        ], dtype=np.float64)

        return features, label
