"""Hard limits and the drawdown governor.

The daily-loss stop is measured from the exchange's own numbers
(cash + positions marked at the bid), never from the bot's bookkeeping, so
it still works if the bookkeeping is wrong.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from .config import DATA_DIR, Settings
from .market import Position
from .strategy import Intent

log = logging.getLogger("adaptbot.risk")


class Risk:
    def __init__(self, s: Settings):
        self.s = s
        self.day = datetime.now().date()
        self.start_equity: float | None = None
        self.equity: float | None = None
        self.halted = ""                 # non-empty = no new entries; reason
        self.flatten = False
        self.consec_losses = 0
        self.cooldown_left = 0
        self.kill_file = os.path.join(DATA_DIR, "KILL")

    # ---------------------------------------------------------------- state
    def update_equity(self, equity: float) -> None:
        today = datetime.now().date()
        if today != self.day:
            self.day, self.start_equity = today, None
            if self.halted.startswith("daily loss"):
                self.halted, self.flatten = "", False
                log.info("new day - daily loss stop reset")
        if self.start_equity is None:
            self.start_equity = equity
        self.equity = equity
        if self.day_pnl <= -self.s.max_daily_loss and not self.halted:
            self.halt(f"daily loss stop: {self.day_pnl:+.2f} <= -{self.s.max_daily_loss:.2f}",
                      flatten=self.s.flatten_on_halt)

    @property
    def day_pnl(self) -> float:
        if self.equity is None or self.start_equity is None:
            return 0.0
        return self.equity - self.start_equity

    def halt(self, reason: str, flatten: bool = False) -> None:
        if not self.halted:
            log.error("HALT: %s", reason)
        self.halted = reason
        self.flatten = self.flatten or flatten

    def check_kill(self) -> None:
        if os.path.exists(self.kill_file) and not self.halted.startswith("kill"):
            self.halt("kill switch", flatten=True)

    def market_done(self, pnl: float, traded: bool) -> None:
        if not traded:
            if self.cooldown_left > 0:
                self.cooldown_left -= 1
            return
        if pnl < 0:
            self.consec_losses += 1
            if self.consec_losses >= self.s.max_consecutive_losses:
                self.cooldown_left = self.s.cooldown_markets
                log.warning("%d losing markets in a row - sitting out %d markets",
                            self.consec_losses, self.cooldown_left)
                self.consec_losses = 0
        else:
            self.consec_losses = 0

    # ---------------------------------------------------------------- gates
    def size_mult(self) -> float:
        """Shrink size as the day's loss approaches the stop (1.0 -> 0.25)."""
        used = max(0.0, -self.day_pnl) / self.s.max_daily_loss
        return max(0.25, 1.0 - used)

    def entry_block(self) -> str:
        if self.halted:
            return self.halted
        if self.cooldown_left > 0:
            return f"cooldown ({self.cooldown_left} markets)"
        return ""

    def check(self, it: Intent, pos: Position, positions: dict[str, Position]) -> str:
        """Return '' if the order is allowed, else the reason it is not."""
        s = self.s
        if it.qty < 1:
            return "qty < 1"
        if it.qty > s.max_contracts_per_order:
            return f"qty {it.qty} > max_contracts_per_order"
        if it.action == "sell":
            return "" if it.qty <= pos.qty + 1e-9 and it.side == pos.side else "sell exceeds position"
        if self.entry_block():
            return self.entry_block()
        worst = it.qty * it.limit + it.qty * 0.02
        if pos.cost + worst > s.max_dollars_per_market + 1e-9:
            return "max_dollars_per_market"
        total = sum(p.cost for p in positions.values())
        if total + worst > s.max_dollars_total + 1e-9:
            return "max_dollars_total"
        return ""


class InstanceLock:
    """One bot per data folder.  Heartbeat-based so it also works on Windows."""

    def __init__(self, path: str | None = None, stale_s: float = 60.0):
        self.path = path or os.path.join(DATA_DIR, "bot.lock")
        self.stale_s = stale_s
        self.held = False

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            try:
                age = time.time() - os.path.getmtime(self.path)
            except OSError:
                age = 0
            if age < self.stale_s:
                return False
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.held = True
        return True

    def beat(self) -> None:
        if self.held:
            os.utime(self.path, None)

    def release(self) -> None:
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.held = False
