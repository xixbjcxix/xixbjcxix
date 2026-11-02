import base64
import math

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from adaptbot.adaptive import Selector, ShadowBook, Stats, sim_fill
from adaptbot.fees import order_fee
from adaptbot.kalshi import KalshiClient, parse_market, parse_order, parse_orderbook
from adaptbot.market import Book, MarketMeta, Position, View
from adaptbot.model import Calibrator, VolEstimator, fair_yes
from adaptbot.risk import Risk
from adaptbot.config import Settings
from adaptbot.strategy import Intent, Profile, Sizing, decide


# ------------------------------------------------------------------ fees / model
def test_fee_rounds_up_to_cent():
    assert order_fee(0.50, 1, 0.07) == 0.02          # 0.0175 -> 0.02
    assert order_fee(0.50, 100, 0.07) == 1.75
    assert order_fee(0.0, 5, 0.07) == 0.0


def test_fair_yes_basics():
    s = 1e-4
    assert fair_yes(100, 100, 600, s, 60) == pytest.approx(0.5, abs=1e-9)
    assert fair_yes(101, 100, 600, s, 60) > 0.5
    assert fair_yes(99, 100, 600, s, 60) < 0.5
    # more time -> less certain
    assert fair_yes(100.2, 100, 600, s, 60) < fair_yes(100.2, 100, 120, s, 60)


def test_fair_yes_inside_window_locks_in():
    # 50 of 60 window seconds averaged well above strike; spot just dipped below
    p = fair_yes(99.9, 100, 10, 1e-4, 60, window_avg=100.5, window_elapsed=50)
    assert p > 0.99


def test_vol_estimator_tracks_scale():
    import random
    rng = random.Random(0)
    v = VolEstimator(fast_hl=200, slow_hl=2000, floor=1e-6, prior=1e-5)
    px = 100.0
    for i in range(6000):
        px *= math.exp(2e-4 * rng.gauss(0, 1))
        v.update(float(i), px)
    assert 1.5e-4 < v.sigma < 2.6e-4


def test_calibrator_shrinks_overconfident_model():
    c = Calibrator(lr=0.2, l2=0.0)
    # model says 0.9 but it only happens half the time
    for i in range(400):
        c.learn([0.9, 0.9], i % 2)
    assert c.apply(0.9) < 0.75


# ------------------------------------------------------------------ book / position
def test_book_derives_asks_from_other_side():
    b = Book(yes_bids=[(0.40, 10)], no_bids=[(0.55, 7)])
    assert b.best_ask("yes") == pytest.approx(0.45)
    assert b.best_ask("no") == pytest.approx(0.60)
    assert b.ask_qty("yes") == 7
    assert b.spread() == pytest.approx(0.05)


def test_position_accounting():
    p = Position("T")
    p.apply_buy("yes", 10, 0.40, 0.17)
    assert p.cost == pytest.approx(4.17)
    pnl = p.apply_sell(4, 0.50, 0.07)
    assert pnl == pytest.approx(4 * 0.5 - 0.07 - 4 * 0.417)
    assert p.settle("yes") == pytest.approx(6 - 6 * 0.417)
    assert p.qty == 0 and p.side == ""


# ------------------------------------------------------------------ strategy
def mkview(ts, p_yes, yes_bid, no_bid, open_ts=0.0, close_ts=900.0):
    meta = MarketMeta("T", "btc", open_ts, close_ts, 100.0)
    book = Book([(yes_bid, 50)], [(no_bid, 50)], ts)
    return View(ts, meta, book, 100.0, ts, 1e-4, p_yes, p_yes)


SIZ = Sizing(bankroll=40, kelly_fraction=0.25, max_contracts=10, max_dollars_market=12, fee_rate=0.07)


def test_entry_requires_edge_window_and_confirmation():
    prof = Profile("x", entry_start_s=30, entry_end_s=300, min_edge=0.06, confirm_s=3)
    pos, memo = Position("T"), {}
    # yes ask 0.50, model 0.65 -> edge ~0.13
    assert decide(mkview(10, 0.65, 0.48, 0.50), pos, prof, SIZ, memo) is None      # too early
    assert decide(mkview(40, 0.65, 0.48, 0.50), pos, prof, SIZ, memo) is None      # starts confirmation
    it = decide(mkview(44, 0.65, 0.48, 0.50), pos, prof, SIZ, memo)
    assert it is not None and it.action == "buy" and it.side == "yes" and it.qty >= 1
    assert it.limit <= 0.65 - 0.03 + 1e-9
    assert decide(mkview(400, 0.65, 0.48, 0.50), Position("T"), prof, SIZ, {}) is None  # too late


def test_no_entry_without_edge():
    prof = Profile("x", confirm_s=0)
    assert decide(mkview(100, 0.52, 0.48, 0.50), Position("T"), prof, SIZ, {}) is None


def test_buys_no_side_when_model_is_low():
    prof = Profile("x", confirm_s=0)
    it = decide(mkview(100, 0.30, 0.48, 0.50), Position("T"), prof, SIZ, {})
    assert it.side == "no"


