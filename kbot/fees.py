"""Kalshi fee model (checked 2026-09-30 against kalshi.com/docs/kalshi-fee-schedule.pdf,
effective 2026-07-07, and docs.kalshi.com).

  taker fee = round_up( M x 0.07   x C x P x (1 - P) )
  maker fee = round_up( M x 0.0175 x C x P x (1 - P) )   only on series whose fee_type has maker fees
  M = the series fee_multiplier; P = price in dollars; C = contracts.

Which fee applies is read per series from GET /series/{series_ticker}:
  fee_type  quadratic                        -> taker fees only
            quadratic_with_maker_fees        -> maker fee = 0.25 x taker coefficient (0.0175)
            quadratic_with_combo_maker_fees  -> maker fee = 0.50 x taker coefficient
            flat                             -> product-specific table; not supported, bot refuses to trade
  fee_multiplier -> M
The July 2026 schedule says most markets now charge maker fees, so the default ASSUMES maker
fees until the series says otherwise.

Rounding: Kalshi rounds so that fee + position cost lands on its balance precision (cent for
FCM-cleared members, centicent for direct members) and rebates any over-rounding later
through an accumulator. We model the conservative case: each fill's fee rounded UP to
`fee_precision` (default $0.01). Set 0.0001 if your account is a direct member.
Fees are always a COST on Kalshi; there is no maker rebate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional

from .models import MarketInfo

TAKER_COEF = 0.07
MAKER_SHARE = {"quadratic": 0.0, "quadratic_with_maker_fees": 0.25, "quadratic_with_combo_maker_fees": 0.5}


def ceil_to(x: float, step: float) -> float:
    if x <= 0:
        return 0.0
    n = math.ceil(round(x / step, 9))
    return round(n * step, 10)


@dataclass(frozen=True)
class FeeModel:
    name: str = "kalshi"
    taker_coef: float = TAKER_COEF
    maker_coef: float = TAKER_COEF * 0.25
    multiplier: float = 1.0
    precision: float = 0.01
    supported: bool = True

    @staticmethod
    def _curve(p: float) -> float:
        p = min(max(p, 0.0), 1.0)
        return p * (1.0 - p)

    # exact per-fill charges ------------------------------------------------------------
    def taker_fee(self, size: float, price: float) -> float:
        return ceil_to(self.multiplier * self.taker_coef * size * self._curve(price), self.precision)

    def maker_fee(self, size: float, price: float) -> float:
        return ceil_to(self.multiplier * self.maker_coef * size * self._curve(price), self.precision)

    def maker_rebate(self, size: float, price: float) -> float:
        return 0.0

    # per-contract (unrounded) for pricing decisions -----------------------------------
    def taker_fee_per_share(self, price: float) -> float:
        return self.multiplier * self.taker_coef * self._curve(price)

    def maker_fee_per_share(self, price: float) -> float:
        return self.multiplier * self.maker_coef * self._curve(price)

    def with_capture(self, _capture: float) -> "FeeModel":   # engine compatibility
        return self


def fee_model_for(market: Optional[MarketInfo], profile: str = "kalshi", rebate_capture: float = 1.0,
                  use_market_schedule: bool = True, precision: float = 0.01,
                  default_fee_type: str = "quadratic_with_maker_fees") -> FeeModel:
    if profile == "zero":
        return FeeModel(name="zero", taker_coef=0.0, maker_coef=0.0)
    fee_type = (market.fee_type if (market and use_market_schedule) else default_fee_type) or default_fee_type
    mult = market.fee_multiplier if (market and use_market_schedule and market.fee_multiplier is not None) else 1.0
    if profile == "kalshi_no_maker_fee":
        fee_type = "quadratic"
    if fee_type not in MAKER_SHARE:
        return FeeModel(name=f"kalshi({fee_type})", supported=False, multiplier=mult, precision=precision)
    return FeeModel(name=f"kalshi({fee_type} x{mult:g})", maker_coef=TAKER_COEF * MAKER_SHARE[fee_type],
                    multiplier=mult, precision=precision)
