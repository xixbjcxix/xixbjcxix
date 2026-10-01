"""Opening-window day-trading agent for Public.com.   python -m pbot <command>

  setup    guided entry of your Public API secret key -> .env, picks your brokerage account
  check    verify the key, show equity / buying power, live quotes and today's bars
  sim      offline demo: a synthetic market on a fast virtual clock (no key needed)
  paper    REAL market data from Public, SIMULATED fills (no orders sent)
  live     REAL-MONEY orders (needs live.enabled: true in pbot.yaml + typed START, or --yes)
  report   trade journal summary (today or --all)
  flatten  sell positions the bot opened (live) and cancel their open orders
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import date, datetime, timedelta

from .clock import MarketCalendar, SystemClock
from .config import BotConfig, load_config

ENV_FILE = ".env"


def _setup_logging(cfg: BotConfig, verbose: bool) -> None:
    os.makedirs(cfg.data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    fh = logging.FileHandler(os.path.join(cfg.data_dir, "pbot.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        pass


def _client(cfg: BotConfig):
    from .public_api import PublicClient
    secret = os.environ.get(cfg.account.secret_env, "").strip()
    acct = os.environ.get(cfg.account.account_id_env, "").strip() or None
    if not secret:
        sys.exit(f"No API secret in {ENV_FILE} ({cfg.account.secret_env}). Run:  python -m pbot setup")
    return PublicClient(secret, acct, cfg.account.base_url, cfg.account.token_validity_minutes)


def _journal(cfg: BotConfig):
    from .journal import Journal
    return Journal(os.path.join(cfg.data_dir, "pbot.db"))


# ---- commands ---------------------------------------------------------------------------------
def cmd_setup(cfg: BotConfig, args) -> int:
    from dotenv import set_key
    from .public_api import PublicAPIError, PublicClient
    print("Public.com API setup\n"
          "  1. In the Public app or at public.com: Settings -> API (or 'Developer') -> create a secret key.\n"
          "  2. Paste it below. It is stored only in .env on this computer (owner-only permissions).\n"
          "  Never paste the key into a chat, email or ticket.\n")
    secret = (args.secret or getpass.getpass("API secret key (input hidden): ")).strip()
    if not secret:
        print("No key entered.")
        return 1
    client = PublicClient(secret, None, cfg.account.base_url, cfg.account.token_validity_minutes)
    try:
        accounts = client.accounts()
    except PublicAPIError as e:
        print(f"Key rejected: {e}")
        return 1
    brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"] or accounts
    if not brokerage:
        print("Key works, but no accounts were returned.")
        return 1
    if args.account:
        acct = args.account
    elif len(brokerage) == 1:
        acct = brokerage[0]["accountId"]
    else:
        for i, a in enumerate(brokerage):
            print(f"  [{i}] {a['accountId']}  {a.get('accountType')}  {a.get('brokerageAccountType', '')}")
        acct = brokerage[int(input("Which account should the bot trade? ") or 0)]["accountId"]
    if not os.path.exists(ENV_FILE):
        open(ENV_FILE, "a").close()
    os.chmod(ENV_FILE, 0o600)
    set_key(ENV_FILE, cfg.account.secret_env, secret, quote_mode="never")
    set_key(ENV_FILE, cfg.account.account_id_env, acct, quote_mode="never")
    os.environ[cfg.account.secret_env] = secret
    os.environ[cfg.account.account_id_env] = acct
    print(f"Saved to {ENV_FILE}: account {acct}\n")
    return cmd_check(cfg, args)


def cmd_check(cfg: BotConfig, args) -> int:
    from .broker import LiveBroker
    client = _client(cfg)
    cal = MarketCalendar(cfg.session)
    if not client.account_id:
        accts = client.accounts()
        print("Accounts:", [(a["accountId"], a.get("accountType")) for a in accts])
        print(f"Set {cfg.account.account_id_env} in .env (or rerun setup).")
        return 1
    eq, bp = LiveBroker(client, cfg.execution, lambda s: None, cfg.risk.use_cash_only_buying_power).account()
    print(f"Account {client.account_id}: equity ${eq:,.2f}, buying power ${bp:,.2f}")
    if cfg.risk.pdt_guard and eq < cfg.risk.pdt_equity_threshold:
        print(f"  Under ${cfg.risk.pdt_equity_threshold:,.0f}: the PDT guard limits the bot to "
              f"{cfg.risk.pdt_max_day_trades} day trades per 5 business days.")
    q = client.quotes(cfg.watchlist)
    print("\nWatchlist quotes:")
    for s in cfg.watchlist:
        x = q.get(s)
        if not x:
            print(f"  {s:<6} (no quote - check the symbol)")
            continue
        sp = x.spread_pct
        print(f"  {s:<6} last {x.last}  bid {x.bid}  ask {x.ask}  spread "
              f"{'n/a' if sp is None else f'{sp:.3f}%'}")
    sym = cfg.watchlist[0]
    bars = client.bars_today(sym)
    print(f"\nToday's 1-minute bars for {sym}: {len(bars)}"
          + (f" (first {bars[0].ts}, last {bars[-1].ts})" if bars else ""))
    today = SystemClock(cal.tz).now().date()
    nxt = cal.next_trading_day(today)
    print(f"\nNext session {nxt}: open {cal.open_time(nxt):%H:%M}, entries until "
          f"{cal.entry_cutoff(nxt):%H:%M}, flat by {cal.flatten_time(nxt):%H:%M} ET")
    print("\nAll good. Next:  python -m pbot paper")
    return 0


def cmd_sim(cfg: BotConfig, args) -> int:
    from .agent import Agent
    from .broker import make_sim
    from .journal import Journal
    cal = MarketCalendar(cfg.session)
    day = date.fromisoformat(args.date) if args.date else cal.next_trading_day(date.today())
    day = cal.next_trading_day(day)
    journal = Journal(os.path.join(cfg.data_dir, "pbot-sim.db") if args.keep else ":memory:")
    total = 0.0
    for i in range(args.days):
        start = cal.open_time(day) - timedelta(minutes=10)
        clock, market, broker = make_sim(cfg.watchlist, cal, start, args.seed + i, cfg.paper.starting_cash)
        agent = Agent(cfg, "sim", clock, market, broker, journal, cal)
        s = agent.run_day() or {}
        total += s.get("net", 0.0)
        day = cal.next_trading_day(day, include_today=False)
    print()
    print(journal.report("sim"))
    print("\nSYNTHETIC data - this shows the machinery working, not that the strategy makes money.")
    return 0


def _run_real(cfg: BotConfig, args, mode: str) -> int:
    from .agent import Agent
    from .broker import LiveBroker, PaperBroker, PublicMarketData
    client = _client(cfg)
    if not client.account_id:
        sys.exit("No account id - run:  python -m pbot setup")
    cal = MarketCalendar(cfg.session)
    clock = SystemClock(cal.tz)
    market = PublicMarketData(client)
    if mode == "live":
        broker = LiveBroker(client, cfg.execution, clock.sleep, cfg.risk.use_cash_only_buying_power)
    else:
        broker = PaperBroker(lambda s: market.quotes([s]).get(s), cfg.paper.starting_cash)
    alert = os.environ.get(cfg.alert_webhook_env) or None
    agent = Agent(cfg, mode, clock, market, broker, _journal(cfg), cal, alert)
    try:
        if args.once:
            agent.run_day()
        else:
            agent.run_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_paper(cfg: BotConfig, args) -> int:
    return _run_real(cfg, args, "paper")


def cmd_live(cfg: BotConfig, args) -> int:
    if not cfg.live.enabled:
        sys.exit("Live trading is off. Paper trade first, then set `live: {enabled: true}` in pbot.yaml.")
    r = cfg.risk
    print("=" * 72)
    print("REAL MONEY. This bot will place real orders on your Public account.")
    print(f"  watchlist      {', '.join(cfg.watchlist)}")
    print(f"  risk / trade   ${r.risk_per_trade_usd:g}   max position ${r.max_position_usd:g}   "
          f"max open {r.max_open_positions}")
    print(f"  trades / day   {r.max_trades_per_day}   daily loss stop ${r.max_daily_loss_usd:g}   "
          f"PDT guard {'on' if r.pdt_guard else 'OFF'}")
    print(f"  window         {cfg.session.open}-{cfg.session.flatten_at} ET, flat every day")
    print("Day trading loses money for most people. Only use money you can afford to lose.")
    print("=" * 72)
    if not args.yes:
        if input("Type START to trade real money: ").strip() != "START":
            print("Not started.")
            return 1
    return _run_real(cfg, args, "live")


def cmd_report(cfg: BotConfig, args) -> int:
    j = _journal(cfg) if args.mode != "sim" else None
    if j is None:
        from .journal import Journal
        j = Journal(os.path.join(cfg.data_dir, "pbot-sim.db"))
    day = None if args.all else (date.fromisoformat(args.date) if args.date else
                                 SystemClock(MarketCalendar(cfg.session).tz).now().date())
    print(j.report(args.mode, day))
    return 0


def cmd_flatten(cfg: BotConfig, args) -> int:
    from .broker import LiveBroker
    client = _client(cfg)
    cal = MarketCalendar(cfg.session)
    clock = SystemClock(cal.tz)
    broker = LiveBroker(client, cfg.execution, clock.sleep, cfg.risk.use_cash_only_buying_power)
    j = _journal(cfg)
    rows = j.open_trades("live")
    if not rows:
        print("The bot has no open live trades in its journal.")
        return 0
    syms = {r["symbol"] for r in rows}
    for o in client.portfolio().get("orders") or []:
        if (o.get("instrument") or {}).get("symbol") in syms and o.get("status") in ("NEW", "PARTIALLY_FILLED"):
            print(f"cancel {o['instrument']['symbol']} order {o['orderId']}")
            broker.cancel(o["orderId"])
    held = broker.positions()
    for r in rows:
        qty = min(r["qty"], held.get(r["symbol"], 0.0))
        if qty <= 0:
            j.close_trade(r["id"], clock.now(), r["entry"], "closed outside the bot (P&L unknown)")
            continue
        st = broker.sell(r["symbol"], qty, None)
        px = st.avg_price or r["entry"]
        pnl = j.close_trade(r["id"], clock.now(), px, "manual flatten", qty=st.filled_qty or qty)
        print(f"sold {r['symbol']} {st.filled_qty:g} @ {px:.2f} ({st.status}) P&L {pnl:+.2f}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="pbot.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="also narrate per-symbol waiting reasons")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="enter your Public API secret key")
    s.add_argument("--secret", help="(scripted) secret key; prefer the hidden prompt")
    s.add_argument("--account", help="(scripted) account id")
    sub.add_parser("check", help="verify key, account and data")
    s = sub.add_parser("sim", help="offline synthetic-market demo")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--date", help="YYYY-MM-DD to simulate")
    s.add_argument("--keep", action="store_true", help="keep results in data/pbot-sim.db")
    for name, h in (("paper", "real data, simulated fills"), ("live", "REAL-MONEY orders")):
        s = sub.add_parser(name, help=h)
        s.add_argument("--once", action="store_true", help="trade one session then exit (default: every day)")
        if name == "live":
            s.add_argument("--yes", action="store_true", help="skip the typed START (for schedulers)")
    s = sub.add_parser("report", help="journal summary")
    s.add_argument("--mode", default="paper", choices=["paper", "live", "sim"])
    s.add_argument("--date")
    s.add_argument("--all", action="store_true")
    sub.add_parser("flatten", help="sell the bot's open live positions now")
    args = p.parse_args(argv)

    _load_env()
    cfg = load_config(args.config)
    _setup_logging(cfg, args.verbose)
    return {"setup": cmd_setup, "check": cmd_check, "sim": cmd_sim, "paper": cmd_paper,
            "live": cmd_live, "report": cmd_report, "flatten": cmd_flatten}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
