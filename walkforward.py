from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from math import prod
from pathlib import Path
from statistics import mean, median

from crypto_bot.backtest.trend_engine import TrendBacktestResult, TrendPullbackBacktestEngine, TrendTrade
from crypto_bot.market.binance import Candle
from crypto_bot.strategy.trend_pullback import PullbackRules

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    name: str
    return_pct: float
    final_value: float


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    number: int
    start_time: int
    end_time: int
    result: TrendBacktestResult
    btc_hold_return: float
    equal_weight_return: float


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    folds: tuple[WalkForwardFold, ...]
    profitable_folds: int
    total_folds: int
    mean_return_pct: float
    median_return_pct: float
    compounded_return_pct: float
    worst_fold_return_pct: float
    best_fold_return_pct: float
    worst_drawdown_pct: float
    aggregate_profit_factor: float | None
    total_trades: int


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    full_result: TrendBacktestResult
    full_benchmarks: tuple[BenchmarkSummary, ...]
    walk_forward: WalkForwardSummary
    warmup_days: int
    fold_days: int
    rules: PullbackRules
    atr_stop_mult: float
    risk_reward: float
    min_stop_pct: float
    max_stop_pct: float


def _closest_candle(candles: list[Candle], target: int, *, after: bool) -> Candle:
    ordered = candles
    if after:
        for c in ordered:
            if c.open_time >= target:
                return c
        return ordered[-1]
    for c in reversed(ordered):
        if c.open_time <= target:
            return c
    return ordered[0]


def _hold_return(candles: list[Candle], start: int, end: int) -> float:
    first = _closest_candle(candles, start, after=True)
    last = _closest_candle(candles, end, after=False)
    return (last.close / first.open - 1.0) * 100.0


def _equal_weight_return(candles_by_symbol: dict[str, list[Candle]], start: int, end: int) -> float:
    returns = [_hold_return(candles, start, end) for candles in candles_by_symbol.values()]
    return mean(returns) if returns else 0.0


def _aggregate_pf(trades: list[TrendTrade]) -> float | None:
    gp = sum(t.pnl for t in trades if t.pnl > 0)
    gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return float("inf")
    return None


