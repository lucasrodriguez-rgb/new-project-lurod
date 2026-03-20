"""
FastAPI dashboard with WebSocket real-time updates.
Serves portfolio analytics, P&L curves, positions, signals, and leaderboard.

Usage:
    python3 -m polymarket_index.dashboard
    # Opens at http://localhost:8050
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from polymarket_index.edge.trade_db import TradeDB

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Polymarket Index Dashboard", version="1.0.0")
connected_clients: list[WebSocket] = []
_db: TradeDB | None = None


def get_db() -> TradeDB:
    global _db
    if _db is None:
        _db = TradeDB()
    return _db


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dashboard</h1><p>static/index.html not found</p>")


@app.get("/api/status")
async def api_status():
    stats = get_db().get_stats()
    paper_data = _load_paper_portfolio()
    result = {
        "edge_trader": stats,
        "paper_trader": paper_data,
        "timestamp": time.time(),
    }
    _sanitize_floats(result)
    return result


@app.get("/api/pnl")
async def api_pnl():
    history = get_db().get_pnl_history(500)
    paper_data = _load_paper_portfolio()
    return {
        "edge_pnl_history": history,
        "paper_trader": {
            "total_value": paper_data.get("total_value", 0),
            "total_pnl": paper_data.get("total_pnl", 0),
            "total_pnl_pct": paper_data.get("total_pnl_pct", 0),
        },
    }


@app.get("/api/positions")
async def api_positions():
    open_trades = get_db().get_open_trades()
    paper_data = _load_paper_portfolio()
    return {
        "edge_positions": [
            {
                "id": t.id,
                "market": t.market_question[:60],
                "side": t.side,
                "size": t.size_usdc,
                "entry_price": t.entry_price,
                "ev_pct": t.ev_pct,
                "confidence": t.confidence,
            }
            for t in open_trades
        ],
        "paper_positions": paper_data.get("open_positions", []),
    }


@app.get("/api/trades")
async def api_trades():
    recent = get_db().get_recent_trades(100)
    return {
        "trades": [
            {
                "id": t.id,
                "timestamp": t.timestamp,
                "market": t.market_question[:60],
                "side": t.side,
                "size": t.size_usdc,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "ev_pct": t.ev_pct,
                "status": t.status,
                "mode": t.mode,
            }
            for t in recent
        ]
    }


@app.get("/api/leaderboard")
async def api_leaderboard():
    paper_data = _load_paper_portfolio()
    signals = paper_data.get("recent_signals", [])
    wallet_counts: dict[str, dict] = {}
    for s in signals:
        w = s.get("wallet", "unknown")
        if w not in wallet_counts:
            wallet_counts[w] = {"wallet": w, "signals": 0, "copied": 0}
        wallet_counts[w]["signals"] += 1
        if s.get("action") == "COPY":
            wallet_counts[w]["copied"] += 1

    ranked = sorted(wallet_counts.values(), key=lambda x: x["copied"], reverse=True)
    return {"wallets": ranked[:50]}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    try:
        while True:
            stats = get_db().get_stats()
            paper_data = _load_paper_portfolio()
            payload = {
                "type": "update",
                "edge": stats,
                "paper": {
                    "total_value": paper_data.get("total_value", 0),
                    "total_pnl": paper_data.get("total_pnl", 0),
                    "total_pnl_pct": paper_data.get("total_pnl_pct", 0),
                    "win_rate": paper_data.get("win_rate", 0),
                    "open_count": paper_data.get("open_position_count", 0),
                    "closed_count": paper_data.get("total_trades", 0),
                    "cash": paper_data.get("cash", 0),
                },
                "timestamp": time.time(),
            }
            await ws.send_json(payload)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        connected_clients.remove(ws)
    except Exception:
        if ws in connected_clients:
            connected_clients.remove(ws)


def _load_paper_portfolio() -> dict:
    path = Path("live_paper_trading.json")
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _sanitize_floats(data)
            return data
        except Exception:
            pass
    return {}


def _sanitize_floats(obj):
    """Replace inf/nan with 0 to prevent JSON serialization errors."""
    import math
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                obj[k] = 0.0
            elif isinstance(v, (dict, list)):
                _sanitize_floats(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                obj[i] = 0.0
            elif isinstance(v, (dict, list)):
                _sanitize_floats(v)


def start_server(host: str = "0.0.0.0", port: int = 8050):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
