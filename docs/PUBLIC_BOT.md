# pbot: opening-window day-trading agent for Public.com

`pbot` runs on its own every trading day. It wakes up before the open, trades small breakouts
during the first hour, and is flat (no positions) by 11:00 ET. It explains each decision in the
console and in its journal.

> **Read this first.** Most retail day traders lose money. This bot is a disciplined,
> risk-capped way to run one well-known intraday strategy. It cannot promise profits. The
> defaults risk **$5 per trade and at most $15 per day**. Paper trade for several weeks, read
> `python -m pbot report --all`, and only then decide whether to go live.

## What it does each day

| Time (ET)     | Agent                                                                                       |
|---------------|---------------------------------------------------------------------------------------------|
| 09:25         | Pre-flight: checks equity and buying power, counts recent day trades, resumes anything open. |
| 09:30 – 09:35 | Builds each watchlist symbol's **opening range** (high/low of the first five 1-minute bars).  |
| 09:35 – 10:30 | Looks for breakouts. It **buys** when price clears the range high while above **VWAP**, the range is 0.25–3% wide, opening volume is real, the spread is tight, and the move is not already extended. |
| until 11:00   | Manages each trade on every 2-second poll. The stop starts at the range midpoint and moves to **breakeven** at +1R. The target is **+2R**. |
| 11:00         | **Flattens everything**, then writes the day summary. It never holds overnight.              |

**Position sizing:** shares = the smallest of
* `risk_per_trade_usd / (entry − stop)`
* `max_position_usd / price`
* 50% of buying power / price

The result is rounded down to whole shares.

**Guards:**
* At most 2 open positions and 4 trades a day.
* A daily loss cap that counts open losses. Hitting it flattens everything and stops the bot for the day.
* A pattern-day-trader guard (see below).
* An outage guard: if market data is down about 30 s while positions are open, it flattens.
* A kill switch: create `data/STOP` or press Ctrl+C.

**Crash protection (live):** after each fill the bot rests a real STOP order at Public, ½R below
its own stop. If your computer or the bot dies, the position is still protected. The bot cancels
that order before it exits normally.

## Start small and build: the size ladder

Every account starts at **micro** and has to earn each step up. Paper and live each have their
own level.

| Level        | Risk/trade | Max position | Open | Trades/day | Daily loss cap |
|--------------|-----------:|-------------:|-----:|-----------:|---------------:|
| 0 `micro`    |        $2  |         $60  |    1 |          2 |            $6  |
| 1 `small`    |        $5  |        $150  |    2 |          4 |           $15  |
| 2 `medium`   |       $10  |        $300  |    2 |          4 |           $30  |
| 3 `standard` |       $20  |        $600  |    3 |          5 |           $60  |

* **Moving up is manual and has to be earned.** `python -m pbot promote` only works once the
  record *at the current level* shows all of these:
  * at least 20 closed trades;
  * at least 10 sessions;
  * a net profit;
  * a profit factor of at least 1.2 (gross wins ÷ gross losses);
  * a drawdown under half the demotion line.

  After a promotion the record starts again from zero.
* **Moving down is automatic.** Losing 6× the level's risk per trade from the peak drops the
  account one level at the next pre-flight.
* **Live has to be earned on paper.** `live` refuses to start until the paper record has passed
  the micro criteria.
* Run `python -m pbot progress` to see the current level, the record and the checklist.

The plan:

1. **Weeks 1+:** run `paper` at micro until `progress` shows every box ticked.
2. **First live session:** start `live` at micro. You risk $2 per trade and at most $6 per day.
3. **Earning the next level:** after 20+ live trades with a passing checklist, run
   `promote --mode live` to move to small. Repeat for each level.

Under the PDT guard (3 day trades per 5 sessions) it takes about 6–7 weeks to collect 20 trades.
That is slow, but it is real evidence. If Public confirms the PDT limit no longer applies to you,
set `risk.pdt_guard: false` and it goes faster.

