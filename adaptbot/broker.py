"""Order execution: PaperBroker (simulated fills on the live book) and
LiveBroker (real orders on Kalshi).  Both return (qty, avg_price, fee, uncertain)."""
from __future__ import annotations

import asyncio
import logging

from .adaptive import sim_fill
from .fees import order_fee
from .kalshi import CLIENT_PREFIX, KalshiClient, KalshiError, count, fill_fee, fill_price
from .market import Book
from .strategy import Intent

log = logging.getLogger("adaptbot.broker")


class PaperBroker:
    live = False
    fatal = ""

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

    async def cancel_own_resting(self, prefixes: tuple = ()) -> int:
        return 0


class LiveBroker:
    live = True

    def __init__(self, client: KalshiClient, fee_rate: float, shard: int = 0):
        self.c, self.fee_rate, self.shard = client, fee_rate, shard
        self.fatal = ""          # set on auth errors: the engine halts

    async def execute(self, ticker: str, it: Intent, book: Book) -> tuple[float, float, float, bool]:
        """Immediate-or-cancel order.  `uncertain` = we do not know whether it
        traded (network error) and the caller must reconcile now."""
        try:
            o = await self.c.place(ticker, it.action, it.side, it.qty, it.limit,
                                   reduce_only=(it.action == "sell"))
        except KalshiError as e:
            log.warning("order rejected %s %s %s x%d @%.2f: %s", ticker, it.action, it.side, it.qty, it.limit, e)
            if e.status in (401, 403):
                self.fatal = f"Kalshi refused the order ({e.status}) - check API key permissions"
            return 0.0, 0.0, 0.0, e.status in (0, 500, 502, 503, 504)
        except Exception as e:  # noqa: BLE001 - network: state unknown
            log.error("order send failed %s: %r - reconciling", ticker, e)
            return 0.0, 0.0, 0.0, True

        if o["status"] == "resting" or o["remaining"] > 0 and o["status"] not in ("canceled", "executed", ""):
            try:   # IOC should never rest; make sure it does not
                await self.c.cancel(o["id"], ticker)
            except Exception as e:  # noqa: BLE001
                log.error("cancel of %s failed: %r", o["id"], e)
                return 0.0, 0.0, 0.0, True

        q = float(o["filled"])
        if q <= 0:
            return 0.0, 0.0, 0.0, False
        # exact prices and fees come from the fills endpoint (may lag a moment)
        fills: list = []
        for _ in range(4):
            try:
                fills = [f for f in await self.c.fills(order_id=o["id"]) if f.get("order_id") in (None, o["id"])]
            except Exception as e:  # noqa: BLE001
                log.warning("fills lookup for %s failed: %r", o["id"], e)
            if sum(count(f, "count") or 0.0 for f in fills) >= q - 1e-9:
                break
            await asyncio.sleep(0.5)
        n = sum(count(f, "count") or 0.0 for f in fills)
        if n >= q - 1e-9 and n > 0:
            avg = sum((count(f, "count") or 0.0) * fill_price(f, it.side) for f in fills) / n
            fee = sum(fill_fee(f) for f in fills)
            q = n
        else:
            log.warning("fills for %s not available yet - booking at limit %.2f", o["id"], it.limit)
            avg, fee = it.limit, order_fee(it.limit, q, self.fee_rate)
        # never book an impossible price: fall back to the worst case (our limit)
        if it.action == "buy" and not (0 < avg <= it.limit + 1e-6):
            avg = it.limit
        if it.action == "sell" and not (it.limit - 1e-6 <= avg < 1):
            avg = it.limit
        return q, avg, fee, False

    def settle(self, qty: float, won: bool) -> None:
        pass   # Kalshi pays out by itself

    async def cash(self) -> float:
        return await self.c.balance(self.shard)

    async def positions(self) -> dict[str, float] | None:
        return await self.c.positions()

    async def cancel_own_resting(self, prefixes: tuple = ()) -> int:
        n = 0
        for o in await self.c.resting_orders():
            if o["client_id"].startswith(CLIENT_PREFIX) and (not prefixes or o["ticker"].startswith(prefixes)):
                await self.c.cancel(o["id"], o["ticker"])
                n += 1
        return n
