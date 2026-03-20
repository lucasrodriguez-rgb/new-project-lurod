"""
KL-Divergence Correlation Mispricing Scanner.

Measures the "distance" between probability distributions of correlated
markets. High KL-divergence between markets that SHOULD be correlated
signals an arbitrage opportunity.

Formula: D_KL(P||Q) = Σ P_i * log(P_i / Q_i)

Use cases:
- Two candidates in the same election priced inconsistently
- Related geopolitical events (Iran regime fall vs. US-Iran ceasefire)
- Correlated sports outcomes
- Crypto price prediction markets at different timeframes
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import numpy as np
from scipy.stats import entropy
from loguru import logger


@dataclass
class MarketDistribution:
    market_id: str
    question: str
    slug: str
    probs: list[float]  # [p_yes, p_no] or multi-outcome
    volume_24h: float = 0
    liquidity: float = 0
    category: str = ""


@dataclass
class KLMispricing:
    market_a_id: str
    market_a_question: str
    market_b_id: str
    market_b_question: str
    kl_divergence: float       # D_KL(P||Q)
    kl_reverse: float          # D_KL(Q||P)
    kl_symmetric: float        # (D_KL(P||Q) + D_KL(Q||P)) / 2
    prob_a: list[float]
    prob_b: list[float]
    correlation_type: str      # "election", "geopolitical", "crypto", "sports"
    arb_signal: bool           # True if KL > threshold
    suggested_action: str
    estimated_edge_pct: float


class KLScanner:
    """
    Scans for mispricings between correlated Polymarket markets
    using KL-divergence as the distance metric.
    """

    KL_THRESHOLD = 0.05     # flag if symmetric KL > this
    STRONG_SIGNAL = 0.15    # strong arb signal
    MATCH_THRESHOLD = 0.35  # minimum string similarity to consider correlated

    # Groups of keywords that indicate correlated markets
    CORRELATION_GROUPS = [
        # Elections
        ["2028", "president", "nomination", "win", "democratic", "republican"],
        ["2026", "midterm", "senate", "house", "governor"],
        # Geopolitical
        ["iran", "regime", "ceasefire", "military", "strike", "sanctions"],
        ["russia", "ukraine", "war", "peace", "nato"],
        ["israel", "gaza", "hamas", "hezbollah", "netanyahu"],
        ["china", "taiwan", "trade", "tariff"],
        # Crypto
        ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto"],
        # Sports same-league
        ["nba", "basketball", "mvp", "champion"],
        ["nfl", "super bowl", "football"],
        ["premier league", "epl", "soccer"],
    ]

    def scan(
        self,
        markets: list[MarketDistribution],
        custom_pairs: list[tuple[str, str]] | None = None,
    ) -> list[KLMispricing]:
        """
        Scan all market pairs for KL-divergence mispricings.
        Returns pairs sorted by symmetric KL (highest = most mispriced).
        """
        mispricings: list[KLMispricing] = []

        # Auto-detect correlated pairs
        pairs = self._find_correlated_pairs(markets)

        # Add custom pairs
        if custom_pairs:
            market_map = {m.market_id: m for m in markets}
            for a_id, b_id in custom_pairs:
                if a_id in market_map and b_id in market_map:
                    pairs.append((market_map[a_id], market_map[b_id], "custom"))

        logger.info("Analyzing {} correlated market pairs", len(pairs))

        for market_a, market_b, corr_type in pairs:
            result = self._compute_kl(market_a, market_b, corr_type)
            if result and result.kl_symmetric > 0.01:
                mispricings.append(result)

        mispricings.sort(key=lambda x: x.kl_symmetric, reverse=True)

        arb_count = sum(1 for m in mispricings if m.arb_signal)
        logger.info("Found {} mispricings, {} with arb signals", len(mispricings), arb_count)

        return mispricings

    def compute_single_kl(
        self, p: list[float], q: list[float]
    ) -> tuple[float, float, float]:
        """
        Compute KL-divergence between two distributions.
        Returns (kl_pq, kl_qp, symmetric).
        """
        p_arr = np.array(p, dtype=np.float64)
        q_arr = np.array(q, dtype=np.float64)

        # Ensure valid probability distributions
        p_arr = np.clip(p_arr, 1e-10, 1.0)
        q_arr = np.clip(q_arr, 1e-10, 1.0)
        p_arr /= p_arr.sum()
        q_arr /= q_arr.sum()

        kl_pq = float(entropy(p_arr, q_arr))
        kl_qp = float(entropy(q_arr, p_arr))
        symmetric = (kl_pq + kl_qp) / 2

        return kl_pq, kl_qp, symmetric

    def _compute_kl(
        self,
        a: MarketDistribution,
        b: MarketDistribution,
        corr_type: str,
    ) -> KLMispricing | None:
        if len(a.probs) != len(b.probs):
            # Pad shorter distribution
            max_len = max(len(a.probs), len(b.probs))
            p = a.probs + [1e-10] * (max_len - len(a.probs))
            q = b.probs + [1e-10] * (max_len - len(b.probs))
        else:
            p, q = a.probs, b.probs

        kl_pq, kl_qp, symmetric = self.compute_single_kl(p, q)

        if math.isnan(symmetric) or math.isinf(symmetric):
            return None

        arb_signal = symmetric > self.KL_THRESHOLD

        # Determine action based on which market is "cheaper"
        if kl_pq > kl_qp:
            # Market A's distribution is further from Q → B may be more accurate
            action = f"BUY {b.question[:25]} / SELL {a.question[:25]}"
        else:
            action = f"BUY {a.question[:25]} / SELL {b.question[:25]}"

        # Edge estimate: KL-divergence maps roughly to price difference
        edge_pct = min(symmetric * 50, 20.0)  # cap at 20%

        if symmetric > self.STRONG_SIGNAL:
            action = "STRONG: " + action

        return KLMispricing(
            market_a_id=a.market_id,
            market_a_question=a.question,
            market_b_id=b.market_id,
            market_b_question=b.question,
            kl_divergence=round(kl_pq, 6),
            kl_reverse=round(kl_qp, 6),
            kl_symmetric=round(symmetric, 6),
            prob_a=p,
            prob_b=q,
            correlation_type=corr_type,
            arb_signal=arb_signal,
            suggested_action=action,
            estimated_edge_pct=round(edge_pct, 2),
        )

    def _find_correlated_pairs(
        self, markets: list[MarketDistribution]
    ) -> list[tuple[MarketDistribution, MarketDistribution, str]]:
        """Find pairs of markets that should be correlated."""
        pairs: list[tuple[MarketDistribution, MarketDistribution, str]] = []
        seen: set[tuple[str, str]] = set()

        for i, a in enumerate(markets):
            for j, b in enumerate(markets):
                if i >= j:
                    continue
                if (a.market_id, b.market_id) in seen:
                    continue

                corr_type = self._detect_correlation(a, b)
                if corr_type:
                    pairs.append((a, b, corr_type))
                    seen.add((a.market_id, b.market_id))

        return pairs

    def _detect_correlation(
        self, a: MarketDistribution, b: MarketDistribution
    ) -> str | None:
        """Detect if two markets are correlated based on their questions."""
        a_text = (a.question + " " + a.slug).lower()
        b_text = (b.question + " " + b.slug).lower()

        # Same slug prefix (e.g., "btc-updown-5m" vs "btc-updown-15m")
        a_slug_parts = a.slug.lower().split("-")
        b_slug_parts = b.slug.lower().split("-")
        if len(a_slug_parts) > 2 and len(b_slug_parts) > 2:
            if a_slug_parts[:2] == b_slug_parts[:2]:
                return "same_event"

        # Check correlation groups
        for group in self.CORRELATION_GROUPS:
            a_hits = sum(1 for kw in group if kw in a_text)
            b_hits = sum(1 for kw in group if kw in b_text)
            if a_hits >= 2 and b_hits >= 2:
                return self._categorize_group(group)

        # String similarity
        similarity = SequenceMatcher(None, a_text, b_text).ratio()
        if similarity > self.MATCH_THRESHOLD:
            return "similar_question"

        return None

    @staticmethod
    def _categorize_group(group: list[str]) -> str:
        if any(kw in group for kw in ["president", "nomination", "election"]):
            return "election"
        if any(kw in group for kw in ["iran", "russia", "israel", "china"]):
            return "geopolitical"
        if any(kw in group for kw in ["bitcoin", "ethereum", "crypto"]):
            return "crypto"
        if any(kw in group for kw in ["nba", "nfl", "premier"]):
            return "sports"
        return "general"


def print_kl_report(mispricings: list[KLMispricing], top_n: int = 15) -> None:
    """Pretty-print KL-divergence scan results."""
    print(f"\n{'='*100}")
    print(f"  KL-DIVERGENCE MISPRICING SCANNER — TOP {min(top_n, len(mispricings))} PAIRS")
    print(f"{'='*100}")

    for i, m in enumerate(mispricings[:top_n]):
        signal = "***ARB***" if m.arb_signal else "  watch "
        print(f"\n  #{i+1} [{signal}] KL={m.kl_symmetric:.4f} | Type: {m.correlation_type}")
        print(f"    A: {m.market_a_question[:60]}  P={m.prob_a}")
        print(f"    B: {m.market_b_question[:60]}  P={m.prob_b}")
        print(f"    Edge: ~{m.estimated_edge_pct:.1f}% | {m.suggested_action}")

    print(f"\n{'='*100}\n")
