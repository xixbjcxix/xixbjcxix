"""Offline demo: replay a recording through the paper-trading core + dashboard at N x speed.

Useful to try the dashboard and kill switch without network access, and to eyeball
what the strategy does tick by tick on recorded (or synthetic) data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from .config import BotConfig, to_dict
from .core import TradingCore
from .dashboard import DashboardServer
from .replay import apply_row, warm_up
from .report import build_report, format_report
from .store import Store

log = logging.getLogger("kbot.demo")


async def run_demo(cfg: BotConfig, recording_db: str, speed: float = 20.0, minutes: Optional[float] = None,
                   start_offset_min: float = 0.0, stop: Optional[asyncio.Event] = None,
                   dashboard: bool = True) -> dict:
    rec = Store(recording_db)
    lo, hi, _ = rec.raw_span()
    if lo is None:
        raise SystemExit(f"empty recording: {recording_db}")
    start_ms = lo + int(start_offset_min * 60000)
    end_ms = start_ms + int(minutes * 60000) if minutes else hi
    run_id = f"demo-{uuid.uuid4().hex[:6]}"
    store = Store(cfg.db_path)
    store.run(run_id, "demo", f"replay x{speed}", to_dict(cfg))
    core = TradingCore(cfg, store, run_id, mode="demo")
    for m in rec.load_markets():
        if m.end_ms >= start_ms and m.start_ms <= end_ms:
            core.add_market(m)
    if start_ms > lo:
        warm_up(core, rec, start_ms)
    dash = None
    if dashboard and cfg.dashboard.enabled:
        dash = DashboardServer(core, cfg.dashboard.host, cfg.dashboard.port)
        await dash.start()
        print(f"dashboard: http://{cfg.dashboard.host}:{cfg.dashboard.port}  (replaying x{speed})")
    stop = stop or asyncio.Event()
    wall0 = time.monotonic()
    next_timer = start_ms
    core.risk.halt_until_ms = 0
    try:
        for recv_ms, source, asset, payload in rec.iter_raw(start_ms, end_ms):
            if stop.is_set():
                break
            target = wall0 + (recv_ms - start_ms) / 1000.0 / speed
            delay = target - time.monotonic()
            if delay > 0.005:
                await asyncio.sleep(delay)
            if stop.is_set():
                break
            while recv_ms >= next_timer:
                core.on_timer(next_timer)
                next_timer += cfg.engine.timer_ms
            apply_row(core, recv_ms, source, payload)
    finally:
        if dash:
            await dash.stop()
        store.close()
        rec.close()
    rep = build_report(core, span_ms=(start_ms, min(end_ms, core.now_ms)), label=run_id)
    print(format_report(rep))
    return rep
