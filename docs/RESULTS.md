# Stage 1–2 results (review before stage 3)

**What these numbers are:** 48 hours of **synthetic** Kalshi data (384 windows, BTC and ETH,
15 minutes each), replayed through the real backtest pipeline. That means the same Kalshi
parser, fee model, strategy, risk manager and queue-aware fill model that the paper and demo
modes use. Kalshi fees apply, with maker fees on and cent rounding. Config is the default:
94¢ all-in target, 10-contract clips, $40 per market, flat 60 s before close.

**What they are not:** evidence of an edge. The build environment cannot reach Kalshi, and I
wrote the synthetic generator. In this synthetic world the average resting-bid fill is followed
by a *favorable* move (+0.9¢ after 10 s). On the Polymarket version of the same world model
that number was about zero. That mean reversion is the main reason this run is profitable.
Real books decide whether it exists. Next steps:
1. `./kbot.sh record --hours 72`, then `./kbot.sh backtest --compare --jobs 2`.
2. A few days of `./kbot.sh demo`.

Full numbers: `synthetic-48h-compare.json`.

## Headline: base config (daily-loss halt lifted so the full 48 h runs)

| metric | value |
|---|---|
| fills | 3,492 (3,125 resting / 367 taker), 73 per hour, all 384 windows traded |
| avg pair cost (before fees) | 0.904, so **9.6¢ edge per pair** |
| fees | $129.91: maker $112.02, taker $17.89. That's **1.2¢ per pair**, leaving **8.4¢ after fees** |
| capital | 98.1% in paired sets, 1.9% residual at expiry |
| paired PnL | +$1,027.29 on 10,708 pairs |
| residual held to expiry | −$190.94 |
| cuts (taker exits of legs) | −$519.82 |
| **net PnL** | **+$186.62 ± ~$59** (1σ); about $0.49 per window |
| max drawdown | −$27.81 |

## One-sided legs (adverse selection)

| metric | value |
|---|---|
| legs (times holding one side) | 1,560 |
| completed into pairs | 85.5% (1,208 passively, 126 by taking the other side) |
| cut before cutoff | 169 |
| still open at expiry | 55; 14% of windows settled with a residual |
| time one-sided | median 29 s, p90 557 s; 37% one-sided > 60 s |
| win rate of legs held to expiry | 0%, by selection: winning legs get paired cheaply, losers remain |
| total cost of legging | $719 (residual losses + cuts + extra paid to pair) |

## With vs without the directional residual, and other variants

| variant | net | ± noise | fees | pair edge |
|---|---:|---:|---:|---:|
| base, daily-loss halt ON (as configured) | +178.13 | 55 | 111.74 | 9.62¢ |
| **base (residual rides while signal agrees)** | **+186.62** | 59 | 129.91 | 9.59¢ |
| residual allowed but still completed at target | +101.18 | 54 | 127.78 | 8.87¢ |
| no residual (always pair or cut) | +59.39 | 41 | 154.12 | 5.33¢ |
| hold legs to expiry (never cut) | +206.72 | 64 | 127.73 | 9.69¢ |
| series **without** maker fees | +269.83 | 57 | 19.09 | 8.13¢ |
| target 0.92 | +114.50 | 60 | 102.59 | 11.69¢ |
| target 0.96 | +157.58 | 52 | 146.15 | 6.65¢ |

How to read it:

* **Residual on vs off:** letting an agreed leg ride beat forcing every leg to pair by about $127
  over 48 h (≈2σ). Forcing pairs pushes completions up to the 0.99 rescue cost, which cuts the
  edge from 9.6¢ to 5.3¢.
* **Maker fees matter:** they cost about $83 over 48 h, roughly a third of the net. `check`
  shows each series' actual `fee_type`. If the 15-minute crypto series carry no maker fees,
  results improve accordingly.
* **Never cutting** came out slightly ahead here, but with a bigger residual loss (−$716) and a
  worse tail. Within noise of base.
* **Targets:** 0.92 fills less; 0.96 keeps less per pair. 0.94 is a reasonable middle, but the
  differences are within noise.
* **Daily-loss halt ($25):** it tripped once in 48 h.

## Stage 2 verification (offline, since this environment can't reach Kalshi)

* **End-to-end demo engine against a fake Kalshi** (test `test_engine_wiring`):
  * discovery → websocket subscribe → books and index;
  * post-only V2 orders on the order path;
  * fills from the private channel;
  * pairs formed and fees booked;
  * a disconnect → cancel-all at the exchange → resync;
  * cancel-all on start and shutdown;
  * position reconciliation passed with no false kill.
* **Order manager:** refuses production hosts, dedupes fills, handles cancels, trips the kill
  switch on 401/403.
* **Signing:** verified against RSA-PSS and Ed25519 public keys.
* **Setup wizard:** stores the key with 600 permissions, writes `.env`, rejects a malformed key.
* **Dashboard:** target, index, time left, signal (z, p), inventory and fees shown on replay.
  Settings save and validate (an out-of-range target is refused). The kill switch is
  token-protected.

The first real connection will be `./kbot.sh setup` on your machine. It ends with a signed
request to Kalshi's demo, so any auth or series-ticker problem shows up there with a clear
message.
