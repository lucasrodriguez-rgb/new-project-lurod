"""
Cross-platform arbitrage detector.
Finds price discrepancies between Polymarket, Kalshi, and sportsbooks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from difflib import SequenceMatcher

from loguru import logger

from polymarket_index.edge.market_scanner import MarketScanner, ActiveMarket
from polymarket_index.arbitrage.odds_feeds import KalshiClient, SportsOddsClient, ExternalOdds, KalshiMarket


@dataclass
class ArbOpportunity:
    polymarket_id: str
    polymarket_question: str
    polymarket_price: float
    polymarket_side: str
    external_source: str
    external_event: str
    external_price: float
    spread: float  # absolute price difference
    arb_pct: float  # guaranteed profit percentage
    confidence: str  # how confident the match is
    action: str  # "buy_poly_sell_ext" or "buy_ext_sell_poly"


class ArbDetector:
    """
    Scans for cross-platform arbitrage opportunities between
    Polymarket and other prediction markets / sportsbooks.
    """

    def __init__(
        self,
        market_scanner: MarketScanner,
        odds_api_key: str = "",
    ) -> None:
        self._scanner = market_scanner
        self._kalshi = KalshiClient()
        self._sports = SportsOddsClient(api_key=odds_api_key)
        self._min_arb_pct = 0.01  # 1% minimum arb

    async def scan(self) -> list[ArbOpportunity]:
        """Scan all sources for arbitrage opportunities."""
        opportunities: list[ArbOpportunity] = []

        # Fetch Polymarket markets
        poly_markets = await self._scanner.scan(min_volume=5000, min_liquidity=500)

        # Check against Kalshi
        kalshi_arbs = await self._check_kalshi(poly_markets)
        opportunities.extend(kalshi_arbs)

        # Check against sportsbooks
        sports_arbs = await self._check_sportsbooks(poly_markets)
        opportunities.extend(sports_arbs)

        opportunities.sort(key=lambda x: x.arb_pct, reverse=True)

        if opportunities:
            logger.info("Found {} arbitrage opportunities", len(opportunities))
            for opp in opportunities[:5]:
                logger.info(
                    "  ARB {:.1%}: {} {} @ {:.3f} vs {} @ {:.3f}",
                    opp.arb_pct, opp.polymarket_side,
                    opp.polymarket_question[:30],
                    opp.polymarket_price,
                    opp.external_source,
                    opp.external_price,
                )

        return opportunities

    async def _check_kalshi(self, poly_markets: list[ActiveMarket]) -> list[ArbOpportunity]:
        """Compare Polymarket prices against Kalshi."""
        try:
            kalshi_markets = await self._kalshi.get_markets(limit=200)
        except Exception:
            return []

        if not kalshi_markets:
            return []

        opportunities: list[ArbOpportunity] = []

        for poly in poly_markets:
            best_match = self._find_best_match(
                poly.question,
                [(k.title, k) for k in kalshi_markets],
            )
            if not best_match:
                continue

            kalshi_market: KalshiMarket = best_match[1]
            match_confidence = best_match[0]

            if match_confidence < 0.5:
                continue

            arb = self._compute_arb(
                poly_yes=poly.yes_price,
                poly_no=poly.no_price,
                ext_yes=kalshi_market.yes_price,
                ext_no=kalshi_market.no_price,
                poly_market=poly,
                ext_source="kalshi",
                ext_event=kalshi_market.title,
                confidence="high" if match_confidence > 0.8 else "medium",
            )
            if arb:
                opportunities.append(arb)

        return opportunities

    async def _check_sportsbooks(self, poly_markets: list[ActiveMarket]) -> list[ArbOpportunity]:
        """Compare Polymarket sports markets against sportsbook odds."""
        sports_markets = [
            m for m in poly_markets
            if any(kw in m.slug.lower() for kw in ["nba", "nfl", "mlb", "nhl", "soccer", "ufc"])
        ]

        if not sports_markets:
            return []

        sports_to_check = set()
        for m in sports_markets:
            slug = m.slug.lower()
            if "nba" in slug:
                sports_to_check.add("basketball_nba")
            elif "nfl" in slug:
                sports_to_check.add("americanfootball_nfl")
            elif "mlb" in slug:
                sports_to_check.add("baseball_mlb")
            elif "nhl" in slug:
                sports_to_check.add("icehockey_nhl")
            elif "soccer" in slug or "epl" in slug:
                sports_to_check.add("soccer_epl")

        all_odds: list[ExternalOdds] = []
        for sport in sports_to_check:
            odds = await self._sports.get_odds(sport=sport)
            all_odds.extend(odds)

        if not all_odds:
            return []

        opportunities: list[ArbOpportunity] = []
        odds_by_event: dict[str, list[ExternalOdds]] = {}
        for o in all_odds:
            odds_by_event.setdefault(o.event_name, []).append(o)

        for poly in sports_markets:
            best_match = self._find_best_match(
                poly.question,
                [(event_name, odds_list) for event_name, odds_list in odds_by_event.items()],
            )
            if not best_match or best_match[0] < 0.4:
                continue

            ext_odds: list[ExternalOdds] = best_match[1]

            for odds in ext_odds:
                ext_yes = odds.price
                ext_no = 1.0 - ext_yes

                arb = self._compute_arb(
                    poly_yes=poly.yes_price,
                    poly_no=poly.no_price,
                    ext_yes=ext_yes,
                    ext_no=ext_no,
                    poly_market=poly,
                    ext_source=odds.source,
                    ext_event=f"{odds.event_name} ({odds.outcome})",
                    confidence="medium" if best_match[0] > 0.6 else "low",
                )
                if arb:
                    opportunities.append(arb)

        return opportunities

    def _compute_arb(
        self,
        poly_yes: float,
        poly_no: float,
        ext_yes: float,
        ext_no: float,
        poly_market: ActiveMarket,
        ext_source: str,
        ext_event: str,
        confidence: str,
    ) -> ArbOpportunity | None:
        """
        Check for arbitrage between two platforms.
        Arb exists when: poly_yes + ext_no < 1 OR poly_no + ext_yes < 1
        """
        # Buy YES on Poly, buy NO on external
        cost_1 = poly_yes + ext_no
        if cost_1 < (1.0 - self._min_arb_pct):
            arb_pct = 1.0 - cost_1
            return ArbOpportunity(
                polymarket_id=poly_market.condition_id,
                polymarket_question=poly_market.question,
                polymarket_price=poly_yes,
                polymarket_side="YES",
                external_source=ext_source,
                external_event=ext_event,
                external_price=ext_no,
                spread=abs(poly_yes - (1.0 - ext_no)),
                arb_pct=round(arb_pct, 4),
                confidence=confidence,
                action="buy_poly_YES_buy_ext_NO",
            )

        # Buy NO on Poly, buy YES on external
        cost_2 = poly_no + ext_yes
        if cost_2 < (1.0 - self._min_arb_pct):
            arb_pct = 1.0 - cost_2
            return ArbOpportunity(
                polymarket_id=poly_market.condition_id,
                polymarket_question=poly_market.question,
                polymarket_price=poly_no,
                polymarket_side="NO",
                external_source=ext_source,
                external_event=ext_event,
                external_price=ext_yes,
                spread=abs(poly_no - (1.0 - ext_yes)),
                arb_pct=round(arb_pct, 4),
                confidence=confidence,
                action="buy_poly_NO_buy_ext_YES",
            )

        return None

    @staticmethod
    def _find_best_match(
        query: str, candidates: list[tuple[str, any]]
    ) -> tuple[float, any] | None:
        """Find the best string match from candidates using fuzzy matching."""
        if not candidates:
            return None

        best_score = 0.0
        best_data = None

        query_lower = query.lower()
        for name, data in candidates:
            score = SequenceMatcher(None, query_lower, name.lower()).ratio()
            if score > best_score:
                best_score = score
                best_data = data

        if best_score < 0.3:
            return None
        return (best_score, best_data)

    async def close(self) -> None:
        await self._kalshi.close()
        await self._sports.close()
