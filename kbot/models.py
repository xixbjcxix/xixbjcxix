"""Core data types shared by every stage (sim, paper, live)."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

UP = "YES"     # internal name UP kept for the shared engine; on Kalshi this is the YES side
DOWN = "NO"
OUTCOMES = (UP, DOWN)

BUY = "BUY"
SELL = "SELL"


def other(outcome: str) -> str:
    return DOWN if outcome == UP else UP


def px(p: float) -> float:
    """Canonical price key (avoids float noise like 0.30000000000000004)."""
    return round(p + 0.0, 4)


def tick_floor(p: float, tick: float) -> float:
    """Largest multiple of tick that is <= p (with float tolerance)."""
    if p <= 0:
        return 0.0
    n = int((p + 1e-9) / tick)
    return px(n * tick)


def tick_ceil(p: float, tick: float) -> float:
    n = int((p - 1e-9) / tick)
    if n * tick < p - 1e-9:
        n += 1
    return px(n * tick)


class OrderStatus(str, Enum):
    PENDING = "pending"      # sent, not yet live (latency)
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class MarketInfo:
    slug: str                # Kalshi market ticker, e.g. KXBTC15M-26SEP301215-15
    asset: str               # "btc" | "eth"
    horizon_s: int           # 900
    start_ms: int            # open_time
    end_ms: int              # close_time
    token_up: str            # "<ticker>:yes"
    token_down: str          # "<ticker>:no"
    condition_id: str = ""   # event ticker
    tick_size: float = 0.01
    min_order_size: float = 1.0
    fees_enabled: bool = True
    fee_rate: Optional[float] = None       # unused on Kalshi (kept for engine compatibility)
    fee_exponent: Optional[float] = None
    rebate_rate: Optional[float] = None
    winner: Optional[str] = None           # YES | NO once determined
    open_price: Optional[float] = None     # the market's target ("price to beat"): floor_strike
    series: str = ""
    fee_type: str = "quadratic_with_maker_fees"   # from GET /series/{ticker}
    fee_multiplier: float = 1.0
    strike_type: str = "greater_or_equal"
    exchange_index: Optional[int] = None     # Kalshi exchange shard (crypto moved to shard 2 in Aug 2026)

    def token(self, outcome: str) -> str:
        return self.token_up if outcome == UP else self.token_down

    def outcome_of(self, token_id: str) -> Optional[str]:
        if token_id == self.token_up:
            return UP
        if token_id == self.token_down:
            return DOWN
        return None


_order_ids = itertools.count(1)


def new_order_id(prefix: str = "o") -> str:
    return f"{prefix}{next(_order_ids)}"


@dataclass
class Order:
    market: str
    outcome: str
    side: str
    price: float
    size: float
    post_only: bool = True
    tag: str = ""                 # why the strategy wanted it: base|complete|rescue|cut|taker_entry
    id: str = field(default_factory=new_order_id)
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    created_ms: int = 0
    live_ms: int = 0              # when the order became active on the book
    cancel_req_ms: Optional[int] = None
    queue_ahead: float = 0.0      # shares ahead of us at our price level (sim only)
    reject_reason: str = ""
    exchange_id: str = ""         # venue id (live)
    token: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN)

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class Fill:
    order_id: str
    market: str
    outcome: str
    side: str
    price: float
    size: float
    liquidity: str            # "maker" | "taker"
    fee: float                # USDC paid (taker)
    rebate: float             # USDC received / estimated (maker)
    ts_ms: int
    tag: str = ""


@dataclass
class Trade:
    """A public trade print on one outcome token."""
    token: str
    price: float
    size: float
    side: str                 # aggressor side (BUY = taker lifted asks, SELL = taker hit bids)
    ts_ms: int
    tx: str = ""
