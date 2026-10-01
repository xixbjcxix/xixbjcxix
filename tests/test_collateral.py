"""Exchange-shard collateral: detection, the transfer offer, and the transfer request body."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kbot.collateral as col  # noqa: E402
import kbot.feeds.kalshi_rest as kr  # noqa: E402
from kbot.config import load_config  # noqa: E402


class FakeRest:
    last = None

    def __init__(self, base, key_id=None, key_path=None, **kw):
        self.bal = {0: 300.0, 2: 0.0}
        self.transfers = []
        FakeRest.last = self

    async def get_markets(self, **kw):
        return [{"ticker": "KXBTC15M-X", "exchange_index": 2}]

    async def get_shard_balances(self):
        return dict(self.bal)

    async def transfer_between_shards(self, dollars, dest_shard, source_shard=0):
        self.transfers.append((dollars, dest_shard, source_shard))
        self.bal[0] -= dollars
        self.bal[dest_shard] += dollars
        return {"transfer_id": "T1"}

    async def close(self):
        pass


class CollateralTests(unittest.TestCase):
    def setUp(self):
        self._orig = kr.KalshiRest
        kr.KalshiRest = FakeRest
        col.credentials = lambda cfg, env: {"key_id": "k", "key_path": "p"}
        col._sleep = self._nosleep

    async def _nosleep(self, *_):
        return None

    def tearDown(self):
        kr.KalshiRest = self._orig

    def test_offers_and_moves_only_with_yes(self):
        cfg = load_config(None, overlay=None)
        shard, have, msg = asyncio.run(col.prepare(cfg, "prod", 60.0, ask=lambda q: "n"))
        self.assertEqual((shard, have, msg), (2, 0.0, "transfer declined"))
        self.assertEqual(FakeRest.last.transfers, [])
        shard, have, msg = asyncio.run(col.prepare(cfg, "prod", 60.0, ask=lambda q: "y"))
        self.assertEqual(msg, "ok")
        self.assertEqual(FakeRest.last.transfers, [(60.0, 2, 0)])
        self.assertAlmostEqual(have, 60.0)

    def test_transfer_body_uses_centicents(self):
        captured = {}

        class R(self._orig):
            def __init__(self):
                pass

            async def request(self, method, path, **kw):
                captured.update(method=method, path=path, body=kw.get("json"))
                return {"transfer_id": "x"}
        asyncio.run(R().transfer_between_shards(12.34, dest_shard=2))
        self.assertEqual(captured["path"], "/portfolio/intra_exchange_instance_transfer")
        self.assertEqual(captured["body"]["amount"], 123400)
        self.assertEqual(captured["body"]["destination_exchange_shard"], 2)
        self.assertEqual(captured["body"]["source"], "event_contract")


if __name__ == "__main__":
    unittest.main()
