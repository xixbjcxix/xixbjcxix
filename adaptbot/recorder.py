"""Tick recorder and reader.  One gzipped JSON-lines file per UTC day.

Line types:
  {"t":"spot","ts":..,"a":"btc","px":..}
  {"t":"meta","ts":..,"ticker":..,"a":..,"open":..,"close":..,"strike":..,"stype":..}
  {"t":"book","ts":..,"ticker":..,"yes":[[p,q],..],"no":[[p,q],..]}
  {"t":"result","ts":..,"ticker":..,"result":"yes"|"no"}
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
import zlib
from datetime import datetime, timezone

from .config import DATA_DIR

log = logging.getLogger("adaptbot.recorder")


class Recorder:
    def __init__(self, folder: str | None = None, enabled: bool = True):
        self.folder = folder or os.path.join(DATA_DIR, "recordings")
        self.enabled = enabled
        self.buf: list[str] = []
        self.last_flush = time.time()
        self.meta_seen: dict[str, tuple] = {}

    def _write(self, d: dict) -> None:
        if self.enabled:
            self.buf.append(json.dumps(d, separators=(",", ":")))
            if len(self.buf) >= 500 or time.time() - self.last_flush > 30:
                self.flush()

    def spot(self, ts: float, asset: str, px: float) -> None:
        self._write({"t": "spot", "ts": round(ts, 3), "a": asset, "px": px})

    def meta(self, ts: float, m) -> None:
        key = (m.open_ts, m.close_ts, m.strike, m.strike_type)
        if self.meta_seen.get(m.ticker) == key:
            return
        self.meta_seen[m.ticker] = key
        self._write({"t": "meta", "ts": round(ts, 3), "ticker": m.ticker, "a": m.asset, "open": m.open_ts,
                     "close": m.close_ts, "strike": m.strike, "stype": m.strike_type})

    def book(self, ts: float, ticker: str, book) -> None:
        self._write({"t": "book", "ts": round(ts, 3), "ticker": ticker,
                     "yes": [[p, q] for p, q in book.yes_bids[:5]],
                     "no": [[p, q] for p, q in book.no_bids[:5]]})

    def result(self, ts: float, ticker: str, result: str) -> None:
        self._write({"t": "result", "ts": round(ts, 3), "ticker": ticker, "result": result})
        self.meta_seen.pop(ticker, None)

    def flush(self) -> None:
        if not self.buf:
            return
        os.makedirs(self.folder, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.folder, f"{day}.jsonl.gz")
        try:
            with gzip.open(path, "at", encoding="utf-8") as f:
                f.write("\n".join(self.buf) + "\n")
            self.buf.clear()
        except OSError as e:
            log.warning("recorder write failed: %r", e)
        self.last_flush = time.time()


def read_records(paths: list[str]):
    """Yield records from recording files in time order (files sorted by name)."""
    for path in sorted(paths):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except (EOFError, OSError, zlib.error) as e:
            log.warning("%s: truncated/corrupt tail ignored (%r)", path, e)
