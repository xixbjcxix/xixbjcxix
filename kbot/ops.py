"""Operations helpers: fee audit, alert notifier, PnL history and the crash-restart supervisor.

* FeeAudit      compares the fee Kalshi actually charged on each fill with what our fee model predicts.
                A silent mismatch means the pair-cost maths (and every backtest) is wrong, so it alerts.
* Notifier      posts short messages to a Slack/Discord-style webhook and/or a Telegram chat. Secrets
                come from .env only. Best effort: a failed alert never touches trading.
* PnlHistory    samples PnL for the dashboard's equity curve.
* supervise()   restarts the engine after an unexpected crash (never after a kill switch, a failed
                preflight or Ctrl-C).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("kbot.ops")


# ---------------------------------------------------------------------------------------------
@dataclass
class AlertsConfig:
    enabled: bool = True                      # alerts only fire if a webhook / Telegram secret is set in .env
    webhook_url_env: str = "KBOT_ALERT_WEBHOOK"          # Slack / Discord / generic webhook URL (in .env)
    telegram_token_env: str = "KBOT_TELEGRAM_TOKEN"
    telegram_chat_env: str = "KBOT_TELEGRAM_CHAT"
    min_interval_s: float = 300.0             # same alert kind at most once per this many seconds
    big_loss_usd: float = 10.0                # alert when one market settles at or below -this (0 = off)
    daily_summary: bool = True                # one summary message at the UTC day rollover
    fee_drift_tolerance: float = 0.25         # alert when charged fees exceed modelled fees by this fraction
    fee_drift_min_fills: int = 20
    fee_drift_halt: bool = False              # also trip the kill switch on fee drift (strict mode)


class FeeAudit:
    """Modelled vs charged fees on real exchange fills (demo / live only: simulated fills are the model)."""

    def __init__(self, tolerance: float = 0.25, min_fills: int = 20, abs_tol: float = 0.02) -> None:
        self.tolerance = tolerance
        self.min_fills = min_fills
        self.abs_tol = abs_tol
        self.n = 0
        self.reported = 0.0
        self.modeled = 0.0
        self.worst_over = 0.0            # largest single-fill (charged - modelled)
        self.by_liq: Dict[str, List[float]] = {"maker": [0.0, 0.0], "taker": [0.0, 0.0]}  # [reported, modeled]
        self.flagged = False

    def record(self, fee_model, liquidity: str, size: float, price: float, reported: float) -> Optional[str]:
        """Add one fill. Returns a message the first time drift is detected, else None."""
        if not getattr(fee_model, "supported", True):
            return None
        mod = fee_model.taker_fee(size, price) if liquidity == "taker" else fee_model.maker_fee(size, price)
        self.n += 1
        self.reported += reported
        self.modeled += mod
        self.worst_over = max(self.worst_over, reported - mod)
        b = self.by_liq.setdefault(liquidity, [0.0, 0.0])
        b[0] += reported
        b[1] += mod
        if self.flagged:
            return None
        msg = None
        if reported - mod > self.abs_tol + 1e-9:
            msg = (f"a {liquidity} fill of {size:g} @ {price:.2f} was charged ${reported:.4f}; "
                   f"the model expects ${mod:.4f}")
        elif self.n >= self.min_fills and self.modeled > 0 and self.drift > self.tolerance:
            msg = (f"over {self.n} fills Kalshi charged ${self.reported:.2f} vs ${self.modeled:.2f} modelled "
                   f"({self.drift:+.0%})")
        if msg:
            self.flagged = True
        return msg

    @property
    def drift(self) -> float:
        return (self.reported - self.modeled) / self.modeled if self.modeled > 0 else 0.0

    def snapshot(self) -> dict:
        return {"fills": self.n, "charged": round(self.reported, 4), "modeled": round(self.modeled, 4),
                "drift_pct": round(self.drift * 100, 1), "worst_over": round(self.worst_over, 4),
                "flagged": self.flagged}


# ---------------------------------------------------------------------------------------------
class Notifier:
    """Fire-and-forget alert sender with per-kind rate limiting."""

    def __init__(self, cfg: AlertsConfig, name: str = "kbot") -> None:
        self.cfg = cfg
        self.name = name
        self.webhook = os.environ.get(cfg.webhook_url_env, "").strip()
        self.tg_token = os.environ.get(cfg.telegram_token_env, "").strip()
        self.tg_chat = os.environ.get(cfg.telegram_chat_env, "").strip()
        self._last: Dict[str, float] = {}
        self.sent: List[Tuple[int, str, str]] = []      # (ts_ms, kind, text), last 50 - shown on the dashboard
        self._tasks: set = set()

    @property
    def configured(self) -> bool:
        return bool(self.webhook or (self.tg_token and self.tg_chat))

    def send(self, kind: str, text: str, force: bool = False) -> bool:
        """Queue an alert. Returns False if it was dropped (disabled / rate limited)."""
        now = time.time()
        if not self.cfg.enabled:
            return False
        if not force and now - self._last.get(kind, 0.0) < self.cfg.min_interval_s:
            return False
        self._last[kind] = now
        self.sent.append((int(now * 1000), kind, text))
        del self.sent[:-50]
        log.warning("ALERT [%s] %s", kind, text)
        if not self.configured:
            return True
        try:
            t = asyncio.get_running_loop().create_task(self._post(f"[{self.name}] {text}"))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except RuntimeError:        # no running loop (tests, sync callers)
            pass
        return True

    async def _post(self, text: str) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                if self.webhook:
                    # "text" is Slack's field, "content" is Discord's; each ignores the other
                    await c.post(self.webhook, json={"text": text, "content": text[:1900]})
                if self.tg_token and self.tg_chat:
                    await c.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                 json={"chat_id": self.tg_chat, "text": text})
        except Exception as e:  # noqa: BLE001
            log.warning("alert delivery failed: %s: %s", type(e).__name__, e)


# ---------------------------------------------------------------------------------------------
class PnlHistory:
    """Downsampled (ts_ms, pnl_today) series for the dashboard's equity curve."""

    def __init__(self, every_ms: int = 10_000, maxlen: int = 2000) -> None:
        self.every_ms = every_ms
        self.points: Deque[Tuple[int, float]] = deque(maxlen=maxlen)

    def add(self, ts_ms: int, pnl: float) -> None:
        if not self.points or ts_ms - self.points[-1][0] >= self.every_ms:
            self.points.append((ts_ms, round(pnl, 4)))

    def as_list(self) -> list:
        return [list(p) for p in self.points]


