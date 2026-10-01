"""Exchange-shard collateral for Kalshi crypto markets.

Since 2026-08-24 Kalshi creates crypto events on exchange shard 2 (docs: Exchange Sharding).
"Programmatic traders must preallocate collateral on a given exchange shard before order
placement" - otherwise orders fail with 404 insufficient_shard_balance. This module finds the
shard the bot's markets live on, shows your balance per shard, and (only with your yes) moves
money inside your own account from the default shard to that shard.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Tuple

from .config import BotConfig, credentials

log = logging.getLogger("kbot.collateral")
DEFAULT_CRYPTO_SHARD = 2
_sleep = asyncio.sleep          # patched in tests


async def market_shard(rest, cfg: BotConfig) -> int:
    for asset in cfg.markets.assets:
        try:
            ms = await rest.get_markets(series_ticker=cfg.kalshi.series[asset], status="open", limit=5)
        except Exception:  # noqa: BLE001
            continue
        for m in ms:
            if m.get("exchange_index") not in (None, ""):
                return int(m["exchange_index"])
    return DEFAULT_CRYPTO_SHARD


async def prepare(cfg: BotConfig, env: str, needed: float, ask=input) -> Tuple[int, float, str]:
    """Returns (shard, balance_on_shard, message). Offers a transfer when the shard is short."""
    from .feeds.kalshi_rest import KalshiError, KalshiRest
    cr = credentials(cfg, env)
    if not cr:
        return DEFAULT_CRYPTO_SHARD, 0.0, "no keys"
    k = cfg.kalshi
    rest = KalshiRest(k.prod_rest if env == "prod" else k.demo_rest, key_id=cr["key_id"], key_path=cr["key_path"])
    try:
        shard = await market_shard(rest, cfg)
        bal = await rest.get_shard_balances()
        have = bal.get(shard, 0.0)
        main = bal.get(0, 0.0) if shard != 0 else 0.0
        print(f"  cash on the crypto exchange (shard {shard}): ${have:,.2f}"
              + (f"   on the default exchange (shard 0): ${main:,.2f}" if shard != 0 else ""))
        if have >= needed or shard == 0:
            return shard, have, "ok"
        move = round(min(needed - have, main), 2)
        if move < 1.0:
            return shard, have, (f"only ${have:.2f} on the crypto exchange and ${main:.2f} to move - deposit funds "
                                 f"or move money between exchanges on kalshi.com")
        ans = ask(f"  The bot's limits need up to ${needed:,.2f} there. Move ${move:,.2f} from your default "
                  f"exchange balance to the crypto exchange now? (y/N): ").strip().lower()
        if not ans.startswith("y"):
            return shard, have, "transfer declined"
        try:
            r = await rest.transfer_between_shards(move, dest_shard=shard, source_shard=0)
        except KalshiError as e:
            return shard, have, f"transfer failed ({e.status}): {e.body[:160]} - you can move it on kalshi.com instead"
        print(f"  transfer requested (id {r.get('transfer_id', '?')}), waiting for it to settle...")
        for _ in range(10):
            await _sleep(1.0)
            have = (await rest.get_shard_balances()).get(shard, 0.0)
            if have >= min(needed, have + move) - 0.01 and have > 0:
                break
        print(f"  cash on the crypto exchange now: ${have:,.2f}")
        return shard, have, "ok"
    finally:
        await rest.close()
