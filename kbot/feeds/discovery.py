"""Market discovery for Kalshi 15-minute crypto series (KXBTC15M, KXETH15M by default).

GET /series/{series}              -> fee_type, fee_multiplier (drives the fee model)
GET /markets?series_ticker=..&status=open|unopened -> current and next windows
Market fields used: ticker, event_ticker, open_time, close_time, floor_strike (the target /
"price to beat"), strike_type, price_ranges (tick), status, result.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from ..book import no_token, yes_token
from ..models import DOWN, UP, MarketInfo

log = logging.getLogger("kbot.discovery")


def iso_ms(s) -> int:
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s * 1000) if s < 1e12 else int(s)
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)


def tick_from(m: dict) -> float:
    pr = m.get("price_ranges") or []
    steps = []
    for r in pr:
        for k in ("step", "tick_size", "step_dollars"):
            if r.get(k) is not None:
                try:
                    steps.append(float(r[k]))
                except (TypeError, ValueError):
                    pass
    # Markets can have finer ticks in the tails (e.g. $0.001 below 10c / above 90c) than in the
    # middle. The COARSEST step is valid at every price (0.01-multiples are also 0.001-multiples),
    # so the bot quotes on that grid everywhere and never sends an invalid price.
    return max(steps) if steps else float(m.get("tick_size_dollars") or 0.01)


def winner_of(m: dict) -> Optional[str]:
    res = str(m.get("result") or "").lower()
    return UP if res == "yes" else DOWN if res == "no" else None


def parse_market(m: dict, asset: str, series: dict) -> MarketInfo:
    t = m["ticker"]
    start, end = iso_ms(m.get("open_time")), iso_ms(m.get("close_time"))
    strike = m.get("floor_strike")
    return MarketInfo(
        slug=t, asset=asset, horizon_s=max(60, round((end - start) / 1000)) if end and start else 900,
        start_ms=start, end_ms=end, token_up=yes_token(t), token_down=no_token(t),
        condition_id=str(m.get("event_ticker", "")), tick_size=tick_from(m), min_order_size=1.0,
        winner=winner_of(m), open_price=float(strike) if strike not in (None, "") else None,
        series=str(series.get("ticker", "")),
        fee_type=str(series.get("fee_type") or "quadratic_with_maker_fees"),
        fee_multiplier=float(series.get("fee_multiplier") or 1.0),
        strike_type=str(m.get("strike_type") or "greater_or_equal"),
        exchange_index=int(m["exchange_index"]) if m.get("exchange_index") not in (None, "") else None,
    )


class KalshiDiscovery:
    def __init__(self, rest, series_by_asset: Dict[str, str], assets: List[str], lookahead: int = 1) -> None:
        self.rest = rest
        self.series_by_asset = {a: series_by_asset[a] for a in assets}
        self.lookahead = lookahead
        self.series_meta: Dict[str, dict] = {}
        self.cache: Dict[str, MarketInfo] = {}

    async def series(self, st: str) -> dict:
        if st not in self.series_meta:
            try:
                self.series_meta[st] = await self.rest.get_series(st)
            except Exception as e:  # noqa: BLE001
                log.warning("series %s lookup failed (%s); assuming maker fees apply", st, e)
                return {"ticker": st}
        return self.series_meta[st]

    async def discover(self) -> List[MarketInfo]:
        out: List[MarketInfo] = []
        for asset, st in self.series_by_asset.items():
            meta = await self.series(st)
            raw = []
            for status in ("open", "unopened"):
                try:
                    raw += await self.rest.get_markets(series_ticker=st, status=status, limit=50)
                except Exception as e:  # noqa: BLE001
                    log.warning("markets %s/%s failed: %s", st, status, e)
            # ignore stale listings (e.g. old demo markets still marked open) and far-future ones
            import time as _t
            now = int(_t.time() * 1000)
            raw = [m for m in raw if now < iso_ms(m.get("close_time")) <= now + (2 + self.lookahead) * 900_000]
            raw.sort(key=lambda m: iso_ms(m.get("close_time")))
            for m in raw[: 1 + self.lookahead]:
                info = parse_market(m, asset, meta)
                old = self.cache.get(info.slug)
                if old is not None:
                    if old.open_price is None and info.open_price is not None:
                        old.open_price = info.open_price      # target published at open
                    info = old
                else:
                    self.cache[info.slug] = info
                out.append(info)
        return out

    async def resolution(self, m: MarketInfo) -> Optional[str]:
        try:
            return winner_of(await self.rest.get_market(m.slug))
        except Exception as e:  # noqa: BLE001
            log.warning("result lookup failed for %s: %s", m.slug, e)
            return None
