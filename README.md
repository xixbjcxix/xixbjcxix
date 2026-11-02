# adaptbot

An adaptive bot for Kalshi's 15-minute crypto markets (`KXBTC15M`, `KXETH15M`, `KXSOL15M`).
It was built from scratch around what the earlier bot's live run showed.

## What we learned from the old bot

| Observation (live run, 16 settled markets, −$5.42) | Design response |
|---|---|
| Completed YES+NO pairs earned ≤ $0.30; one-sided "residuals" won or lost $1.50–$5.20 and decided the day | No pair strategy, no residuals. Every position is a deliberate, sized bet with an exit plan |
| Resting bids were filled on the side that was losing value (adverse selection) | **Taker only.** No resting orders, so nothing can be picked off |
| A taker pair is impossible anyway: a NO bid at p *is* a YES ask at 1−p, so YES ask + NO ask ≥ $1 | (Same: the pair edge only existed for makers) |
| Internal positions drifted from Kalshi's (`ours 0, kalshi ±5`); the loss counter froze while money moved | Reconcile every 5 s; two mismatches → stop entries, adopt Kalshi's position and manage it. **Daily-loss stop uses Kalshi's cash + positions**, not the bot's own count |
| `target=None` for every market after the first | Strike is re-read once the market opens; no strike → no trade |
| Preflight "passed" without its test order | Live preflight must pass every check or nothing trades |
| `data_ok=True` during a network outage | Spot needs ≥ 2 fresh sources; book older than 4 s → no entries; spot gone for 30 s → exit open positions |
| Possible second instance | Lock file per data folder; own orders are tagged `adb-` |

## How it trades: "Early Conviction / Early Cut"

1. **Fair value.** Live BTC from Coinbase, Kraken and Bitstamp (median). A volatility estimate adapts to the market. The model computes P(the **60-second settlement average** ends above the strike). Using the averaging window makes it much sharper in the final minute than a plain close-price model.
2. **Early determination.** Enter only inside an early window, only when model probability − ask − fee ≥ a minimum edge, and only after that edge has held for a few seconds.
3. **Fractional Kelly sizing**, capped by your hard limits and shrunk as the day's loss approaches the stop.
4. **Early cut-off.** Exit as soon as the thesis breaks (probability falls a set amount below the entry price, or a trailing amount below its peak). Take profit when the bid pays more than fair value. Near the close, only hold positions the model is confident in.

## How it adapts

- **Every profile trades every market in shadow.** A profile is a parameter set: entry window, edge, cut rules. Shadow trades use simulated fills against the real order book. Because all profiles see the same data, this is a fair head-to-head, and testing an idea costs nothing.
- **The live account trades the profile with the best risk-adjusted record.** "Best" means the lower confidence bound of $/market, recent markets weighted more. A profile needs 8+ traded markets before it can be chosen.
- **If no profile shows an edge, the bot sits out.** It keeps learning in shadow while it waits.
- **Calibration.** After each market settles, the model's probabilities are checked against the outcome. If the model is over-confident, its edges are automatically shrunk.
- **Losing-streak cooldown.** After 4 losing markets in a row, it skips 2 markets.

The default profiles test your intuition directly. They differ mainly in **when** they commit and **how fast** they cut:
`early_cut`, `early_trail`, `early_hold`, `sniper`, `balanced`, `patient`, `late_favorite`.
To test an idea, from Grok or from you, add it as a profile in `data/settings.yaml` under `profiles:`. The dashboard will show how it does against the others.

## Use

Windows: double-click `login.bat` once to connect your Kalshi key (a window opens), then `start.bat`. It creates a Python environment on first run and opens the menu.
Elsewhere: `pip install -r requirements.txt` then `python -m adaptbot`.

```
1 Login (window)       2 Check keys / markets / price feeds
3 PAPER + dashboard    4 LIVE + dashboard
5 Backtest recordings  6 Backtest synthetic (plumbing test)   7 Record only
```

Recommended path:
0. **Login (1):** a window opens in your browser. Click *Use my kbot key* if it finds your old kbot install, or enter the key ID and choose the private-key file. **Test connection** shows your cash on each exchange shard. The key never leaves your PC.
1. **Check (2):** confirms keys, markets, order book and all three price feeds.
2. **PAPER (3) for at least a day.** It records every tick to `data/recordings/`, and the dashboard (http://127.0.0.1:8787) shows each profile's head-to-head results.
3. **Backtest (5):** `python -m adaptbot backtest --seed` replays your recordings. `--seed` saves the learning state so live trading starts warmed up.
4. **LIVE (4)** only once the dashboard shows a profile with a positive score over many markets. Until then, live trades 1 contract (warm-up) or sits out.

Stop: Ctrl-C, the dashboard **KILL** button (stops entries and sells everything), or create `data/KILL`.

## Settings (`data/settings.yaml`, created on first run)

Key limits (defaults): `bankroll 40`, `max_contracts_per_order 10`, `max_dollars_per_market 12`,
`max_dollars_total 30`, `max_daily_loss 8`, `kelly_fraction 0.25`, `warmup_contracts 1`.
See `settings.example.yaml` for everything.

## What has and hasn't been checked

The Kalshi API details were ported from the old `kbot`, which ran live against Kalshi on 2026-10-02:
- V2 order endpoint `/portfolio/events/orders`, with orders written on the YES book (the V1 order endpoints were retired in June 2026)
- fill prices and fees taken from `/portfolio/fills`
- cash read per exchange shard; crypto markets are on shard 2, and the bot offers to move cash there, as kbot did
- the fee multiplier read from the series settings

This code itself was built where Kalshi and the exchanges could not be reached, so run **Check** and **PAPER** before LIVE.

Note: kbot's `docs/RESULTS.md` profits came from **synthetic** data too. The only real evidence so far is the live day log, which lost $5.42 and had untracked positions. Synthetic backtests here also only prove the machinery works. Whether there is a real edge is decided by your recordings and paper results.

## Tests

`pip install -r requirements-dev.txt && python -m pytest -q`
