"""Plain data types shared by every mode (live, paper, shadow, backtest)."""
from __future__ import annotations

from dataclasses import dataclass, field

# Kalshi has one book per market.  A NO bid at p is a YES ask at 1-p, so
# buying YES and NO together can never cost less than $1 as a taker: a taker
# "pair arbitrage" does not exist.  The old bot could only earn it as a maker,
# which is exactly where it got picked off.  This bot only takes.


@dataclass
class Book:
    """Bids on each side, best first.  Prices in dollars, sizes in contracts."""
    yes_bids: list[tuple[float, float]] = field(default_factory=list)
    no_bids: list[tuple[float, float]] = field(default_factory=list)
    ts: float = 0.0

    def bids(self, side: str) -> list[tuple[float, float]]:
        return self.yes_bids if side == "yes" else self.no_bids

    def asks(self, side: str) -> list[tuple[float, float]]:
        """Asks for `side`, cheapest first (derived from the other side's bids)."""
        other = self.no_bids if side == "yes" else self.yes_bids
        return [(round(1.0 - p, 4), q) for p, q in other]

    def best_bid(self, side: str) -> float | None:
        b = self.bids(side)
        return b[0][0] if b else None

    def best_ask(self, side: str) -> float | None:
        a = self.asks(side)
        return a[0][0] if a else None

    def ask_qty(self, side: str) -> float:
        a = self.asks(side)
        return a[0][1] if a else 0.0

    def spread(self) -> float | None:
        a, b = self.best_ask("yes"), self.best_bid("yes")
        if a is None or b is None:
            return None
        return a - b

    @staticmethod
    def normalize(levels) -> list[tuple[float, float]]:
        out = [(float(p), float(q)) for p, q in levels if float(q) > 0 and 0 < float(p) < 1]
        out.sort(key=lambda x: -x[0])
        return out


@dataclass
class MarketMeta:
    ticker: str
    asset: str
    open_ts: float
    close_ts: float
    strike: float | None
    strike_type: str = "greater"
    status: str = ""
    result: str = ""          # "yes" / "no" once settled


@dataclass
class View:
    """Everything a strategy may look at for one market at one instant."""
    ts: float
    meta: MarketMeta
    book: Book
    spot: float | None
    spot_ts: float
    sigma: float
    p_raw: float | None       # model probability of YES, uncalibrated
    p_yes: float | None       # calibrated

    @property
    def elapsed(self) -> float:
        return self.ts - self.meta.open_ts

    @property
    def remaining(self) -> float:
        return self.meta.close_ts - self.ts


@dataclass
class Position:
    ticker: str
    side: str = ""            # "yes" / "no" / "" when flat
    qty: float = 0.0
    cost: float = 0.0         # dollars paid for the open qty, fees included
    realized: float = 0.0     # closed P&L in this market, fees included
    entries: int = 0
    profile: str = ""         # profile that opened the position
    entry_price: float = 0.0  # average price excluding fees
    peak_p: float = 0.0       # best model prob for our side since entry

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty > 0 else 0.0

    def apply_buy(self, side: str, qty: float, price: float, fee: float, profile: str = "") -> None:
        if qty <= 0:
            return
        if self.qty > 0 and side != self.side:
            raise ValueError("buy on the opposite side - close first")
        notional = self.entry_price * self.qty + price * qty
        self.side = side
        self.qty += qty
        self.cost += price * qty + fee
        self.entry_price = notional / self.qty
        self.entries += 1
        if profile:
            self.profile = profile

    def apply_sell(self, qty: float, price: float, fee: float) -> float:
        qty = min(qty, self.qty)
        if qty <= 0:
            return 0.0
        basis = self.avg_cost * qty
        pnl = price * qty - fee - basis
        self.cost -= basis
        self.qty -= qty
        self.realized += pnl
        if self.qty <= 1e-9:
            self.qty, self.cost, self.side = 0.0, 0.0, ""
        return pnl

    def settle(self, result: str) -> float:
        """Close the position at settlement; returns the P&L of the open part."""
        if self.qty <= 0:
            return 0.0
        payout = self.qty * (1.0 if result == self.side else 0.0)
        pnl = payout - self.cost
        self.realized += pnl
        self.qty, self.cost, self.side = 0.0, 0.0, ""
        return pnl

    def mark(self, book: Book) -> float:
        """Liquidation value at the best bid (no fee)."""
        if self.qty <= 0:
            return 0.0
        bid = book.best_bid(self.side) or 0.0
        return bid * self.qty
