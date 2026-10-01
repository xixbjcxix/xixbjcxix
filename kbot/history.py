"""`python -m kbot history` - learn from YOUR Kalshi account history.

Pulls your past fills (GET /portfolio/fills, signed, read-only), keeps the crypto 15-minute ones, and:
  1. FEE CALIBRATION   charged fee vs the fee model, per liquidity, plus the implied fee coefficient
                       (charged / (contracts x p x (1-p))). This is the number the pair-cost maths depends on.
  2. MARKET REBUILD    replays your fills per market through the bot's own inventory logic and settles with
                       the official result: paired sets, pair cost, residual, cuts, fees, net PnL.
Nothing here places orders. Raw fills are saved to data/history.db so reruns are incremental and offline.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import statistics
from datetime import datetime
from typing import Dict, List, Optional

from .fees import FeeModel, fee_model_for
from .inventory import MarketInventory
from .models import BUY, DOWN, SELL, UP, Fill, MarketInfo

HIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_fills (
  trade_id TEXT PRIMARY KEY, ts_ms INTEGER, ticker TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS market_results (ticker TEXT PRIMARY KEY, result TEXT);
"""


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts_ms(raw: dict) -> int:
    t = raw.get("created_time") or raw.get("ts")
    if isinstance(t, (int, float)):
        return int(t * 1000 if t < 1e11 else t)
    if t:
        return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000)
    return 0


def parse_fill(raw: dict) -> Optional[Fill]:
    """One Kalshi fill -> our Fill. Tolerates dollar-string, fixed-point and legacy-cent field names."""
    side = str(raw.get("side", "")).lower()
    if side not in ("yes", "no"):
        return None
    outcome = UP if side == "yes" else DOWN
    action = str(raw.get("action", "buy")).lower()
    own = "yes" if side == "yes" else "no"
    price = None
    for k in (f"{own}_price_dollars", f"{own}_price_fixed"):
        if raw.get(k) not in (None, ""):
            price = _f(raw[k])
            break
    if price is None and raw.get(f"{own}_price") not in (None, ""):
        price = _f(raw[f"{own}_price"]) / 100.0
    if price is None:
        other = "no" if side == "yes" else "yes"
        for k in (f"{other}_price_dollars", f"{other}_price_fixed"):
            if raw.get(k) not in (None, ""):
                price = 1.0 - _f(raw[k])
                break
        if price is None and raw.get(f"{other}_price") not in (None, ""):
            price = 1.0 - _f(raw[f"{other}_price"]) / 100.0
    n = _f(raw.get("count_fp", raw.get("count", 0)))
    if price is None or n <= 0:
        return None
    return Fill(order_id=str(raw.get("order_id", "")), market=str(raw.get("ticker", raw.get("market_ticker", ""))),
                outcome=outcome, side=SELL if action == "sell" else BUY, price=round(price, 4), size=n,
                liquidity="taker" if raw.get("is_taker") else "maker", fee=_f(raw.get("fee_cost", 0)), rebate=0.0,
                ts_ms=_ts_ms(raw), tag="history")


