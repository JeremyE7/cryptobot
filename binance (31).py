from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

import httpx

from crypto_bot.config import DEFAULT_BASE_URL, SUPPORTED_INTERVALS


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_asset_volume: float
    number_of_trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float

    @classmethod
    def from_binance(cls, symbol: str, interval: str, row: Sequence[object]) -> "Candle":
        if len(row) < 11:
            raise ValueError(f"Unexpected Binance kline row length: {len(row)}")

        return cls(
            symbol=symbol.upper(),
            interval=interval,
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
            quote_asset_volume=float(row[7]),
            number_of_trades=int(row[8]),
            taker_buy_base_volume=float(row[9]),
            taker_buy_quote_volume=float(row[10]),
        )


class BinanceMarketDataClient:
    """Small client for Binance public Spot market data.

    No API key is required. The default base URL is Binance's market-data-only
    endpoint so this project never needs trading credentials during simulation.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BinanceMarketDataClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        params: dict[str, str | int] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        response = self._client.get("/api/v3/klines", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"Unexpected Binance response: {payload!r}")

        return [Candle.from_binance(symbol, interval, row) for row in payload]

    def iter_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int | None = None,
        page_size: int = 1000,
    ) -> Iterator[list[Candle]]:
        """Yield chronological pages, advancing from each last candle open time.

        Binance treats startTime as inclusive, so the next request starts at
        last_open_time + 1ms to avoid returning the same candle twice.
        """
        cursor = start_time_ms

        while True:
            page = self.get_klines(
                symbol,
                interval,
                start_time_ms=cursor,
                end_time_ms=end_time_ms,
                limit=page_size,
            )
            if not page:
                break

            yield page

            next_cursor = page[-1].open_time + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance pagination did not advance")

            cursor = next_cursor

            if len(page) < page_size:
                break
            if end_time_ms is not None and cursor > end_time_ms:
                break
