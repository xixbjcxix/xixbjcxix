# Kalshi 15-minute crypto bot (BTC / ETH / SOL Up or Down)

Complete-set accumulation with a gated directional residual, for Kalshi's 15-minute
"Up or Down" series (`KXBTC15M`, `KXETH15M`, `KXSOL15M`).

**Built:**
* **Stage 1:** record live data, then backtest it.
* **Stage 2:** paper trading. Either simulated fills on live data, or **real orders on Kalshi's
  demo exchange**.

**Stage 3:** real-money trading on kalshi.com (`live`). It sits behind an explicit flag, a typed
START confirmation, a 1¢ test order and small starter limits. See "Stage 3" below.

> **Results so far:** see [`docs/RESULTS.md`](docs/RESULTS.md). They come from synthetic data,
> so they validate the machinery, not the edge.

---

## Get running in 5 minutes

You need Python 3.10+ and a Kalshi **demo** account.

### 1. Create a demo API key (fake money)

1. Go to **https://demo.kalshi.co** and create an account. It is separate from your real
   Kalshi login.
2. Open the profile menu → **Account & security** → **API Keys** → **Create new API key**.
3. Kalshi shows a **Key ID** and downloads a **private key file**. The key is shown only once,
   so keep the file.

*Optional:* create a production key the same way at kalshi.com. The bot uses it **read-only**
to record the real order books and the CF Benchmarks settlement index. Without it, recording
falls back to polling the public order books and a Coinbase price feed.

### 2. Run setup (guided; no files to edit)

```bash
./kbot.sh setup          # macOS / Linux
kbot setup               # Windows (kbot.bat)
```

The first run creates a virtual environment and installs requirements. Setup then:

* asks for the Key ID;
* asks for the private key. You can drag the downloaded file into the terminal, type its path,
  or type `paste` and paste it.
* checks the key loads, then saves it to `keys/kalshi-demo.pem` (owner-only permissions);
* writes the Key ID and file path to `.env`;
* runs `check`, which signs a request to Kalshi and prints your demo balance, your API tier,
  each series' fee settings, and the current markets with their target prices.

Prefer plain commands? `python -m pip install -r requirements.txt`, then `python -m kbot setup`.

Scripted version:
`python -m kbot setup --demo-key-id <ID> --demo-key-file ~/Downloads/xxx.key`

**Never paste your private key into a chat, email or ticket.** It only needs to exist on this
computer. If anything fails, share the error output, never the key.

### 3. Trade on demo, watch the dashboard

```bash
./kbot.sh demo           # real orders on demo.kalshi.co; open http://127.0.0.1:8787
```

The dashboard shows:
* each active market's target, the live index and time left;
* YES / NO / paired / residual inventory;
* open orders, live PnL (after fees) and risk-limit status;
* a **kill switch** button.

The **Settings** panel changes the main knobs while the bot runs: target pair cost, contracts
per order, cutoff, residual, risk limits and assets. Changes save to `data/settings.yaml`.

### Adding/removing tickers

`markets.assets` in `config.yaml` (or the Settings panel's Assets field) picks which of the
configured assets to actually trade - it ships as `[btc, eth]`. `kalshi.series` and
`spot.index_ids` list every asset the bot *knows how to trade*; it ships with `btc`, `eth` and
`sol` (Kalshi added 15-minute SOL Up/Down markets alongside BTC/ETH). To trade SOL, add it to
`markets.assets`:

```yaml
markets:
  assets: [btc, eth, sol]
```

or type `btc,eth,sol` into the dashboard's Assets field (it only accepts assets already listed
under `kalshi.series`, and applies on restart).

To add an asset Kalshi doesn't have wired in yet (XRP, DOGE, etc. don't have live 15-minute
series as of writing - `check` below tells you what it actually finds), add three lines:

```yaml
kalshi:
  series: {..., xrp: KXXRP15M}        # the Kalshi series ticker - verify it exists first
spot:
  index_ids: {..., xrp: XRPUSD_RTI}   # its CF Benchmarks settlement index
```

