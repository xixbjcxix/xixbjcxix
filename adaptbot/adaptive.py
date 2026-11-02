"""The adaptive layer.

Every profile trades every market in shadow (simulated taker fills against
the real book).  Because they all see the same data, their results are a
fair head-to-head comparison and exploring costs nothing.  The live engine
trades the profile with the best risk-adjusted recent record, and sits out
entirely when no profile is showing an edge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .fees import order_fee
from .market import Book, Position, View
from .strategy import Intent, Profile, Sizing, decide


def sim_fill(book: Book, it: Intent, fee_rate: float, slip: float = 0.0,
             fill_ratio: float = 1.0) -> tuple[float, float, float]:
    """Fill a taker order against the visible book.

    Returns (qty, avg_price, fee).  `slip` makes every fill that much worse,
    `fill_ratio` assumes we only get part of each level (latency, competition).
    """
    want = float(it.qty)
    got = cost = 0.0
    if it.action == "buy":
        levels = book.asks(it.side)
        for px, q in levels:
            px2 = round(px + slip, 4)
            if px2 > it.limit + 1e-9 or want <= 0:
                break
            take = min(want, math.floor(q * fill_ratio))
            got += take
            cost += take * px2
            want -= take
    else:
        levels = book.bids(it.side)
        for px, q in levels:
            px2 = round(px - slip, 4)
            if px2 < it.limit - 1e-9 or want <= 0:
                break
            take = min(want, math.floor(q * fill_ratio))
            got += take
            cost += take * px2
            want -= take
    if got <= 0:
        return 0.0, 0.0, 0.0
    avg = cost / got
    return got, avg, order_fee(avg, got, fee_rate)


def apply_fill(pos: Position, it: Intent, qty: float, avg: float, fee: float, profile: str) -> float:
    if qty <= 0:
        return 0.0
    if it.action == "buy":
        pos.apply_buy(it.side, qty, avg, fee, profile)
        pos.peak_p = max(pos.peak_p, it.p_side)
        return 0.0
    return pos.apply_sell(qty, avg, fee)


@dataclass
class Stats:
    """Exponentially weighted per-market P&L of one profile."""
    halflife: float = 30.0
    w: float = 0.0          # sum of weights
    m: float = 0.0          # weighted mean
    s2: float = 0.0         # weighted variance
    n: int = 0
    total: float = 0.0
    traded: int = 0
    wins: int = 0
    losses: int = 0
    recent: list = field(default_factory=list)

    def add(self, pnl: float, traded: bool) -> None:
        decay = 0.5 ** (1.0 / self.halflife)
        self.w = self.w * decay + 1.0
        a = 1.0 / self.w
        d = pnl - self.m
        self.m += a * d
        self.s2 = (1 - a) * (self.s2 + a * d * d)
        self.n += 1
        self.total += pnl
        if traded:
            self.traded += 1
            if pnl > 0:
                self.wins += 1
            elif pnl < 0:
                self.losses += 1
        self.recent = (self.recent + [round(pnl, 2)])[-20:]

    def score(self, k: float) -> float:
        """Lower confidence bound on the mean per-market P&L."""
        if self.n == 0:
            return 0.0
        n_eff = max(1.0, min(self.w, float(self.n)))
        return self.m - k * math.sqrt(max(self.s2, 0.0) / n_eff)

    def to_dict(self) -> dict:
        return dict(w=self.w, m=self.m, s2=self.s2, n=self.n, total=self.total,
                    traded=self.traded, wins=self.wins, losses=self.losses, recent=self.recent)

    def load(self, d: dict) -> None:
        for k, v in d.items():
            if hasattr(self, k):
                setattr(self, k, v)


class ShadowBook:
    """Runs every profile on every market against simulated fills."""

    def __init__(self, profiles: list[Profile], fee_rate: float, slip: float,
                 fill_ratio: float, halflife: float):
        self.profiles = {p.name: p for p in profiles}
        self.fee_rate, self.slip, self.fill_ratio = fee_rate, slip, fill_ratio
        self.pos: dict[str, dict[str, Position]] = {n: {} for n in self.profiles}
        self.memo: dict[str, dict[str, dict]] = {n: {} for n in self.profiles}
        self.stats: dict[str, Stats] = {n: Stats(halflife=halflife) for n in self.profiles}
        self.log: list[dict] = []

    def step(self, view: View, sizing: Sizing, allow_entry: bool = True) -> None:
        t = view.meta.ticker
        for name, prof in self.profiles.items():
            pos = self.pos[name].setdefault(t, Position(t))
            memo = self.memo[name].setdefault(t, {})
            it = decide(view, pos, prof, sizing, memo, allow_entry=allow_entry)
            if it is None:
                continue
            q, avg, fee = sim_fill(view.book, it, self.fee_rate, self.slip, self.fill_ratio)
            if q > 0:
                apply_fill(pos, it, q, avg, fee, name)

    def settle(self, ticker: str, result: str) -> dict[str, float]:
        out = {}
        for name in self.profiles:
            pos = self.pos[name].pop(ticker, None)
            self.memo[name].pop(ticker, None)
            if pos is None:
                pnl, traded = 0.0, False
            else:
                pos.settle(result)
                pnl, traded = pos.realized, pos.entries > 0
            self.stats[name].add(pnl, traded)
            out[name] = round(pnl, 4)
        self.log = (self.log + [{"ticker": ticker, "result": result, "pnl": out}])[-200:]
        return out

    def drop(self, ticker: str) -> None:
        """Forget a market without scoring it (e.g. no result available)."""
        for name in self.profiles:
            self.pos[name].pop(ticker, None)
            self.memo[name].pop(ticker, None)

    def to_dict(self) -> dict:
        return {"stats": {n: s.to_dict() for n, s in self.stats.items()}}

    def load(self, d: dict) -> None:
        for n, sd in d.get("stats", {}).items():
            if n in self.stats:
                self.stats[n].load(sd)


@dataclass
class Selector:
    """Chooses which profile trades real money."""
    default: str
    min_markets: int = 12
    k: float = 0.5               # LCB width
    switch_margin: float = 0.02  # $ per market better before switching
    sit_out: bool = True
    min_trades: int = 8          # a profile needs this many traded markets to be trusted
    current: str = ""
    reason: str = "warm-up"

    def choose(self, shadow: ShadowBook) -> tuple[str | None, bool]:
        """Returns (profile name or None to sit out, warm_up flag)."""
        if not self.current or self.current not in shadow.profiles:
            self.current = self.default if self.default in shadow.profiles else next(iter(shadow.profiles))
        n = min((s.n for s in shadow.stats.values()), default=0)
        if n < self.min_markets:
            self.reason = f"warm-up ({n}/{self.min_markets} markets scored)"
            return self.current, True
        scores = {name: (s.score(self.k) if s.traded >= self.min_trades else float("-inf"))
                  for name, s in shadow.stats.items()}
        best = max(scores, key=scores.get)
        if scores[best] == float("-inf"):
            self.reason = f"sitting out: no profile has {self.min_trades}+ traded markets yet"
            return (None, False) if self.sit_out else (self.current, True)
        if best != self.current and scores[best] > scores[self.current] + self.switch_margin:
            self.current = best
        if self.sit_out and scores[self.current] <= 0:
            self.reason = (f"sitting out: best profile {best} has no proven edge "
                           f"(score {scores[best]:+.3f}/market)")
            return None, False
        self.reason = f"trading {self.current} (score {scores[self.current]:+.3f}/market)"
        return self.current, False

    def to_dict(self) -> dict:
        return {"current": self.current}

    def load(self, d: dict) -> None:
        self.current = d.get("current", self.current)
