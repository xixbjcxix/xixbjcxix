"""End-to-end wiring of the live engine (demo mode) with fake Kalshi REST + websocket.
Exercises: discovery -> subscribe -> books + index -> strategy -> real-order path (fake REST)
-> fills from the private channel -> inventory/pairs -> data gap -> cancel-all -> SQLite."""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kbot.engine as engine  # noqa: E402
from kbot.config import load_config, with_overrides  # noqa: E402

T0 = time.time()
TK = "KXBTC15M-TEST-00"


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class FakeRest:
    instances = []

    def __init__(self, base, key_id=None, key_path=None, **kw):
        self.base = base
        self.created = []
        self.cancel_alls = 0
        self.net = 0.0
        FakeRest.instances.append(self)

    async def close(self):
        pass

    async def get_series(self, st):
        return {"ticker": st, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}

    async def get_markets(self, series_ticker, status, limit=50):
        if status != "open" or not series_ticker.startswith("KXBTC"):
            return []
        return [{"ticker": TK, "event_ticker": "KXBTC15M-TEST", "open_time": iso(T0 - 60), "close_time": iso(T0 + 600),
                 "floor_strike": 78000.0, "strike_type": "greater_or_equal"}]

    async def get_market(self, t):
        return {"ticker": t, "result": ""}

    async def create_order(self, **kw):
        self.created.append(kw)
        oid = f"X{len(self.created)}"
        FakeWS.instance.pending.append((oid, kw))
        return {"order_id": oid, "fill_count": "0.00", "remaining_count": f"{kw['count']:.2f}"}

    async def cancel_order(self, oid, ticker=None):
        return {}

    async def cancel_all(self):
        self.cancel_alls += 1
        return {}

    async def get_fills(self, min_ts=None):
        return []

    async def get_balance(self):
        return {"balance": 100000}

    async def get_shard_balances(self):
        return {0: 900.0, 2: 100.0}

    async def get_open_orders(self):
        return []

    async def get_positions(self):
        return [{"ticker": TK, "position_fp": f"{self.net:.2f}"}]


class FakeWS:
    instance = None

    def __init__(self, url, key_id, key_path, on_frame, on_gap, index_ids=(), private_channels=False, stale_s=20):
        self.on_frame, self.on_gap = on_frame, on_gap
        self.tickers = set()
        self.pending = []
        FakeWS.instance = self

    async def subscribe_markets(self, tickers):
        self.tickers |= set(tickers)

    async def run(self, stop):
        while not self.tickers and not stop.is_set():
            await asyncio.sleep(0.02)
        now = lambda: int(time.time() * 1000)  # noqa: E731
        snap = {"type": "orderbook_snapshot", "msg": {"market_ticker": TK,
                "yes_dollars_fp": [["0.4700", "100.00"], ["0.4800", "50.00"]],
                "no_dollars_fp": [["0.4700", "100.00"], ["0.4800", "50.00"]]}}
        self.on_frame(snap, now(), "{}")
        i = 0
        while not stop.is_set():
            await asyncio.sleep(0.05)
            i += 1
            self.on_frame({"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI",
                                                                 "data": '{"value": "78000.0"}'}}, now(), "{}")
            # every resting order gets filled shortly after it's placed (both sides -> pairs)
            while self.pending:
                oid, kw = self.pending.pop(0)
                if kw.get("client_order_id", "").startswith("kbot-smoke"):
                    continue
                yes_px = kw["price"] if kw["outcome_yes"] else round(1 - kw["price"], 4)
                rest = FakeRest.instances[-1]
                rest.net += kw["count"] if kw["outcome_yes"] == kw["buy"] else -kw["count"]
                self.on_frame({"type": "fill", "msg": {"trade_id": f"T{oid}", "order_id": oid,
                                                      "yes_price_dollars": f"{yes_px:.4f}",
                                                      "count_fp": f"{kw['count']:.2f}", "fee_cost": "0.01",
                                                      "is_taker": not kw["post_only"]}}, now(), "{}")
            if i == 30:
                self.on_gap("test_disconnect")
                await asyncio.sleep(0.2)
                self.on_frame(snap, now(), "{}")


async def no_spot(*a, **k):
    await asyncio.sleep(3600)


