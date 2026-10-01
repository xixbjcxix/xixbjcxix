"""Stage 1: replay a recording through TradingCore + SimExchange and report."""
from __future__ import annotations

import time
import uuid
from typing import Callable, Dict, List, Optional

from .config import BotConfig, to_dict
from .core import TradingCore
from .replay import apply_row, warm_up
from .models import MarketInfo
from .report import build_report
from .store import NullStore, Store


def run_backtest(cfg: BotConfig, recording_db: str, label: str = "base",
                 out_store: Optional[Store] = None, progress: Optional[Callable[[int, int], None]] = None,
                 start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> dict:
    rec = Store(recording_db)
    markets = rec.load_markets()
    lo, hi, n_rows = rec.raw_span()
    run_id = f"bt-{label}-{uuid.uuid4().hex[:6]}"
    store = out_store or NullStore()
    store.run(run_id, "backtest", label, to_dict(cfg))
    core = TradingCore(cfg, store, run_id, mode="backtest")
    for m in markets:
        if start_ms and m.end_ms < start_ms:
            continue
        if end_ms and m.start_ms > end_ms:
            continue
        core.add_market(m)
    winners: Dict[str, Optional[str]] = {m.slug: m.winner for m in markets}
    if start_ms and lo is not None and start_ms > lo:
        warm_up(core, rec, start_ms)
    timer_ms = cfg.engine.timer_ms
    next_timer = None
    t_wall = time.time()
    i = 0
    for recv_ms, source, asset, payload in rec.iter_raw(start_ms, end_ms):
        if next_timer is None:
            next_timer = recv_ms
        while recv_ms >= next_timer:
            core.on_timer(next_timer)
            next_timer += timer_ms
        apply_row(core, recv_ms, source, payload)
        i += 1
        if progress and i % 100000 == 0:
            progress(i, n_rows)
    # run timers a little past the end so the final windows settle
    if next_timer is not None:
        stop = next_timer + cfg.backtest.settle_delay_ms + 5000
        while next_timer <= stop:
            core.on_timer(next_timer)
            next_timer += timer_ms
    core.settle_remaining(lambda m: winners.get(m.slug))
    store.flush()
    rec.close()
    rep = build_report(core, span_ms=(lo, hi), label=label)
    rep["run_id"] = run_id
    rep["wall_s"] = round(time.time() - t_wall, 1)
    rep["rows"] = n_rows
    return rep
