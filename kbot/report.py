"""Backtest / paper-session metrics."""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple


def _q(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def _r(x, n=4):
    return None if x is None else round(x, n)


def build_report(core, span_ms: Tuple[Optional[int], Optional[int]], label: str = "") -> dict:
    S = core.settlements
    fills = core.fills
    hours = ((span_ms[1] - span_ms[0]) / 3.6e6) if span_ms[0] and span_ms[1] else None
    traded = [s for s in S if s["n_fills"] > 0]

    paired_sh = sum(s["paired_shares"] for s in S)
    paired_cap = sum(s["paired_capital"] for s in S)
    avg_pair_cost = paired_cap / paired_sh if paired_sh > 0 else None
    residual_cap = sum(s["residual_capital"] for s in S)
    fees = sum(s["fees"] for s in S)
    rebates = sum(s["rebates"] for s in S)
    paired_pnl = sum(s["paired_pnl"] for s in S)
    residual_pnl = sum(s["residual_pnl"] for s in S)
    cut_pnl = sum(s["cut_pnl"] for s in S)
    net = sum(s["total_pnl"] for s in S)
    rescue_excess = sum(s["rescue_excess"] for s in S)

    # equity curve / drawdown in settlement order
    eq, peak, mdd = 0.0, 0.0, 0.0
    for s in sorted(S, key=lambda s: s["settled_ms"]):
        eq += s["total_pnl"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)

    # one-sided legs
    eps = [e for s in S for e in s["episodes"]]
    durs = [(e[1] - e[0]) / 1000.0 for e in eps if e[1] is not None]
    closed_by = Counter(e[4] for e in eps)
    mk_resid_at_res = sum(1 for s in traded if s["residual_shares"] > 1e-9)
    resid_pnls = [s["residual_pnl"] for s in traded if s["residual_shares"] > 1e-9]

    # adverse selection on maker buys
    adv: Dict[int, List[float]] = defaultdict(list)
    for p in core.probes:
        if p.liquidity != "maker" or p.side != "BUY":
            continue
        for h, mid in p.after.items():
            if mid is not None:
                adv[h].append((mid - p.price) * 100.0)

    per_bucket: Dict[str, dict] = {}
    for s in S:
        k = f"{s['asset']}-{'5m' if s['horizon_s'] == 300 else '15m'}"
        b = per_bucket.setdefault(k, {"markets": 0, "traded": 0, "pnl": 0.0, "paired_shares": 0.0,
                                      "paired_capital": 0.0, "residual_pnl": 0.0, "fees": 0.0})
        b["markets"] += 1
        b["traded"] += 1 if s["n_fills"] else 0
        b["pnl"] += s["total_pnl"]
        b["paired_shares"] += s["paired_shares"]
        b["paired_capital"] += s["paired_capital"]
        b["residual_pnl"] += s["residual_pnl"]
        b["fees"] += s["fees"]
    for b in per_bucket.values():
        b["pair_edge_c"] = _r(100 * (1 - b["paired_capital"] / b["paired_shares"]), 2) if b["paired_shares"] else None
        b["pnl"] = _r(b["pnl"], 2)
        b["residual_pnl"] = _r(b["residual_pnl"], 2)
        b["fees"] = _r(b["fees"], 2)
        del b["paired_capital"]

    mkt_pnls = [s["total_pnl"] for s in traded]
    n_maker = sum(1 for f in fills if f.liquidity == "maker")
    n_taker = len(fills) - n_maker
    rej = Counter(r[2].split(" ")[0] for r in core.risk.rejects)

    open_mk = [st for st in core.active_markets() if st.inv.fills]
    open_summary = {
        "markets": len(open_mk),
        "paired_shares": _r(sum(st.inv.paired for st in open_mk), 1),
        "residual_usd": _r(sum(st.inv.residual_cost_usd for st in open_mk), 2),
        "mtm_pnl": _r(sum(st.inv.mark_to_market(core.books.book(st.info.token_up).mid(),
                                                  core.books.book(st.info.token_down).mid()) for st in open_mk), 2),
    }
    return {
        "label": label,
        "unsettled_positions": open_summary,
        "fee_profile": core.cfg.fees.profile,
        "hours": _r(hours, 2),
        "markets_seen": len(S),
        "markets_traded": len(traded),
        "fills": len(fills),
        "maker_fills": n_maker,
        "taker_fills": n_taker,
        "fills_per_hour": _r(len(fills) / hours, 1) if hours else None,
        "shares_bought": _r(sum(f.size for f in fills if f.side == "BUY"), 1),
        "avg_pair_cost": _r(avg_pair_cost, 4),
        "pair_edge_cents": _r(100 * (1 - avg_pair_cost), 2) if avg_pair_cost is not None else None,
        # edge per pair AFTER all fees of the session, spread over paired contracts
        "pair_edge_after_fees_cents": _r(100 * (1 - avg_pair_cost) - 100 * fees / paired_sh, 2)
        if avg_pair_cost is not None and paired_sh else None,
        "paired_shares": _r(paired_sh, 1),
        "capital_paired_usd": _r(paired_cap, 2),
        "capital_residual_usd": _r(residual_cap, 2),
        "pct_capital_paired": _r(100 * paired_cap / (paired_cap + residual_cap), 1) if paired_cap + residual_cap else None,
        "paired_pnl": _r(paired_pnl, 2),
        "residual_pnl": _r(residual_pnl, 2),
        "cut_pnl": _r(cut_pnl, 2),
        "fees_paid": _r(fees, 2),
        "maker_fees": _r(sum(f.fee for f in fills if f.liquidity == "maker"), 2),
        "taker_fees": _r(sum(f.fee for f in fills if f.liquidity == "taker"), 2),
        "fees_per_pair_cents": _r(100 * fees / paired_sh, 2) if paired_sh else None,
        "maker_rebates_est": _r(rebates, 2),
        "net_pnl": _r(net, 2),
        "net_pnl_ex_rebates": _r(net - rebates, 2),
        # rough 1-sigma noise band on net PnL (per-market sd * sqrt(markets)); treat |net| < 2*se as zero
        "net_pnl_se": _r(statistics.pstdev(mkt_pnls) * math.sqrt(len(mkt_pnls)), 2) if len(mkt_pnls) > 1 else None,
        "max_drawdown": _r(mdd, 2),
        "pnl_per_traded_market_mean": _r(statistics.mean(mkt_pnls), 3) if mkt_pnls else None,
        "pnl_per_traded_market_stdev": _r(statistics.pstdev(mkt_pnls), 3) if len(mkt_pnls) > 1 else None,
        "worst_market_pnl": _r(min(mkt_pnls), 2) if mkt_pnls else None,
        "best_market_pnl": _r(max(mkt_pnls), 2) if mkt_pnls else None,
        "legging": {
            "episodes": len(eps),
            "pct_legs_completed": _r(100 * (closed_by.get("passive", 0) + closed_by.get("taker_complete", 0)) / len(eps), 1) if eps else None,
            "pct_legs_over_30s": _r(100 * sum(1 for d in durs if d > 30) / len(durs), 1) if durs else None,
            "pct_legs_over_60s": _r(100 * sum(1 for d in durs if d > 60) / len(durs), 1) if durs else None,
            "pct_traded_markets_resolving_with_residual": _r(100 * mk_resid_at_res / len(traded), 1) if traded else None,
            "median_time_one_sided_s": _r(_q(durs, 0.5), 1),
            "p90_time_one_sided_s": _r(_q(durs, 0.9), 1),
            "closed_by": dict(closed_by),
            "residual_pnl_when_held": _r(sum(resid_pnls), 2),
            "residual_win_rate_pct": _r(100 * sum(1 for x in resid_pnls if x > 0) / len(resid_pnls), 1) if resid_pnls else None,
            "rescue_excess_paid": _r(rescue_excess, 2),
            "cost_of_legging": _r(-(residual_pnl + cut_pnl) + rescue_excess, 2),
        },
        "adverse_selection_cents": {
            f"{h}s": {"mean": _r(statistics.mean(v), 2), "pct_adverse": _r(100 * sum(1 for x in v if x < 0) / len(v), 1),
                      "n": len(v)}
            for h, v in sorted(adv.items()) if v
        },
        "by_bucket": per_bucket,
        "risk_rejects": dict(rej),
        "postonly_or_venue_rejects": core.exchange.rejections,
        "data_gaps": len(core.gaps),
        "winners_inferred_from_spot": core.inferred_winners,
        "daily_loss_halts": len(core.risk.halts),
        "killed": core.risk.kill_reason or None,
    }


def format_report(r: dict) -> str:
    L = []
    L.append(f"== {r['label']}  (fees: {r['fee_profile']}) ==")
    L.append(f"span {r['hours']} h | markets {r['markets_seen']} (traded {r['markets_traded']}) | "
             f"fills {r['fills']} ({r['maker_fills']} maker / {r['taker_fills']} taker) | {r['fills_per_hour']}/h")
    L.append(f"avg pair cost {r['avg_pair_cost']} -> edge {r['pair_edge_cents']}c before fees, "
             f"{r.get('pair_edge_after_fees_cents')}c after fees | paired {r['paired_shares']} sh | "
             f"capital paired ${r['capital_paired_usd']} vs residual ${r['capital_residual_usd']} ({r['pct_capital_paired']}% paired)")
    L.append(f"PnL: paired {r['paired_pnl']} | residual {r['residual_pnl']} | cuts {r['cut_pnl']} | fees -{r['fees_paid']} "
             f"(maker {r.get('maker_fees')}, taker {r.get('taker_fees')}) | NET {r['net_pnl']} ±{r.get('net_pnl_se')} | maxDD {r['max_drawdown']}")
    lg = r["legging"]
    L.append(f"legging: {lg['episodes']} one-sided legs, {lg['pct_legs_completed']}% completed into pairs, "
             f"{lg['pct_legs_over_60s']}% one-sided >60s; {lg['pct_traded_markets_resolving_with_residual']}% of traded markets "
             f"resolved with residual; median {lg['median_time_one_sided_s']}s "
             f"one-sided (p90 {lg['p90_time_one_sided_s']}s); closed_by {lg['closed_by']}; cost of legging ${lg['cost_of_legging']}")
    L.append(f"adverse selection (mid move after resting-bid fill, cents): {r['adverse_selection_cents']}")
    L.append(f"by bucket: {r['by_bucket']}")
    if r.get("unsettled_positions", {}).get("markets"):
        u = r["unsettled_positions"]
        L.append(f"unsettled: {u['markets']} markets, {u['paired_shares']} paired sh, residual ${u['residual_usd']}, MtM {u['mtm_pnl']}")
    L.append(f"risk rejects {r['risk_rejects']} | venue rejects {r['postonly_or_venue_rejects']} | data gaps {r['data_gaps']} | "
             f"daily-loss halts {r.get('daily_loss_halts')} | winners inferred {r.get('winners_inferred_from_spot')}")
    return "\n".join(L)
