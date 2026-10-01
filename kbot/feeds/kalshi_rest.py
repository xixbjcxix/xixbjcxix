"""Kalshi Trade API v2 REST client (async, httpx) with request signing and rate limiting.

Auth (docs.kalshi.com/getting_started/quick_start_authenticated_requests):
  headers KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE
  signature = base64( sign( f"{timestamp}{METHOD}{path_without_query}" ) )
  RSA keys: RSA-PSS, SHA-256, MGF1(SHA-256), salt = digest length. Ed25519 keys: sign directly.
  `path` is the full path from the host root, e.g. /trade-api/v2/portfolio/events/orders

Orders use the V2 endpoints (V1 /portfolio/orders mutations were deprecated in June 2026):
  POST   /portfolio/events/orders             create
  DELETE /portfolio/events/orders/{order_id}  cancel
  DELETE /portfolio/events/orders             cancel all
V2 orders are expressed on the YES book (docs "order direction"):
  buy YES at p  -> side "bid", price p
  buy NO  at q  -> side "ask", price 1 - q
  sell YES at p -> side "ask", price p      (reduce_only)
  sell NO  at q -> side "bid", price 1 - q  (reduce_only)
Kalshi nets YES and NO in the same market, so completing a pair returns $1 of collateral at once.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger("kbot.rest")


def load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as fh:
        data = fh.read()
    return serialization.load_pem_private_key(data, password=None)


def sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    msg = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()


def auth_headers(key_id: str, private_key, method: str, path: str) -> Dict[str, str]:
    ts = int(time.time() * 1000)
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts, method, path)}


class TokenBucket:
    def __init__(self, rate: float, capacity: Optional[float] = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.t = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, n: float) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                await asyncio.sleep((n - self.tokens) / self.rate)


class KalshiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def fmt_price(p: float) -> str:
    return f"{p:.4f}"


def fmt_count(c: float) -> str:
    return f"{c:.2f}"


def v2_order_fields(outcome_yes: bool, buy: bool, price: float) -> Tuple[str, float]:
    """(side, yes-book price) for a V2 order. price is in the outcome's own terms."""
    if outcome_yes:
        return ("bid" if buy else "ask"), price
    return ("ask" if buy else "bid"), round(1.0 - price, 4)


