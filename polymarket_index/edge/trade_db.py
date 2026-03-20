"""
Lightweight SQLite trade history for the edge trading system.
Standalone from the main DB — just a single file for trade logging.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from loguru import logger

DB_PATH = Path("edge_trades.db")


@dataclass
class EdgeTradeRecord:
    id: int | None
    timestamp: str
    market_id: str
    market_question: str
    side: str
    size_usdc: float
    entry_price: float
    exit_price: float | None
    pnl: float | None
    ev_pct: float
    edge: float
    confidence: str
    status: str  # open / closed / cancelled
    mode: str  # dry_run / live
    reasoning: str


class TradeDB:
    """SQLite-backed trade history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or DB_PATH
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_question TEXT,
                side TEXT NOT NULL,
                size_usdc REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                pnl REAL,
                ev_pct REAL,
                edge REAL,
                confidence TEXT,
                status TEXT DEFAULT 'open',
                mode TEXT DEFAULT 'dry_run',
                reasoning TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pnl_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_value REAL,
                cash REAL,
                realized_pnl REAL,
                unrealized_pnl REAL,
                open_positions INTEGER,
                closed_trades INTEGER,
                win_rate REAL
            )
        """)
        self._conn.commit()
        logger.debug("Trade DB initialized at {}", self._path)

    def insert_trade(self, trade: EdgeTradeRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO trades
            (timestamp, market_id, market_question, side, size_usdc,
             entry_price, exit_price, pnl, ev_pct, edge, confidence,
             status, mode, reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.timestamp, trade.market_id, trade.market_question,
                trade.side, trade.size_usdc, trade.entry_price,
                trade.exit_price, trade.pnl, trade.ev_pct, trade.edge,
                trade.confidence, trade.status, trade.mode, trade.reasoning,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, pnl: float) -> None:
        self._conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, status='closed' WHERE id=?",
            (exit_price, pnl, trade_id),
        )
        self._conn.commit()

    def cancel_trade(self, trade_id: int) -> None:
        self._conn.execute(
            "UPDATE trades SET status='cancelled' WHERE id=?", (trade_id,),
        )
        self._conn.commit()

    def get_open_trades(self) -> list[EdgeTradeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY timestamp DESC"
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_recent_trades(self, limit: int = 50) -> list[EdgeTradeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_stats(self) -> dict:
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='closed' AND pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='closed' AND pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END) as total_pnl,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_count,
                AVG(CASE WHEN status='closed' AND pnl > 0 THEN pnl END) as avg_win,
                AVG(CASE WHEN status='closed' AND pnl < 0 THEN pnl END) as avg_loss
            FROM trades
        """).fetchone()

        total = row["total"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        closed = wins + losses

        return {
            "total_trades": total,
            "open": row["open_count"] or 0,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / closed * 100) if closed > 0 else 0,
            "total_pnl": round(row["total_pnl"] or 0, 2),
            "avg_win": round(row["avg_win"] or 0, 2),
            "avg_loss": round(row["avg_loss"] or 0, 2),
        }

    def save_pnl_snapshot(
        self, total_value: float, cash: float, realized: float,
        unrealized: float, open_count: int, closed_count: int, win_rate: float
    ) -> None:
        self._conn.execute(
            """INSERT INTO pnl_snapshots
            (timestamp, total_value, cash, realized_pnl, unrealized_pnl,
             open_positions, closed_trades, win_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dt.datetime.now(dt.timezone.utc).isoformat(),
                total_value, cash, realized, unrealized,
                open_count, closed_count, win_rate,
            ),
        )
        self._conn.commit()

    def get_pnl_history(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM pnl_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> EdgeTradeRecord:
        return EdgeTradeRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            market_id=row["market_id"],
            market_question=row["market_question"],
            side=row["side"],
            size_usdc=row["size_usdc"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            pnl=row["pnl"],
            ev_pct=row["ev_pct"],
            edge=row["edge"],
            confidence=row["confidence"],
            status=row["status"],
            mode=row["mode"],
            reasoning=row["reasoning"],
        )
