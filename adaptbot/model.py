"""Fair-value model for "will the asset close above the target?" markets.

The KX*15M markets settle on the average of the reference price over the
last `window` seconds before close, not on a single print.  That changes the
math in two ways:

* With tau >= window left, the average's log-variance is
  sigma^2 * (tau - 2*window/3) instead of sigma^2 * tau.
* Inside the window part of the average is already locked in, so
  uncertainty collapses quickly.

Everything here is pure and deterministic so the same code runs in live,
paper, shadow and backtest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

P_MIN, P_MAX = 0.001, 0.999


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def clamp_p(p: float) -> float:
    return min(P_MAX, max(P_MIN, p))


def logit(p: float) -> float:
    p = clamp_p(p)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def fair_yes(spot: float, strike: float, tau: float, sigma: float,
             window: float, window_avg: float | None = None,
             window_elapsed: float = 0.0) -> float:
    """P(average of price over the final `window` s > strike).

    spot          current reference price
    tau           seconds until close
    sigma         log-volatility per sqrt(second)
    window_avg    mean of the price samples already inside the window
    window_elapsed seconds of the window already observed
    """
    if spot <= 0 or strike <= 0:
        return 0.5
    tau = max(0.0, tau)
    if window <= 0:
        window = 1e-9
    if tau >= window or window_avg is None:
        eff = max(tau - 2.0 * window / 3.0, tau / 3.0, 1e-9)
        sd = sigma * math.sqrt(eff)
        if sd <= 1e-12:
            return clamp_p(1.0 if spot > strike else 0.0)
        return clamp_p(norm_cdf(math.log(spot / strike) / sd))
    # inside the averaging window
    done = min(window, max(window_elapsed, window - tau))
    f_known = done / window
    f_rest = 1.0 - f_known
    mean = f_known * window_avg + f_rest * spot
    sd = f_rest * spot * sigma * math.sqrt(max(tau, 0.0) / 3.0)
    if sd <= 1e-12:
        return clamp_p(1.0 if mean > strike else 0.0)
    return clamp_p(norm_cdf((mean - strike) / sd))


class VolEstimator:
    """Blend of a fast and a slow EWMA of squared log returns (per second)."""

    def __init__(self, fast_hl: float = 300.0, slow_hl: float = 3600.0,
                 floor: float = 3e-5, mult: float = 1.0, fast_weight: float = 0.6,
                 prior: float = 1.0e-4):
        self.fast_hl, self.slow_hl = fast_hl, slow_hl
        self.floor, self.mult, self.fast_weight = floor, mult, fast_weight
        self.var_fast = prior * prior
        self.var_slow = prior * prior
        self.last_ts: float | None = None
        self.last_px: float | None = None
        self.samples = 0

    def update(self, ts: float, price: float) -> None:
        if price <= 0:
            return
        if self.last_ts is None:
            self.last_ts, self.last_px = ts, price
            return
        dt = ts - self.last_ts
        if dt < 0.5:          # sample at most ~2 Hz
            return
        dt = min(dt, 60.0)    # a feed gap should not look like one huge move
        r = math.log(price / self.last_px)
        inst = r * r / dt
        # clip single-tick outliers (bad prints) at 25 sigma of the slow estimate
        inst = min(inst, 25.0 * 25.0 * self.var_slow)
        af = 1.0 - 0.5 ** (dt / self.fast_hl)
        asl = 1.0 - 0.5 ** (dt / self.slow_hl)
        self.var_fast += af * (inst - self.var_fast)
        self.var_slow += asl * (inst - self.var_slow)
        self.last_ts, self.last_px = ts, price
        self.samples += 1

    @property
    def sigma(self) -> float:
        var = self.fast_weight * self.var_fast + (1 - self.fast_weight) * self.var_slow
        return max(self.floor, math.sqrt(max(var, 0.0)) * self.mult)

    def to_dict(self) -> dict:
        return {"var_fast": self.var_fast, "var_slow": self.var_slow}

    def load(self, d: dict) -> None:
        self.var_fast = float(d.get("var_fast", self.var_fast))
        self.var_slow = float(d.get("var_slow", self.var_slow))


@dataclass
class Calibrator:
    """Online Platt scaling: p_cal = sigmoid(a * logit(p) + b).

    Learns from settled markets whether the raw model is over- or
    under-confident.  Starts as the identity and is held near it by an L2
    pull, so a handful of markets cannot wreck it.
    """
    a: float = 1.0
    b: float = 0.0
    lr: float = 0.02
    l2: float = 0.01
    n: int = 0
    enabled: bool = True

    def apply(self, p: float) -> float:
        if not self.enabled:
            return clamp_p(p)
        return clamp_p(sigmoid(self.a * logit(p) + self.b))

    def learn(self, samples: list[float], outcome: int) -> None:
        """samples: raw model probabilities seen during one market."""
        if not samples:
            return
        ga = gb = 0.0
        for p in samples:
            x = logit(p)
            q = sigmoid(self.a * x + self.b)
            err = q - outcome
            ga += err * x
            gb += err
        k = len(samples)
        self.a -= self.lr * (ga / k + self.l2 * (self.a - 1.0))
        self.b -= self.lr * (gb / k + self.l2 * self.b)
        self.a = min(1.5, max(0.4, self.a))
        self.b = min(0.5, max(-0.5, self.b))
        self.n += 1

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "n": self.n}

    def load(self, d: dict) -> None:
        self.a = float(d.get("a", 1.0))
        self.b = float(d.get("b", 0.0))
        self.n = int(d.get("n", 0))


@dataclass
class WindowAverager:
    """Running mean of reference-price samples inside the settlement window."""
    total: float = 0.0
    count: int = 0
    first_ts: float | None = None

    def add(self, ts: float, price: float) -> None:
        if self.first_ts is None:
            self.first_ts = ts
        self.total += price
        self.count += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None