class KalshiRest:
    def __init__(self, base_url: str, key_id: Optional[str] = None, key_path: Optional[str] = None,
                 read_rate: float = 200.0, write_rate: float = 100.0, timeout: float = 10.0) -> None:
        import httpx
        self.base = base_url.rstrip("/")
        self.root_path = urlparse(self.base).path            # /trade-api/v2
        self.key_id = key_id
        self.pkey = load_private_key(key_path) if key_path else None
        self.client = httpx.AsyncClient(timeout=timeout)
        self.read_bucket = TokenBucket(read_rate)
        self.write_bucket = TokenBucket(write_rate)
        self.n_429 = 0

    @property
    def authed(self) -> bool:
        return self.pkey is not None and bool(self.key_id)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, params: Optional[dict] = None, json: Any = None,
                      auth: bool = True, write: bool = False, cost: float = 10.0, retries: int = 4) -> Any:
        url = self.base + path
        full_path = self.root_path + path
        for attempt in range(retries + 1):
            await (self.write_bucket if write else self.read_bucket).take(cost)
            headers = {"Content-Type": "application/json"}
            if auth:
                if not self.authed:
                    raise RuntimeError("this call needs API keys - run `python -m kbot setup`")
                headers.update(auth_headers(self.key_id, self.pkey, method, full_path))
            r = await self.client.request(method, url, params=params, json=json, headers=headers)
            if r.status_code == 429:
                self.n_429 += 1
                await asyncio.sleep(min(2.0, 0.1 * 2 ** attempt))
                continue
            if r.status_code >= 500 and attempt < retries and method == "GET":
                await asyncio.sleep(0.3 * 2 ** attempt)
                continue
            if r.status_code >= 400:
                raise KalshiError(r.status_code, r.text)
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()
        raise KalshiError(429, "rate limited after retries")

    # ---- public market data ---------------------------------------------------------------
    async def exchange_status(self) -> dict:
        return await self.request("GET", "/exchange/status", auth=False)

    async def get_series(self, series_ticker: str) -> dict:
        r = await self.request("GET", f"/series/{series_ticker}", auth=False)
        return r.get("series", r)

    async def get_markets(self, **params) -> List[dict]:
        out, cursor = [], None
        while True:
            p = dict(params, limit=params.get("limit", 200))
            if cursor:
                p["cursor"] = cursor
            r = await self.request("GET", "/markets", params=p, auth=False)
            out.extend(r.get("markets", []))
            cursor = r.get("cursor")
            if not cursor or len(out) >= 2000:
                return out

    async def get_market(self, ticker: str) -> dict:
        r = await self.request("GET", f"/markets/{ticker}", auth=False)
        return r.get("market", r)

    async def get_orderbook(self, ticker: str) -> dict:
        return await self.request("GET", f"/markets/{ticker}/orderbook", auth=False)

    async def get_trades(self, ticker: str, min_ts: Optional[int] = None, limit: int = 200) -> List[dict]:
        p: Dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts:
            p["min_ts"] = min_ts
        r = await self.request("GET", "/markets/trades", params=p, auth=False)
        return r.get("trades", [])

    # ---- account ------------------------------------------------------------------------------
    async def get_balance(self) -> dict:
        return await self.request("GET", "/portfolio/balance")

    async def get_shard_balances(self) -> Dict[int, float]:
        """Available balance ($) per exchange shard. Kalshi runs some market groups (crypto: shard 2
        since 2026-08-24) on separate exchange shards; orders there need collateral ON that shard."""
        b = await self.get_balance()
        out: Dict[int, float] = {}
        for it in b.get("balance_breakdown") or []:
            try:
                out[int(it.get("exchange_index", 0))] = float(it.get("balance", 0))
            except (TypeError, ValueError):
                pass
        if not out and b.get("balance") is not None:
            out[0] = float(b["balance"]) / 100.0
        return out

    async def transfer_between_shards(self, dollars: float, dest_shard: int, source_shard: int = 0) -> dict:
        """Move your own funds between exchange shards of the same account (event-contract side)."""
        body = {"source": "event_contract", "destination": "event_contract",
                "amount": int(round(dollars * 10000)),            # centicents
                "source_exchange_shard": int(source_shard), "destination_exchange_shard": int(dest_shard)}
        return await self.request("POST", "/portfolio/intra_exchange_instance_transfer", json=body,
                                  write=True, cost=10.0, retries=0)

    async def get_limits(self) -> dict:
        return await self.request("GET", "/account/limits")

    async def get_positions(self, ticker: Optional[str] = None) -> List[dict]:
        p: Dict[str, Any] = {"limit": 200}
        if ticker:
            p["ticker"] = ticker
        r = await self.request("GET", "/portfolio/positions", params=p)
        return r.get("market_positions", [])

    async def get_open_orders(self) -> List[dict]:
        r = await self.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
        return r.get("orders", [])

    async def get_fills(self, min_ts: Optional[int] = None, limit: int = 200) -> List[dict]:
        p: Dict[str, Any] = {"limit": limit}
        if min_ts:
            p["min_ts"] = min_ts
        r = await self.request("GET", "/portfolio/fills", params=p)
        return r.get("fills", [])

    # ---- orders (V2) --------------------------------------------------------------------------
    async def create_order(self, ticker: str, outcome_yes: bool, buy: bool, price: float, count: float,
                           post_only: bool, tif: str = "good_till_canceled", reduce_only: bool = False,
                           client_order_id: Optional[str] = None,
                           expiration_s: Optional[int] = None) -> dict:
        side, yes_px = v2_order_fields(outcome_yes, buy, price)
        body: Dict[str, Any] = {
            "ticker": ticker, "side": side, "count": fmt_count(count), "price": fmt_price(yes_px),
            "time_in_force": tif, "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if post_only:
            body["post_only"] = True
        if reduce_only:
            body["reduce_only"] = True
        if expiration_s:
            body["expiration_time"] = int(expiration_s)
        return await self.request("POST", "/portfolio/events/orders", json=body, write=True, cost=10.0, retries=2)

    async def cancel_order(self, order_id: str, ticker: Optional[str] = None) -> dict:
        params = {"market_ticker": ticker} if ticker else None
        return await self.request("DELETE", f"/portfolio/events/orders/{order_id}", params=params,
                                  write=True, cost=2.0, retries=3)

    async def cancel_all(self) -> dict:
        return await self.request("DELETE", "/portfolio/events/orders", write=True, cost=2.0, retries=4)
