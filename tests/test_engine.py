import asyncio
import time as real_time

import pytest

import adaptbot.engine as engine_mod
from adaptbot.brain import Brain
from adaptbot.broker import PaperBroker
from adaptbot.config import Settings
from adaptbot.engine import Engine
from adaptbot.market import Book
from adaptbot.recorder import Recorder
from adaptbot.strategy import Profile

T0 = 1_800_000_000.0
TICKER = "KXBTC15M-TEST"


class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def localtime(self, x=None):
        return real_time.localtime(x)

    def strftime(self, fmt, t=None):
        return real_time.strftime(fmt, t) if t else real_time.strftime(fmt)


class FakeClient:
    def __init__(self, clock):
        self.clock = clock
        self.result = ""
        self.yes_bid, self.no_bid = 0.48, 0.50

    def _m(self):
        iso = lambda t: real_time.strftime("%Y-%m-%dT%H:%M:%SZ", real_time.gmtime(t))
        return {"ticker": TICKER, "open_time": iso(T0 - 100), "close_time": iso(T0 + 800),
                "floor_strike": 100.0, "strike_type": "greater", "status": "active", "result": self.result}

    async def markets(self, series, status=None, limit=50):
        return [self._m()] if self.clock.t < T0 + 800 else []

    async def market(self, t):
        return self._m()

    async def orderbook(self, t, depth=10):
        return Book([(self.yes_bid, 50)], [(self.no_bid, 50)], self.clock.t)


class FakeSpot:
    def __init__(self, px):
        self.px = px

    def price(self, asset, now=None):
        return self.px, 3

    def status(self):
        return {}


def settings():
    s = Settings(adaptive=False, default_profile="t", record=False,
                 profiles=[Profile("t", entry_start_s=30, entry_end_s=600, min_edge=0.05, confirm_s=0,
                                   cut_loss=None, exit_before_close_s=0).to_dict()])
    return s


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock(T0)
    monkeypatch.setattr(engine_mod, "time", c)
    monkeypatch.setattr("adaptbot.risk.DATA_DIR", str(tmp_path))
    return c


def make_engine(clock, broker, spot_px=100.4):
    s = settings()
    eng = Engine(s, FakeClient(clock), broker, FakeSpot(spot_px), Brain(s, state_file=""),
                 Recorder(enabled=False))
    eng.risk.kill_file = "/nonexistent/KILL"
    return eng


def test_paper_buy_then_settle(clock):
    broker = PaperBroker(40.0, 0.07, 0.0, 1.0)
    eng = make_engine(clock, broker)
    asyncio.run(eng.tick())
    pos = eng.positions[TICKER]
    assert pos.side == "yes" and pos.qty >= 1
    cash_after_buy = broker._cash
    assert cash_after_buy < 40.0
    # market closes, Kalshi publishes YES
    clock.t = T0 + 820
    eng.c.result = "yes"
    asyncio.run(eng.tick())
    assert TICKER not in eng.positions
    assert broker._cash > 40.0
    assert eng.market_log[-1]["pnl"] > 0
    assert eng.realized_today == pytest.approx(broker._cash - 40.0, abs=1e-6)


def test_no_trade_on_stale_spot(clock):
    eng = make_engine(clock, PaperBroker(40.0, 0.07, 0.0, 1.0), spot_px=None)
    asyncio.run(eng.tick())
    assert eng.positions.get(TICKER) is None or eng.positions[TICKER].qty == 0


class FakeLiveBroker:
    live = True

    def __init__(self, ex_positions):
        self.ex = ex_positions

    async def execute(self, *a):
        return 0.0, 0.0, 0.0, False

    async def cash(self):
        return 50.0

    async def positions(self):
        return dict(self.ex)

    def settle(self, *a):
        pass


def test_mismatch_halts_entries_and_adopts_position(clock):
    eng = make_engine(clock, FakeLiveBroker({TICKER: -5.0}), spot_px=100.0)
    eng.trade = False
    asyncio.run(eng.tick())                 # first mismatch: warning only
    assert not eng.risk.halted
    clock.t += 6
    asyncio.run(eng.tick())                 # second: halt + adopt
    assert "mismatch" in eng.risk.halted
    pos = eng.positions[TICKER]
    assert pos.side == "no" and pos.qty == 5


def test_daily_loss_from_exchange_equity_triggers_flatten(clock):
    b = FakeLiveBroker({})
    eng = make_engine(clock, b, spot_px=100.0)
    asyncio.run(eng.tick())
    assert eng.risk.start_equity == 50.0

    async def lower():
        return 40.0
    b.cash = lower
    clock.t += 6
    asyncio.run(eng.tick())
    assert eng.risk.halted.startswith("daily loss") and eng.risk.flatten
