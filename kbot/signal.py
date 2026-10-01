"""Spot/index tracking and the signal that gates the directional residual.

Kalshi 15-minute crypto markets settle on the CF Benchmarks real-time index (BRTI for BTC,
ETHUSD_RTI for ETH), averaged over the final 60 seconds, against the market's target
(floor_strike). The bot tracks that index directly via Kalshi's `cfbenchmarks_value`
websocket channel when keys are available, and falls back to a Coinbase/Kraken spot feed.

Signal, per market:
  z        = ln(S / K) / (sigma * sqrt(tau))        distance to target in volatility units
  p_model  = Phi(z)                                  model P(YES), no drift
  momentum = % change of S over `lookback_s`
Direction is YES (UP) when z >= min_z AND momentum >= min_momentum_bps; NO (DOWN) for the
mirror; otherwise none. sigma = realized vol of 1-second log returns over `vol_lookback_s`, floored.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import DOWN, UP, MarketInfo


class SpotTracker:
    def __init__(self, keep_s: int = 1800) -> None:
        self.keep_ms = keep_s * 1000
        self.times: Dict[str, List[int]] = {}
        self.prices: Dict[str, List[float]] = {}

    def add(self, asset: str, ts_ms: int, price: float) -> None:
        ts = self.times.setdefault(asset, [])
        ps = self.prices.setdefault(asset, [])
        if ts and ts_ms < ts[-1]:
            ts_ms = ts[-1]
        ts.append(ts_ms)
        ps.append(price)
        if len(ts) > 4096 and ts[0] < ts_ms - self.keep_ms:
            i = bisect.bisect_left(ts, ts_ms - self.keep_ms)
            del ts[:i]
            del ps[:i]

    def last(self, asset: str) -> Optional[Tuple[int, float]]:
        ts = self.times.get(asset)
        return (ts[-1], self.prices[asset][-1]) if ts else None

    def price_at(self, asset: str, ts_ms: int) -> Optional[float]:
        """Last price at or before ts_ms."""
        ts = self.times.get(asset)
        if not ts:
            return None
        i = bisect.bisect_right(ts, ts_ms) - 1
        if i < 0:
            return None
        return self.prices[asset][i]

    def is_stale(self, asset: str, now_ms: int, stale_ms: int) -> bool:
        last = self.last(asset)
        return last is None or now_ms - last[0] > stale_ms


@dataclass
class SignalConfig:
    lookback_s: int = 30              # momentum window
    min_momentum_bps: float = 2.0
    min_z: float = 0.6                # |distance to target| in vol units required to agree
    vol_lookback_s: int = 900
    vol_short_lookback_s: int = 120   # short window for the volatility-regime ratio (short sigma / long sigma)
    sigma_floor_per_sqrt_s: float = 3e-5   # ~17%/yr; guards against quiet-tape blowups
    spot_stale_ms: int = 5000


@dataclass
class SignalReading:
    ok: bool
    direction: Optional[str]
    momentum_bps: float = 0.0
    distance_bps: float = 0.0
    z: float = 0.0
    p_model: Optional[float] = None
    sigma: float = 0.0
    vol_ratio: float = 1.0            # short-window sigma / long-window sigma; >1 = volatility spike
    reason: str = ""

    def p_side(self, outcome: str) -> Optional[float]:
        """Model probability that `outcome` (UP/DOWN) wins, or None when the signal isn't usable."""
        if not self.ok or self.p_model is None:
            return None
        return self.p_model if outcome == UP else 1.0 - self.p_model


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class MomentumSignal:
    def __init__(self, spot: SpotTracker, cfg: SignalConfig) -> None:
        self.spot = spot
        self.cfg = cfg
        self._vol_cache: Dict[str, Tuple[int, float, float]] = {}

    def window_open(self, m: MarketInfo) -> Optional[float]:
        return m.open_price                 # Kalshi: the market's target (floor_strike)

    def _sigma(self, asset: str, now_ms: int, lookback_s: int) -> float:
        ts = self.spot.times.get(asset) or []
        ps = self.spot.prices.get(asset) or []
        i = bisect.bisect_left(ts, now_ms - lookback_s * 1000)
        ss, n = 0.0, 0
        for j in range(i + 1, len(ts)):
            if ps[j] > 0 and ps[j - 1] > 0:
                r = math.log(ps[j] / ps[j - 1])
                ss += r * r
                n += 1
        span = (ts[-1] - ts[i]) / 1000.0 if len(ts) > i + 1 else 0.0
        sig = math.sqrt(ss / span) if span > 30 and n > 10 else 0.0
        return max(sig, self.cfg.sigma_floor_per_sqrt_s)

    def sigma(self, asset: str, now_ms: int) -> float:
        return self.sigmas(asset, now_ms)[0]

    def sigmas(self, asset: str, now_ms: int) -> Tuple[float, float]:
        """(long-window sigma, short/long ratio), cached for 5 s."""
        c = self._vol_cache.get(asset)
        if c and now_ms - c[0] < 5000:
            return c[1], c[2]
        long_s = self._sigma(asset, now_ms, self.cfg.vol_lookback_s)
        short_s = self._sigma(asset, now_ms, self.cfg.vol_short_lookback_s)
        ratio = short_s / long_s if long_s > 0 else 1.0
        self._vol_cache[asset] = (now_ms, long_s, ratio)
        return long_s, ratio

    def read(self, m: MarketInfo, now_ms: int) -> SignalReading:
        if self.spot.is_stale(m.asset, now_ms, self.cfg.spot_stale_ms):
            return SignalReading(ok=False, direction=None, reason="spot_stale")
        k = self.window_open(m)
        if not k:
            return SignalReading(ok=False, direction=None, reason="no_target")
        p_now = self.spot.last(m.asset)[1]
        p_then = self.spot.price_at(m.asset, now_ms - self.cfg.lookback_s * 1000)
        if p_then is None:
            return SignalReading(ok=False, direction=None, reason="warming_up")
        sig, vol_ratio = self.sigmas(m.asset, now_ms)
        tau = max(1.0, (m.end_ms - now_ms) / 1000.0)
        z = math.log(p_now / k) / (sig * math.sqrt(tau))
        mom = (p_now / p_then - 1.0) * 1e4
        dist = (p_now / k - 1.0) * 1e4
        direction = None
        if z >= self.cfg.min_z and mom >= self.cfg.min_momentum_bps:
            direction = UP
        elif z <= -self.cfg.min_z and mom <= -self.cfg.min_momentum_bps:
            direction = DOWN
        return SignalReading(ok=True, direction=direction, momentum_bps=mom, distance_bps=dist, z=z,
                             p_model=_phi(z), sigma=sig, vol_ratio=vol_ratio)
