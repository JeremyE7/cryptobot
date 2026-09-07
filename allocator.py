from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from math import prod
from pathlib import Path
from statistics import mean, median

from crypto_bot.backtest.allocator_engine import AllocatorBacktestEngine, AllocatorBacktestResult
from crypto_bot.market.binance import Candle
from crypto_bot.market.resample import resample_candles
from crypto_bot.strategy.allocator import AllocationRules

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class AllocatorBenchmark:
    name: str
    return_pct: float
    final_value: float
    max_drawdown_pct: float
    annualized_return_pct: float
    calmar_ratio: float | None


@dataclass(frozen=True, slots=True)
class AllocatorFold:
    number: int
    start_time: int
    end_time: int
    result: AllocatorBacktestResult
    btc_hold: AllocatorBenchmark
    equal_weight: AllocatorBenchmark


@dataclass(frozen=True, slots=True)
class AllocatorWalkForward:
    folds: tuple[AllocatorFold, ...]
    profitable_folds: int
    total_folds: int
    beat_equal_weight_folds: int
    mean_return_pct: float
    median_return_pct: float
    compounded_return_pct: float
    worst_fold_return_pct: float
    best_fold_return_pct: float
    worst_drawdown_pct: float
    mean_rebalance_events: float
    total_trade_legs: int


@dataclass(frozen=True, slots=True)
class AllocatorEvaluationReport:
    full_result: AllocatorBacktestResult
    raw_control_result: AllocatorBacktestResult
    full_benchmarks: tuple[AllocatorBenchmark, ...]
    walk_forward: AllocatorWalkForward
    warmup_days: int
    fold_days: int
    rules: AllocationRules
    fee_rate: float
    slippage_rate: float


def _daily_map(candles: list[Candle]) -> dict[int, Candle]:
    return {c.open_time: c for c in resample_candles(candles, "1d")}


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1.0) * 100.0)
    return worst


def _benchmark_from_curves(name: str, values: list[float], days: int) -> AllocatorBenchmark:
    initial = values[0]
    final = values[-1]
    ret = (final / initial - 1.0) * 100.0 if initial > 0 else 0.0
    annualized = ((final / initial) ** (365.0 / max(days, 1)) - 1.0) * 100.0 if initial > 0 else 0.0
    dd = _max_drawdown(values)
    calmar = annualized / abs(dd) if dd < 0 else None
    return AllocatorBenchmark(name, ret, final, dd, annualized, calmar)


def _single_hold_benchmark(
    name: str,
    daily: dict[int, Candle],
    start: int,
    end: int,
    initial_cash: float,
) -> AllocatorBenchmark:
    times = [t for t in sorted(daily) if start <= t <= end]
    if not times:
        return AllocatorBenchmark(name, 0.0, initial_cash, 0.0, 0.0, None)
    first = daily[times[0]]
    qty = initial_cash / first.open
    curve = [initial_cash] + [qty * daily[t].close for t in times]
    return _benchmark_from_curves(name, curve, len(times))


def _equal_weight_benchmark(
    daily_by_symbol: dict[str, dict[int, Candle]],
    start: int,
    end: int,
    initial_cash: float,
) -> AllocatorBenchmark:
    common = sorted(
        set.intersection(*(set(rows) for rows in daily_by_symbol.values()))
    )
    times = [t for t in common if start <= t <= end]
    if not times:
        return AllocatorBenchmark("EQUAL-WEIGHT HOLD", 0.0, initial_cash, 0.0, 0.0, None)
    first_t = times[0]
    per_asset = initial_cash / len(daily_by_symbol)
    qty = {s: per_asset / rows[first_t].open for s, rows in daily_by_symbol.items()}
    curve = [initial_cash]
    for t in times:
        curve.append(sum(qty[s] * rows[t].close for s, rows in daily_by_symbol.items()))
    return _benchmark_from_curves("EQUAL-WEIGHT HOLD", curve, len(times))


