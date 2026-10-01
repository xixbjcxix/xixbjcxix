"""kbot: complete-set accumulation bot for Kalshi 15-minute crypto Up/Down markets (BTC, ETH).

Stages:
  1. record + backtest (replay recorded books through a queue-aware fill model)
  2. paper trading: simulated fills on live data, or real orders on Kalshi's demo environment
  3. live trading on kalshi.com (real money; explicit flag + typed START + 1c test order)
"""

__version__ = "0.1.0"
