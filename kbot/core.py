"""TradingCore: the event-driven heart shared by backtest and paper trading.

Pipeline per event:
  raw frame -> BookStore (books/trades) -> SimExchange (fills) -> inventory/store
            -> Strategy.plan() for touched markets (throttled) -> RiskManager -> exchange

The exchange is pluggable: SimExchange (backtest, sim paper) or KalshiDemoExchange (real orders
on Kalshi's demo environment). Both expose submit / cancel / advance / active_orders.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .book import BookEvent, KalshiBookStore, ResolvedEvent
from .config import BotConfig
from .fees import FeeModel, fee_model_for
from .fillmodel import SimExchange
from .inventory import MarketInventory
from .ops import FeeAudit, PnlHistory
from .models import BUY, DOWN, OUTCOMES, SELL, UP, Fill, MarketInfo, Order, OrderStatus, Trade, other, px
from .risk import RiskManager
from .signal import MomentumSignal, SignalReading, SpotTracker
from .store import Store
from .strategy import Plan, Strategy


@dataclass
class MarketState:
    info: MarketInfo
    inv: MarketInventory
    fee: FeeModel
    last_tick_ms: int = 0
    dirty: bool = True
    settled: bool = False
    last_plan: Optional[Plan] = None
    last_signal: Optional[SignalReading] = None
    tob: Dict[str, Tuple[Optional[float], Optional[float]]] = field(default_factory=dict)
    settle_breakdown: Optional[dict] = None


@dataclass
class AdverseProbe:
    token: str
    price: float
    side: str
    ts_ms: int
    liquidity: str
    tag: str
    after: Dict[int, Optional[float]] = field(default_factory=dict)  # horizon_s -> mid


class TradingCore:
    ADVERSE_HORIZONS = (10, 30)

    def __init__(self, cfg: BotConfig, store: Store, run_id: str, mode: str = "backtest",
                 exchange_factory: Optional[Callable[["TradingCore"], object]] = None) -> None:
        self.cfg = cfg
        self.store = store
        self.run_id = run_id
        self.mode = mode
        self.books = KalshiBookStore()
        self.spot = SpotTracker()
        self.signal = MomentumSignal(self.spot, cfg.signal)
        self.strategy = Strategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk)
        self.markets: Dict[str, MarketState] = {}
        self._upcoming: List[MarketState] = []      # sorted by start; not yet in the live set
        self._live: Dict[str, MarketState] = {}      # markets within [start - 2 min, settlement]
        self.token_to_market: Dict[str, str] = {}
        self.cond_to_market: Dict[str, str] = {}
        if exchange_factory is not None:
            self.exchange = exchange_factory(self)          # e.g. KalshiDemoExchange (real demo orders)
        else:
            self.exchange = SimExchange(self.books, cfg.fill_model, self._fee_for_market, self._market_for_token)
        self.exchange.on_final = lambda o: self.store.order(self.run_id, o, o.created_ms)  # ts = placement time
        self.now_ms = 0
        self.data_ok = True
        self.data_gap_reason = ""
        self.spot_gap = False
        self.last_pm_ms = 0
        self.gaps: List[Tuple[int, str]] = []
        self.fills: List[Fill] = []
        self.settlements: List[dict] = []
        self.probes: List[AdverseProbe] = []
        self._open_probes: Dict[str, List[AdverseProbe]] = {}
        self.day_start_ms = 0
        self.day_realized = 0.0
        self.on_fill_hooks: List[Callable[[Fill], None]] = []
        self._backoff: Dict[Tuple[str, str], int] = {}
        self.inferred_winners = 0
        self.status_message = ""          # shown at the top of the dashboard (startup problems etc.)
        self.blocked_markets: set = set()  # markets you already held a position in: the bot won't touch them
        self.trading_enabled = True      # False during replay warm-up (books/spot only)
        a = cfg.alerts
        self.fee_audit = FeeAudit(a.fee_drift_tolerance, a.fee_drift_min_fills)
        self.pnl_hist = PnlHistory()
        self.notify: Callable[[str, str], object] = lambda kind, text: None   # engine installs the Notifier
        self.restarts = 0                 # set by the supervisor
        self._halts_seen = 0
        self.started_ms = 0

    # ---- setup -------------------------------------------------------------------------
    def _fee_for_market(self, slug: str) -> FeeModel:
        return self.markets[slug].fee

    def _market_for_token(self, token: str) -> Optional[MarketInfo]:
        s = self.token_to_market.get(token)
        return self.markets[s].info if s else None

    def add_market(self, m: MarketInfo) -> None:
        if m.slug in self.markets:
            return
        fee = fee_model_for(m, self.cfg.fees.profile, self.cfg.fees.rebate_capture,
                            self.cfg.fees.use_market_schedule, self.cfg.fees.precision,
                            self.cfg.fees.default_fee_type)
        st = MarketState(info=m, inv=MarketInventory(market=m.slug), fee=fee)
        self.markets[m.slug] = st
        self._upcoming.append(st)
        self._upcoming.sort(key=lambda x: x.info.start_ms)
        self._activate_due()
        self.token_to_market[m.token_up] = m.slug
        self.token_to_market[m.token_down] = m.slug
        if m.condition_id:
            self.cond_to_market[m.condition_id] = m.slug
        self.books.book(m.token_up).tick_size = m.tick_size
        self.books.book(m.token_down).tick_size = m.tick_size

    LIVE_LEAD_MS = 120000

    def _activate_due(self) -> None:
        while self._upcoming and self._upcoming[0].info.start_ms - self.LIVE_LEAD_MS <= self.now_ms:
            st = self._upcoming.pop(0)
            if not st.settled:
                self._live[st.info.slug] = st

    def active_markets(self) -> List[MarketState]:
        """Unsettled markets whose window is open or about to open."""
        return [s for s in self._live.values() if not s.settled]

    # ---- inbound data ------------------------------------------------------------------
    def set_time(self, now_ms: int) -> None:
        if now_ms > self.now_ms:
            self.now_ms = now_ms
            if self._upcoming and self._upcoming[0].info.start_ms - self.LIVE_LEAD_MS <= now_ms:
                self._activate_due()
        self._handle_fills(self.exchange.advance(self.now_ms))

    def on_pm_frame(self, msg, recv_ms: int) -> None:
        self.set_time(recv_ms)
        self.last_pm_ms = recv_ms
        touched: set = set()
        for ev in self.books.handle(msg, recv_ms):
            if isinstance(ev, BookEvent):
                slug = self.token_to_market.get(ev.token)
                if slug is None:
                    continue
                self._handle_fills(self.exchange.on_book_event(ev))
                touched.add(slug)
                self._record_tob(slug, ev.token)
                self._check_probes(ev.token)
            elif isinstance(ev, Trade):
                slug = self.token_to_market.get(ev.token)
                if slug is None:
                    continue
                self._handle_fills(self.exchange.on_trade(ev))
                touched.add(slug)
            elif isinstance(ev, ResolvedEvent):
                slug = self.cond_to_market.get(ev.condition_id) or self.token_to_market.get(ev.winning_token)
                if slug:
                    st = self.markets[slug]
                    w = st.info.outcome_of(ev.winning_token)
                    if w is None and ev.winning_outcome:
                        w = UP if ev.winning_outcome.lower() in ("up", "yes") else DOWN
                    st.info.winner = w
        if not self.data_ok and self._books_resynced():
            self.data_ok = True
            self.store.risk_event(self.run_id, recv_ms, "data_resumed", self.data_gap_reason)
        for slug in touched:
            self.markets[slug].dirty = True
            self._maybe_tick(self.markets[slug])

    def on_spot(self, asset: str, price: float, recv_ms: int) -> None:
        self.set_time(recv_ms)
        self.spot.add(asset, recv_ms, price)
        if self.spot_gap:
            self.spot_gap = False
            self.store.risk_event(self.run_id, recv_ms, "spot_resumed", "")

    def on_data_gap(self, reason: str, now_ms: int) -> None:
        """Market-data gap (disconnect, stale): cancel everything, drop books, wait for fresh snapshots."""
        self.set_time(now_ms)
        if self.data_ok:
            self.gaps.append((now_ms, reason))
            self.store.risk_event(self.run_id, now_ms, "data_gap", reason)
            self.notify("data_gap", f"market-data gap ({reason}): all orders cancelled, waiting for fresh books")
        self.data_ok = False
        self.data_gap_reason = reason
        self.books.invalidate_all()
        self.cancel_all(now_ms, "data_gap")
        if hasattr(self.exchange, "cancel_everything"):
            self.exchange.cancel_everything()

    def on_spot_gap(self, reason: str, now_ms: int) -> None:
        """Spot feed gap: cancel everything and pause until spot ticks resume (books stay valid)."""
        self.set_time(now_ms)
        if not self.spot_gap:
            self.gaps.append((now_ms, reason))
            self.store.risk_event(self.run_id, now_ms, "spot_gap", reason)
        self.spot_gap = True
        self.cancel_all(now_ms, "spot_gap")

    def _books_resynced(self) -> bool:
        for st in self.active_markets():
            if st.info.start_ms - 120000 <= self.now_ms <= st.info.end_ms:
                for t in (st.info.token_up, st.info.token_down):
                    if not self.books.book(t).has_snapshot:
                        return False
        return True

    # ---- timer ------------------------------------------------------------------------
    def on_timer(self, now_ms: int) -> None:
        self.set_time(now_ms)
        self.risk.check_kill_file(now_ms) if self.mode != "backtest" else None
        if self.data_ok and self.last_pm_ms and now_ms - self.last_pm_ms > self.cfg.risk.pm_stale_ms:
            self.on_data_gap("pm_stale", now_ms)
        if not self.spot_gap:
            for asset in {s.info.asset for s in self.active_markets()}:
                last = self.spot.last(asset)
                if last is not None and now_ms - last[0] > 3 * self.cfg.signal.spot_stale_ms:
                    self.on_spot_gap(f"spot_stale:{asset}", now_ms)
                    break
        # daily loss (UTC day)
        day = now_ms // 86400000 * 86400000
        if day != self.day_start_ms:
            self.day_start_ms = day
            self.day_realized = 0.0
        pnl = self.pnl_today()
        self.risk.check_daily_loss(pnl, now_ms)
        if len(self.risk.halts) > self._halts_seen:
            self._halts_seen = len(self.risk.halts)
            self.notify("daily_loss_halt", f"daily loss limit hit ({self.risk.halt_reason}): no new orders "
                                           f"until the next UTC day")
        if self.mode != "backtest":
            self.pnl_hist.add(now_ms, pnl)
        if self.risk.blocked(now_ms):
            self.cancel_all(now_ms, "blocked")
        for st in list(self._live.values()):
            if st.settled:
                self._live.pop(st.info.slug, None)
                continue
            m = st.info
            if now_ms >= m.end_ms:
                self._cancel_market(st, now_ms, "window_end")
                if (m.winner is None or m.winner == "UNKNOWN") and self.cfg.backtest.infer_winner_from_spot \
                        and now_ms >= m.end_ms + self.cfg.backtest.settle_delay_ms:
                    p0 = self.spot.price_at(m.asset, m.start_ms)
                    p1 = self.spot.price_at(m.asset, m.end_ms)
                    if p0 and p1:
                        m.winner = UP if p1 >= p0 else DOWN
                        self.inferred_winners += 1
                if m.winner in (UP, DOWN) and now_ms >= m.end_ms + self.cfg.backtest.settle_delay_ms:
                    self._settle(st, m.winner, now_ms)
                continue
            self._maybe_tick(st, force=True)

    def pnl_today(self) -> float:
        mtm = 0.0
        for st in self.active_markets():
            bu = self.books.book(st.info.token_up)
            bd = self.books.book(st.info.token_down)
            mtm += st.inv.mark_to_market(bu.mid(), bd.mid())
        return self.day_realized + mtm

    # ---- strategy -----------------------------------------------------------------------
    def _maybe_tick(self, st: MarketState, force: bool = False) -> None:
        if st.settled or not self.trading_enabled:
            return
        if not force and self.now_ms - st.last_tick_ms < self.cfg.engine.min_tick_ms:
            return
        if not st.dirty and not force:
            return
        st.last_tick_ms = self.now_ms
        st.dirty = False
        m = st.info
        if self.now_ms < m.start_ms - 1000 or self.now_ms >= m.end_ms:
            return
        if m.slug in self.blocked_markets:
            from .strategy import Plan
            st.last_plan = Plan(mode="skipped: you hold a position here")
            return
        blocked = self.risk.blocked(self.now_ms)
        if blocked or not self.data_ok or self.spot_gap:
            why = blocked or ("data_gap" if not self.data_ok else "spot_gap")
            self._cancel_market(st, self.now_ms, why)
            return
        sig = self.signal.read(m, self.now_ms)
        st.last_signal = sig
        books = {UP: self.books.book(m.token_up), DOWN: self.books.book(m.token_down)}
        open_buy = sum(o.remaining * o.price for o in self.exchange.active_orders(m.slug) if o.side == BUY)
        budget = self.cfg.risk.max_market_usd - st.inv.cost_basis_usd - open_buy
        total_open = sum(o.remaining * o.price for o in self.exchange.active_orders() if o.side == BUY)
        total_budget = self.cfg.risk.max_total_usd - sum(s.inv.cost_basis_usd for s in self.active_markets()) - total_open
        if not st.fee.supported:
            from .strategy import Plan
            plan = Plan(cancel_all=True, mode="unsupported_fee_type")
        else:
            plan = self.strategy.plan(m, self.now_ms, books, st.inv, sig, st.fee,
                                      budget_usd=min(budget, total_budget))
        st.last_plan = plan
        self._reconcile(st, plan)

    def _reconcile(self, st: MarketState, plan: Plan) -> None:
        now = self.now_ms
        active = [o for o in self.exchange.active_orders(st.info.slug)]
        if plan.cancel_all:
            self._cancel_market(st, now, plan.mode)
            return
        eng = self.cfg.engine
        for outcome in OUTCOMES:
            q = plan.quotes.get(outcome)
            mine = [o for o in active if o.outcome == outcome and o.side == BUY and o.post_only
                    and o.cancel_req_ms is None]
            if q is None:
                for o in mine:
                    self._cancel(o, now, "no_quote")
                continue
            keep: Optional[Order] = None
            tick = st.info.tick_size
            for o in mine:
                size_ok = o.remaining >= min(q.size, max(st.info.min_order_size, q.size * 0.5)) - 1e-9 \
                    and o.remaining <= q.size + 1e-9
                diff_ticks = (q.price - o.price) / tick
                if keep is None and size_ok:
                    if abs(diff_ticks) < 1e-6:
                        keep = o
                        continue
                    if diff_ticks > 0 and diff_ticks < eng.requote_up_ticks - 1e-6 \
                            and now - o.created_ms < eng.requote_stale_ms:
                        keep = o          # small improvement: not worth losing queue position yet
                        continue
                # lowering a bid (safer) or a big/stale improvement: replace
                self._cancel(o, now, "requote")
            if keep is None and not self._backing_off(st.info.slug, outcome, now):
                self._submit(st, Order(market=st.info.slug, outcome=outcome, side=BUY, price=q.price,
                                       size=round(q.size, 2), post_only=True, tag=q.tag))
            if self.cfg.store_our_quotes:
                self.store.quote(now, self.run_id, st.info.slug, outcome, "ours", q.price, None, q.size, None)
        for t in plan.takes:
            busy = [o for o in active if not o.post_only and o.outcome == t.outcome and o.side == t.side]
            if busy or self._backing_off(st.info.slug, t.outcome + t.side, now):
                continue
            if t.side == BUY:
                # don't let a passive completion bid and a taker completion double up
                for o in active:
                    if o.post_only and o.outcome == t.outcome and o.side == BUY:
                        self._cancel(o, now, "taker_replaces")
            self._submit(st, Order(market=st.info.slug, outcome=t.outcome, side=t.side, price=t.limit,
                                   size=round(t.size, 2), post_only=False, tag=t.tag))

    def _submit(self, st: MarketState, o: Order) -> None:
        if o.size < 0.01:
            return
        mkt_open = self.exchange.active_orders(st.info.slug)
        all_open = self.exchange.active_orders()
        total_basis = sum(s.inv.cost_basis_usd for s in self.active_markets())
        d = self.risk.check_order(o, st.inv, mkt_open, all_open, total_basis, self.now_ms)
        if d.ok and o.side == BUY and st.inv.shares[o.outcome] + o.size > self.cfg.kalshi.max_position_contracts:
            from .risk import RiskDecision
            d = RiskDecision(False, "kalshi_position_limit")
        if not d.ok:
            key = o.outcome if o.post_only else o.outcome + o.side
            self._backoff[(st.info.slug, key)] = self.now_ms + self.cfg.engine.reject_backoff_ms
            o.status = OrderStatus.REJECTED
            o.reject_reason = "risk:" + d.reason
            self.store.order(self.run_id, o, self.now_ms)
            return
        self.exchange.submit(o, st.info, self.now_ms)
        self.store.order(self.run_id, o, self.now_ms)
        self.store.order_event(self.run_id, o.id, "submit", self.now_ms, o.tag)

    def _backing_off(self, slug: str, key: str, now: int) -> bool:
        until = self._backoff.get((slug, key))
        return until is not None and now < until

    def _cancel(self, o: Order, now: int, why: str) -> None:
        self.exchange.cancel(o.id, now)
        self.store.order_event(self.run_id, o.id, "cancel_req", now, why)

    def _cancel_market(self, st: MarketState, now: int, why: str) -> None:
        for o in self.exchange.active_orders(st.info.slug):
            if o.cancel_req_ms is None:
                self._cancel(o, now, why)

    def cancel_all(self, now: int, why: str) -> None:
        for o in self.exchange.active_orders():
            if o.cancel_req_ms is None:
                self._cancel(o, now, why)

    def kill(self, reason: str) -> None:
        if not self.risk.killed:
            self.notify("kill", f"KILL SWITCH tripped: {reason}. Orders cancelled; trading is stopped until restart.")
        self.risk.kill(reason, self.now_ms)
        self.store.risk_event(self.run_id, self.now_ms, "kill", reason)
        self.cancel_all(self.now_ms, "killed")
        if hasattr(self.exchange, "cancel_everything"):
            self.exchange.cancel_everything()

    # ---- fills & settlement ---------------------------------------------------------------
    def _handle_fills(self, fills: List[Fill]) -> None:
        for f in fills:
            st = self.markets.get(f.market)
            if st is None:
                continue
            inv = st.inv
            before_paired = inv.paired
            other_avg = inv.avg_cost(other(f.outcome))
            inv.apply_fill(f)
            # edge given up when a leg is completed above the target pair cost (marginal pair cost)
            new_pairs = inv.paired - before_paired
            if new_pairs > 1e-9 and f.side == BUY:
                excess = (f.price + other_avg) - self.cfg.strategy.target_pair_cost
                if excess > 0:
                    inv.rescue_excess += excess * new_pairs
            self.fills.append(f)
            if self.mode in ("demo", "live"):
                msg = self.fee_audit.record(st.fee, f.liquidity, f.size, f.price, f.fee)
                if msg:
                    self.store.risk_event(self.run_id, f.ts_ms, "fee_drift", msg)
                    self.notify("fee_drift", "fee model mismatch - " + msg + ". Pair-cost maths may be off; "
                                "check `kbot check` fee settings.")
                    if self.cfg.alerts.fee_drift_halt:
                        self.kill("fee_drift: " + msg[:100])
            self.store.fill(self.run_id, f)
            self.store.order_event(self.run_id, f.order_id, "fill", f.ts_ms,
                                   f"{f.size}@{f.price} {f.liquidity}")
            tok = st.info.token(f.outcome)
            p = AdverseProbe(token=tok, price=f.price, side=f.side, ts_ms=f.ts_ms, liquidity=f.liquidity, tag=f.tag)
            self.probes.append(p)
            self._open_probes.setdefault(tok, []).append(p)
            # (cut PnL sits in inv.realized, which mark_to_market includes, so pnl_today sees it at once)
            for h in self.on_fill_hooks:
                h(f)
            st.dirty = True

    def _check_probes(self, token: str) -> None:
        lst = self._open_probes.get(token)
        if not lst:
            return
        mid = self.books.book(token).mid()
        keep = []
        for p in lst:
            for h in self.ADVERSE_HORIZONS:
                if h not in p.after and self.now_ms >= p.ts_ms + h * 1000:
                    p.after[h] = mid
            if len(p.after) < len(self.ADVERSE_HORIZONS):
                keep.append(p)
        self._open_probes[token] = keep

    def _settle(self, st: MarketState, winner: str, now: int) -> None:
        b = st.inv.settle(winner, now)
        b["market"] = st.info.slug
        b["asset"] = st.info.asset
        b["horizon_s"] = st.info.horizon_s
        b["end_ms"] = st.info.end_ms
        b["settled_ms"] = now
        b["episodes"] = [(e.start_ms, e.end_ms, e.side, e.max_shares, e.closed_by) for e in st.inv.episodes]
        b["n_fills"] = len(st.inv.fills)
        b["bought_cost_total"] = st.inv.bought_cost_total
        st.settled = True
        st.settle_breakdown = b
        self.settlements.append(b)
        self.day_realized += b["total_pnl"]
        big = self.cfg.alerts.big_loss_usd
        if big and b["total_pnl"] <= -abs(big):
            self.notify("big_loss", f"{st.info.slug} settled at {b['total_pnl']:+.2f} "
                                    f"(residual {b['residual_shares']:g} sh, cuts {b['cut_pnl']:+.2f})")
        self.store.pnl(self.run_id, now, st.info.slug, "settle", b["total_pnl"],
                       {k: v for k, v in b.items() if k != "episodes"})

    def settle_remaining(self, winner_lookup: Callable[[MarketInfo], Optional[str]]) -> None:
        for st in [s for s in self.markets.values() if not s.settled]:
            w = st.info.winner or winner_lookup(st.info)
            if w and self.now_ms >= st.info.end_ms:
                self._settle(st, w, max(self.now_ms, st.info.end_ms))

    # ---- helpers ------------------------------------------------------------------------
    def _record_tob(self, slug: str, token: str) -> None:
        if not self.cfg.store_tob:
            return
        st = self.markets[slug]
        b = self.books.book(token)
        tob = (b.best_bid, b.best_ask)
        oc = st.info.outcome_of(token)
        if st.tob.get(oc) != tob:
            st.tob[oc] = tob
            self.store.quote(self.now_ms, self.run_id, slug, oc, "tob", tob[0], tob[1],
                             b.bids.get(tob[0]) if tob[0] else None, b.asks.get(tob[1]) if tob[1] else None)

    def snapshot(self) -> dict:
        """State for the dashboard."""
        out = {"now_ms": self.now_ms or int(__import__("time").time() * 1000), "mode": self.mode,
               "run_id": self.run_id, "status_message": self.status_message,
               "killed": self.risk.killed, "kill_reason": self.risk.kill_reason,
               "halted": bool(self.risk.blocked(self.now_ms)) and not self.risk.killed,
               "halt_reason": self.risk.halt_reason,
               "data_ok": self.data_ok, "data_gap_reason": self.data_gap_reason, "spot_gap": self.spot_gap,
               "pnl_today": round(self.pnl_today(), 4),
               "realized_total": round(sum(s["total_pnl"] for s in self.settlements), 4),
               "settled_markets": len(self.settlements),
               "risk": {k: getattr(self.cfg.risk, k) for k in (
                   "max_order_usd", "max_market_usd", "max_residual_usd", "max_residual_shares",
                   "max_total_usd", "max_daily_loss_usd")},
               "recent_rejects": self.risk.rejects[-10:], "markets": [], "orders": [],
               "pnl_history": self.pnl_hist.as_list(), "fee_audit": self.fee_audit.snapshot(),
               "health": {"gaps": len(self.gaps), "data_age_s": round((self.now_ms - self.last_pm_ms) / 1000, 1)
                          if self.last_pm_ms else None,
                          "uptime_s": round((self.now_ms - self.started_ms) / 1000) if self.started_ms else None,
                          "restarts": self.restarts, "rejects": len(self.risk.rejects)},
               "recent_gaps": [[t, r] for t, r in self.gaps[-5:]]}
        for st in sorted(self.active_markets(), key=lambda s: s.info.end_ms):
            m = st.info
            bu, bd = self.books.book(m.token_up), self.books.book(m.token_down)
            sig = st.last_signal
            out["markets"].append({
                "slug": m.slug, "asset": m.asset, "horizon_s": m.horizon_s,
                "t_left_s": round((m.end_ms - self.now_ms) / 1000, 1),
                "up_bid": bu.best_bid, "up_ask": bu.best_ask, "down_bid": bd.best_bid, "down_ask": bd.best_ask,
                "up": st.inv.up, "down": st.inv.down, "paired": st.inv.paired, "residual": st.inv.residual,
                "pair_cost": round(st.inv.pair_cost, 4) if st.inv.paired else None,
                "residual_usd": round(st.inv.residual_cost_usd, 3),
                "worst_case_pnl": round(st.inv.worst_case_pnl(), 3),
                "mtm_pnl": round(st.inv.mark_to_market(bu.mid(), bd.mid()), 3),
                "fees": round(st.inv.fees, 4), "rebates": round(st.inv.rebates, 4),
                "mode": st.last_plan.mode if st.last_plan else "",
                "target": m.open_price, "spot": (self.spot.last(m.asset) or (None, None))[1],
                "fee_model": st.fee.name,
                "signal": None if sig is None else {"ok": sig.ok, "dir": sig.direction,
                                                    "mom_bps": round(sig.momentum_bps, 1),
                                                    "dist_bps": round(sig.distance_bps, 1),
                                                    "z": round(sig.z, 2),
                                                    "p_model": None if sig.p_model is None else round(sig.p_model, 3),
                                                    "reason": sig.reason},
            })
        for o in self.exchange.active_orders():
            out["orders"].append({"id": o.id, "market": o.market, "outcome": o.outcome, "side": o.side,
                                  "price": o.price, "size": o.size, "filled": o.filled, "status": o.status.value,
                                  "tag": o.tag, "queue_ahead": round(o.queue_ahead, 1)})
        return out
