from __future__ import annotations

DEFAULT_BASE_URL = "https://data-api.binance.vision"
DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "LINKUSDT",
)
DEFAULT_INTERVAL = "15m"
DEFAULT_DB_PATH = "data/market.db"

SUPPORTED_INTERVALS = {
    "1s",
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}

RESEARCH_SYMBOLS = ("BNBUSDT", "ETHUSDT", "LINKUSDT")
