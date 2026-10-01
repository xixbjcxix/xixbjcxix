"""Thin client for the Public.com Trading API (https://public.com/api/docs).

Auth: your API *secret key* (Public app -> Settings -> API) is exchanged for a short-lived
bearer token at POST /userapiauthservice/personal/access-tokens. The token is refreshed
automatically before it expires and once more on a 401.

Endpoints used (same paths as Public's official `publicdotcom-py` SDK):
  GET    /userapigateway/trading/account                         accounts
  GET    /userapigateway/trading/{acct}/portfolio/v2             buying power, positions, open orders
  POST   /userapigateway/marketdata/{acct}/quotes                last / bid / ask
  GET    /userapigateway/historicdata/EQUITY/{sym}/DAY/ONE_MINUTE  today's 1-minute bars
  POST   /userapigateway/trading/{acct}/order                    place (asynchronous; client-chosen UUID)
  GET    /userapigateway/trading/{acct}/order/{id}               order status / fills
  DELETE /userapigateway/trading/{acct}/order/{id}               cancel
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

log = logging.getLogger("pbot.api")

TERMINAL = {"FILLED", "CANCELLED", "QUEUED_CANCELLED", "REJECTED", "EXPIRED", "REPLACED"}


class PublicAPIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


@dataclass
class Quote:
    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    ts: Optional[str] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100
        return None


@dataclass
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderState:
    order_id: str
    status: str
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    reject_reason: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_num(x: float, places: int = 2) -> str:
    s = f"{x:.{places}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


class PublicClient:
    def __init__(self, secret: str, account_id: Optional[str] = None,
                 base_url: str = "https://api.public.com", token_validity_minutes: int = 60,
                 http: Optional[httpx.Client] = None, sleep: Callable[[float], None] = time.sleep):
        if not secret:
            raise ValueError("missing Public API secret key - run `python -m pbot setup`")
        self.secret = secret
        self.account_id = account_id
        self.base_url = base_url.rstrip("/")
        self.validity = max(5, min(1440, int(token_validity_minutes)))
        self.http = http or httpx.Client(timeout=15.0)
        self.sleep = sleep
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    # ---- plumbing -------------------------------------------------------------------------
    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        r = self.http.post(f"{self.base_url}/userapiauthservice/personal/access-tokens",
                           json={"secret": self.secret, "validityInMinutes": self.validity},
                           headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            raise PublicAPIError(r.status_code, "could not create access token - check the secret key "
                                 "in .env (PUBLIC_API_SECRET)", _safe_json(r))
        tok = (_safe_json(r) or {}).get("accessToken")
        if not tok:
            raise PublicAPIError(r.status_code, "auth response had no accessToken")
        self._token = tok
        self._token_expiry = time.time() + max(60, (self.validity - 5) * 60)
        return tok

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None,
                 retry: bool = True) -> Any:
        url = f"{self.base_url}{path}"
        attempts = 4 if retry else 1
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                r = self.http.request(method, url, json=json, params=params,
                                      headers={"Authorization": f"Bearer {self._auth()}",
                                               "Content-Type": "application/json"})
            except httpx.TransportError as e:
                last_exc = e
                if attempt + 1 < attempts:
                    self.sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise
            if r.status_code == 401 and attempt == 0:
                self._token = None                    # token revoked/expired early: re-auth once
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt + 1 < attempts:
                ra = _f(r.headers.get("Retry-After"))
                self.sleep(ra if ra is not None else min(8.0, 0.5 * 2 ** attempt))
                continue
            if r.status_code >= 400:
                body = _safe_json(r)
                msg = (body or {}).get("message") if isinstance(body, dict) else None
                raise PublicAPIError(r.status_code, msg or r.text[:300], body)
            return _safe_json(r) if r.content else {}
        if last_exc:
            raise last_exc
        raise PublicAPIError(0, "request failed after retries")

    def _acct(self) -> str:
        if not self.account_id:
            raise ValueError("no account id - run `python -m pbot setup`")
        return self.account_id

    # ---- account ----------------------------------------------------------------------------
    def accounts(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/userapigateway/trading/account").get("accounts", [])

    def portfolio(self) -> Dict[str, Any]:
        return self._request("GET", f"/userapigateway/trading/{self._acct()}/portfolio/v2")

    # ---- market data ------------------------------------------------------------------------
    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        body = {"instruments": [{"symbol": s, "type": "EQUITY"} for s in symbols]}
        data = self._request("POST", f"/userapigateway/marketdata/{self._acct()}/quotes", json=body)
        out: Dict[str, Quote] = {}
        for q in data.get("quotes", []):
            if q.get("outcome", "SUCCESS") != "SUCCESS":
                continue
            sym = (q.get("instrument") or {}).get("symbol")
            if sym:
                out[sym] = Quote(sym, _f(q.get("last")), _f(q.get("bid")), _f(q.get("ask")),
                                 q.get("lastTimestamp"))
        return out

    def bars_today(self, symbol: str) -> List[Bar]:
        """Today's regular-session 1-minute bars, oldest first (bar i covers open + i minutes)."""
        data = self._request("GET", f"/userapigateway/historicdata/EQUITY/{symbol}/DAY/ONE_MINUTE")
        bars = ((data.get("regularMarket") or {}).get("bars")) or []
        out = []
        for b in bars:
            o, h, l, c = (_f(b.get(k)) for k in ("open", "high", "low", "close"))
            if None in (o, h, l, c):
                continue
            out.append(Bar(str(b.get("timestamp")), o, h, l, c, _f(b.get("volume")) or 0.0))
        return out

    # ---- orders -----------------------------------------------------------------------------
    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "LIMIT",
                    limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                    order_id: Optional[str] = None) -> str:
        oid = order_id or str(uuid.uuid4())
        body: Dict[str, Any] = {
            "orderId": oid,
            "instrument": {"symbol": symbol, "type": "EQUITY"},
            "orderSide": side,
            "orderType": order_type,
            "expiration": {"timeInForce": "DAY"},
            "quantity": fmt_num(qty, 5),
            "equityMarketSession": "CORE",
        }
        if limit_price is not None:
            body["limitPrice"] = fmt_num(limit_price, 2)
        if stop_price is not None:
            body["stopPrice"] = fmt_num(stop_price, 2)
        path = f"/userapigateway/trading/{self._acct()}/order"
        try:
            self._request("POST", path, json=body, retry=False)
        except (httpx.TransportError, PublicAPIError) as e:
            if isinstance(e, PublicAPIError) and e.status < 500 and e.status != 429:
                raise
            # Unknown outcome: the orderId makes this safe to check before trying once more.
            self.sleep(1.0)
            if self.get_order(oid) is None:
                self._request("POST", path, json=body, retry=False)
        return oid

    def get_order(self, order_id: str) -> Optional[OrderState]:
        """None while the (asynchronous) order is not yet visible."""
        try:
            d = self._request("GET", f"/userapigateway/trading/{self._acct()}/order/{order_id}")
        except PublicAPIError as e:
            if e.status == 404:
                return None
            raise
        return OrderState(order_id, str(d.get("status", "UNKNOWN")),
                          _f(d.get("filledQuantity")) or 0.0, _f(d.get("averagePrice")),
                          d.get("rejectReason"))

    def cancel_order(self, order_id: str) -> None:
        try:
            self._request("DELETE", f"/userapigateway/trading/{self._acct()}/order/{order_id}")
        except PublicAPIError as e:
            if e.status not in (400, 404, 409):     # already terminal / unknown: nothing to cancel
                raise


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
