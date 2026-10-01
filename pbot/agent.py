"""The trading agent: one opening window per trading day.

  pre-open   wake up, check the account, reconcile anything left open, pick up the watchlist
  09:30-35   build each symbol's opening range from 1-minute bars; screen out bad ranges
  09:35-10:30  look for breakouts; size by risk; enter with marketable limit orders
  until 11:00  manage stops / targets / breakeven every poll; enforce the daily loss limit
  11:00      flatten everything, cancel leftovers, write the day report

It narrates every decision (console + journal) so you can see *why* it did or did not trade.
Drop a file named STOP in the data directory (or press Ctrl+C) to flatten and stop.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import httpx

from .clock import MarketCalendar
from .config import BotConfig
from .journal import Journal
from .public_api import Bar, Quote
from .risk import RiskManager
from .strategy import OpeningRange, Position, evaluate_long, manage, opening_range, screen_range

log = logging.getLogger("pbot.agent")


class Agent:
    def __init__(self, cfg: BotConfig, mode: str, clock, market, broker, journal: Journal,
                 calendar: Optional[MarketCalendar] = None, alert_url: Optional[str] = None):
        self.cfg = cfg
        self.mode = mode
        self.clock = clock
        self.market = market
        self.broker = broker
        self.journal = journal
        self.cal = calendar or MarketCalendar(cfg.session)
        self.risk = RiskManager(cfg.risk, journal, self.cal, mode)
        self.alert_url = alert_url
        self.kill_file = os.path.join(cfg.data_dir, "STOP")
        self._reset_day()

    # ---- bookkeeping ------------------------------------------------------------------------
    def _reset_day(self) -> None:
        self.positions: Dict[str, Position] = {}
        self.ranges: Dict[str, OpeningRange] = {}
        self.bars: Dict[str, List[Bar]] = {}
        self.bars_minute: Dict[str, int] = {}
        self.status: Dict[str, str] = {}          # symbol -> last narrated status
        self.done: Dict[str, str] = {}            # symbol -> why it is finished for the day
        self.quotes: Dict[str, Quote] = {}
        self.data_errors = 0
        self.last_stop_check = datetime.min.replace(tzinfo=self.cal.tz)
        self.risk.halted = None
        self.equity = 0.0
        self.buying_power = 0.0
        self.cutoff_announced = False

    def say(self, msg: str, level: str = "INFO", alert: bool = False) -> None:
        log.log(getattr(logging, level, logging.INFO), msg)
        self.journal.event(self.clock.now(), self.mode, level, msg)
        if alert and self.alert_url:
            try:
                httpx.post(self.alert_url, json={"text": f"[pbot {self.mode}] {msg}",
                                                 "content": f"[pbot {self.mode}] {msg}"}, timeout=5)
            except Exception:  # noqa: BLE001 - alerts must never break trading
                pass

    def _note(self, sym: str, text: str) -> None:
        if self.status.get(sym) != text:
            self.status[sym] = text
            log.debug("%s: %s", sym, text)

    # ---- day loop ---------------------------------------------------------------------------
    def run_day(self) -> Optional[dict]:
        now = self.clock.now()
        d = now.date()
        if not self.cal.is_trading_day(d):
            self.say(f"{d} is not a trading day - nothing to do")
            return None
        self._reset_day()
        t_open, t_range = self.cal.open_time(d), self.cal.range_end(d)
        t_cut, t_flat = self.cal.entry_cutoff(d), self.cal.flatten_time(d)

        self._reconcile(d)
        if now >= t_flat:
            self.say(f"opening window already over ({t_flat:%H:%M} ET) - flattening leftovers only")
            self.flatten_all("window already over")
            return self.journal.summary(self.mode, d)

        wake = t_open - timedelta(minutes=self.cfg.session.preflight_minutes)
        if now < wake:
            self.say(f"sleeping until pre-open {wake:%Y-%m-%d %H:%M} ET")
            self.clock.sleep_until(wake)
        self._preflight()
        if self.clock.now() < t_open:
            self.clock.sleep_until(t_open, chunk=5)
        self.say(f"market open - building {self.cfg.session.opening_range_minutes}-minute opening ranges "
                 f"for {', '.join(self.cfg.watchlist)}; entries until {t_cut:%H:%M}, flat by {t_flat:%H:%M} ET")

        try:
            while self.clock.now() < t_flat:
                now = self.clock.now()
                if os.path.exists(self.kill_file):
                    self.risk.halt("kill switch file")
                    self.say("kill switch file found - flattening and stopping", "WARNING", alert=True)
                    break
                self._tick(now, t_range, t_cut)
                if self.risk.halted and not self.positions:
                    if now < t_flat - timedelta(seconds=1):
                        self.say(f"done for the day: {self.risk.halted}")
                    break
                self.clock.sleep(self.cfg.session.poll_seconds)
        except KeyboardInterrupt:
            self.say("interrupted - flattening", "WARNING", alert=True)
            self.flatten_all("interrupted")
            raise
        finally:
            if self.positions:
                self.flatten_all("end of window" if self.clock.now() >= t_flat else "stopping")
        s = self.journal.summary(self.mode, d)
        self.say(f"day complete: {s['trades']} trades, net {s['net']:+.2f}, win rate {s['win_rate']:.0f}%",
                 alert=True)
        return s

    def run_forever(self) -> None:
        while True:
            self.run_day()
            if os.path.exists(self.kill_file):
                self.say("kill switch file present - not scheduling another day")
                return
            nxt = self.cal.next_trading_day(self.clock.now().date(), include_today=False)
            wake = self.cal.open_time(nxt) - timedelta(minutes=self.cfg.session.preflight_minutes + 5)
            self.say(f"next session {nxt} - sleeping until {wake:%Y-%m-%d %H:%M} ET")
            self.clock.sleep_until(wake, chunk=300)

    # ---- phases -----------------------------------------------------------------------------
    def _preflight(self) -> None:
        eq, bp = self.broker.account()
        self.equity, self.buying_power = eq, bp
        d = self.clock.now().date()
        dt = self.risk.day_trades_in_window(d)
        self.say(f"pre-flight: equity ${eq:,.2f}, buying power ${bp:,.2f}, day trades in last 5 "
                 f"sessions: {dt}, risk/trade ${self.cfg.risk.risk_per_trade_usd:g}, daily loss cap "
                 f"${self.cfg.risk.max_daily_loss_usd:g}")
        if self.cfg.risk.pdt_guard and eq < self.cfg.risk.pdt_equity_threshold:
            left = max(0, self.cfg.risk.pdt_max_day_trades - dt)
            self.say(f"account under ${self.cfg.risk.pdt_equity_threshold:,.0f}: PDT guard allows {left} "
                     f"more day trade(s) in the rolling window")
        if bp <= 0:
            self.risk.halt("no buying power")

    def _reconcile(self, d: date) -> None:
        """Pick up positions this bot opened earlier today (e.g. after a restart)."""
        held = self.broker.positions()
        for row in self.journal.open_trades(self.mode):
            sym = row["symbol"]
            if sym in held and row["day"] == d.isoformat():
                qty = min(row["qty"], held[sym])
                risk = max(0.01, row["entry"] - row["stop"])
                self.positions[sym] = Position(sym, qty, row["entry"], row["stop"], row["target"], risk,
                                               trade_id=row["id"], high_water=row["entry"])
                self.done[sym] = "already traded today"
                self.say(f"resumed managing {sym}: {qty:g} @ {row['entry']:.2f} (stop {row['stop']:.2f})")
            elif sym in held:
                self.say(f"{sym}: position from {row['day']} is still open at the broker - not touching "
                         f"it; close it in the Public app", "WARNING", alert=True)
            else:
                self.journal.close_trade(row["id"], self.clock.now(), row["entry"],
                                         "closed outside the bot (P&L unknown)", qty=row["qty"])
                self.say(f"{sym}: journal trade {row['id']} no longer held at broker - marked closed", "WARNING")
        if self.positions and not self.broker.software_stops_only:
            # Only the symbols this bot is managing (e.g. its own safety stops from before a restart).
            for oid in self.broker.open_order_ids(list(self.positions)):
                self.say(f"cancelling stale bot order {oid}", "WARNING")
                self.broker.cancel(oid)
        if self.cfg.execution.broker_stop and not self.broker.software_stops_only:
            for p in self.positions.values():
                self._place_broker_stop(p)     # stale stops were just cancelled above

    def _refresh_bars(self, now: datetime, symbols: List[str]) -> None:
        minute = int(now.timestamp() // 60)
        for s in symbols:
            if self.bars_minute.get(s) == minute:
                continue
            try:
                self.bars[s] = self.market.bars(s)
                self.bars_minute[s] = minute
            except Exception as e:  # noqa: BLE001
                log.warning("bars for %s failed: %s", s, e)

    def _tick(self, now: datetime, t_range: datetime, t_cut: datetime) -> None:
        try:
            self.quotes = self.market.quotes(self.cfg.watchlist)
            self.data_errors = 0
        except Exception as e:  # noqa: BLE001
            self.data_errors += 1
            log.warning("quote request failed (%d in a row): %s", self.data_errors, e)
            if self.data_errors >= 15 and self.positions:
                self.say("market data down for ~30s with open positions - flattening", "ERROR", alert=True)
                self.flatten_all("market data outage")
                self.risk.halt("market data outage")
            return

        # 1) manage what we hold
        for sym in list(self.positions):
            try:
                self._manage(sym, now)
            except Exception as e:  # noqa: BLE001 - retry next poll rather than abandon the position
                self.say(f"{sym}: managing position failed ({e}) - retrying", "ERROR")

        # 2) daily loss limit counts open losses too
        unreal = sum(p.unrealized((self.quotes.get(s) or Quote(s, None, None, None)).bid)
                     for s, p in self.positions.items())
        if not self.risk.halted and self.risk.loss_breached(now.date(), unreal):
            self.risk.halt("daily loss limit")
            self.say(f"daily loss limit ${self.cfg.risk.max_daily_loss_usd:g} hit - flattening", "WARNING",
                     alert=True)
            self.flatten_all("daily loss limit")
            return

        # 3) entries
        if now < t_range or now >= t_cut or self.risk.halted:
            if now >= t_cut and not self.cutoff_announced:
                self.cutoff_announced = True
                self.say(f"entry cutoff {t_cut:%H:%M} - managing open positions only")
            return
        candidates = [s for s in self.cfg.watchlist if s not in self.done and s not in self.positions]
        self._refresh_bars(now, candidates)
        for sym in candidates:
            bars = self.bars.get(sym) or []
            if sym not in self.ranges:
                orng = opening_range(bars, self.cfg.session.opening_range_minutes)
                if not orng:
                    self._note(sym, "waiting for opening-range bars")
                    continue
                self.ranges[sym] = orng
                why = screen_range(orng, self.cfg.strategy)
                if why:
                    self.done[sym] = why
                    self.say(f"{sym}: skip today - {why}")
                    continue
                self.say(f"{sym}: opening range {orng.low:.2f}-{orng.high:.2f} "
                         f"({orng.width / orng.high * 100:.2f}%), vol {orng.volume:,.0f}")
            q = self.quotes.get(sym)
            if not q:
                continue
            sig, why = evaluate_long(sym, q, bars, self.ranges[sym], self.cfg.strategy)
            if not sig:
                self._note(sym, why)
                continue
            ok, why = self.risk.can_enter(now.date(), len(self.positions), self.equity)
            if not ok:
                self._note(sym, f"signal but blocked: {why}")
                if self.status.get("_risk") != why:
                    self.status["_risk"] = why
                    self.say(f"{sym}: breakout but no entry - {why}")
                continue
            try:
                self._enter(sig, q, now)
            except Exception as e:  # noqa: BLE001 - e.g. order rejected; skip this symbol, keep going
                self.done[sym] = f"entry error: {e}"
                self.say(f"{sym}: entry failed ({e}) - skipping it today", "ERROR", alert=True)

    # ---- execution --------------------------------------------------------------------------
    def _enter(self, sig, q: Quote, now: datetime) -> None:
        ex = self.cfg.execution
        try:
            self.equity, self.buying_power = self.broker.account()
        except Exception as e:  # noqa: BLE001
            log.warning("account refresh failed: %s", e)
        qty = self.risk.size(q.ask, sig.stop, self.buying_power, ex.fractional)
        if qty <= 0:
            self.done[sig.symbol] = "position size rounds to 0"
            self.say(f"{sig.symbol}: signal but size rounds to 0 shares at {q.ask:.2f} "
                     f"(risk/share {q.ask - sig.stop:.2f}) - raise risk_per_trade_usd/max_position_usd "
                     f"or enable fractional")
            return
        limit = round(q.ask * (1 + ex.entry_slippage_pct / 100), 2)
        self.say(f"{sig.symbol}: BUY {qty:g} @ limit {limit:.2f} - {sig.reason}")
        st = self.broker.buy(sig.symbol, qty, limit)
        self.done[sig.symbol] = "traded today"
        if st.filled_qty <= 0 or not st.avg_price:
            self.say(f"{sig.symbol}: entry not filled ({st.status}{', ' + st.reject_reason if st.reject_reason else ''})")
            return
        fill = st.avg_price
        risk_ps = fill - sig.stop
        if risk_ps <= 0:
            pos = Position(sig.symbol, st.filled_qty, fill, sig.stop, fill, 0.01)
            pos.trade_id = self.journal.open_trade(self.mode, now, sig.symbol, st.filled_qty, fill,
                                                   sig.stop, fill, sig.reason)
            self.positions[sig.symbol] = pos
            self._exit(sig.symbol, "filled below stop")
            return
        target = round(fill + self.cfg.strategy.target_r * risk_ps, 2)
        pos = Position(sig.symbol, st.filled_qty, fill, sig.stop, target, risk_ps, high_water=fill)
        pos.trade_id = self.journal.open_trade(self.mode, now, sig.symbol, st.filled_qty, fill,
                                               sig.stop, target, sig.reason)
        self.positions[sig.symbol] = pos
        self.say(f"{sig.symbol}: FILLED {st.filled_qty:g} @ {fill:.2f}; stop {sig.stop:.2f} "
                 f"(risk ${risk_ps * st.filled_qty:.2f}), target {target:.2f}", alert=True)
        if ex.broker_stop and not self.broker.software_stops_only:
            self._place_broker_stop(pos)

    def _place_broker_stop(self, pos: Position) -> None:
        px = round(pos.stop - self.cfg.execution.broker_stop_extra_r * pos.risk_per_share, 2)
        if px <= 0:
            return
        try:
            pos.broker_stop_id = self.broker.place_stop(pos.symbol, pos.qty, px)
            pos.broker_stop_price = px
            self.say(f"{pos.symbol}: safety STOP resting at Public @ {px:.2f}")
        except Exception as e:  # noqa: BLE001
            self.say(f"{pos.symbol}: could not place safety stop ({e}) - software stop only", "WARNING",
                     alert=True)

    def _manage(self, sym: str, now: datetime) -> None:
        pos = self.positions[sym]
        # Did the broker-side safety stop fire (gap through our stop, or the bot lagged)?
        if pos.broker_stop_id and (now - self.last_stop_check).total_seconds() >= 10:
            self.last_stop_check = now
            try:
                st = self.broker.stop_state(pos.broker_stop_id)
                if st and st.status == "FILLED":
                    self._record_exit(pos, st.avg_price or pos.broker_stop_price or pos.stop,
                                      "broker safety stop", now)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("stop status for %s failed: %s", sym, e)
        q = self.quotes.get(sym)
        if not q:
            return
        reason = manage(pos, q, self.cfg.strategy)
        if pos.notes:
            for n in pos.notes:
                self.say(f"{sym}: {n}")
            pos.notes.clear()
        if reason:
            self._exit(sym, reason)

    def _exit(self, sym: str, reason: str) -> None:
        pos = self.positions.get(sym)
        if not pos:
            return
        now = self.clock.now()
        if pos.broker_stop_id:
            try:
                st = self.broker.cancel(pos.broker_stop_id)
                if st and st.status == "FILLED":
                    self._record_exit(pos, st.avg_price or pos.stop, "broker safety stop", now)
                    return
                if st and st.filled_qty:
                    pos.qty -= st.filled_qty
            except Exception as e:  # noqa: BLE001
                self.say(f"{sym}: cancelling safety stop failed ({e})", "WARNING")
        q = self.quotes.get(sym)
        limit = None
        if q and q.bid:
            limit = round(q.bid * (1 - self.cfg.execution.exit_slippage_pct / 100), 2)
        filled, value = 0.0, 0.0
        st = self.broker.sell(sym, pos.qty, limit)
        if st.filled_qty:
            filled += st.filled_qty
            value += st.filled_qty * (st.avg_price or limit or pos.entry)
        if filled < pos.qty - 1e-9:
            st = self.broker.sell(sym, pos.qty - filled, None)       # market for the remainder
            if st.filled_qty:
                filled += st.filled_qty
                value += st.filled_qty * (st.avg_price or (q.bid if q else pos.entry))
        if filled < pos.qty - 1e-9:
            self.say(f"{sym}: EXIT INCOMPLETE - {pos.qty - filled:g} shares still open. Check the Public "
                     f"app.", "ERROR", alert=True)
            pos.qty -= filled
            if filled <= 0:
                return
        self._record_exit(pos, value / filled, reason, now, qty=filled)

    def _record_exit(self, pos: Position, px: float, reason: str, now: datetime,
                     qty: Optional[float] = None) -> None:
        pnl = self.journal.close_trade(pos.trade_id, now, round(px, 4), reason, qty=qty or pos.qty)
        self.positions.pop(pos.symbol, None)
        self.say(f"{pos.symbol}: SOLD {qty or pos.qty:g} @ {px:.2f} ({reason}) P&L {pnl:+.2f}; "
                 f"day net {self.journal.realized(self.mode, now.date()):+.2f}", alert=True)

    def flatten_all(self, reason: str) -> None:
        for sym in list(self.positions):
            try:
                self._exit(sym, reason)
            except Exception as e:  # noqa: BLE001
                self.say(f"{sym}: flatten failed ({e}) - CLOSE IT IN THE PUBLIC APP", "ERROR", alert=True)
