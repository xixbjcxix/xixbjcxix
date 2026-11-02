"""Settings: defaults here, overrides in data/settings.yaml."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields

import yaml

from .strategy import DEFAULT_PROFILES, Profile

DATA_DIR = os.environ.get("ADAPTBOT_DATA", "data")

SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M"}

BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


@dataclass
class Settings:
    assets: list = field(default_factory=lambda: ["btc"])
    kalshi_env: str = "prod"
    kalshi_base_url: str = ""          # override BASE_URLS if set
    poll_interval_s: float = 1.0

    # --- money / risk (hard limits, checked on every order) ---
    bankroll: float = 40.0             # sizing base; live uses min(this, cash)
    max_contracts_per_order: int = 10
    max_dollars_per_market: float = 12.0
    max_dollars_total: float = 30.0
    max_daily_loss: float = 8.0        # measured from Kalshi cash + positions
    max_consecutive_losses: int = 4    # live losing markets in a row ...
    cooldown_markets: int = 2          # ... then skip this many markets
    flatten_on_halt: bool = True
    reconcile_interval_s: float = 5.0
    stale_spot_s: float = 5.0
    stale_book_s: float = 4.0
    min_spot_sources: int = 2

    # --- execution ---
    max_slippage: float = 0.01
    time_in_force: str = "immediate_or_cancel"
    taker_fee_rate: float = 0.07       # x the series fee_multiplier, read from Kalshi at start
    crypto_shard: int = -1             # -1 = read from the markets (crypto is on shard 2)

    # --- model ---
    settle_window_s: float = 60.0
    vol_fast_halflife_s: float = 300.0
    vol_slow_halflife_s: float = 3600.0
    vol_floor: float = 3e-5
    vol_mult: float = 1.0
    calibration: bool = True

    # --- sizing ---
    kelly_fraction: float = 0.25

    # --- adaptive ---
    adaptive: bool = True
    default_profile: str = "early_cut"
    min_markets_before_trusting: int = 12
    score_halflife_markets: float = 30.0
    score_k: float = 0.5
    switch_margin: float = 0.02
    sit_out_when_no_edge: bool = True
    min_profile_trades: int = 8        # traded markets before a profile can be chosen
    warmup_contracts: int = 1          # live size while the shadow record is too short
    shadow_slippage: float = 0.01
    shadow_fill_ratio: float = 0.7

    # --- paper ---
    paper_start_cash: float = 40.0
    paper_slippage: float = 0.01
    paper_fill_ratio: float = 0.7

    dashboard_port: int = 8787
    record: bool = True
    profiles: list = field(default_factory=lambda: [p.to_dict() for p in DEFAULT_PROFILES])

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self.kalshi_base_url or BASE_URLS[self.kalshi_env]

    @property
    def series(self) -> list[str]:
        return [SERIES[a.strip().lower()] for a in self.assets if a.strip().lower() in SERIES]

    def profile_objs(self) -> list[Profile]:
        return [Profile.from_dict(p) for p in self.profiles]

    def validate(self) -> list[str]:
        errs = []
        if not self.series:
            errs.append("no valid assets (use btc, eth, sol)")
        if self.max_dollars_per_market > self.max_dollars_total:
            errs.append("max_dollars_per_market > max_dollars_total")
        if not (0 < self.kelly_fraction <= 1):
            errs.append("kelly_fraction must be in (0, 1]")
        if self.max_daily_loss <= 0:
            errs.append("max_daily_loss must be > 0")
        names = [p.get("name") for p in self.profiles]
        if len(set(names)) != len(names):
            errs.append("profile names must be unique")
        if self.default_profile not in names:
            errs.append(f"default_profile {self.default_profile!r} is not a profile")
        return errs


def settings_path() -> str:
    return os.path.join(DATA_DIR, "settings.yaml")


def load_settings(path: str | None = None) -> Settings:
    path = path or settings_path()
    s = Settings()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        known = {f.name for f in fields(Settings)}
        unknown = sorted(set(raw) - known)
        if unknown:
            print(f"warning: unknown settings ignored: {', '.join(unknown)}")
        for k, v in raw.items():
            if k in known:
                setattr(s, k, v)
    return s


def save_settings(s: Settings, path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(s), f, sort_keys=False)
