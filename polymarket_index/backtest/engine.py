"""
Backtest engine that replays historical trades through the scoring,
signal detection, validation, and copy-trading pipeline.

Simulates a virtual portfolio and tracks PnL over time.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from loguru import logger

from polymarket_index.config import settings
from polymarket_index.backtest.data_collector import WalletHistory


@dataclass
class BacktestTrade:
    timestamp: dt.datetime
    wallet_address: str
    market_id: str
    side: str
    size: float
    price: float
    outcome: str | None


@dataclass
class CopyTradeSim:
    """A simulated copy trade in the virtual portfolio."""
    timestamp: dt.datetime
    source_wallet: str
    source_rank: int
    market_id: str
    side: str
    entry_price: float
    size_usdc: float
    outcome: str | None = None
    exit_price: float | None = None
    pnl: float = 0.0
    resolved: bool = False


@dataclass
class DailySnapshot:
    date: str
    portfolio_value: float
    cash: float
    open_positions: int
    cumulative_pnl: float
    daily_pnl: float
    total_trades: int
    win_count: int
    loss_count: int


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    initial_capital: float
    final_value: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    total_copy_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    profit_factor: float
    best_day_pnl: float
    worst_day_pnl: float
    avg_daily_pnl: float
    daily_snapshots: list[DailySnapshot]
    top_wallets_followed: list[dict]
    config_used: dict


class BacktestEngine:
    """
    Replays historical wallet trades to simulate what copy-trading
    the top-scored wallets would have produced.

    Steps:
    1. Split data into scoring_window (to build the index) + trading_window
    2. Score wallets using only data from the scoring window
    3. Walk forward day-by-day through the trading window
    4. For each day: find new trades from top wallets → validate → simulate copy
    5. Resolve positions when market outcomes are known
    6. Track portfolio value, PnL, and performance metrics
    """

    def __init__(
        self,
        wallet_histories: dict[str, WalletHistory],
        initial_capital: float = 10_000.0,
        scoring_lookback_days: int = 30,
        top_n: int = 20,
        base_trade_pct: float = 0.01,
        min_liquidity: float = 5_000.0,
        max_crowding: int = 3,
    ) -> None:
        self._histories = wallet_histories
        self._initial_capital = initial_capital
        self._scoring_lookback = scoring_lookback_days
        self._top_n = top_n
        self._base_trade_pct = base_trade_pct
        self._min_liquidity = min_liquidity
        self._max_crowding = max_crowding

        self._all_trades: list[BacktestTrade] = []
        self._portfolio_cash: float = initial_capital
        self._open_positions: list[CopyTradeSim] = []
        self._closed_positions: list[CopyTradeSim] = []
        self._daily_snapshots: list[DailySnapshot] = []

    def run(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> BacktestResult:
        """
        Run the full backtest. Dates are ISO format strings (YYYY-MM-DD).
        If not provided, uses the full range of available data.
        """
        self._parse_all_trades()

        if not self._all_trades:
            raise ValueError("No trades found in wallet histories")

        all_dates = sorted({t.timestamp.date() for t in self._all_trades})

        if start_date:
            sim_start = dt.date.fromisoformat(start_date)
        else:
            sim_start = all_dates[0] + dt.timedelta(days=self._scoring_lookback)

        if end_date:
            sim_end = dt.date.fromisoformat(end_date)
        else:
            sim_end = all_dates[-1]

        if sim_start >= sim_end:
            raise ValueError(
                f"Start date {sim_start} must be before end date {sim_end}. "
                f"Need at least {self._scoring_lookback} days of history before start."
            )

        logger.info("=" * 60)
        logger.info("BACKTEST: {} → {}", sim_start, sim_end)
        logger.info(
            "Capital: ${:,.0f} | Top-N: {} | Scoring window: {}d",
            self._initial_capital,
            self._top_n,
            self._scoring_lookback,
        )
        logger.info("{} wallets, {} total trades", len(self._histories), len(self._all_trades))
        logger.info("=" * 60)

        current = sim_start
        prev_value = self._initial_capital
        cumulative_pnl = 0.0
        total_copies = 0
        wins = 0
        losses = 0

        while current <= sim_end:
            scoring_start = current - dt.timedelta(days=self._scoring_lookback)
            wallet_scores = self._score_wallets(scoring_start, current)
            top_wallets = wallet_scores[: self._top_n]
            top_addresses = {ws["address"] for ws in top_wallets}

            day_trades = [
                t
                for t in self._all_trades
                if t.timestamp.date() == current and t.wallet_address in top_addresses
            ]

            day_copies = 0
            for trade in day_trades:
                if not self._validate_trade_for_backtest(trade, top_addresses):
                    continue

                wallet_rank = next(
                    (
                        i + 1
                        for i, ws in enumerate(top_wallets)
                        if ws["address"] == trade.wallet_address
                    ),
                    self._top_n,
                )

                size_pct = self._compute_size(wallet_rank, trade, top_addresses)
                size_usdc = self._portfolio_value() * size_pct

                if size_usdc < 1.0 or size_usdc > self._portfolio_cash:
                    continue

                copy = CopyTradeSim(
                    timestamp=trade.timestamp,
                    source_wallet=trade.wallet_address,
                    source_rank=wallet_rank,
                    market_id=trade.market_id,
                    side=trade.side,
                    entry_price=trade.price,
                    size_usdc=size_usdc,
                    outcome=trade.outcome,
                )

                self._portfolio_cash -= size_usdc
                self._open_positions.append(copy)
                day_copies += 1
                total_copies += 1

            self._resolve_positions(current)

            daily_pnl_val = self._portfolio_value() - prev_value
            cumulative_pnl += daily_pnl_val

            for p in self._closed_positions:
                if p.resolved and p.timestamp.date() == current:
                    if p.pnl > 0:
                        wins += 1
                    elif p.pnl < 0:
                        losses += 1

            self._daily_snapshots.append(
                DailySnapshot(
                    date=current.isoformat(),
                    portfolio_value=self._portfolio_value(),
                    cash=self._portfolio_cash,
                    open_positions=len(self._open_positions),
                    cumulative_pnl=cumulative_pnl,
                    daily_pnl=daily_pnl_val,
                    total_trades=total_copies,
                    win_count=wins,
                    loss_count=losses,
                )
            )

            prev_value = self._portfolio_value()
            current += dt.timedelta(days=1)

        self._force_resolve_remaining()

        return self._compile_result(sim_start, sim_end, wallet_scores)

    # ── Internals ───────────────────────────────────────────────────────

    def _parse_all_trades(self) -> None:
        for addr, history in self._histories.items():
            for t in history.trades:
                ts = _parse_ts(t.get("timestamp", ""))
                if ts is None:
                    continue
                self._all_trades.append(
                    BacktestTrade(
                        timestamp=ts,
                        wallet_address=addr.lower(),
                        market_id=t.get("market_id", ""),
                        side=t.get("side", "").upper(),
                        size=float(t.get("size", 0)),
                        price=float(t.get("price", 0)),
                        outcome=t.get("outcome"),
                    )
                )
        self._all_trades.sort(key=lambda t: t.timestamp)

    def _score_wallets(
        self, window_start: dt.date, window_end: dt.date
    ) -> list[dict]:
        """Score all wallets using only trades within the scoring window."""
        wallet_metrics: list[dict] = []

        for addr, history in self._histories.items():
            trades_in_window = [
                t
                for t in history.trades
                if _trade_in_range(t, window_start, window_end)
            ]

            if len(trades_in_window) < 3:
                continue

            total_capital = 0.0
            total_pnl = 0.0
            wins = 0
            resolved = 0
            markets: set[str] = set()
            daily_pnl: dict[str, float] = {}

            for t in trades_in_window:
                capital = float(t.get("size", 0)) * float(t.get("price", 0))
                total_capital += capital
                markets.add(t.get("market_id", ""))

                outcome = t.get("outcome")
                if outcome == "win":
                    price = float(t.get("price", 0.5))
                    pnl = capital * (1.0 / price - 1.0) if price > 0 else 0
                    total_pnl += pnl
                    wins += 1
                    resolved += 1
                    day = t.get("timestamp", "")[:10]
                    daily_pnl[day] = daily_pnl.get(day, 0) + pnl
                elif outcome == "loss":
                    total_pnl -= capital
                    resolved += 1
                    day = t.get("timestamp", "")[:10]
                    daily_pnl[day] = daily_pnl.get(day, 0) - capital

            roi = total_pnl / total_capital if total_capital > 0 else 0
            win_rate = wins / resolved if resolved > 0 else 0

            if len(daily_pnl) >= 2:
                returns = list(daily_pnl.values())
                mean_r = float(np.mean(returns))
                std_r = float(np.std(returns, ddof=1))
                sharpe = (mean_r / std_r * math.sqrt(365)) if std_r > 0 else 0
            else:
                sharpe = 0

            volume = total_capital
            n_trades = len(trades_in_window)
            n_markets = len(markets)
            if n_trades > 0 and n_markets > 0:
                counts = {}
                for t in trades_in_window:
                    mid = t.get("market_id", "")
                    counts[mid] = counts.get(mid, 0) + 1
                hhi = sum((c / n_trades) ** 2 for c in counts.values())
                diversity = 1.0 - hhi
            else:
                diversity = 0

            def _norm(v: float, lo: float, hi: float) -> float:
                if hi <= lo:
                    return 50.0
                return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))

            score = (
                0.35 * _norm(roi, -0.5, 2.0)
                + 0.25 * _norm(sharpe, -1.0, 5.0)
                + 0.20 * _norm(win_rate, 0.3, 0.85)
                + 0.10 * _norm(math.log10(max(volume, 1)), 2.0, 6.0)
                + 0.10 * _norm(diversity, 0.0, 0.9)
            )

            wallet_metrics.append(
                {
                    "address": addr,
                    "score": round(score, 2),
                    "roi": round(roi, 4),
                    "win_rate": round(win_rate, 4),
                    "sharpe": round(sharpe, 2),
                    "volume": round(volume, 2),
                    "diversity": round(diversity, 3),
                    "trade_count": n_trades,
                }
            )

        wallet_metrics.sort(key=lambda w: w["score"], reverse=True)
        return wallet_metrics

    def _validate_trade_for_backtest(
        self,
        trade: BacktestTrade,
        top_addresses: set[str],
    ) -> bool:
        if trade.price <= 0 or trade.price >= 1.0:
            return False
        if trade.size <= 0:
            return False

        holding_count = sum(
            1
            for p in self._open_positions
            if p.market_id == trade.market_id and p.side == trade.side
        )
        if holding_count >= self._max_crowding:
            return False

        return True

    def _compute_size(
        self,
        wallet_rank: int,
        trade: BacktestTrade,
        top_addresses: set[str],
    ) -> float:
        base = self._base_trade_pct

        agreement = sum(
            1
            for t in self._all_trades
            if (
                t.market_id == trade.market_id
                and t.side == trade.side
                and t.wallet_address in top_addresses
                and t.wallet_address != trade.wallet_address
                and abs((t.timestamp - trade.timestamp).total_seconds()) < 86400
            )
        )

        if wallet_rank <= 10 and agreement >= 2:
            return base * 2

        market_ids = {p.market_id for p in self._open_positions}
        if len(market_ids) > 0:
            concentration = sum(
                1 for p in self._open_positions if p.market_id == trade.market_id
            ) / len(self._open_positions)
            if concentration > 0.3:
                return base * 0.5

        return base

    def _resolve_positions(self, current_date: dt.date) -> None:
        """Resolve positions whose outcome is known by current_date."""
        still_open: list[CopyTradeSim] = []

        for pos in self._open_positions:
            if pos.outcome in ("win", "loss"):
                if pos.outcome == "win":
                    payout = pos.size_usdc / pos.entry_price
                    pos.pnl = payout - pos.size_usdc
                    pos.exit_price = 1.0
                else:
                    pos.pnl = -pos.size_usdc
                    pos.exit_price = 0.0

                pos.resolved = True
                self._portfolio_cash += pos.size_usdc + pos.pnl
                self._closed_positions.append(pos)
            else:
                still_open.append(pos)

        self._open_positions = still_open

    def _force_resolve_remaining(self) -> None:
        """At end of backtest, mark-to-market any unresolved positions."""
        for pos in self._open_positions:
            pos.pnl = 0.0
            pos.resolved = False
            self._portfolio_cash += pos.size_usdc
            self._closed_positions.append(pos)
        self._open_positions = []

    def _portfolio_value(self) -> float:
        open_value = sum(p.size_usdc for p in self._open_positions)
        return self._portfolio_cash + open_value

    def _compile_result(
        self,
        start: dt.date,
        end: dt.date,
        final_scores: list[dict],
    ) -> BacktestResult:
        final_val = self._portfolio_value()
        total_return = (final_val - self._initial_capital) / self._initial_capital
        days = (end - start).days or 1
        ann_return = (1 + total_return) ** (365 / days) - 1

        resolved = [p for p in self._closed_positions if p.resolved]
        wins = [p for p in resolved if p.pnl > 0]
        losses = [p for p in resolved if p.pnl < 0]
        win_rate = len(wins) / len(resolved) if resolved else 0

        avg_win = sum(p.pnl for p in wins) / len(wins) if wins else 0
        avg_loss = sum(p.pnl for p in losses) / len(losses) if losses else 0
        gross_profit = sum(p.pnl for p in wins)
        gross_loss = abs(sum(p.pnl for p in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        daily_pnls = [s.daily_pnl for s in self._daily_snapshots]
        if len(daily_pnls) >= 2:
            mean_d = float(np.mean(daily_pnls))
            std_d = float(np.std(daily_pnls, ddof=1))
            sharpe = (mean_d / std_d * math.sqrt(365)) if std_d > 0 else 0
        else:
            sharpe = 0

        peak = self._initial_capital
        max_dd = 0.0
        for s in self._daily_snapshots:
            peak = max(peak, s.portfolio_value)
            dd = (peak - s.portfolio_value) / peak
            max_dd = max(max_dd, dd)

        return BacktestResult(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            initial_capital=self._initial_capital,
            final_value=round(final_val, 2),
            total_return_pct=round(total_return * 100, 2),
            annualized_return_pct=round(ann_return * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            win_rate=round(win_rate * 100, 2),
            total_copy_trades=len(resolved),
            winning_trades=len(wins),
            losing_trades=len(losses),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            best_day_pnl=round(max(daily_pnls) if daily_pnls else 0, 2),
            worst_day_pnl=round(min(daily_pnls) if daily_pnls else 0, 2),
            avg_daily_pnl=round(float(np.mean(daily_pnls)) if daily_pnls else 0, 2),
            daily_snapshots=self._daily_snapshots,
            top_wallets_followed=final_scores[: self._top_n],
            config_used={
                "initial_capital": self._initial_capital,
                "scoring_lookback_days": self._scoring_lookback,
                "top_n": self._top_n,
                "base_trade_pct": self._base_trade_pct,
                "min_liquidity": self._min_liquidity,
                "max_crowding": self._max_crowding,
            },
        )


# ── Helpers ─────────────────────────────────────────────────────────────


def _parse_ts(ts: str) -> dt.datetime | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def _trade_in_range(
    trade: dict, start: dt.date, end: dt.date
) -> bool:
    ts = _parse_ts(trade.get("timestamp", ""))
    if ts is None:
        return False
    return start <= ts.date() <= end