def run_allocator_evaluation(
    candles_by_symbol: dict[str, list[Candle]],
    btc_candles: list[Candle],
    *,
    initial_cash: float = 10.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    warmup_days: int = 240,
    fold_days: int = 90,
    rules: AllocationRules | None = None,
) -> AllocatorEvaluationReport:
    rules = rules or AllocationRules()
    daily_by_symbol = {s: _daily_map(rows) for s, rows in candles_by_symbol.items()}
    btc_daily = _daily_map(btc_candles)
    common = sorted(set(btc_daily).intersection(*(set(rows) for rows in daily_by_symbol.values())))
    if len(common) < warmup_days + 31:
        raise ValueError("Not enough aligned daily data after allocator warmup")

    data_start, data_end = common[0], common[-1]
    target_start = data_start + warmup_days * DAY_MS
    eval_start = next((t for t in common if t >= target_start), None)
    if eval_start is None:
        raise ValueError("Allocator evaluation cannot start after warmup")

    def engine(start: int, end: int) -> AllocatorBacktestEngine:
        return AllocatorBacktestEngine(
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            rules=rules,
            trade_start_time=start,
            trade_end_time=end,
        )

    full = engine(eval_start, data_end).run(candles_by_symbol, btc_candles)
    # Simple architectural control: no deadband, no mixed slow-bull hold and
    # immediate regime transitions. This is intentionally simpler than the
    # selected v6 rules and is used only as an A/B research baseline.
    control_rules = replace(
        rules,
        recovery_confirm_days=1,
        neutral_confirm_days=1,
        strong_bull_confirm_days=1,
        recovery_reentry_lockout_days=0,
        fast_entry_gap_pct=0.0,
        fast_exit_gap_pct=0.0,
        slow_bull_mixed_to_recovery=False,
    )
    raw_control = AllocatorBacktestEngine(
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        rules=control_rules,
        trade_start_time=eval_start,
        trade_end_time=data_end,
    ).run(candles_by_symbol, btc_candles)
    benchmarks: list[AllocatorBenchmark] = [
        AllocatorBenchmark("CASH", 0.0, initial_cash, 0.0, 0.0, None),
        _single_hold_benchmark("BTCUSDT HOLD", btc_daily, full.effective_start_time, full.effective_end_time, initial_cash),
    ]
    for symbol, rows in sorted(daily_by_symbol.items()):
        benchmarks.append(_single_hold_benchmark(f"{symbol} HOLD", rows, full.effective_start_time, full.effective_end_time, initial_cash))
    equal_full = _equal_weight_benchmark(daily_by_symbol, full.effective_start_time, full.effective_end_time, initial_cash)
    benchmarks.append(equal_full)

    folds: list[AllocatorFold] = []
    fold_start = eval_start
    fold_no = 1
    while fold_start < data_end:
        target_end = fold_start + fold_days * DAY_MS
        fold_times = [t for t in common if fold_start <= t <= min(target_end, data_end)]
        if not fold_times:
            break
        fold_end = fold_times[-1]
        if fold_end - fold_start < 30 * DAY_MS:
            break
        result = engine(fold_start, fold_end).run(candles_by_symbol, btc_candles)
        folds.append(
            AllocatorFold(
                number=fold_no,
                start_time=result.effective_start_time,
                end_time=result.effective_end_time,
                result=result,
                btc_hold=_single_hold_benchmark("BTCUSDT HOLD", btc_daily, result.effective_start_time, result.effective_end_time, initial_cash),
                equal_weight=_equal_weight_benchmark(daily_by_symbol, result.effective_start_time, result.effective_end_time, initial_cash),
            )
        )
        fold_no += 1
        next_target = fold_start + fold_days * DAY_MS
        next_start = next((t for t in common if t > next_target), None)
        if next_start is None:
            break
        fold_start = next_start

    if not folds:
        raise ValueError("No allocator walk-forward folds could be created")
    returns = [f.result.return_pct for f in folds]
    wf = AllocatorWalkForward(
        folds=tuple(folds),
        profitable_folds=sum(x > 0 for x in returns),
        total_folds=len(folds),
        beat_equal_weight_folds=sum(f.result.return_pct > f.equal_weight.return_pct for f in folds),
        mean_return_pct=mean(returns),
        median_return_pct=median(returns),
        compounded_return_pct=(prod(1.0 + x / 100.0 for x in returns) - 1.0) * 100.0,
        worst_fold_return_pct=min(returns),
        best_fold_return_pct=max(returns),
        worst_drawdown_pct=min(f.result.max_drawdown_pct for f in folds),
        mean_rebalance_events=mean(f.result.rebalance_events for f in folds),
        total_trade_legs=sum(f.result.trade_legs for f in folds),
    )
    return AllocatorEvaluationReport(
        full_result=full,
        raw_control_result=raw_control,
        full_benchmarks=tuple(benchmarks),
        walk_forward=wf,
        warmup_days=warmup_days,
        fold_days=fold_days,
        rules=rules,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
    )


