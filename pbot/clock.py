"""Market calendar and clocks. Everything is in exchange time (America/New_York)."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import SessionConfig


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


class MarketCalendar:
    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.holidays = {date.fromisoformat(d) for d in cfg.holidays}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def at(self, d: date, hhmm: str) -> datetime:
        h, m = _hm(hhmm)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)

    def open_time(self, d: date) -> datetime:
        return self.at(d, self.cfg.open)

    def close_time(self, d: date) -> datetime:
        return self.at(d, self.cfg.early_closes.get(d.isoformat(), self.cfg.close))

    def range_end(self, d: date) -> datetime:
        return self.open_time(d) + timedelta(minutes=self.cfg.opening_range_minutes)

    def entry_cutoff(self, d: date) -> datetime:
        return min(self.at(d, self.cfg.entry_cutoff), self.close_time(d) - timedelta(minutes=30))

    def flatten_time(self, d: date) -> datetime:
        return min(self.at(d, self.cfg.flatten_at), self.close_time(d) - timedelta(minutes=5))

    def next_trading_day(self, d: date, include_today: bool = True) -> date:
        cur = d if include_today else d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def business_days_back(self, d: date, n: int) -> date:
        """Date of the n-th trading day ending at d (d counts as day 1)."""
        cur, count = d, 0
        while True:
            if self.is_trading_day(cur):
                count += 1
                if count >= n:
                    return cur
            cur -= timedelta(days=1)


class SystemClock:
    def __init__(self, tz: ZoneInfo):
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def sleep_until(self, when: datetime, chunk: float = 60.0) -> None:
        while True:
            left = (when - self.now()).total_seconds()
            if left <= 0:
                return
            self.sleep(min(left, chunk))


class SimClock(SystemClock):
    """Virtual time for `sim` mode and tests: sleep() advances the clock instantly."""

    def __init__(self, start: datetime):
        super().__init__(start.tzinfo)  # type: ignore[arg-type]
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def set(self, when: datetime, *, only_forward: bool = True) -> None:
        if not only_forward or when > self._now:
            self._now = when


def minutes_since(start: datetime, now: Optional[datetime]) -> float:
    return ((now or start) - start).total_seconds() / 60.0
