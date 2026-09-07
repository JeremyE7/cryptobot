from crypto_bot.indicators import IndicatorSnapshot
from crypto_bot.strategy import MomentumScoreStrategy


def snapshot(*, volume: float, momentum: float, ema9: float = 102.0, atr_pct: float = 0.8) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ema9=ema9,
        ema21=101.0,
        ema50=99.0,
        rsi14=55.0,
        volume_ratio=volume,
        momentum_3=momentum,
        atr14=0.8,
        atr_pct=atr_pct,
    )


def test_quality_is_continuous_and_rewards_stronger_signal() -> None:
    strategy = MomentumScoreStrategy()
    weak = strategy.quality(snapshot(volume=1.2, momentum=0.0031, ema9=101.2, atr_pct=0.3))
    strong = strategy.quality(snapshot(volume=2.5, momentum=0.012, ema9=102.0, atr_pct=0.9))

    assert 0 <= weak.total <= 100
    assert 0 <= strong.total <= 100
    assert strong.total > weak.total
    assert strong.volume > weak.volume
    assert strong.momentum > weak.momentum


def test_classic_score_can_tie_while_quality_differs() -> None:
    strategy = MomentumScoreStrategy()
    a = snapshot(volume=1.21, momentum=0.0031, ema9=101.2, atr_pct=0.3)
    b = snapshot(volume=2.4, momentum=0.010, ema9=102.0, atr_pct=0.9)

    assert strategy.score(a).score == 100
    assert strategy.score(b).score == 100
    assert strategy.quality(b).total > strategy.quality(a).total