def test_early_cut_take_profit_and_late_exit():
    prof = Profile("x", cut_loss=0.10, tp_margin=0.03, exit_before_close_s=90, hold_conf=0.85)
    pos = Position("T")
    pos.apply_buy("yes", 5, 0.50, 0.09, "x")
    cut = decide(mkview(200, 0.38, 0.36, 0.62), pos, prof, SIZ, {})
    assert cut.action == "sell" and cut.reason == "early_cut"
    tp = decide(mkview(200, 0.60, 0.70, 0.28), pos, prof, SIZ, {})
    assert tp.reason == "take_profit"
    late = decide(mkview(850, 0.70, 0.68, 0.30), pos, prof, SIZ, {})
    assert late.reason == "late_exit"
    assert decide(mkview(850, 0.95, 0.90, 0.08), pos, prof, SIZ, {}) is None        # confident: hold


def test_sizing_respects_caps():
    prof = Profile("x", confirm_s=0, max_price=0.99)
    big = Sizing(bankroll=10_000, kelly_fraction=1, max_contracts=7, max_dollars_market=12, fee_rate=0.07)
    it = decide(mkview(100, 0.95, 0.48, 0.50), Position("T"), prof, big, {})
    assert it.qty == 7


# ------------------------------------------------------------------ adaptive
def test_sim_fill_walks_levels_and_respects_limit():
    b = Book([(0.40, 5)], [(0.55, 3), (0.54, 10)])   # yes asks 0.45 x3, 0.46 x10
    q, avg, fee = sim_fill(b, Intent("buy", "yes", 8, 0.46, ""), 0.07)
    assert q == 8 and avg == pytest.approx((3 * 0.45 + 5 * 0.46) / 8)
    q, _, _ = sim_fill(b, Intent("buy", "yes", 8, 0.45, ""), 0.07)
    assert q == 3


def test_selector_warmup_then_best_then_sit_out():
    profs = [Profile("a"), Profile("b")]
    sb = ShadowBook(profs, 0.07, 0.0, 1.0, 30)
    sel = Selector(default="a", min_markets=5, min_trades=3)
    assert sel.choose(sb) == ("a", True)
    for _ in range(10):
        sb.stats["a"].add(-0.2, True)
        sb.stats["b"].add(0.5, True)
    assert sel.choose(sb) == ("b", False)
    for _ in range(60):
        sb.stats["a"].add(-0.2, True)
        sb.stats["b"].add(-0.3, True)
    name, _ = sel.choose(sb)
    assert name is None


def test_stats_lcb_penalises_noise():
    steady, noisy = Stats(), Stats()
    for i in range(20):
        steady.add(0.2, True)
        noisy.add(2.2 if i % 2 else -1.8, True)
    assert steady.score(0.5) > noisy.score(0.5)


# ------------------------------------------------------------------ kalshi parsing
def test_parse_orderbook_cents_and_dollars():
    b1 = parse_orderbook({"orderbook": {"yes": [[40, 10], [42, 5]], "no": [[55, 3]]}})
    assert b1.best_bid("yes") == 0.42 and b1.best_ask("yes") == pytest.approx(0.45)
    b2 = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.4200", "5.00"]], "no_dollars": [["0.5500", "3"]]}})
    assert b2.best_bid("yes") == 0.42 and b2.ask_qty("yes") == 3
    b3 = parse_orderbook({"orderbook": {"yes": None, "no": None}})
    assert b3.best_bid("yes") is None


def test_parse_market_and_order():
    m = parse_market({"ticker": "KXBTC15M-X", "open_time": "2026-10-02T15:00:00Z",
                      "close_time": "2026-10-02T15:15:00Z", "floor_strike": 85666.06,
                      "strike_type": "greater", "status": "active", "result": ""}, "btc")
    assert m.strike == 85666.06 and m.close_ts - m.open_ts == 900
    o = parse_order({"order_id": "1", "status": "executed", "fill_count_fp": "5.00", "remaining_count": 0,
                     "taker_fill_cost_dollars": "2.4500", "taker_fees_dollars": "0.09"})
    assert o["filled"] == 5 and o["cost"] == pytest.approx(2.45) and o["fees"] == pytest.approx(0.09)


def test_request_signature_verifies(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    kp = tmp_path / "k.pem"
    kp.write_bytes(pem)
    c = KalshiClient("https://api.elections.kalshi.com/trade-api/v2", "kid", str(kp))
    h = c._headers("GET", "/portfolio/balance")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/balance").encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


# ------------------------------------------------------------------ risk
def test_risk_limits_and_daily_stop():
    s = Settings(max_dollars_per_market=5, max_dollars_total=8, max_daily_loss=3, max_contracts_per_order=10)
    r = Risk(s)
    r.kill_file = "/nonexistent/KILL"
    pos = Position("T")
    assert r.check(Intent("buy", "yes", 5, 0.50, ""), pos, {"T": pos}) == ""
    assert r.check(Intent("buy", "yes", 10, 0.50, ""), pos, {"T": pos}) == "max_dollars_per_market"
    assert r.check(Intent("buy", "yes", 11, 0.10, ""), pos, {"T": pos}).startswith("qty")
    r.update_equity(50)
    r.update_equity(46.9)
    assert r.halted.startswith("daily loss") and r.flatten
    assert r.check(Intent("buy", "yes", 1, 0.5, ""), pos, {"T": pos})        # entries blocked
    pos.apply_buy("yes", 2, 0.5, 0.0)
    assert r.check(Intent("sell", "yes", 2, 0.4, ""), pos, {"T": pos}) == ""  # exits still allowed
