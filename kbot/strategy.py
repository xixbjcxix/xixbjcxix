"""Complete-set accumulation with a gated directional residual.

Each window pays $1 to exactly one of YES/NO (UP/DOWN internally). All pair costs below are
ALL-IN: contract prices plus Kalshi fees (maker fees on resting fills, taker fees on crossing). Owning 1 UP + 1 DOWN bought for a
combined price below $1 locks (1 - pair_cost) per set at resolution, whatever happens.

Per market, every tick, the strategy returns a Plan:
  * Flat inventory -> rest post-only BUYs on both sides priced off the book's implied
    fair value so that bid_UP + bid_DOWN <= target_pair_cost, never more than
    `improve_ticks` above the best bid and never crossing the ask.
  * Holding an unpaired leg (say long UP at avg cost c) -> stop adding UP and work the
    DOWN side to complete: passive bid up to (target - c); take the DOWN ask if
    ask + taker fee + c <= target (pair still locked at target after fees).
  * Residual policy: the unpaired leg may be kept (up to caps) only while the spot
    momentum signal points the same way as the leg. If the signal is unusable,
    disagrees, residual is disabled, or the window is ending, switch to rescue:
    raise the completion limit to rescue_pair_cost, and in the final flatten
    window exit the excess with a taker order - completing the pair or selling the
    leg, whichever loses less after fees.
  * Inside the last `cutoff` seconds: cancel everything, place nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .book import OrderBook
from .fees import FeeModel
from .inventory import MarketInventory
from .models import BUY, DOWN, OUTCOMES, SELL, UP, MarketInfo, other, px, tick_floor
from .signal import SignalReading


@dataclass
class ResidualConfig:
    enabled: bool = True
    max_shares: float = 20.0
    max_usd: float = 8.0
    # While the signal agrees, let the allowed part of the leg ride (stop working the other
    # side). False = keep completing it opportunistically at the target (locks the pair instead).
    ride_when_agreeing: bool = True
    # Model-edge gate: keep a leg only if model P(win) - leg cost (incl. fee) >= this ($ per contract).
    # Direction agreement alone says "spot is moving that way"; this also asks "is the price still cheap?"
    min_edge: float = 0.02


@dataclass
class StrategyConfig:
    target_pair_cost: float = 0.94      # YES + NO cost per set INCLUDING fees
    clip_shares: float = 10.0
    improve_ticks: int = 1
    min_price: float = 0.05
    max_price: float = 0.90
    start_offset_s: float = 0.0          # start quoting this many seconds after window open
    # settlement averages the index over the final 60 s, so by default we are flat before it starts
    cutoff_s: Dict[int, float] = field(default_factory=lambda: {900: 60.0})
    rescue_window_s: float = 90.0        # before cutoff: raise completion limit if residual not allowed
    flatten_s: float = 30.0              # before cutoff: taker-exit residual that isn't allowed
    rescue_pair_cost: float = 0.99
    max_exit_pair_cost: float = 1.03     # taker-complete in flatten window only if pair cost (incl fee) <= this
    taker_complete: bool = True
    taker_entry: bool = False
    taker_entry_pair_cost: float = 0.92
    no_new_legs_in_rescue: bool = True   # stop opening new two-sided quotes once rescue window starts
    # Volatility-aware entry: when short-window vol runs above long-window vol (a regime spike), resting
    # bids get filled by informed flow more often, so demand more edge: target -= vol_widen_per_unit *
    # (vol_ratio - 1), capped at max_vol_widen. 0 = off.
    vol_widen_per_unit: float = 0.0
    max_vol_widen: float = 0.04
    # Adverse-selection guard: when the spot signal points one way (|z| >= adverse_guard_z), the resting
    # bid on the OTHER side is the one informed traders hit. Lower it by adverse_skew_ticks. 0 = off.
    adverse_skew_ticks: int = 0
    adverse_guard_z: float = 1.0
    # Stop opening new two-sided quotes in a market after this many leg episodes (one-sided stretches). 0 = off.
    max_legs_per_market: int = 0
    residual: ResidualConfig = field(default_factory=ResidualConfig)

    def cutoff_for(self, horizon_s: int) -> float:
        return float(self.cutoff_s.get(horizon_s, max(self.cutoff_s.values()) if self.cutoff_s else 20.0))


@dataclass
class Quote:
    outcome: str
    price: float
    size: float
    tag: str


@dataclass
class TakeAction:
    outcome: str
    side: str
    limit: float
    size: float
    tag: str


@dataclass
class Plan:
    quotes: Dict[str, Optional[Quote]] = field(default_factory=lambda: {UP: None, DOWN: None})
    takes: List[TakeAction] = field(default_factory=list)
    cancel_all: bool = False
    mode: str = "idle"
    note: str = ""


class Strategy:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    # ---------------------------------------------------------------------------------
    def plan(self, m: MarketInfo, now_ms: int, books: Dict[str, OrderBook], inv: MarketInventory,
             sig: SignalReading, fee: FeeModel, budget_usd: float = float("inf")) -> Plan:
        c = self.cfg
        t_left = (m.end_ms - now_ms) / 1000.0
        cutoff = c.cutoff_for(m.horizon_s)
        if now_ms < m.start_ms + c.start_offset_s * 1000:
            return Plan(mode="before_start")
        if t_left <= cutoff:
            return Plan(cancel_all=True, mode="cutoff")
        bu, bd = books[UP], books[DOWN]
        if not (bu.has_snapshot and bd.has_snapshot):
            return Plan(cancel_all=True, mode="no_book")
        tick = m.tick_size
        in_rescue = t_left <= cutoff + c.rescue_window_s
        in_flatten = t_left <= cutoff + c.flatten_s

        side = inv.residual_side
        plan = Plan(mode="accumulate")
        if side is None:
            if in_rescue and c.no_new_legs_in_rescue:
                return Plan(mode="late_flat")
            if c.max_legs_per_market and len(inv.episodes) >= c.max_legs_per_market:
                return Plan(mode="leg_limit")
            self._base_quotes(plan, m, bu, bd, tick, fee, budget_usd, sig)
            return plan

        # ---- holding an unpaired leg -------------------------------------------------
        short = other(side)
        res_sh = abs(inv.residual)
        raw_cost = inv.avg_cost(side)
        cost = raw_cost + fee.maker_fee_per_share(raw_cost)     # leg cost incl. (maker) fee
        b_short = books[short]
        b_long = books[side]

        allowed_sh = 0.0
        if c.residual.enabled and sig.ok and sig.direction == side and self._edge_ok(sig, side, cost):
            allowed_sh = min(c.residual.max_shares, c.residual.max_usd / max(raw_cost, 1e-6))
        excess = max(0.0, res_sh - allowed_sh)
        disagrees = (not sig.ok) or (sig.direction == short) or (not c.residual.enabled)
        rescue = excess > 1e-9 and (in_rescue or disagrees)
        plan.mode = "rescue" if rescue else ("hold_residual" if excess <= 1e-9 else "complete")

        # shares we are working to pair: all of it, or only the excess while an agreed leg rides
        work_sh = excess if c.residual.ride_when_agreeing else res_sh
        if work_sh <= 1e-9:
            plan.mode = "ride_residual"
            return plan
        limit = (c.rescue_pair_cost if rescue else c.target_pair_cost) - cost
        bid = self._passive_bid(b_short, self._net_of_maker_fee(limit, tick, fee), tick)
        if bid is not None:
            plan.quotes[short] = Quote(short, bid, work_sh, "rescue" if rescue else "complete")

        # taker completion if the pair is still locked at the relevant limit after fees
        ask = b_short.best_ask
        if c.taker_complete and ask is not None:
            pair_all_in = cost + ask + fee.taker_fee_per_share(ask)
            thresh = c.rescue_pair_cost if rescue else c.target_pair_cost
            if pair_all_in <= thresh + 1e-9:
                plan.takes.append(TakeAction(short, BUY, ask, work_sh, "taker_complete"))
                return plan

        # flatten window: exit whatever residual isn't allowed, cheapest way after fees
        if in_flatten and excess > 1e-9:
            best = None
            if ask is not None:
                pnl_complete = 1.0 - (cost + ask + fee.taker_fee_per_share(ask))
                if cost + ask + fee.taker_fee_per_share(ask) <= c.max_exit_pair_cost:
                    best = ("complete", pnl_complete, TakeAction(short, BUY, ask, excess, "flatten_complete"))
            bid_long = b_long.best_bid
            if bid_long is not None:
                pnl_sell = bid_long - fee.taker_fee_per_share(bid_long) - cost
                if best is None or pnl_sell > best[1]:
                    best = ("sell", pnl_sell, TakeAction(side, SELL, bid_long, excess, "flatten_sell"))
            if best is not None:
                plan.takes.append(best[2])
                plan.mode = "flatten_" + best[0]
        return plan

    # ---------------------------------------------------------------------------------
    def _edge_ok(self, sig: SignalReading, side: str, cost_allin: float) -> bool:
        need = self.cfg.residual.min_edge
        if need <= 0:
            return True
        p = sig.p_side(side)
        return p is not None and p - cost_allin >= need - 1e-12

    def vol_widen(self, sig: Optional[SignalReading]) -> float:
        c = self.cfg
        if c.vol_widen_per_unit <= 0 or sig is None or not sig.ok:
            return 0.0
        return min(c.max_vol_widen, max(0.0, c.vol_widen_per_unit * (sig.vol_ratio - 1.0)))

    @staticmethod
    def _net_of_maker_fee(limit_allin: float, tick: float, fee: FeeModel) -> float:
        """Highest price p on the tick grid with p + maker_fee(p) <= limit_allin."""
        p = tick_floor(limit_allin, tick)
        while p > 0 and p + fee.maker_fee_per_share(p) > limit_allin + 1e-12:
            p = px(p - tick)
        return p

    def _passive_bid(self, book: OrderBook, limit: float, tick: float) -> Optional[float]:
        c = self.cfg
        p = limit
        bb, ba = book.best_bid, book.best_ask
        if bb is not None:
            p = min(p, bb + c.improve_ticks * tick)
        if ba is not None:
            p = min(p, ba - tick)
        p = tick_floor(p, tick)
        if p < c.min_price or p <= 0:
            return None
        return min(p, c.max_price)

    def _base_quotes(self, plan: Plan, m: MarketInfo, bu: OrderBook, bd: OrderBook,
                     tick: float, fee: FeeModel, budget_usd: float = float("inf"),
                     sig: Optional[SignalReading] = None) -> None:
        c = self.cfg
        target = c.target_pair_cost - self.vol_widen(sig)
        mu, md = bu.mid(), bd.mid()
        if mu is None or md is None or mu + md <= 0:
            plan.mode = "no_mid"
            return
        fair_u = mu / (mu + md)
        fair_d = 1.0 - fair_u
        half = (1.0 - target) / 2.0
        pu = self._passive_bid(bu, fair_u - half, tick)
        pd = self._passive_bid(bd, fair_d - half, tick)
        if pu is None or pd is None:
            plan.mode = "out_of_range"
            return
        if c.adverse_skew_ticks and sig is not None and sig.ok and sig.direction is not None \
                and abs(sig.z) >= c.adverse_guard_z:
            # spot is moving toward `sig.direction`; the opposite outcome is getting cheaper for a reason
            skew = c.adverse_skew_ticks * tick
            if sig.direction == UP:
                pd = tick_floor(px(pd - skew), tick)
            else:
                pu = tick_floor(px(pu - skew), tick)
            if pu < c.min_price or pd < c.min_price:
                plan.mode = "adverse_guard"
                return
        guard = 0
        allin = lambda a, b: a + b + fee.maker_fee_per_share(a) + fee.maker_fee_per_share(b)  # noqa: E731
        while allin(pu, pd) > target + 1e-9 and guard < 200:
            if pu >= pd:
                pu = px(pu - tick)
            else:
                pd = px(pd - tick)
            guard += 1
        if pu < c.min_price or pd < c.min_price:
            plan.mode = "out_of_range"
            return
        size = max(c.clip_shares, m.min_order_size)
        if budget_usd < size * (pu + pd):
            size = int(max(budget_usd, 0.0) / (pu + pd))
            if size < m.min_order_size:
                plan.mode = "capacity"
                return
        plan.quotes[UP] = Quote(UP, pu, size, "base")
        plan.quotes[DOWN] = Quote(DOWN, pd, size, "base")

        if c.taker_entry:
            for o, b, ob in ((UP, bu, bd), (DOWN, bd, bu)):
                ask = b.best_ask
                other_bid = ob.best_bid
                if ask is None or other_bid is None:
                    continue
                if ask + fee.taker_fee_per_share(ask) + other_bid + fee.maker_fee_per_share(other_bid) + tick \
                        <= c.taker_entry_pair_cost:
                    plan.takes.append(TakeAction(o, BUY, ask, size, "taker_entry"))
                    plan.quotes[o] = None
                    break
