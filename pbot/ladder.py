"""Start small and build: a size ladder that has to be earned.

Each level is a set of risk limits. Every account (paper / live) starts at level 0 ("micro").
  * Promotion is MANUAL (`python -m pbot promote`) and only allowed once the record at the
    current level meets every criterion: enough trades, enough sessions, profitable, a profit
    factor above the bar, and no deep drawdown.
  * Demotion is AUTOMATIC: a drawdown of `demote_drawdown_r` x the level's risk-per-trade since
    reaching the level drops it one level at the next pre-flight.
Live trading also requires the paper record to have passed the level-0 criteria first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import BotConfig, LadderConfig
from .journal import Journal


@dataclass
class LevelStats:
    trades: int
    days: int
    net: float
    profit_factor: float
    max_drawdown: float          # <= 0, in dollars
    wins: int
    losses: int


class Ladder:
    def __init__(self, cfg: LadderConfig, journal: Journal):
        self.cfg = cfg
        self.j = journal
        self.j.db.executescript("""
        CREATE TABLE IF NOT EXISTS ladder_state (mode TEXT PRIMARY KEY, level INTEGER, since TEXT);
        CREATE TABLE IF NOT EXISTS ladder_log (ts TEXT, mode TEXT, from_level INTEGER,
                                               to_level INTEGER, reason TEXT);
        """)
        self.j.db.commit()

    # ---- state ------------------------------------------------------------------------------
    def state(self, mode: str, now: datetime) -> Tuple[int, str]:
        row = self.j.db.execute("SELECT level, since FROM ladder_state WHERE mode=?", (mode,)).fetchone()
        if row:
            return min(int(row["level"]), len(self.cfg.levels) - 1), row["since"]
        self._set(mode, 0, now, "start")
        return 0, now.isoformat()

    def _set(self, mode: str, level: int, now: datetime, reason: str, old: Optional[int] = None) -> None:
        self.j.db.execute("INSERT INTO ladder_state (mode, level, since) VALUES (?,?,?) "
                          "ON CONFLICT(mode) DO UPDATE SET level=excluded.level, since=excluded.since",
                          (mode, level, now.isoformat()))
        self.j.db.execute("INSERT INTO ladder_log VALUES (?,?,?,?,?)",
                          (now.isoformat(), mode, old, level, reason))
        self.j.db.commit()

    def level_cfg(self, level: int) -> Dict[str, Any]:
        return self.cfg.levels[level]

    def name(self, level: int) -> str:
        return str(self.cfg.levels[level].get("name", f"level {level}"))

    # ---- record -----------------------------------------------------------------------------
    def stats(self, mode: str, since: str) -> LevelStats:
        rows = self.j.db.execute(
            "SELECT day, pnl FROM trades WHERE mode=? AND exit_ts IS NOT NULL AND exit_ts>=? "
            "AND exit_reason NOT LIKE 'closed outside the bot%' ORDER BY exit_ts", (mode, since)).fetchall()
        gains = sum(r["pnl"] for r in rows if r["pnl"] > 0)
        losses = -sum(r["pnl"] for r in rows if r["pnl"] < 0)
        eq = peak = dd = 0.0
        for r in rows:
            eq += r["pnl"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        pf = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
        return LevelStats(len(rows), len({r["day"] for r in rows}), round(eq, 2), pf, round(dd, 2),
                          sum(1 for r in rows if r["pnl"] > 0), sum(1 for r in rows if r["pnl"] < 0))

    def criteria(self, mode: str, now: datetime) -> Tuple[bool, List[Tuple[str, bool]], LevelStats, int]:
        level, since = self.state(mode, now)
        s = self.stats(mode, since)
        risk = float(self.level_cfg(level)["risk_per_trade_usd"])
        dd_cap = self.cfg.demote_drawdown_r * risk
        checks = [
            (f"closed trades {s.trades} >= {self.cfg.promote_min_trades}", s.trades >= self.cfg.promote_min_trades),
            (f"trading sessions {s.days} >= {self.cfg.promote_min_days}", s.days >= self.cfg.promote_min_days),
            (f"net P&L {s.net:+.2f} > 0", s.net > 0),
            (f"profit factor {s.profit_factor:.2f} >= {self.cfg.promote_min_profit_factor}",
             s.profit_factor >= self.cfg.promote_min_profit_factor),
            (f"max drawdown {s.max_drawdown:.2f} better than -{dd_cap * 0.5:.2f} (half the demotion line)",
             s.max_drawdown > -dd_cap * 0.5),
        ]
        return all(ok for _, ok in checks), checks, s, level

    # ---- transitions ------------------------------------------------------------------------
    def check_demotion(self, mode: str, now: datetime) -> Optional[str]:
        level, since = self.state(mode, now)
        if level == 0:
            return None
        s = self.stats(mode, since)
        cap = self.cfg.demote_drawdown_r * float(self.level_cfg(level)["risk_per_trade_usd"])
        if s.max_drawdown <= -cap:
            why = (f"drawdown {s.max_drawdown:.2f} reached -{cap:.2f} "
                   f"({self.cfg.demote_drawdown_r:g}R) at {self.name(level)}")
            self._set(mode, level - 1, now, "auto demote: " + why, level)
            return f"demoted {self.name(level)} -> {self.name(level - 1)}: {why}"
        return None

    def promote(self, mode: str, now: datetime, force: bool = False) -> Tuple[bool, str]:
        ok, checks, _, level = self.criteria(mode, now)
        if level >= len(self.cfg.levels) - 1:
            return False, f"already at the top level ({self.name(level)})"
        if not ok and not force:
            missing = "; ".join(t for t, good in checks if not good)
            return False, f"not yet: {missing}"
        self._set(mode, level + 1, now, "promote" + (" (forced)" if force and not ok else ""), level)
        return True, f"promoted {self.name(level)} -> {self.name(level + 1)}"

    def demote(self, mode: str, now: datetime) -> str:
        level, _ = self.state(mode, now)
        if level == 0:
            return "already at the bottom level"
        self._set(mode, level - 1, now, "manual demote", level)
        return f"demoted {self.name(level)} -> {self.name(level - 1)}"

    def apply(self, cfg: BotConfig, mode: str, now: datetime) -> int:
        """Overwrite cfg.risk with the current level's limits (in place). Returns the level."""
        level, _ = self.state(mode, now)
        for k, v in self.level_cfg(level).items():
            if k != "name":
                setattr(cfg.risk, k, v)
        return level

    def paper_ready_for_live(self, now: datetime) -> Tuple[bool, List[Tuple[str, bool]]]:
        """Live needs the paper record to have passed level 0 (promoted at least once, or passing now)."""
        level, _ = self.state("paper", now)
        if level >= 1:
            return True, [(f"paper is at {self.name(level)}", True)]
        ok, checks, _, _ = self.criteria("paper", now)
        return ok, checks

    def progress_text(self, mode: str, now: datetime) -> str:
        ok, checks, s, level = self.criteria(mode, now)
        lc = self.level_cfg(level)
        lines = [f"{mode}: level {level} '{self.name(level)}' of {len(self.cfg.levels) - 1}",
                 "  limits: " + ", ".join(f"{k}={v}" for k, v in lc.items() if k != "name"),
                 f"  since reaching it: {s.trades} trades over {s.days} sessions, {s.wins}W/{s.losses}L, "
                 f"net {s.net:+.2f}, max drawdown {s.max_drawdown:.2f}",
                 "  promotion criteria:"]
        lines += [f"    [{'x' if good else ' '}] {t}" for t, good in checks]
        if level >= len(self.cfg.levels) - 1:
            lines.append("  top level reached.")
        elif ok:
            lines.append(f"  ready: run  python -m pbot promote --mode {mode}  to move to "
                         f"'{self.name(level + 1)}'")
        cap = self.cfg.demote_drawdown_r * float(lc["risk_per_trade_usd"])
        if level > 0:
            lines.append(f"  auto-demotes if drawdown at this level reaches -{cap:.2f}")
        return "\n".join(lines)