# ---------------------------------------------------------------------------------------------
async def supervise(run_once: Callable[[int], Awaitable[object]], max_restarts: int = 5, window_s: float = 3600.0,
                    backoff_s: float = 5.0, notify: Optional[Callable[[str, str], object]] = None,
                    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> int:
    """Run `run_once(attempt)` until it ends normally. After an unexpected exception, wait (exponential
    backoff) and run again, at most `max_restarts` times within `window_s`.

    `run_once` must return normally for a clean stop (Ctrl-C, --hours deadline, kill switch) and raise
    for a crash. SystemExit (bad keys, failed setup) is never retried. Returns the number of restarts."""
    crashes: Deque[float] = deque()
    restarts = 0
    attempt = 0
    while True:
        try:
            await run_once(attempt)
            return restarts
        except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001
            now = time.time()
            crashes.append(now)
            while crashes and now - crashes[0] > window_s:
                crashes.popleft()
            msg = f"engine crashed: {type(e).__name__}: {e}"
            log.error("%s", msg, exc_info=True)
            if len(crashes) > max_restarts:
                if notify:
                    notify("supervisor_giving_up", f"{msg} - {len(crashes)} crashes in {window_s / 60:.0f} min, "
                                                   f"not restarting. Orders were cancelled on exit; check the logs.")
                raise
            delay = min(backoff_s * 2 ** (len(crashes) - 1), 120.0)
            if notify:
                notify("engine_restart", f"{msg} - restarting in {delay:.0f}s "
                                         f"(restart {len(crashes)}/{max_restarts})")
            restarts += 1
            attempt += 1
            await sleep(delay)
