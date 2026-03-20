"""
WebSocket listener for live Polymarket price changes.
Streams real-time bid/ask updates, trade executions, and orderbook changes.

Usage:
    python3 -m polymarket_index.api.ws_streamer --token TOKEN_ID
    python3 -m polymarket_index.api.ws_streamer --slug "netanyahu-out-by-march-31-854"
    python3 -m polymarket_index.api.ws_streamer --top 10
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

from loguru import logger

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
    HAS_WS = True
except ImportError:
    HAS_WS = False

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10


@dataclass
class PriceUpdate:
    token_id: str
    event_type: str  # price_change, last_trade_price, book
    best_bid: float
    best_ask: float
    mid_price: float
    last_trade_price: float
    last_trade_size: float
    timestamp: float


class PriceStreamer:
    """
    Streams live price updates from the Polymarket CLOB WebSocket.
    Connects to the market channel and receives bid/ask/trade events.
    """

    def __init__(self) -> None:
        if not HAS_WS:
            raise ImportError("websockets is required: pip install websockets")

        self._ws: WebSocketClientProtocol | None = None
        self._running = False
        self._callbacks: list[Callable[[PriceUpdate], Awaitable[None] | None]] = []
        self._latest_prices: dict[str, PriceUpdate] = {}

    def on_price_update(self, callback: Callable[[PriceUpdate], Awaitable[None] | None]) -> None:
        self._callbacks.append(callback)

    async def stream(self, token_ids: list[str]) -> None:
        """Connect and stream price updates for the given token IDs."""
        if not token_ids:
            logger.warning("No token IDs to stream")
            return

        self._running = True
        logger.info("Connecting to Polymarket WebSocket for {} tokens...", len(token_ids))

        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self._ws = ws
                    logger.info("Connected to {}", WS_URL)

                    subscribe_msg = json.dumps({
                        "assets_ids": token_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    })
                    await ws.send(subscribe_msg)
                    logger.info("Subscribed to {} token streams", len(token_ids))

                    ping_task = asyncio.create_task(self._ping_loop(ws))

                    try:
                        async for raw_msg in ws:
                            if not self._running:
                                break
                            await self._handle_message(raw_msg)
                    finally:
                        ping_task.cancel()

            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket closed: {}. Reconnecting in 3s...", e)
                await asyncio.sleep(3)
            except Exception as e:
                logger.error("WebSocket error: {}. Reconnecting in 5s...", e)
                await asyncio.sleep(5)

    async def _ping_loop(self, ws: WebSocketClientProtocol) -> None:
        while self._running:
            try:
                await ws.send("PING")
                await asyncio.sleep(PING_INTERVAL)
            except Exception:
                break

    async def _handle_message(self, raw: str) -> None:
        if raw == "PONG":
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for item in data:
                await self._process_event(item)
        elif isinstance(data, dict):
            await self._process_event(data)

    async def _process_event(self, event: dict) -> None:
        event_type = event.get("event_type", event.get("type", "unknown"))
        asset_id = event.get("asset_id", event.get("token_id", ""))

        # Extract best bid/ask from the order book arrays
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0

        last_trade = float(event.get("last_trade_price", 0) or 0)

        # Compute total book size for volume tracking
        total_bid_size = sum(float(b.get("size", 0)) for b in bids[:5]) if bids else 0
        total_ask_size = sum(float(a.get("size", 0)) for a in asks[:5]) if asks else 0

        update = PriceUpdate(
            token_id=asset_id,
            event_type=event_type,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=0,
            last_trade_price=last_trade,
            last_trade_size=total_bid_size + total_ask_size,
            timestamp=time.time(),
        )

        if best_bid > 0 and best_ask > 0:
            update.mid_price = (best_bid + best_ask) / 2
        elif last_trade > 0:
            update.mid_price = last_trade
        elif best_bid > 0:
            update.mid_price = best_bid
        elif best_ask > 0:
            update.mid_price = best_ask

        if asset_id:
            self._latest_prices[asset_id] = update

        for cb in self._callbacks:
            try:
                result = cb(update)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("Callback error: {}", exc)

    def get_latest_price(self, token_id: str) -> PriceUpdate | None:
        return self._latest_prices.get(token_id)

    def stop(self) -> None:
        self._running = False


class PriceAlertManager:
    """
    Monitors price streams and fires alerts when conditions are met.

    Alert types:
    - price_above: fires when price exceeds threshold
    - price_below: fires when price drops below threshold
    - spread_above: fires when bid-ask spread exceeds threshold
    - volume_spike: fires when trade size exceeds threshold
    """

    def __init__(self) -> None:
        self._alerts: list[dict] = []
        self._fired: set[str] = set()
        self._alert_callbacks: list[Callable] = []

    def add_alert(
        self,
        token_id: str,
        alert_type: str,
        threshold: float,
        label: str = "",
        one_shot: bool = True,
    ) -> str:
        alert_id = f"alert-{len(self._alerts)+1}"
        self._alerts.append({
            "id": alert_id,
            "token_id": token_id,
            "type": alert_type,
            "threshold": threshold,
            "label": label or f"{alert_type} {threshold}",
            "one_shot": one_shot,
        })
        logger.info("Alert added: {} — {} {} {}", alert_id, label or token_id[:12], alert_type, threshold)
        return alert_id

    def on_alert(self, callback: Callable) -> None:
        self._alert_callbacks.append(callback)

    async def check(self, update: PriceUpdate) -> None:
        for alert in self._alerts:
            if alert["token_id"] != update.token_id:
                continue
            if alert["one_shot"] and alert["id"] in self._fired:
                continue

            triggered = False
            if alert["type"] == "price_above" and update.mid_price > alert["threshold"]:
                triggered = True
            elif alert["type"] == "price_below" and update.mid_price < alert["threshold"]:
                triggered = True
            elif alert["type"] == "spread_above":
                spread = update.best_ask - update.best_bid
                if spread > alert["threshold"]:
                    triggered = True
            elif alert["type"] == "volume_spike" and update.last_trade_size > alert["threshold"]:
                triggered = True

            if triggered:
                self._fired.add(alert["id"])
                msg = (
                    f"ALERT: {alert['label']}\n"
                    f"  Token: {update.token_id[:20]}...\n"
                    f"  Price: bid={update.best_bid:.4f} ask={update.best_ask:.4f} "
                    f"mid={update.mid_price:.4f}\n"
                    f"  Type: {alert['type']} threshold={alert['threshold']}"
                )
                logger.warning(msg)
                for cb in self._alert_callbacks:
                    try:
                        result = cb(alert, update)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass


async def stream_top_markets(n: int = 10, duration: float = 60) -> None:
    """Stream live prices for the top N markets by volume."""
    from polymarket_index.api.gamma_markets import fetch_active_markets, print_markets

    logger.info("Fetching top {} markets...", n)
    markets = await fetch_active_markets(limit=n, min_volume_24h=1000)

    if not markets:
        logger.error("No markets found")
        return

    print_markets(markets, top_n=n)

    token_ids = []
    token_labels: dict[str, str] = {}
    condition_labels: dict[str, str] = {}
    for m in markets:
        condition_labels[m.condition_id] = m.question[:40]
        if m.token_id_yes:
            token_ids.append(m.token_id_yes)
            token_labels[m.token_id_yes] = f"{m.question[:40]} (YES)"
        if m.token_id_no:
            token_ids.append(m.token_id_no)
            token_labels[m.token_id_no] = f"{m.question[:40]} (NO)"

    streamer = PriceStreamer()

    def on_update(update: PriceUpdate) -> None:
        label = token_labels.get(update.token_id, "")
        if not label and update.token_id:
            label = update.token_id[:20] + "..."

        if update.mid_price > 0 or update.best_bid > 0 or update.last_trade_price > 0:
            parts = []
            if update.best_bid:
                parts.append(f"bid={update.best_bid:.4f}")
            if update.best_ask:
                parts.append(f"ask={update.best_ask:.4f}")
            if update.mid_price:
                parts.append(f"mid={update.mid_price:.4f}")
            if update.last_trade_price:
                parts.append(f"last={update.last_trade_price:.4f}")
            prices_str = " | ".join(parts) if parts else "—"
            print(f"  [{update.event_type:20s}] {prices_str:60s} | {label}")

    streamer.on_price_update(on_update)

    print(f"\nStreaming live prices for {len(token_ids)} tokens ({duration}s)...\n")

    stream_task = asyncio.create_task(streamer.stream(token_ids))
    await asyncio.sleep(duration)
    streamer.stop()

    try:
        await asyncio.wait_for(stream_task, timeout=3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    print("\nStream ended.")
