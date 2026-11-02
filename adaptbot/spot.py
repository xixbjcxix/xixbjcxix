"""Live reference price from several exchanges.

Kalshi's crypto markets settle on a CF Benchmarks index built from major
USD spot venues, so the median of Coinbase, Kraken and Bitstamp tracks it
closely.  If fewer than `min_sources` feeds are fresh, the price is treated
as unavailable and the engine stops opening positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time

import aiohttp

log = logging.getLogger("adaptbot.spot")

PRODUCTS = {
    "btc": {"coinbase": "BTC-USD", "kraken": "BTC/USD", "bitstamp": "btcusd"},
    "eth": {"coinbase": "ETH-USD", "kraken": "ETH/USD", "bitstamp": "ethusd"},
    "sol": {"coinbase": "SOL-USD", "kraken": "SOL/USD", "bitstamp": "solusd"},
}


class SpotHub:
    def __init__(self, assets: list[str], max_age: float = 5.0, min_sources: int = 2):
        self.assets = [a.lower() for a in assets]
        self.max_age, self.min_sources = max_age, min_sources
        self.last: dict[str, dict[str, tuple[float, float]]] = {a: {} for a in self.assets}
        self.tasks: list[asyncio.Task] = []

    def set(self, asset: str, source: str, price: float) -> None:
        if price > 0:
            self.last[asset][source] = (price, time.time())

    def price(self, asset: str, now: float | None = None) -> tuple[float | None, int]:
        now = now or time.time()
        fresh = [p for p, ts in self.last.get(asset, {}).values() if now - ts <= self.max_age]
        if len(fresh) < self.min_sources:
            return None, len(fresh)
        return statistics.median(fresh), len(fresh)

    def status(self) -> dict:
        now = time.time()
        return {a: {s: {"price": p, "age": round(now - ts, 1)} for s, (p, ts) in src.items()}
                for a, src in self.last.items()}

    def start(self) -> None:
        for name, fn in (("coinbase", self._coinbase), ("kraken", self._kraken), ("bitstamp", self._bitstamp)):
            self.tasks.append(asyncio.create_task(self._forever(name, fn)))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _forever(self, name, fn) -> None:
        delay = 1.0
        while True:
            try:
                async with aiohttp.ClientSession(trust_env=True) as s:
                    await fn(s)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - feeds must never kill the bot
                log.warning("%s feed error: %r (retry in %.0fs)", name, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _coinbase(self, s: aiohttp.ClientSession) -> None:
        prods = {PRODUCTS[a]["coinbase"]: a for a in self.assets}
        async with s.ws_connect("wss://ws-feed.exchange.coinbase.com", heartbeat=20) as ws:
            await ws.send_json({"type": "subscribe", "product_ids": list(prods), "channels": ["ticker"]})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("type") == "ticker" and d.get("product_id") in prods:
                    self.set(prods[d["product_id"]], "coinbase", float(d["price"]))

    async def _kraken(self, s: aiohttp.ClientSession) -> None:
        syms = {PRODUCTS[a]["kraken"]: a for a in self.assets}
        async with s.ws_connect("wss://ws.kraken.com/v2", heartbeat=20) as ws:
            await ws.send_json({"method": "subscribe", "params": {"channel": "ticker", "symbol": list(syms)}})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("channel") == "ticker":
                    for t in d.get("data") or []:
                        if t.get("symbol") in syms and t.get("last"):
                            self.set(syms[t["symbol"]], "kraken", float(t["last"]))

    async def _bitstamp(self, s: aiohttp.ClientSession) -> None:
        chans = {f"live_trades_{PRODUCTS[a]['bitstamp']}": a for a in self.assets}
        async with s.ws_connect("wss://ws.bitstamp.net", heartbeat=20) as ws:
            for c in chans:
                await ws.send_json({"event": "bts:subscribe", "data": {"channel": c}})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("event") == "trade" and d.get("channel") in chans:
                    self.set(chans[d["channel"]], "bitstamp", float(d["data"]["price"]))
                elif d.get("event") == "bts:request_reconnect":
                    break
