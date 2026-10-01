"""L2 order books.

OrderBook/BookStore are the generic per-outcome books used by the engine and fill model.
KalshiBookStore (below) turns Kalshi's YES-bid/NO-bid websocket messages into two
per-outcome books (YES and NO) plus trade prints, so backtest, paper and demo trading all see
the same data through the same parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .models import BUY, SELL, Trade, px


@dataclass
class OrderBook:
    token: str
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    last_update_ms: int = 0
    has_snapshot: bool = False
    tick_size: float = 0.01

    def apply_snapshot(self, bids: Iterable, asks: Iterable, ts_ms: int) -> None:
        self.bids = {px(float(l["price"])): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {px(float(l["price"])): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.last_update_ms = ts_ms
        self.has_snapshot = True

    def apply_level(self, side: str, price: float, size: float, ts_ms: int) -> float:
        """Set an absolute level size. Returns the previous size at that level."""
        book = self.bids if side == BUY else self.asks
        p = px(price)
        prev = book.get(p, 0.0)
        if size <= 0:
            book.pop(p, None)
        else:
            book[p] = size
        self.last_update_ms = ts_ms
        return prev

    def invalidate(self) -> None:
        self.has_snapshot = False

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None and a is None:
            return None
        if b is None:
            return a
        if a is None:
            return b
        return (a + b) / 2.0

    def size_at(self, side: str, price: float) -> float:
        book = self.bids if side == BUY else self.asks
        return book.get(px(price), 0.0)

    def asks_ascending(self) -> List[Tuple[float, float]]:
        return sorted(self.asks.items())

    def bids_descending(self) -> List[Tuple[float, float]]:
        return sorted(self.bids.items(), reverse=True)

    def depth_to(self, side: str, limit: float) -> float:
        """Shares available to a taker up to a limit price (side = taker side)."""
        if side == BUY:
            return sum(s for p, s in self.asks.items() if p <= limit + 1e-9)
        return sum(s for p, s in self.bids.items() if p >= limit - 1e-9)


# ---- normalized events emitted by the parser -------------------------------------------

@dataclass
class BookEvent:
    token: str
    ts_ms: int
    kind: str                                  # "snapshot" | "change"
    changes: List[Tuple[str, float, float, float]] = field(default_factory=list)
    # each change: (side, price, new_size, prev_size)


@dataclass
class ResolvedEvent:
    condition_id: str
    winning_token: str
    winning_outcome: str
    ts_ms: int


class BookStore:
    """Holds books for every subscribed token and turns raw frames into events."""

    def __init__(self) -> None:
        self.books: Dict[str, OrderBook] = {}
        self.seen_tx: Dict[str, int] = {}

    def book(self, token: str) -> OrderBook:
        b = self.books.get(token)
        if b is None:
            b = self.books[token] = OrderBook(token=token)
        return b

    def invalidate_all(self) -> None:
        for b in self.books.values():
            b.invalidate()

    def handle(self, msg, recv_ms: int) -> List[object]:
        """Parse one raw frame (dict or list). Returns BookEvent/Trade/ResolvedEvent items.

        recv_ms (local receive time) is used as the event clock: that's what a live bot
        can act on, and it keeps replay causally honest.
        """
        if isinstance(msg, list):
            out: List[object] = []
            for m in msg:
                out.extend(self.handle(m, recv_ms))
            return out
        if not isinstance(msg, dict):
            return []
        et = msg.get("event_type") or msg.get("type")
        if et == "book":
            tok = str(msg["asset_id"])
            self.book(tok).apply_snapshot(msg.get("bids", []), msg.get("asks", []), recv_ms)
            return [BookEvent(token=tok, ts_ms=recv_ms, kind="snapshot")]
        if et == "price_change":
            by_tok: Dict[str, BookEvent] = {}
            for ch in msg.get("price_changes", []):
                tok = str(ch["asset_id"])
                side = BUY if str(ch.get("side", "")).upper() == "BUY" else SELL
                price, size = float(ch["price"]), float(ch["size"])
                b = self.book(tok)
                prev = b.apply_level(side, price, size, recv_ms)
                ev = by_tok.get(tok)
                if ev is None:
                    ev = by_tok[tok] = BookEvent(token=tok, ts_ms=recv_ms, kind="change")
                ev.changes.append((side, px(price), size, prev))
            return list(by_tok.values())
        if et == "last_trade_price":
            tok = str(msg["asset_id"])
            side = BUY if str(msg.get("side", "")).upper() == "BUY" else SELL
            return [Trade(token=tok, price=px(float(msg["price"])), size=float(msg.get("size") or 0.0),
                          side=side, ts_ms=recv_ms, tx=str(msg.get("transaction_hash") or ""))]
        if et == "tick_size_change":
            tok = str(msg["asset_id"])
            self.book(tok).tick_size = float(msg["new_tick_size"])
            return []
        if et == "market_resolved":
            win = str(msg.get("winning_asset_id") or "")
            return [ResolvedEvent(condition_id=str(msg.get("market", "")), winning_token=win,
                                  winning_outcome=str(msg.get("winning_outcome") or ""), ts_ms=recv_ms)]
        return []


# ======================================================================================
# Kalshi: one book of YES bids and NO bids per market. We derive two per-outcome books so
# the shared engine (fill model, strategy) can treat YES and NO like two tokens:
#   YES book: bids = YES bids,  asks = 1 - NO bids
#   NO  book: bids = NO bids,   asks = 1 - YES bids
# Formats per docs.kalshi.com/websockets (orderbook_snapshot / orderbook_delta / trade).
# ======================================================================================

def _f(x) -> float:
    return float(x) if x is not None and x != "" else 0.0


def yes_token(ticker: str) -> str:
    return f"{ticker}:yes"


def no_token(ticker: str) -> str:
    return f"{ticker}:no"


def _levels(msg: dict, side: str):
    """Snapshot ladders: prefer *_dollars_fp ([["0.4200","13.00"],..]); fall back to legacy cents."""
    for key in (f"{side}_dollars_fp", f"{side}_dollars"):
        if key in msg and msg[key] is not None:
            return [(round(_f(p), 4), _f(c)) for p, c in msg[key]]
    if side in msg and msg[side] is not None:          # legacy: [[cents, count], ...]
        return [(round(_f(p) / 100.0, 4), _f(c)) for p, c in msg[side]]
    return []


class KalshiBookStore(BookStore):
    def __init__(self) -> None:
        super().__init__()
        self.ladders: Dict[str, Dict[str, Dict[float, float]]] = {}   # ticker -> {"yes":{p:c}, "no":{p:c}}

    def _lad(self, ticker: str) -> Dict[str, Dict[float, float]]:
        l = self.ladders.get(ticker)
        if l is None:
            l = self.ladders[ticker] = {"yes": {}, "no": {}}
        return l

    def invalidate_all(self) -> None:
        super().invalidate_all()

    def handle(self, msg, recv_ms: int) -> List[object]:
        if isinstance(msg, list):
            out: List[object] = []
            for m in msg:
                out.extend(self.handle(m, recv_ms))
            return out
        if not isinstance(msg, dict):
            return []
        t = msg.get("type")
        body = msg.get("msg") or {}
        if t == "orderbook_snapshot":
            tk = body["market_ticker"]
            lad = self._lad(tk)
            lad["yes"] = {p: c for p, c in _levels(body, "yes") if c > 0}
            lad["no"] = {p: c for p, c in _levels(body, "no") if c > 0}
            yb, nb = self.book(yes_token(tk)), self.book(no_token(tk))
            yb.bids = dict(lad["yes"])
            yb.asks = {px(1.0 - p): c for p, c in lad["no"].items()}
            nb.bids = dict(lad["no"])
            nb.asks = {px(1.0 - p): c for p, c in lad["yes"].items()}
            for b in (yb, nb):
                b.last_update_ms = recv_ms
                b.has_snapshot = True
            return [BookEvent(token=yes_token(tk), ts_ms=recv_ms, kind="snapshot"),
                    BookEvent(token=no_token(tk), ts_ms=recv_ms, kind="snapshot")]
        if t == "orderbook_delta":
            tk = body["market_ticker"]
            side = str(body.get("side", "")).lower()
            if side not in ("yes", "no"):
                return []
            p = round(_f(body.get("price_dollars", body.get("price", 0))), 4)
            if "price_dollars" not in body and "price" in body:
                p = round(_f(body["price"]) / 100.0, 4)
            d = _f(body.get("delta_fp", body.get("delta", 0)))
            lad = self._lad(tk)[side]
            prev = lad.get(p, 0.0)
            new = max(0.0, round(prev + d, 2))
            if new <= 0:
                lad.pop(p, None)
            else:
                lad[p] = new
            own, oth = (yes_token(tk), no_token(tk)) if side == "yes" else (no_token(tk), yes_token(tk))
            bo, bx = self.book(own), self.book(oth)
            bo.apply_level(BUY, p, new, recv_ms)
            bx.apply_level(SELL, px(1.0 - p), new, recv_ms)
            return [BookEvent(token=own, ts_ms=recv_ms, kind="change", changes=[(BUY, p, new, prev)]),
                    BookEvent(token=oth, ts_ms=recv_ms, kind="change", changes=[(SELL, px(1.0 - p), new, prev)])]
        if t == "trade":
            tk = body["market_ticker"]
            yp = round(_f(body.get("yes_price_dollars", 0)), 4)
            np_ = round(_f(body.get("no_price_dollars", 0)), 4) or px(1.0 - yp)
            n = _f(body.get("count_fp", body.get("count", 0)))
            taker = str(body.get("taker_side", "")).lower()
            tid = str(body.get("trade_id", ""))
            if taker == "yes":   # taker bought YES; resting NO bids were hit
                return [Trade(token=no_token(tk), price=np_, size=n, side=SELL, ts_ms=recv_ms, tx=tid),
                        Trade(token=yes_token(tk), price=yp, size=n, side=BUY, ts_ms=recv_ms, tx=tid)]
            if taker == "no":    # taker bought NO; resting YES bids were hit
                return [Trade(token=yes_token(tk), price=yp, size=n, side=SELL, ts_ms=recv_ms, tx=tid),
                        Trade(token=no_token(tk), price=np_, size=n, side=BUY, ts_ms=recv_ms, tx=tid)]
            return []
        if t in ("market_lifecycle_v2", "market_lifecycle", "market_result"):
            tk = body.get("market_ticker", "")
            res = str(body.get("result", "") or "").lower()
            if tk and res in ("yes", "no"):
                win = yes_token(tk) if res == "yes" else no_token(tk)
                return [ResolvedEvent(condition_id="", winning_token=win, winning_outcome=res, ts_ms=recv_ms)]
        return []
