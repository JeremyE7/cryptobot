from __future__ import annotations

from dataclasses import dataclass

from crypto_bot.indicators.core import IndicatorSnapshot


@dataclass(frozen=True, slots=True)
class Signal:
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    total: float
    trend: float
    momentum: float
    volume: float
    rsi: float
    atr: float


class MomentumScoreStrategy:
    """Transparent momentum strategy + continuous market-quality assessment.

    The classic integer score remains the coarse entry gate. The quality score
    preserves more information so two classic score=100 signals no longer look
    identical to the selector.
    """

    def score(self, indicators: IndicatorSnapshot) -> Signal:
        score = 0
        reasons: list[str] = []

        if indicators.ema9 > indicators.ema21:
            score += 25
            reasons.append("EMA9 > EMA21")

        if indicators.ema21 > indicators.ema50:
            score += 20
            reasons.append("EMA21 > EMA50")

        if 45 <= indicators.rsi14 <= 65:
            score += 20
            reasons.append("RSI 45-65")
        elif 40 <= indicators.rsi14 < 45 or 65 < indicators.rsi14 <= 70:
            score += 10
            reasons.append("RSI acceptable")

        if indicators.volume_ratio >= 1.20:
            score += 20
            reasons.append("volume >= 1.2x")
        elif indicators.volume_ratio >= 1.00:
            score += 10
            reasons.append("volume >= average")

        if indicators.momentum_3 > 0.003:
            score += 15
            reasons.append("3-candle momentum > 0.3%")
        elif indicators.momentum_3 > 0:
            score += 8
            reasons.append("positive momentum")

        return Signal(score=min(score, 100), reasons=tuple(reasons))

    def quality(self, indicators: IndicatorSnapshot) -> QualityAssessment:
        """Return a continuous 0..100 quality score.

        The normalizers are intentionally broad and capped. They are not fitted
        to the current backtest window; they simply turn raw indicator strength
        into comparable dimensions.
        """
        ema9_21 = self._positive_ratio(indicators.ema9, indicators.ema21)
        ema21_50 = self._positive_ratio(indicators.ema21, indicators.ema50)

        # 25 points total: short trend saturates at +0.50%, medium at +1.00%.
        trend = 12.5 * self._clamp01(ema9_21 / 0.005) + 12.5 * self._clamp01(ema21_50 / 0.010)

        # 25 points: 3-candle momentum saturates at +1.50%.
        momentum = 25.0 * self._clamp01(indicators.momentum_3 / 0.015)

        # 20 points: no reward below average volume; saturates at 3x average.
        volume = 20.0 * self._clamp01((indicators.volume_ratio - 1.0) / 2.0)

        # 15 points: ideal momentum RSI is around 55; linearly fades toward 40/70.
        rsi_distance = abs(indicators.rsi14 - 55.0)
        rsi = 15.0 * self._clamp01(1.0 - rsi_distance / 15.0)

        # 15 points: enough intraday movement to make a 2%/4% system tradable.
        # Saturates at ATR=1.0% of price; values below 0.2% receive no reward.
        atr = 15.0 * self._clamp01((indicators.atr_pct - 0.20) / 0.80)

        total = trend + momentum + volume + rsi + atr
        return QualityAssessment(
            total=round(self._clamp(total, 0.0, 100.0), 4),
            trend=trend,
            momentum=momentum,
            volume=volume,
            rsi=rsi,
            atr=atr,
        )

    @staticmethod
    def _positive_ratio(a: float, b: float) -> float:
        if b <= 0:
            return 0.0
        return max(0.0, a / b - 1.0)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @classmethod
    def _clamp01(cls, value: float) -> float:
        return cls._clamp(value, 0.0, 1.0)
