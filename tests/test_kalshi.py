"""Kalshi-specific tests. Run: python -m unittest discover -s tests   (no network needed)"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kbot.book import KalshiBookStore, no_token, yes_token  # noqa: E402
from kbot.config import load_config, with_overrides  # noqa: E402
from kbot.core import TradingCore  # noqa: E402
from kbot.fees import FeeModel, fee_model_for  # noqa: E402
from kbot.feeds.discovery import parse_market  # noqa: E402
from kbot.feeds.kalshi_rest import KalshiError, sign, v2_order_fields  # noqa: E402
from kbot.fillmodel import FillModelConfig, SimExchange  # noqa: E402
from kbot.inventory import MarketInventory  # noqa: E402
from kbot.models import BUY, DOWN, SELL, UP, Fill, MarketInfo, Order, OrderStatus  # noqa: E402
from kbot.signal import SignalConfig, MomentumSignal, SignalReading, SpotTracker  # noqa: E402
from kbot.store import NullStore  # noqa: E402
from kbot.strategy import Strategy, StrategyConfig  # noqa: E402

TK = "KXBTC15M-26SEP301215-15"


def mk_market(start_ms=0, fee_type="quadratic_with_maker_fees"):
    return MarketInfo(slug=TK, asset="btc", horizon_s=900, start_ms=start_ms, end_ms=start_ms + 900000,
                      token_up=yes_token(TK), token_down=no_token(TK), tick_size=0.01, min_order_size=1,
                      open_price=78000.0, fee_type=fee_type, fee_multiplier=1.0)


def snap(yes, no):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": TK, "yes_dollars_fp": [[f"{p:.4f}", f"{c:.2f}"] for p, c in yes],
        "no_dollars_fp": [[f"{p:.4f}", f"{c:.2f}"] for p, c in no]}}


def delta(side, p, d):
    return {"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {
        "market_ticker": TK, "side": side, "price_dollars": f"{p:.4f}", "delta_fp": f"{d:.2f}"}}


def trade(taker_side, yes_px, n, tid="t1"):
    return {"type": "trade", "msg": {"market_ticker": TK, "trade_id": tid, "taker_side": taker_side,
                                     "yes_price_dollars": f"{yes_px:.4f}", "no_price_dollars": f"{1 - yes_px:.4f}",
                                     "count_fp": f"{n:.2f}"}}


class FeeTests(unittest.TestCase):
    def test_formula_and_rounding(self):
        fm = FeeModel()                                     # maker fees on, cent rounding
        self.assertAlmostEqual(fm.taker_fee(100, 0.50), 1.75)         # 0.07*100*.25
        self.assertAlmostEqual(fm.taker_fee(1, 0.50), 0.02)           # 0.0175 -> rounds UP to 0.02
        self.assertAlmostEqual(fm.maker_fee(100, 0.50), 0.44)         # 0.4375 -> 0.44
        self.assertAlmostEqual(fm.taker_fee(10, 0.99), 0.01)          # 0.00693 -> 0.01
        fine = FeeModel(precision=0.0001)
        self.assertAlmostEqual(fine.taker_fee(1, 0.50), 0.0175)

    def test_series_fee_types(self):
        self.assertEqual(fee_model_for(mk_market(fee_type="quadratic")).maker_fee(100, 0.5), 0.0)
        self.assertAlmostEqual(fee_model_for(mk_market(fee_type="quadratic_with_combo_maker_fees")).maker_fee(100, 0.5), 0.88)
        m = mk_market()
        m.fee_multiplier = 2.0
        self.assertAlmostEqual(fee_model_for(m).taker_fee(100, 0.5), 3.5)
        self.assertFalse(fee_model_for(mk_market(fee_type="flat")).supported)


class SigningTests(unittest.TestCase):
    def test_rsa_pss_and_ed25519(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sig = base64.b64decode(sign(k, 1700000000000, "post", "/trade-api/v2/portfolio/events/orders?x=1"))
        k.public_key().verify(sig, b"1700000000000POST/trade-api/v2/portfolio/events/orders",
                              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                              hashes.SHA256())
        e = ed25519.Ed25519PrivateKey.generate()
        sig = base64.b64decode(sign(e, 1, "GET", "/trade-api/ws/v2"))
        e.public_key().verify(sig, b"1GET/trade-api/ws/v2")

    def test_v2_order_mapping(self):
        self.assertEqual(v2_order_fields(True, True, 0.47), ("bid", 0.47))     # buy YES
        self.assertEqual(v2_order_fields(False, True, 0.40), ("ask", 0.60))    # buy NO @0.40
        self.assertEqual(v2_order_fields(True, False, 0.55), ("ask", 0.55))    # sell YES
        self.assertEqual(v2_order_fields(False, False, 0.30), ("bid", 0.70))   # sell NO


class BookTests(unittest.TestCase):
    def test_yes_no_books_are_mirrors(self):
        bs = KalshiBookStore()
        bs.handle(snap([(0.45, 100), (0.46, 50)], [(0.50, 80), (0.52, 20)]), 1)
        y, n = bs.book(yes_token(TK)), bs.book(no_token(TK))
        self.assertEqual((y.best_bid, y.best_ask), (0.46, 0.48))
        self.assertEqual((n.best_bid, n.best_ask), (0.52, 0.54))
        evs = bs.handle(delta("no", 0.52, -20), 2)
        self.assertEqual(y.best_ask, 0.50)
        self.assertEqual(len(evs), 2)
        evs = bs.handle(trade("yes", 0.50, 10), 3)
        self.assertEqual({(t.token, t.side, t.price) for t in evs},
                         {(no_token(TK), SELL, 0.50), (yes_token(TK), BUY, 0.50)})


class FillIntegrationTests(unittest.TestCase):
    def test_yes_bid_filled_by_no_taker_after_queue(self):
        m = mk_market()
        bs = KalshiBookStore()
        ex = SimExchange(bs, FillModelConfig(order_latency_ms=0, cancel_latency_ms=0, taker_delay_ms=0),
                         lambda s: fee_model_for(m), lambda t: m)
        bs.handle(snap([(0.45, 30)], [(0.50, 80)]), 0)
        o = Order(market=TK, outcome=UP, side=BUY, price=0.45, size=10)
        ex.submit(o, m, 0)
        ex.advance(1)
        self.assertEqual(o.queue_ahead, 30)

        def feed(fr, t):
            out = []
            for ev in bs.handle(fr, t):
                out += ex.on_book_event(ev) if hasattr(ev, "changes") else ex.on_trade(ev)
            return out
        self.assertEqual(feed(trade("no", 0.45, 25, "a"), 2), [])
        f = feed(trade("no", 0.45, 20, "b"), 3)
        self.assertAlmostEqual(sum(x.size for x in f), 10)
        self.assertGreater(f[0].fee, 0)                   # Kalshi maker fee charged
        self.assertEqual(f[0].rebate, 0)


class StrategyTests(unittest.TestCase):
    def test_target_includes_maker_fees(self):
        m = mk_market()
        bs = KalshiBookStore()
        bs.handle(snap([(0.48, 100), (0.47, 100)], [(0.49, 100), (0.48, 100)]), 0)
        books = {UP: bs.book(yes_token(TK)), DOWN: bs.book(no_token(TK))}
        fee = fee_model_for(m)
        p = Strategy(StrategyConfig(target_pair_cost=0.94)).plan(m, 60000, books, MarketInventory(market=TK),
                                                                SignalReading(ok=True, direction=None), fee)
        qu, qd = p.quotes[UP], p.quotes[DOWN]
        allin = qu.price + qd.price + fee.maker_fee_per_share(qu.price) + fee.maker_fee_per_share(qd.price)
        self.assertLessEqual(allin, 0.94 + 1e-9)
        self.assertGreater(allin, 0.90)

    def test_cutoff_60s_before_close(self):
        m = mk_market()
        bs = KalshiBookStore()
        bs.handle(snap([(0.48, 100)], [(0.49, 100)]), 0)
        books = {UP: bs.book(yes_token(TK)), DOWN: bs.book(no_token(TK))}
        p = Strategy(StrategyConfig()).plan(m, 900000 - 59000, books, MarketInventory(market=TK),
                                            SignalReading(ok=True, direction=None), fee_model_for(m))
        self.assertTrue(p.cancel_all)


class SignalTests(unittest.TestCase):
    def test_z_score_direction(self):
        sp = SpotTracker()
        m = mk_market(start_ms=0)
        for i in range(0, 600):
            sp.add("btc", i * 1000, 78000 * (1 + 0.00001 * (i % 3 - 1)))
        for i in range(600, 660):
            sp.add("btc", i * 1000, 78000 * (1 + 0.0003 * (i - 599)))    # rally well above target
        s = MomentumSignal(sp, SignalConfig()).read(m, 659000)
        self.assertTrue(s.ok)
        self.assertEqual(s.direction, UP)
        self.assertGreater(s.p_model, 0.9)


class DiscoveryTests(unittest.TestCase):
    def test_parse_market(self):
        raw = {"ticker": TK, "event_ticker": "KXBTC15M-26SEP301215", "open_time": "2026-09-30T19:00:00Z",
               "close_time": "2026-09-30T19:15:00Z", "floor_strike": 78123.45, "strike_type": "greater_or_equal",
               "price_ranges": [{"start": "0.001", "end": "0.10", "step": "0.001"},
                                {"start": "0.10", "end": "0.90", "step": "0.01"},
                                {"start": "0.90", "end": "0.999", "step": "0.001"}], "result": "yes"}
        m = parse_market(raw, "btc", {"ticker": "KXBTC15M", "fee_type": "quadratic", "fee_multiplier": 1})
        self.assertEqual(m.horizon_s, 900)
        self.assertEqual(m.open_price, 78123.45)
        self.assertEqual(m.winner, UP)
        self.assertEqual(m.fee_type, "quadratic")
        self.assertEqual(m.tick_size, 0.01)


class FakeRest:
    base = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self, fail_status=None):
        self.created, self.cancelled, self.cancel_alls = [], [], 0
        self.fail_status = fail_status

    async def create_order(self, **kw):
        if self.fail_status:
            raise KalshiError(self.fail_status, '{"error":"nope"}')
        self.created.append(kw)
        return {"order_id": f"X{len(self.created)}", "fill_count": "0.00", "remaining_count": f"{kw['count']:.2f}"}

    async def cancel_order(self, oid, ticker=None):
        self.cancelled.append(oid)
        return {"order_id": oid}

    async def cancel_all(self):
        self.cancel_alls += 1
        return {}

    async def get_open_orders(self):
        return [{"order_id": "OLD1", "ticker": TK, "client_order_id": "kbot-o9-abc"},          # left by the bot
                {"order_id": "MINE", "ticker": TK, "client_order_id": "manual-123"},           # your manual order
                {"order_id": "OTHER", "ticker": "KXNFL-X", "client_order_id": "kbot-o1-x"}]    # other series


class DemoExchangeTests(unittest.TestCase):
    def _core(self, rest):
        from kbot.demo_exchange import KalshiExchange as KalshiDemoExchange
        cfg = with_overrides(load_config(None, overlay=None), **{"risk.kill_file": ""})
        holder = {}

        def factory(core):
            holder["ex"] = KalshiDemoExchange(rest, core, series_prefixes=["KXBTC15M", "KXETH15M"])
            return holder["ex"]
        core = TradingCore(cfg, NullStore(), "t", mode="demo", exchange_factory=factory)
        core.add_market(mk_market(start_ms=0))
        return core, holder["ex"]

    def test_production_host_needs_live_mode(self):
        from kbot.demo_exchange import KalshiExchange
        r = FakeRest()
        r.base = "https://external-api.kalshi.com/trade-api/v2"
        with self.assertRaises(RuntimeError):
            KalshiExchange(r, None)
        KalshiExchange(r, None, allow_prod=True)          # only `live` passes allow_prod

    def test_smoke_test_places_and_cancels(self):
        from kbot.demo_exchange import smoke_test

        async def go():
            rest = FakeRest()

            async def none():
                return []
            rest.get_open_orders = none
            msg = await smoke_test(rest, TK, 0.001)
            self.assertIn("OK", msg)
            kw = rest.created[0]
            self.assertEqual((kw["price"], kw["count"], kw["post_only"], kw["outcome_yes"]), (0.01, 1.0, True, True))
            self.assertEqual(rest.cancelled, ["X1"])
        asyncio.run(go())

    def test_place_fill_cancel(self):
        async def go():
            rest = FakeRest()
            core, ex = self._core(rest)
            o = Order(market=TK, outcome=DOWN, side=BUY, price=0.40, size=10, post_only=True)
            ex.submit(o, core.markets[TK].info, 0)
            await asyncio.sleep(0.01)
            self.assertEqual(o.status, OrderStatus.OPEN)
            kw = rest.created[0]
            self.assertFalse(kw["outcome_yes"])
            self.assertTrue(kw["post_only"])
            ex.on_private_frame({"type": "fill", "msg": {"trade_id": "T1", "order_id": o.exchange_id,
                                                        "yes_price_dollars": "0.6000", "count_fp": "4.00",
                                                        "fee_cost": "0.02", "is_taker": False}})
            ex.on_private_frame({"type": "fill", "msg": {"trade_id": "T1", "order_id": o.exchange_id,
                                                        "yes_price_dollars": "0.6000", "count_fp": "4.00"}})  # dup
            fills = ex.advance(1)
            self.assertEqual(len(fills), 1)
            self.assertAlmostEqual(fills[0].price, 0.40)     # NO price = 1 - yes price
            self.assertEqual(fills[0].liquidity, "maker")
            self.assertAlmostEqual(fills[0].fee, 0.02)
            ex.cancel(o.id, 2)
            await asyncio.sleep(0.01)
            self.assertEqual(rest.cancelled, [o.exchange_id])
            self.assertEqual(o.status, OrderStatus.CANCELLED)
            core.on_data_gap("test", 3)
            await asyncio.sleep(0.01)
            self.assertEqual(rest.cancel_alls, 0)                  # never account-wide
            self.assertIn("OLD1", rest.cancelled)                  # sweeps the bot's own leftovers
            self.assertNotIn("MINE", rest.cancelled)               # never your manual orders
            self.assertNotIn("OTHER", rest.cancelled)              # never outside the bot's series
        asyncio.run(go())

    def test_auth_error_trips_kill(self):
        async def go():
            core, ex = self._core(FakeRest(fail_status=401))
            ex.submit(Order(market=TK, outcome=UP, side=BUY, price=0.4, size=5), core.markets[TK].info, 0)
            await asyncio.sleep(0.01)
            self.assertTrue(core.risk.killed)
        asyncio.run(go())


class SetupTests(unittest.TestCase):
    def test_key_saved_locked_and_env_written(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from kbot import setup_wizard as sw
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            with open("dl.key", "wb") as fh:
                fh.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                         serialization.NoEncryption()))
            got = sw.collect_key("demo", "a952bcbe-ec3b-4b5b-b8f9-11dae589608c", "dl.key", interactive=False)
            self.assertIsNotNone(got)
            self.assertEqual(stat.S_IMODE(os.stat(got[1]).st_mode), 0o600)
            sw.write_env({"KALSHI_DEMO_KEY_ID": got[0], "KALSHI_DEMO_KEY_PATH": got[1]})
            self.assertEqual(stat.S_IMODE(os.stat(".env").st_mode), 0o600)
            self.assertIn("KALSHI_DEMO_KEY_ID=a952", open(".env").read())
            sw.ensure_gitignore()
            self.assertIn("keys/", open(".gitignore").read())
            with open("bad.key", "w") as fh:
                fh.write("not a key")
            self.assertIsNone(sw.collect_key("demo", "abc", "bad.key", interactive=False))
        finally:
            os.chdir(cwd)


class SettingsTests(unittest.TestCase):
    def test_apply_and_validate(self):
        from kbot.dashboard import apply_settings, get_settings
        core = TradingCore(load_config(None, overlay=None), NullStore(), "t")
        path = os.path.join(tempfile.mkdtemp(), "settings.yaml")
        apply_settings(core, {"strategy.target_pair_cost": 0.93, "risk.max_order_usd": 25,
                              "strategy.cutoff_s.900": 45, "markets.assets": "btc"}, overlay_path=path)
        self.assertEqual(core.strategy.cfg.target_pair_cost, 0.93)       # live objects updated
        self.assertEqual(core.risk.cfg.max_order_usd, 25)
        self.assertEqual(core.cfg.strategy.cutoff_for(900), 45)
        cfg2 = load_config(None, overlay=path)
        self.assertEqual(cfg2.strategy.target_pair_cost, 0.93)
        self.assertEqual(cfg2.markets.assets, ["btc"])
        self.assertEqual(get_settings(cfg2)["strategy.cutoff_s.900"], 45)
        with self.assertRaises(ValueError):
            apply_settings(core, {"strategy.target_pair_cost": 1.5}, overlay_path=path)
        with self.assertRaises(ValueError):
            apply_settings(core, {"kalshi.prod_rest": "x"}, overlay_path=path)

    def test_assets_setting_allows_any_configured_ticker(self):
        """sol (and any future asset added to kalshi.series) should be selectable, and an asset
        Kalshi doesn't actually have a series for should still be rejected."""
        from kbot.dashboard import apply_settings, get_settings
        core = TradingCore(load_config(None, overlay=None), NullStore(), "t")
        self.assertIn("sol", core.cfg.kalshi.series)                 # shipped with sol wired in
        path = os.path.join(tempfile.mkdtemp(), "settings.yaml")
        apply_settings(core, {"markets.assets": "btc,sol"}, overlay_path=path)
        self.assertEqual(core.cfg.markets.assets, ["btc", "sol"])
        self.assertEqual(get_settings(core.cfg)["_available_assets"], sorted(core.cfg.kalshi.series))
        with self.assertRaises(ValueError):
            apply_settings(core, {"markets.assets": "dogecoin"}, overlay_path=path)


class MultiAssetSpotTests(unittest.TestCase):
    def test_index_asset_uses_config_map_first(self):
        from kbot.feeds.spot import index_asset, parse_index
        # a brand-new asset with a non-standard index name resolves only via the config map
        self.assertEqual(index_asset("WEIRD_IDX", {"WEIRD_IDX": "zzz"}), "zzz")
        # the built-in table still works without any config map
        self.assertEqual(index_asset("SOLUSD_RTI"), "sol")
        # and an asset that's in neither table but follows the "<ASSET>USD..." convention still
        # resolves generically, so adding a new config entry is enough even before this file
        # ships an explicit row for it
        self.assertEqual(index_asset("XLMUSD_RTI"), "xlm")
        p = parse_index({"msg": {"index_id": "SOLUSD_RTI", "data": '{"value": "210.5"}'}})
        self.assertEqual(p, ("sol", 210.5))
        # config-provided map takes priority even when the built-in table also has an entry
        p2 = parse_index({"msg": {"index_id": "SOLUSD_RTI", "data": '{"value": "210.5"}'}}, {"SOLUSD_RTI": "solana"})
        self.assertEqual(p2, ("solana", 210.5))


if __name__ == "__main__":
    unittest.main()
