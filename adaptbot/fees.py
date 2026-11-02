"""Kalshi trading fees.

Kalshi charges ceil_to_cent(rate * count * price * (1 - price)) per order.
Settlement is free.  We only take liquidity, so the taker rate applies.
"""
import math


def order_fee(price: float, count: float, rate: float) -> float:
    if count <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = rate * count * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def fee_per_contract(price: float, count: float, rate: float) -> float:
    return order_fee(price, count, rate) / count if count > 0 else 0.0
