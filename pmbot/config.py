"""Endpoints and constants."""
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
BINANCE_REST = "https://data-api.binance.vision/api/v3"  # public market-data mirror (api.binance.com is geo-blocked here)

WS_CLOB_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RTDS = "wss://ws-live-data.polymarket.com"  # legacy RTDS, still serves crypto_prices_chainlink without auth
WS_BINANCE = "wss://data-stream.binance.vision/stream"

ASSETS = ["btc", "eth", "sol", "xrp"]
BINANCE_SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
CHAINLINK_SYMBOL = {"btc": "btc/usd", "eth": "eth/usd", "sol": "sol/usd", "xrp": "xrp/usd"}

WINDOW = 300          # 5-minute markets
TWAP_LOOKBACK = 60    # since 2026-08-14 5m markets resolve on a 60s Chainlink TWAP
CRYPTO_FEE_RATE = 0.07
CRYPTO_MAKER_REBATE_SHARE = 0.20
TAKER_DELAY_MS = 150  # crypto markets, since 2026-09-04
