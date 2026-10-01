"""Configuration: YAML file for settings, .env for secrets (never in YAML, never in code)."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .fillmodel import FillModelConfig
from .risk import RiskConfig
from .signal import SignalConfig
from .strategy import ResidualConfig, StrategyConfig


@dataclass
class FeesConfig:
    profile: str = "kalshi"                   # kalshi | kalshi_no_maker_fee | zero
    precision: float = 0.01                   # fees rounded UP per fill to this ($0.0001 for direct members)
    default_fee_type: str = "quadratic_with_maker_fees"   # assumed until GET /series says otherwise
    use_market_schedule: bool = True          # use each series' fee_type and fee_multiplier
    rebate_capture: float = 1.0               # unused on Kalshi (no maker rebates); kept for engine compatibility


@dataclass
class KalshiConfig:
    # asset key -> Kalshi series ticker. Add a row here (and to spot.index_ids below) for any
    # 15-minute crypto series Kalshi lists, then add the key to markets.assets to trade it.
    # SOL launched alongside BTC/ETH in 2026; XRP/DOGE/etc. are not live 15m series yet as of
    # writing, so trading them here will just find no open markets - check `python -m kbot check`.
    series: Dict[str, str] = field(default_factory=lambda: {
        "btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M"})
    prod_rest: str = "https://external-api.kalshi.com/trade-api/v2"
    prod_ws: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    demo_rest: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    demo_ws: str = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    # env var NAMES (values live in .env, written by `python -m kbot setup`)
    demo_key_id_env: str = "KALSHI_DEMO_KEY_ID"
    demo_key_path_env: str = "KALSHI_DEMO_KEY_PATH"
    prod_key_id_env: str = "KALSHI_PROD_KEY_ID"
    prod_key_path_env: str = "KALSHI_PROD_KEY_PATH"
    write_tokens_per_s: float = 100.0         # Basic tier; `python -m kbot check` prints yours
    read_tokens_per_s: float = 200.0
    order_cost_tokens: float = 10.0
    cancel_cost_tokens: float = 2.0
    poll_book_ms: int = 1000                  # REST fallback when no websocket keys for the data env
    lookahead_windows: int = 1                # also watch the next window(s) before they open
    discovery_interval_s: int = 15
    max_position_contracts: float = 25000.0   # Kalshi per-market position limit guard (check your account)


@dataclass
class MarketsConfig:
    assets: List[str] = field(default_factory=lambda: ["btc", "eth"])
    data_env: str = "prod"                    # where market data comes from for record / sim paper: prod | demo


@dataclass
class SpotConfig:
    # kalshi_index = CF Benchmarks index via Kalshi's websocket (the settlement source; needs keys)
    source: str = "kalshi_index"              # kalshi_index | coinbase | kraken
    fallback: str = "coinbase"                # used when kalshi_index is unavailable
    index_ids: Dict[str, str] = field(default_factory=lambda: {
        "btc": "BRTI", "eth": "ETHUSD_RTI", "sol": "SOLUSD_RTI"})
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    kraken_ws_url: str = "wss://ws.kraken.com/v2"


@dataclass
class EngineConfig:
    min_tick_ms: int = 250                    # min time between strategy evaluations per market
    timer_ms: int = 250
    requote_up_ticks: int = 2                 # raise a resting bid only if the new price is >= this many ticks better
    requote_stale_ms: int = 5000              # ...or the resting order is older than this
    reject_backoff_ms: int = 2000             # after a risk reject, wait before retrying that quote


@dataclass
class BacktestConfig:
    settle_delay_ms: int = 5000
    # if the official winner is unknown, infer it from the spot feed (close >= open -> UP).
    # Official settlement is the 60 s CF Benchmarks average vs floor_strike, so this is approximate.
    infer_winner_from_spot: bool = True


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True                 # open the dashboard in your browser on start


@dataclass
class LiveConfig:
    enabled: bool = True                      # set false to disable `live` entirely
    use_starter_limits: bool = True           # first live runs: override risk/size with the small limits below
    smoke_test: bool = True                   # place + cancel one 1-cent test order before trading
    clip_shares: float = 5.0
    max_order_usd: float = 5.0
    max_market_usd: float = 20.0
    max_residual_usd: float = 5.0
    max_residual_shares: float = 10.0
    max_total_usd: float = 60.0
    max_daily_loss_usd: float = 15.0


@dataclass
class BotConfig:
    db_path: str = "data/kbot.db"
    recording_db: str = "data/recording.db"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    fill_model: FillModelConfig = field(default_factory=FillModelConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    spot: SpotConfig = field(default_factory=SpotConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    store_tob: bool = True
    store_our_quotes: bool = True
    record_raw_in_paper: bool = True


def _build(cls, data: Optional[Dict[str, Any]]):
    """Recursively build a dataclass from a dict, rejecting unknown keys (typos matter here)."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"expected mapping for {cls.__name__}, got {type(data).__name__}")
    kwargs = {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for k, v in data.items():
        if k not in fields:
            raise ValueError(f"unknown config key '{k}' in {cls.__name__}")
        f = fields[k]
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default  # type: ignore
        if dataclasses.is_dataclass(default):
            kwargs[k] = _build(type(default), v)
        elif k in ("cutoff_s",) and isinstance(v, dict):
            kwargs[k] = {int(kk): float(vv) for kk, vv in v.items()}
        else:
            kwargs[k] = v
    return cls(**kwargs)


SETTINGS_OVERLAY = "data/settings.yaml"     # written by the dashboard settings panel


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None, overlay: Optional[str] = SETTINGS_OVERLAY) -> BotConfig:
    """config.yaml, then the dashboard's settings overlay on top (if present)."""
    data: Dict[str, Any] = {}
    if path:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh) or {}
    if overlay and os.path.exists(overlay):
        with open(overlay, "r") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    return _build(BotConfig, data)


def to_dict(cfg: BotConfig) -> dict:
    return dataclasses.asdict(cfg)


def with_overrides(cfg: BotConfig, **dotted: Any) -> BotConfig:
    """Copy with dotted overrides, e.g. with_overrides(cfg, **{'strategy.residual.enabled': False})."""
    d = to_dict(cfg)
    for key, val in dotted.items():
        cur = d
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        if parts[-1] not in cur:
            raise KeyError(key)
        cur[parts[-1]] = val
    return _build(BotConfig, d)


def load_env(env_path: str = ".env") -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:   # minimal fallback parser
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials(cfg: BotConfig, env: str) -> Optional[Dict[str, str]]:
    """(key_id, key_path) for 'demo' or 'prod' from the environment / .env, or None."""
    load_env()
    k = cfg.kalshi
    kid = os.environ.get(k.demo_key_id_env if env == "demo" else k.prod_key_id_env, "").strip()
    kpath = os.environ.get(k.demo_key_path_env if env == "demo" else k.prod_key_path_env, "").strip()
    if kid and kpath:
        return {"key_id": kid, "key_path": os.path.expanduser(kpath)}
    return None
