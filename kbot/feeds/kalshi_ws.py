"""Kalshi websocket client (docs.kalshi.com/websockets).

One authenticated connection carries:
  orderbook_delta     orderbook_snapshot + orderbook_delta for our market tickers (checked by seq)
  trade               public trades
  cfbenchmarks_value  the CF Benchmarks index that settles the markets (BRTI, ETHUSD_RTI)
  fill, user_orders   our own fills / order updates (demo trading only)
Auth at the handshake: sign  timestamp + "GET" + "/trade-api/ws/v2".
Kalshi sends a ping every 10 s; the websockets library answers with pong automatically.
Any disconnect, sequence gap or silence longer than stale_s -> on_gap() and reconnect, which
yields fresh snapshots.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from .kalshi_rest import auth_headers, load_private_key

log = logging.getLogger("kbot.ws")

OnFrame = Callable[[dict, int, str], None]
OnGap = Callable[[str], None]


class SeqGap(Exception):
    pass


class KalshiWS:
    def __init__(self, url: str, key_id: str, key_path: str, on_frame: OnFrame, on_gap: OnGap,
                 index_ids: Iterable[str] = (), private_channels: bool = False, stale_s: float = 20.0) -> None:
        self.url = url
        self.path = urlparse(url).path or "/trade-api/ws/v2"
        self.key_id = key_id
        self.pkey = load_private_key(key_path)
        self.on_frame = on_frame
        self.on_gap = on_gap
        self.index_ids = list(index_ids)
        self.private_channels = private_channels
        self.stale_s = stale_s
        self.tickers: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._ws = None
        self._id = 0
        self._seq: Dict[int, int] = {}
        self.connected = asyncio.Event()
        self.last_msg_ms = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, cmd: dict) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(cmd))

    async def subscribe_markets(self, tickers: Iterable[str]) -> None:
        new = set(tickers) - self.tickers
        self.tickers |= new
        if new and self.connected.is_set():
            await self._subscribe_books(sorted(new))

    async def _subscribe_books(self, tickers: List[str]) -> None:
        await self._send({"id": self._next_id(), "cmd": "subscribe",
                          "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": tickers}})
        self._subscribed |= set(tickers)

    def check_seq(self, msg: dict) -> None:
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return
        if msg.get("type") == "orderbook_snapshot":
            self._seq[sid] = seq
            return
        if msg.get("type") != "orderbook_delta":
            return
        prev = self._seq.get(sid)
        if prev is not None and seq != prev + 1:
            raise SeqGap(f"sid {sid}: expected {prev + 1}, got {seq}")
        self._seq[sid] = seq

    async def _connect(self):
        import websockets
        headers = auth_headers(self.key_id, self.pkey, "GET", self.path)
        try:
            return await websockets.connect(self.url, additional_headers=headers, max_size=2**24,
                                            ping_interval=20, ping_timeout=20)
        except TypeError:   # websockets < 14
            return await websockets.connect(self.url, extra_headers=headers, max_size=2**24,
                                            ping_interval=20, ping_timeout=20)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                ws = await self._connect()
                self._ws = ws
                self._seq.clear()
                self._subscribed.clear()
                try:
                    if self.index_ids:
                        await self._send({"id": self._next_id(), "cmd": "subscribe",
                                          "params": {"channels": ["cfbenchmarks_value"], "index_ids": self.index_ids}})
                    if self.private_channels:
                        await self._send({"id": self._next_id(), "cmd": "subscribe",
                                          "params": {"channels": ["fill", "user_orders"]}})
                    if self.tickers:
                        await self._subscribe_books(sorted(self.tickers))
                    self.connected.set()
                    log.info("kalshi ws connected (%s), %d markets", self.url, len(self.tickers))
                    backoff = 1.0
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_s)
                        recv_ms = int(time.time() * 1000)
                        self.last_msg_ms = recv_ms
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if msg.get("type") == "error":
                            log.warning("kalshi ws error: %s", msg)
                            continue
                        self.check_seq(msg)
                        self.on_frame(msg, recv_ms, raw if isinstance(raw, str) else raw.decode())
                finally:
                    self.connected.clear()
                    self._ws = None
                    await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - disconnect, timeout, seq gap: all are data gaps
                log.warning("kalshi ws gap: %s %s", type(e).__name__, e)
                self.on_gap(f"kalshi_ws:{type(e).__name__}")
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


class RestBookPoller:
    """Fallback when there are no keys for the data environment: poll public REST order books
    (and trades) and turn them into the same snapshot/trade frames the websocket would send."""

    def __init__(self, rest, on_frame: OnFrame, on_gap: OnGap, poll_ms: int = 1000) -> None:
        self.rest = rest
        self.on_frame = on_frame
        self.on_gap = on_gap
        self.poll_ms = poll_ms
        self.tickers: Set[str] = set()
        self.last_trade_ts: Dict[str, int] = {}
        self.seen_trades: Set[str] = set()

    async def subscribe_markets(self, tickers: Iterable[str]) -> None:
        self.tickers |= set(tickers)

    async def run(self, stop: asyncio.Event) -> None:
        fails = 0
        while not stop.is_set():
            t0 = time.monotonic()
            for tk in sorted(self.tickers):
                try:
                    ob = await self.rest.get_orderbook(tk)
                    body = ob.get("orderbook_fp") or ob.get("orderbook") or {}
                    frame = {"type": "orderbook_snapshot", "msg": {
                        "market_ticker": tk,
                        "yes_dollars_fp": body.get("yes_dollars") or body.get("yes_dollars_fp") or [],
                        "no_dollars_fp": body.get("no_dollars") or body.get("no_dollars_fp") or []}}
                    self.on_frame(frame, int(time.time() * 1000), json.dumps(frame))
                    first = tk not in self.last_trade_ts
                    trades = await self.rest.get_trades(tk, min_ts=self.last_trade_ts.get(tk), limit=100)
                    for tr in sorted(trades, key=lambda x: str(x.get("created_time", ""))):
                        tid = str(tr.get("trade_id", ""))
                        if tid in self.seen_trades:
                            continue
                        if first:                 # history from before we started: never replay as fills
                            self.seen_trades.add(tid)
                            continue
                        self.seen_trades.add(tid)
                        f = {"type": "trade", "msg": {
                            "market_ticker": tk, "trade_id": tid,
                            "yes_price_dollars": tr.get("yes_price_dollars"),
                            "no_price_dollars": tr.get("no_price_dollars"),
                            "count_fp": tr.get("count_fp", tr.get("count")), "taker_side": tr.get("taker_side")}}
                        self.on_frame(f, int(time.time() * 1000), json.dumps(f))
                    self.last_trade_ts[tk] = int(time.time()) - 5
                    fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    log.warning("REST poll failed for %s: %s", tk, e)
                    if fails >= 3:
                        self.on_gap("rest_poll_failures")
            if len(self.seen_trades) > 50000:
                self.seen_trades.clear()
            await asyncio.sleep(max(0.05, self.poll_ms / 1000.0 - (time.monotonic() - t0)))
