"""Replay recordings (or synthetic data) through the exact live logic.

Every profile is scored in shadow, and the adaptive selector trades a
simulated account the same way the engine would.  Results can be saved as
the bot's learning state so live trading starts already warmed up.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field

from .adaptive import apply_fill, sim_fill
from .brain import Brain
from .config import Settings
from .market import Book, MarketMeta, Position
from .model import fair_yes
from .strategy import Sizing, decide


@dataclass
class SimAccount:
    cash: float
    positions: dict = field(default_factory=dict)
    memo: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    curve: list = field(default_factory=list)
    markets: list = field(default_factory=list)


def run(s: Settings, records, brain: Brain | None = None, verbose: bool = False) -> dict:
    brain = brain or Brain(s, state_file="")
    acct = SimAccount(cash=s.paper_start_cash)
    metas: dict[str, MarketMeta] = {}
    spot: dict[str, tuple[float, float]] = {}
    sat_out = 0
    for r in records:
        typ, ts = r.get("t"), r.get("ts", 0.0)
        if typ == "spot":
            spot[r["a"]] = (r["px"], ts)
            brain.on_spot(r["a"], ts, r["px"])
        elif typ == "meta":
            metas[r["ticker"]] = MarketMeta(r["ticker"], r.get("a", "btc"), r["open"], r["close"],
                                            r.get("strike"), r.get("stype") or "greater")
        elif typ == "book":
            m = metas.get(r["ticker"])
            if m is None or not (m.open_ts <= ts < m.close_ts):
                continue
            book = Book(Book.normalize(r.get("yes") or []), Book.normalize(r.get("no") or []), ts)
            px, pts = spot.get(m.asset, (None, 0.0))
            if px is not None and ts - pts > s.stale_spot_s:
                px = None
            v = brain.view(ts, m, book, px, pts)
            brain.shadow.step(v, brain.shadow_sizing())
            name, warm = brain.selector.choose(brain.shadow) if s.adaptive else (s.default_profile, False)
            pos = acct.positions.setdefault(m.ticker, Position(m.ticker))
            use = pos.profile if pos.qty > 0 and pos.profile else name
            if use is None:
                sat_out += 1
                continue
            sizing = Sizing(bankroll=min(s.bankroll, max(acct.cash, 0.0)), kelly_fraction=s.kelly_fraction,
                            max_contracts=s.max_contracts_per_order,
                            max_dollars_market=s.max_dollars_per_market, fee_rate=s.taker_fee_rate,
                            max_slippage=s.max_slippage,
                            fixed_contracts=s.warmup_contracts if warm else None)
            exposure = sum(p.cost for p in acct.positions.values())
            it = decide(v, pos, brain.shadow.profiles[use], sizing, acct.memo.setdefault(m.ticker, {}),
                        allow_entry=name is not None)
            if it is None:
                continue
            if it.action == "buy" and exposure + it.qty * it.limit > s.max_dollars_total:
                continue
            q, avg, fee = sim_fill(book, it, s.taker_fee_rate, s.paper_slippage, s.paper_fill_ratio)
            if q <= 0:
                continue
            pnl = apply_fill(pos, it, q, avg, fee, use)
            acct.cash += (-(q * avg + fee)) if it.action == "buy" else (q * avg - fee)
            acct.trades.append({"ts": ts, "ticker": m.ticker, "action": it.action, "side": it.side,
                                "qty": q, "price": round(avg, 3), "reason": it.reason, "profile": use,
                                "pnl": round(pnl, 3)})
        elif typ == "result":
            t, res = r["ticker"], r["result"]
            if res not in ("yes", "no"):
                continue
            pos = acct.positions.pop(t, None)
            acct.memo.pop(t, None)
            if pos is not None:
                won = pos.side == res
                qty = pos.qty
                pos.settle(res)
                if qty > 0 and won:
                    acct.cash += qty
                if pos.entries:
                    acct.markets.append({"ticker": t, "pnl": round(pos.realized, 3), "profile": pos.profile})
            shadow = brain.settle(t, res)
            acct.curve.append(acct.cash)
            metas.pop(t, None)
            if verbose:
                best = max(shadow, key=shadow.get)
                print(f"{t} -> {res}  cash {acct.cash:8.2f}  best shadow {best} {shadow[best]:+.2f}")
    return report(s, brain, acct, sat_out)


def max_drawdown(curve: list[float], start: float) -> float:
    peak, dd = start, 0.0
    for x in curve:
        peak = max(peak, x)
        dd = max(dd, peak - x)
    return dd


def report(s: Settings, brain: Brain, acct: SimAccount, sat_out: int) -> dict:
    profs = {}
    for n, st in brain.shadow.stats.items():
        profs[n] = {"markets": st.n, "traded": st.traded, "wins": st.wins, "losses": st.losses,
                    "total": round(st.total, 2), "per_market": round(st.total / st.n, 3) if st.n else 0.0,
                    "score": round(st.score(s.score_k), 3)}
    wins = sum(1 for m in acct.markets if m["pnl"] > 0)
    return {
        "adaptive": {"start": s.paper_start_cash, "end": round(acct.cash, 2),
                     "pnl": round(acct.cash - s.paper_start_cash, 2),
                     "markets_traded": len(acct.markets), "markets_won": wins,
                     "trades": len(acct.trades),
                     "max_drawdown": round(max_drawdown(acct.curve, s.paper_start_cash), 2),
                     "final_profile": brain.selector.current, "selector": brain.selector.reason},
        "profiles": profs,
        "calibrator": brain.cal.to_dict(),
    }


def print_report(rep: dict) -> None:
    a = rep["adaptive"]
    print("\n=== adaptive (what the bot would have done) ===")
    print(f"  ${a['start']:.2f} -> ${a['end']:.2f}  ({a['pnl']:+.2f})   markets traded {a['markets_traded']}"
          f" (won {a['markets_won']})   trades {a['trades']}   max drawdown ${a['max_drawdown']:.2f}")
    print(f"  selector: {a['selector']}")
    print("\n=== every profile, head to head (shadow) ===")
    print(f"  {'profile':<15}{'markets':>8}{'traded':>8}{'won':>6}{'lost':>6}{'total $':>10}{'$/mkt':>8}{'score':>8}")
    for n, p in sorted(rep["profiles"].items(), key=lambda kv: -kv[1]["score"]):
        print(f"  {n:<15}{p['markets']:>8}{p['traded']:>8}{p['wins']:>6}{p['losses']:>6}"
              f"{p['total']:>10.2f}{p['per_market']:>8.3f}{p['score']:>8.3f}")
    c = rep["calibrator"]
    print(f"\n  calibration: a={c['a']:.3f} b={c['b']:+.3f} after {c['n']} markets "
          "(a<1 means the raw model was over-confident)")


# ====================================================================== synthetic
def synthetic(n_markets: int = 96, seed: int = 1, edge: str = "lag", sigma: float = 9e-5,
              start_px: float = 90000.0, window: float = 60.0):
    """Generate fake recordings.  edge="lag": the market maker prices off a
    stale spot (the bot can beat that).  edge="none": the maker knows the
    true fair value (the bot should not make money and should sit out).

    Synthetic results only prove the plumbing works - not that the bot will
    be profitable on Kalshi."""
    rng = random.Random(seed)
    px = start_px
    hist: list[float] = []
    t0 = 1_700_000_000.0
    vol = sigma
    for k in range(n_markets):
        open_ts = t0 + k * 900
        close_ts = open_ts + 900
        ticker = f"SYN-{k:04d}"
        strike = round(px, 2)
        yield {"t": "meta", "ts": open_ts, "ticker": ticker, "a": "btc", "open": open_ts,
               "close": close_ts, "strike": strike, "stype": "greater"}
        if rng.random() < 0.1:                       # occasional volatility regime change
            vol = sigma * rng.choice([0.6, 1.0, 1.5, 2.0])
        win_sum, win_n = 0.0, 0
        lag = rng.randint(4, 15)
        for i in range(900):
            ts = open_ts + i
            px *= math.exp(vol * rng.gauss(0, 1) - 0.5 * vol * vol)
            hist.append(px)
            yield {"t": "spot", "ts": ts, "a": "btc", "px": round(px, 2)}
            tau = close_ts - ts
            if tau <= window:
                win_sum += px
                win_n += 1
            if edge == "lag":
                ref = hist[-1 - min(lag, len(hist) - 1)]
                fair = fair_yes(ref, strike, tau, sigma, window,
                                window_avg=(win_sum / win_n) if win_n else None,
                                window_elapsed=window - tau if win_n else 0.0)
                fair = min(0.99, max(0.01, fair + rng.gauss(0, 0.03)))
            else:
                fair = fair_yes(px, strike, tau, vol, window,
                                window_avg=(win_sum / win_n) if win_n else None,
                                window_elapsed=window - tau if win_n else 0.0)
            half = rng.choice([0.01, 0.01, 0.02, 0.03])
            yb = math.floor((fair - half) * 100) / 100
            nb = math.floor((1 - fair - half) * 100) / 100
            yes = [[round(yb - 0.01 * j, 2), rng.randint(5, 60)] for j in range(3) if yb - 0.01 * j >= 0.01]
            no = [[round(nb - 0.01 * j, 2), rng.randint(5, 60)] for j in range(3) if nb - 0.01 * j >= 0.01]
            yield {"t": "book", "ts": ts + 0.5, "ticker": ticker, "yes": yes, "no": no}
        avg = win_sum / max(win_n, 1)
        yield {"t": "result", "ts": close_ts + 1, "ticker": ticker, "result": "yes" if avg > strike else "no"}


def save_report(rep: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
