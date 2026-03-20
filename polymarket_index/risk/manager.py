"""
Risk management system with circuit breakers, exposure limits,
and drawdown protection.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from loguru import logger

from polymarket_index.config import settings


@dataclass
class RiskLimits:
    max_daily_loss_pct: float = 5.0
    max_total_drawdown_pct: float = 15.0
    max_single_position_pct: float = 5.0
    max_market_exposure_pct: float = 10.0
    max_category_exposure_pct: float = 25.0
    max_open_positions: int = 50
    max_correlated_positions: int = 5
    cooldown_after_loss_streak: int = 3
    cooldown_duration_minutes: int = 30


@dataclass
class PortfolioSnapshot:
    timestamp: str
    total_value: float
    cash: float
    positions: list[dict]
    realized_pnl_today: float
    peak_value: float


class RiskManager:
    """
    Enforces risk limits and circuit breakers.
    Must approve every trade before execution.
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        limits: RiskLimits | None = None,
    ) -> None:
        self._limits = limits or RiskLimits()
        self._initial_capital = initial_capital
        self._peak_value = initial_capital
        self._daily_pnl = 0.0
        self._daily_start_value = initial_capital
        self._loss_streak = 0
        self._cooldown_until: dt.datetime | None = None
        self._last_reset_date: str = ""
        self._position_history: list[dict] = []

    def check_trade(
        self,
        size_usdc: float,
        market_id: str,
        category: str,
        current_value: float,
        open_positions: list[dict],
    ) -> tuple[bool, str]:
        """
        Check if a proposed trade passes all risk checks.
        Returns (approved, reason).
        """
        self._maybe_reset_daily(current_value)
        self._peak_value = max(self._peak_value, current_value)

        # Circuit breaker: cooldown after loss streak
        if self._cooldown_until:
            now = dt.datetime.now(dt.timezone.utc)
            if now < self._cooldown_until:
                remaining = (self._cooldown_until - now).total_seconds() / 60
                return False, f"cooldown active ({remaining:.0f}m remaining after {self._limits.cooldown_after_loss_streak} consecutive losses)"
            self._cooldown_until = None

        # Max daily loss
        daily_pnl_pct = (
            (current_value - self._daily_start_value) / self._daily_start_value * 100
        )
        if daily_pnl_pct < -self._limits.max_daily_loss_pct:
            return False, f"daily loss limit hit ({daily_pnl_pct:.1f}% < -{self._limits.max_daily_loss_pct}%)"

        # Max drawdown from peak
        drawdown_pct = (
            (self._peak_value - current_value) / self._peak_value * 100
        )
        if drawdown_pct > self._limits.max_total_drawdown_pct:
            return False, f"max drawdown hit ({drawdown_pct:.1f}% > {self._limits.max_total_drawdown_pct}%)"

        # Single position size limit
        position_pct = size_usdc / current_value * 100 if current_value > 0 else 100
        if position_pct > self._limits.max_single_position_pct:
            return False, f"position too large ({position_pct:.1f}% > {self._limits.max_single_position_pct}%)"

        # Max open positions
        if len(open_positions) >= self._limits.max_open_positions:
            return False, f"max positions reached ({len(open_positions)})"

        # Market exposure limit
        market_exposure = sum(
            p.get("size_usdc", 0) for p in open_positions
            if p.get("market_id") == market_id
        )
        market_exposure_pct = (market_exposure + size_usdc) / current_value * 100 if current_value > 0 else 100
        if market_exposure_pct > self._limits.max_market_exposure_pct:
            return False, f"market exposure limit ({market_exposure_pct:.1f}% > {self._limits.max_market_exposure_pct}%)"

        # Category exposure limit
        category_exposure = sum(
            p.get("size_usdc", 0) for p in open_positions
            if p.get("category", "") == category
        )
        category_exposure_pct = (category_exposure + size_usdc) / current_value * 100 if current_value > 0 else 100
        if category_exposure_pct > self._limits.max_category_exposure_pct:
            return False, f"category exposure limit ({category_exposure_pct:.1f}% > {self._limits.max_category_exposure_pct}%)"

        return True, "approved"

    def record_trade_result(self, pnl: float) -> None:
        """Record a closed trade result for streak tracking."""
        if pnl < 0:
            self._loss_streak += 1
            if self._loss_streak >= self._limits.cooldown_after_loss_streak:
                self._cooldown_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                    minutes=self._limits.cooldown_duration_minutes
                )
                logger.warning(
                    "Loss streak of {} — entering {}m cooldown",
                    self._loss_streak, self._limits.cooldown_duration_minutes,
                )
        else:
            self._loss_streak = 0

        self._daily_pnl += pnl

    def adjust_position_size(
        self,
        proposed_size: float,
        current_value: float,
        open_positions: list[dict],
    ) -> float:
        """
        Adjust position size based on current risk state.
        Reduces size when approaching limits.
        """
        drawdown_pct = (
            (self._peak_value - current_value) / self._peak_value * 100
            if self._peak_value > 0 else 0
        )

        # Scale down as drawdown increases
        if drawdown_pct > self._limits.max_total_drawdown_pct * 0.5:
            scale = 1.0 - (drawdown_pct / self._limits.max_total_drawdown_pct)
            proposed_size *= max(0.25, scale)

        # Scale down with more open positions
        n_pos = len(open_positions)
        if n_pos > self._limits.max_open_positions * 0.7:
            scale = 1.0 - (n_pos / self._limits.max_open_positions)
            proposed_size *= max(0.25, scale)

        # Scale down after losses
        if self._loss_streak >= 2:
            proposed_size *= 0.5

        # Hard cap
        max_size = current_value * self._limits.max_single_position_pct / 100
        proposed_size = min(proposed_size, max_size)

        return max(proposed_size, 1.0)

    def get_status(self) -> dict:
        return {
            "peak_value": self._peak_value,
            "daily_pnl": self._daily_pnl,
            "loss_streak": self._loss_streak,
            "in_cooldown": self._cooldown_until is not None,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
        }

    def _maybe_reset_daily(self, current_value: float) -> None:
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._daily_pnl = 0.0
            self._daily_start_value = current_value
            self._last_reset_date = today