then add `xrp` to `markets.assets`. Everything else (fees, risk limits, the strategy, the
directional signal, live-mode safety checks) already applies to any asset in that list - none of
it is BTC/ETH-specific. Run `check` after adding an asset to confirm Kalshi actually lists open
markets under that series before trading it live.

Other commands:

| command | what it does |
|---|---|
| `check` | re-test keys, balance, series fees, current markets |
| `paper` | live production data, **simulated** fills (queue-aware), no orders sent |
| `record --hours 72` | stage 1 data capture: books, trades and the index into `data/recording.db` |
| `backtest --compare --jobs 2` | replay the recording through the strategy; prints the variant table |
| `replay --speed 30` | replay a recording through the dashboard (offline) |
| `synth --hours 24` | synthetic data for testing the pipeline (not evidence of edge) |
| `report [--day YYYY-MM-DD] [--mode demo]` | daily report from the trade database: net, paired/residual/cuts, fees, drawdown, risk events |
| `paper|demo|live ... --supervise` | auto-restart after an unexpected crash (never after a kill switch or bad key) |
| `live --i-understand-real-money` | **real-money** trading on kalshi.com (see Stage 3) |

Stop at any time with Ctrl-C. The bot cancels all orders on the way out. You can also trip the
kill switch with `touch data/KILL`.

---

## Strategy, fee and ops upgrades (v2)

**Strategy** (all in `strategy:` in `config.yaml`, all adjustable from the dashboard Settings panel):

| setting | what it does | default |
|---|---|---|
| `residual.min_edge` | A leg only rides unpaired if the signal agrees **and** model P(win) - leg cost (incl. fee) >= this. Direction agreement alone can mean "already priced in". | 0.02 |
| `vol_widen_per_unit` | When short-term volatility spikes above long-term (`signal.vol_short_lookback_s` vs `vol_lookback_s`), demand more edge: target -= this x (ratio - 1), capped by `max_vol_widen`. | 0 (off) |
| `adverse_skew_ticks` | When spot is clearly moving one way (`adverse_guard_z`), lower the bid on the side it is moving *away from* (the one informed flow hits). | 0 (off) |
| `max_legs_per_market` | Stop opening new two-sided quotes in a market after N one-sided episodes. | 0 (off) |

Only `min_edge` is on by default. The other three are **off because the synthetic data cannot validate
them**: the generator has no informed order flow, so every variant lands inside the noise. `backtest
--compare` now includes each of them, so run it on your own `record`ed data and keep only what beats base
by more than the printed `±`.

**Fee audit.** In `demo` and `live`, every real fill's fee (`fee_cost` from Kalshi) is compared with the fee
model. A single fill charged >2c more than modelled, or >25% over across 20+ fills, raises a `fee_drift` alert
and a dashboard flag ("fee model vs Kalshi"). Set `alerts.fee_drift_halt: true` to also trip the kill switch.
Undercharges are ignored (Kalshi rebates rounding over time, and some series charge no maker fee).

**Alerts.** Put a Slack/Discord webhook in `.env` as `KBOT_ALERT_WEBHOOK`, and/or `KBOT_TELEGRAM_TOKEN` +
`KBOT_TELEGRAM_CHAT`. Sent on: start/stop, kill switch, data gap, daily-loss halt, fee drift, a market settling
at or below `-alerts.big_loss_usd`, engine restarts, and a daily summary at the UTC rollover. Each kind is rate
limited (`alerts.min_interval_s`). With no secret set, alerts are only logged.

**Supervisor.** `./kbot.sh demo --supervise` restarts the engine after an unexpected crash (a background task
dying, an unhandled exception) with exponential backoff, up to `--max-restarts` (default 5) per hour. On every
exit all of the bot's orders are cancelled first. It never restarts after the kill switch, a failed preflight,
bad keys or Ctrl-C. Restarts are shown on the dashboard and sent as alerts. In `live` mode the typed START
confirmation still happens once, before the first run.

