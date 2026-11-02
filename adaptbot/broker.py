"""Order execution: PaperBroker (simulated fills on the live book) and
LiveBroker (real orders on Kalshi).  Both return (qty, avg_price, fee)."""
from __future__ import annotations

import asyncio
import logging
import time

from .adaptive import sim_fill
from .fees import order_fee
from .kalshi import CLIENT_PREFIX, KalshiClient, KalshiError
from .market import Book
from .strategy import Intent

log = logging.getLogger("adaptbot.broker")


class PaperBroker:
    live = False

    def __init__(self, cash: float, fee_rate: float, slip: float, fill_ratio: float):
        self._cash, self.fee_rate, self.slip, self.fill_ratio = cash, fee_rate, slip, fill_ratio

    async def execute(self, ticker: str, it: Intent, book: Book) -> tuple[float, float, float, bool]:
        q, avg, fee = sim_fill(book, it, self.fee_rate, self.slip, self.fill_ratio)
        if q > 0:
            if it.action == "buy":
                self._cash -= q * avg + fee
            else:
                self._cash += q * avg - fee
        return q, avg, fee, False

    def settle(self, qty: float, won: bool) -> None:
        if won:
            self._cash += qty

    async def cash(self) -> float:
        return self._cash

    async def positions(self) -> dict[str, float] | None:
        return None   # paper has nothing to reconcile against

    async def cancel_own_resting(self) -> int:
        return 0


class LiveBroker:
    live = True

    def __init__(self, client: KalshiClient, fee_rate: float, ttl: float):
        self.c, self.fee_rate, self.ttl = client, fee_rate, ttl

    async def execute(self, ticker: str, it: Intent, book: Book) -> tuple[float, float, float, bool]:
        """Returns (qty, avg, fee, uncertain).  `uncertain` means we do not
        know whether the order traded - the caller must reconcile now."""
        try:
            o = await self.c.place(ticker, it.action, it.side, it.qty, it.limit,
                                   reduce_only=(it.action == "sell"))
        except KalshiError as e:
            log.warning("order rejected %s %s %s x%d @%.2f: %s", ticker, it.action, it.side, it.qty, it.limit, e)
            return 0.0, 0.0, 0.0, False
        except Exception as e:  # noqa: BLE001 - network: state unknown
            log.error("order send failed %s: %r - reconciling", ticker, e)
            return 0.0, 0.0, 0.0, True

        deadline = time.time() + self.ttl
        try:
            while o["status"] == "resting" and time.time() < deadline:
                await asyncio.sleep(0.4)
                o = await self.c.get_order(o["id"])
            if o["status"] == "resting":
                await self.c.cancel(o["id"])
                o = await self.c.get_order(o["id"])
        except Exception as e:  # noqa: BLE001
            log.error("order follow-up failed %s %s: %r - reconciling", ticker, o.get("id"), e)
            return 0.0, 0.0, 0.0, True

        q = float(o["filled"])
        if q <= 0:
            return 0.0, 0.0, 0.0, False
        avg = o["cost"] / q if o["cost"] > 0 else it.limit
        # Never trust an implausible price; fall back to the worst case (our limit).
        if it.action == "buy" and not (0 < avg <= it.limit + 1e-6):
            avg = it.limit
        if it.action == "sell" and not (it.limit - 1e-6 <= avg < 1):
            avg = it.limit
        fee = o["fees"] if o["fees"] > 0 else order_fee(avg, q, self.fee_rate)
        return q, avg, fee, False

    def settle(self, qty: float, won: bool) -> None:
        pass   # Kalshi pays out by itself

    async def cash(self) -> float:
        return await self.c.balance()

    async def positions(self) -> dict[str, float] | None:
        return await self.c.positions()

    async def cancel_own_resting(self) -> int:
        n = 0
        for o in await self.c.resting_orders():
            if o["client_id"].startswith(CLIENT_PREFIX):
                await self.c.cancel(o["id"])
                n += 1
        return n
