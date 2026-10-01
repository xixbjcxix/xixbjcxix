"""Real order execution on Kalshi: the DEMO environment (stage 2) or PRODUCTION (stage 3, `live`).

Implements the same surface as the simulator (submit / cancel / advance / active_orders), so
TradingCore runs unchanged. Orders go out over REST (V2 endpoints), fills come back from the
`fill` websocket channel, with a REST /portfolio/fills poll as a backstop (deduped by trade_id).

Safety:
  * production hosts are refused unless the engine was started in live mode (allow_prod=True)
  * cancels only the bot's OWN orders (client_order_id prefix "kbot-", in the bot's series) on start,
    on any data gap, on kill and on shutdown - never your manual orders elsewhere
  * markets where you already held a position when the bot started are left alone
  * periodic position reconciliation: if Kalshi's net position disagrees with ours twice in a
    row, the kill switch trips
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable, Dict, List, Optional

from .feeds.kalshi_rest import KalshiError, KalshiRest
from .models import BUY, DOWN, SELL, UP, Fill, MarketInfo, Order, OrderStatus, px

log = logging.getLogger("kbot.demo_exchange")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


COID_PREFIX = "kbot-"


class KalshiExchange:
    def __init__(self, rest: KalshiRest, core, allow_prod: bool = False, series_prefixes=()) -> None:
        host = rest.base
        if "demo" not in host and not allow_prod:
            raise RuntimeError(f"refusing to send orders to {host}: production orders need `live` mode")
        self.series_prefixes = tuple(series_prefixes)
        self.baseline: Dict[str, float] = {}        # positions you held before the bot started
        self.rest = rest
        self.core = core
        self.orders: Dict[str, Order] = {}          # our id -> order
        self.by_client: Dict[str, str] = {}         # client_order_id -> our id
        self.by_exch: Dict[str, str] = {}           # kalshi order_id -> our id
        self._fills: List[Fill] = []
        self._seen_trades: set = set()
        self.rejections = 0
        self.on_final: Optional[Callable[[Order], None]] = None
        self._tasks: set = set()
        self.last_fill_poll_s = int(time.time()) - 5
        self.errors: List[str] = []

    # ---- helpers --------------------------------------------------------------------------
    def _spawn(self, coro) -> None:
        t = asyncio.get_event_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _final(self, o: Order) -> None:
        if self.on_final:
            self.on_final(o)

    def _market(self, o: Order) -> MarketInfo:
        return self.core.markets[o.market].info

    # ---- engine surface -------------------------------------------------------------------------
    def submit(self, o: Order, market: MarketInfo, now_ms: int) -> Order:
        o.created_ms = now_ms
        o.status = OrderStatus.PENDING
        o.token = market.token(o.outcome)
        coid = f"{COID_PREFIX}{o.id}-{uuid.uuid4().hex[:8]}"
        self.orders[o.id] = o
        self.by_client[coid] = o.id
        self._spawn(self._place(o, market, coid))
        return o

    def cancel(self, oid: str, now_ms: int) -> None:
        o = self.orders.get(oid)
        if o is None or not o.is_active or o.cancel_req_ms is not None:
            return
        o.cancel_req_ms = now_ms
        if o.exchange_id:
            self._spawn(self._cancel(o))
        # else: _place will cancel it as soon as the exchange id is known

    def cancel_everything(self) -> None:
        self._spawn(self._cancel_all())

    def advance(self, now_ms: int) -> List[Fill]:
        out, self._fills = self._fills, []
        return out

    def active_orders(self, market: Optional[str] = None) -> List[Order]:
        return [o for o in self.orders.values() if o.is_active and (market is None or o.market == market)]

    def on_book_event(self, ev) -> List[Fill]:
        return []

    def on_trade(self, t) -> List[Fill]:
        return []

    # ---- REST actions ---------------------------------------------------------------------------
    async def _place(self, o: Order, m: MarketInfo, coid: str) -> None:
        try:
            r = await self.rest.create_order(
                ticker=m.slug, outcome_yes=(o.outcome == UP), buy=(o.side == BUY), price=o.price,
                count=o.size, post_only=o.post_only,
                tif="good_till_canceled" if o.post_only else "immediate_or_cancel",
                reduce_only=(o.side == SELL), client_order_id=coid)
        except KalshiError as e:
            o.status = OrderStatus.REJECTED
            o.reject_reason = f"kalshi {e.status}: {e.body[:120]}"
            self.rejections += 1
            self.errors.append(o.reject_reason)
            self._final(o)
            if e.status in (401, 403):
                self.core.kill(f"auth_error_{e.status}")
            return
        except Exception as e:  # noqa: BLE001 - network: we don't know if it rested; cancel-all to be safe
            o.status = OrderStatus.REJECTED
            o.reject_reason = f"network: {type(e).__name__}"
            self.rejections += 1
            self._final(o)
            await self._cancel_all()
            return
        o.exchange_id = str(r.get("order_id", ""))
        self.by_exch[o.exchange_id] = o.id
        remaining = _f(r.get("remaining_count", o.size))
        o.live_ms = int(time.time() * 1000)
        if not o.post_only or remaining <= 1e-9:
            # IOC: whatever didn't fill is gone; fills themselves arrive on the fill channel
            if o.status == OrderStatus.PENDING:
                o.status = OrderStatus.CANCELLED if remaining > 1e-9 else OrderStatus.FILLED
                if remaining > 1e-9 and _f(r.get("fill_count", 0)) <= 1e-9:
                    o.reject_reason = "no_liquidity_at_limit"
            self._final(o)
            return
        if o.status == OrderStatus.PENDING:
            o.status = OrderStatus.OPEN
        if o.cancel_req_ms is not None:
            await self._cancel(o)

    async def _cancel(self, o: Order) -> None:
        try:
            await self.rest.cancel_order(o.exchange_id, ticker=o.market)
        except KalshiError as e:
            if e.status not in (404, 409, 400):
                log.warning("cancel %s failed: %s", o.exchange_id, e)
                return
        if o.is_active:
            o.status = OrderStatus.CANCELLED
            self._final(o)

    def _is_ours(self, od: dict) -> bool:
        coid = str(od.get("client_order_id") or "")
        tk = str(od.get("ticker") or od.get("market_ticker") or "")
        in_series = not self.series_prefixes or tk.startswith(self.series_prefixes)
        return coid.startswith(COID_PREFIX) and in_series

    async def _cancel_all(self) -> None:
        """Cancel the bot's own orders only (never your manual orders)."""
        for o in list(self.orders.values()):
            if o.is_active and o.exchange_id:
                await self._cancel(o)
        try:
            resting = await self.rest.get_open_orders()
        except Exception as e:  # noqa: BLE001
            log.error("could not list open orders to sweep: %s", e)
            self.errors.append(f"sweep failed: {e}")
            return
        for od in resting:
            if self._is_ours(od):
                try:
                    await self.rest.cancel_order(str(od.get("order_id")), ticker=od.get("ticker"))
                except KalshiError as e:
                    if e.status not in (404, 409, 400):
                        log.error("sweep cancel %s failed: %s", od.get("order_id"), e)

    async def sweep_own_orders(self) -> int:
        """Startup: cancel resting orders left by an earlier bot session. Returns how many."""
        resting = await self.rest.get_open_orders()
        n = 0
        for od in resting:
            if self._is_ours(od):
                await self.rest.cancel_order(str(od.get("order_id")), ticker=od.get("ticker"))
                n += 1
        return n

    async def load_baseline(self) -> Dict[str, float]:
        pos = await self.rest.get_positions()
        self.baseline = {p.get("ticker"): _f(p.get("position_fp", p.get("position", 0))) for p in pos
                         if abs(_f(p.get("position_fp", p.get("position", 0)))) > 1e-9}
        return self.baseline

    # ---- fills ----------------------------------------------------------------------------------
    def on_private_frame(self, msg: dict) -> None:
        t = msg.get("type")
        body = msg.get("msg") or {}
        if t == "fill":
            self._on_fill(body)
        elif t in ("user_order", "user_orders", "order"):
            oid = self.by_exch.get(str(body.get("order_id", ""))) or self.by_client.get(str(body.get("client_order_id", "")))
            o = self.orders.get(oid) if oid else None
            if o is None:
                return
            st = str(body.get("status", ""))
            if st == "resting" and o.status == OrderStatus.PENDING:
                o.status = OrderStatus.OPEN
            elif st == "canceled" and o.is_active:
                o.status = OrderStatus.CANCELLED
                self._final(o)
            elif st == "executed" and o.is_active and o.filled >= o.size - 1e-9:
                o.status = OrderStatus.FILLED
                self._final(o)

    def _on_fill(self, b: dict) -> None:
        tid = str(b.get("trade_id") or b.get("fill_id") or "")
        if not tid or tid in self._seen_trades:
            return
        oid = self.by_exch.get(str(b.get("order_id", ""))) or self.by_client.get(str(b.get("client_order_id", "")))
        o = self.orders.get(oid) if oid else None
        if o is None:
            return                                  # not ours (another session / manual trade)
        self._seen_trades.add(tid)
        yes_px = _f(b.get("yes_price_dollars", b.get("yes_price_fixed", 0)))
        if yes_px == 0 and b.get("yes_price") is not None:
            yes_px = _f(b["yes_price"]) / 100.0
        price = px(yes_px if o.outcome == UP else 1.0 - yes_px)
        n = _f(b.get("count_fp", b.get("count", 0)))
        fee = _f(b.get("fee_cost", 0))
        taker = bool(b.get("is_taker"))
        ts = int(b.get("ts_ms") or int(time.time() * 1000))
        o.filled += n
        if o.remaining <= 1e-9 and o.is_active:
            o.status = OrderStatus.FILLED
            self._final(o)
        self._fills.append(Fill(order_id=o.id, market=o.market, outcome=o.outcome, side=o.side, price=price,
                                size=n, liquidity="taker" if taker else "maker", fee=fee, rebate=0.0,
                                ts_ms=ts, tag=o.tag))

    async def poll_fills_forever(self, stop: asyncio.Event, every_s: float = 5.0) -> None:
        """Backstop for missed websocket fill messages."""
        while not stop.is_set():
            await asyncio.sleep(every_s)
            try:
                since = self.last_fill_poll_s
                self.last_fill_poll_s = int(time.time()) - 10
                for f in await self.rest.get_fills(min_ts=since):
                    self._on_fill(f)
            except Exception as e:  # noqa: BLE001
                log.warning("fill poll failed: %s", e)

    async def reconcile_forever(self, stop: asyncio.Event, every_s: float = 30.0) -> None:
        strikes: Dict[str, int] = {}
        while not stop.is_set():
            await asyncio.sleep(every_s)
            try:
                pos = {p.get("ticker"): _f(p.get("position_fp", p.get("position", 0)))
                       for p in await self.rest.get_positions()}
            except Exception as e:  # noqa: BLE001
                log.warning("position reconcile failed: %s", e)
                continue
            for st in self.core.active_markets():
                if st.info.slug in self.core.blocked_markets:
                    continue
                ours = st.inv.up - st.inv.down                 # Kalshi nets YES/NO: + = YES
                theirs = pos.get(st.info.slug, 0.0) - self.baseline.get(st.info.slug, 0.0)
                if abs(ours - theirs) > 0.5:
                    strikes[st.info.slug] = strikes.get(st.info.slug, 0) + 1
                    log.warning("position mismatch %s: ours %.2f kalshi %.2f", st.info.slug, ours, theirs)
                    if strikes[st.info.slug] >= 2:
                        self.core.kill(f"position_mismatch {st.info.slug}: ours {ours:.2f} vs kalshi {theirs:.2f}")
                else:
                    strikes.pop(st.info.slug, None)


KalshiDemoExchange = KalshiExchange     # backwards-compatible name


async def smoke_test(rest: KalshiRest, ticker: str, tick: float = 0.01) -> str:
    """Place 1 post-only contract at the lowest price (buy YES @ 1 tick) and cancel it.
    Proves signing, order entry and cancel work before any real trading. Worst case if it
    somehow fills: 1 contract at 1 cent."""
    price = max(tick, 0.01)
    r = await rest.create_order(ticker=ticker, outcome_yes=True, buy=True, price=price, count=1.0,
                                post_only=True, client_order_id=f"{COID_PREFIX}smoke-{uuid.uuid4().hex[:8]}")
    oid = str(r.get("order_id", ""))
    if not oid:
        raise RuntimeError(f"test order returned no order_id: {r}")
    await asyncio.sleep(0.5)
    await rest.cancel_order(oid, ticker=ticker)
    await asyncio.sleep(0.5)
    still = [od for od in await rest.get_open_orders() if str(od.get("order_id")) == oid]
    if still:
        raise RuntimeError(f"test order {oid} still resting after cancel - cancel it on kalshi.com")
    return f"test order placed and cancelled OK ({ticker}, 1 contract @ ${price:.2f})"
