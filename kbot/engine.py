"""Live asyncio engine for `record`, `paper` (simulated fills) and `demo` (real demo orders).

  discovery (REST) -> book listener (WS, or REST poll fallback) -> spot/index -> core
     core: signal -> strategy -> risk -> order manager (SimExchange | KalshiExchange)
  -> SQLite store + dashboard

Data environment:
  record / paper : market data from `markets.data_env` (prod by default - read only)
  demo           : market data AND orders on the demo environment (orders must match the book they hit)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from typing import Optional

from .config import BotConfig, credentials, to_dict
from .core import TradingCore
from .dashboard import DashboardServer
from .feeds.discovery import KalshiDiscovery
from .feeds.kalshi_rest import KalshiRest
from .feeds.kalshi_ws import KalshiWS, RestBookPoller
from .feeds.spot import run_exchange_feed
from .report import build_report, format_report
from .store import Store

log = logging.getLogger("kbot.engine")


def startup_error_message(e: Exception, env: str) -> str:
    from .feeds.kalshi_rest import KalshiError
    site = "demo.kalshi.co" if env == "demo" else "kalshi.com"
    if isinstance(e, KalshiError) and e.status in (401, 403):
        return (f"Kalshi rejected the {env} API key ({e.status}). Re-run setup with a key created on {site}, "
                f"and make sure this computer's clock is set automatically.")
    if isinstance(e, KalshiError) and "insufficient_shard_balance" in e.body:
        return ("No cash on Kalshi's crypto exchange (shard 2). Restart the bot and answer y when it offers to "
                "move money there, or move it on the Kalshi website.")
    if isinstance(e, KalshiError):
        return f"Kalshi returned HTTP {e.status} at startup: {e.body[:160]}"
    if isinstance(e, RuntimeError):
        return str(e)
    return f"Could not reach Kalshi ({type(e).__name__}: {e}). Check your internet connection / firewall / VPN."


def now_ms() -> int:
    return int(time.time() * 1000)


class Engine:
    def __init__(self, cfg: BotConfig, mode: str, config_path: Optional[str] = None) -> None:
        assert mode in ("record", "paper", "demo", "live")
        self.trading = mode in ("demo", "live")          # real orders at Kalshi
        self.cfg = cfg
        self.mode = mode
        self.config_path = config_path
        k = cfg.kalshi
        self.env = "demo" if mode == "demo" else "prod" if mode == "live" else cfg.markets.data_env
        if mode == "live" and cfg.live.use_starter_limits:
            lv, r = cfg.live, cfg.risk
            cfg.strategy.clip_shares = min(cfg.strategy.clip_shares, lv.clip_shares)
            for f in ("max_order_usd", "max_market_usd", "max_residual_usd", "max_residual_shares",
                      "max_total_usd", "max_daily_loss_usd"):
                setattr(r, f, min(getattr(r, f), getattr(lv, f)))
        self.creds = credentials(cfg, self.env)
        if mode == "live" and not self.creds:
            raise SystemExit("live trading needs your PRODUCTION API key - run setup and add it as the production key")
        if mode == "demo" and not self.creds:
            raise SystemExit("demo trading needs demo API keys - run `python -m kbot setup` first")
        rest_url = k.demo_rest if self.env == "demo" else k.prod_rest
        self.ws_url = k.demo_ws if self.env == "demo" else k.prod_ws
        try:
            self.rest = KalshiRest(rest_url, **({"key_id": self.creds["key_id"], "key_path": self.creds["key_path"]}
                                                if self.creds else {}),
                                   read_rate=k.read_tokens_per_s, write_rate=k.write_tokens_per_s)
        except FileNotFoundError:
            raise SystemExit(f"private key file not found: {self.creds['key_path']} - run `setup` again "
                             f"from the bot's folder")
        except ValueError as e:
            raise SystemExit(f"could not load the private key ({e}) - run `setup` again")
        self.run_id = f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.rec: Optional[Store] = Store(cfg.recording_db, flush_every=500) \
            if (mode == "record" or cfg.record_raw_in_paper) else None
        self.store = Store(cfg.db_path, flush_every=200)
        self.store.run(self.run_id, mode, f"{mode}@{self.env}", to_dict(cfg))
        self.exchange = None
        factory = None
        if self.trading:
            from .demo_exchange import KalshiExchange

            def factory(core):
                self.exchange = KalshiExchange(self.rest, core, allow_prod=(mode == "live"),
                                               series_prefixes=list(k.series.values()))
                return self.exchange
        self.core = TradingCore(cfg, self.store, self.run_id, mode=mode, exchange_factory=factory)
        if mode == "record":
            self.core.trading_enabled = False
        self.stop = asyncio.Event()
        self.started_ms = now_ms()
        self.index_seen_ms = 0
        self.feed = None
        self.fallback_task: Optional[asyncio.Task] = None

    # ---- callbacks ------------------------------------------------------------------------------
    def _on_frame(self, msg: dict, recv_ms: int, raw: str) -> None:
        t = msg.get("type")
        if t in ("cfbenchmarks_value", "cfbenchmarks_value_5hz"):
            self.index_seen_ms = recv_ms
            if self.rec:
                self.rec.raw(recv_ms, "spot", raw)
            from .feeds.spot import parse_index
            p = parse_index(msg, {v: k for k, v in self.cfg.spot.index_ids.items()})
            if p:
                self.core.on_spot(p[0], p[1], recv_ms)
            return
        if t in ("fill", "user_order", "user_orders"):
            if self.exchange is not None:
                self.exchange.on_private_frame(msg)
            return
        if self.rec:
            self.rec.raw(recv_ms, "kx", raw)
        if self.core.status_message.startswith(("Kalshi " + self.env + " websocket", "Cannot connect")):
            self.core.status_message = ""
        self.core.last_pm_ms = recv_ms
        self.core.on_pm_frame(msg, recv_ms)

    def _on_gap(self, reason: str) -> None:
        t = now_ms()
        if "InvalidStatus" in reason or "401" in reason or "403" in reason:
            self.core.status_message = (f"Kalshi {self.env} websocket refused the API key. Re-run setup "
                                        f"(key must come from {'demo.kalshi.co' if self.env == 'demo' else 'kalshi.com'})"
                                        " and check the computer clock.")
        elif "InvalidURI" in reason or "gaierror" in reason or "ConnectionRefused" in reason or "OSError" in reason:
            self.core.status_message = "Cannot connect to Kalshi's websocket - check internet / firewall / VPN."
        if self.rec:
            self.rec.raw(t, "meta", {"type": "ws_disconnect", "reason": reason})
        self.core.on_data_gap(reason, t)

    def _on_spot(self, asset: str, price: float, recv_ms: int, raw) -> None:
        if self.rec:
            self.rec.raw(recv_ms, "spot", raw if isinstance(raw, str) else raw.decode())
        self.core.on_spot(asset, price, recv_ms)

    def _on_spot_gap(self, reason: str) -> None:
        t = now_ms()
        if self.rec:
            self.rec.raw(t, "meta", {"type": "spot_disconnect", "reason": reason})
        self.core.on_spot_gap(reason, t)

    # ---- preflight (demo / live) -------------------------------------------------------------
    async def _preflight(self, disc: KalshiDiscovery) -> None:
        """Keys -> balance -> sweep our old orders -> existing positions -> 1-cent test order.
        Trading stays disabled until every step passes; failures show on the dashboard."""
        self.core.trading_enabled = False
        where = "kalshi.com (REAL MONEY)" if self.mode == "live" else "demo.kalshi.co"
        try:
            bal = await self.rest.get_balance()
            cents = bal.get("balance")
            dollars = bal.get("balance_dollars") or (cents / 100 if isinstance(cents, (int, float)) else cents)
            log.info("%s balance: $%s", where, dollars)
            n = await self.exchange.sweep_own_orders()
            if n:
                log.info("cancelled %d resting order(s) left by an earlier bot session", n)
            base = await self.exchange.load_baseline()
            mine = {t for t in base if t.startswith(tuple(self.cfg.kalshi.series.values()))}
            self.core.blocked_markets |= mine
            if mine:
                log.warning("you already hold positions in %s - the bot will not trade those markets", sorted(mine))
            from .collateral import market_shard
            shard = await market_shard(self.rest, self.cfg)
            cash = (await self.rest.get_shard_balances()).get(shard, 0.0)
            log.info("cash on the crypto exchange (shard %d): $%.2f", shard, cash)
            if cash < self.cfg.risk.max_order_usd:
                raise RuntimeError(f"only ${cash:.2f} on Kalshi's crypto exchange (shard {shard}). Crypto markets "
                                   f"need cash moved there first: restart and answer y to the transfer "
                                   f"question, or move it on the Kalshi website.")
            if self.cfg.live.smoke_test:
                ms = await disc.discover()
                live_now = [m for m in ms if m.start_ms <= now_ms() < m.end_ms - 120_000]
                if live_now:
                    from .demo_exchange import smoke_test
                    log.info(await smoke_test(self.rest, live_now[0].slug, live_now[0].tick_size))
                else:
                    log.warning("no open market for the test order right now; skipping it")
            self.core.status_message = ""
            self.core.trading_enabled = True
            log.info("preflight passed - trading on %s", where)
        except Exception as e:  # noqa: BLE001
            msg = startup_error_message(e, self.env)
            log.error("preflight failed: %s", msg)
            self.core.status_message = "Preflight failed, not trading: " + msg
            self.core.kill("preflight: " + msg[:120])

    # ---- loops ------------------------------------------------------------------------------------
    async def _timer(self) -> None:
        while not self.stop.is_set():
            t = now_ms()
            if self.rec and t - getattr(self, "_hb", 0) >= 5000 and self.core.data_ok:
                self._hb = t
                self.rec.raw(t, "meta", {"type": "heartbeat"})
            self.core.on_timer(t)
            await asyncio.sleep(self.cfg.engine.timer_ms / 1000.0)

    async def _discovery(self, disc: KalshiDiscovery) -> None:
        while not self.stop.is_set():
            try:
                for m in await disc.discover():
                    if m.slug not in self.core.markets:
                        self.core.add_market(m)
                        if self.rec:
                            self.rec.upsert_market(m, {"source": self.mode, "env": self.env})
                        await self.feed.subscribe_markets([m.slug])
                        log.info("tracking %s  target=%s  closes %s  fees=%s", m.slug, m.open_price,
                                 time.strftime("%H:%M:%S", time.localtime(m.end_ms / 1000)),
                                 self.core.markets[m.slug].fee.name)
                    elif self.rec and m.open_price:
                        self.rec.upsert_market(m)
                t = now_ms()
                for st in list(self.core.markets.values()):
                    m = st.info
                    if m.winner is None and t > m.end_ms + 20000:
                        w = await disc.resolution(m)
                        if w:
                            m.winner = w
                            if self.rec:
                                self.rec.set_winner(m.slug, w)
                self.store.flush()
                if self.rec:
                    self.rec.flush()
            except Exception as e:  # noqa: BLE001
                log.warning("discovery error: %s", e)
            await asyncio.sleep(self.cfg.kalshi.discovery_interval_s)

    async def _index_watchdog(self) -> None:
        """Start the exchange spot fallback if the settlement index isn't streaming."""
        sc = self.cfg.spot
        urls = {"coinbase": sc.coinbase_ws_url, "kraken": sc.kraken_ws_url}
        want_index = sc.source == "kalshi_index" and self.creds is not None
        if want_index:
            await asyncio.sleep(20)
        if not want_index or now_ms() - self.index_seen_ms > 15000:
            src = sc.source if sc.source in ("coinbase", "kraken") else sc.fallback
            log.warning("reference price: using %s spot feed (settlement index %s)", src,
                        "unavailable without keys" if not self.creds else "not streaming")
            await run_exchange_feed(src, self.cfg.markets.assets, urls, self._on_spot, self._on_spot_gap, self.stop)

    async def _status(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(60)
            s = self.core.snapshot()
            log.info("%s: pnl_today=%.2f realized=%.2f settled=%d open_orders=%d data_ok=%s",
                     self.mode, s["pnl_today"], s["realized_total"], s["settled_markets"], len(s["orders"]),
                     s["data_ok"])

    async def run(self, hours: Optional[float] = None) -> dict:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:   # Windows: route Ctrl-C to a clean shutdown (cancel all orders)
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.stop.set))
        k, sc = self.cfg.kalshi, self.cfg.spot
        dash = None
        if self.cfg.dashboard.enabled and self.mode != "record":
            # start the dashboard FIRST so it is reachable even if a later startup step fails
            dash = DashboardServer(self.core, self.cfg.dashboard.host, self.cfg.dashboard.port,
                                   config_path=self.config_path)
            url = await dash.start_any_port()
            banner = "=" * 64
            print(f"\n{banner}\n  Dashboard: {url}\n  Keep this window open - closing it stops the bot.\n{banner}\n",
                  flush=True)
            if self.cfg.dashboard.open_browser:
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass
        if self.creds:
            idx = [sc.index_ids[a] for a in self.cfg.markets.assets] if sc.source == "kalshi_index" else []
            self.feed = KalshiWS(self.ws_url, self.creds["key_id"], self.creds["key_path"], self._on_frame,
                                 self._on_gap, index_ids=idx, private_channels=self.trading,
                                 stale_s=self.cfg.risk.pm_stale_ms / 1000.0)
        else:
            log.warning("no %s API keys: polling public REST order books every %d ms (run `python -m kbot setup` "
                        "for websocket data and the settlement index)", self.env, k.poll_book_ms)
            self.feed = RestBookPoller(self.rest, self._on_frame, self._on_gap, k.poll_book_ms)
        disc = KalshiDiscovery(self.rest, k.series, self.cfg.markets.assets, k.lookahead_windows)
        if self.trading:
            await self._preflight(disc)
        tasks = [asyncio.create_task(self._discovery(disc)), asyncio.create_task(self.feed.run(self.stop)),
                 asyncio.create_task(self._index_watchdog()), asyncio.create_task(self._timer()),
                 asyncio.create_task(self._status())]
        if self.trading:
            tasks += [asyncio.create_task(self.exchange.poll_fills_forever(self.stop)),
                      asyncio.create_task(self.exchange.reconcile_forever(self.stop))]
        if hours:
            async def _deadline():
                await asyncio.sleep(hours * 3600)
                self.stop.set()
            tasks.append(asyncio.create_task(_deadline()))
        await self.stop.wait()
        log.info("stopping: cancelling all orders")
        self.core.cancel_all(now_ms(), "shutdown")
        if self.trading:
            try:
                await self.exchange._cancel_all()
            except Exception as e:  # noqa: BLE001
                log.error("final cancel failed: %s  -> check open orders on %s", e,
                          "kalshi.com" if self.mode == "live" else "demo.kalshi.co")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if dash:
            await dash.stop()
        await self.rest.close()
        rep = build_report(self.core, span_ms=(self.started_ms, now_ms()), label=self.run_id)
        self.store.pnl(self.run_id, now_ms(), "*", "session_report", rep["net_pnl"] or 0.0, rep)
        self.store.close()
        if self.rec:
            self.rec.close()
        if self.mode != "record":
            print(format_report(rep))
        return rep
