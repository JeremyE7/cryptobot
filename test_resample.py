from crypto_bot.market.binance import Candle
from crypto_bot.market.resample import resample_candles


def c(i: int, price: float) -> Candle:
    step = 15 * 60 * 1000
    t = i * step
    return Candle("BTCUSDT", "15m", t, price, price + 1, price - 1, price + 0.5, 10, t + step - 1, 100, 1, 5, 50)


def test_resample_only_emits_complete_hour():
    rows = [c(i, 100 + i) for i in range(5)]
    out = resample_candles(rows, "1h")
    assert len(out) == 1
    assert out[0].open == 100
    assert out[0].close == 103.5
    assert out[0].high == 104
    assert out[0].low == 99
