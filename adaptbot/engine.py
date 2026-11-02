"""The live/paper engine: one loop, once per second.

Each tick:
  1. discover markets, refresh strikes, fetch order books
  2. feed spot prices to the model, build a View per open market
  3. every profile trades it in shadow; the selected profile trades for real
  4. settle closed markets, reconcile with Kalshi, enforce limits
"""
from __future__ import annotations

import asyncio
import logging
import time

from .brain import Brain
from .broker import LiveBroker, PaperBroker
from .config import SERIES, Settings
from .kalshi import KalshiClient, parse_market
from .market import Book, MarketMeta, Position
from .recorder import Recorder
from .risk import InstanceLock, Risk
from .spot import SpotHub
from .strategy import Intent, Sizing, decide

log = logging.getLogger("adaptbot.engine")

DISCOVER_EVERY_S = 15.0
SETTLE_GIVE_UP_S = 3600.0
DATA_LOSS_EXIT_S = 30.0


class Engine:
    def __init__(self, s: Settings, client: KalshiClient, broker, spot: SpotHub,
                 brain: Brain, recorder: Recorder, trade: bool = True):
        self.s, self.c, self.broker, self.spot = s, client, broker, spot
        self.brain, self.rec, self.trade = brain, recorder, trade
        self.risk = Risk(s)
        self.markets: dict[str, MarketMeta] = {}
        self.books: dict[str, Book] = {}
        self.positions: dict[str, Position] = {}
        self.memo: dict[str, dict] = {}
        self.pending_settle: dict[str, float] = {}     # ticker -> close_ts
        self.trades: list[dict] = []
        self.market_log: list[dict] = []
        self.realized_today = 0.0
        self.profile: str | None = None
        self.warmup = True
        self.mismatch_count: dict[str, int] = {}
        self.last_discover = 0.0
        self.last_reconcile = 0.0
        self.last_status = 0.0
        self.last_meta_refresh: dict[str, float] = {}
        self.need_reconcile = False
        self.last_good_spot: dict[str, float] = {}
        self.recently_settled: dict[str, float] = {}
        self._cash_cache: float | None = None
        self.stop = asyncio.Event()

    # ================================================================ helpers
    def _asset_of(self, series: str) -> str:
        return next(a for a, sr in SERIES.items() if sr == series)

    def live_sizing(self, cash: float | None) -> Sizing:
        s = self.s
        bank = s.bankroll if cash is None else min(s.bankroll, cash)
        return Sizing(bankroll=bank, kelly_fraction=s.kelly_fraction,
                      max_contracts=s.max_contracts_per_order,
                      max_dollars_market=s.max_dollars_per_market, fee_rate=s.taker_fee_rate,
                      max_slippage=s.max_slippage, size_mult=self.risk.size_mult(),
                      fixed_contracts=s.warmup_contracts if self.warmup else None)

    def total_exposure(self) -> float:
        return sum(p.cost for p in self.positions.values())

    # ================================================================ discovery
    async def discover(self, now: float) -> None:
        for series in self.s.series:
            asset = self._asset_of(series)
            try:
                raw = await self.c.markets(series, status="open")
            except Exception as e:  # noqa: BLE001
                log.warning("discover %s failed: %r", series, e)
                continue
            for m in raw:
                meta = parse_market(m, asset)
                if meta.close_ts <= now or meta.open_ts > now + 60:
                    continue
                old = self.markets.get(meta.ticker)
                if old is None:
                    log.info("tracking %s strike=%s closes %s", meta.ticker, meta.strike,
                             time.strftime("%H:%M:%S", time.localtime(meta.close_ts)))
                    self.markets[meta.ticker] = meta
                elif old.strike is None and meta.strike is not None:
                    old.strike = meta.strike
                    log.info("%s strike now %s", meta.ticker, meta.strike)

    async def refresh_strikes(self, now: float) -> None:
        for t, m in self.markets.items():
            if m.strike is None and m.open_ts <= now and now - self.last_meta_refresh.get(t, 0) > 5:
                self.last_meta_refresh[t] = now
                try:
                    fresh = parse_market(await self.c.market(t), m.asset)
                except Exception as e:  # noqa: BLE001
                    log.warning("market %s refresh failed: %r", t, e)
                    continue
                if fresh.strike is not None:
                    m.strike = fresh.strike
                    log.info("%s strike now %s", t, m.strike)

    async def fetch_books(self, now: float) -> None:
        live = [t for t, m in self.markets.items() if m.open_ts <= now < m.close_ts]

        async def one(t):
            try:
                self.books[t] = await self.c.orderbook(t)
            except Exception as e:  # noqa: BLE001
                log.debug("orderbook %s failed: %r", t, e)
        await asyncio.gather(*(one(t) for t in live))

    # ================================================================ trading
    async def act(self, t: str, it: Intent, book: Book, why_profile: str) -> None:
        pos = self.positions.setdefault(t, Position(t))
        block = self.risk.check(it, pos, self.positions)
        if block:
            if it.action == "buy":
                return
            log.warning("sell blocked on %s: %s", t, block)
            return
        q, avg, fee, uncertain = await self.broker.execute(t, it, book)
        if uncertain:
            self.need_reconcile = True
        if q <= 0:
            return
        if it.action == "buy":
            pos.apply_buy(it.side, q, avg, fee, why_profile)
            pos.peak_p = max(pos.peak_p, it.p_side)
            pnl = 0.0
        else:
            pnl = pos.apply_sell(q, avg, fee)
            self.realized_today += pnl
        rec = {"ts": time.time(), "ticker": t, "action": it.action, "side": it.side, "qty": q,
               "price": round(avg, 4), "fee": fee, "reason": it.reason, "profile": why_profile,
               "p_side": round(it.p_side, 3), "edge": round(it.edge, 3), "pnl": round(pnl, 2)}
        self.trades = (self.trades + [rec])[-300:]
        log.info("%s %s %s x%g @%.2f fee %.2f  [%s/%s] p=%.2f%s", it.action.upper(), t, it.side, q, avg,
                 fee, why_profile, it.reason, it.p_side, f" pnl {pnl:+.2f}" if it.action == "sell" else "")

    def flatten_intent(self, pos: Position, book: Book, reason: str) -> Intent | None:
        bid = book.best_bid(pos.side) if book else None
        if pos.qty < 1 or bid is None:
            return None
        lim = max(0.01, round(bid - 0.03, 2))
        return Intent("sell", pos.side, int(pos.qty), lim, reason)

    async def tick(self) -> None:
        now = time.time()
        self.risk.check_kill()
        if now - self.last_discover > DISCOVER_EVERY_S:
            self.last_discover = now
            await self.discover(now)
        await self.refresh_strikes(now)
        await self.fetch_books(now)

        spot_now = {}
        for asset in self.s.assets:
            px, _n = self.spot.price(asset, now)
            spot_now[asset] = px
            if px is not None:
                self.brain.on_spot(asset, now, px)
                self.last_good_spot[asset] = now
                self.rec.spot(now, asset, px)

        if self.s.adaptive:
            self.profile, self.warmup = self.brain.selector.choose(self.brain.shadow)
        else:
            self.profile, self.warmup = self.s.default_profile, False
        profiles = self.brain.shadow.profiles
        cash = self._cash_cache if self.broker.live else await self.broker.cash()
        sizing = self.live_sizing(cash)
        shadow_sizing = self.brain.shadow_sizing()

        for t, m in list(self.markets.items()):
            if not (m.open_ts <= now < m.close_ts):
                continue
            book = self.books.get(t)
            if book is None or now - book.ts > self.s.stale_book_s:
                continue
            px = spot_now.get(m.asset)
            self.rec.meta(now, m)
            self.rec.book(now, t, book)
            v = self.brain.view(now, m, book, px, now if px else 0.0)
            self.brain.shadow.step(v, shadow_sizing)
            if not self.trade:
                continue
            pos = self.positions.setdefault(t, Position(t))

            if self.risk.flatten and pos.qty > 0:
                it = self.flatten_intent(pos, book, "halt_flatten")
                if it:
                    await self.act(t, it, book, pos.profile or "-")
                continue
            if pos.qty > 0 and px is None and now - self.last_good_spot.get(m.asset, now) > DATA_LOSS_EXIT_S:
                it = self.flatten_intent(pos, book, "data_loss_exit")
                if it:
                    await self.act(t, it, book, pos.profile or "-")
                continue

            name = pos.profile if pos.qty > 0 and pos.profile in profiles else self.profile
            if name is None:
                continue        # sitting out and nothing to manage
            allow = (self.profile is not None and not self.risk.entry_block() and px is not None)
            it = decide(v, pos, profiles[name], sizing, self.memo.setdefault(t, {}), allow_entry=allow)
            if it is not None:
                await self.act(t, it, book, name)

        await self.settle_closed(now)
        if self.broker.live and (self.need_reconcile or now - self.last_reconcile > self.s.reconcile_interval_s):
            await self.reconcile(now)
        elif not self.broker.live:
            eq = await self.broker.cash() + sum(p.mark(self.books[t]) for t, p in self.positions.items()
                                                if t in self.books)
            self.risk.update_equity(eq)
        if now - self.last_status > 60:
            self.last_status = now
            self.log_status()

    # ================================================================ settlement
    async def settle_closed(self, now: float) -> None:
        for t, m in list(self.markets.items()):
            if m.close_ts <= now and t not in self.pending_settle:
                self.pending_settle[t] = m.close_ts
        for t, close_ts in list(self.pending_settle.items()):
            if now - close_ts < 5:
                continue
            m = self.markets.get(t)
            last_try = self.last_meta_refresh.get("settle:" + t, 0)
            if now - last_try < 10:
                continue
            self.last_meta_refresh["settle:" + t] = now
            try:
                fresh = parse_market(await self.c.market(t), m.asset if m else "btc")
            except Exception as e:  # noqa: BLE001
                log.debug("settle check %s: %r", t, e)
                fresh = None
            if fresh and fresh.result in ("yes", "no"):
                self._finish(t, fresh.result)
            elif now - close_ts > SETTLE_GIVE_UP_S:
                log.warning("%s: no result after %.0f min - dropping", t, SETTLE_GIVE_UP_S / 60)
                self.brain.shadow.drop(t)
                self.brain.forget(t)
                self._cleanup(t)

    def _finish(self, t: str, result: str) -> None:
        pos = self.positions.get(t) or Position(t)
        qty, side = pos.qty, pos.side
        self.realized_today += pos.settle(result)
        if qty > 0:
            self.broker.settle(qty, result == side)
        traded = pos.entries > 0
        pnl = pos.realized
        self.recently_settled[t] = time.time()
        self.rec.result(time.time(), t, result)
        shadow = self.brain.settle(t, result)
        self.risk.market_done(pnl, traded)
        entry = {"ticker": t, "result": result, "pnl": round(pnl, 2), "traded": traded,
                 "profile": pos.profile, "shadow": shadow}
        self.market_log = (self.market_log + [entry])[-100:]
        best = max(shadow, key=shadow.get) if shadow else "-"
        log.info("SETTLED %s -> %s  live %+.2f%s  | shadow best %s %+.2f", t, result.upper(), pnl,
                 f" ({pos.profile})" if traded else " (no trade)", best, shadow.get(best, 0.0))
        self.brain.save()
        self._cleanup(t)

    def _cleanup(self, t: str) -> None:
        for d in (self.markets, self.books, self.positions, self.memo, self.pending_settle, self.mismatch_count):
            d.pop(t, None)

    # ================================================================ reconcile
    async def reconcile(self, now: float) -> None:
        self.last_reconcile = now
        self.need_reconcile = False
        try:
            ex_pos = await self.broker.positions()
            cash = await self.broker.cash()
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile failed: %r", e)
            return
        self._cash_cache = cash
        prefixes = tuple(self.s.series)
        ours = {t: (p.qty if p.side == "yes" else -p.qty) for t, p in self.positions.items() if p.qty > 0}
        # Kalshi can show a settled position for a little while after the result
        self.recently_settled = {t: ts for t, ts in self.recently_settled.items() if now - ts < 600}
        tickers = {t for t in ex_pos if t.startswith(prefixes) and t not in self.recently_settled} | set(ours)
        for t in tickers:
            ex = ex_pos.get(t, 0.0)
            mine = ours.get(t, 0.0)
            if abs(ex - mine) < 1e-6:
                self.mismatch_count.pop(t, None)
                continue
            n = self.mismatch_count.get(t, 0) + 1
            self.mismatch_count[t] = n
            log.warning("position mismatch %s: ours %+g kalshi %+g (check %d)", t, mine, ex, n)
            if n >= 2:
                self._adopt(t, ex)
                self.risk.halt(f"position mismatch on {t} (ours {mine:+g}, Kalshi {ex:+g}) - "
                               "is another copy of the bot or a manual trade running? "
                               "New entries stopped; existing positions are still managed.")
        equity = cash
        for t, q in ex_pos.items():
            b = self.books.get(t)
            if b is not None:
                side = "yes" if q > 0 else "no"
                equity += abs(q) * (b.best_bid(side) or 0.0)
            elif t in self.positions:
                equity += self.positions[t].cost       # unknown mark: use cost
        self.risk.update_equity(equity)

    def _adopt(self, t: str, signed: float) -> None:
        """Make Kalshi's position ours so the exit logic can manage it."""
        pos = self.positions.setdefault(t, Position(t))
        if signed == 0:
            pos.qty, pos.cost, pos.side = 0.0, 0.0, ""
            return
        side = "yes" if signed > 0 else "no"
        b = self.books.get(t)
        px = (b.best_ask(side) if b else None) or 0.5
        pos.side, pos.qty = side, abs(signed)
        pos.cost = px * pos.qty
        pos.entry_price = px
        pos.entries = max(pos.entries, 1)
        pos.profile = pos.profile or self.s.default_profile

    # ================================================================ status
    def log_status(self) -> None:
        open_pos = {t: f"{p.side}x{p.qty:g}" for t, p in self.positions.items() if p.qty > 0}
        log.info("status: day_pnl=%+.2f (exchange) realized=%+.2f exposure=$%.2f positions=%s profile=%s | %s%s",
                 self.risk.day_pnl, self.realized_today, self.total_exposure(), open_pos or "-",
                 self.profile or "NONE", self.brain.selector.reason,
                 f" | HALTED: {self.risk.halted}" if self.risk.halted else "")

    def snapshot(self) -> dict:
        now = time.time()
        mk = []
        for t, m in self.markets.items():
            b = self.books.get(t)
            p = self.positions.get(t)
            mk.append({"ticker": t, "strike": m.strike, "remaining": round(m.close_ts - now),
                       "yes_bid": b.best_bid("yes") if b else None, "yes_ask": b.best_ask("yes") if b else None,
                       "position": f"{p.side} x{p.qty:g} @ {p.entry_price:.2f}" if p and p.qty > 0 else "",
                       "profile": p.profile if p and p.qty > 0 else ""})
        stats = {n: {**st.to_dict(), "score": round(st.score(self.s.score_k), 4)}
                 for n, st in self.brain.shadow.stats.items()}
        return {
            "now": now, "live": self.broker.live, "trading": self.trade,
            "profile": self.profile, "warmup": self.warmup, "selector": self.brain.selector.reason,
            "day_pnl": round(self.risk.day_pnl, 2), "realized": round(self.realized_today, 2),
            "equity": self.risk.equity, "halted": self.risk.halted,
            "exposure": round(self.total_exposure(), 2),
            "calibrator": self.brain.cal.to_dict(),
            "sigma": {a: v.sigma for a, v in self.brain.vol.items()},
            "spot": self.spot.status(), "markets": mk, "trades": self.trades[-30:],
            "settled": self.market_log[-30:], "profiles": stats,
        }

    # ================================================================ run
    async def run(self) -> None:
        log.info("engine running (%s)", "LIVE - REAL MONEY" if self.broker.live else "paper")
        try:
            while not self.stop.is_set():
                t0 = time.time()
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - keep running, but loudly
                    log.exception("tick failed")
                if time.time() - self.rec.last_flush > 30:
                    self.rec.flush()
                lock = getattr(self, "lock", None)
                if lock:
                    lock.beat()
                await asyncio.sleep(max(0.05, self.s.poll_interval_s - (time.time() - t0)))
        finally:
            self.rec.flush()
            self.brain.save()
            left = {t: f"{p.side} x{p.qty:g}" for t, p in self.positions.items() if p.qty > 0}
            if left:
                log.warning("stopped with open positions (they will settle on Kalshi): %s", left)