class HistoryStore:
    def __init__(self, path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(HIST_SCHEMA)

    def add_fills(self, raws: List[dict]) -> int:
        rows = []
        for r in raws:
            tid = str(r.get("trade_id") or r.get("fill_id") or "")
            if tid:
                rows.append((tid, _ts_ms(r), str(r.get("ticker", "")), json.dumps(r)))
        with self.conn:
            before = self.conn.total_changes
            self.conn.executemany("INSERT OR IGNORE INTO account_fills VALUES (?,?,?,?)", rows)
            return self.conn.total_changes - before

    def last_ts_ms(self) -> Optional[int]:
        r = self.conn.execute("SELECT MAX(ts_ms) FROM account_fills").fetchone()[0]
        return r or None

    def fills(self, prefixes: List[str]) -> List[dict]:
        out = []
        for (raw,) in self.conn.execute("SELECT raw FROM account_fills ORDER BY ts_ms, trade_id"):
            r = json.loads(raw)
            if not prefixes or str(r.get("ticker", "")).startswith(tuple(prefixes)):
                out.append(r)
        return out

    def set_result(self, ticker: str, result: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO market_results VALUES (?,?)", (ticker, result))

    def results(self) -> Dict[str, str]:
        return dict(self.conn.execute("SELECT ticker, result FROM market_results"))

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------------------------
def fee_calibration(fills: List[Fill], fee_for) -> dict:
    """Charged vs modelled fees. `fee_for(ticker) -> FeeModel`. Coefficients are per contract x p(1-p)."""
    out: Dict[str, dict] = {}
    for liq in ("maker", "taker"):
        rows = [f for f in fills if f.liquidity == liq and f.size > 0]
        if not rows:
            continue
        charged = sum(f.fee for f in rows)
        modeled = sum((fee_for(f.market).taker_fee if liq == "taker" else fee_for(f.market).maker_fee)(f.size, f.price)
                      for f in rows)
        # per-fill implied coefficient is noisy (cent rounding): use the ratio of sums over fills with real curve
        curve = sum(f.size * f.price * (1 - f.price) for f in rows)
        zero = sum(1 for f in rows if f.fee <= 0)
        out[liq] = {"fills": len(rows), "contracts": round(sum(f.size for f in rows), 2),
                    "charged": round(charged, 4), "modeled": round(modeled, 4),
                    "drift_pct": round(100 * (charged - modeled) / modeled, 1) if modeled > 0 else None,
                    "implied_coef": round(charged / curve, 5) if curve > 0 else None,
                    "zero_fee_fills": zero,
                    "median_fee_per_contract": round(statistics.median(f.fee / f.size for f in rows), 5)}
    return out


def rebuild_markets(fills: List[Fill], results: Dict[str, str], fee_for) -> List[dict]:
    """Replay fills per market through MarketInventory and settle with the official result when known."""
    by: Dict[str, List[Fill]] = {}
    for f in fills:
        by.setdefault(f.market, []).append(f)
    rows = []
    for ticker, fl in by.items():
        inv = MarketInventory(market=ticker)
        for f in sorted(fl, key=lambda x: x.ts_ms):
            inv.apply_fill(f)
        res = (results.get(ticker) or "").lower()
        winner = UP if res == "yes" else DOWN if res == "no" else None
        row = {"market": ticker, "fills": len(fl), "yes": inv.up, "no": inv.down, "paired": inv.paired,
               "residual": inv.residual, "pair_cost": round(inv.pair_cost, 4) if inv.paired else None,
               "fees": round(inv.fees, 4), "cut_pnl": round(inv.realized, 4), "result": res or "open/unknown",
               "start_ms": fl[0].ts_ms}
        if winner:
            b = inv.settle(winner, fl[-1].ts_ms)
            row.update(paired_pnl=round(b["paired_pnl"], 4), residual_pnl=round(b["residual_pnl"], 4),
                       net_pnl=round(b["total_pnl"], 4))
        else:
            row["net_pnl"] = None
        rows.append(row)
    rows.sort(key=lambda r: r["start_ms"])
    return rows


def summarize(rows: List[dict]) -> dict:
    done = [r for r in rows if r["net_pnl"] is not None]
    paired = sum(r["paired"] for r in done)
    edge = None
    cost = sum((r["pair_cost"] or 0) * r["paired"] for r in done)
    if paired > 0:
        edge = round(100 * (1 - cost / paired), 2)
    cum, peak, dd = 0.0, 0.0, 0.0
    for r in done:
        cum += r["net_pnl"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"markets": len(rows), "settled": len(done), "net_pnl": round(sum(r["net_pnl"] for r in done), 2),
            "paired_pnl": round(sum(r.get("paired_pnl", 0) for r in done), 2),
            "residual_pnl": round(sum(r.get("residual_pnl", 0) for r in done), 2),
            "cut_pnl": round(sum(r["cut_pnl"] for r in done), 2), "fees": round(sum(r["fees"] for r in done), 2),
            "pair_edge_cents": edge, "paired_sets": round(paired, 1), "max_drawdown": round(dd, 2),
            "win_rate": round(100 * sum(1 for r in done if r["net_pnl"] > 0) / len(done), 1) if done else None}


def format_history(cal: dict, summ: dict, model: FeeModel) -> str:
    L = ["== Your account history (crypto 15-minute markets) ==",
         f"{summ['markets']} markets ({summ['settled']} with a known result) | NET {summ['net_pnl']:+.2f} | "
         f"win rate {summ['win_rate']}% | max DD {summ['max_drawdown']:+.2f}",
         f"paired {summ['paired_pnl']:+.2f} ({summ['paired_sets']} sets, edge {summ['pair_edge_cents']}c) | "
         f"residual {summ['residual_pnl']:+.2f} | cuts {summ['cut_pnl']:+.2f} | fees -{summ['fees']:.2f}", "",
         "Fee calibration (charged vs bot's model: " + model.name + "):"]
    for liq, c in cal.items():
        L.append(f"  {liq:6} {c['fills']} fills, {c['contracts']} contracts: charged ${c['charged']:.2f} vs modelled "
                 f"${c['modeled']:.2f} ({c['drift_pct']}%), implied coefficient {c['implied_coef']} "
                 f"(model {model.taker_coef if liq == 'taker' else model.maker_coef:.5f}), "
                 f"{c['zero_fee_fills']} zero-fee fills")
    if not cal:
        L.append("  no fills")
    return "\n".join(L)


def write_csv(rows: List[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    keys = ["market", "fills", "yes", "no", "paired", "residual", "pair_cost", "fees", "cut_pnl", "result",
            "paired_pnl", "residual_pnl", "net_pnl"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


async def pull(rest, store: HistoryStore, prefixes: List[str], since_days: float, now_s: float,
               fetch_results: bool = True) -> dict:
    """Download new fills (incremental) and the official result of each traded market."""
    last = store.last_ts_ms()
    min_ts = int(last / 1000) - 3600 if last else int(now_s - since_days * 86400)
    raws = await rest.get_fills_all(min_ts=min_ts)
    new = store.add_fills(raws)
    tickers = {str(r.get("ticker", "")) for r in store.fills(prefixes)}
    known = store.results()
    got = 0
    if fetch_results:
        for t in sorted(tickers - {k for k, v in known.items() if v}):
            try:
                m = await rest.get_market(t)
            except Exception:  # noqa: BLE001
                continue
            res = str(m.get("result", "") or "").lower()
            if res in ("yes", "no"):
                store.set_result(t, res)
                got += 1
    return {"downloaded": len(raws), "new": new, "markets": len(tickers), "results_fetched": got}