class EngineWiringTest(unittest.TestCase):
    def test_demo_engine_end_to_end(self):
        engine.KalshiRest = FakeRest
        engine.KalshiWS = FakeWS
        engine.run_exchange_feed = no_spot
        engine.credentials = lambda cfg, env: {"key_id": "k", "key_path": "p"}
        d = tempfile.mkdtemp()
        cfg = with_overrides(load_config(None, overlay=None), **{
            "db_path": os.path.join(d, "bot.db"), "recording_db": os.path.join(d, "rec.db"),
            "dashboard.enabled": False, "risk.kill_file": "", "kalshi.discovery_interval_s": 1,
            "signal.lookback_s": 1, "engine.requote_stale_ms": 300, "markets.assets": ["btc"]})

        async def go():
            eng = engine.Engine(cfg, "demo")
            await eng.run(hours=4.0 / 3600)
            return eng
        eng = asyncio.run(go())
        core = eng.core
        rest = FakeRest.instances[-1]
        self.assertIn(TK, core.markets)
        self.assertGreater(len(rest.created), 1)
        self.assertTrue(all(kw["post_only"] for kw in rest.created if kw["buy"]))
        inv = core.markets[TK].inv
        self.assertGreater(inv.paired, 0)
        self.assertGreater(inv.fees, 0)
        self.assertTrue(any(kw["client_order_id"].startswith("kbot-smoke") for kw in rest.created))  # 1c test order
        self.assertEqual(rest.cancel_alls, 0)                 # never the account-wide cancel-all
        self.assertTrue(any("test_disconnect" in g[1] for g in core.gaps))
        self.assertFalse(core.risk.killed, core.risk.kill_reason)
        con = sqlite3.connect(cfg.db_path)
        self.assertGreater(con.execute("select count(*) from fills").fetchone()[0], 0)
        rec = sqlite3.connect(cfg.recording_db)
        self.assertGreater(rec.execute("select count(*) from raw_messages where source='spot'").fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()


class StartupFailureTest(unittest.TestCase):
    def test_dashboard_stays_up_when_key_rejected(self):
        from kbot.feeds.kalshi_rest import KalshiError

        class BadKeyRest(FakeRest):
            async def get_balance(self):
                raise KalshiError(401, '{"error":"unauthorized"}')

        engine.KalshiRest = BadKeyRest
        engine.KalshiWS = FakeWS
        engine.run_exchange_feed = no_spot
        engine.credentials = lambda cfg, env: {"key_id": "k", "key_path": "p"}
        d = tempfile.mkdtemp()
        cfg = with_overrides(load_config(None, overlay=None), **{
            "db_path": os.path.join(d, "bot.db"), "recording_db": os.path.join(d, "rec.db"),
            "dashboard.port": 18787, "dashboard.open_browser": False, "risk.kill_file": "",
            "markets.assets": ["btc"]})
        seen = {}

        async def go():
            eng = engine.Engine(cfg, "demo")
            task = asyncio.create_task(eng.run(hours=2.0 / 3600))
            await asyncio.sleep(0.8)
            import json as _j
            for port in range(18787, 18800):       # the engine may have moved to a free port
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    continue
                w.write(b"GET /api/state HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
                await w.drain()
                body = (await r.read()).decode().split("\r\n\r\n", 1)[1]
                w.close()
                seen.update(_j.loads(body))
                break
            await task
        asyncio.run(go())
        self.assertIn("rejected", seen.get("status_message", ""))
        self.assertTrue(seen.get("killed"))


class LiveModeTest(unittest.TestCase):
    def _run(self, rest_cls):
        engine.KalshiRest = rest_cls
        engine.KalshiWS = FakeWS
        engine.run_exchange_feed = no_spot
        engine.credentials = lambda cfg, env: {"key_id": "k", "key_path": "p"} if env == "prod" else None
        d = tempfile.mkdtemp()
        cfg = with_overrides(load_config(None, overlay=None), **{
            "db_path": os.path.join(d, "bot.db"), "recording_db": os.path.join(d, "rec.db"),
            "dashboard.enabled": False, "risk.kill_file": "", "markets.assets": ["btc"],
            "risk.max_order_usd": 100.0, "strategy.clip_shares": 50.0})

        async def go():
            eng = engine.Engine(cfg, "live")
            await eng.run(hours=2.5 / 3600)
            return eng
        return asyncio.run(go())

    def test_live_uses_prod_starter_limits_and_trades(self):
        class ProdRest(FakeRest):
            pass
        eng = self._run(ProdRest)
        rest = FakeRest.instances[-1]
        self.assertIn("kalshi.com", rest.base)
        self.assertNotIn("demo", rest.base)
        self.assertEqual(eng.cfg.risk.max_order_usd, 5.0)          # starter limit wins over 100
        self.assertEqual(eng.cfg.strategy.clip_shares, 5.0)
        base_orders = [kw for kw in rest.created if not kw["client_order_id"].startswith("kbot-smoke")]
        self.assertTrue(base_orders)
        self.assertTrue(all(kw["count"] <= 5.0 for kw in base_orders))
        self.assertFalse(eng.core.risk.killed, eng.core.risk.kill_reason)

    def test_existing_position_market_is_left_alone(self):
        class HeldRest(FakeRest):
            async def get_positions(self):
                return [{"ticker": TK, "position_fp": "3.00"}]
        eng = self._run(HeldRest)
        rest = FakeRest.instances[-1]
        self.assertIn(TK, eng.core.blocked_markets)
        self.assertEqual([kw for kw in rest.created if not kw["client_order_id"].startswith("kbot-smoke")], [])