**Dashboard.** New: PnL-today equity curve, health panel (uptime, restarts, market-data age, data gaps, risk
rejects, fee audit) and the new strategy knobs in Settings.

---

## Troubleshooting

**Windows:** double-click `kbot.bat` for a menu (1 = setup, 3 = demo + dashboard). From
PowerShell type `.\kbot demo`; plain `kbot demo` only works in Command Prompt. Keep the bot folder
**outside OneDrive** (e.g. `C:\kalshibot`): OneDrive uploads your key files and can lock the
SQLite databases. After setup, delete the original downloaded key file; setup keeps its own copy
in `keys\`.

**The dashboard page won't load**
1. The dashboard only runs while `./kbot.sh demo` (or `paper`) is running. `setup`, `check` and
   `record` don't start it. Keep that terminal window open.
2. Look for the `Dashboard: http://127.0.0.1:....` line in the terminal and open exactly that
   address. If 8787 was busy, the bot picks the next free port.
3. If the terminal shows an error and returns to the prompt, the bot stopped. Send the last
   ~20 lines (never the key).
4. Kalshi problems (rejected key, no internet, wrong series) appear as a red banner at the top
   of the dashboard.

**"Kalshi rejected the API key"**: create the key on **demo.kalshi.co** for `demo`, not on
kalshi.com. Re-run `setup`. Make sure your computer clock is set automatically; signatures are
time-stamped.

**No markets listed**: the demo exchange may not list the 15-minute crypto series. Use
`./kbot.sh paper` (production data, simulated fills) meanwhile.

## How Kalshi's pieces are modelled (checked against docs.kalshi.com, 2026-09-30)

| topic | implementation |
|---|---|
| Auth | RSA-PSS (SHA-256, MGF1, salt = digest length) or Ed25519. Signs `timestamp_ms + METHOD + /trade-api/v2/path` (no query string). The websocket handshake signs `GET /trade-api/ws/v2`. |
| Hosts | prod `external-api.kalshi.com`, demo `external-api.demo.kalshi.co` (REST and WS). Separate keys per environment. |
| Orders | V2 `POST /portfolio/events/orders` (V1 order mutations were deprecated June 2026). Buy YES@p = `bid` p. Buy NO@q = `ask` (1−q). Resting quotes are `post_only`, GTC. Taker orders are IOC. Exits are `reduce_only`. Cancels use `DELETE /portfolio/events/orders/{id}`; cancel-all is `DELETE /portfolio/events/orders`. |
| Book | `orderbook_snapshot` / `orderbook_delta`: YES bids and NO bids only. YES ask = 1 − best NO bid. The bot builds a YES book and a mirror NO book. Sequence gaps force a resync. |
| Fills | `fill` websocket channel, deduped by `trade_id`, plus a `/portfolio/fills` poll every 5 s as backup. Positions are reconciled every 30 s; two mismatches in a row trip the kill switch. |
| Rate limits | Token buckets: write 100/s and read 200/s (Basic tier); order = 10 tokens, cancel = 2. 429s back off and retry. |
| Position limits | `kalshi.max_position_contracts` guard, on top of the $ limits. |
| Netting | Kalshi nets YES and NO in the same market. A completed pair turns straight back into $1 of collateral. PnL is identical; capital frees up sooner. |
| Settlement index | CF Benchmarks **BRTI** / **ETHUSD_RTI**, 60-second average over the final minute, compared with the market's `floor_strike`. The bot streams the index through Kalshi's `cfbenchmarks_value` channel when keys exist, else Coinbase/Kraken. Default cutoff is 60 s, so the bot is flat before the averaging minute starts. |

### Fees (modelled exactly, per series)

From Kalshi's fee schedule (effective 2026-07-07):

