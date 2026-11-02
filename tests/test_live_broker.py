import asyncio

import pytest

from adaptbot.broker import LiveBroker
from adaptbot.kalshi import KalshiClient, KalshiError, fill_price, v2_side_price
from adaptbot.market import Book
from adaptbot.strategy import Intent


def test_v2_mapping_matches_yes_book():
    assert v2_side_price("yes", "buy", 0.45) == ("bid", 0.45)
    assert v2_side_price("yes", "sell", 0.45) == ("ask", 0.45)
    assert v2_side_price("no", "buy", 0.30) == ("ask", 0.70)
    assert v2_side_price("no", "sell", 0.30) == ("bid", 0.70)


def test_fill_price_converts_to_side():
    assert fill_price({"yes_price_dollars": "0.6200"}, "yes") == pytest.approx(0.62)
    assert fill_price({"yes_price_dollars": "0.6200"}, "no") == pytest.approx(0.38)
    assert fill_price({"yes_price": 62}, "no") == pytest.approx(0.38)


class FakeClient:
    def __init__(self, order, fills, error=None):
        self.order, self._fills, self.error = order, fills, error
        self.placed, self.cancelled = [], []

    async def place(self, *a, **k):
        if self.error:
            raise self.error
        self.placed.append((a, k))
        return self.order

    async def fills(self, order_id=None, min_ts=None):
        return self._fills

    async def cancel(self, oid, ticker=None):
        self.cancelled.append(oid)


BOOK = Book([(0.40, 10)], [(0.55, 10)])


def run(b, it):
    return asyncio.run(b.execute("KXBTC15M-T", it, BOOK))


def test_buy_no_books_fill_prices_and_fees():
    c = FakeClient({"id": "o1", "status": "executed", "filled": 5.0, "remaining": 0.0},
                   [{"order_id": "o1", "count_fp": "3.00", "yes_price_dollars": "0.5500", "fee_cost": "0.06"},
                    {"order_id": "o1", "count_fp": "2.00", "yes_price_dollars": "0.5400", "fee_cost": "0.04"}])
    q, avg, fee, unc = run(LiveBroker(c, 0.07), Intent("buy", "no", 5, 0.47, "entry"))
    assert q == 5 and not unc
    assert avg == pytest.approx((3 * 0.45 + 2 * 0.46) / 5)
    assert fee == pytest.approx(0.10)
    assert c.placed[0][1]["reduce_only"] is False


def test_unfilled_ioc_and_rejection():
    c = FakeClient({"id": "o2", "status": "canceled", "filled": 0.0, "remaining": 5.0}, [])
    assert run(LiveBroker(c, 0.07), Intent("buy", "yes", 5, 0.45, "")) == (0.0, 0.0, 0.0, False)
    b = LiveBroker(FakeClient(None, [], KalshiError(403, "forbidden", "/x")), 0.07)
    assert run(b, Intent("buy", "yes", 5, 0.45, ""))[3] is False
    assert b.fatal


def test_network_error_is_uncertain():
    b = LiveBroker(FakeClient(None, [], OSError("reset")), 0.07)
    assert run(b, Intent("sell", "yes", 5, 0.45, ""))[3] is True


def test_missing_fills_book_worst_case():
    c = FakeClient({"id": "o3", "status": "executed", "filled": 2.0, "remaining": 0.0}, [])
    import adaptbot.broker as br
    orig = br.asyncio.sleep

    async def fast(_):
        return None
    br.asyncio.sleep = fast
    try:
        q, avg, fee, _ = run(LiveBroker(c, 0.07), Intent("buy", "yes", 2, 0.46, ""))
    finally:
        br.asyncio.sleep = orig
    assert q == 2 and avg == 0.46 and fee > 0


def test_shard_balances_parse():
    c = KalshiClient("https://x/trade-api/v2")

    async def req(*a, **k):
        return {"balance": 7295, "balance_breakdown": [{"exchange_index": 0, "balance": 39.27},
                                                       {"exchange_index": 2, "balance": 33.68}]}
    c._req = req
    assert asyncio.run(c.balances()) == {0: 39.27, 2: 33.68}
    assert asyncio.run(c.balance(2)) == 33.68


def test_retries_after_429(monkeypatch):
    import adaptbot.kalshi as k
    calls = []

    class Resp:
        def __init__(self, status, text):
            self.status, self._t = status, text

        async def text(self):
            return self._t

        async def json(self, content_type=None):
            import json
            return json.loads(self._t)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def request(self, method, url, **kw):
            calls.append(method)
            return Resp(429, "{}") if len(calls) < 3 else Resp(200, '{"balance": 100}')

    async def nosleep(_):
        return None
    monkeypatch.setattr(k.asyncio, "sleep", nosleep)
    c = KalshiClient("https://x/trade-api/v2")
    c.session = Sess()
    assert asyncio.run(c.balances()) == {0: 1.0}
    assert len(calls) == 3
    calls.clear()
    with pytest.raises(KalshiError):
        asyncio.run(c._req("POST", "/portfolio/events/orders", body={}))   # orders never retried
    assert len(calls) == 1
