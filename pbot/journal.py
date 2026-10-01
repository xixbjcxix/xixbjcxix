"""SQLite journal: every trade and every decision the agent makes, plus the daily report."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional


class Journal:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, day TEXT, symbol TEXT, qty REAL,
            entry REAL, entry_ts TEXT, stop REAL, target REAL, reason TEXT,
            exit REAL, exit_ts TEXT, exit_reason TEXT, pnl REAL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, mode TEXT, level TEXT, msg TEXT);
        """)
        self.db.commit()

    def event(self, ts: datetime, mode: str, level: str, msg: str) -> None:
        self.db.execute("INSERT INTO events (ts, mode, level, msg) VALUES (?,?,?,?)",
                        (ts.isoformat(), mode, level, msg))
        self.db.commit()

    def open_trade(self, mode: str, ts: datetime, symbol: str, qty: float, entry: float,
                   stop: float, target: float, reason: str) -> int:
        cur = self.db.execute(
            "INSERT INTO trades (mode, day, symbol, qty, entry, entry_ts, stop, target, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (mode, ts.date().isoformat(), symbol, qty, entry, ts.isoformat(), stop, target, reason))
        self.db.commit()
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, ts: datetime, exit_px: float, reason: str,
                    qty: Optional[float] = None) -> float:
        row = self.db.execute("SELECT qty, entry FROM trades WHERE id=?", (trade_id,)).fetchone()
        q = qty if qty is not None else row["qty"]
        pnl = round((exit_px - row["entry"]) * q, 4)
        self.db.execute("UPDATE trades SET exit=?, exit_ts=?, exit_reason=?, pnl=?, qty=? WHERE id=?",
                        (exit_px, ts.isoformat(), reason, pnl, q, trade_id))
        self.db.commit()
        return pnl

    def open_trades(self, mode: str) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE mode=? AND exit_ts IS NULL",
                               (mode,)).fetchall()

    def trades(self, mode: str, day: Optional[date] = None) -> List[sqlite3.Row]:
        if day:
            return self.db.execute("SELECT * FROM trades WHERE mode=? AND day=? ORDER BY id",
                                   (mode, day.isoformat())).fetchall()
        return self.db.execute("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,)).fetchall()

    def realized(self, mode: str, day: date) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE mode=? AND day=? "
                            "AND exit_ts IS NOT NULL", (mode, day.isoformat())).fetchone()
        return float(r[0])

    def entries_on(self, mode: str, day: date) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM trades WHERE mode=? AND day=?",
                                   (mode, day.isoformat())).fetchone()[0])

    def day_trades_since(self, mode: str, since: date) -> int:
        """Round trips opened and closed on the same day (the FINRA day-trade definition)."""
        return int(self.db.execute(
            "SELECT COUNT(*) FROM trades WHERE mode=? AND day>=? AND exit_ts IS NOT NULL "
            "AND substr(exit_ts,1,10)=day", (mode, since.isoformat())).fetchone()[0])

    def summary(self, mode: str, day: Optional[date] = None) -> Dict[str, Any]:
        rows = [r for r in self.trades(mode, day) if r["exit_ts"]]
        wins = [r["pnl"] for r in rows if r["pnl"] > 0]
        losses = [r["pnl"] for r in rows if r["pnl"] < 0]
        eq, peak, dd = 0.0, 0.0, 0.0
        for r in rows:
            eq += r["pnl"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        return {
            "trades": len(rows), "wins": len(wins), "losses": len(losses),
            "scratches": len(rows) - len(wins) - len(losses),
            "win_rate": (len(wins) / len(rows) * 100) if rows else 0.0,
            "net": round(sum(r["pnl"] for r in rows), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "max_drawdown": round(dd, 2),
        }

    def report(self, mode: str, day: Optional[date] = None) -> str:
        lines = []
        title = f"pbot report - mode={mode}" + (f" - {day.isoformat()}" if day else " - all time")
        lines.append(title)
        lines.append("-" * len(title))
        for r in self.trades(mode, day):
            status = (f"exit {r['exit']:.2f} ({r['exit_reason']})  pnl {r['pnl']:+.2f}"
                      if r["exit_ts"] else "OPEN")
            lines.append(f"{r['day']} {r['symbol']:<6} {r['qty']:>7g} @ {r['entry']:.2f} "
                         f"stop {r['stop']:.2f} tgt {r['target']:.2f}  {status}")
        s = self.summary(mode, day)
        lines.append("")
        lines.append(f"closed trades {s['trades']}  wins {s['wins']}  losses {s['losses']}  "
                     f"scratches {s['scratches']}  win rate {s['win_rate']:.0f}%")
        lines.append(f"net P&L {s['net']:+.2f}  avg win {s['avg_win']:+.2f}  "
                     f"avg loss {s['avg_loss']:+.2f}  max drawdown {s['max_drawdown']:.2f}")
        return "\n".join(lines)
