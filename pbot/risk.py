"""Risk gates and position sizing. Every new entry has to pass all of them."""
from __future__ import annotations

import math
from datetime import date
from typing import Optional, Tuple

from .clock import MarketCalendar
from .config import RiskConfig
from .journal import Journal


class RiskManager:
    def __init__(self, cfg: RiskConfig, journal: Journal, calendar: MarketCalendar, mode: str):
        self.cfg = cfg
        self.journal = journal
        self.calendar = calendar
        self.mode = mode
        self.halted: Optional[str] = None

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = reason

    def day_trades_in_window(self, today: date) -> int:
        since = self.calendar.business_days_back(today, 5)
        return self.journal.day_trades_since(self.mode, since)

    def can_enter(self, today: date, open_positions: int, equity: float) -> Tuple[bool, str]:
        if self.halted:
            return False, f"halted: {self.halted}"
        if open_positions >= self.cfg.max_open_positions:
            return False, f"max open positions ({self.cfg.max_open_positions})"
        n = self.journal.entries_on(self.mode, today)
        if n >= self.cfg.max_trades_per_day:
            return False, f"max trades per day ({self.cfg.max_trades_per_day})"
        if self.journal.realized(self.mode, today) <= -abs(self.cfg.max_daily_loss_usd):
            return False, "daily loss limit reached"
        if self.cfg.pdt_guard and equity < self.cfg.pdt_equity_threshold:
            # Every entry here becomes a day trade (flattened before close), so count it up front.
            used = self.day_trades_in_window(today) + open_positions
            if used >= self.cfg.pdt_max_day_trades:
                return False, (f"PDT guard: {used} day trades in 5 business days with equity "
                               f"${equity:,.0f} < ${self.cfg.pdt_equity_threshold:,.0f}")
        return True, "ok"

    def size(self, entry: float, stop: float, buying_power: float, fractional: bool = False) -> float:
        risk_ps = entry - stop
        if risk_ps <= 0 or entry <= 0:
            return 0
        by_risk = self.cfg.risk_per_trade_usd / risk_ps
        by_notional = self.cfg.max_position_usd / entry
        by_bp = max(0.0, buying_power) * self.cfg.max_buying_power_frac / entry
        q = min(by_risk, by_notional, by_bp)
        return math.floor(q * 1000) / 1000 if fractional else float(math.floor(q))

    def loss_breached(self, today: date, unrealized: float) -> bool:
        return self.journal.realized(self.mode, today) + unrealized <= -abs(self.cfg.max_daily_loss_usd)
