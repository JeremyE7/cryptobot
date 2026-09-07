from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from crypto_bot.backtest import BacktestEngine, BacktestResult, ClosedTrade, FeatureFilter
from crypto_bot.market.binance import Candle


@dataclass(frozen=True, slots=True)
class FeatureComparison:
    feature: str
    wins: int
    losses: int
    winner_mean: float
    loser_mean: float
    winner_median: float
    loser_median: float
    relative_gap_pct: float


@dataclass(frozen=True, slots=True)
class FilterExperiment:
    name: str
    feature_filter: FeatureFilter
    train_return: float
    train_pf: float | None
    train_drawdown: float
    train_trades: int
    test_return: float
    test_pf: float | None
    test_drawdown: float
    test_trades: int

    @property
    def train_profitable(self) -> bool:
        return self.train_return > 0 and (self.train_pf or 0) > 1

    @property
    def test_profitable(self) -> bool:
        return self.test_return > 0 and (self.test_pf or 0) > 1


@dataclass(frozen=True, slots=True)
class ResearchReport:
    split_time: int
    train_ratio: float
    baseline_train: BacktestResult
    baseline_test: BacktestResult
    feature_comparisons: tuple[FeatureComparison, ...]
    experiments: tuple[FilterExperiment, ...]

    @property
    def robust_candidates(self) -> tuple[FilterExperiment, ...]:
        baseline_pf = self.baseline_test.profit_factor or 0.0
        return tuple(
            item
            for item in self.experiments
            if item.train_profitable
            and item.test_profitable
            and item.test_return > self.baseline_test.return_pct
            and (item.test_pf or 0.0) >= baseline_pf
        )


FEATURES: dict[str, callable] = {
    "volume_ratio": lambda t: t.volume_ratio,
    "momentum_3": lambda t: t.momentum_3,
    "ema_gap_pct": lambda t: t.ema_gap_pct,
    "rsi14": lambda t: t.rsi14,
    "atr_pct": lambda t: t.atr_pct,
}


def _percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def compare_winners_losers(trades: Iterable[ClosedTrade]) -> tuple[FeatureComparison, ...]:
    trades = tuple(trades)
    winners = tuple(t for t in trades if t.pnl > 0)
    losers = tuple(t for t in trades if t.pnl < 0)
    rows: list[FeatureComparison] = []
    if not winners or not losers:
        return ()

    for name, getter in FEATURES.items():
        win_values = [float(getter(t)) for t in winners]
        loss_values = [float(getter(t)) for t in losers]
        wmean = mean(win_values)
        lmean = mean(loss_values)
        denom = abs(lmean) if abs(lmean) > 1e-12 else 1.0
        rows.append(
            FeatureComparison(
                feature=name,
                wins=len(win_values),
                losses=len(loss_values),
                winner_mean=wmean,
                loser_mean=lmean,
                winner_median=median(win_values),
                loser_median=median(loss_values),
                relative_gap_pct=(wmean - lmean) / denom * 100.0,
            )
        )
    return tuple(rows)


def _filter_kwargs(feature: str, *, minimum: float | None = None, maximum: float | None = None) -> dict[str, float]:
    mapping = {
        "volume_ratio": ("min_volume_ratio", "max_volume_ratio"),
        "momentum_3": ("min_momentum_3", "max_momentum_3"),
        "ema_gap_pct": ("min_ema_gap_pct", "max_ema_gap_pct"),
        "rsi14": ("min_rsi14", "max_rsi14"),
        "atr_pct": ("min_atr_pct", "max_atr_pct"),
    }
    lo_name, hi_name = mapping[feature]
    result: dict[str, float] = {}
    if minimum is not None:
        result[lo_name] = minimum
    if maximum is not None:
        result[hi_name] = maximum
    return result


def generate_single_feature_filters(trades: Iterable[ClosedTrade]) -> tuple[FeatureFilter, ...]:
    trades = tuple(trades)
    filters: list[FeatureFilter] = []
    seen: set[tuple] = set()

    for feature, getter in FEATURES.items():
        values = [float(getter(t)) for t in trades]
        if len(values) < 8:
            continue
        for q in (0.20, 0.35, 0.50, 0.65, 0.80):
            threshold = _percentile(values, q)
            for side in ("max", "min"):
                kwargs = _filter_kwargs(
                    feature,
                    minimum=threshold if side == "min" else None,
                    maximum=threshold if side == "max" else None,
                )
                rounded = round(threshold, 10)
                key = (feature, side, rounded)
                if key in seen:
                    continue
                seen.add(key)
                filters.append(
                    FeatureFilter(
                        name=f"{feature} {side} q{int(q*100)} ({threshold:.6g})",
                        **kwargs,
                    )
                )

    # RSI commonly behaves better as a band than a one-sided threshold.
    rsi_values = [t.rsi14 for t in trades]
    if len(rsi_values) >= 8:
        for low_q, high_q in ((0.10, 0.90), (0.20, 0.80), (0.30, 0.70)):
            low = _percentile(rsi_values, low_q)
            high = _percentile(rsi_values, high_q)
            filters.append(
                FeatureFilter(
                    name=f"rsi14 band q{int(low_q*100)}-q{int(high_q*100)} ({low:.2f}-{high:.2f})",
                    min_rsi14=low,
                    max_rsi14=high,
                )
            )
    return tuple(filters)