def allocator_evaluation_to_json(report: AllocatorEvaluationReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def result_dict(r: AllocatorBacktestResult) -> dict:
        return {
            "initial_cash": r.initial_cash,
            "final_equity": r.final_equity,
            "return_pct": r.return_pct,
            "annualized_return_pct": r.annualized_return_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "annualized_volatility_pct": r.annualized_volatility_pct,
            "calmar_ratio": r.calmar_ratio,
            "daily_profit_factor": r.daily_profit_factor,
            "total_fees": r.total_fees,
            "total_slippage_cost": r.total_slippage_cost,
            "total_costs": r.total_costs,
            "total_turnover": r.total_turnover,
            "rebalance_events": r.rebalance_events,
            "regime_change_events": r.regime_change_events,
            "raw_regime_changes": r.raw_regime_changes,
            "stabilized_regime_changes": r.stabilized_regime_changes,
            "filtered_regime_changes": r.filtered_regime_changes,
            "hysteresis_hold_days": r.hysteresis_hold_days,
            "reentry_blocked_days": r.reentry_blocked_days,
            "trade_legs": r.trade_legs,
            "average_exposure_pct": r.average_exposure_pct,
            "effective_start_time": r.effective_start_time,
            "effective_end_time": r.effective_end_time,
            "regime_days": dict(r.regime_days),
            "raw_regime_days": dict(r.raw_regime_days),
        }

    payload = {
        "strategy": "dual_regime_deadband_allocator_v6",
        "warmup_days": report.warmup_days,
        "fold_days": report.fold_days,
        "fee_rate": report.fee_rate,
        "slippage_rate": report.slippage_rate,
        "rules": asdict(report.rules),
        "full_result": result_dict(report.full_result),
        "raw_control_result": result_dict(report.raw_control_result),
        "full_benchmarks": [asdict(x) for x in report.full_benchmarks],
        "walk_forward": {
            "profitable_folds": report.walk_forward.profitable_folds,
            "total_folds": report.walk_forward.total_folds,
            "beat_equal_weight_folds": report.walk_forward.beat_equal_weight_folds,
            "mean_return_pct": report.walk_forward.mean_return_pct,
            "median_return_pct": report.walk_forward.median_return_pct,
            "compounded_return_pct": report.walk_forward.compounded_return_pct,
            "worst_fold_return_pct": report.walk_forward.worst_fold_return_pct,
            "best_fold_return_pct": report.walk_forward.best_fold_return_pct,
            "worst_drawdown_pct": report.walk_forward.worst_drawdown_pct,
            "mean_rebalance_events": report.walk_forward.mean_rebalance_events,
            "total_trade_legs": report.walk_forward.total_trade_legs,
            "folds": [
                {
                    "number": f.number,
                    "start_time": f.start_time,
                    "end_time": f.end_time,
                    "strategy": result_dict(f.result),
                    "btc_hold": asdict(f.btc_hold),
                    "equal_weight": asdict(f.equal_weight),
                }
                for f in report.walk_forward.folds
            ],
        },
        "events": [
            {
                "time": e.time,
                "regime": e.regime,
                "raw_regime": e.raw_regime,
                "previous_regime": e.previous_regime,
                "trigger": e.trigger,
                "selected": list(e.selected),
                "target_exposure": e.target_exposure,
                "equity_before": e.equity_before,
                "equity_after_trades": e.equity_after_trades,
                "ranked": [asdict(x) for x in e.ranked],
                "legs": [asdict(x) for x in e.legs],
            }
            for e in report.full_result.events
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
