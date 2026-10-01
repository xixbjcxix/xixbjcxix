"""SQLite persistence: raw recordings, markets, quotes, orders, fills, PnL and risk events."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .models import Fill, MarketInfo, Order

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recv_ms INTEGER NOT NULL,
  source TEXT NOT NULL,           -- pm | spot | meta
  asset TEXT,                     -- for spot rows: btc | eth
  payload BLOB NOT NULL,          -- zlib-compressed JSON when z=1
  z INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_recv ON raw_messages(recv_ms, id);
CREATE TABLE IF NOT EXISTS markets (
  slug TEXT PRIMARY KEY, asset TEXT, horizon_s INTEGER, start_ms INTEGER, end_ms INTEGER,
  token_up TEXT, token_down TEXT, condition_id TEXT, tick_size REAL, min_order_size REAL,
  fees_enabled INTEGER, fee_rate REAL, fee_exponent REAL, rebate_rate REAL,
  winner TEXT, open_price REAL, meta TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_ms INTEGER, mode TEXT, label TEXT, config TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
  ts_ms INTEGER, run_id TEXT, market TEXT, outcome TEXT, kind TEXT,  -- kind: tob | ours
  bid REAL, ask REAL, bid_size REAL, ask_size REAL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT, run_id TEXT, ts_ms INTEGER, market TEXT, outcome TEXT, side TEXT, price REAL, size REAL,
  post_only INTEGER, tag TEXT, status TEXT, reason TEXT, PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS order_events (
  ts_ms INTEGER, run_id TEXT, order_id TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  ts_ms INTEGER, run_id TEXT, order_id TEXT, market TEXT, outcome TEXT, side TEXT, price REAL,
  size REAL, liquidity TEXT, fee REAL, rebate REAL, tag TEXT
);
CREATE TABLE IF NOT EXISTS pnl_events (
  ts_ms INTEGER, run_id TEXT, market TEXT, kind TEXT, pnl REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
  ts_ms INTEGER, run_id TEXT, kind TEXT, detail TEXT
);
"""


