from crypto_bot.indicators.core import IndicatorSnapshot
from crypto_bot.market.binance import Candle
from crypto_bot.strategy.trend_pullback import HigherTimeframeTrend, TrendPullbackStrategy


def candle(t, o, h, l, c):
    return Candle("BNBUSDT", "15m", t, o, h, l, c, 100, t+899999, 0, 1, 0, 0)


def snap(e9, e21, e50, rsi=55, vol=1.2, mom=0.004, atr=0.5):
    close = e9
    return IndicatorSnapshot(e9, e21, e50, rsi, vol, mom, atr, atr / close * 100)


def test_pullback_requires_bull_regime_and_recovery():
    strategy = TrendPullbackStrategy()
    prev = candle(0, 100, 101, 99.9, 100.1)
    cur = candle(900000, 100.1, 101, 100, 100.8)
    ps = snap(100.4, 100.0, 99.5)
    cs = snap(100.6, 100.2, 99.7)
    bull = HigherTimeframeTrend(0, 110, 105, 100, 0.01)
    decision = strategy.evaluate(
        symbol="BNBUSDT", previous_candle=prev, current_candle=cur,
        previous_snapshot=ps, current_snapshot=cs, one_hour=bull, btc_four_hour=bull,
    )
    assert decision.reason == "PASS"
    assert decision.candidate is not None

    bear = HigherTimeframeTrend(0, 90, 95, 100, -0.01)
    rejected = strategy.evaluate(
        symbol="BNBUSDT", previous_candle=prev, current_candle=cur,
        previous_snapshot=ps, current_snapshot=cs, one_hour=bull, btc_four_hour=bear,
    )
    assert rejected.reason == "BTC_REGIME"
