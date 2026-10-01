"""SYNTHETIC market generator - for testing the pipeline, NOT evidence of edge.

Writes a recording DB in exactly the format the live recorder produces (raw Polymarket
market-channel frames, Coinbase ticker frames, meta heartbeat/gap rows, markets table),
so the backtester runs the identical code path on synthetic and real data.

Model (per asset, per window):
  * spot: 1 Hz GBM with occasional jumps (BTC ~50%/yr, ETH ~65%/yr vol).
  * true P(UP) = Phi( ln(S_t/S_open) / (sigma * sqrt(tau)) ).
  * market makers quote around a LAGGED estimate (spot lag ~2 s + noise), with
    1-2 tick half-spreads and 5 levels; DOWN book mirrors UP with a small extra spread,
    so best bids sum to ~0.96-0.98 and best asks to ~1.02-1.04 (typical of these books).
  * takers arrive Poisson; a fraction are informed (trade toward the true probability,
    i.e. they pick off stale quotes), the rest are noise. Rare large "dump" orders
    sweep several levels, mostly on the side that spot is moving against.
  * rare websocket gaps (meta rows) followed by fresh book snapshots.
The informed flow + quote lag is what creates adverse selection for resting bids.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import DOWN, UP, MarketInfo, px
from .store import Store

SECONDS_PER_YEAR = 365 * 24 * 3600


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class SynthParams:
    hours: float = 12.0
    start_s: int = 1790000000 // 900 * 900      # a fixed, 15m-aligned epoch for reproducibility
    assets: Tuple[str, ...] = ("btc", "eth")
    horizons: Tuple[int, ...] = (300, 900)
    seed: int = 7
    step_ms: int = 500
    vol: Tuple[Tuple[str, float], ...] = (("btc", 0.50), ("eth", 0.65))
    start_px: Tuple[Tuple[str, float], ...] = (("btc", 78000.0), ("eth", 3100.0))
    jump_prob_per_s: float = 1.0 / 1800
    jump_bps: float = 25.0
    mm_lag_s: int = 2
    mm_noise: float = 0.008
    taker_rate_per_s: float = 0.8
    informed_frac: float = 0.35
    dump_prob_per_step: float = 0.003
    gap_every_h: float = 4.0
    tick: float = 0.01
    pre_open_s: int = 30


class _Book:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}


class SynthGenerator:
    def __init__(self, p: SynthParams, store: Store) -> None:
        self.p = p
        self.store = store
        self.rng = random.Random(p.seed)
        self.vol = dict(p.vol)
        self.px0 = dict(p.start_px)
        self.tx = 0
        self.n_rows = 0

    def _emit(self, ts: int, source: str, payload, asset: Optional[str] = None) -> None:
        # rows are written unsorted; the backtest reads them ordered by (recv_ms, id)
        self.store.raw(ts, source, json.dumps(payload, separators=(",", ":")), asset)
        self.n_rows += 1

    def _token(self) -> str:
        return str(self.rng.getrandbits(250))

    # ---- spot ------------------------------------------------------------------------
    def _spot_paths(self, n_s: int) -> Dict[str, List[float]]:
        out = {}
        for a in self.p.assets:
            sig = self.vol[a] / math.sqrt(SECONDS_PER_YEAR)
            s = self.px0[a]
            path = []
            for _ in range(n_s + 1):
                z = self.rng.gauss(0, 1)
                s *= math.exp(sig * z - 0.5 * sig * sig)
                if self.rng.random() < self.p.jump_prob_per_s:
                    s *= 1 + self.rng.choice((-1, 1)) * self.p.jump_bps * 1e-4 * self.rng.uniform(0.5, 2.0)
                path.append(s)
            out[a] = path
        return out

    # ---- books -------------------------------------------------------------------------
    def _target_levels(self, p_up: float, hs_ticks: int, extra_ticks: int) -> Tuple[Dict, Dict, Dict, Dict]:
        t = self.p.tick
        p_up = min(max(p_up, 0.02), 0.98)
        bid_u = px(max(t, math.floor((p_up - hs_ticks * t) / t + 1e-9) * t))
        ask_u = px(min(1 - t, math.ceil((p_up + hs_ticks * t) / t - 1e-9) * t))
        if ask_u <= bid_u:
            ask_u = px(bid_u + t)
        bid_d = px(1.0 - ask_u - extra_ticks * t)
        ask_d = px(1.0 - bid_u + extra_ticks * t)
        r = self.rng

        def ladder(best: float, direction: int) -> Dict[float, float]:
            lv = {}
            for i in range(5):
                p = px(best + direction * i * t)
                if 0 < p < 1:
                    base = r.uniform(20, 150) if i == 0 else r.uniform(50, 400)
                    lv[p] = round(base, 2)
            return lv

        return ladder(bid_u, -1), ladder(ask_u, 1), ladder(bid_d, -1), ladder(ask_d, 1)

    @staticmethod
    def _diff(old: Dict[float, float], new: Dict[float, float]) -> List[Tuple[float, float]]:
        ch = []
        for p in set(old) | set(new):
            a, b = old.get(p, 0.0), new.get(p, 0.0)
            if abs(a - b) > 1e-9:
                ch.append((p, b))
        return ch

    def _snapshot(self, ts: int, cond: str, tok: str, b: _Book) -> None:
        self._emit(ts, "pm", {"event_type": "book", "market": cond, "asset_id": tok, "timestamp": str(ts),
                              "bids": [{"price": f"{p:.2f}", "size": f"{s:.2f}"} for p, s in sorted(b.bids.items())],
                              "asks": [{"price": f"{p:.2f}", "size": f"{s:.2f}"} for p, s in sorted(b.asks.items(), reverse=True)]})

    # ---- main -------------------------------------------------------------------------
    def generate(self) -> List[MarketInfo]:
        p = self.p
        n_s = int(p.hours * 3600)
        t0 = p.start_s
        spot = self._spot_paths(n_s + 1000)
        # spot frames 1 Hz
        for a in p.assets:
            prod = f"{a.upper()}-USD"
            for i in range(n_s):
                ts = (t0 + i) * 1000 + self.rng.randint(0, 200)
                self._emit(ts, "spot", {"type": "ticker", "product_id": prod, "price": f"{spot[a][i]:.2f}"}, a)
        # gaps
        gaps: List[Tuple[int, int]] = []
        g = t0 + int(p.gap_every_h * 3600 * self.rng.uniform(0.3, 1.0))
        while g < t0 + n_s:
            dur = self.rng.randint(5, 20)
            gaps.append((g * 1000, (g + dur) * 1000))
            g += int(p.gap_every_h * 3600 * self.rng.uniform(0.6, 1.4))
        for gs, ge in gaps:
            self._emit(gs, "meta", {"type": "ws_disconnect", "reason": "synthetic"})
            self._emit(ge, "meta", {"type": "ws_reconnect"})
        hb = t0
        while hb < t0 + n_s:
            if not any(gs <= hb * 1000 < ge for gs, ge in gaps):
                self._emit(hb * 1000, "meta", {"type": "heartbeat"})
            hb += 10

        markets: List[MarketInfo] = []
        for a in p.assets:
            for h in p.horizons:
                start = t0
                while start + h <= t0 + n_s:
                    m = self._window(a, h, start, spot[a], t0, gaps)
                    markets.append(m)
                    start += h
        self.store.flush()
        for m in markets:
            self.store.upsert_market(m, {"synthetic": True})
        return markets

    def _in_gap(self, ts: int, gaps) -> Optional[Tuple[int, int]]:
        for g in gaps:
            if g[0] <= ts < g[1]:
                return g
        return None

    def _window(self, asset: str, h: int, start: int, path: List[float], t0: int, gaps) -> MarketInfo:
        p, r = self.p, self.rng
        horizon = "5m" if h == 300 else "15m"
        slug = f"{asset}-updown-{horizon}-{start}"
        tok_u, tok_d = self._token(), self._token()
        cond = "0x" + format(r.getrandbits(256), "064x")
        k_idx = start - t0
        s_open = path[k_idx]
        s_close = path[k_idx + h]
        winner = UP if s_close >= s_open else DOWN
        sig = self.vol[asset] / math.sqrt(SECONDS_PER_YEAR)
        m = MarketInfo(slug=slug, asset=asset, horizon_s=h, start_ms=start * 1000, end_ms=(start + h) * 1000,
                       token_up=tok_u, token_down=tok_d, condition_id=cond, tick_size=p.tick, min_order_size=5.0,
                       fees_enabled=True, fee_rate=0.07, fee_exponent=1.0, rebate_rate=0.20, winner=winner)
        books = {UP: _Book(), DOWN: _Book()}
        toks = {UP: tok_u, DOWN: tok_d}
        hs_ticks = r.choice((1, 1, 2))
        extra = r.choice((0, 1, 1, 2))
        noise = 0.0
        first_ms = (start - p.pre_open_s) * 1000
        end_ms = (start + h) * 1000
        step = p.step_ms
        was_gap = False
        snap_needed = True
        ts = first_ms
        while ts < end_ms:
            gap = self._in_gap(ts, gaps)
            sec = max(0, (ts // 1000) - t0)
            s_now = path[min(sec, len(path) - 1)]
            s_lag = path[max(0, min(sec - p.mm_lag_s, len(path) - 1))]
            tau = max(1.0, (end_ms - ts) / 1000.0)
            if ts < start * 1000:
                true_p = 0.5
                mm_p = 0.5
            else:
                true_p = _phi(math.log(s_now / s_open) / (sig * math.sqrt(tau)))
                noise = 0.85 * noise + r.gauss(0, p.mm_noise * 0.5)
                mm_p = _phi(math.log(s_lag / s_open) / (sig * math.sqrt(tau))) + noise
            bu, au, bd, ad = self._target_levels(mm_p, hs_ticks, extra)
            targets = {UP: (bu, au), DOWN: (bd, ad)}
            if gap:
                was_gap = True
                ts += step
                continue
            if was_gap:
                was_gap = False
                snap_needed = True
            if snap_needed:
                for o in (UP, DOWN):
                    books[o].bids, books[o].asks = dict(targets[o][0]), dict(targets[o][1])
                    self._snapshot(ts + r.randint(0, 50), cond, toks[o], books[o])
                snap_needed = False
            else:
                changes = []
                for o in (UP, DOWN):
                    for side_name, cur, tgt in (("BUY", books[o].bids, targets[o][0]),
                                                ("SELL", books[o].asks, targets[o][1])):
                        # makers only move when the target best changes, or randomly refresh sizes
                        moved = (max(cur) if cur else None) != (max(tgt) if tgt else None) if side_name == "BUY" \
                            else (min(cur) if cur else None) != (min(tgt) if tgt else None)
                        if moved or r.random() < 0.15:
                            new = tgt if moved else {k: (tgt.get(k, v) if r.random() < 0.5 else v) for k, v in cur.items()}
                            for pp, ss in self._diff(cur, new):
                                changes.append({"asset_id": toks[o], "price": f"{pp:.2f}", "size": f"{ss:.2f}",
                                                "side": side_name})
                            cur.clear()
                            cur.update(new)
                if changes:
                    self._emit(ts + r.randint(0, 40), "pm", {"event_type": "price_change", "market": cond,
                                                             "price_changes": changes, "timestamp": str(ts)})
            # takers
            if ts >= start * 1000:
                n = self._poisson(p.taker_rate_per_s * step / 1000.0)
                for _ in range(n):
                    self._taker(ts + r.randint(50, step - 1), cond, toks, books, true_p, mm_p, informed=r.random() < p.informed_frac)
                if r.random() < p.dump_prob_per_step:
                    move = math.log(s_now / path[max(0, sec - 10)])
                    loser = DOWN if move > 0 else UP
                    if r.random() > 0.7:
                        loser = UP if loser == DOWN else DOWN
                    self._sweep(ts + r.randint(50, step - 1), cond, toks[loser], books[loser], "SELL",
                                size=r.uniform(200, 800))
            ts += step
        # resolution frame
        res_ms = end_ms + 2000
        if not self._in_gap(res_ms, gaps):
            self._emit(res_ms, "pm", {"event_type": "market_resolved", "market": cond, "assets_ids": [tok_u, tok_d],
                                      "winning_asset_id": tok_u if winner == UP else tok_d,
                                      "winning_outcome": "Up" if winner == UP else "Down", "timestamp": str(res_ms)})
        return m

    def _poisson(self, lam: float) -> int:
        l, k, pp = math.exp(-lam), 0, 1.0
        while True:
            pp *= self.rng.random()
            if pp < l:
                return k
            k += 1

    def _taker(self, ts: int, cond: str, toks, books, true_p: float, mm_p: float, informed: bool) -> None:
        r = self.rng
        if informed and abs(true_p - mm_p) > 0.01:
            if true_p > mm_p:
                o, side = (UP, "BUY") if r.random() < 0.5 else (DOWN, "SELL")
            else:
                o, side = (UP, "SELL") if r.random() < 0.5 else (DOWN, "BUY")
        else:
            o = r.choice((UP, DOWN))
            side = r.choice(("BUY", "SELL"))
        size = min(600.0, math.exp(r.gauss(math.log(15), 1.0)))
        self._sweep(ts, cond, toks[o], books[o], side, size)

    def _sweep(self, ts: int, cond: str, tok: str, b: _Book, side: str, size: float) -> None:
        self.tx += 1
        tx = f"0x{self.tx:064x}"
        levels = sorted(b.asks.items()) if side == "BUY" else sorted(b.bids.items(), reverse=True)
        book = b.asks if side == "BUY" else b.bids
        rem = size
        changes = []
        for price, avail in levels[:6]:
            if rem <= 0.01:
                break
            q = min(rem, avail)
            rem -= q
            self._emit(ts, "pm", {"event_type": "last_trade_price", "market": cond, "asset_id": tok,
                                  "price": f"{price:.2f}", "size": f"{q:.2f}", "side": side,
                                  "fee_rate_bps": "0", "timestamp": str(ts), "transaction_hash": tx})
            left = round(avail - q, 2)
            if left <= 0.01:
                book.pop(price, None)
                left = 0.0
            else:
                book[price] = left
            changes.append({"asset_id": tok, "price": f"{price:.2f}", "size": f"{left:.2f}",
                            "side": "SELL" if side == "BUY" else "BUY"})
        if changes:
            self._emit(ts + 1, "pm", {"event_type": "price_change", "market": cond, "price_changes": changes,
                                      "timestamp": str(ts)})


def generate(path: str, params: Optional[SynthParams] = None) -> List[MarketInfo]:
    import os
    if os.path.exists(path):
        os.remove(path)
        for suf in ("-wal", "-shm"):
            if os.path.exists(path + suf):
                os.remove(path + suf)
    st = Store(path, flush_every=50000)
    gen = SynthGenerator(params or SynthParams(), st)
    ms = gen.generate()
    st.close()
    return ms
