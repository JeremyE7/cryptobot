from __future__ import annotations

from crypto_bot.market.binance import Candle
from crypto_bot.storage.sqlite import CandleRepository


def make_candle(close: float = 105.0) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="15m",
        open_time=1_700_000_000_000,
        open=100.0,
        high=110.0,
        low=95.0,
        close=close,
        volume=12.5,
        close_time=1_700_000_899_999,
        quote_asset_volume=1300.0,
        number_of_trades=42,
        taker_buy_base_volume=6.0,
        taker_buy_quote_volume=630.0,
    )


def test_upsert_is_idempotent(tmp_path) -> None:
    db = tmp_path / "market.db"
    with CandleRepository(db) as repo:
        repo.upsert_many([make_candle(105.0)])
        repo.upsert_many([make_candle(106.0)])

        assert repo.count("BTCUSDT", "15m") == 1
        first, last = repo.first_last_open_time("BTCUSDT", "15m")
        assert first == 1_700_000_000_000
        assert last == first
