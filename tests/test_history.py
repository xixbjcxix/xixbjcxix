"""Account-history import: fill parsing, fee calibration, market rebuild, incremental pull."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kbot import history as H  # noqa: E402
from kbot.fees import fee_model_for  # noqa: E402
from kbot.models import BUY, DOWN, SELL, UP  # noqa: E402

TK = "KXBTC15M-26OCT011200-00"


def raw(tid, side, price, n, taker=False, fee="0.00", action="buy", t="2026-10-01T12:03:00Z", ticker=TK, key=None):
    r = {"trade_id": tid, "order_id": "o" + tid, "ticker": ticker, "side": side, "action": action,
         "count_fp": f"{n:.2f}", "is_taker": taker, "fee_cost": fee, "created_time": t}
    r[key or f"{side}_price_dollars"] = f"{price:.4f}"
    return r


class ParseTests(unittest.TestCase):
    def test_field_variants(self):
        a = H.parse_fill(raw("1", "yes", 0.47, 10))
        self.assertEqual((a.outcome, a.side, a.price, a.size, a.liquidity), (UP, BUY, 0.47, 10, "maker"))
        b = H.parse_fill(raw("2", "no", 0.44, 5, taker=True, action="sell"))
        self.assertEqual((b.outcome, b.side, b.liquidity), (DOWN, SELL, "taker"))
        legacy = {"trade_id": "3", "ticker": TK, "side": "yes", "action": "buy", "count": 4, "yes_price": 47,
                  "created_time": 1_790_000_000}
        c = H.parse_fill(legacy)
        self.assertAlmostEqual(c.price, 0.47)
        self.assertEqual(c.ts_ms, 1_790_000_000_000)
        # only the opposite side's price given -> complement
        d = H.parse_fill(raw("4", "no", 0.53, 5, key="yes_price_dollars"))
        self.assertAlmostEqual(d.price, 0.47)
        self.assertIsNone(H.parse_fill({"trade_id": "5", "side": "weird"}))
        self.assertIsNone(H.parse_fill(raw("6", "yes", 0.5, 0)))


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fee = fee_model_for(None)
        self.fee_for = lambda t: self.fee

    def fills(self):
        mk = self.fee.maker_fee(10, 0.47)
        return [H.parse_fill(x) for x in [
            raw("1", "yes", 0.47, 10, fee=f"{mk:.2f}", t="2026-10-01T12:01:00Z"),
            raw("2", "no", 0.45, 10, fee=f"{self.fee.maker_fee(10, 0.45):.2f}", t="2026-10-01T12:02:00Z"),
            raw("3", "yes", 0.40, 5, taker=True, fee="0.50", t="2026-10-01T12:04:00Z"),   # overcharged taker
        ]]

    def test_calibration(self):
        cal = H.fee_calibration(self.fills(), self.fee_for)
        self.assertEqual(cal["maker"]["fills"], 2)
        self.assertAlmostEqual(cal["maker"]["drift_pct"], 0.0, places=1)
        self.assertGreater(cal["taker"]["drift_pct"], 100)          # 0.50 vs ~0.09
        self.assertAlmostEqual(cal["maker"]["implied_coef"], self.fee.maker_coef, delta=0.004)

    def test_rebuild_and_settle(self):
        rows = H.rebuild_markets(self.fills(), {TK: "yes"}, self.fee_for)
        r = rows[0]
        self.assertEqual((r["yes"], r["no"], r["paired"], r["residual"]), (15, 10, 10, 5))
        # 10 pairs @ (0.47..avg yes 0.4467 + 0.45) -> locked; 5 residual YES wins -> positive
        self.assertGreater(r["paired_pnl"], 0)
        self.assertGreater(r["residual_pnl"], 0)
        lose = H.rebuild_markets(self.fills(), {TK: "no"}, self.fee_for)[0]
        self.assertLess(lose["residual_pnl"], 0)
        self.assertIsNone(H.rebuild_markets(self.fills(), {}, self.fee_for)[0]["net_pnl"])
        s = H.summarize(rows)
        self.assertEqual(s["settled"], 1)
        self.assertGreater(s["pair_edge_cents"], 0)


class FakeRest:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get_fills_all(self, min_ts=None, max_ts=None, max_rows=0):
        self.calls.append(min_ts)
        return self.pages

    async def get_market(self, t):
        return {"ticker": t, "result": "yes"}


class PullTests(unittest.TestCase):
    def test_incremental_dedup_and_results(self):
        with tempfile.TemporaryDirectory() as d:
            st = H.HistoryStore(os.path.join(d, "h.db"))
            pages = [raw("1", "yes", 0.47, 10), raw("2", "no", 0.45, 10, ticker="KXOTHER-1")]
            rest = FakeRest(pages)
            r1 = asyncio.run(H.pull(rest, st, ["KXBTC15M"], 30, 1_790_000_000))
            self.assertEqual((r1["downloaded"], r1["new"], r1["markets"], r1["results_fetched"]), (2, 2, 1, 1))
            self.assertEqual(st.results(), {TK: "yes"})
            r2 = asyncio.run(H.pull(rest, st, ["KXBTC15M"], 30, 1_790_000_000))
            self.assertEqual(r2["new"], 0)                       # same trade_ids ignored
            self.assertEqual(r2["results_fetched"], 0)           # result already known
            self.assertGreater(rest.calls[1], rest.calls[0] or 0)  # second pull starts from last fill, not 30 days back
            self.assertEqual(len(st.fills(["KXBTC15M"])), 1)     # series filter
            H.write_csv(H.rebuild_markets([H.parse_fill(x) for x in st.fills(["KXBTC15M"])], st.results(),
                                          lambda t: fee_model_for(None)), os.path.join(d, "o.csv"))
            self.assertTrue(os.path.exists(os.path.join(d, "o.csv")))
            st.close()


if __name__ == "__main__":
    unittest.main()