def _common_times(candles_by_symbol: dict[str, list[Candle]]) -> list[int]:
    valid = [set(c.open_time for c in candles) for candles in candles_by_symbol.values() if len(candles) >= 60]
    if not valid:
        raise ValueError("No symbols have enough candle data")
    times = sorted(set.intersection(*valid))
    if len(times) < 120:
        raise ValueError("Research mode needs at least 120 aligned candles")
    return times


def _truncate(candles_by_symbol: dict[str, list[Candle]], end_time: int) -> dict[str, list[Candle]]:
    return {
        symbol: [c for c in candles if c.open_time <= end_time]
        for symbol, candles in candles_by_symbol.items()
    }


def _engine(
    *,
    initial_cash: float,
    trade_percent: float,
    min_score: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    fee_rate: float,
    slippage_rate: float,
    feature_filter: FeatureFilter | None = None,
    trade_start_time: int | None = None,
) -> BacktestEngine:
    return BacktestEngine(
        initial_cash=initial_cash,
        trade_size=None,
        trade_percent=trade_percent,
        min_score=min_score,
        min_quality=0.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        cooldown_candles=0,
        feature_filter=feature_filter,
        trade_start_time=trade_start_time,
        # v1 research deliberately does NOT use the failed v0.5 quality gate.
    )


def run_research(
    candles_by_symbol: dict[str, list[Candle]],
    *,
    initial_cash: float = 10.0,
    trade_percent: float = 50.0,
    min_score: int = 100,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.04,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    train_ratio: float = 0.70,
    top_n: int = 8,
) -> ResearchReport:
    if not 0.50 <= train_ratio <= 0.90:
        raise ValueError("train_ratio must be between 0.50 and 0.90")
    if top_n <= 0:
        raise ValueError("top_n must be > 0")

    times = _common_times(candles_by_symbol)
    split_idx = max(60, min(len(times) - 2, int(len(times) * train_ratio)))
    split_time = times[split_idx]
    train_data = _truncate(candles_by_symbol, split_time)

    train_engine = _engine(
        initial_cash=initial_cash,
        trade_percent=trade_percent,
        min_score=min_score,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
    )
    baseline_train = train_engine.run(train_data)

    test_engine = _engine(
        initial_cash=initial_cash,
        trade_percent=trade_percent,
        min_score=min_score,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        trade_start_time=split_time,
    )
    baseline_test = test_engine.run(candles_by_symbol)

    comparisons = compare_winners_losers(baseline_train.trades)
    filters = generate_single_feature_filters(baseline_train.trades)
    min_trades = max(6, int(len(baseline_train.trades) * 0.25))

    train_ranked: list[tuple[FeatureFilter, BacktestResult]] = []
    for feature_filter in filters:
        result = _engine(
            initial_cash=initial_cash,
            trade_percent=trade_percent,
            min_score=min_score,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            feature_filter=feature_filter,
        ).run(train_data)
        if len(result.trades) < min_trades:
            continue
        train_ranked.append((feature_filter, result))

    def rank(item: tuple[FeatureFilter, BacktestResult]) -> tuple[float, float, float, int]:
        _filter, result = item
        pf = result.profit_factor if result.profit_factor not in (None, float("inf")) else 99.0
        # Prefer positive expectancy first, then PF, then return, then shallower DD.
        return (
            1.0 if result.return_pct > 0 and pf > 1 else 0.0,
            pf,
            result.return_pct,
            result.max_drawdown_pct,
        )

    train_ranked.sort(key=rank, reverse=True)
    experiments: list[FilterExperiment] = []
    for feature_filter, train_result in train_ranked[:top_n]:
        test_result = _engine(
            initial_cash=initial_cash,
            trade_percent=trade_percent,
            min_score=min_score,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            feature_filter=feature_filter,
            trade_start_time=split_time,
        ).run(candles_by_symbol)
        experiments.append(
            FilterExperiment(
                name=feature_filter.name,
                feature_filter=feature_filter,
                train_return=train_result.return_pct,
                train_pf=train_result.profit_factor,
                train_drawdown=train_result.max_drawdown_pct,
                train_trades=len(train_result.trades),
                test_return=test_result.return_pct,
                test_pf=test_result.profit_factor,
                test_drawdown=test_result.max_drawdown_pct,
                test_trades=len(test_result.trades),
            )
        )

    return ResearchReport(
        split_time=split_time,
        train_ratio=train_ratio,
        baseline_train=baseline_train,
        baseline_test=baseline_test,
        feature_comparisons=comparisons,
        experiments=tuple(experiments),
    )


def report_to_json(report: ResearchReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def result_summary(result: BacktestResult) -> dict[str, float | int | None]:
        return {
            "return_pct": result.return_pct,
            "trades": len(result.trades),
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_costs": result.total_costs,
        }

    payload = {
        "split_time": report.split_time,
        "train_ratio": report.train_ratio,
        "baseline_train": result_summary(report.baseline_train),
        "baseline_test": result_summary(report.baseline_test),
        "feature_comparisons": [asdict(row) for row in report.feature_comparisons],
        "experiments": [
            {
                **{k: v for k, v in asdict(row).items() if k != "feature_filter"},
                "feature_filter": asdict(row.feature_filter),
                "train_profitable": row.train_profitable,
                "test_profitable": row.test_profitable,
            }
            for row in report.experiments
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