def run_evaluation(
    candles_by_symbol: dict[str, list[Candle]],
    btc_candles: list[Candle],
    *,
    initial_cash: float = 10.0,
    trade_percent: float = 50.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    warmup_days: int = 240,
    fold_days: int = 90,
    atr_stop_mult: float = 2.0,
    risk_reward: float = 2.0,
    min_stop_pct: float = 0.01,
    max_stop_pct: float = 0.03,
    rules: PullbackRules | None = None,
) -> EvaluationReport:
    rules = rules or PullbackRules()
    all_times = sorted(set.intersection(*(set(c.open_time for c in rows) for rows in candles_by_symbol.values())))
    if not all_times:
        raise ValueError("No aligned trade-symbol candles")
    data_start, data_end = all_times[0], all_times[-1]
    eval_start_target = data_start + warmup_days * DAY_MS
    eval_start = next((t for t in all_times if t >= eval_start_target), None)
    if eval_start is None or data_end - eval_start < 30 * DAY_MS:
        raise ValueError("Not enough data after warmup for evaluation")

    def engine(start: int | None, end: int | None) -> TrendPullbackBacktestEngine:
        return TrendPullbackBacktestEngine(
            initial_cash=initial_cash,
            trade_percent=trade_percent,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            atr_stop_mult=atr_stop_mult,
            risk_reward=risk_reward,
            min_stop_pct=min_stop_pct,
            max_stop_pct=max_stop_pct,
            rules=rules,
            trade_start_time=start,
            trade_end_time=end,
        )

    full = engine(eval_start, data_end).run(candles_by_symbol, btc_candles)
    benchmarks: list[BenchmarkSummary] = [BenchmarkSummary("CASH", 0.0, initial_cash)]
    btc_ret = _hold_return(btc_candles, full.effective_start_time, full.effective_end_time)
    benchmarks.append(BenchmarkSummary("BTCUSDT HOLD", btc_ret, initial_cash * (1 + btc_ret / 100.0)))
    for b in full.benchmarks:
        benchmarks.append(BenchmarkSummary(f"{b.symbol} HOLD", b.return_pct, b.final_value))
    eq_ret = mean([b.return_pct for b in full.benchmarks]) if full.benchmarks else 0.0
    benchmarks.append(BenchmarkSummary("EQUAL-WEIGHT TRADE SYMBOLS", eq_ret, initial_cash * (1 + eq_ret / 100.0)))

    folds: list[WalkForwardFold] = []
    fold_start = eval_start
    fold_no = 1
    while fold_start < data_end:
        desired_end = fold_start + fold_days * DAY_MS
        fold_times = [t for t in all_times if fold_start <= t <= min(desired_end, data_end)]
        if not fold_times:
            break
        fold_end = fold_times[-1]
        if fold_end - fold_start < 30 * DAY_MS:
            break
        result = engine(fold_start, fold_end).run(candles_by_symbol, btc_candles)
        folds.append(
            WalkForwardFold(
                number=fold_no,
                start_time=result.effective_start_time,
                end_time=result.effective_end_time,
                result=result,
                btc_hold_return=_hold_return(btc_candles, result.effective_start_time, result.effective_end_time),
                equal_weight_return=_equal_weight_return(candles_by_symbol, result.effective_start_time, result.effective_end_time),
            )
        )
        fold_no += 1
        next_target = fold_start + fold_days * DAY_MS
        next_start = next((t for t in all_times if t > next_target), None)
        if next_start is None:
            break
        fold_start = next_start

    if not folds:
        raise ValueError("No walk-forward folds could be created")
    fold_returns = [f.result.return_pct for f in folds]
    all_trades = [t for f in folds for t in f.result.trades]
    compounded = (prod(1.0 + r / 100.0 for r in fold_returns) - 1.0) * 100.0
    summary = WalkForwardSummary(
        folds=tuple(folds),
        profitable_folds=sum(r > 0 for r in fold_returns),
        total_folds=len(folds),
        mean_return_pct=mean(fold_returns),
        median_return_pct=median(fold_returns),
        compounded_return_pct=compounded,
        worst_fold_return_pct=min(fold_returns),
        best_fold_return_pct=max(fold_returns),
        worst_drawdown_pct=min(f.result.max_drawdown_pct for f in folds),
        aggregate_profit_factor=_aggregate_pf(all_trades),
        total_trades=len(all_trades),
    )
    return EvaluationReport(
        full_result=full,
        full_benchmarks=tuple(benchmarks),
        walk_forward=summary,
        warmup_days=warmup_days,
        fold_days=fold_days,
        rules=rules,
        atr_stop_mult=atr_stop_mult,
        risk_reward=risk_reward,
        min_stop_pct=min_stop_pct,
        max_stop_pct=max_stop_pct,
    )


def evaluation_to_json(report: EvaluationReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def result_dict(result: TrendBacktestResult) -> dict:
        return {
            "initial_cash": result.initial_cash,
            "final_equity": result.final_equity,
            "return_pct": result.return_pct,
            "trades": len(result.trades),
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_costs": result.total_costs,
            "gross_pnl": result.gross_pnl,
            "effective_start_time": result.effective_start_time,
            "effective_end_time": result.effective_end_time,
            "gate_counts": dict(result.gate_counts),
        }

    payload = {
        "strategy": "trend_pullback_v2",
        "warmup_days": report.warmup_days,
        "fold_days": report.fold_days,
        "rules": asdict(report.rules),
        "atr_stop_mult": report.atr_stop_mult,
        "risk_reward": report.risk_reward,
        "min_stop_pct": report.min_stop_pct,
        "max_stop_pct": report.max_stop_pct,
        "full_result": result_dict(report.full_result),
        "full_benchmarks": [asdict(b) for b in report.full_benchmarks],
        "walk_forward": {
            "profitable_folds": report.walk_forward.profitable_folds,
            "total_folds": report.walk_forward.total_folds,
            "mean_return_pct": report.walk_forward.mean_return_pct,
            "median_return_pct": report.walk_forward.median_return_pct,
            "compounded_return_pct": report.walk_forward.compounded_return_pct,
            "worst_fold_return_pct": report.walk_forward.worst_fold_return_pct,
            "best_fold_return_pct": report.walk_forward.best_fold_return_pct,
            "worst_drawdown_pct": report.walk_forward.worst_drawdown_pct,
            "aggregate_profit_factor": report.walk_forward.aggregate_profit_factor,
            "total_trades": report.walk_forward.total_trades,
            "folds": [
                {
                    "number": f.number,
                    "start_time": f.start_time,
                    "end_time": f.end_time,
                    "strategy": result_dict(f.result),
                    "btc_hold_return": f.btc_hold_return,
                    "equal_weight_return": f.equal_weight_return,
                }
                for f in report.walk_forward.folds
            ],
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