class Store:
    def __init__(self, path: str, flush_every: int = 2000, compress_raw: bool = True) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._buf: Dict[str, List[Tuple]] = {}
        self._sql: Dict[str, str] = {}
        self.flush_every = flush_every
        self._n = 0
        self.enabled_quotes = True
        self.compress_raw = compress_raw

    # ---- buffered inserts ------------------------------------------------------------
    def _add(self, table: str, sql: str, row: Tuple) -> None:
        self._sql[table] = sql
        self._buf.setdefault(table, []).append(row)
        self._n += 1
        if self._n >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._n:
            return
        with self.conn:
            for t, rows in self._buf.items():
                if rows:
                    self.conn.executemany(self._sql[t], rows)
        self._buf = {}
        self._n = 0

    def close(self) -> None:
        self.flush()
        self.conn.close()

    # ---- recorder --------------------------------------------------------------------
    def raw(self, recv_ms: int, source: str, payload: Any, asset: Optional[str] = None) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"))
        if self.compress_raw and len(payload) > 200:
            row = (recv_ms, source, asset, zlib.compress(payload.encode(), 6), 1)
        else:
            row = (recv_ms, source, asset, payload, 0)
        self._add("raw", "INSERT INTO raw_messages(recv_ms,source,asset,payload,z) VALUES (?,?,?,?,?)", row)

    def upsert_market(self, m: MarketInfo, meta: Optional[dict] = None) -> None:
        self.flush()
        with self.conn:
            self.conn.execute(
                """INSERT INTO markets(slug,asset,horizon_s,start_ms,end_ms,token_up,token_down,condition_id,
                   tick_size,min_order_size,fees_enabled,fee_rate,fee_exponent,rebate_rate,winner,open_price,meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET winner=COALESCE(excluded.winner, markets.winner),
                   open_price=COALESCE(excluded.open_price, markets.open_price),
                   fee_rate=COALESCE(excluded.fee_rate, markets.fee_rate),
                   fee_exponent=COALESCE(excluded.fee_exponent, markets.fee_exponent),
                   rebate_rate=COALESCE(excluded.rebate_rate, markets.rebate_rate),
                   meta=excluded.meta""",
                (m.slug, m.asset, m.horizon_s, m.start_ms, m.end_ms, m.token_up, m.token_down, m.condition_id,
                 m.tick_size, m.min_order_size, int(m.fees_enabled), m.fee_rate, m.fee_exponent, m.rebate_rate,
                 m.winner, m.open_price, json.dumps(dict(meta or {}, series=m.series, fee_type=m.fee_type,
                                                         fee_multiplier=m.fee_multiplier,
                                                         strike_type=m.strike_type))))

    def set_winner(self, slug: str, winner: str) -> None:
        self.flush()
        with self.conn:
            self.conn.execute("UPDATE markets SET winner=? WHERE slug=?", (winner, slug))

    def load_markets(self) -> List[MarketInfo]:
        rows = self.conn.execute(
            """SELECT slug,asset,horizon_s,start_ms,end_ms,token_up,token_down,condition_id,tick_size,
               min_order_size,fees_enabled,fee_rate,fee_exponent,rebate_rate,winner,open_price,meta FROM markets
               ORDER BY start_ms""").fetchall()
        out = []
        for r in rows:
            meta = json.loads(r[16] or "{}")
            out.append(MarketInfo(slug=r[0], asset=r[1], horizon_s=r[2], start_ms=r[3], end_ms=r[4], token_up=r[5],
                                  token_down=r[6], condition_id=r[7] or "", tick_size=r[8] or 0.01,
                                  min_order_size=r[9] or 1.0, fees_enabled=bool(r[10]), fee_rate=r[11],
                                  fee_exponent=r[12], rebate_rate=r[13], winner=r[14], open_price=r[15],
                                  series=meta.get("series", ""),
                                  fee_type=meta.get("fee_type", "quadratic_with_maker_fees"),
                                  fee_multiplier=float(meta.get("fee_multiplier", 1.0) or 1.0),
                                  strike_type=meta.get("strike_type", "greater_or_equal")))
        return out

    def iter_raw(self, start_ms: Optional[int] = None, end_ms: Optional[int] = None,
                 batch: int = 20000) -> Iterator[Tuple[int, str, Optional[str], str]]:
        """Yield (recv_ms, source, asset, payload) in receive-time order (keyset pagination)."""
        self.flush()
        q = ("SELECT id, recv_ms, source, asset, payload, z FROM raw_messages "
             "WHERE (recv_ms > ? OR (recv_ms = ? AND id > ?))")
        args: List[Any] = []
        if end_ms is not None:
            q += " AND recv_ms <= ?"
            args.append(end_ms)
        q += " ORDER BY recv_ms, id LIMIT ?"
        last_ms, last_id = (start_ms - 1 if start_ms is not None else -1), 0
        while True:
            rows = self.conn.execute(q, [last_ms, last_ms, last_id] + args + [batch]).fetchall()
            if not rows:
                return
            for r in rows:
                pl = r[4]
                if r[5]:
                    pl = zlib.decompress(pl).decode()
                elif isinstance(pl, bytes):
                    pl = pl.decode()
                yield r[1], r[2], r[3], pl
            last_id, last_ms = rows[-1][0], rows[-1][1]

    def raw_span(self) -> Tuple[Optional[int], Optional[int], int]:
        r = self.conn.execute("SELECT MIN(recv_ms), MAX(recv_ms), COUNT(*) FROM raw_messages").fetchone()
        return r[0], r[1], r[2]

    # ---- trading records ---------------------------------------------------------------
    def run(self, run_id: str, mode: str, label: str, config: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                              (run_id, int(time.time() * 1000), mode, label, json.dumps(config, default=str)))

    def quote(self, ts: int, run_id: str, market: str, outcome: str, kind: str,
              bid, ask, bid_size=None, ask_size=None) -> None:
        if not self.enabled_quotes:
            return
        self._add("quotes", "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?)",
                  (ts, run_id, market, outcome, kind, bid, ask, bid_size, ask_size))

    def order(self, run_id: str, o: Order, ts: int) -> None:
        self._add("orders", "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (o.id, run_id, ts, o.market, o.outcome, o.side, o.price, o.size, int(o.post_only), o.tag,
                   o.status.value, o.reject_reason))

    def order_event(self, run_id: str, oid: str, event: str, ts: int, detail: str = "") -> None:
        self._add("order_events", "INSERT INTO order_events VALUES (?,?,?,?,?)", (ts, run_id, oid, event, detail))

    def fill(self, run_id: str, f: Fill) -> None:
        self._add("fills", "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f.ts_ms, run_id, f.order_id, f.market, f.outcome, f.side, f.price, f.size, f.liquidity,
                   f.fee, f.rebate, f.tag))

    def pnl(self, run_id: str, ts: int, market: str, kind: str, pnl: float, detail: dict) -> None:
        self._add("pnl", "INSERT INTO pnl_events VALUES (?,?,?,?,?,?)",
                  (ts, run_id, market, kind, pnl, json.dumps(detail, default=str)))

    def risk_event(self, run_id: str, ts: int, kind: str, detail: str) -> None:
        self._add("risk", "INSERT INTO risk_events VALUES (?,?,?,?)", (ts, run_id, kind, detail))


class NullStore(Store):
    """Drop-in store that writes nothing (used for fast parameter sweeps)."""

    def __init__(self) -> None:  # noqa: D401
        self.enabled_quotes = False

    def _add(self, *a, **k) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def run(self, *a, **k) -> None:
        pass