* **Taker fee** = round-up(M × **0.07** × C × P × (1−P)). That's 1.75¢/contract at 50¢, times multiplier M.
* **Maker fee** = round-up(M × **0.0175** × C × P × (1−P)), on series whose `fee_type` carries
  maker fees. `quadratic_with_combo_maker_fees` uses 0.035.

The schedule says most markets now charge maker fees, so the bot **assumes maker fees until
`GET /series` says otherwise**. It reads `fee_type` and `fee_multiplier` per series. An
unsupported `flat` type blocks trading.

Rounding: each fill is rounded **up to $0.01** (`fees.precision`). That is the conservative
case. Kalshi aligns direct members to $0.0001 and refunds over-rounding through an accumulator.
If that applies to you, set `precision: 0.0001`.

**`target_pair_cost` (0.94) is all-in.** Price of YES + price of NO + both fees must be ≤ 94¢.
A taker completion is taken only if leg cost + ask + taker fee still clears it.

---

## Strategy

1. **Flat:** rest post-only bids on YES and NO, priced off the book's fair value, so that
   YES + NO + maker fees ≤ `target_pair_cost`. Never more than one tick above the best bid;
   never crossing.
2. **One side fills (a leg):** stop adding to that side. Work the other side passively up to the
   all-in target. Take the other side's ask immediately if the pair still clears the target after
   the taker fee.
3. **Residual policy:** an unpaired leg may ride (capped by `residual.max_shares` / `max_usd`)
   only while the signal agrees. The signal reads the index vs. the target:
   * `z = ln(S/K) / (σ·√τ)` must be ≥ `min_z` in the leg's direction;
   * 30-second momentum must agree;
   * `p_model = Φ(z)` is shown on the dashboard.

   Otherwise the bot rescues: it raises the completion limit to `rescue_pair_cost`. In the last
   `flatten_s` before cutoff it exits the excess with a taker order, completing the pair or
   selling the leg, whichever loses less after fees.
4. **Hard stop:** in the final `cutoff_s` (60 s), cancel everything and place nothing.
5. **Rollover:** discovery polls the series every 15 s and subscribes to the next window before
   it opens.

## Where the edge comes from, and when it disappears

The edge is **getting paid for liquidity during short dislocations**. A bid a few ticks under
the best bid fills only when someone sells through the book: an impatient trader, a sweep, a
stale quote picked off. If both sides fill that way in the same window, you own a set below $1.

It disappears when:

* **Adverse selection dominates.** Your YES bid fills *because* BTC is dropping, so NO now costs
  more. The backtest reports how often you go one-sided, for how long, how each leg ended, and
  what legging cost. That is the number to watch.
* **Maker fees eat the discount.** At 45–50¢ the maker fee is ~0.44¢/contract per side, about
  0.9¢ per pair (more after cent rounding on small fills). A 94¢ target leaves ~5¢ after fees.
  Deeper targets fill less.
* **Queue competition.** Kalshi makers already sit at these prices, ahead of you in the queue.
* **Near expiry,** probabilities jump. The last minute is the settlement average, so the bot is
  flat by then.
* **Index basis.** Coinbase ≠ BRTI; use the Kalshi index channel (needs keys) for the signal.
* **Capacity.** Edge per set is cents; limits are small by default and should only grow after
  evidence.

---

## Risk controls (every order passes them)

| control | config | behaviour |
|---|---|---|
| $ per order | `risk.max_order_usd` | reject |
| $ per market | `risk.max_market_usd` | cost basis + open bids |
| unpaired residual | `risk.max_residual_usd`, `max_residual_shares` | worst case: all open bids on one side fill |
| total $ | `risk.max_total_usd` | across markets |
| daily loss | `risk.max_daily_loss_usd` | realized + mark-to-market; halts until next UTC day |
| order rate | `risk.max_orders_per_min` + token buckets | reject / wait |
| Kalshi position limit | `kalshi.max_position_contracts` | reject |
| kill switch | dashboard button, `data/KILL`, Ctrl-C, auth errors, position mismatch | cancel the bot's orders at Kalshi; sticky |
| data gaps | WS disconnect, sequence gap, silence > `pm_stale_ms`, stale index | cancel the bot's orders at Kalshi; resume after fresh snapshot |
| rejections / partial fills | V2 errors mark the order rejected; partial fills tracked per fill | backoff before re-quoting |

