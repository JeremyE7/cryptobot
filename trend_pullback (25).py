from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from math import isfinite

from crypto_bot.indicators.core import IndicatorSnapshot, ema
from crypto_bot.market.binance import Candle


@dataclass(frozen=True, slots=True)
class HigherTimeframeTrend:
    close_time: int
    close: float
    ema50: float
    ema200: float
    ema50_slope: float

    @property
    def bullish(self) -> bool:
        return self.close > self.ema200 and self.ema50 > self.ema200 and self.ema50_slope > 0


class TrendSeries:
    def __init__(self, candles: list[Candle], slope_lookback: int = 4) -> None:
        closes = [c.close for c in candles]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        points: list[HigherTimeframeTrend] = []
        for i, candle in enumerate(candles):
            if e50[i] is None or e200[i] is None or i < slope_lookback:
                continue
            previous = e50[i - slope_lookback]
            if previous is None or previous <= 0:
                continue
            slope = float(e50[i]) / float(previous) - 1.0
            vals = (candle.close, float(e50[i]), float(e200[i]), slope)
            if all(isfinite(v) for v in vals):
                points.append(
                    HigherTimeframeTrend(
                        close_time=candle.close_time,
                        close=candle.close,
                        ema50=float(e50[i]),
                        ema200=float(e200[i]),
                        ema50_slope=slope,
                    )
                )
        self._points = points
        self._times = [p.close_time for p in points]

    def latest(self, available_at_ms: int) -> HigherTimeframeTrend | None:
        idx = bisect_right(self._times, available_at_ms) - 1
        return self._points[idx] if idx >= 0 else None


@dataclass(frozen=True, slots=True)
class PullbackCandidate:
    symbol: str
    atr14: float
    atr_pct: float
    ema_gap_pct: float
    momentum_3: float
    volume_ratio: float
    rsi14: float
    pullback_distance_pct: float
    one_hour_strength_pct: float
    btc_regime_strength_pct: float


@dataclass(frozen=True, slots=True)
class GateDecision:
    candidate: PullbackCandidate | None
    reason: str


@dataclass(frozen=True, slots=True)
class PullbackRules:
    rsi_min: float = 45.0
    rsi_max: float = 65.0
    max_momentum_3: float = 0.015       # +1.5% over 3 candles
    max_ema_gap_pct: float = 0.60       # percent
    min_atr_pct: float = 0.15           # percent
    max_atr_pct: float = 2.00           # percent
    pullback_tolerance_pct: float = 0.30  # previous close/low may be 0.30% above EMA21


class TrendPullbackStrategy:
    """Long-only trend/pullback entry logic.

    4H BTC decides whether risk is allowed at all. The traded symbol must be in
    a 1H uptrend. Entry on 15m requires an existing EMA stack plus a completed
    pullback/recovery rather than buying an already-extended momentum candle.
    """

    def __init__(self, rules: PullbackRules | None = None) -> None:
        self.rules = rules or PullbackRules()

    def evaluate(
        self,
        *,
        symbol: str,
        previous_candle: Candle,
        current_candle: Candle,
        previous_snapshot: IndicatorSnapshot,
        current_snapshot: IndicatorSnapshot,
        one_hour: HigherTimeframeTrend | None,
        btc_four_hour: HigherTimeframeTrend | None,
    ) -> GateDecision:
        if btc_four_hour is None or not btc_four_hour.bullish:
            return GateDecision(None, "BTC_REGIME")
        if one_hour is None or not one_hour.bullish:
            return GateDecision(None, "SYMBOL_1H")

        s = current_snapshot
        p = previous_snapshot
        if not (s.ema9 > s.ema21 > s.ema50):
            return GateDecision(None, "TREND_15M")
        if not (self.rules.rsi_min <= s.rsi14 <= self.rules.rsi_max):
            return GateDecision(None, "RSI")
        if not (self.rules.min_atr_pct <= s.atr_pct <= self.rules.max_atr_pct):
            return GateDecision(None, "ATR")

        ema_gap_pct = (s.ema9 / s.ema21 - 1.0) * 100.0 if s.ema21 else 999.0
        if ema_gap_pct > self.rules.max_ema_gap_pct:
            return GateDecision(None, "EXTENDED_EMA")
        if s.momentum_3 > self.rules.max_momentum_3:
            return GateDecision(None, "EXTENDED_MOMENTUM")

        tolerance = 1.0 + self.rules.pullback_tolerance_pct / 100.0
        touched_pullback = (
            previous_candle.low <= p.ema21 * tolerance
            or previous_candle.close <= p.ema21 * tolerance
            or current_candle.low <= s.ema21 * tolerance
        )
        recovered = current_candle.close > s.ema9 and current_candle.close >= previous_candle.close
        if not touched_pullback or not recovered:
            return GateDecision(None, "NO_PULLBACK_RECOVERY")

        pullback_distance = abs(previous_candle.low / p.ema21 - 1.0) * 100.0 if p.ema21 else 999.0
        one_hour_strength = (one_hour.ema50 / one_hour.ema200 - 1.0) * 100.0
        btc_strength = (btc_four_hour.ema50 / btc_four_hour.ema200 - 1.0) * 100.0
        return GateDecision(
            PullbackCandidate(
                symbol=symbol,
                atr14=s.atr14,
                atr_pct=s.atr_pct,
                ema_gap_pct=ema_gap_pct,
                momentum_3=s.momentum_3,
                volume_ratio=s.volume_ratio,
                rsi14=s.rsi14,
                pullback_distance_pct=pullback_distance,
                one_hour_strength_pct=one_hour_strength,
                btc_regime_strength_pct=btc_strength,
            ),
            "PASS",
        )

    @staticmethod
    def rank(candidate: PullbackCandidate) -> tuple[float, float, float, str]:
        # Prefer the least extended valid recovery, then stronger 1H trend.
        return (
            candidate.ema_gap_pct,
            candidate.momentum_3,
            -candidate.one_hour_strength_pct,
            candidate.symbol,
        )
