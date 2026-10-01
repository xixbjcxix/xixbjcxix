"""Configuration: pbot.yaml for settings, .env for secrets (never in YAML, never in code)."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml


@dataclass
class AccountConfig:
    base_url: str = "https://api.public.com"
    secret_env: str = "PUBLIC_API_SECRET"        # env var NAME; the value lives in .env
    account_id_env: str = "PUBLIC_ACCOUNT_ID"
    token_validity_minutes: int = 60


@dataclass
class SessionConfig:
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    opening_range_minutes: int = 5              # build the range from the first N one-minute bars
    entry_cutoff: str = "10:30"                 # no new entries after this
    flatten_at: str = "11:00"                   # everything is closed by this time - no overnight holds
    preflight_minutes: int = 5                  # wake up this long before the open
    poll_seconds: float = 2.0                   # quote polling interval while the window is open
    holidays: List[str] = field(default_factory=lambda: [
        # NYSE full-day closures. Add next year's here (or in pbot.yaml) when they are published.
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
        "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
        "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"])
    early_closes: Dict[str, str] = field(default_factory=lambda: {
        "2026-11-27": "13:00", "2026-12-24": "13:00", "2027-11-26": "13:00"})


@dataclass
class StrategyConfig:
    breakout_buffer_pct: float = 0.05            # price must clear the range high by this % (0.05 = 0.05%)
    min_range_pct: float = 0.25                  # skip if the opening range is narrower than this % of price
    max_range_pct: float = 3.0                   # ...or wider (too volatile for a small stop)
    require_above_vwap: bool = True
    stop_mode: str = "mid"                       # mid = range midpoint (tighter) | low = range low
    target_r: float = 2.0                        # take profit at N x the risk per share
    breakeven_at_r: float = 1.0                  # once up N x risk, move the stop to the entry price
    max_chase_range_frac: float = 0.5            # don't buy more than 0.5 x range width above the range high
    max_spread_pct: float = 0.15                 # skip entries while bid/ask spread is wider than this %
    min_range_volume: float = 50000              # shares traded during the opening range (liquidity filter)
    min_price: float = 2.0
    max_price: float = 500.0


@dataclass
class RiskConfig:
    risk_per_trade_usd: float = 5.0              # loss if the stop is hit (before slippage)
    max_position_usd: float = 150.0              # notional cap per position
    max_open_positions: int = 2
    max_trades_per_day: int = 4
    max_daily_loss_usd: float = 15.0             # realized + open loss; hit it -> flatten and stop for the day
    max_buying_power_frac: float = 0.5           # never commit more than this share of buying power
    use_cash_only_buying_power: bool = True      # size from cash, not margin
    # FINRA's $25k pattern-day-trader rule was repealed (SEC approval Apr 2026, effective Jun 4 2026),
    # but brokers may phase the new intraday-margin rules in over months. Keep this on until Public
    # confirms your account is no longer subject to PDT limits, then set it to false.
    pdt_guard: bool = True                       # block a 4th day trade in 5 business days under $25k equity
    pdt_equity_threshold: float = 25000.0
    pdt_max_day_trades: int = 3


@dataclass
class ExecutionConfig:
    entry_slippage_pct: float = 0.05             # entry limit = ask + this %  (marketable limit, not market)
    exit_slippage_pct: float = 0.10              # exit limit = bid - this %, then market if unfilled
    order_timeout_s: float = 8.0
    broker_stop: bool = True                     # also rest a STOP order at Public in case the bot dies
    broker_stop_extra_r: float = 0.5             # ...placed this many R below the software stop
    fractional: bool = False                     # whole shares only by default


@dataclass
class PaperConfig:
    starting_cash: float = 1000.0


@dataclass
class LadderConfig:
    enabled: bool = True
    # Each level overrides these risk.* keys. Everyone starts at level 0; see pbot/ladder.py.
    levels: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"name": "micro", "risk_per_trade_usd": 2, "max_position_usd": 60, "max_open_positions": 1,
         "max_trades_per_day": 2, "max_daily_loss_usd": 6},
        {"name": "small", "risk_per_trade_usd": 5, "max_position_usd": 150, "max_open_positions": 2,
         "max_trades_per_day": 4, "max_daily_loss_usd": 15},
        {"name": "medium", "risk_per_trade_usd": 10, "max_position_usd": 300, "max_open_positions": 2,
         "max_trades_per_day": 4, "max_daily_loss_usd": 30},
        {"name": "standard", "risk_per_trade_usd": 20, "max_position_usd": 600, "max_open_positions": 3,
         "max_trades_per_day": 5, "max_daily_loss_usd": 60},
    ])
    promote_min_trades: int = 20
    promote_min_days: int = 10
    promote_min_profit_factor: float = 1.2       # gross wins / gross losses
    demote_drawdown_r: float = 6.0               # drop a level after losing 6 x risk-per-trade from the peak


@dataclass
class LiveConfig:
    enabled: bool = False                        # second gate besides the typed confirmation


@dataclass
class BotConfig:
    watchlist: List[str] = field(default_factory=lambda: [
        "SOFI", "PLTR", "AMD", "F", "INTC", "HOOD", "RIVN", "NIO", "AAL", "MARA"])
    account: AccountConfig = field(default_factory=AccountConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    ladder: LadderConfig = field(default_factory=LadderConfig)
    data_dir: str = "data"
    alert_webhook_env: str = "PBOT_ALERT_WEBHOOK"


def _merge(obj: Any, data: Dict[str, Any], path: str = "") -> Any:
    for k, v in (data or {}).items():
        if not hasattr(obj, k):
            raise ValueError(f"unknown config key: {path}{k}")
        cur = getattr(obj, k)
        if dataclasses.is_dataclass(cur) and isinstance(v, dict):
            _merge(cur, v, f"{path}{k}.")
        else:
            setattr(obj, k, v)
    return obj


def load_config(path: str = "pbot.yaml") -> BotConfig:
    cfg = BotConfig()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            _merge(cfg, yaml.safe_load(f) or {})
    cfg.watchlist = [s.strip().upper() for s in cfg.watchlist if s and s.strip()]
    if not cfg.ladder.levels:
        raise ValueError("ladder.levels must have at least one level")
    for lv in cfg.ladder.levels:
        for k in lv:
            if k != "name" and not hasattr(cfg.risk, k):
                raise ValueError(f"ladder level {lv.get('name')}: unknown risk key {k}")
        if "risk_per_trade_usd" not in lv:
            raise ValueError(f"ladder level {lv.get('name')}: risk_per_trade_usd is required")
    return cfg


def with_overrides(cfg: BotConfig, **dotted: Any) -> BotConfig:
    """Copy of cfg with {'risk.max_open_positions': 3}-style overrides."""
    new = dataclasses.replace(cfg)
    for f in dataclasses.fields(new):
        v = getattr(new, f.name)
        if dataclasses.is_dataclass(v):
            setattr(new, f.name, dataclasses.replace(v))
    for key, val in dotted.items():
        obj = new
        parts = key.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        if not hasattr(obj, parts[-1]):
            raise ValueError(f"unknown config key: {key}")
        setattr(obj, parts[-1], val)
    return new
