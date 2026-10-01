"""Kalshi 15-minute crypto bot.   python -m kbot <command>

  setup     guided entry of your Kalshi API keys (demo, optional prod) -> .env, then runs check
  check     verify keys, balance, fee settings of the series, and list the current markets
  record    stage 1: record live order books, trades and the settlement index to SQLite
  backtest  stage 1: replay a recording through the strategy + fill model (--compare for variants)
  paper     stage 2: live market data, SIMULATED fills (no orders sent), with dashboard
  demo      stage 2: REAL orders on Kalshi's demo environment (fake money), with dashboard
  replay    replay a recording through the dashboard at N x speed (offline)
  synth     write a SYNTHETIC recording (pipeline testing only - not evidence of edge)
  report    daily report (net, paired/residual/cuts, fees, drawdown, risk events) from the trade database
  live      stage 3: REAL-MONEY orders on kalshi.com (needs --i-understand-real-money + typed START)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import List, Tuple

from .config import BotConfig, load_config, with_overrides


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def standard_variants(cfg: BotConfig) -> List[Tuple[str, BotConfig]]:
    """Variants on equal footing (daily-loss halt lifted); the first row keeps it as configured."""
    r = with_overrides(cfg, **{"risk.max_daily_loss_usd": 1e9})
    return [
        ("base, daily-loss halt ON (as configured)", cfg),
        ("base (residual gated by signal)", r),
        ("no residual (always pair or cut)", with_overrides(r, **{"strategy.residual.enabled": False})),
        ("residual allowed but still completed", with_overrides(r, **{"strategy.residual.ride_when_agreeing": False})),
        ("hold legs to expiry (no rescue/cut)", with_overrides(r, **{
            "strategy.rescue_window_s": 0.0, "strategy.flatten_s": 0.0,
            "strategy.rescue_pair_cost": cfg.strategy.target_pair_cost,
            "strategy.residual.max_shares": 1e9, "strategy.residual.max_usd": 1e9})),
        ("no maker fees (series without them)", with_overrides(r, **{"fees.profile": "kalshi_no_maker_fee"})),
        ("target pair cost 0.92", with_overrides(r, **{"strategy.target_pair_cost": 0.92})),
        ("target pair cost 0.96", with_overrides(r, **{"strategy.target_pair_cost": 0.96})),
        ("no model-edge gate on residual", with_overrides(r, **{"strategy.residual.min_edge": 0.0})),
        ("vol-aware entry (0.02/unit)", with_overrides(r, **{"strategy.vol_widen_per_unit": 0.02})),
        ("adverse-selection skew 1 tick", with_overrides(r, **{"strategy.adverse_skew_ticks": 1})),
        ("max 3 legs per market", with_overrides(r, **{"strategy.max_legs_per_market": 3})),
    ]


def summary_table(results: List[dict]) -> str:
    cols = [("variant", "label", 40), ("net", "net_pnl", 9), ("±se", "net_pnl_se", 7),
            ("paired", "paired_pnl", 9), ("resid", "residual_pnl", 8), ("cuts", "cut_pnl", 8),
            ("fees", "fees_paid", 7), ("edge c", "pair_edge_cents", 7), ("%paired", "pct_capital_paired", 8),
            ("maxDD", "max_drawdown", 8), ("fills/h", "fills_per_hour", 8), ("halts", "daily_loss_halts", 6)]
    lines = ["  ".join(h.ljust(w) if i == 0 else h.rjust(w) for i, (h, _, w) in enumerate(cols))]
    for r in results:
        row = []
        for i, (_, k, w) in enumerate(cols):
            v = r.get(k)
            s = str(v) if v is not None else "-"
            row.append(s[:w].ljust(w) if i == 0 else s.rjust(w))
        lines.append("  ".join(row))
    return "\n".join(lines)


def cmd_backtest(args, cfg: BotConfig) -> int:
    from .backtest import run_backtest
    from .report import format_report
    from .store import Store

    db = args.db or cfg.recording_db
    if not os.path.exists(db):
        print(f"recording not found: {db} (run `record` or `synth` first)")
        return 2
    variants = standard_variants(cfg) if args.compare else [("base", cfg)]
    results = []
    if args.jobs > 1 and len(variants) > 1 and not args.store:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for f in [ex.submit(run_backtest, vcfg, db, label) for label, vcfg in variants]:
                rep = f.result()
                results.append(rep)
                print(format_report(rep), f"\n  ({rep['wall_s']:.0f}s)\n", flush=True)
    else:
        out_store = Store(cfg.db_path) if args.store else None
        for label, vcfg in variants:
            rep = run_backtest(vcfg, db, label=label, out_store=out_store if label == variants[0][0] else None)
            results.append(rep)
            print(format_report(rep), f"\n  ({rep['wall_s']:.0f}s)\n", flush=True)
        if out_store:
            out_store.close()
    os.makedirs("reports", exist_ok=True)
    path = args.out or f"reports/backtest-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(path, "w") as fh:
        json.dump({"recording": db, "results": results}, fh, indent=2, default=str)
    if len(results) > 1:
        print(summary_table(results))
    print(f"saved {path}")
    return 0


def cmd_synth(args, cfg: BotConfig) -> int:
    from .synth import SynthParams, generate
    out = args.out or "data/synthetic.db"
    ms = generate(out, SynthParams(hours=args.hours, seed=args.seed))
    print(f"SYNTHETIC recording written to {out}: {len(ms)} markets over {args.hours} h")
    return 0


def _engine(mode: str, args, cfg: BotConfig) -> int:
    if mode == "demo":
        from .collateral import prepare
        print("Checking cash on the demo crypto exchange...")
        try:
            _, _, msg = asyncio.run(prepare(cfg, "demo", cfg.risk.max_total_usd))
            if msg != "ok":
                print(f"  note: {msg}")
        except Exception as e:  # noqa: BLE001
            print(f"  could not check shard balances: {e}")
    _run_engine(cfg, mode, args)
    return 0


def _run_engine(cfg: BotConfig, mode: str, args) -> None:
    """Run the engine once, or under the crash-restart supervisor with --supervise."""
    from .engine import Engine
    if not getattr(args, "supervise", False):
        asyncio.run(Engine(cfg, mode, config_path=args.config).run(hours=args.hours))
        return
    from .ops import Notifier, supervise
    note = Notifier(cfg.alerts, name=f"kbot {mode}")
    deadline = time.time() + args.hours * 3600 if args.hours else None

    async def once(attempt: int) -> None:
        left = (deadline - time.time()) / 3600 if deadline else None
        if left is not None and left <= 0:
            return
        eng = Engine(cfg, mode, config_path=args.config)
        eng.core.restarts = attempt
        await eng.run(hours=left)

    n = asyncio.run(supervise(once, max_restarts=args.max_restarts, notify=note.send))
    if n:
        print(f"finished after {n} automatic restart(s)")


def cmd_report(args, cfg: BotConfig) -> int:
    from .dayreport import build_day_report, format_day_report
    try:
        r = build_day_report(args.db or cfg.db_path, args.day, args.mode)
    except FileNotFoundError as e:
        print(f"trade database not found: {e} (run `paper` or `demo` first)")
        return 2
    print(json.dumps(r, indent=2) if args.json else format_day_report(r))
    return 0


def cmd_replay(args, cfg: BotConfig) -> int:
    from .replay_ui import run_demo
    asyncio.run(run_demo(cfg, args.db or cfg.recording_db, speed=args.speed, minutes=args.minutes,
                         start_offset_min=args.start_offset))
    return 0


def cmd_setup(args, cfg: BotConfig) -> int:
    from .setup_wizard import run_setup
    return run_setup(cfg, args)


def cmd_check(args, cfg: BotConfig) -> int:
    from .setup_wizard import run_check
    return asyncio.run(run_check(cfg))


def cmd_live(args, cfg: BotConfig) -> int:
    from .config import credentials
    if not cfg.live.enabled:
        print("live mode is disabled in config.yaml (live.enabled: false)")
        return 1
    if not args.i_understand_real_money:
        print("Live mode places REAL-MONEY orders on kalshi.com. Start it with:\n"
              "    kbot live --i-understand-real-money\n(or choose it from the kbot.bat menu)")
        return 1
    if not credentials(cfg, "prod"):
        print("No production API key found. Run setup and add your kalshi.com key as the production key.")
        return 1
    lv, r, st = cfg.live, cfg.risk, cfg.strategy
    lim = (lambda a, b: min(a, b)) if lv.use_starter_limits else (lambda a, b: a)
    print("\n" + "!" * 66)
    print("  LIVE TRADING - REAL MONEY on kalshi.com")
    print("!" * 66)
    print(f"  markets        : {', '.join(cfg.kalshi.series[a] for a in cfg.markets.assets)}")
    print(f"  contracts/order: {lim(st.clip_shares, lv.clip_shares):g}   target pair cost (incl. fees): ${st.target_pair_cost}")
    print(f"  max $/order    : {lim(r.max_order_usd, lv.max_order_usd):g}   max $/market: {lim(r.max_market_usd, lv.max_market_usd):g}"
          f"   max $ total: {lim(r.max_total_usd, lv.max_total_usd):g}")
    print(f"  max unpaired   : ${lim(r.max_residual_usd, lv.max_residual_usd):g} / "
          f"{lim(r.max_residual_shares, lv.max_residual_shares):g} contracts per market")
    print(f"  daily loss stop: ${lim(r.max_daily_loss_usd, lv.max_daily_loss_usd):g}"
          + ("   (starter limits ON)" if lv.use_starter_limits else ""))
    print("  The bot only cancels its own orders and skips markets you already hold.")
    print("  Stop: Ctrl-C in this window, the dashboard Kill switch, or create data\\KILL.")
    print("  Results so far are from simulated data only - you can lose money.\n")
    if not args.yes:
        try:
            ans = input("Type the word START to begin live trading (anything else cancels): ").strip()
        except EOFError:
            ans = ""
        if ans.upper() != "START":
            print("cancelled - no orders placed.")
            return 1
    from .collateral import prepare
    need = lim(r.max_total_usd, lv.max_total_usd)
    print("\nChecking cash on Kalshi's crypto exchange...")
    try:
        shard, have, msg = asyncio.run(prepare(cfg, "prod", need, ask=(lambda q: "y") if args.yes else input))
    except Exception as e:  # noqa: BLE001
        shard, have, msg = 2, 0.0, f"{type(e).__name__}: {e}"
    if msg != "ok":
        print(f"  note: {msg}")
    if have < lim(r.max_order_usd, lv.max_order_usd):
        print(f"\nNot enough cash on the crypto exchange (shard {shard}) to place orders (${have:.2f}).\n"
              "Run live again and answer y to the transfer, or move money between exchanges on kalshi.com.")
        return 1
    from .engine import Engine
    _run_engine(cfg, "live", args)
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows consoles: never crash on a non-ASCII character
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    p = argparse.ArgumentParser(prog="kbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.yaml" if os.path.exists("config.yaml") else None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="enter API keys (guided)")
    s.add_argument("--demo-key-id")
    s.add_argument("--demo-key-file")
    s.add_argument("--prod-key-id")
    s.add_argument("--prod-key-file")
    sub.add_parser("check", help="verify keys and list markets")
    for name, h in (("record", "record live data"), ("paper", "live data, simulated fills"),
                    ("demo", "real orders on the demo exchange")):
        s = sub.add_parser(name, help=h)
        s.add_argument("--hours", type=float, default=None)
        s.add_argument("--supervise", action="store_true",
                       help="restart automatically after an unexpected crash (not after a kill switch)")
        s.add_argument("--max-restarts", type=int, default=5, help="give up after this many crashes per hour")
    s = sub.add_parser("report", help="daily report from the trade database")
    s.add_argument("--day", help="UTC date YYYY-MM-DD (default today)")
    s.add_argument("--mode", choices=["paper", "demo", "live"], help="only runs of this mode")
    s.add_argument("--db")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("backtest", help="replay a recording")
    s.add_argument("--db")
    s.add_argument("--compare", action="store_true")
    s.add_argument("--jobs", type=int, default=1)
    s.add_argument("--store", action="store_true")
    s.add_argument("--out")
    s = sub.add_parser("replay", help="replay a recording through the dashboard")
    s.add_argument("--db")
    s.add_argument("--speed", type=float, default=20.0)
    s.add_argument("--minutes", type=float, default=None)
    s.add_argument("--start-offset", type=float, default=0.0)
    s = sub.add_parser("synth", help="SYNTHETIC recording")
    s.add_argument("--hours", type=float, default=24.0)
    s.add_argument("--seed", type=int, default=11)
    s.add_argument("--out")
    s = sub.add_parser("live", help="REAL-MONEY trading on kalshi.com")
    s.add_argument("--i-understand-real-money", action="store_true")
    s.add_argument("--yes", action="store_true", help="skip the typed START confirmation")
    s.add_argument("--hours", type=float, default=None)
    s.add_argument("--supervise", action="store_true", help="restart after an unexpected crash (never after a kill)")
    s.add_argument("--max-restarts", type=int, default=5)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    cmds = {"setup": cmd_setup, "check": cmd_check, "backtest": cmd_backtest, "synth": cmd_synth,
            "replay": cmd_replay, "live": cmd_live, "report": cmd_report,
            "record": lambda a, c: _engine("record", a, c), "paper": lambda a, c: _engine("paper", a, c),
            "demo": lambda a, c: _engine("demo", a, c)}
    return cmds[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
