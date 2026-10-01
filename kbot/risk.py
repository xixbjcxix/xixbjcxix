"""Pre-trade risk checks, loss limits and the kill switch. Non-negotiable: every order
from every stage goes through check_order(); a tripped kill switch is sticky until restart."""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional, Tuple

from .inventory import MarketInventory
from .models import BUY, SELL, UP, Order, other


@dataclass
class RiskConfig:
    max_order_usd: float = 10.0
    max_market_usd: float = 40.0          # cost basis + open buy notional, per market
    max_residual_usd: float = 10.0        # worst-case unpaired $ per market
    max_residual_shares: float = 25.0     # worst-case unpaired shares per market
    max_total_usd: float = 150.0          # across all markets
    max_daily_loss_usd: float = 25.0      # realized + mark-to-market, since UTC midnight
    max_open_orders: int = 40
    max_orders_per_min: int = 300
    pm_stale_ms: int = 20000              # no Kalshi market data for this long -> data gap
    kill_file: str = "data/KILL"          # touch this file to trip the kill switch


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self.killed = False
        self.kill_reason = ""
        self.kill_ms: Optional[int] = None
        self.halt_until_ms = 0            # daily-loss halt: no new orders until next UTC day
        self.halt_reason = ""
        self.halts: List[Tuple[int, str]] = []
        self.order_times: Deque[int] = deque()
        self.rejects: List[Tuple[int, str, str]] = []   # (ts, order tag, reason)

    def kill(self, reason: str, now_ms: int) -> None:
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
            self.kill_ms = now_ms

    def check_kill_file(self, now_ms: int) -> None:
        if self.cfg.kill_file and os.path.exists(self.cfg.kill_file):
            self.kill("kill_file", now_ms)

    def check_daily_loss(self, pnl_today: float, now_ms: int) -> None:
        if now_ms < self.halt_until_ms:
            return
        if pnl_today <= -abs(self.cfg.max_daily_loss_usd):
            self.halt_until_ms = (now_ms // 86400000 + 1) * 86400000
            self.halt_reason = f"daily_loss {pnl_today:.2f}"
            self.halts.append((now_ms, self.halt_reason))

    def blocked(self, now_ms: int) -> Optional[str]:
        if self.killed:
            return "killed:" + self.kill_reason
        if now_ms < self.halt_until_ms:
            return "halted:" + self.halt_reason
        return None

    # ---------------------------------------------------------------------------------
    def check_order(self, o: Order, inv: MarketInventory, market_open: Iterable[Order],
                    all_open: Iterable[Order], total_cost_basis: float, now_ms: int) -> RiskDecision:
        d = self._check(o, inv, list(market_open), list(all_open), total_cost_basis, now_ms)
        if not d.ok:
            self.rejects.append((now_ms, o.tag, d.reason))
            if len(self.rejects) > 5000:
                del self.rejects[:1000]
        else:
            self.order_times.append(now_ms)
        return d

    def _check(self, o: Order, inv: MarketInventory, mkt_open: List[Order], all_open: List[Order],
               total_cost_basis: float, now_ms: int) -> RiskDecision:
        c = self.cfg
        b = self.blocked(now_ms)
        if b:
            return RiskDecision(False, b)
        if o.size <= 0 or not (0.0 < o.price < 1.0):
            return RiskDecision(False, "bad_order")
        while self.order_times and now_ms - self.order_times[0] > 60000:
            self.order_times.popleft()
        if len(self.order_times) >= c.max_orders_per_min:
            return RiskDecision(False, "rate_limit")
        if len([x for x in all_open if x.is_active]) >= c.max_open_orders:
            return RiskDecision(False, "max_open_orders")
        if o.notional > c.max_order_usd + 1e-9:
            return RiskDecision(False, f"max_order_usd {o.notional:.2f}")

        if o.side == SELL:
            held = inv.shares[o.outcome]
            pending_sells = sum(x.remaining for x in mkt_open if x.is_active and x.side == SELL
                                and x.outcome == o.outcome)
            if o.size > held - pending_sells + 1e-9:
                return RiskDecision(False, "sell_exceeds_position")
            return RiskDecision(True)

        # BUY: worst case = every open buy on this outcome fills, nothing on the other side does
        open_same = sum(x.remaining for x in mkt_open if x.is_active and x.side == BUY and x.outcome == o.outcome)
        long_after = inv.shares[o.outcome] + open_same + o.size
        unpaired = long_after - inv.shares[other(o.outcome)]
        reduces = inv.residual_side == other(o.outcome)   # buying the short side completes pairs
        if not reduces or unpaired > abs(inv.residual) + 1e-9:
            if unpaired > c.max_residual_shares + 1e-9:
                return RiskDecision(False, f"residual_shares {unpaired:.1f}")
            avg_px = ((inv.cost[o.outcome] + (open_same + o.size) * o.price) / long_after) if long_after > 0 else o.price
            if unpaired * avg_px > c.max_residual_usd + 1e-9:
                return RiskDecision(False, f"residual_usd {unpaired * avg_px:.2f}")

        open_buy_notional = sum(x.remaining * x.price for x in mkt_open if x.is_active and x.side == BUY)
        mkt_exposure = inv.cost_basis_usd + open_buy_notional + o.notional
        cap = c.max_market_usd + (c.max_residual_usd if reduces else 0.0)
        if mkt_exposure > cap + 1e-9:
            return RiskDecision(False, f"max_market_usd {mkt_exposure:.2f}")
        all_open_notional = sum(x.remaining * x.price for x in all_open if x.is_active and x.side == BUY)
        if total_cost_basis + all_open_notional + o.notional > c.max_total_usd + (c.max_residual_usd if reduces else 0.0) + 1e-9:
            return RiskDecision(False, "max_total_usd")
        return RiskDecision(True)
