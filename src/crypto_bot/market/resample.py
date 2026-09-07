from __future__ import annotations

from collections import defaultdict

from crypto_bot.market.binance import Candle

_INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def resample_candles(candles: list[Candle], target_interval: str) -> list[Candle]:
    """Resample aligned 15m candles to complete UTC higher-timeframe candles.

    Only complete buckets are emitted. This prevents higher-timeframe indicators
    from seeing partial candles and keeps strategy decisions look-ahead safe.
    """
    if target_interval not in {"1h", "4h", "1d"}:
        raise ValueError("target_interval must be 1h, 4h or 1d")
    if not candles:
        return []
    if any(c.interval != "15m" for c in candles):
        raise ValueError("resample_candles expects 15m source candles")

    bucket_ms = _INTERVAL_MS[target_interval]
    expected = bucket_ms // _INTERVAL_MS["15m"]
    groups: dict[int, list[Candle]] = defaultdict(list)
    for candle in sorted(candles, key=lambda c: c.open_time):
        bucket = candle.open_time - (candle.open_time % bucket_ms)
        groups[bucket].append(candle)

    result: list[Candle] = []
    for bucket, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda c: c.open_time)
        expected_times = [bucket + i * _INTERVAL_MS["15m"] for i in range(expected)]
        if len(rows) != expected or [c.open_time for c in rows] != expected_times:
            continue
        first, last = rows[0], rows[-1]
        result.append(
            Candle(
                symbol=first.symbol,
                interval=target_interval,
                open_time=bucket,
                open=first.open,
                high=max(c.high for c in rows),
                low=min(c.low for c in rows),
                close=last.close,
                volume=sum(c.volume for c in rows),
                close_time=last.close_time,
                quote_asset_volume=sum(c.quote_asset_volume for c in rows),
                number_of_trades=sum(c.number_of_trades for c in rows),
                taker_buy_base_volume=sum(c.taker_buy_base_volume for c in rows),
                taker_buy_quote_volume=sum(c.taker_buy_quote_volume for c in rows),
            )
        )
    return result
