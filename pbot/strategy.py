"""Opening-range breakout (ORB), long only, with a VWAP filter.

The first N one-minute bars after the open define the opening range (OR). After it is set, a
symbol becomes a candidate when price breaks above the OR high while trading above VWAP, the
range is neither too tight nor too wide, enough shares traded, and the spread is tight.

Exits (checked every poll):
  * stop     - range midpoint (or range low); moved to breakeven once the trade is up 1R
  * target   - entry + target_r x risk per share
  * time     - everything is flattened at the end of the opening window

All functions here are pure so they can be unit-tested and replayed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import StrategyConfig
from .public_api import Bar, Quote


@dataclass
class OpeningRange:
    high: float
    low: float
    volume: float

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


@dataclass
class Signal:
    symbol: str
    ref_price: float           # ask at decision time
    stop: float
    target: float
    risk_per_share: float
    reason: str


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    stop: float
    target: float
    risk_per_share: float
    trade_id: int = 0
    broker_stop_id: Optional[str] = None
    broker_stop_price: Optional[float] = None
    breakeven_done: bool = False
    high_water: float = 0.0
    notes: List[str] = field(default_factory=list)

    def unrealized(self, price: Optional[float]) -> float:
        return 0.0 if price is None else (price - self.entry) * self.qty


def opening_range(bars: List[Bar], n: int) -> Optional[OpeningRange]:
    if n <= 0 or len(bars) < n:
        return None
    first = bars[:n]
    return OpeningRange(max(b.high for b in first), min(b.low for b in first),
                        sum(b.volume for b in first))


def vwap(bars: List[Bar]) -> Optional[float]:
    pv = vol = 0.0
    for b in bars:
        pv += (b.high + b.low + b.close) / 3 * b.volume
        vol += b.volume
    return pv / vol if vol > 0 else None


def screen_range(orng: OpeningRange, cfg: StrategyConfig) -> Optional[str]:
    """Reason to skip this symbol for the whole day, or None if it is tradable."""
    ref = orng.high
    if ref < cfg.min_price or ref > cfg.max_price:
        return f"price {ref:.2f} outside {cfg.min_price}-{cfg.max_price}"
    width_pct = orng.width / ref * 100 if ref else 0
    if width_pct < cfg.min_range_pct:
        return f"opening range too tight ({width_pct:.2f}% < {cfg.min_range_pct}%)"
    if width_pct > cfg.max_range_pct:
        return f"opening range too wide ({width_pct:.2f}% > {cfg.max_range_pct}%)"
    if orng.volume < cfg.min_range_volume:
        return f"thin opening volume ({orng.volume:,.0f} < {cfg.min_range_volume:,.0f} shares)"
    return None


def evaluate_long(symbol: str, quote: Quote, bars: List[Bar], orng: OpeningRange,
                  cfg: StrategyConfig) -> Tuple[Optional[Signal], str]:
    """(signal, why). `why` explains a pass so the agent can narrate what it is waiting for."""
    if quote.last is None or quote.ask is None or quote.bid is None:
        return None, "no live quote"
    trigger = orng.high * (1 + cfg.breakout_buffer_pct / 100)
    if quote.last < trigger:
        return None, f"waiting: last {quote.last:.2f} < trigger {trigger:.2f}"
    sp = quote.spread_pct
    if sp is None or sp > cfg.max_spread_pct:
        return None, f"spread too wide ({sp if sp is None else round(sp, 3)}%)"
    vw = vwap(bars)
    if cfg.require_above_vwap and (vw is None or quote.last <= vw):
        return None, f"below VWAP ({vw})"
    stop = orng.mid if cfg.stop_mode == "mid" else orng.low
    entry = quote.ask
    risk = entry - stop
    if risk <= 0:
        return None, "stop above entry"
    # A breakout that has already run well past the range high is a chase - skip it for now.
    if entry - orng.high > cfg.max_chase_range_frac * orng.width:
        return None, (f"extended: ask {entry:.2f} is >{cfg.max_chase_range_frac:g}x the range "
                      f"past {orng.high:.2f}")
    target = entry + cfg.target_r * risk
    return Signal(symbol, entry, round(stop, 2), round(target, 2), risk,
                  f"broke OR high {orng.high:.2f} (range {orng.low:.2f}-{orng.high:.2f}), "
                  f"above VWAP {vw:.2f}" if vw else f"broke OR high {orng.high:.2f}"), "signal"


def manage(pos: Position, quote: Quote, cfg: StrategyConfig) -> Optional[str]:
    """Update the position's stop in place; return an exit reason or None to hold."""
    px = quote.bid if quote.bid else quote.last
    if px is None:
        return None
    pos.high_water = max(pos.high_water, px)
    if px <= pos.stop:
        return "breakeven stop" if pos.breakeven_done else "stop"
    if px >= pos.target:
        return "target"
    if (not pos.breakeven_done and cfg.breakeven_at_r > 0
            and px >= pos.entry + cfg.breakeven_at_r * pos.risk_per_share):
        pos.stop = round(max(pos.stop, pos.entry), 2)
        pos.breakeven_done = True
        pos.notes.append(f"stop -> breakeven {pos.stop:.2f}")
    return None
