"""Feed recorded rows into a TradingCore (shared by backtest and demo replay).

Row sources: "kx" = Kalshi websocket frame (orderbook / trade / lifecycle),
             "spot" = reference price (CF Benchmarks index via Kalshi, or Coinbase/Kraken),
             "meta" = recorder heartbeats and disconnects.
"""
from __future__ import annotations

import json

from .feeds.spot import parse_spot


def apply_row(core, recv_ms: int, source: str, payload: str) -> None:
    if source in ("kx", "pm"):
        core.on_pm_frame(json.loads(payload), recv_ms)
    elif source == "spot":
        ps = parse_spot(payload)
        if ps:
            core.on_spot(ps[0], ps[1], recv_ms)
    elif source == "meta":
        t = json.loads(payload).get("type")
        if t == "ws_disconnect":
            core.on_data_gap("disconnect(recorded)", recv_ms)
        elif t == "spot_disconnect":
            core.on_spot_gap("spot_disconnect(recorded)", recv_ms)
        elif t in ("heartbeat", "ws_reconnect"):
            core.last_pm_ms = recv_ms


WARMUP_MS = 16 * 60 * 1000   # one 15m window + margin, so books/spot history are primed


def warm_up(core, rec, start_ms: int) -> None:
    core.trading_enabled = False
    for recv_ms, source, asset, payload in rec.iter_raw(start_ms - WARMUP_MS, start_ms - 1):
        apply_row(core, recv_ms, source, payload)
    core.trading_enabled = True
