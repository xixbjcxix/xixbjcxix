"""pbot tests: strategy, risk, calendar, Public API client (mocked HTTP), live broker, full agent day."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pbot.agent import Agent  # noqa: E402
from pbot.broker import LiveBroker, PaperBroker, make_sim  # noqa: E402
from pbot.clock import MarketCalendar, SimClock  # noqa: E402
from pbot.config import BotConfig, ExecutionConfig, RiskConfig, StrategyConfig, load_config, with_overrides  # noqa: E402
from pbot.journal import Journal  # noqa: E402
from pbot.ladder import Ladder  # noqa: E402
from pbot.public_api import Bar, OrderState, PublicClient, Quote  # noqa: E402
from pbot.risk import RiskManager  # noqa: E402
from pbot.strategy import Position, evaluate_long, manage, opening_range, screen_range, vwap  # noqa: E402

TODAY = date(2026, 10, 1)          # a Thursday


def bars_from(rows, vol=100000):
    return [Bar(str(i), o, h, l, c, vol) for i, (o, h, l, c) in enumerate(rows)]


def cfg_tmp():
    cfg = BotConfig()
    cfg.data_dir = tempfile.mkdtemp()
    return cfg


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.bars = bars_from([(10.0, 10.10, 9.95, 10.05), (10.05, 10.12, 10.0, 10.08),
                               (10.08, 10.10, 10.02, 10.04), (10.04, 10.09, 9.98, 10.0),
                               (10.0, 10.08, 9.97, 10.06), (10.06, 10.15, 10.05, 10.14)])

    def test_opening_range_and_vwap(self):
        orng = opening_range(self.bars, 5)
        self.assertEqual((orng.high, orng.low), (10.12, 9.95))
        self.assertIsNone(opening_range(self.bars[:3], 5))
        self.assertAlmostEqual(vwap(self.bars), 10.055, places=2)

    def test_screen(self):
        orng = opening_range(self.bars, 5)
        self.assertIsNone(screen_range(orng, self.cfg))
        tight = opening_range(bars_from([(10, 10.01, 10, 10)] * 5), 5)
        self.assertIn("too tight", screen_range(tight, self.cfg))
        thin = opening_range(bars_from([(10, 10.1, 9.9, 10)] * 5, vol=10), 5)
        self.assertIn("thin", screen_range(thin, self.cfg))

    def test_breakout_signal(self):
        orng = opening_range(self.bars, 5)
        sig, why = evaluate_long("X", Quote("X", 10.10, 10.09, 10.10), self.bars, orng, self.cfg)
        self.assertIsNone(sig)
        self.assertIn("waiting", why)
        sig, why = evaluate_long("X", Quote("X", 10.14, 10.14, 10.15), self.bars, orng, self.cfg)
        self.assertIsNotNone(sig, why)
        self.assertAlmostEqual(sig.stop, 10.04, places=2)       # range midpoint (rounded)
        self.assertAlmostEqual(sig.target, 10.15 + 2 * (10.15 - orng.mid), places=2)

    def test_wide_spread_and_chase_are_skipped(self):
        orng = opening_range(self.bars, 5)
        sig, why = evaluate_long("X", Quote("X", 10.14, 10.10, 10.20), self.bars, orng, self.cfg)
        self.assertIsNone(sig)
        self.assertIn("spread", why)
        sig, why = evaluate_long("X", Quote("X", 10.40, 10.40, 10.41), self.bars, orng, self.cfg)
        self.assertIsNone(sig)
        self.assertIn("extended", why)

    def test_manage_breakeven_target_stop(self):
        p = Position("X", 10, 10.0, 9.9, 10.2, 0.1, high_water=10.0)
        self.assertIsNone(manage(p, Quote("X", 10.05, 10.05, 10.06), self.cfg))
        self.assertIsNone(manage(p, Quote("X", 10.11, 10.11, 10.12), self.cfg))
        self.assertTrue(p.breakeven_done)
        self.assertEqual(p.stop, 10.0)
        self.assertEqual(manage(p, Quote("X", 10.0, 10.0, 10.01), self.cfg), "breakeven stop")
        p2 = Position("X", 10, 10.0, 9.9, 10.2, 0.1)
        self.assertEqual(manage(p2, Quote("X", 10.2, 10.2, 10.21), self.cfg), "target")
        p3 = Position("X", 10, 10.0, 9.9, 10.2, 0.1)
        self.assertEqual(manage(p3, Quote("X", 9.9, 9.89, 9.9), self.cfg), "stop")


class RiskAndCalendarTests(unittest.TestCase):
    def setUp(self):
        self.cal = MarketCalendar(BotConfig().session)
        self.j = Journal(":memory:")
        self.rm = RiskManager(RiskConfig(), self.j, self.cal, "paper")

    def test_sizing(self):
        self.assertEqual(self.rm.size(10.0, 9.9, 10000), 15)       # $150 cap beats $5/0.10=50 shares
        self.assertEqual(self.rm.size(10.0, 9.0, 10000), 5)        # risk $5 / $1
        self.assertEqual(self.rm.size(10.0, 9.9, 100), 5)          # 50% of $100 buying power
        self.assertEqual(self.rm.size(400.0, 399.0, 10000), 0)     # can't afford one share under the cap
        self.assertAlmostEqual(self.rm.size(400.0, 399.0, 10000, fractional=True), 0.375)

    def test_pdt_guard_and_limits(self):
        ts = datetime(2026, 9, 30, 10, 0, tzinfo=self.cal.tz)
        for _ in range(3):
            t = self.j.open_trade("paper", ts, "X", 1, 10, 9.9, 10.2, "t")
            self.j.close_trade(t, ts + timedelta(minutes=5), 10.1, "target")
        ok, why = self.rm.can_enter(TODAY, 0, 1000)
        self.assertFalse(ok)
        self.assertIn("PDT", why)
        ok, _ = self.rm.can_enter(TODAY, 0, 30000)
        self.assertTrue(ok)
        # Trades older than five sessions roll off.
        ok, _ = self.rm.can_enter(date(2026, 10, 8), 0, 1000)
        self.assertTrue(ok)

    def test_daily_loss_halts(self):
        ts = datetime(2026, 10, 1, 10, 0, tzinfo=self.cal.tz)
        t = self.j.open_trade("paper", ts, "X", 10, 10, 9, 12, "t")
        self.j.close_trade(t, ts, 8.4, "stop")                    # -16
        ok, why = self.rm.can_enter(TODAY, 0, 50000)
        self.assertFalse(ok)
        self.assertIn("daily loss", why)
        self.assertTrue(self.rm.loss_breached(TODAY, 0))

    def test_calendar(self):
        self.assertFalse(self.cal.is_trading_day(date(2026, 11, 26)))    # Thanksgiving
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 3)))     # Saturday
        self.assertEqual(self.cal.next_trading_day(date(2026, 10, 3)), date(2026, 10, 5))
        self.assertEqual(self.cal.flatten_time(date(2026, 11, 27)).strftime("%H:%M"), "11:00")
        self.assertEqual(self.cal.business_days_back(date(2026, 10, 5), 5), date(2026, 9, 29))

    def test_config_yaml(self):
        here = os.path.join(os.path.dirname(__file__), "..", "pbot.yaml")
        cfg = load_config(here)
        self.assertFalse(cfg.live.enabled)
        self.assertEqual(cfg.risk.risk_per_trade_usd, 5)
        c2 = with_overrides(cfg, **{"risk.max_open_positions": 5})
        self.assertEqual(c2.risk.max_open_positions, 5)
        self.assertEqual(cfg.risk.max_open_positions, 2)


class FakePublic:
    """httpx.MockTransport handler that mimics the Public endpoints the bot uses."""

    def __init__(self):
        self.calls = []
        self.orders = {}
        self.auth_count = 0
        self.fail_next_401 = False
        self.fill_on_poll = 1          # order turns FILLED on the n-th status poll (0 = never)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, body, req.headers.get("authorization")))
        if path.endswith("/personal/access-tokens"):
            self.auth_count += 1
            assert body["secret"] == "s3cret"
            return httpx.Response(200, json={"accessToken": f"tok{self.auth_count}"})
        if self.fail_next_401:
            self.fail_next_401 = False
            return httpx.Response(401, json={"message": "expired"})
        if path.endswith("/quotes"):
            return httpx.Response(200, json={"quotes": [
                {"instrument": i, "outcome": "SUCCESS", "last": "10.05", "bid": "10.04", "ask": "10.06"}
                for i in body["instruments"]]})
        if "/historicdata/EQUITY/" in path:
            return httpx.Response(200, json={"regularMarket": {"bars": [
                {"timestamp": "2026-10-01T13:30:00Z", "open": "10", "high": "10.1", "low": "9.9",
                 "close": "10.05", "volume": "12345", "value": "10.05"}]}})
        if path.endswith("/order") and req.method == "POST":
            self.orders[body["orderId"]] = {"polls": 0, "body": body, "status": "NEW"}
            return httpx.Response(200, json={"orderId": body["orderId"]})
        if "/order/" in path:
            oid = path.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            if o is None:
                return httpx.Response(404, json={"message": "not found"})
            if req.method == "DELETE":
                o["status"] = "CANCELLED"
                return httpx.Response(200, json={})
            o["polls"] += 1
            if o["status"] == "NEW" and self.fill_on_poll and o["polls"] >= self.fill_on_poll:
                o["status"] = "FILLED"
            filled = o["body"]["quantity"] if o["status"] == "FILLED" else "0"
            return httpx.Response(200, json={"orderId": oid, "status": o["status"], "filledQuantity": filled,
                                             "averagePrice": "10.06" if o["status"] == "FILLED" else None})
        if path.endswith("/portfolio/v2"):
            return httpx.Response(200, json={"accountId": "A1", "accountType": "BROKERAGE",
                                             "buyingPower": {"cashOnlyBuyingPower": "900", "buyingPower": "1800",
                                                             "optionsBuyingPower": "0"},
                                             "totalAccountValue": "1000", "equity": [], "positions": [],
                                             "orders": []})
        return httpx.Response(404, json={"message": "unknown"})


class PublicClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePublic()
        self.client = PublicClient("s3cret", "A1", http=httpx.Client(transport=httpx.MockTransport(self.fake)),
                                   sleep=lambda s: None)

    def test_auth_quotes_bars(self):
        q = self.client.quotes(["SOFI", "F"])
        self.assertEqual(q["SOFI"].ask, 10.06)
        self.assertEqual(self.fake.calls[1][3], "Bearer tok1")
        bars = self.client.bars_today("SOFI")
        self.assertEqual(bars[0].volume, 12345)
        self.assertEqual(self.fake.auth_count, 1)             # token reused

    def test_reauth_on_401(self):
        self.client.quotes(["SOFI"])
        self.fake.fail_next_401 = True
        self.client.quotes(["SOFI"])
        self.assertEqual(self.fake.auth_count, 2)

    def test_order_body_and_live_broker(self):
        b = LiveBroker(self.client, ExecutionConfig(), sleep=lambda s: None)
        st = b.buy("SOFI", 3, 10.07)
        self.assertEqual(st.status, "FILLED")
        self.assertEqual((st.filled_qty, st.avg_price), (3.0, 10.06))
        post = [c for c in self.fake.calls if c[0] == "POST" and c[1].endswith("/order")][0][2]
        self.assertEqual(post["instrument"], {"symbol": "SOFI", "type": "EQUITY"})
        self.assertEqual((post["orderSide"], post["orderType"], post["quantity"], post["limitPrice"]),
                         ("BUY", "LIMIT", "3", "10.07"))
        self.assertEqual(post["expiration"], {"timeInForce": "DAY"})
        self.assertEqual(len(post["orderId"]), 36)
        self.assertEqual(b.account(), (1000.0, 900.0))

    def test_unfilled_entry_is_cancelled(self):
        self.fake.fill_on_poll = 0
        b = LiveBroker(self.client, ExecutionConfig(order_timeout_s=2), sleep=lambda s: None)
        st = b.buy("SOFI", 3, 10.0)
        self.assertEqual(st.status, "CANCELLED")
        self.assertEqual(st.filled_qty, 0)
        self.assertTrue(any(c[0] == "DELETE" for c in self.fake.calls))


class AgentTests(unittest.TestCase):
    def run_sim(self, cfg, seed=7, day=TODAY):
        cal = MarketCalendar(cfg.session)
        clock, market, broker = make_sim(cfg.watchlist, cal, cal.open_time(day) - timedelta(minutes=10),
                                         seed, cfg.paper.starting_cash)
        j = Journal(":memory:")
        agent = Agent(cfg, "sim", clock, market, broker, j, cal)
        agent.run_day()
        return agent, j, broker, cal

    def test_full_day_trades_and_ends_flat(self):
        cfg = with_overrides(cfg_tmp(), **{"risk.pdt_guard": False})
        traded = 0
        for seed in range(1, 6):
            agent, j, broker, cal = self.run_sim(cfg, seed)
            rows = j.trades("sim", TODAY)
            traded += len(rows)
            self.assertEqual(broker.positions(), {}, "must end the window flat")
            self.assertFalse(agent.positions)
            for r in rows:
                self.assertIsNotNone(r["exit_ts"])
                entry = datetime.fromisoformat(r["entry_ts"])
                self.assertGreaterEqual(entry, cal.range_end(TODAY))
                self.assertLess(entry, cal.entry_cutoff(TODAY))
                self.assertLessEqual(datetime.fromisoformat(r["exit_ts"]), cal.flatten_time(TODAY)
                                     + timedelta(seconds=5))
            self.assertLessEqual(len(rows), cfg.risk.max_trades_per_day)
        self.assertGreater(traded, 0)

    def test_holiday_does_nothing(self):
        agent, j, _, _ = self.run_sim(cfg_tmp(), day=date(2026, 11, 26))
        self.assertEqual(j.trades("sim"), [])

    def test_kill_switch(self):
        cfg = cfg_tmp()
        open(os.path.join(cfg.data_dir, "STOP"), "w").close()
        agent, j, broker, _ = self.run_sim(cfg)
        self.assertEqual(j.trades("sim"), [])
        self.assertEqual(agent.risk.halted, "kill switch file")

    def test_broker_safety_stop_fill_is_recorded(self):
        cfg = cfg_tmp()
        cal = MarketCalendar(cfg.session)
        clock = SimClock(cal.open_time(TODAY) + timedelta(minutes=20))

        class StopFilledBroker(PaperBroker):
            software_stops_only = False

            def cancel(self, oid):
                return OrderState(oid, "FILLED", 5, 9.85)

        broker = StopFilledBroker(lambda s: Quote(s, 9.8, 9.8, 9.81), 1000)
        j = Journal(":memory:")
        agent = Agent(cfg, "paper", clock, None, broker, j, cal)
        tid = j.open_trade("paper", clock.now(), "X", 5, 10.0, 9.9, 10.2, "t")
        agent.positions["X"] = Position("X", 5, 10.0, 9.9, 10.2, 0.1, trade_id=tid, broker_stop_id="S1")
        agent.quotes = {"X": Quote("X", 9.8, 9.8, 9.81)}
        agent._exit("X", "stop")
        row = j.trades("paper")[0]
        self.assertEqual(row["exit_reason"], "broker safety stop")
        self.assertAlmostEqual(row["pnl"], -0.75)

    def test_rejected_entry_does_not_end_the_day(self):
        cfg = with_overrides(cfg_tmp(), **{"risk.pdt_guard": False})
        cal = MarketCalendar(cfg.session)
        clock, market, broker = make_sim(cfg.watchlist, cal, cal.open_time(TODAY) - timedelta(minutes=10),
                                         1, cfg.paper.starting_cash)

        def reject(*a, **k):
            raise RuntimeError("HTTP 400: insufficient buying power")
        broker.buy = reject
        j = Journal(":memory:")
        agent = Agent(cfg, "sim", clock, market, broker, j, cal)
        agent.run_day()
        self.assertGreaterEqual(clock.now(), cal.flatten_time(TODAY))
        self.assertTrue(any("entry error" in v for v in agent.done.values()))

    def test_only_bot_symbols_orders_are_cancelled(self):
        fake = FakePublic()
        client = PublicClient("s3cret", "A1", http=httpx.Client(transport=httpx.MockTransport(fake)),
                              sleep=lambda s: None)
        b = LiveBroker(client, ExecutionConfig(), sleep=lambda s: None)
        orders = [{"orderId": "mine", "instrument": {"symbol": "SOFI"}, "status": "NEW"},
                  {"orderId": "users", "instrument": {"symbol": "AAPL"}, "status": "NEW"}]
        client.portfolio = lambda: {"orders": orders}
        self.assertEqual(b.open_order_ids(["SOFI"]), ["mine"])


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = BotConfig()
        self.j = Journal(":memory:")
        self.lad = Ladder(self.cfg.ladder, self.j)
        self.tz = MarketCalendar(self.cfg.session).tz
        self.t0 = datetime(2026, 9, 1, 9, 0, tzinfo=self.tz)

    def trade(self, day_offset, pnl, mode="paper"):
        ts = self.t0 + timedelta(days=day_offset, hours=1)
        t = self.j.open_trade(mode, ts, "F", 10, 10.0, 9.8, 10.4, "t")
        self.j.close_trade(t, ts + timedelta(minutes=10), 10.0 + pnl / 10, "x")

    def test_starts_micro_and_applies_limits(self):
        cfg = BotConfig()
        self.assertEqual(self.lad.apply(cfg, "paper", self.t0), 0)
        self.assertEqual((cfg.risk.risk_per_trade_usd, cfg.risk.max_open_positions), (2, 1))

    def test_promotion_must_be_earned(self):
        self.lad.state("paper", self.t0)
        ok, msg = self.lad.promote("paper", self.t0 + timedelta(days=1))
        self.assertFalse(ok)
        self.assertIn("not yet", msg)
        for i in range(24):                       # 24 trades over 12 days, 2:1 winners
            self.trade(i // 2, 2.0 if i % 3 else -2.0)
        ok, msg = self.lad.promote("paper", self.t0 + timedelta(days=13))
        self.assertTrue(ok, msg)
        self.assertEqual(self.lad.state("paper", self.t0)[0], 1)
        cfg = BotConfig()
        self.lad.apply(cfg, "paper", self.t0)
        self.assertEqual(cfg.risk.risk_per_trade_usd, 5)
        # The record restarts at the new level.
        ok, _ = self.lad.promote("paper", self.t0 + timedelta(days=13, minutes=1))
        self.assertFalse(ok)

    def test_losing_record_cannot_promote_and_force_works(self):
        self.lad.state("paper", self.t0)
        for i in range(24):
            self.trade(i // 2, -1.0 if i % 3 else 1.0)
        ok, msg = self.lad.promote("paper", self.t0 + timedelta(days=13))
        self.assertFalse(ok)
        self.assertIn("net P&L", msg)
        ok, _ = self.lad.promote("paper", self.t0 + timedelta(days=13), force=True)
        self.assertTrue(ok)

    def test_auto_demotion_on_drawdown(self):
        self.lad._set("live", 1, self.t0, "test")              # 'small': risk $5 -> demote at -$30
        for i in range(6):
            self.trade(i, -5.5, mode="live")
        msg = self.lad.check_demotion("live", self.t0 + timedelta(days=7))
        self.assertIn("demoted", msg)
        self.assertEqual(self.lad.state("live", self.t0)[0], 0)
        self.assertIsNone(self.lad.check_demotion("live", self.t0 + timedelta(days=7)))   # floor

    def test_live_needs_paper_record(self):
        ready, _ = self.lad.paper_ready_for_live(self.t0)
        self.assertFalse(ready)
        self.lad._set("paper", 1, self.t0, "test")
        ready, _ = self.lad.paper_ready_for_live(self.t0)
        self.assertTrue(ready)

    def test_bad_level_key_rejected(self):
        path = os.path.join(tempfile.mkdtemp(), "c.yaml")
        with open(path, "w") as f:
            f.write("ladder:\n  levels:\n    - {name: x, risk_per_trade_usd: 1, bogus: 2}\n")
        with self.assertRaises(ValueError):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