async def preflight(s: Settings, c: KalshiClient, spot: SpotHub, broker) -> list[str]:
    """Everything must pass before real money is used."""
    problems = []
    try:
        cash = await broker.cash()
        print(f"  cash: ${cash:.2f}")
        if cash < s.max_dollars_per_market:
            problems.append(f"cash ${cash:.2f} is below max_dollars_per_market ${s.max_dollars_per_market:.2f}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"balance request failed: {e!r}")
    try:
        pos = await broker.positions()
        mine = {t: q for t, q in (pos or {}).items() if t.startswith(tuple(s.series))}
        if mine:
            problems.append(f"you already hold positions in these markets: {mine} - close them or wait "
                            "for settlement (otherwise the daily-loss baseline is wrong)")
    except Exception as e:  # noqa: BLE001
        problems.append(f"positions request failed: {e!r}")
    found = 0
    for series in s.series:
        try:
            found += len(await c.markets(series, status="open"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"market list for {series} failed: {e!r}")
    if found == 0:
        problems.append("no open markets found")
    for _ in range(20):
        if all(spot.price(a)[0] is not None for a in s.assets):
            break
        await asyncio.sleep(1)
    for a in s.assets:
        px, n = spot.price(a)
        if px is None:
            problems.append(f"{a} price: only {n} fresh source(s), need {s.min_spot_sources}")
        else:
            print(f"  {a} price: {px:,.2f} from {n} sources")
    return problems


def make_broker(s: Settings, c: KalshiClient, live: bool):
    if live:
        return LiveBroker(c, s.taker_fee_rate, s.order_ttl_s)
    return PaperBroker(s.paper_start_cash, s.taker_fee_rate, s.paper_slippage, s.paper_fill_ratio)
