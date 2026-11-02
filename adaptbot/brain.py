"""Model + learning state shared by the live engine and the backtester."""
from __future__ import annotations

import json
import logging
import os

from .adaptive import Selector, ShadowBook
from .config import DATA_DIR, Settings
from .market import Book, MarketMeta, View
from .model import Calibrator, VolEstimator, WindowAverager, fair_yes
from .strategy import Sizing

log = logging.getLogger("adaptbot.brain")

CAL_SAMPLE_EVERY_S = 30.0


class Brain:
    def __init__(self, s: Settings, state_file: str | None = None):
        self.s = s
        self.state_file = state_file if state_file is not None else os.path.join(DATA_DIR, "state.json")
        self.vol: dict[str, VolEstimator] = {}
        self.cal = Calibrator(enabled=s.calibration)
        self.shadow = ShadowBook(s.profile_objs(), s.taker_fee_rate, s.shadow_slippage,
                                 s.shadow_fill_ratio, s.score_halflife_markets)
        self.selector = Selector(default=s.default_profile, min_markets=s.min_markets_before_trusting,
                                 k=s.score_k, switch_margin=s.switch_margin,
                                 sit_out=s.sit_out_when_no_edge, min_trades=s.min_profile_trades)
        self.windows: dict[str, WindowAverager] = {}
        self.win_last: dict[str, float] = {}
        self.cal_samples: dict[str, list[float]] = {}
        self.cal_last: dict[str, float] = {}

    # ---------------------------------------------------------------- inputs
    def vol_for(self, asset: str) -> VolEstimator:
        if asset not in self.vol:
            s = self.s
            self.vol[asset] = VolEstimator(s.vol_fast_halflife_s, s.vol_slow_halflife_s, s.vol_floor, s.vol_mult)
        return self.vol[asset]

    def on_spot(self, asset: str, ts: float, price: float) -> None:
        self.vol_for(asset).update(ts, price)

    def view(self, ts: float, meta: MarketMeta, book: Book, spot: float | None, spot_ts: float) -> View:
        sigma = self.vol_for(meta.asset).sigma
        p_raw = p_cal = None
        if spot is not None and meta.strike:
            tau = meta.close_ts - ts
            w = self.s.settle_window_s
            win = None
            if 0 < tau <= w:
                win = self.windows.setdefault(meta.ticker, WindowAverager())
                if ts - self.win_last.get(meta.ticker, 0.0) >= 0.999:
                    win.add(ts, spot)
                    self.win_last[meta.ticker] = ts
            p = fair_yes(spot, meta.strike, tau, sigma, w,
                         window_avg=win.mean if win else None,
                         window_elapsed=(w - tau) if win else 0.0)
            p_raw = 1.0 - p if meta.strike_type == "less" else p
            p_cal = self.cal.apply(p_raw)
            elapsed = ts - meta.open_ts
            if elapsed >= 30 and tau >= 30 and ts - self.cal_last.get(meta.ticker, 0.0) >= CAL_SAMPLE_EVERY_S:
                self.cal_samples.setdefault(meta.ticker, []).append(p_raw)
                self.cal_last[meta.ticker] = ts
        return View(ts=ts, meta=meta, book=book, spot=spot, spot_ts=spot_ts,
                    sigma=sigma, p_raw=p_raw, p_yes=p_cal)

    def shadow_sizing(self) -> Sizing:
        s = self.s
        return Sizing(bankroll=s.bankroll, kelly_fraction=s.kelly_fraction,
                      max_contracts=s.max_contracts_per_order,
                      max_dollars_market=s.max_dollars_per_market,
                      fee_rate=s.taker_fee_rate, max_slippage=s.max_slippage)

    # ---------------------------------------------------------------- outcomes
    def settle(self, ticker: str, result: str) -> dict[str, float]:
        out = self.shadow.settle(ticker, result)
        samples = self.cal_samples.pop(ticker, [])
        if self.s.calibration and samples and result in ("yes", "no"):
            self.cal.learn(samples, 1 if result == "yes" else 0)
        self.forget(ticker)
        return out

    def forget(self, ticker: str) -> None:
        self.windows.pop(ticker, None)
        self.win_last.pop(ticker, None)
        self.cal_samples.pop(ticker, None)
        self.cal_last.pop(ticker, None)

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        if not self.state_file:
            return
        d = {
            "calibrator": self.cal.to_dict(),
            "shadow": self.shadow.to_dict(),
            "selector": self.selector.to_dict(),
            "vol": {a: v.to_dict() for a, v in self.vol.items()},
        }
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.state_file)

    def load(self) -> bool:
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("could not read %s: %r - starting fresh", self.state_file, e)
            return False
        self.cal.load(d.get("calibrator", {}))
        self.shadow.load(d.get("shadow", {}))
        self.selector.load(d.get("selector", {}))
        for a, vd in d.get("vol", {}).items():
            self.vol_for(a).load(vd)
        return True
