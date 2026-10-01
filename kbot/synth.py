"""SYNTHETIC Kalshi data - for testing the pipeline, NOT evidence of edge.

Same world model as the generic generator in _synth_base.py (GBM spot with jumps, market makers quoting a
lagged fair value, Poisson takers with an informed share, occasional sweeps, rare websocket
gaps), but emitted exactly as Kalshi sends it: orderbook_snapshot / orderbook_delta with YES
bids and NO bids, `trade` messages with taker_side, and a markets table with floor_strike
(the target = index at window open) and the result. Written in the recorder's format, so the
backtest runs the identical code path on synthetic and recorded data.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from typing import Dict, List, Optional

from ._synth_base import SECONDS_PER_YEAR, SynthGenerator, SynthParams, _phi
from .book import no_token, yes_token
from .models import DOWN, UP, MarketInfo, px
from .store import Store


class KalshiSynth(SynthGenerator):
    def _emit(self, ts, source, payload, asset=None):
        if source == "pm":
            source = "kx"
        super()._emit(ts, source, payload, asset)

    def _ksnap(self, ts: int, tk: str, lad: Dict[str, Dict[float, float]]) -> None:
        self._emit(ts, "kx", {"type": "orderbook_snapshot", "msg": {
            "market_ticker": tk,
            "yes_dollars_fp": [[f"{p:.4f}", f"{c:.2f}"] for p, c in sorted(lad["yes"].items())],
            "no_dollars_fp": [[f"{p:.4f}", f"{c:.2f}"] for p, c in sorted(lad["no"].items())]}})

    def _kdelta(self, ts: int, tk: str, side: str, p: float, delta: float) -> None:
        self._emit(ts, "kx", {"type": "orderbook_delta", "msg": {
            "market_ticker": tk, "price_dollars": f"{p:.4f}", "delta_fp": f"{delta:.2f}", "side": side,
            "ts_ms": ts}})

    def _ksweep(self, ts: int, tk: str, lad, consume: str, size: float) -> None:
        """Taker hits resting `consume` bids (consume='yes' -> taker bought NO)."""
        book = lad[consume]
        rem = size
        for price in sorted(book, reverse=True)[:6]:
            if rem <= 0.01:
                break
            q = min(rem, book[price])
            rem -= q
            self.tx += 1
            yes_px = price if consume == "yes" else px(1.0 - price)
            self._emit(ts, "kx", {"type": "trade", "msg": {
                "market_ticker": tk, "trade_id": f"t{self.tx}", "yes_price_dollars": f"{yes_px:.4f}",
                "no_price_dollars": f"{1 - yes_px:.4f}", "count_fp": f"{q:.2f}",
                "taker_side": "no" if consume == "yes" else "yes"}})
            left = round(book[price] - q, 2)
            if left <= 0.01:
                book.pop(price)
                left = 0.0
            else:
                book[price] = left
            self._kdelta(ts + 1, tk, consume, price, -q)

    def _window(self, asset, h, start, path, t0, gaps) -> MarketInfo:
        p, r = self.p, self.rng
        k_idx = start - t0
        s_open, s_close = path[k_idx], path[k_idx + h]
        target = round(s_open, 2)
        winner = UP if s_close >= target else DOWN
        stamp = __import__("time").strftime("%y%b%d%H%M", __import__("time").gmtime(start + h)).upper()
        tk = f"KX{asset.upper()}15M-{stamp}-{int(start + h) % 100:02d}"
        sig = self.vol[asset] / math.sqrt(SECONDS_PER_YEAR)
        m = MarketInfo(slug=tk, asset=asset, horizon_s=h, start_ms=start * 1000, end_ms=(start + h) * 1000,
                       token_up=yes_token(tk), token_down=no_token(tk), condition_id=tk.rsplit("-", 1)[0],
                       tick_size=p.tick, min_order_size=1.0, winner=winner, open_price=target,
                       series=f"KX{asset.upper()}15M", fee_type="quadratic_with_maker_fees", fee_multiplier=1.0)
        lad = {"yes": {}, "no": {}}
        hs_ticks = r.choice((1, 1, 2))
        extra = r.choice((0, 1, 1, 2))
        noise = 0.0
        end_ms = (start + h) * 1000
        ts = (start - p.pre_open_s) * 1000
        was_gap, snap_needed = False, True
        while ts < end_ms:
            gap = self._in_gap(ts, gaps)
            sec = max(0, (ts // 1000) - t0)
            s_now = path[min(sec, len(path) - 1)]
            s_lag = path[max(0, min(sec - p.mm_lag_s, len(path) - 1))]
            tau = max(1.0, (end_ms - ts) / 1000.0)
            if ts < start * 1000:
                true_p = mm_p = 0.5
            else:
                true_p = _phi(math.log(s_now / target) / (sig * math.sqrt(tau)))
                noise = 0.85 * noise + r.gauss(0, p.mm_noise * 0.5)
                mm_p = _phi(math.log(s_lag / target) / (sig * math.sqrt(tau))) + noise
            bu, _, bd, _ = self._target_levels(mm_p, hs_ticks, extra)
            tgt = {"yes": bu, "no": bd}
            if gap:
                was_gap = True
                ts += p.step_ms
                continue
            if was_gap:
                was_gap, snap_needed = False, True
            if snap_needed:
                lad = {"yes": dict(bu), "no": dict(bd)}
                self._ksnap(ts + r.randint(0, 50), tk, lad)
                snap_needed = False
            else:
                for side in ("yes", "no"):
                    cur, new_t = lad[side], tgt[side]
                    moved = (max(cur) if cur else None) != (max(new_t) if new_t else None)
                    if moved or r.random() < 0.15:
                        new = new_t if moved else {k: (new_t.get(k, v) if r.random() < 0.5 else v) for k, v in cur.items()}
                        t_emit = ts + r.randint(0, 40)
                        for pp in set(cur) | set(new):
                            d = round(new.get(pp, 0.0) - cur.get(pp, 0.0), 2)
                            if abs(d) >= 0.01:
                                self._kdelta(t_emit, tk, side, pp, d)
                        lad[side] = dict(new)
            if ts >= start * 1000:
                for _ in range(self._poisson(p.taker_rate_per_s * p.step_ms / 1000.0)):
                    t_tr = ts + r.randint(50, p.step_ms - 1)
                    size = min(600.0, math.exp(r.gauss(math.log(15), 1.0)))
                    if r.random() < p.informed_frac and abs(true_p - mm_p) > 0.01:
                        consume = "no" if true_p > mm_p else "yes"     # informed buy YES hits NO bids
                    else:
                        consume = r.choice(("yes", "no"))
                    self._ksweep(t_tr, tk, lad, consume, size)
                if r.random() < p.dump_prob_per_step:
                    move = math.log(s_now / path[max(0, sec - 10)])
                    loser = "no" if move > 0 else "yes"
                    if r.random() > 0.7:
                        loser = "yes" if loser == "no" else "no"
                    self._ksweep(ts + r.randint(50, p.step_ms - 1), tk, lad, loser, r.uniform(200, 800))
            ts += p.step_ms
        return m


def generate(path: str, params: Optional[SynthParams] = None) -> List[MarketInfo]:
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(path + suf):
            os.remove(path + suf)
    params = replace(params or SynthParams(), horizons=(900,))
    st = Store(path, flush_every=50000)
    ms = KalshiSynth(params, st).generate()
    st.close()
    return ms
