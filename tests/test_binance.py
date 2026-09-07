from __future__ import annotations

import httpx

from crypto_bot.market.binance import BinanceMarketDataClient


def test_get_klines_maps_binance_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/klines"
        return httpx.Response(
            200,
            json=[
                [
                    1_700_000_000_000,
                    "100.0",
                    "110.0",
                    "95.0",
                    "105.0",
                    "12.5",
                    1_700_000_899_999,
                    "1300.0",
                    42,
                    "6.0",
                    "630.0",
                    "0",
                ]
            ],
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://example.test", transport=transport)
    client = BinanceMarketDataClient(client=http_client)

    candles = client.get_klines("btcusdt", "15m", start_time_ms=1, limit=1000)

    assert len(candles) == 1
    assert candles[0].symbol == "BTCUSDT"
    assert candles[0].close == 105.0
    assert candles[0].number_of_trades == 42