## Setup

You need Python 3.10+ and a Public brokerage account with API access.

```bash
./pbot.sh setup        # macOS / Linux    (Windows: pbot.bat → 1)
```

1. In Public, open **Settings → API** and create a **secret key**.
2. Paste the key at the hidden prompt. Setup checks it, picks your brokerage account and saves
   both to `.env`, which is git-ignored and readable only by you.
3. Setup then runs `check`. It shows equity, buying power, live quotes for the watchlist, today's
   1-minute bars and the next session's schedule.

Never paste the secret key into a chat, email or ticket.

## Run it

```bash
./pbot.sh sim --days 10     # offline: synthetic market, fast clock, no key needed
./pbot.sh paper             # REAL Public quotes, SIMULATED fills (no orders sent), every trading day
./pbot.sh report            # today's trades;  --all for everything, --mode live for live
./pbot.sh progress          # size level + what's left before you can move up
```

To go live, set `live: {enabled: true}` in `pbot.yaml`, then run:

```bash
./pbot.sh live              # asks you to type START, then trades every session until stopped
./pbot.sh live --once       # one session only
./pbot.sh flatten           # emergency: sell whatever the bot opened, now
```

To run it unattended, start `./pbot.sh live --yes` from a scheduler (cron, launchd or Task
Scheduler) on a machine that stays awake. It sleeps until each pre-open. Set `PBOT_ALERT_WEBHOOK`
in `.env` to a Slack or Discord webhook to get entries, exits and halts on your phone.

## Tuning (`pbot.yaml`)

* **`watchlist`:** liquid names whose price fits `max_position_usd`. A $400 stock cannot be bought
  in whole shares under a $150 cap. Raise the cap or set `execution.fractional: true`. Check
  first that Public accepts fractional orders through the API for that symbol.
* **`ladder.*`:** sets the level sizes and promotion rules. While the ladder is on, the per-level
  limits replace the matching `risk.*` keys.
* **`strategy.*`:** range filters, VWAP requirement, stop placement (`mid` or `low`), target and
  breakeven multiples.
* **`session.*`:** window times and the holiday calendar. Add next year's NYSE holidays when they
  are published.

## Rules you need to know

* **Pattern day trading:** FINRA's $25k PDT rule was repealed. The SEC approved the change in
  April 2026 and it took effect June 4, 2026. Brokers can phase in the replacement
  intraday-margin rules, so `risk.pdt_guard` stays **on** by default. It caps the bot at 3 day
  trades per 5 business days under $25k equity. Turn it off only once Public confirms your
  account is no longer subject to PDT limits.
* **Cash accounts:** stock sales settle T+1. If you buy with unsettled sale proceeds and sell
  the same day, that is a good-faith violation. The bot sizes from cash-only buying power, but
  how often a cash account can trade is still limited by settlement.
* **Taxes:** frequent small trades mean short-term gains and possible wash sales. Public reports
  them on your 1099.

## Known limits

* Fills in `paper` and `sim` are optimistic: instant, at the quote. Live fills will be somewhat
  worse.
* `sim` uses synthetic prices. It tests the machinery, not whether the strategy has an edge.
* Data comes from Public's REST quote and bars endpoints polled every 2 s, not a tick stream.
  That is fine for this strategy but not for scalping.
* The strategy is long-only. Short selling is not implemented.

## Files

```
pbot/public_api.py   Public REST client (token auth, retries, quotes, bars, orders)
pbot/strategy.py     opening range, VWAP, entry/exit rules (pure functions)
pbot/risk.py         sizing, daily loss, trade caps, PDT guard
pbot/ladder.py       start-small size ladder (earned promotion, automatic demotion)
pbot/broker.py       live broker, paper broker, synthetic market
pbot/agent.py        the daily loop
pbot/journal.py      SQLite journal (data/pbot.db) + reports
tests/test_pbot.py   unit tests + mocked-HTTP Public API tests + full simulated days
```
