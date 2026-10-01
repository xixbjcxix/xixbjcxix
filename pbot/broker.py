"""Market data + execution back-ends.

  live   PublicMarketData + LiveBroker   real quotes, REAL orders on your Public account
  paper  PublicMarketData + PaperBroker  real quotes, simulated fills (no orders sent)
  sim    SimMarket        + PaperBroker  synthetic market on a virtual clock (offline demo/tests)
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .clock import MarketCalendar, SimClock
from .config import ExecutionConfig
from .public_api import Bar, OrderState, PublicClient, Quote, _f

log = logging.getLogger("pbot.broker")


# ---------------------------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------------------------
class PublicMarketData:
    def __init__(self, client: PublicClient):
        self.client = client

    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return self.client.quotes(symbols)

    def bars(self, symbol: str) -> List[Bar]:
        return self.client.bars_today(symbol)


class SimMarket:
    """Seeded random-walk market with per-symbol drift so some names trend out of the range."""

    def __init__(self, symbols: List[str], clock: SimClock, calendar: MarketCalendar, seed: int = 7):
        self.clock = clock
        self.calendar = calendar
        self.paths: Dict[str, List[Tuple[float, float, float, float, float]]] = {}
        rng = random.Random(seed)
        for s in symbols:
            p = rng.uniform(8, 80)
            drift = rng.uniform(-0.0003, 0.0006)
            vol = rng.uniform(0.0008, 0.0025)
            rows = []
            for i in range(390):
                v = vol * (2.5 if i < 15 else 1.0)            # the open is the most volatile part
                c = p * math.exp(drift + rng.gauss(0, v))
                hi = max(p, c) * (1 + abs(rng.gauss(0, v / 3)))
                lo = min(p, c) * (1 - abs(rng.gauss(0, v / 3)))
                volume = rng.uniform(20000, 120000) * (3 if i < 15 else 1)
                rows.append((p, hi, lo, c, volume))
                p = c
            self.paths[s] = rows

    def _minute(self) -> Tuple[int, float]:
        now = self.clock.now()
        start = self.calendar.open_time(now.date())
        el = (now - start).total_seconds() / 60.0
        return int(math.floor(el)), el - math.floor(el)

    def price(self, symbol: str) -> float:
        i, frac = self._minute()
        rows = self.paths[symbol]
        if i < 0:
            return rows[0][0]
        if i >= len(rows):
            return rows[-1][3]
        o, _, _, c, _ = rows[i]
        return o + (c - o) * frac

    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out = {}
        for s in symbols:
            p = self.price(s)
            half = max(0.005, p * 0.0002)
            out[s] = Quote(s, round(p, 2), round(p - half, 2), round(p + half, 2))
        return out

    def bars(self, symbol: str) -> List[Bar]:
        i, _ = self._minute()
        rows = self.paths[symbol][:max(0, min(i + 1, 390))]   # includes the forming bar, like Public
        start = self.calendar.open_time(self.clock.now().date())
        return [Bar((start + timedelta(minutes=k)).isoformat(), o, h, l, c, v)
                for k, (o, h, l, c, v) in enumerate(rows)]


# ---------------------------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------------------------
class PaperBroker:
    """Instant fills against the current quote: buys at the ask, sells at the bid.

    Marketable limits fill only if the limit crosses the quote, otherwise nothing fills (like a
    timed-out order). Optimistic about queue/latency - real fills will be somewhat worse."""

    software_stops_only = True

    def __init__(self, quote_fn: Callable[[str], Optional[Quote]], starting_cash: float):
        self.quote_fn = quote_fn
        self.cash = starting_cash
        self.pos: Dict[str, Tuple[float, float]] = {}      # symbol -> (qty, avg)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"paper-{self._n}"

    def account(self) -> Tuple[float, float]:
        eq = self.cash
        for s, (q, avg) in self.pos.items():
            qt = self.quote_fn(s)
            eq += q * (qt.bid if qt and qt.bid else avg)
        return eq, self.cash

    def positions(self) -> Dict[str, float]:
        return {s: q for s, (q, _) in self.pos.items() if q > 0}

    def buy(self, symbol: str, qty: float, limit: float) -> OrderState:
        q = self.quote_fn(symbol)
        if not q or q.ask is None or limit < q.ask or qty <= 0 or qty * q.ask > self.cash:
            return OrderState(self._id(), "CANCELLED")
        self.cash -= qty * q.ask
        old_q, old_avg = self.pos.get(symbol, (0.0, 0.0))
        nq = old_q + qty
        self.pos[symbol] = (nq, (old_q * old_avg + qty * q.ask) / nq)
        return OrderState(self._id(), "FILLED", qty, q.ask)

    def sell(self, symbol: str, qty: float, limit: Optional[float] = None) -> OrderState:
        q = self.quote_fn(symbol)
        have, avg = self.pos.get(symbol, (0.0, 0.0))
        qty = min(qty, have)
        px = q.bid if q and q.bid else (q.last if q else None)
        if px is None or qty <= 0 or (limit is not None and limit > px):
            return OrderState(self._id(), "CANCELLED")
        self.cash += qty * px
        rest = have - qty
        if rest > 1e-9:
            self.pos[symbol] = (rest, avg)
        else:
            self.pos.pop(symbol, None)
        return OrderState(self._id(), "FILLED", qty, px)

    def place_stop(self, symbol: str, qty: float, stop: float) -> Optional[str]:
        return None

    def stop_state(self, order_id: str) -> Optional[OrderState]:
        return None

    def cancel(self, order_id: str) -> Optional[OrderState]:
        return None

    def open_order_ids(self, symbols: List[str]) -> List[str]:
        return []


class LiveBroker:
    """Real orders on Public. Orders are asynchronous there, so every call polls to a final state."""

    software_stops_only = False

    def __init__(self, client: PublicClient, cfg: ExecutionConfig, sleep: Callable[[float], None],
                 use_cash_only: bool = True):
        self.client = client
        self.cfg = cfg
        self.sleep = sleep
        self.use_cash_only = use_cash_only

    def account(self) -> Tuple[float, float]:
        p = self.client.portfolio()
        bp = p.get("buyingPower") or {}
        buying_power = _f(bp.get("cashOnlyBuyingPower" if self.use_cash_only else "buyingPower"))
        equity = _f(p.get("totalAccountValue"))
        if equity is None:
            equity = sum(_f(e.get("value")) or 0.0 for e in p.get("equity") or [])
        return equity or 0.0, buying_power or 0.0

    def positions(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for pos in self.client.portfolio().get("positions") or []:
            inst = pos.get("instrument") or {}
            if inst.get("type", "EQUITY") == "EQUITY":
                q = _f(pos.get("quantity")) or 0.0
                if q:
                    out[inst.get("symbol")] = q
        return out

    def open_order_ids(self, symbols: List[str]) -> List[str]:
        """Working orders for these symbols only - never touch orders you placed on other names."""
        return [o.get("orderId") for o in self.client.portfolio().get("orders") or []
                if o.get("orderId") and (o.get("instrument") or {}).get("symbol") in symbols
                and o.get("status") in ("NEW", "PARTIALLY_FILLED", "PENDING_REPLACE")]

    def _await(self, oid: str, timeout: float) -> OrderState:
        waited, st = 0.0, None
        while waited < timeout:
            st = self.client.get_order(oid)
            if st and st.terminal:
                return st
            self.sleep(0.5)
            waited += 0.5
        return st or OrderState(oid, "UNKNOWN")

    def _await_or_cancel(self, oid: str, timeout: float) -> OrderState:
        st = self._await(oid, timeout)
        if st.terminal:
            return st
        self.client.cancel_order(oid)
        st2 = self._await(oid, 10.0)
        if not st2.terminal:
            log.warning("order %s still %s after cancel - check the Public app", oid, st2.status)
        return st2

    def buy(self, symbol: str, qty: float, limit: float) -> OrderState:
        oid = self.client.place_order(symbol, "BUY", qty, "LIMIT", limit_price=limit)
        return self._await_or_cancel(oid, self.cfg.order_timeout_s)

    def sell(self, symbol: str, qty: float, limit: Optional[float] = None) -> OrderState:
        if limit is None:
            oid = self.client.place_order(symbol, "SELL", qty, "MARKET")
            return self._await_or_cancel(oid, 20.0)
        oid = self.client.place_order(symbol, "SELL", qty, "LIMIT", limit_price=limit)
        return self._await_or_cancel(oid, self.cfg.order_timeout_s)

    def place_stop(self, symbol: str, qty: float, stop: float) -> Optional[str]:
        return self.client.place_order(symbol, "SELL", qty, "STOP", stop_price=stop)

    def stop_state(self, order_id: str) -> Optional[OrderState]:
        return self.client.get_order(order_id)

    def cancel(self, order_id: str) -> Optional[OrderState]:
        """Cancel and return the final state (FILLED means the stop had already triggered)."""
        st = self.client.get_order(order_id)
        if st and st.terminal:
            return st
        self.client.cancel_order(order_id)
        return self._await(order_id, 10.0)


def make_sim(symbols: List[str], calendar: MarketCalendar, day: datetime, seed: int,
             starting_cash: float):
    clock = SimClock(day)
    market = SimMarket(symbols, clock, calendar, seed)
    broker = PaperBroker(lambda s: market.quotes([s]).get(s), starting_cash)
    return clock, market, broker
