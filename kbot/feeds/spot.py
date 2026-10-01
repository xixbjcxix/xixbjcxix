"""Reference-price feeds.

Preferred: the CF Benchmarks index that settles these markets, streamed by Kalshi's own
websocket channel `cfbenchmarks_value` (BRTI, ETHUSD_RTI). That arrives on the Kalshi
connection (see kalshi_ws.py); this module parses it and provides exchange fallbacks:
Coinbase Exchange ticker, Kraken v2 ticker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("kbot.spot")

# Known symbols for assets Kalshi has actually listed 15m Up/Down markets for. Add a new asset
# here (and to KalshiConfig.series / SpotConfig.index_ids in config.py) to trade it - nothing else
# in the codebase is hardcoded to a fixed asset list. `index_asset()` below also works for any
# CF Benchmarks index that isn't in this table, as long as it follows the "<ASSET>USD..." naming
# convention (true of every crypto index Kalshi has used so far).
COINBASE_PRODUCTS = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD",
                     "doge": "DOGE-USD", "ltc": "LTC-USD", "avax": "AVAX-USD", "ada": "ADA-USD"}
KRAKEN_SYMBOLS = {"btc": "BTC/USD", "eth": "ETH/USD", "sol": "SOL/USD", "xrp": "XRP/USD",
                  "doge": "DOGE/USD", "ltc": "LTC/USD", "avax": "AVAX/USD", "ada": "ADA/USD"}
# CF Benchmarks Real Time Index ids, by Kalshi asset key. BTC's index (BRTI) doesn't follow the
# "<ASSET>USD_RTI" pattern the others do, so it needs an explicit entry; everything else is
# recovered generically in index_asset() below even if it's missing here.
INDEX_ASSET = {"BRTI": "btc", "ETHUSD_RTI": "eth", "SOLUSD_RTI": "sol", "XRPUSD_RTI": "xrp",
               "DOGEUSD_RTI": "doge", "LTCUSD_RTI": "ltc"}


def index_asset(index_id: str, extra: Optional[Dict[str, str]] = None) -> str:
    """index_id -> kbot asset key, using `extra` (usually cfg.spot.index_ids, reversed) first,
    then the static table above, then the generic "<ASSET>USD..." convention as a last resort -
    so a new asset works once it's added to config, even before this file is updated."""
    iid = str(index_id or "")
    if extra and iid in extra:
        return extra[iid]
    if iid in INDEX_ASSET:
        return INDEX_ASSET[iid]
    return iid.split("USD")[0].lower() if "USD" in iid else ""


def parse_index(payload, index_ids: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, float]]:
    """Kalshi `cfbenchmarks_value` message -> (asset, index value). `index_ids` is an optional
    {index_id: asset} map (pass cfg.spot.index_ids reversed) checked before the built-in tables."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    msg = payload.get("msg", payload)
    iid = str(msg.get("index_id", ""))
    asset = index_asset(iid, index_ids)
    val = None
    data = msg.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(data, dict):
        val = data.get("value", data.get("v"))
    if val is None and isinstance(msg.get("avg_60s_data"), dict):
        val = msg["avg_60s_data"].get("value")
    try:
        return (asset, float(val)) if asset and val is not None else None
    except (TypeError, ValueError):
        return None


def parse_spot(payload) -> Optional[Tuple[str, float]]:
    """Coinbase / Kraken / Kalshi-index frame -> (asset, price)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    t = payload.get("type")
    if t in ("cfbenchmarks_value", "cfbenchmarks_value_5hz") or "index_id" in (payload.get("msg") or {}):
        return parse_index(payload)
    if t in ("ticker", "match", "last_match"):          # Coinbase Exchange
        prod = str(payload.get("product_id", ""))
        try:
            return prod.split("-")[0].lower(), float(payload["price"])
        except (KeyError, ValueError, TypeError):
            return None
    if payload.get("channel") == "ticker" and isinstance(payload.get("data"), list):   # Kraken v2
        for d in payload["data"]:
            sym = str(d.get("symbol", ""))
            try:
                return sym.split("/")[0].lower().replace("xbt", "btc"), float(d["last"])
            except (KeyError, ValueError, TypeError):
                continue
    return None


OnSpot = Callable[[str, float, int, str], None]
OnGap = Callable[[str], None]


async def run_exchange_feed(source: str, assets: List[str], urls: Dict[str, str], on_spot: OnSpot,
                            on_gap: OnGap, stop: asyncio.Event) -> None:
    import websockets

    backoff = 1.0
    while not stop.is_set():
        try:
            if source == "coinbase":
                url = urls["coinbase"]
                sub = {"type": "subscribe", "product_ids": [COINBASE_PRODUCTS[a] for a in assets],
                       "channels": ["ticker"]}
            elif source == "kraken":
                url = urls["kraken"]
                sub = {"method": "subscribe", "params": {"channel": "ticker",
                                                         "symbol": [KRAKEN_SYMBOLS[a] for a in assets]}}
            else:
                raise ValueError(f"unknown spot source {source}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps(sub))
                log.info("spot feed connected: %s", source)
                backoff = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    parsed = parse_spot(raw)
                    if parsed:
                        on_spot(parsed[0], parsed[1], int(time.time() * 1000), raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("spot feed error (%s): %s", source, e)
            on_gap(f"spot_disconnect:{type(e).__name__}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
