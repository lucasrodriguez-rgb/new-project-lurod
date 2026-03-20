from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from polymarket_index.config import settings
from polymarket_index.db.models import Trade, async_session
from polymarket_index.db import queries


@dataclass
class WalletMetrics:
    address: str
    roi: float  # realized PnL / capital deployed
    win_rate: float  # fraction of profitable resolved trades
    sharpe: float  # daily PnL Sharpe ratio (rf=0)
    volume: float  # total USDC wagered
    diversity: float  # market diversity score (0-1)
    trade_count: int
    lookback_days: int


@dataclass
class WalletScore:
    address: str
    total_score: float  # 0–100
    roi_score: float
    sharpe_score: float
    win_rate_score: float
    volume_score: float
    diversity_score: float
    metrics: WalletMetrics


class PerformanceScorer:
    """
    Compute per-wallet performance metrics and a composite score
    over configurable lookback windows.
    """

    def __init__(self, lookback_days: int | None = None) -> None:
        self.lookback_days = lookback_days or settings.score_lookback_days

    async def score_wallet(self, address: str) -> WalletScore | None:
        """Score a single wallet."""
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=self.lookback_days
        )

        async with async_session() as session:
            trades = list(await queries.get_wallet_trades(session, address, since=since))

        if len(trades) < 2:
            return None

        metrics = self._compute_metrics(address, trades)
        return self._compute_score(metrics)

    async def score_all_wallets(self) -> list[WalletScore]:
        """Score every wallet in the database."""
        async with async_session() as session:
            wallets = await queries.get_all_wallets(session)

        scores: list[WalletScore] = []
        for wallet in wallets:
            score = await self.score_wallet(wallet.address)
            if score is not None:
                scores.append(score)

        scores.sort(key=lambda s: s.total_score, reverse=True)
        logger.info("Scored {} wallets", len(scores))
        return scores

    def _compute_metrics(
        self, address: str, trades: list[Trade]
    ) -> WalletMetrics:
        now = dt.datetime.now(dt.timezone.utc)
        decay_lambda = math.log(2) / 14  # half-life of 14 days

        total_capital = 0.0
        total_pnl = 0.0
        wins = 0
        resolved = 0
        daily_pnl: dict[str, float] = {}
        markets_seen: set[str] = set()
        weighted_sizes: list[float] = []

        for trade in trades:
            trade_ts = trade.timestamp
            if trade_ts.tzinfo is None:
                trade_ts = trade_ts.replace(tzinfo=dt.timezone.utc)

            age_days = (now - trade_ts).total_seconds() / 86400
            recency_weight = math.exp(-decay_lambda * age_days)

            capital = trade.size * trade.price
            total_capital += capital

            markets_seen.add(trade.market_id)

            if trade.pnl is not None:
                pnl_weighted = trade.pnl * recency_weight
                total_pnl += pnl_weighted
                resolved += 1
                if trade.pnl > 0:
                    wins += 1

                day_key = trade_ts.strftime("%Y-%m-%d")
                daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + pnl_weighted
            elif trade.outcome is not None:
                if trade.outcome == "win":
                    estimated_pnl = capital * (1.0 / trade.price - 1.0)
                    total_pnl += estimated_pnl * recency_weight
                    wins += 1
                    resolved += 1
                    day_key = trade_ts.strftime("%Y-%m-%d")
                    daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) + estimated_pnl * recency_weight
                elif trade.outcome == "loss":
                    total_pnl -= capital * recency_weight
                    resolved += 1
                    day_key = trade_ts.strftime("%Y-%m-%d")
                    daily_pnl[day_key] = daily_pnl.get(day_key, 0.0) - capital * recency_weight

            weighted_sizes.append(capital * recency_weight)

        roi = total_pnl / total_capital if total_capital > 0 else 0.0
        win_rate = wins / resolved if resolved > 0 else 0.0

        # Sharpe ratio from daily PnL
        if len(daily_pnl) >= 2:
            returns = list(daily_pnl.values())
            mean_r = float(np.mean(returns))
            std_r = float(np.std(returns, ddof=1))
            sharpe = (mean_r / std_r * math.sqrt(365)) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        volume = sum(t.size * t.price for t in trades)

        # Diversity: penalize wallets concentrated in few markets
        n_markets = len(markets_seen)
        n_trades = len(trades)
        if n_trades > 0 and n_markets > 0:
            market_counts = {}
            for t in trades:
                market_counts[t.market_id] = market_counts.get(t.market_id, 0) + 1
            fracs = [c / n_trades for c in market_counts.values()]
            hhi = sum(f * f for f in fracs)
            diversity = 1.0 - hhi  # 0 = one market, approaches 1 for many
        else:
            diversity = 0.0

        return WalletMetrics(
            address=address,
            roi=roi,
            win_rate=win_rate,
            sharpe=sharpe,
            volume=volume,
            diversity=diversity,
            trade_count=len(trades),
            lookback_days=self.lookback_days,
        )

    def _compute_score(self, m: WalletMetrics) -> WalletScore:
        roi_score = self._normalize_score(m.roi, floor=-0.5, ceiling=2.0)
        sharpe_score = self._normalize_score(m.sharpe, floor=-1.0, ceiling=5.0)
        win_rate_score = self._normalize_score(m.win_rate, floor=0.3, ceiling=0.85)
        volume_score = self._normalize_score(
            math.log10(max(m.volume, 1)), floor=2.0, ceiling=6.0
        )
        diversity_score = self._normalize_score(m.diversity, floor=0.0, ceiling=0.9)

        total = (
            settings.score_weight_roi * roi_score
            + settings.score_weight_sharpe * sharpe_score
            + settings.score_weight_win_rate * win_rate_score
            + settings.score_weight_volume * volume_score
            + settings.score_weight_diversity * diversity_score
        )

        return WalletScore(
            address=m.address,
            total_score=round(total, 2),
            roi_score=round(roi_score, 2),
            sharpe_score=round(sharpe_score, 2),
            win_rate_score=round(win_rate_score, 2),
            volume_score=round(volume_score, 2),
            diversity_score=round(diversity_score, 2),
            metrics=m,
        )

    @staticmethod
    def _normalize_score(value: float, floor: float, ceiling: float) -> float:
        """Linearly map a value from [floor, ceiling] to [0, 100], clamped."""
        if ceiling <= floor:
            return 50.0
        normalized = (value - floor) / (ceiling - floor) * 100.0
        return max(0.0, min(100.0, normalized))
