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
                 time_in_force: str = "immediate_or_cancel"):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path
        self.key_id = key_id
        self.key = None
        if key_path:
            with open(key_path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), password=None)
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

    async def series(self, series: str) -> dict:
        d = await self._req("GET", f"/series/{series}")
        return d.get("series") or d

    # ---- portfolio (auth)
    async def balances(self) -> dict[int, float]:
        """Cash in dollars per exchange shard.  Kalshi runs crypto markets on their
        own shard (2 since 2026-08-24); orders there need cash ON that shard."""
        d = await self._req("GET", "/portfolio/balance", auth=True)
        out: dict[int, float] = {}
        for it in d.get("balance_breakdown") or []:
            try:
                out[int(it.get("exchange_index", 0))] = float(it.get("balance", 0))
            except (TypeError, ValueError):
                pass
        if not out:
            v = dollars(d, "balance")
            if v is None:
                raise KalshiError(0, str(d), "/portfolio/balance")
            out[0] = v
        return out

    async def balance(self, shard: int = 0) -> float:
        b = await self.balances()
        return b.get(shard, 0.0) if len(b) > 1 or shard in b else next(iter(b.values()))

    async def transfer_between_shards(self, amount: float, dest: int, source: int = 0) -> dict:
        """Move your own cash between exchange shards of the same account."""
        body = {"source": "event_contract", "destination": "event_contract",
                "amount": int(round(amount * 10000)),          # centicents
                "source_exchange_shard": int(source), "destination_exchange_shard": int(dest)}
        return await self._req("POST", "/portfolio/intra_exchange_instance_transfer", body=body, auth=True)

    async def positions(self) -> dict[str, float]:
        """Signed contract count per ticker: +YES, -NO.  Only non-zero."""
        out: dict[str, float] = {}
        cursor = None
        for _ in range(20):
            params = {"limit": 200}
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

    async def fills(self, order_id: str | None = None, min_ts: int | None = None) -> list[dict]:
        params: dict = {"limit": 200}
        if order_id:
            params["order_id"] = order_id
        if min_ts:
            params["min_ts"] = min_ts
        d = await self._req("GET", "/portfolio/fills", params=params, auth=True)
        return d.get("fills") or []

    async def cancel(self, order_id: str, ticker: str | None = None) -> None:
        try:
            await self._req("DELETE", f"/portfolio/events/orders/{order_id}",
                            params={"market_ticker": ticker} if ticker else None, auth=True)
        except KalshiError as e:
            if e.status not in (400, 404, 409):
                raise

    async def place(self, ticker: str, action: str, side: str, qty: int, limit: float,
                    reduce_only: bool = False) -> dict:
        """V2 order (the V1 /portfolio/orders endpoints were retired in June 2026).

        V2 orders are written on the YES book:
          buy YES @p -> bid p        sell YES @p -> ask p
          buy NO  @q -> ask 1-q      sell NO  @q -> bid 1-q
        """
        book_side, yes_px = v2_side_price(side, action, limit)
        body = {
            "ticker": ticker,
            "client_order_id": CLIENT_PREFIX + uuid.uuid4().hex[:20],
            "side": book_side,
            "count": f"{float(qty):.2f}",
            "price": f"{yes_px:.4f}",
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if reduce_only:
            body["reduce_only"] = True
        d = await self._req("POST", "/portfolio/events/orders", body=body, auth=True)
        o = parse_order(d.get("order") or d)
        o["client_id"] = o["client_id"] or body["client_order_id"]
        return o


def v2_side_price(side: str, action: str, limit: float) -> tuple[str, float]:
    """(YES-book side, YES-book price) for buying/selling `side` at `limit` (that side's price)."""
    limit = min(0.99, max(0.01, round(limit, 2)))
    buy = action == "buy"
    if side == "yes":
        return ("bid" if buy else "ask"), limit
    return ("ask" if buy else "bid"), round(1.0 - limit, 4)


def fill_price(f: dict, side: str) -> float:
    """Price of a fill in terms of `side` (fills report the YES price)."""
    yp = f.get("yes_price_dollars", f.get("yes_price_fixed"))
    if yp not in (None, ""):
        y = float(yp)
    elif f.get("yes_price") is not None:
        y = float(f["yes_price"]) / 100.0
    else:
        return 0.0
    return y if side == "yes" else round(1.0 - y, 4)


def fill_fee(f: dict) -> float:
    for k in ("fee_cost", "fee_cost_dollars"):
        v = f.get(k)
        if v not in (None, ""):
            return float(v)
    return 0.0
