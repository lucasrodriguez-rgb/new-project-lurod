"""
Virtual portfolio for live paper trading.
Tracks positions, cash, P&L, and trade history in memory + periodic JSON snapshots.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class PaperPosition:
    id: str
    market_id: str
    market_title: str
    side: str
    entry_price: float
    size_usdc: float
    shares: float
    source_wallet: str
    source_rank: int
    opened_at: str
    current_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class ClosedPosition:
    id: str
    market_id: str
    market_title: str
    side: str
    entry_price: float
    exit_price: float
    size_usdc: float
    pnl: float
    pnl_pct: float
    source_wallet: str
    source_rank: int
    opened_at: str
    closed_at: str
    hold_time_minutes: float
    won: bool


@dataclass
class TradeSignal:
    timestamp: str
    wallet: str
    wallet_rank: int
    market_id: str
    market_title: str
    side: str
    size_usdc: float
    price: float
    action: str  # "COPY" | "SKIP"
    skip_reason: str = ""


@dataclass
class PaperPortfolio:
    initial_capital: float = 10_000.0
    cash: float = 10_000.0
    open_positions: list[PaperPosition] = field(default_factory=list)
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    signals_log: list[TradeSignal] = field(default_factory=list)
    started_at: str = ""
    last_update: str = ""
    _next_id: int = 0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.size_usdc + p.unrealized_pnl for p in self.open_positions)

    @property
    def total_pnl(self) -> float:
        return self.total_value - self.initial_capital

    @property
    def total_pnl_pct(self) -> float:
        return (self.total_pnl / self.initial_capital) * 100 if self.initial_capital > 0 else 0

    @property
    def realized_pnl(self) -> float:
        return sum(p.pnl for p in self.closed_positions)

    @property
    def win_count(self) -> int:
        return sum(1 for p in self.closed_positions if p.won)

    @property
    def loss_count(self) -> int:
        return sum(1 for p in self.closed_positions if not p.won)

    @property
    def win_rate(self) -> float:
        total = len(self.closed_positions)
        return (self.win_count / total * 100) if total > 0 else 0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(p.pnl for p in self.closed_positions if p.pnl > 0)
        gross_loss = abs(sum(p.pnl for p in self.closed_positions if p.pnl < 0))
        return gross_win / gross_loss if gross_loss > 0 else 0.0

    def open_position(
        self,
        market_id: str,
        market_title: str,
        side: str,
        price: float,
        size_usdc: float,
        source_wallet: str,
        source_rank: int,
    ) -> PaperPosition | None:
        if size_usdc > self.cash or size_usdc < 0.50:
            return None
        if price <= 0 or price >= 1.0:
            return None

        self._next_id += 1
        pos_id = f"PT-{self._next_id:06d}"
        shares = size_usdc / price

        pos = PaperPosition(
            id=pos_id,
            market_id=market_id,
            market_title=market_title[:60],
            side=side,
            entry_price=price,
            size_usdc=size_usdc,
            shares=shares,
            source_wallet=source_wallet,
            source_rank=source_rank,
            opened_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            current_price=price,
        )

        self.cash -= size_usdc
        self.open_positions.append(pos)
        return pos

    def update_position_price(self, market_id: str, new_price: float) -> None:
        for pos in self.open_positions:
            if pos.market_id == market_id:
                pos.current_price = new_price
                if pos.side.upper() in ("YES", "BUY"):
                    pos.unrealized_pnl = pos.shares * new_price - pos.size_usdc
                else:
                    pos.unrealized_pnl = pos.size_usdc - pos.shares * new_price

    def close_position(
        self, pos_id: str, exit_price: float, reason: str = ""
    ) -> ClosedPosition | None:
        pos = next((p for p in self.open_positions if p.id == pos_id), None)
        if pos is None:
            return None

        if pos.side.upper() in ("YES", "BUY"):
            pnl = pos.shares * exit_price - pos.size_usdc
        else:
            pnl = pos.size_usdc - pos.shares * exit_price

        pnl_pct = (pnl / pos.size_usdc * 100) if pos.size_usdc > 0 else 0

        now = dt.datetime.now(dt.timezone.utc)
        opened = dt.datetime.fromisoformat(pos.opened_at)
        hold_mins = (now - opened).total_seconds() / 60

        closed = ClosedPosition(
            id=pos.id,
            market_id=pos.market_id,
            market_title=pos.market_title,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usdc=pos.size_usdc,
            pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 2),
            source_wallet=pos.source_wallet,
            source_rank=pos.source_rank,
            opened_at=pos.opened_at,
            closed_at=now.isoformat(),
            hold_time_minutes=round(hold_mins, 1),
            won=pnl > 0,
        )

        self.cash += pos.size_usdc + pnl
        self.open_positions = [p for p in self.open_positions if p.id != pos_id]
        self.closed_positions.append(closed)
        return closed

    def log_signal(self, signal: TradeSignal) -> None:
        self.signals_log.append(signal)

    def save_snapshot(self, path: Path) -> None:
        self.last_update = dt.datetime.now(dt.timezone.utc).isoformat()
        data = {
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 4),
            "total_value": round(self.total_value, 4),
            "total_pnl": round(self.total_pnl, 4),
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 2),
            "total_trades": len(self.closed_positions),
            "wins": self.win_count,
            "losses": self.loss_count,
            "open_position_count": len(self.open_positions),
            "started_at": self.started_at,
            "last_update": self.last_update,
            "open_positions": [asdict(p) for p in self.open_positions],
            "recent_closed": [asdict(p) for p in self.closed_positions[-50:]],
            "recent_signals": [asdict(s) for s in self.signals_log[-100:]],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load_snapshot(cls, path: Path) -> PaperPortfolio:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        portfolio = cls(
            initial_capital=data.get("initial_capital", 10_000),
            cash=data.get("cash", 10_000),
            started_at=data.get("started_at", ""),
        )
        for p in data.get("open_positions", []):
            portfolio.open_positions.append(PaperPosition(**p))
        for p in data.get("recent_closed", []):
            portfolio.closed_positions.append(ClosedPosition(**p))
        for s in data.get("recent_signals", []):
            portfolio.signals_log.append(TradeSignal(**s))
        portfolio._next_id = len(portfolio.closed_positions) + len(portfolio.open_positions)
        return portfolio
