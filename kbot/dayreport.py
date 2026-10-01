"""End-of-day report straight from the trade database (data/kbot.db): works after the bot has stopped.

  python -m kbot report                 # today (UTC)
  python -m kbot report --day 2026-10-01 [--mode demo]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Optional


def _day_bounds(day: Optional[str]) -> tuple:
    d = dt.datetime.strptime(day, "%Y-%m-%d").date() if day else dt.datetime.now(dt.timezone.utc).date()
    start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    return d.isoformat(), int(start.timestamp() * 1000), int((start + dt.timedelta(days=1)).timestamp() * 1000)


def build_day_report(db_path: str, day: Optional[str] = None, mode: Optional[str] = None) -> dict:
    label, lo, hi = _day_bounds(day)
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(db_path)
    try:
        runs = {r[0] for r in con.execute("SELECT run_id FROM runs WHERE (? IS NULL OR mode = ?)", (mode, mode))}
        ph = lambda: ",".join("?" * len(runs)) or "''"  # noqa: E731
        rl = list(runs)
        settles = []
        for ts, market, pnl, detail in con.execute(
                f"SELECT ts_ms, market, pnl, detail FROM pnl_events WHERE kind='settle' AND ts_ms>=? AND ts_ms<? "
                f"AND run_id IN ({ph()}) ORDER BY ts_ms", [lo, hi] + rl):
            d = json.loads(detail or "{}")
            d.update(ts_ms=ts, market=market, total_pnl=pnl)
            settles.append(d)
        fills = con.execute(
            f"SELECT liquidity, COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(fee),0) FROM fills "
            f"WHERE ts_ms>=? AND ts_ms<? AND run_id IN ({ph()}) GROUP BY liquidity", [lo, hi] + rl).fetchall()
        events = dict(con.execute(
            f"SELECT kind, COUNT(*) FROM risk_events WHERE ts_ms>=? AND ts_ms<? AND run_id IN ({ph()}) "
            f"GROUP BY kind", [lo, hi] + rl).fetchall())
    finally:
        con.close()
    net = sum(s["total_pnl"] for s in settles)
    paired_sh = sum(s.get("paired_shares", 0) or 0 for s in settles)
    paired_cap = sum(s.get("paired_capital", 0) or 0 for s in settles)
    resid_cap = sum(s.get("residual_capital", 0) or 0 for s in settles)
    worst = min(settles, key=lambda s: s["total_pnl"]) if settles else None
    best = max(settles, key=lambda s: s["total_pnl"]) if settles else None
    cum, peak, dd = 0.0, 0.0, 0.0
    for s in settles:
        cum += s["total_pnl"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {
        "day": label, "mode": mode or "all", "markets_settled": len(settles), "net_pnl": round(net, 2),
        "paired_pnl": round(sum(s.get("paired_pnl", 0) or 0 for s in settles), 2),
        "residual_pnl": round(sum(s.get("residual_pnl", 0) or 0 for s in settles), 2),
        "cut_pnl": round(sum(s.get("cut_pnl", 0) or 0 for s in settles), 2),
        "fees": round(sum(s.get("fees", 0) or 0 for s in settles), 2),
        "pair_edge_cents": round(100 * (paired_sh - paired_cap) / paired_sh, 2) if paired_sh > 0 else None,
        "pct_capital_paired": round(100 * paired_cap / (paired_cap + resid_cap), 1) if paired_cap + resid_cap else None,
        "win_rate": round(100 * sum(1 for s in settles if s["total_pnl"] > 0) / len(settles), 1) if settles else None,
        "max_drawdown": round(dd, 2),
        "best": {"market": best["market"], "pnl": round(best["total_pnl"], 2)} if best else None,
        "worst": {"market": worst["market"], "pnl": round(worst["total_pnl"], 2)} if worst else None,
        "fills": {liq: {"n": n, "contracts": round(sz, 1), "fees": round(fee, 2)} for liq, n, sz, fee in fills},
        "risk_events": events,
    }


def format_day_report(r: dict) -> str:
    L = [f"== Daily report {r['day']} ({r['mode']}) ==",
         f"markets settled {r['markets_settled']}  |  NET {r['net_pnl']:+.2f}  |  win rate {r['win_rate']}%  |  "
         f"max drawdown {r['max_drawdown']:+.2f}",
         f"paired {r['paired_pnl']:+.2f} | residual {r['residual_pnl']:+.2f} | cuts {r['cut_pnl']:+.2f} | "
         f"fees -{r['fees']:.2f}",
         f"pair edge {r['pair_edge_cents']}c | capital paired {r['pct_capital_paired']}%"]
    if r["best"]:
        L.append(f"best {r['best']['market']} {r['best']['pnl']:+.2f} | worst {r['worst']['market']} "
                 f"{r['worst']['pnl']:+.2f}")
    for liq, v in r["fills"].items():
        L.append(f"{liq} fills: {v['n']} ({v['contracts']} contracts, fees {v['fees']:.2f})")
    if r["risk_events"]:
        L.append("risk events: " + ", ".join(f"{k} x{v}" for k, v in sorted(r["risk_events"].items())))
    return "\n".join(L)


def summary_line(r: dict) -> str:
    """One compact line for the alert channel."""
    return (f"Daily {r['day']}: net {r['net_pnl']:+.2f} over {r['markets_settled']} markets "
            f"(paired {r['paired_pnl']:+.2f}, residual {r['residual_pnl']:+.2f}, cuts {r['cut_pnl']:+.2f}, "
            f"fees -{r['fees']:.2f}), max DD {r['max_drawdown']:+.2f}")
