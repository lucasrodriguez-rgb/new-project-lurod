from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from loguru import logger

from polymarket_index.config import settings


# ── Response dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    slug: str
    active: bool
    closed: bool
    volume: float
    liquidity: float
    close_time: str | None
    outcome_prices: dict[str, float] = field(default_factory=dict)
    category: str = ""


@dataclass(frozen=True)
class Position:
    market_id: str
    market_slug: str
    outcome: str
    size: float
    avg_price: float
    current_value: float


@dataclass(frozen=True)
class TradeRecord:
    id: str
    market_id: str
    market_slug: str
    side: str
    size: float
    price: float
    timestamp: str
    outcome: str | None = None


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    market_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    mid_price: float = 0.0


@dataclass(frozen=True)
class OrderResponse:
    order_id: str
    status: str
    market_id: str
    side: str
    size: float
    price: float


# ── Rate limiter ────────────────────────────────────────────────────────


class AsyncRateLimiter:
    """Token-bucket rate limiter for async code."""

    def __init__(self, rate: int, period: float = 1.0):
        self._rate = rate
        self._period = period
        self._tokens = float(rate)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / self._period))
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (self._period / self._rate)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ── Polymarket API client ──────────────────────────────────────────────


class PolymarketClient:
    """Async client wrapping Polymarket Gamma + CLOB APIs."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._limiter = AsyncRateLimiter(settings.api_rate_limit_rps)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Generic request with rate limiting + retries ────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_body: dict | None = None,
        auth: bool = False,
    ) -> Any:
        session = await self._get_session()
        headers: dict[str, str] = {}

        if auth:
            headers.update(self._auth_headers(method, url, json_body or {}))

        last_exc: Exception | None = None
        for attempt in range(settings.api_max_retries):
            await self._limiter.acquire()
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                ) as resp:
                    if resp.status == 429:
                        wait = settings.api_base_backoff_seconds * (2**attempt)
                        logger.warning(
                            "Rate limited on {} (attempt {}), backing off {:.1f}s",
                            url,
                            attempt + 1,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return await resp.json()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                wait = settings.api_base_backoff_seconds * (2**attempt)
                logger.warning(
                    "Request to {} failed (attempt {}): {} — retrying in {:.1f}s",
                    url,
                    attempt + 1,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)

        raise ConnectionError(
            f"Failed after {settings.api_max_retries} retries: {url}"
        ) from last_exc

    def _auth_headers(
        self, method: str, url: str, body: dict
    ) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + url
        if body:
            import json as _json
            message += _json.dumps(body, separators=(",", ":"))

        signature = hmac.new(
            settings.polymarket_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "POLY-API-KEY": settings.polymarket_api_key,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": settings.polymarket_passphrase,
        }

    # ── Gamma API: Markets ──────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> list[Market]:
        url = f"{settings.gamma_api_url}/markets"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"

        data = await self._request("GET", url, params=params)
        if not isinstance(data, list):
            data = data.get("data", data.get("markets", []))

        markets: list[Market] = []
        for m in data:
            outcome_prices: dict[str, float] = {}
            for token in m.get("tokens", []):
                outcome_prices[token.get("outcome", "unknown")] = float(
                    token.get("price", 0)
                )

            markets.append(
                Market(
                    id=str(m.get("condition_id", m.get("id", ""))),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    active=m.get("active", False),
                    closed=m.get("closed", False),
                    volume=float(m.get("volume", 0)),
                    liquidity=float(m.get("liquidity", 0)),
                    close_time=m.get("end_date_iso") or m.get("close_time"),
                    outcome_prices=outcome_prices,
                    category=m.get("category", ""),
                )
            )
        return markets

    async def get_all_active_markets(self) -> list[Market]:
        all_markets: list[Market] = []
        offset = 0
        while True:
            batch = await self.get_markets(limit=100, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return all_markets

    async def get_market_by_id(self, market_id: str) -> Market | None:
        url = f"{settings.gamma_api_url}/markets/{market_id}"
        try:
            m = await self._request("GET", url)
        except Exception:
            return None

        outcome_prices: dict[str, float] = {}
        for token in m.get("tokens", []):
            outcome_prices[token.get("outcome", "unknown")] = float(
                token.get("price", 0)
            )

        return Market(
            id=str(m.get("condition_id", m.get("id", ""))),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            active=m.get("active", False),
            closed=m.get("closed", False),
            volume=float(m.get("volume", 0)),
            liquidity=float(m.get("liquidity", 0)),
            close_time=m.get("end_date_iso") or m.get("close_time"),
            outcome_prices=outcome_prices,
            category=m.get("category", ""),
        )

    # ── Data API: Positions ─────────────────────────────────────────────

    async def get_positions(self, address: str) -> list[Position]:
        url = f"{settings.data_api_url}/positions"
        params: dict[str, Any] = {"user": address.lower(), "limit": 500}
        data = await self._request("GET", url, params=params)

        if not isinstance(data, list):
            data = data.get("data", data.get("positions", []))

        return [
            Position(
                market_id=str(p.get("conditionId", p.get("market_id", ""))),
                market_slug=p.get("slug", p.get("market_slug", "")),
                outcome=p.get("outcome", ""),
                size=float(p.get("size", p.get("tokens", 0)) or 0),
                avg_price=float(p.get("avgPrice", p.get("avg_price", 0)) or 0),
                current_value=float(p.get("currentValue", p.get("value", 0)) or 0),
            )
            for p in data
        ]

    # ── Data API: Trade history ──────────────────────────────────────────

    MAX_ACTIVITY_OFFSET = 3000

    async def get_trades(
        self,
        address: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TradeRecord]:
        if offset >= self.MAX_ACTIVITY_OFFSET:
            return []

        url = f"{settings.data_api_url}/activity"
        params: dict[str, Any] = {
            "user": address.lower(),
            "limit": min(limit, 500),
            "offset": offset,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        data = await self._request("GET", url, params=params)

        if not isinstance(data, list):
            data = data.get("data", data.get("history", data.get("activity", [])))

        results: list[TradeRecord] = []
        for idx, t in enumerate(data):
            tx_hash = t.get("transactionHash", "")
            trade_id = tx_hash or f"{address[:10]}-{offset + idx}"

            market_id = str(t.get("conditionId", ""))

            side = (t.get("side", "") or "").upper()
            if side == "BUY":
                side = "YES"
            elif side == "SELL":
                side = "NO"
            elif not side:
                outcome_idx = t.get("outcomeIndex")
                side = "YES" if outcome_idx == 0 else "NO"

            size_val = float(t.get("size", 0) or 0)
            price_val = float(t.get("price", 0) or 0)
            usdc_size = float(t.get("usdcSize", 0) or 0)

            ts = t.get("timestamp", "")
            if isinstance(ts, (int, float)):
                import datetime as _dt
                ts = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()

            results.append(
                TradeRecord(
                    id=trade_id,
                    market_id=market_id,
                    market_slug=t.get("slug", ""),
                    side=side or "YES",
                    size=usdc_size if usdc_size > 0 else (size_val * price_val),
                    price=price_val,
                    timestamp=str(ts),
                    outcome=t.get("outcome"),
                )
            )
        return results

    async def get_all_trades(self, address: str) -> list[TradeRecord]:
        all_trades: list[TradeRecord] = []
        offset = 0
        page_size = 500
        while offset < self.MAX_ACTIVITY_OFFSET:
            batch = await self.get_trades(address, limit=page_size, offset=offset)
            if not batch:
                break
            all_trades.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)

        logger.info("Fetched {} trades for {}", len(all_trades), address[:10])
        return all_trades

    # ── CLOB API: Order book ────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> OrderBook:
        url = f"{settings.clob_api_url}/book"
        params = {"token_id": token_id}

        data = await self._request("GET", url, params=params)

        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]

        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 1.0
        mid = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else 0.0

        return OrderBook(
            market_id=token_id,
            bids=bids,
            asks=asks,
            mid_price=mid,
        )

    # ── CLOB API: Place order ───────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> OrderResponse:
        url = f"{settings.clob_api_url}/order"
        body = {
            "tokenID": token_id,
            "side": side.upper(),
            "size": str(size),
            "price": str(price),
            "type": "GTC",
        }

        data = await self._request("POST", url, json_body=body, auth=True)

        return OrderResponse(
            order_id=data.get("orderID", data.get("id", "")),
            status=data.get("status", "unknown"),
            market_id=token_id,
            side=side.upper(),
            size=size,
            price=price,
        )

    # ── CLOB API: Cancel order ──────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> dict:
        url = f"{settings.clob_api_url}/order/{order_id}"
        return await self._request("DELETE", url, auth=True)

    # ── Leaderboard (Data API) ──────────────────────────────────────────

    async def scrape_leaderboard(
        self,
        limit: int = 50,
        time_period: str = "ALL",
        order_by: str = "PNL",
    ) -> list[str]:
        """
        Fetch top wallet addresses from the Polymarket Data API leaderboard.
        Endpoint: GET https://data-api.polymarket.com/v1/leaderboard
        """
        addresses: list[str] = []
        offset = 0
        page_size = min(limit, 50)

        while len(addresses) < limit:
            url = f"{settings.data_api_url}/v1/leaderboard"
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": offset,
                "timePeriod": time_period,
                "orderBy": order_by,
            }

            try:
                data = await self._request("GET", url, params=params)
            except Exception as exc:
                logger.warning("Leaderboard fetch failed at offset {}: {}", offset, exc)
                break

            if not isinstance(data, list):
                data = data.get("data", data.get("leaderboard", []))

            if not data:
                break

            for entry in data:
                addr = (
                    entry.get("proxyWallet", "")
                    or entry.get("address", "")
                    or entry.get("user", "")
                )
                if addr:
                    addresses.append(addr.lower())

            if len(data) < page_size:
                break
            offset += page_size

        logger.info("Leaderboard: fetched {} wallet addresses", len(addresses))
        return addresses[:limit]
