"""Simulated exchange with a queue-aware, trade-through fill model.

Used by BOTH the backtest (driven by recorded frames) and paper trading (driven by
live frames), so the two see identical execution logic.

Resting (maker) BUY at price p on a token fills only when:
  1. a taker SELL prints BELOW p  (price traded through us -> we would have been hit first), or
  2. a taker SELL prints AT p and the shares queued ahead of us are exhausted, or
  3. (optional, fill_on_cross) the public ask drops to <= p (someone offered through us).
When we join a level, everyone already resting there is ahead of us. Level-size drops
that are not explained by trades are cancellations; `queue_mode` decides whose:
  pessimistic  - cancels come from behind us (queue_ahead only shrinks to the level size)
  proportional - cancels are spread evenly through the queue
Symmetric rules apply to resting SELLs.

Latency: orders go live `order_latency_ms` after submission; cancels take effect
`cancel_latency_ms` after the request, and the order can still fill in between.
Marketable (taker) orders execute against the book `taker_delay_ms` later (network/matching
latency), and never sweep past their limit price. Unfilled taker remainder is cancelled (FAK).

Post-only orders that would cross on arrival are rejected, as the venue would.
On Kalshi every trade prints on both outcome books (a YES taker consumes resting NO bids), and
the adapter emits both sides, so complement_trades should stay False.
Our own orders are NOT inserted into the replayed book, so the model cannot capture
our impact on other participants; keep clip sizes small relative to displayed depth.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .book import BookEvent, BookStore
from .fees import FeeModel
from .models import BUY, SELL, Fill, MarketInfo, Order, OrderStatus, Trade, other, px


@dataclass
class FillModelConfig:
    order_latency_ms: int = 150
    cancel_latency_ms: int = 150
    taker_delay_ms: int = 50
    queue_mode: str = "pessimistic"      # pessimistic | proportional
    fill_on_cross: bool = True
    complement_trades: bool = False      # count taker BUYs of the other outcome as SELLs through our bid (mint matching)


class SimExchange:
    def __init__(self, books: BookStore, cfg: FillModelConfig,
                 fee_for: Callable[[str], FeeModel],
                 market_for_token: Callable[[str], Optional[MarketInfo]]) -> None:
        self.books = books
        self.cfg = cfg
        self.fee_for = fee_for                    # market slug -> FeeModel
        self.market_for_token = market_for_token
        self.orders: Dict[str, Order] = {}
        self.by_token: Dict[str, List[Order]] = {}
        self._pending: List[Tuple[int, int, str, str]] = []  # (due_ms, seq, kind, order_id)
        self._seq = 0
        self.now_ms = 0
        self._traded_at: Dict[Tuple[str, str, float], float] = {}  # (token, side, price) -> recent traded size
        self._seen_tx: Dict[Tuple[str, str], bool] = {}
        self.rejections = 0
        self.on_final: Optional[Callable[[Order], None]] = None

    # ---- order entry -------------------------------------------------------------
    def _push(self, due: int, kind: str, oid: str) -> None:
        self._seq += 1
        heapq.heappush(self._pending, (due, self._seq, kind, oid))

    def submit(self, o: Order, market: MarketInfo, now_ms: int) -> Order:
        o.created_ms = now_ms
        o.status = OrderStatus.PENDING
        self.orders[o.id] = o
        tok = market.token(o.outcome)
        o.token = tok
        self.by_token.setdefault(tok, []).append(o)
        if o.post_only:
            self._push(now_ms + self.cfg.order_latency_ms, "activate", o.id)
        else:
            self._push(now_ms + self.cfg.order_latency_ms + self.cfg.taker_delay_ms, "take", o.id)
        return o

    def cancel(self, oid: str, now_ms: int) -> None:
        o = self.orders.get(oid)
        if o is None or not o.is_active or o.cancel_req_ms is not None:
            return
        o.cancel_req_ms = now_ms
        self._push(now_ms + self.cfg.cancel_latency_ms, "cancel", oid)

    def active_orders(self, market: Optional[str] = None) -> List[Order]:
        return [o for o in self.orders.values() if o.is_active and (market is None or o.market == market)]

    # ---- clock ---------------------------------------------------------------------
    def advance(self, now_ms: int) -> List[Fill]:
        """Process latency-delayed actions due at or before now_ms."""
        fills: List[Fill] = []
        while self._pending and self._pending[0][0] <= now_ms:
            due, _, kind, oid = heapq.heappop(self._pending)
            self.now_ms = due
            o = self.orders.get(oid)
            if o is None:
                continue
            if kind == "activate":
                self._activate(o, due)
            elif kind == "take":
                fills.extend(self._take(o, due))
            elif kind == "cancel":
                if o.is_active:
                    o.status = OrderStatus.CANCELLED
                    self._drop(o)
        self.now_ms = max(self.now_ms, now_ms)
        return fills

    def _token_of(self, o: Order) -> str:
        return o.token

    def _drop(self, o: Order) -> None:
        if self.on_final is not None and o.id in self.orders:
            self.on_final(o)
        tok = self._token_of(o)
        if tok:
            try:
                self.by_token[tok].remove(o)
            except ValueError:
                pass
        self.orders.pop(o.id, None)

    def _activate(self, o: Order, now: int) -> None:
        if not o.is_active:
            return
        tok = self._token_of(o)
        book = self.books.book(tok)
        if not book.has_snapshot:
            o.status, o.reject_reason = OrderStatus.REJECTED, "no_book"
            self.rejections += 1
            self._drop(o)
            return
        if o.side == BUY:
            ba = book.best_ask
            if ba is not None and o.price >= ba - 1e-9:
                o.status, o.reject_reason = OrderStatus.REJECTED, "post_only_would_cross"
                self.rejections += 1
                self._drop(o)
                return
            o.queue_ahead = book.size_at(BUY, o.price)
        else:
            bb = book.best_bid
            if bb is not None and o.price <= bb + 1e-9:
                o.status, o.reject_reason = OrderStatus.REJECTED, "post_only_would_cross"
                self.rejections += 1
                self._drop(o)
                return
            o.queue_ahead = book.size_at(SELL, o.price)
        o.status = OrderStatus.OPEN
        o.live_ms = now

    def _take(self, o: Order, now: int) -> List[Fill]:
        """Execute a marketable order against the current book, up to its limit (FAK)."""
        if not o.is_active:
            return []
        tok = self._token_of(o)
        book = self.books.book(tok)
        fills: List[Fill] = []
        if not book.has_snapshot:
            o.status, o.reject_reason = OrderStatus.REJECTED, "no_book"
            self.rejections += 1
            self._drop(o)
            return fills
        levels = book.asks_ascending() if o.side == BUY else book.bids_descending()
        for price, size in levels:
            if o.remaining <= 1e-9:
                break
            if (o.side == BUY and price > o.price + 1e-9) or (o.side == SELL and price < o.price - 1e-9):
                break
            q = min(o.remaining, size)
            fills.append(self._fill(o, price, q, "taker", now))
        if o.remaining > 1e-9:
            o.status = OrderStatus.CANCELLED  # FAK remainder
            if not fills:
                o.reject_reason = "no_liquidity_at_limit"
        else:
            o.status = OrderStatus.FILLED
        self._drop(o)
        return fills

    def _fill(self, o: Order, price: float, size: float, liquidity: str, now: int) -> Fill:
        fm = self.fee_for(o.market)
        fee = fm.taker_fee(size, price) if liquidity == "taker" else fm.maker_fee(size, price)
        rebate = 0.0
        o.filled += size
        if o.remaining <= 1e-9:
            o.status = OrderStatus.FILLED
        return Fill(order_id=o.id, market=o.market, outcome=o.outcome, side=o.side,
                    price=px(price), size=size, liquidity=liquidity, fee=fee, rebate=rebate,
                    ts_ms=now, tag=o.tag)

    # ---- market data -----------------------------------------------------------------
    def on_book_event(self, ev: BookEvent) -> List[Fill]:
        fills: List[Fill] = []
        resting = [o for o in self.by_token.get(ev.token, []) if o.status == OrderStatus.OPEN]
        if not resting:
            return fills
        book = self.books.book(ev.token)
        for o in resting:
            lvl_side = o.side  # our BUY rests among bids; SELL among asks
            if ev.kind == "snapshot":
                o.queue_ahead = min(o.queue_ahead, book.size_at(lvl_side, o.price))
            else:
                for side, price, new, prev in ev.changes:
                    if side != lvl_side or abs(price - o.price) > 1e-9:
                        continue
                    drop = prev - new
                    if drop <= 0:
                        continue  # growth joins behind us
                    key = (ev.token, side, o.price)
                    explained = min(drop, self._traded_at.pop(key, 0.0))
                    unexplained = drop - explained
                    if self.cfg.queue_mode == "proportional" and unexplained > 0 and prev > 0:
                        o.queue_ahead = max(0.0, o.queue_ahead - unexplained * (o.queue_ahead / prev))
                    o.queue_ahead = min(o.queue_ahead, new)
            if self.cfg.fill_on_cross:
                if o.side == BUY:
                    crossing = [(p, s) for p, s in book.asks.items() if p <= o.price + 1e-9]
                else:
                    crossing = [(p, s) for p, s in book.bids.items() if p >= o.price - 1e-9]
                avail = sum(s for _, s in crossing)
                if avail > 0 and o.remaining > 1e-9:
                    q = min(o.remaining, avail)
                    fills.append(self._fill(o, o.price, q, "maker", ev.ts_ms))
        for o in [o for o in resting if o.status == OrderStatus.FILLED]:
            self._drop(o)
        return fills

    def on_trade(self, t: Trade) -> List[Fill]:
        fills: List[Fill] = []
        fills.extend(self._trade_hits(t.token, t.side, t.price, t.size, t.ts_ms, t.tx))
        if self.cfg.complement_trades:
            m = self.market_for_token(t.token)
            if m is not None:
                oc = m.outcome_of(t.token)
                comp_tok = m.token(other(oc))
                # taker BUY of the complement at q can match our BUY at >= 1-q (mint)
                if t.side == BUY:
                    fills.extend(self._trade_hits(comp_tok, SELL, px(1.0 - t.price), t.size, t.ts_ms, t.tx))
        return fills

    def _trade_hits(self, token: str, aggressor: str, price: float, size: float,
                    ts: int, tx: str) -> List[Fill]:
        fills: List[Fill] = []
        if tx:
            k = (token, tx + aggressor + str(price))
            if k in self._seen_tx:
                return fills
            self._seen_tx[k] = True
            if len(self._seen_tx) > 200000:
                self._seen_tx.clear()
        # which of our resting orders does a taker of `aggressor` side hit?
        if aggressor == SELL:
            cands = [o for o in self.by_token.get(token, []) if o.status == OrderStatus.OPEN and o.side == BUY
                     and o.price >= price - 1e-9]
            cands.sort(key=lambda o: (-o.price, o.live_ms))
        else:
            cands = [o for o in self.by_token.get(token, []) if o.status == OrderStatus.OPEN and o.side == SELL
                     and o.price <= price + 1e-9]
            cands.sort(key=lambda o: (o.price, o.live_ms))
        if any(abs(o.price - price) < 1e-9 for o in cands):
            # remember traded size at our level so the next price_change isn't read as cancels
            rest_side = BUY if aggressor == SELL else SELL
            key = (token, rest_side, price)
            self._traded_at[key] = self._traded_at.get(key, 0.0) + size
        budget = size
        for o in cands:
            if budget <= 1e-9:
                break
            through = (o.price > price + 1e-9) if o.side == BUY else (o.price < price - 1e-9)
            if through:
                q = min(o.remaining, budget)
            else:
                if o.queue_ahead >= budget:
                    o.queue_ahead -= budget
                    budget = 0.0
                    break
                budget -= o.queue_ahead
                o.queue_ahead = 0.0
                q = min(o.remaining, budget)
            if q > 1e-9:
                fills.append(self._fill(o, o.price, q, "maker", ts))
                budget -= q
        for o in [o for o in cands if o.status == OrderStatus.FILLED]:
            self._drop(o)
        return fills
