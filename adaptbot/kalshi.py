"""Kalshi REST client (trade-api v2) with RSA-PSS request signing.

Kalshi has been moving fields from integer cents ("yes_bid": 45) to dollar
strings ("yes_bid_dollars": "0.4500") and fixed-point counts ("position_fp").
Every parser here accepts either form.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import DATA_DIR
from .market import Book, MarketMeta

log = logging.getLogger("adaptbot.kalshi")

CLIENT_PREFIX = "adb-"


class KalshiError(Exception):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{status} {path}: {body[:300]}")
        self.status, self.body, self.path = status, body, path


# ---------------------------------------------------------------- parsing
def dollars(obj: dict, key: str) -> float | None:
    """Read a price as dollars from `key_dollars` (str) or `key` (cents)."""
    v = obj.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = obj.get(key)
    if v in (None, ""):
        return None
    return float(v) / 100.0


def count(obj: dict, key: str) -> float | None:
    v = obj.get(f"{key}_fp")
    if v not in (None, ""):
        return float(v)
    v = obj.get(key)
    return None if v in (None, "") else float(v)


def parse_ts(v) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def parse_market(m: dict, asset: str) -> MarketMeta:
    strike = m.get("floor_strike")
    if strike in (None, ""):
        strike = m.get("cap_strike") if m.get("strike_type") == "less" else None
    return MarketMeta(
        ticker=m["ticker"],
        asset=asset,
        open_ts=parse_ts(m.get("open_time")),
        close_ts=parse_ts(m.get("close_time")),
        strike=float(strike) if strike not in (None, "") else None,
        strike_type=m.get("strike_type") or "greater",
        status=m.get("status") or "",
        result=(m.get("result") or "").lower(),
    )


def _levels(raw) -> list[tuple[float, float]]:
    out = []
    for lv in raw or []:
        p, q = lv[0], lv[1]
        if isinstance(p, str):
            p = float(p)              # dollar string
        else:
            p = float(p) / 100.0      # cents
        out.append((p, float(q)))
    return out


def parse_orderbook(body: dict) -> Book:
    ob = body.get("orderbook_fp") or body.get("orderbook") or {}
    yes = ob.get("yes_dollars") if ob.get("yes_dollars") is not None else ob.get("yes")
    no = ob.get("no_dollars") if ob.get("no_dollars") is not None else ob.get("no")
    return Book(yes_bids=Book.normalize(_levels(yes)), no_bids=Book.normalize(_levels(no)), ts=time.time())


def parse_order(o: dict) -> dict:
    """Normalise an order object to {id, client_id, status, filled, remaining, cost, fees}."""
    filled = count(o, "fill_count")
    remaining = count(o, "remaining_count")
    initial = count(o, "initial_count") or count(o, "count")
    if filled is None and initial is not None and remaining is not None:
        filled = initial - remaining
    cost = 0.0
    for k in ("taker_fill_cost", "maker_fill_cost"):
        v = dollars(o, k)
        cost += v or 0.0
    fees = 0.0
    for k in ("taker_fees", "maker_fees"):
        v = dollars(o, k)
        fees += v or 0.0
    return {
        "id": o.get("order_id", ""),
        "client_id": o.get("client_order_id", ""),
        "ticker": o.get("ticker", ""),
        "status": o.get("status", ""),
        "side": o.get("side", ""),
        "action": o.get("action", ""),
        "filled": filled or 0.0,
        "remaining": remaining or 0.0,
        "cost": cost,
        "fees": fees,
    }


# ---------------------------------------------------------------- keys
def keys_path() -> str:
    return os.path.join(DATA_DIR, "keys.yaml")


def load_keys() -> tuple[str, str] | None:
    p = keys_path()
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    if not d.get("key_id") or not d.get("private_key_path"):
        return None
    return d["key_id"], d["private_key_path"]


def save_keys(key_id: str, private_key_path: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(keys_path(), "w", encoding="utf-8") as f:
        yaml.safe_dump({"key_id": key_id, "private_key_path": private_key_path}, f)


class _Bucket:
    def __init__(self, rate: float):
        self.rate, self.tokens, self.last = rate, rate, time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


# ---------------------------------------------------------------- client
class KalshiClient:
    def __init__(self, base_url: str, key_id: str | None = None, key_path: str | None = None,
                 price_format: str = "cents", time_in_force: str = "immediate_or_cancel"):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path
        self.key_id = key_id
        self.key = None
        if key_path:
            with open(key_path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), password=None)
        self.price_format = price_format
        self.time_in_force = time_in_force
        self.session: aiohttp.ClientSession | None = None
        self.reads = _Bucket(8)
        self.writes = _Bucket(4)

    @property
    def authed(self) -> bool:
        return self.key is not None and bool(self.key_id)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True)
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _headers(self, method: str, path: str) -> dict:
        if not self.authed:
            return {}
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + self.path_prefix + path).encode()
        sig = self.key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _req(self, method: str, path: str, params: dict | None = None,
                   body: dict | None = None, auth: bool = False) -> dict:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True)
        await (self.writes if method in ("POST", "DELETE") else self.reads).take()
        headers = self._headers(method, path) if (auth or self.authed) else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with self.session.request(method, self.base_url + path, params=params,
                                        json=body, headers=headers) as r:
            text = await r.text()
            if r.status >= 400:
                raise KalshiError(r.status, text, path)
            return await r.json(content_type=None) if text else {}

    # ---- market data (public)
    async def markets(self, series: str, status: str | None = None, limit: int = 50) -> list[dict]:
        params = {"series_ticker": series, "limit": limit}
        if status:
            params["status"] = status
        d = await self._req("GET", "/markets", params=params)
        return d.get("markets") or []

    async def market(self, ticker: str) -> dict:
        d = await self._req("GET", f"/markets/{ticker}")
        return d.get("market") or d

    async def orderbook(self, ticker: str, depth: int = 10) -> Book:
        d = await self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_orderbook(d)

    # ---- portfolio (auth)
    async def balance(self) -> float:
        d = await self._req("GET", "/portfolio/balance", auth=True)
        v = dollars(d, "balance")
        if v is None:
            raise KalshiError(0, str(d), "/portfolio/balance")
        return v

    async def positions(self) -> dict[str, float]:
        """Signed contract count per ticker: +YES, -NO.  Only non-zero."""
        out: dict[str, float] = {}
        cursor = None
        for _ in range(20):
            params = {"limit": 200, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            d = await self._req("GET", "/portfolio/positions", params=params, auth=True)
            for p in d.get("market_positions") or []:
                q = count(p, "position")
                if q:
                    out[p["ticker"]] = q
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    async def resting_orders(self) -> list[dict]:
        d = await self._req("GET", "/portfolio/orders", params={"status": "resting", "limit": 200}, auth=True)
        return [parse_order(o) for o in d.get("orders") or []]

    async def get_order(self, order_id: str) -> dict:
        d = await self._req("GET", f"/portfolio/orders/{order_id}", auth=True)
        return parse_order(d.get("order") or d)

    async def cancel(self, order_id: str) -> None:
        try:
            await self._req("DELETE", f"/portfolio/orders/{order_id}", auth=True)
        except KalshiError as e:
            if e.status != 404:
                raise

    async def place(self, ticker: str, action: str, side: str, qty: int, limit: float,
                    reduce_only: bool = False) -> dict:
        cents = int(round(limit * 100))
        cents = min(99, max(1, cents))
        body = {
            "ticker": ticker,
            "client_order_id": CLIENT_PREFIX + uuid.uuid4().hex[:20],
            "action": action,
            "side": side,
            "count": int(qty),
            "type": "limit",
        }
        key = f"{side}_price"
        if self.price_format == "dollars":
            body[key + "_dollars"] = f"{cents / 100:.2f}"
        else:
            body[key] = cents
        if self.time_in_force:
            body["time_in_force"] = self.time_in_force
        if reduce_only:
            body["reduce_only"] = True
        d = await self._req("POST", "/portfolio/orders", body=body, auth=True)
        o = parse_order(d.get("order") or d)
        o["client_id"] = o["client_id"] or body["client_order_id"]
        return o
