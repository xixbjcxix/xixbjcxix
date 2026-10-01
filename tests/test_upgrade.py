"""Tests for the strategy / fee-audit / ops upgrade. No network needed."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kbot.book import KalshiBookStore, no_token, yes_token  # noqa: E402
from kbot.config import load_config, with_overrides  # noqa: E402
from kbot.core import TradingCore  # noqa: E402
from kbot.dashboard import validate_settings  # noqa: E402
from kbot.dayreport import build_day_report, format_day_report  # noqa: E402
from kbot.fees import fee_model_for  # noqa: E402
from kbot.inventory import LegEpisode, MarketInventory  # noqa: E402
from kbot.models import BUY, DOWN, UP, Fill  # noqa: E402
from kbot.ops import AlertsConfig, FeeAudit, Notifier, PnlHistory, supervise  # noqa: E402
from kbot.signal import MomentumSignal, SignalConfig, SignalReading, SpotTracker  # noqa: E402
from kbot.store import NullStore, Store  # noqa: E402
from kbot.strategy import ResidualConfig, Strategy, StrategyConfig  # noqa: E402

from test_kalshi import TK, mk_market, snap  # noqa: E402

SIG_UP = dict(ok=True, direction=UP, z=1.5, p_model=0.80, vol_ratio=1.0)


def books_for(yes, no):
    bs = KalshiBookStore()
    bs.handle(snap(yes, no), 0)
    return {UP: bs.book(yes_token(TK)), DOWN: bs.book(no_token(TK))}


class SignalUpgradeTests(unittest.TestCase):
    def test_p_side_and_vol_ratio(self):
        self.assertAlmostEqual(SignalReading(**SIG_UP).p_side(UP), 0.80)
        self.assertAlmostEqual(SignalReading(**SIG_UP).p_side(DOWN), 0.20)
        self.assertIsNone(SignalReading(ok=False, direction=None).p_side(UP))
        sp = SpotTracker()
        m = mk_market(start_ms=0)
        for i in range(0, 700):                       # calm for 10 minutes ...
            sp.add("btc", i * 1000, 78000 * (1 + 0.00001 * (i % 3 - 1)))
        for i in range(700, 900):                     # ... then a volatility spike
            sp.add("btc", i * 1000, 78000 * (1 + 0.0004 * ((i % 2) * 2 - 1)))
        s = MomentumSignal(sp, SignalConfig()).read(m, 899000)
        self.assertTrue(s.ok)
        self.assertGreater(s.vol_ratio, 1.5)


class StrategyUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.m = mk_market()
        self.fee = fee_model_for(self.m)
        self.books = books_for([(0.48, 100), (0.47, 100)], [(0.49, 100), (0.48, 100)])

    def plan(self, cfg, sig=None, inv=None):
        return Strategy(cfg).plan(self.m, 60000, self.books, inv or MarketInventory(market=TK),
                                  sig or SignalReading(ok=True, direction=None), self.fee)

    def test_vol_widen_lowers_bids(self):
        base = self.plan(StrategyConfig(target_pair_cost=0.94))
        wide = self.plan(StrategyConfig(target_pair_cost=0.94, vol_widen_per_unit=0.04),
                         SignalReading(ok=True, direction=None, vol_ratio=2.0))
        sb = base.quotes[UP].price + base.quotes[DOWN].price
        sw = wide.quotes[UP].price + wide.quotes[DOWN].price
        self.assertLess(sw, sb)
        # capped
        capped = Strategy(StrategyConfig(vol_widen_per_unit=1.0, max_vol_widen=0.03))
        self.assertAlmostEqual(capped.vol_widen(SignalReading(ok=True, direction=None, vol_ratio=9.0)), 0.03)
        self.assertEqual(Strategy(StrategyConfig()).vol_widen(SignalReading(**SIG_UP)), 0.0)

    def test_adverse_skew_lowers_the_side_spot_is_leaving(self):
        base = self.plan(StrategyConfig(), SignalReading(**SIG_UP))
        skew = self.plan(StrategyConfig(adverse_skew_ticks=2), SignalReading(**SIG_UP))
        self.assertEqual(skew.quotes[UP].price, base.quotes[UP].price)           # favoured side untouched
        self.assertLess(skew.quotes[DOWN].price, base.quotes[DOWN].price)        # adverse side pulled back
        # signal below the guard threshold: no change
        weak = self.plan(StrategyConfig(adverse_skew_ticks=2), SignalReading(ok=True, direction=UP, z=0.5))
        self.assertEqual(weak.quotes[DOWN].price, base.quotes[DOWN].price)

    def test_max_legs_per_market(self):
        inv = MarketInventory(market=TK)
        inv.episodes = [LegEpisode(0, UP, 5, 1, "cut"), LegEpisode(2, DOWN, 5, 3, "passive")]
        self.assertEqual(self.plan(StrategyConfig(max_legs_per_market=2), inv=inv).mode, "leg_limit")
        self.assertIsNotNone(self.plan(StrategyConfig(max_legs_per_market=3), inv=inv).quotes[UP])
        self.assertIsNotNone(self.plan(StrategyConfig(), inv=inv).quotes[UP])

    def _holding_up(self):
        inv = MarketInventory(market=TK)
        inv.apply_fill(Fill(order_id="o", market=TK, outcome=UP, side=BUY, price=0.47, size=5, liquidity="maker",
                            fee=0.0, rebate=0.0, ts_ms=1000, tag="base"))
        return inv

    def test_residual_edge_gate(self):
        # leg bought at 0.47; model says UP wins 80% -> edge ~0.32: ride it
        ride = self.plan(StrategyConfig(residual=ResidualConfig(min_edge=0.02)), SignalReading(**SIG_UP),
                         self._holding_up())
        self.assertEqual(ride.mode, "ride_residual")
        # model only 48% -> no edge after fees: must work to complete the pair instead of riding
        thin = SignalReading(ok=True, direction=UP, z=0.7, p_model=0.48)
        no_edge = self.plan(StrategyConfig(residual=ResidualConfig(min_edge=0.02)), thin, self._holding_up())
        self.assertNotEqual(no_edge.mode, "ride_residual")
        self.assertIsNotNone(no_edge.quotes[DOWN])
        # gate off -> direction agreement alone lets it ride (old behaviour)
        old = self.plan(StrategyConfig(residual=ResidualConfig(min_edge=0.0)), thin, self._holding_up())
        self.assertEqual(old.mode, "ride_residual")


class FeeAuditTests(unittest.TestCase):
    def setUp(self):
        self.fee = fee_model_for(mk_market())

    def test_matching_fees_do_not_flag(self):
        a = FeeAudit(min_fills=5)
        for _ in range(10):
            self.assertIsNone(a.record(self.fee, "maker", 10, 0.47, self.fee.maker_fee(10, 0.47)))
            self.assertIsNone(a.record(self.fee, "taker", 10, 0.47, self.fee.taker_fee(10, 0.47)))
        self.assertFalse(a.flagged)
        self.assertAlmostEqual(a.drift, 0.0)

    def test_single_large_overcharge_flags_once(self):
        a = FeeAudit()
        msg = a.record(self.fee, "maker", 10, 0.47, 0.30)
        self.assertIn("charged", msg)
        self.assertIsNone(a.record(self.fee, "maker", 10, 0.47, 0.30))      # already flagged
        self.assertTrue(a.snapshot()["flagged"])

    def test_cumulative_drift_flags(self):
        a = FeeAudit(tolerance=0.25, min_fills=10, abs_tol=1.0)
        modeled = self.fee.maker_fee(10, 0.47)
        msgs = [a.record(self.fee, "maker", 10, 0.47, modeled * 1.5) for _ in range(12)]
        self.assertTrue(any(msgs))
        self.assertGreater(a.drift, 0.25)

    def test_undercharge_is_fine(self):
        a = FeeAudit(min_fills=3)
        for _ in range(10):
            self.assertIsNone(a.record(self.fee, "maker", 10, 0.47, 0.0))     # e.g. a fee-free series
        self.assertFalse(a.flagged)

    def test_core_audits_real_fills_only(self):
        cfg = load_config(None, overlay=None)
        for mode, expect in (("paper", 0), ("demo", 1)):
            core = TradingCore(cfg, NullStore(), "r", mode=mode)
            core.add_market(mk_market())
            seen = []
            core.notify = lambda k, t: seen.append(k)
            core._handle_fills([Fill(order_id="o", market=TK, outcome=UP, side=BUY, price=0.47, size=10,
                                     liquidity="maker", fee=5.0, rebate=0.0, ts_ms=1, tag="base")])
            self.assertEqual(core.fee_audit.n, expect)
            self.assertEqual(seen, ["fee_drift"] if expect else [])


class NotifierTests(unittest.TestCase):
    def test_rate_limit_and_disabled(self):
        n = Notifier(AlertsConfig(min_interval_s=60))
        n.webhook = n.tg_token = ""                       # nothing configured: log only
        self.assertTrue(n.send("kill", "x"))
        self.assertFalse(n.send("kill", "again"))         # same kind inside the interval
        self.assertTrue(n.send("data_gap", "y"))          # different kind
        self.assertTrue(n.send("kill", "forced", force=True))
        self.assertEqual([k for _, k, _ in n.sent], ["kill", "data_gap", "kill"])
        self.assertFalse(Notifier(AlertsConfig(enabled=False)).send("kill", "x"))

    def test_webhook_post(self):
        posted = []

        class Fake(Notifier):
            async def _post(self, text):
                posted.append(text)

        async def go():
            n = Fake(AlertsConfig())
            n.webhook = "https://example.invalid/hook"
            n.send("kill", "boom")
            await asyncio.sleep(0.05)
        asyncio.run(go())
        self.assertEqual(len(posted), 1)
        self.assertIn("boom", posted[0])

    def test_core_alerts_on_kill_gap_and_halt(self):
        cfg = load_config(None, overlay=None)
        core = TradingCore(cfg, NullStore(), "r", mode="paper")
        seen = []
        core.notify = lambda k, t: seen.append(k)
        core.on_data_gap("ws_closed", 1000)
        core.kill("test")
        core.kill("again")                                # only the first kill alerts
        self.assertEqual(seen, ["data_gap", "kill"])


class SupervisorTests(unittest.TestCase):
    def run_sup(self, behaviours, **kw):
        calls, notes, sleeps = [], [], []

        async def once(attempt):
            calls.append(attempt)
            b = behaviours[min(attempt, len(behaviours) - 1)]
            if b == "crash":
                raise RuntimeError("boom")
            if b == "exit":
                raise SystemExit("bad keys")

        async def fake_sleep(d):
            sleeps.append(d)

        async def go():
            return await supervise(once, notify=lambda k, t: notes.append(k), sleep=fake_sleep, **kw)
        return go, calls, notes, sleeps

    def test_restarts_after_crash_then_stops_cleanly(self):
        go, calls, notes, sleeps = self.run_sup(["crash", "crash", "ok"], max_restarts=5)
        self.assertEqual(asyncio.run(go()), 2)
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(notes, ["engine_restart", "engine_restart"])
        self.assertEqual(sleeps, [5.0, 10.0])             # exponential backoff

    def test_gives_up_after_too_many_crashes(self):
        go, calls, notes, _ = self.run_sup(["crash"], max_restarts=2)
        with self.assertRaises(RuntimeError):
            asyncio.run(go())
        self.assertEqual(len(calls), 3)
        self.assertEqual(notes[-1], "supervisor_giving_up")

    def test_systemexit_never_retried(self):
        go, calls, _, _ = self.run_sup(["exit"])
        with self.assertRaises(SystemExit):
            asyncio.run(go())
        self.assertEqual(calls, [0])


class DayReportTests(unittest.TestCase):
    def test_report_from_db(self):
        import datetime as dt
        base = int(dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            st = Store(path)
            st.run("demo-1", "demo", "x", {})
            st.run("paper-1", "paper", "x", {})
            st.pnl("demo-1", base, "M1", "settle", 5.0, {"paired_shares": 100, "paired_capital": 90.0,
                   "paired_pnl": 10.0, "residual_pnl": -2.0, "cut_pnl": -1.0, "fees": 2.0,
                   "residual_capital": 5.0})
            st.pnl("demo-1", base + 1000, "M2", "settle", -3.0, {"paired_shares": 0, "paired_capital": 0,
                   "paired_pnl": 0.0, "residual_pnl": -3.0, "cut_pnl": 0.0, "fees": 0.0, "residual_capital": 3.0})
            st.pnl("paper-1", base, "M3", "settle", 99.0, {})
            st.pnl("demo-1", base + 86_400_000, "M4", "settle", 50.0, {})     # next day: excluded
            st.fill("demo-1", Fill(order_id="a", market="M1", outcome=UP, side=BUY, price=0.4, size=10,
                                   liquidity="maker", fee=0.1, rebate=0, ts_ms=base, tag="b"))
            st.risk_event("demo-1", base, "data_gap", "x")
            st.close()
            r = build_day_report(path, "2026-10-01", "demo")
            self.assertEqual(r["markets_settled"], 2)
            self.assertEqual(r["net_pnl"], 2.0)
            self.assertEqual(r["paired_pnl"], 10.0)
            self.assertEqual(r["pair_edge_cents"], 10.0)
            self.assertEqual(r["win_rate"], 50.0)
            self.assertEqual(r["worst"]["market"], "M2")
            self.assertEqual(r["fills"]["maker"]["n"], 1)
            self.assertEqual(r["risk_events"], {"data_gap": 1})
            self.assertIn("NET +2.00", format_day_report(r))
            self.assertEqual(build_day_report(path, "2026-10-01")["markets_settled"], 3)   # all modes
            with self.assertRaises(FileNotFoundError):
                build_day_report(os.path.join(d, "nope.db"))


class DashboardOpsTests(unittest.TestCase):
    def test_snapshot_has_ops_fields(self):
        cfg = load_config(None, overlay=None)
        core = TradingCore(cfg, NullStore(), "r", mode="paper")
        core.add_market(mk_market())
        core.started_ms = 1000
        core.on_timer(61_000)
        core.on_timer(80_000)
        s = json.loads(json.dumps(core.snapshot(), default=str))
        self.assertGreaterEqual(len(s["pnl_history"]), 2)
        self.assertEqual(s["fee_audit"]["fills"], 0)
        self.assertEqual(s["health"]["restarts"], 0)
        self.assertIn("gaps", s["health"])

    def test_pnl_history_downsamples(self):
        h = PnlHistory(every_ms=10_000, maxlen=3)
        for t in range(0, 100_000, 1000):
            h.add(t, t / 1000)
        self.assertEqual(len(h.points), 3)
        self.assertEqual(h.points[-1][0], 90_000)

    def test_new_settings_validated(self):
        ok = validate_settings({"strategy.residual.min_edge": 0.03, "strategy.adverse_skew_ticks": 2})
        self.assertEqual(ok["strategy.adverse_skew_ticks"], 2.0)
        with self.assertRaises(ValueError):
            validate_settings({"strategy.residual.min_edge": 5})

    def test_config_loads_alerts_and_new_strategy_keys(self):
        cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"), overlay=None)
        self.assertEqual(cfg.alerts.fee_drift_min_fills, 20)
        self.assertEqual(cfg.strategy.residual.min_edge, 0.02)
        self.assertEqual(cfg.strategy.adverse_skew_ticks, 0)
        c2 = with_overrides(cfg, **{"strategy.max_legs_per_market": 3})
        self.assertEqual(c2.strategy.max_legs_per_market, 3)


if __name__ == "__main__":
    unittest.main()
