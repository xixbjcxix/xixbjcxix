"""Per-market inventory: paired complete sets (locked edge) vs unpaired residual."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import BUY, DOWN, UP, Fill, other


@dataclass
class LegEpisode:
    """A stretch of time during which the market held an unpaired residual."""
    start_ms: int
    side: str                       # outcome held long unpaired
    max_shares: float = 0.0
    end_ms: Optional[int] = None
    closed_by: str = ""             # passive | taker_complete | cut | resolution | flip


@dataclass
class MarketInventory:
    market: str
    shares: Dict[str, float] = field(default_factory=lambda: {UP: 0.0, DOWN: 0.0})
    cost: Dict[str, float] = field(default_factory=lambda: {UP: 0.0, DOWN: 0.0})  # price*size, excl fees
    fees: float = 0.0
    rebates: float = 0.0
    realized: float = 0.0           # PnL from sells (price - avg cost), excl fees
    bought_cost_total: float = 0.0  # total USDC spent on buys (capital usage)
    settled: bool = False
    settle_pnl: Optional[float] = None
    episodes: List[LegEpisode] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)
    rescue_excess: float = 0.0      # $ paid above target pair cost to complete pairs

    # ---- views ---------------------------------------------------------------------
    @property
    def up(self) -> float:
        return self.shares[UP]

    @property
    def down(self) -> float:
        return self.shares[DOWN]

    @property
    def paired(self) -> float:
        return min(self.up, self.down)

    @property
    def residual(self) -> float:
        """Signed unpaired shares: + = long UP, - = long DOWN."""
        return self.up - self.down

    @property
    def residual_side(self) -> Optional[str]:
        r = self.residual
        if abs(r) < 1e-9:
            return None
        return UP if r > 0 else DOWN

    def avg_cost(self, outcome: str) -> float:
        s = self.shares[outcome]
        return self.cost[outcome] / s if s > 1e-12 else 0.0

    @property
    def pair_cost(self) -> float:
        """Average price paid for one complete set (UP + DOWN), excl fees."""
        return self.avg_cost(UP) + self.avg_cost(DOWN)

    @property
    def residual_cost_usd(self) -> float:
        side = self.residual_side
        return abs(self.residual) * self.avg_cost(side) if side else 0.0

    @property
    def cost_basis_usd(self) -> float:
        return self.cost[UP] + self.cost[DOWN]

    # ---- mutations -----------------------------------------------------------------
    def apply_fill(self, f: Fill) -> None:
        before_side = self.residual_side
        before_abs = abs(self.residual)
        if f.side == BUY:
            self.shares[f.outcome] += f.size
            self.cost[f.outcome] += f.price * f.size
            self.bought_cost_total += f.price * f.size
        else:
            avg = self.avg_cost(f.outcome)
            sz = min(f.size, self.shares[f.outcome])
            self.shares[f.outcome] -= sz
            self.cost[f.outcome] -= avg * sz
            self.realized += (f.price - avg) * sz
            if self.shares[f.outcome] < 1e-9:
                self.shares[f.outcome] = 0.0
                self.cost[f.outcome] = 0.0
        self.fees += f.fee
        self.rebates += f.rebate
        self.fills.append(f)
        self._track_episode(f, before_side, before_abs)

    def _track_episode(self, f: Fill, before_side: Optional[str], before_abs: float) -> None:
        side = self.residual_side
        ep = self.episodes[-1] if self.episodes and self.episodes[-1].end_ms is None else None
        if ep is None and side is not None:
            ep = LegEpisode(start_ms=f.ts_ms, side=side)
            self.episodes.append(ep)
        if ep is None:
            return
        if side is None:
            ep.end_ms = f.ts_ms
            if f.side == BUY:
                ep.closed_by = "taker_complete" if f.liquidity == "taker" else "passive"
            else:
                ep.closed_by = "cut"
        elif side != ep.side:
            ep.end_ms = f.ts_ms
            ep.closed_by = "flip"
            self.episodes.append(LegEpisode(start_ms=f.ts_ms, side=side, max_shares=abs(self.residual)))
        else:
            ep.max_shares = max(ep.max_shares, abs(self.residual))

    def settle(self, winner: str, ts_ms: int) -> dict:
        """Resolve: each share of the winning outcome pays $1. Returns a PnL breakdown."""
        paired = self.paired
        side = self.residual_side
        res_sh = abs(self.residual)
        avg_up, avg_down = self.avg_cost(UP), self.avg_cost(DOWN)
        paired_pnl = paired * (1.0 - avg_up - avg_down)
        residual_pnl = 0.0
        if side:
            residual_pnl = res_sh * ((1.0 if side == winner else 0.0) - self.avg_cost(side))
        total = paired_pnl + residual_pnl + self.realized - self.fees + self.rebates
        ep = self.episodes[-1] if self.episodes and self.episodes[-1].end_ms is None else None
        if ep is not None:
            ep.end_ms = ts_ms
            ep.closed_by = "resolution"
        self.settled = True
        self.settle_pnl = total
        return {
            "winner": winner,
            "paired_shares": paired,
            "pair_cost": avg_up + avg_down if paired > 0 else None,
            "paired_capital": paired * (avg_up + avg_down),
            "paired_pnl": paired_pnl,
            "residual_side": side,
            "residual_shares": res_sh,
            "residual_capital": res_sh * (self.avg_cost(side) if side else 0.0),
            "residual_pnl": residual_pnl,
            "cut_pnl": self.realized,
            "fees": self.fees,
            "rebates": self.rebates,
            "rescue_excess": self.rescue_excess,
            "total_pnl": total,
        }

    def mark_to_market(self, mid_up: Optional[float], mid_down: Optional[float]) -> float:
        """Unrealized + realized PnL at current mids (for daily-loss checks)."""
        mu = mid_up if mid_up is not None else self.avg_cost(UP)
        md = mid_down if mid_down is not None else self.avg_cost(DOWN)
        val = self.up * mu + self.down * md
        return val - self.cost_basis_usd + self.realized - self.fees + self.rebates

    def worst_case_pnl(self) -> float:
        """PnL if the residual side loses (paired sets still pay $1)."""
        paired_pnl = self.paired * (1.0 - self.pair_cost) if self.paired > 0 else 0.0
        return paired_pnl - self.residual_cost_usd + self.realized - self.fees + self.rebates
