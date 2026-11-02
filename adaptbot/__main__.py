"""Menu and command line.

    python -m adaptbot                 menu
    python -m adaptbot paper|live|record|check
    python -m adaptbot backtest [files...]            replay recordings
    python -m adaptbot backtest --synthetic [N] [--no-edge]
    python -m adaptbot backtest ... --seed            also save the learning state
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import sys

from . import backtest as bt
from .brain import Brain
from .config import DATA_DIR, load_settings, save_settings, settings_path
from .dashboard import start_dashboard
from .engine import Engine, make_broker, preflight
from .kalshi import KalshiClient, load_keys, save_keys
from .recorder import Recorder, read_records
from .risk import InstanceLock
from .spot import SpotHub

MENU = """
adaptbot - adaptive Kalshi 15-minute bot
----------------------------------------
  1  Setup (enter API keys)
  2  Check keys, markets and price feeds
  3  PAPER trading (real data, simulated fills) + dashboard
  4  LIVE trading - REAL MONEY + dashboard
  5  Backtest recorded data
  6  Backtest synthetic data (plumbing test only)
  7  Record market data only
  8  Quit
"""


def setup_logging() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), encoding="utf-8"),
    ])
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def ensure_settings():
    s = load_settings()
    if not os.path.exists(settings_path()):
        save_settings(s)
        print(f"created {settings_path()} with safe defaults - edit it to change limits")
    errs = s.validate()
    if errs:
        print("settings problems:\n  " + "\n  ".join(errs))
        sys.exit(1)
    return s


def client_for(s, need_auth: bool) -> KalshiClient:
    keys = load_keys()
    if need_auth and not keys:
        print("No API keys yet - run Setup (1) first.")
        sys.exit(1)
    kid, kpath = keys if keys else (None, None)
    return KalshiClient(s.base_url, kid, kpath, s.price_format, s.time_in_force)


def do_setup() -> None:
    print("Create an API key on kalshi.com (Account -> API keys) and download the private key file.")
    kid = input("API key id: ").strip()
    path = input("Path to the private key .pem/.key file: ").strip().strip('"')
    if not os.path.exists(path):
        print("file not found")
        return
    save_keys(kid, os.path.abspath(path))
    print("saved. Run Check (2) next.")


async def do_check() -> None:
    s = ensure_settings()
    c = client_for(s, need_auth=False)
    spot = SpotHub(s.assets, s.stale_spot_s, s.min_spot_sources)
    spot.start()
    try:
        if c.authed:
            try:
                print(f"balance: ${await c.balance():.2f}")
                print(f"positions: {await c.positions() or 'none'}")
            except Exception as e:  # noqa: BLE001
                print(f"auth check FAILED: {e!r}")
        else:
            print("no keys - skipping account check")
        for series in s.series:
            ms = await c.markets(series, status="open")
            print(f"{series}: {len(ms)} open markets")
            for m in ms[:3]:
                print(f"   {m.get('ticker')} strike={m.get('floor_strike')} closes {m.get('close_time')}")
            if ms:
                b = await c.orderbook(ms[0]["ticker"])
                print(f"   book: yes bid {b.best_bid('yes')} / yes ask {b.best_ask('yes')}")
        await asyncio.sleep(8)
        for a in s.assets:
            px, n = spot.price(a)
            print(f"{a} reference price: {px} from {n} sources  {spot.status()[a]}")
    finally:
        await spot.stop()
        await c.close()


async def do_run(live: bool, trade: bool = True) -> None:
    s = ensure_settings()
    lock = InstanceLock()
    if not lock.acquire():
        print(f"Another copy of the bot is running (lock {lock.path}). Stop it first. "
              "If none is running, wait a minute and retry.")
        return
    kill = os.path.join(DATA_DIR, "KILL")
    if os.path.exists(kill):
        os.remove(kill)
    c = client_for(s, need_auth=live)
    spot = SpotHub(s.assets, s.stale_spot_s, s.min_spot_sources)
    spot.start()
    brain = Brain(s)
    if brain.load():
        print(f"loaded learning state ({brain.selector.current}, calibration n={brain.cal.n})")
    broker = make_broker(s, c, live)
    try:
        if live:
            print("\n" + "!" * 66 + "\n  LIVE TRADING - REAL MONEY\n" + "!" * 66)
            print(f"  assets {s.assets}  bankroll ${s.bankroll}  max/order {s.max_contracts_per_order} contracts")
            print(f"  max $/market {s.max_dollars_per_market}  max $ total {s.max_dollars_total}"
                  f"  daily loss stop ${s.max_daily_loss}")
            print("  preflight...")
            problems = await preflight(s, c, spot, broker)
            if problems:
                print("PREFLIGHT FAILED - not trading:\n  - " + "\n  - ".join(problems))
                return
            n = await broker.cancel_own_resting()
            if n:
                print(f"  cancelled {n} leftover bot orders")
            if input("Type START to trade real money: ").strip() != "START":
                print("cancelled")
                return
        eng = Engine(s, c, broker, spot, brain, Recorder(enabled=s.record), trade=trade)
        eng.lock = lock
        runner = await start_dashboard(eng, s.dashboard_port)
        print(f"\n  Dashboard: http://127.0.0.1:{s.dashboard_port}   Stop: Ctrl-C, dashboard KILL, or create {kill}\n")
        try:
            await eng.run()
        finally:
            await runner.cleanup()
    finally:
        await spot.stop()
        await c.close()
        lock.release()


def do_backtest(args: list[str]) -> None:
    s = ensure_settings()
    seed = "--seed" in args
    if "--synthetic" in args and seed:
        print("--seed ignored: never teach the bot from synthetic data")
        seed = False
    if "--synthetic" in args:
        nums = [a for a in args if a.isdigit()]
        recs = bt.synthetic(int(nums[0]) if nums else 192, edge="none" if "--no-edge" in args else "lag")
        print("SYNTHETIC data: this tests the machinery, it says nothing about real profitability.")
    else:
        files = [a for a in args if not a.startswith("--")] or \
            sorted(glob.glob(os.path.join(DATA_DIR, "recordings", "*.jsonl*")))
        if not files:
            print("no recordings found - run PAPER or Record mode for a while first")
            return
        print(f"replaying {len(files)} file(s)")
        recs = read_records(files)
    brain = Brain(s, state_file=os.path.join(DATA_DIR, "state.json") if seed else "")
    rep = bt.run(s, recs, brain=brain)
    bt.print_report(rep)
    out = os.path.join(DATA_DIR, "backtest_report.json")
    bt.save_report(rep, out)
    print(f"\nreport saved to {out}")
    if seed:
        brain.save()
        print("learning state saved - paper/live will start from these results")


def main(argv: list[str]) -> None:
    setup_logging()
    cmd = argv[0] if argv else None
    if cmd is None:
        print(MENU)
        cmd = {"1": "setup", "2": "check", "3": "paper", "4": "live", "5": "backtest",
               "6": "synthetic", "7": "record"}.get(input("Choose 1-8: ").strip(), "quit")
    try:
        if cmd == "setup":
            do_setup()
        elif cmd == "check":
            asyncio.run(do_check())
        elif cmd == "paper":
            asyncio.run(do_run(live=False))
        elif cmd == "live":
            asyncio.run(do_run(live=True))
        elif cmd == "record":
            asyncio.run(do_run(live=False, trade=False))
        elif cmd == "backtest":
            do_backtest(argv[1:])
        elif cmd == "synthetic":
            do_backtest(["--synthetic"])
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main(sys.argv[1:])
