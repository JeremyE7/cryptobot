from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    ema9: float
    ema21: float
    ema50: float
    rsi14: float
    volume_ratio: float
    momentum_3: float
    atr14: float
    atr_pct: float


def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be > 0")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    multiplier = 2 / (period + 1)
    previous = seed

    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        result[i] = previous

    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be > 0")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[i] = calc(avg_gain, avg_loss)

    return result


def rolling_volume_ratio(values: list[float], period: int = 20) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be > 0")

    for i in range(period, len(values)):
        avg = sum(values[i - period:i]) / period
        result[i] = values[i] / avg if avg > 0 else 1.0
    return result


def momentum(values: list[float], lookback: int = 3) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if lookback <= 0:
        raise ValueError("lookback must be > 0")

    for i in range(lookback, len(values)):
        previous = values[i - lookback]
        result[i] = (values[i] / previous - 1.0) if previous > 0 else 0.0
    return result


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """Wilder Average True Range."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal length")

    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result

    true_ranges: list[float] = [0.0] * len(closes)
    true_ranges[0] = highs[0] - lows[0]
    for i in range(1, len(closes)):
        true_ranges[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    current = sum(true_ranges[1 : period + 1]) / period
    result[period] = current
    for i in range(period + 1, len(closes)):
        current = ((current * (period - 1)) + true_ranges[i]) / period
        result[i] = current
    return result


def build_snapshots(
    closes: list[float],
    volumes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> list[IndicatorSnapshot | None]:
    if len(closes) != len(volumes):
        raise ValueError("closes and volumes must have equal length")
    if highs is None:
        highs = list(closes)
    if lows is None:
        lows = list(closes)
    if not (len(closes) == len(highs) == len(lows)):
        raise ValueError("closes, highs and lows must have equal length")

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    rs = rsi(closes, 14)
    vr = rolling_volume_ratio(volumes, 20)
    mom = momentum(closes, 3)
    atr_values = atr(highs, lows, closes, 14)

    snapshots: list[IndicatorSnapshot | None] = []
    for i in range(len(closes)):
        parts = (e9[i], e21[i], e50[i], rs[i], vr[i], mom[i], atr_values[i])
        if any(value is None for value in parts):
            snapshots.append(None)
            continue
        numeric = tuple(float(value) for value in parts)  # type: ignore[arg-type]
        if not all(isfinite(value) for value in numeric):
            snapshots.append(None)
            continue
        atr_value = numeric[6]
        atr_pct = (atr_value / closes[i] * 100.0) if closes[i] > 0 else 0.0
        snapshots.append(IndicatorSnapshot(*numeric, atr_pct))

    return snapshots
