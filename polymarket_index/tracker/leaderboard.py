from __future__ import annotations

import datetime as dt

from loguru import logger

from polymarket_index.config import settings
from polymarket_index.db.models import async_session
from polymarket_index.db import queries
from polymarket_index.tracker.performance import PerformanceScorer, WalletScore


class LeaderboardEvent:
    """Simple event emitted when wallets enter/exit the top index."""

    def __init__(self, event_type: str, address: str, rank: int | None, score: float):
        self.event_type = event_type  # "entered" | "exited" | "rank_changed"
        self.address = address
        self.rank = rank
        self.score = score
        self.timestamp = dt.datetime.now(dt.timezone.utc)

    def __repr__(self) -> str:
        return (
            f"LeaderboardEvent({self.event_type}, "
            f"{self.address[:10]}..., rank={self.rank}, score={self.score:.1f})"
        )


class Leaderboard:
    """
    Maintains a ranked index of top-performing wallets.
    Re-scores periodically and emits events on index changes.
    """

    def __init__(self, scorer: PerformanceScorer | None = None) -> None:
        self._scorer = scorer or PerformanceScorer()
        self._top_n = settings.top_n_wallets
        self._event_handlers: list = []

    def on_event(self, handler) -> None:
        self._event_handlers.append(handler)

    async def _emit(self, event: LeaderboardEvent) -> None:
        logger.info("Leaderboard event: {}", event)
        for handler in self._event_handlers:
            try:
                result = handler(event)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.error("Event handler failed: {}", exc)

    async def update(self) -> list[LeaderboardEvent]:
        """
        Re-score all wallets, update rankings, and detect index changes.
        Returns list of events that occurred.
        """
        scores = await self._scorer.score_all_wallets()
        if not scores:
            logger.warning("No wallet scores computed, skipping leaderboard update")
            return []

        async with async_session() as session:
            old_indexed = {
                w.address for w in await queries.get_indexed_wallets(session)
            }

        now = dt.datetime.now(dt.timezone.utc)
        new_indexed: set[str] = set()
        events: list[LeaderboardEvent] = []

        async with async_session() as session:
            async with session.begin():
                for rank_idx, ws in enumerate(scores):
                    rank = rank_idx + 1
                    in_index = rank <= self._top_n and ws.total_score >= settings.min_score_for_index

                    index_entered_at = None
                    if in_index:
                        new_indexed.add(ws.address)
                        if ws.address not in old_indexed:
                            index_entered_at = now
                            events.append(
                                LeaderboardEvent("entered", ws.address, rank, ws.total_score)
                            )
                        else:
                            existing = await queries.get_wallet(session, ws.address)
                            if existing and existing.index_entered_at:
                                index_entered_at = existing.index_entered_at

                    await queries.update_wallet_score(
                        session,
                        ws.address,
                        score=ws.total_score,
                        rank=rank if in_index else None,
                        in_index=in_index,
                        index_entered_at=index_entered_at,
                    )

                exited = old_indexed - new_indexed
                for addr in exited:
                    events.append(
                        LeaderboardEvent("exited", addr, None, 0.0)
                    )
                    await queries.update_wallet_score(
                        session, addr, score=0.0, rank=None, in_index=False
                    )

        for event in events:
            await self._emit(event)

        logger.info(
            "Leaderboard updated: {} wallets scored, {} in index, {} entered, {} exited",
            len(scores),
            len(new_indexed),
            sum(1 for e in events if e.event_type == "entered"),
            sum(1 for e in events if e.event_type == "exited"),
        )

        return events

    async def snapshot(self) -> None:
        """Save current leaderboard state to DB."""
        async with async_session() as session:
            async with session.begin():
                indexed = await queries.get_indexed_wallets(session)
                wallet_data = [
                    {
                        "address": w.address,
                        "score": w.current_score,
                        "rank": w.rank,
                    }
                    for w in indexed
                ]
                await queries.save_leaderboard_snapshot(session, wallet_data)

        logger.info("Leaderboard snapshot saved ({} wallets)", len(wallet_data))

    async def get_top_wallets(self) -> list[dict]:
        """Return current top-N wallet data."""
        async with async_session() as session:
            indexed = await queries.get_indexed_wallets(session)
            return [
                {
                    "address": w.address,
                    "score": w.current_score,
                    "rank": w.rank,
                    "last_active": w.last_active.isoformat() if w.last_active else None,
                    "total_trades": w.total_trades,
                    "in_index_since": (
                        w.index_entered_at.isoformat() if w.index_entered_at else None
                    ),
                }
                for w in indexed
            ]

    async def flag_inactive_wallets(self) -> list[str]:
        """
        Remove wallets from the index if they've been inactive
        longer than the configured threshold.
        """
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=settings.inactive_days_threshold
        )
        flagged: list[str] = []

        async with async_session() as session:
            async with session.begin():
                indexed = await queries.get_indexed_wallets(session)
                for w in indexed:
                    if w.last_active and w.last_active < cutoff:
                        await queries.update_wallet_score(
                            session,
                            w.address,
                            score=w.current_score,
                            rank=None,
                            in_index=False,
                        )
                        flagged.append(w.address)

        if flagged:
            logger.info("Flagged {} inactive wallets for removal", len(flagged))
            for addr in flagged:
                await self._emit(
                    LeaderboardEvent("exited", addr, None, 0.0)
                )

        return flagged