## Data (SQLite)

* `data/recording.db`: every Kalshi frame, index tick, heartbeat and disconnect, plus the
  markets table with target, fee type and result.
* `data/kbot.db`:
  * `runs`
  * `quotes` (top of book and our quotes)
  * `orders`, `order_events`
  * `fills` (price, size, maker/taker, **fee**)
  * `pnl_events`
  * `risk_events` (gaps, kills, settings changes)

## Config reference

Everything is in `config.yaml`, with comments. The dashboard overlay (`data/settings.yaml`)
wins over it. Unknown keys are rejected. Main knobs:

| key | default | meaning |
|---|---|---|
| `strategy.target_pair_cost` | 0.94 | all-in cost per YES+NO set |
| `strategy.clip_shares` | 10 | contracts per order |
| `strategy.cutoff_s` | {900: 60} | hard stop before close |
| `strategy.rescue_pair_cost` | 0.99 | how much edge to give up to pair a leg when rescuing |
| `strategy.residual.*` | on, 20, $8, ride | directional residual when the signal agrees |
| `signal.min_z`, `min_momentum_bps` | 0.6, 2 | signal thresholds |
| `fees.precision` | 0.01 | per-fill fee rounding |
| `markets.data_env` | prod | where `record`/`paper` read market data |
| `spot.source` | kalshi_index | settlement index via Kalshi, or `coinbase` / `kraken` |

## Tests

`python -m unittest discover -s tests`: 31 tests, no network needed. They cover:
* signing (RSA-PSS and Ed25519);
* V2 order mapping;
* YES/NO book mirroring;
* fees and rounding;
* the queue-aware fill model;
* the fee-inclusive strategy;
* the demo order manager (placing, fills, dedupe, cancels, auth-error kill, production refusal);
* setup (key saved with 600 permissions, `.env` written, bad keys rejected);
* dashboard settings;
* an end-to-end demo-engine run against a fake Kalshi.

## Stage 3: live trading (REAL MONEY)

Start it from the `kbot.bat` menu (option 6), or run
`python -m kbot live --i-understand-real-money`. You need your **kalshi.com** key entered as the
*production* key in setup.

Before any order, the bot:
1. shows the markets and limits, and waits for you to type **START**;
2. signs in and reads your balance. A rejected key stops here, with an explanation on the
   dashboard;
3. cancels resting orders left by an earlier bot session. Only its own: client order IDs starting
   `kbot-`, in `KXBTC15M` / `KXETH15M`. Your manual orders are never touched;
4. reads your positions. **Markets where you already hold contracts are skipped.** Reconciliation
   ignores them, so your own trades don't trip the mismatch kill;
5. places **one 1-contract buy-YES order at 1¢** (post-only) and cancels it, proving signing,
   order entry and cancel work. If that fails, it doesn't trade.

**Starter limits** (`live.use_starter_limits: true`) apply on top of your settings:
* 5 contracts per order
* $5 per order, $20 per market, $60 total
* $5 / 10 contracts unpaired per market
* $15 daily loss stop

Raise them in `config.yaml` → `live:` once you trust the numbers, or set `use_starter_limits:
false` to use the `risk:` section and dashboard settings as-is.

The dashboard header shows **LIVE · REAL MONEY**. Stop the bot with Ctrl-C in its window, the
Kill switch, or by creating `data\KILL`; each cancels the bot's orders at Kalshi. Positions
already filled stay open and settle normally at the window's close (15 minutes at most).

**Results so far come from simulated data only. You can lose money.** Kalshi is a CFTC-regulated
exchange and is generally available in California. This is not financial advice.
