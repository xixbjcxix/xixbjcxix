"""Shared-engine tests: generic book, queue-aware fill model, inventory, risk."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kbot.book import BookStore  # noqa: E402
from kbot.config import BotConfig, load_config, with_overrides  # noqa: E402
from kbot.core import TradingCore  # noqa: E402
from kbot.fees import fee_model_for  # noqa: E402
from kbot.fillmodel import FillModelConfig, SimExchange  # noqa: E402
from kbot.inventory import MarketInventory  # noqa: E402
from kbot.models import BUY, DOWN, SELL, UP, Fill, MarketInfo, Order, OrderStatus  # noqa: E402
from kbot.risk import RiskConfig, RiskManager  # noqa: E402
from kbot.signal import SignalReading  # noqa: E402
from kbot.store import NullStore  # noqa: E402
from kbot.strategy import Strategy, StrategyConfig  # noqa: E402


def mk_market(start_ms=0, horizon=300):
    return MarketInfo(slug="btc-updown-5m-0", asset="btc", horizon_s=horizon, start_ms=start_ms,
                      end_ms=start_ms + horizon * 1000, token_up="U", token_down="D", condition_id="C",
                      tick_size=0.01, min_order_size=5, fee_rate=0.07, fee_exponent=1, rebate_rate=0.2)


def book_frame(tok, bids, asks):
    return {"event_type": "book", "asset_id": tok,
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def change_frame(changes):
    return {"event_type": "price_change", "price_changes": [
        {"asset_id": t, "price": str(p), "size": str(s), "side": side} for t, side, p, s in changes]}


def trade_frame(tok, price, size, side, tx="0x1"):
    return {"event_type": "last_trade_price", "asset_id": tok, "price": str(price), "size": str(size),
            "side": side, "transaction_hash": tx}


class BookTests(unittest.TestCase):
    def test_snapshot_change_trade(self):
        bs = BookStore()
        evs = bs.handle([book_frame("U", [(0.48, 100), (0.47, 200)], [(0.50, 50)])], 1000)
        self.assertEqual(len(evs), 1)
        b = bs.book("U")
        self.assertEqual(b.best_bid, 0.48)
        self.assertEqual(b.best_ask, 0.50)
        bs.handle(change_frame([("U", "BUY", 0.48, 0)]), 1100)
        self.assertEqual(b.best_bid, 0.47)
        evs = bs.handle(trade_frame("U", 0.47, 10, "SELL"), 1200)
        self.assertEqual(evs[0].side, SELL)
        self.assertEqual(evs[0].price, 0.47)


class FillModelTests(unittest.TestCase):
    def setUp(self):
        self.m = mk_market()
        self.bs = BookStore()
        fm = fee_model_for(self.m)
        self.ex = SimExchange(self.bs, FillModelConfig(order_latency_ms=100, cancel_latency_ms=100, taker_delay_ms=150),
                              lambda s: fm, lambda t: self.m)
        self.bs.handle(book_frame("U", [(0.48, 100), (0.47, 200)], [(0.50, 50), (0.51, 80)]), 0)

    def place(self, price, size=10, side=BUY, post_only=True, t=0):
        o = Order(market=self.m.slug, outcome=UP, side=side, price=price, size=size, post_only=post_only)
        self.ex.submit(o, self.m, t)
        return o

    def feed(self, frame, t):
        fills = self.ex.advance(t)
        for ev in self.bs.handle(frame, t):
            if hasattr(ev, "changes"):
                fills += self.ex.on_book_event(ev)
            elif hasattr(ev, "tx"):
                fills += self.ex.on_trade(ev)
        return fills

    def test_joins_back_of_queue(self):
        o = self.place(0.47)
        self.ex.advance(200)
        self.assertEqual(o.status, OrderStatus.OPEN)
        self.assertEqual(o.queue_ahead, 200)
        # trade at our price smaller than queue: no fill
        self.assertEqual(self.feed(trade_frame("U", 0.47, 150, "SELL", "a"), 300), [])
        self.assertAlmostEqual(o.queue_ahead, 50)
        f = self.feed(trade_frame("U", 0.47, 55, "SELL", "b"), 400)
        self.assertEqual(len(f), 1)
        self.assertAlmostEqual(f[0].size, 5)
        self.assertEqual(f[0].liquidity, "maker")
        self.assertGreater(f[0].fee, 0.0)            # Kalshi charges maker fees (no rebates)

    def test_trade_through_fills_regardless_of_queue(self):
        o = self.place(0.47)
        self.ex.advance(200)
        f = self.feed(trade_frame("U", 0.46, 30, "SELL", "c"), 300)
        self.assertAlmostEqual(sum(x.size for x in f), 10)
        self.assertEqual(o.status, OrderStatus.FILLED)
        self.assertEqual(f[0].price, 0.47)

    def test_buy_aggressor_does_not_fill_our_bid(self):
        self.place(0.47)
        self.ex.advance(200)
        self.assertEqual(self.feed(trade_frame("U", 0.46, 30, "BUY", "d"), 300), [])

    def test_post_only_reject(self):
        o = self.place(0.50)
        self.ex.advance(200)
        self.assertEqual(o.status, OrderStatus.REJECTED)
        self.assertIn("post_only", o.reject_reason)

    def test_cancel_latency_still_fills(self):
        o = self.place(0.47)
        self.ex.advance(200)
        self.ex.cancel(o.id, 250)
        f = self.feed(trade_frame("U", 0.45, 30, "SELL", "e"), 300)   # before cancel is effective (350)
        self.assertAlmostEqual(sum(x.size for x in f), 10)

    def test_cancel_effective(self):
        o = self.place(0.47)
        self.ex.advance(200)
        self.ex.cancel(o.id, 250)
        self.ex.advance(400)
        self.assertEqual(o.status, OrderStatus.CANCELLED)
        self.assertEqual(self.feed(trade_frame("U", 0.45, 30, "SELL", "f"), 500), [])

    def test_pessimistic_cancels_behind_us(self):
        o = self.place(0.47)
        self.ex.advance(200)
        self.feed(change_frame([("U", "BUY", 0.47, 150)]), 300)   # 50 cancelled, assumed behind us
        self.assertAlmostEqual(o.queue_ahead, 150)
        self.feed(change_frame([("U", "BUY", 0.47, 20)]), 400)    # level smaller than our queue
        self.assertAlmostEqual(o.queue_ahead, 20)

    def test_fill_on_cross(self):
        o = self.place(0.47)
        self.ex.advance(200)
        f = self.feed(change_frame([("U", "SELL", 0.47, 40)]), 300)
        self.assertAlmostEqual(sum(x.size for x in f), 10)

    def test_taker_walks_book_with_limit_and_fee(self):
        o = self.place(0.50, size=70, post_only=False)
        f = self.ex.advance(300)
        self.assertAlmostEqual(sum(x.size for x in f), 50)   # limit 0.50: only the 0.50 level
        self.assertTrue(all(x.liquidity == "taker" for x in f))
        self.assertAlmostEqual(f[0].fee, 0.88)      # Kalshi: 0.07*50*0.25 = 0.875, rounded UP to the cent
        self.assertEqual(o.status, OrderStatus.CANCELLED)       # FAK remainder


class InventoryTests(unittest.TestCase):
    def fill(self, outcome, side, price, size, ts=0, liq="maker", fee=0.0, rebate=0.0):
        return Fill(order_id="x", market="m", outcome=outcome, side=side, price=price, size=size,
                    liquidity=liq, fee=fee, rebate=rebate, ts_ms=ts)

    def test_pair_locks_edge_either_way(self):
        for winner in (UP, DOWN):
            inv = MarketInventory(market="m")
            inv.apply_fill(self.fill(UP, BUY, 0.45, 10, 0))
            inv.apply_fill(self.fill(DOWN, BUY, 0.50, 10, 1000))
            self.assertAlmostEqual(inv.paired, 10)
            self.assertAlmostEqual(inv.pair_cost, 0.95)
            b = inv.settle(winner, 2000)
            self.assertAlmostEqual(b["total_pnl"], 0.5)
            self.assertEqual(inv.episodes[0].closed_by, "passive")

    def test_residual_and_cut(self):
        inv = MarketInventory(market="m")
        inv.apply_fill(self.fill(UP, BUY, 0.45, 10, 0))
        inv.apply_fill(self.fill(DOWN, BUY, 0.50, 6, 100))
        self.assertAlmostEqual(inv.residual, 4)
        inv.apply_fill(self.fill(UP, SELL, 0.30, 4, 200, liq="taker", fee=0.01))
        self.assertAlmostEqual(inv.residual, 0)
        b = inv.settle(DOWN, 300)
        self.assertAlmostEqual(b["paired_pnl"], 6 * 0.05)
        self.assertAlmostEqual(b["cut_pnl"], 4 * (0.30 - 0.45))
        self.assertAlmostEqual(b["total_pnl"], 0.3 - 0.6 - 0.01)
        self.assertEqual(inv.episodes[0].closed_by, "cut")


class RiskTests(unittest.TestCase):
    def test_limits(self):
        r = RiskManager(RiskConfig(max_order_usd=5, max_residual_shares=15, max_residual_usd=100, kill_file=""))
        inv = MarketInventory(market="m")
        big = Order(market="m", outcome=UP, side=BUY, price=0.5, size=20)
        self.assertFalse(r.check_order(big, inv, [], [], 0, 0).ok)
        ok = Order(market="m", outcome=UP, side=BUY, price=0.4, size=10)
        self.assertTrue(r.check_order(ok, inv, [], [], 0, 0).ok)
        ok.status = OrderStatus.OPEN
        more = Order(market="m", outcome=UP, side=BUY, price=0.4, size=10)
        d = r.check_order(more, inv, [ok], [ok], 0, 0)
        self.assertFalse(d.ok)
        self.assertIn("residual_shares", d.reason)
        r.kill("test", 0)
        self.assertFalse(r.check_order(ok, inv, [], [], 0, 0).ok)

    def test_completion_allowed_when_residual_full(self):
        r = RiskManager(RiskConfig(max_residual_shares=10, kill_file=""))
        inv = MarketInventory(market="m")
        inv.apply_fill(Fill("x", "m", UP, BUY, 0.4, 10, "maker", 0, 0, 0))
        comp = Order(market="m", outcome=DOWN, side=BUY, price=0.5, size=10)
        self.assertTrue(r.check_order(comp, inv, [], [], 4.0, 0).ok)

    def test_daily_loss_halts_until_next_day(self):
        r = RiskManager(RiskConfig(max_daily_loss_usd=10, kill_file=""))
        r.check_daily_loss(-11, 1000)
        self.assertTrue(r.blocked(2000))
        self.assertFalse(r.killed)
        self.assertIsNone(r.blocked(86400000 + 1))


if __name__ == "__main__":
    unittest.main()
