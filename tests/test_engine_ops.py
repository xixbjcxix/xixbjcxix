"""Engine-level ops behaviour: crash surfacing (for the supervisor), alerts, supervised restart."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kbot.engine as engine  # noqa: E402
from kbot.config import load_config, with_overrides  # noqa: E402
from kbot.ops import supervise  # noqa: E402

import test_engine_wiring as w  # noqa: E402


class CrashingWS(w.FakeWS):
    """Market-data task that blows up after a moment (e.g. an unexpected parser error)."""
    crashes_left = 1

    async def run(self, stop):
        await asyncio.sleep(0.2)
        if CrashingWS.crashes_left > 0:
            CrashingWS.crashes_left -= 1
            raise ValueError("unexpected frame")
        await super().run(stop)


def make_cfg():
    d = tempfile.mkdtemp()
    return with_overrides(load_config(None, overlay=None), **{
        "db_path": os.path.join(d, "bot.db"), "recording_db": os.path.join(d, "rec.db"),
        "dashboard.enabled": False, "risk.kill_file": "", "kalshi.discovery_interval_s": 1,
        "signal.lookback_s": 1, "markets.assets": ["btc"]})


def patch(ws):
    engine.KalshiRest = w.FakeRest
    engine.KalshiWS = ws
    engine.run_exchange_feed = w.no_spot
    engine.credentials = lambda cfg, env: {"key_id": "k", "key_path": "p"}


class EngineOpsTest(unittest.TestCase):
    def test_background_crash_cancels_orders_and_raises(self):
        patch(CrashingWS)
        CrashingWS.crashes_left = 1
        cfg = make_cfg()
        eng = engine.Engine(cfg, "paper")

        async def go():
            await eng.run(hours=30 / 3600)
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(go())
        self.assertIn("unexpected frame", str(cm.exception))
        self.assertIsInstance(eng.crashed, ValueError)

    def test_supervisor_restarts_engine_after_crash(self):
        patch(CrashingWS)
        CrashingWS.crashes_left = 1
        cfg = make_cfg()
        attempts = []

        async def once(attempt):
            attempts.append(attempt)
            eng = engine.Engine(cfg, "paper")
            eng.core.restarts = attempt
            await eng.run(hours=1.0 / 3600)

        async def nosleep(_):
            pass
        notes = []
        n = asyncio.run(supervise(once, max_restarts=3, notify=lambda k, t: notes.append(k), sleep=nosleep))
        self.assertEqual(n, 1)
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(notes, ["engine_restart"])

    def test_clean_run_does_not_raise_and_sets_uptime(self):
        patch(w.FakeWS)
        eng = engine.Engine(make_cfg(), "paper")
        asyncio.run(eng.run(hours=1.0 / 3600))
        self.assertIsNone(eng.crashed)
        self.assertGreater(eng.core.started_ms, 0)


if __name__ == "__main__":
    unittest.main()
