"""`python -m kbot setup` and `python -m kbot check`.

setup: asks for your Kalshi API key ID and private key (file path or paste), stores the key as a
permission-locked file under keys/, writes the key ID + path to .env, then runs check.
Nothing is sent anywhere except signed requests to Kalshi itself.
"""
from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
import time
from typing import Dict, List, Optional, Tuple

from .config import BotConfig, credentials, load_env

ENV_FILE = ".env"
KEY_DIR = "keys"

INTRO = """
Kalshi bot setup
================
Two kinds of key, both optional - enter whichever you have:
  * DEMO key (demo.kalshi.co, fake money): `demo` mode, real orders on the demo exchange.
  * PRODUCTION key (kalshi.com, your real account): real order books + the settlement index for
    `paper`/`record`, and REAL-MONEY trading with `live`. Live mode only starts after you type START.

How to create a demo key:
  1. Go to https://demo.kalshi.co and create a demo account (separate from your real account).
  2. Account & security (profile menu) -> API Keys -> Create new API key.
  3. Kalshi shows a Key ID and downloads a private key file (.key / .txt). Keep that file.
     The private key is shown only once.
For production (optional) do the same at https://kalshi.com.

Never paste your private key into chats, emails or tickets. It stays on this computer.
"""


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


def _validate_key_bytes(data: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    k = serialization.load_pem_private_key(data, password=None)
    if isinstance(k, RSAPrivateKey):
        return f"RSA {k.key_size}-bit"
    if isinstance(k, Ed25519PrivateKey):
        return "Ed25519"
    raise ValueError("unsupported key type (need RSA or Ed25519)")


def _read_pasted_key() -> bytes:
    print("Paste the private key, including the -----BEGIN ...----- and -----END ...----- lines:")
    lines: List[str] = []
    while True:
        try:
            ln = input()
        except EOFError:
            break
        lines.append(ln)
        if ln.strip().startswith("-----END"):
            break
    return ("\n".join(lines).strip() + "\n").encode()


def _read_file(path: str) -> bytes:
    # accept paths dragged into the terminal (quoted, with escaped spaces)
    path = os.path.expanduser(path.strip().strip("'\"").replace("\\ ", " "))
    with open(path, "rb") as fh:
        return fh.read()


def save_key(env: str, data: bytes) -> str:
    os.makedirs(KEY_DIR, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    path = os.path.join(KEY_DIR, f"kalshi-{env}.pem")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)
    return path


def write_env(updates: Dict[str, str], path: str = ENV_FILE) -> None:
    lines: List[str] = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    keys_done = set()
    out = []
    for ln in lines:
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", ln)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            keys_done.add(m.group(1))
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in keys_done:
            out.append(f"{k}={v}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out).strip() + "\n")
    os.chmod(path, 0o600)
    for k, v in updates.items():
        os.environ[k] = v


def ensure_gitignore() -> None:
    wanted = [".env", "keys/", "data/"]
    have = []
    if os.path.exists(".gitignore"):
        with open(".gitignore") as fh:
            have = fh.read().splitlines()
    add = [w for w in wanted if w not in have]
    if add:
        with open(".gitignore", "a") as fh:
            fh.write("\n".join([""] + add) + "\n")


def collect_key(env: str, key_id: Optional[str] = None, key_file: Optional[str] = None,
                interactive: bool = True) -> Optional[Tuple[str, str]]:
    label = "DEMO (demo.kalshi.co)" if env == "demo" else "PRODUCTION (kalshi.com, real account)"
    if interactive and not key_id:
        print(f"\n--- {label} key ---")
        key_id = _ask("Key ID (looks like a UUID; leave empty to skip)")
    if not key_id:
        return None
    if not re.fullmatch(r"[0-9a-fA-F-]{16,64}", key_id):
        print(f"  warning: '{key_id}' doesn't look like a Kalshi key ID; continuing anyway")
    for attempt in range(3):
        if key_file is None and interactive:
            src = _ask("Private key: path to the downloaded file, or type 'paste'")
            data = _read_pasted_key() if src.lower() == "paste" else _read_file(src) if src else b""
        else:
            data = _read_file(key_file) if key_file else b""
        try:
            kind = _validate_key_bytes(data)
            path = save_key(env, data)
            print(f"  ok: {kind} private key saved to {path} (readable only by you)")
            return key_id, path
        except Exception as e:  # noqa: BLE001
            print(f"  that key could not be loaded ({type(e).__name__}: {e}).")
            key_file = None
            if not interactive:
                return None
    return None


def run_setup(cfg: BotConfig, args) -> int:
    interactive = not (args.demo_key_id or args.prod_key_id)
    if interactive:
        print(INTRO)
    ensure_gitignore()
    updates: Dict[str, str] = {}
    got = collect_key("demo", args.demo_key_id, args.demo_key_file, interactive)
    if got:
        updates[cfg.kalshi.demo_key_id_env], updates[cfg.kalshi.demo_key_path_env] = got
    if interactive:
        want_prod = _ask("\nAdd your PRODUCTION (kalshi.com) key? (y/N)", "n").lower().startswith("y")
    else:
        want_prod = bool(args.prod_key_id)
    if want_prod:
        got = collect_key("prod", args.prod_key_id, args.prod_key_file, interactive)
        if got:
            updates[cfg.kalshi.prod_key_id_env], updates[cfg.kalshi.prod_key_path_env] = got
    if updates:
        write_env(updates)
        print(f"\nSaved to {ENV_FILE} (permissions 600). Key IDs and key paths only; key files live in {KEY_DIR}/.")
    else:
        print("\nNo keys entered. You can still record/paper-trade with public REST data.")
    print("\nChecking your setup against Kalshi...\n")
    return asyncio.run(run_check(cfg))


# ---------------------------------------------------------------------------------------------
async def _check_env(cfg: BotConfig, env: str) -> bool:
    from .fees import fee_model_for
    from .feeds.discovery import KalshiDiscovery
    from .feeds.kalshi_rest import KalshiError, KalshiRest
    k = cfg.kalshi
    base = k.demo_rest if env == "demo" else k.prod_rest
    cr = credentials(cfg, env)
    print(f"[{env}] {base}")
    ok = True
    rest = KalshiRest(base, **({"key_id": cr["key_id"], "key_path": cr["key_path"]} if cr else {}))
    try:
        st = await rest.exchange_status()
        print(f"  exchange: trading_active={st.get('trading_active')} exchange_active={st.get('exchange_active')}")
        if cr:
            try:
                bal = await rest.get_balance()
                cents = bal.get("balance")
                dollars = bal.get("balance_dollars") or (f"{cents / 100:.2f}" if isinstance(cents, (int, float)) else cents)
                print(f"  keys OK - balance ${dollars}")
            except KalshiError as e:
                ok = False
                print(f"  KEYS REJECTED ({e.status}). Check the key ID matches the private key and that the key "
                      f"was created on {'demo.kalshi.co' if env == 'demo' else 'kalshi.com'}; your computer's clock "
                      f"must also be correct. Details: {e.body[:200]}")
                print("  " + await diagnose_key(cfg, env, cr))
            try:
                lim = await rest.get_limits()
                print(f"  API tier/limits: {str(lim)[:200]}")
            except Exception:  # noqa: BLE001
                pass
        else:
            print(f"  no {env} keys found in {os.path.abspath(ENV_FILE)} (public data only)"
                  + ("  <- run menu option 1 (setup) in THIS folder" if env == "demo" else ""))
        disc = KalshiDiscovery(rest, k.series, cfg.markets.assets, lookahead=1)
        for asset, series in disc.series_by_asset.items():
            meta = await disc.series(series)
            print(f"  series {series}: fee_type={meta.get('fee_type', '?')} fee_multiplier={meta.get('fee_multiplier', '?')}"
                  f"  settlement={[s.get('name') for s in meta.get('settlement_sources') or []]}")
        ms = await disc.discover()
        if not ms:
            print("  no open/upcoming markets found for these series. On demo, crypto 15-minute markets may not be "
                  "listed; check series tickers in config.yaml (kalshi.series).")
        for m in ms:
            fm = fee_model_for(m, cfg.fees.profile, precision=cfg.fees.precision,
                               default_fee_type=cfg.fees.default_fee_type)
            print(f"  {m.slug}: target={m.open_price} closes {time.strftime('%H:%M:%S', time.localtime(m.end_ms / 1000))}"
                  f" tick={m.tick_size} | 10 contracts @0.47: taker ${fm.taker_fee(10, 0.47):.2f}"
                  f" maker ${fm.maker_fee(10, 0.47):.2f}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  could not reach Kalshi: {type(e).__name__}: {e}")
    finally:
        await rest.close()
    return ok


async def diagnose_key(cfg: BotConfig, env: str, cr: Dict[str, str]) -> str:
    """Try the same key against the OTHER environment (read-only balance call) to explain a 401."""
    from datetime import datetime, timezone
    from .feeds.kalshi_rest import KalshiError, KalshiRest
    k = cfg.kalshi
    other = "prod" if env == "demo" else "demo"
    rest = KalshiRest(k.prod_rest if other == "prod" else k.demo_rest, key_id=cr["key_id"], key_path=cr["key_path"])
    try:
        await rest.get_balance()
        return (f"DIAGNOSIS: this key works on {other.upper()}, not {env}. It was created on "
                f"{'kalshi.com' if other == 'prod' else 'demo.kalshi.co'}. Create a key on "
                f"{'demo.kalshi.co' if env == 'demo' else 'kalshi.com'} and run setup again.")
    except KalshiError:
        pass
    except Exception as e:  # noqa: BLE001
        return f"(could not test against {other}: {type(e).__name__})"
    finally:
        await rest.close()
    local = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    return ("DIAGNOSIS: the key is not accepted on either environment, so the Key ID and the private key file "
            "probably don't belong together (e.g. Key ID from one key, file from another), or the key was deleted. "
            f"Create a fresh key on {'demo.kalshi.co' if env == 'demo' else 'kalshi.com'} and run setup again. "
            f"Also confirm your clock is right: this computer says {local}.")


async def run_check(cfg: BotConfig) -> int:
    load_env()
    ok_demo = await _check_env(cfg, "demo")
    print()
    ok_prod = await _check_env(cfg, "prod")
    print()
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("websockets package missing: pip install -r requirements.txt")
        return 1
    if ok_prod and credentials(cfg, "prod"):
        print("Production key works. Next: menu 4 / `kbot paper` (simulated fills on real books), or "
              "menu 6 / `kbot live --i-understand-real-money` (REAL MONEY).")
        return 0
    if ok_demo and credentials(cfg, "demo"):
        print("Ready. Next: `python -m kbot demo` (real orders on the demo exchange) or "
              "`python -m kbot paper` (simulated fills on live data).")
        return 0
    print("Public data works" if ok_prod else "Problems above need fixing first.")
    return 0 if ok_prod else 1
