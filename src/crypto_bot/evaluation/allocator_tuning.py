from __future__ import annotations

from dataclasses import dataclass, replace
from math import prod
from statistics import mean

from crypto_bot.market.binance import Candle
from crypto_bot.strategy.allocator import AllocationRules
from crypto_bot.evaluation.allocator import AllocatorEvaluationReport, run_allocator_evaluation


@dataclass(frozen=True, slots=True)
class CandidateResult:
    name: str
    rules: AllocationRules
    report: AllocatorEvaluationReport
    dev_folds: int
    dev_profitable_folds: int
    dev_compounded_return_pct: float
    dev_mean_return_pct: float
    dev_worst_return_pct: float
    dev_worst_drawdown_pct: float
    holdout_folds: int
    holdout_profitable_folds: int
    holdout_compounded_return_pct: float
    holdout_mean_return_pct: float
    holdout_worst_return_pct: float
    holdout_worst_drawdown_pct: float

    @property
    def dev_sort_key(self) -> tuple[float, ...]:
        """Selection key that deliberately never looks at holdout folds.

        Robustness is more important than maximizing a single total return:
        profitable fold count -> compounded return -> worst fold -> drawdown ->
        lower churn. This is intentionally lexicographic rather than a fitted
        weighted formula.
        """
        r = self.report.full_result
        return (
            float(self.dev_profitable_folds),
            self.dev_compounded_return_pct,
            self.dev_worst_return_pct,
            self.dev_worst_drawdown_pct,
            -float(r.trade_legs),
            -r.total_costs,
        )


def default_candidate_rules(base: AllocationRules | None = None) -> tuple[tuple[str, AllocationRules], ...]:
    base = base or AllocationRules()

    def r(name: str, **kwargs) -> tuple[str, AllocationRules]:
        return name, replace(base, **kwargs)

    # This grid is intentionally small and coarse. We are testing architecture,
    # not searching for a magical basis-point threshold.
    return (
        r(
            "raw-4-style",
            recovery_confirm_days=1,
            neutral_confirm_days=1,
            strong_bull_confirm_days=1,
            recovery_reentry_lockout_days=0,
            fast_entry_gap_pct=0.0,
            fast_exit_gap_pct=0.0,
            slow_bull_mixed_to_recovery=False,
        ),
        r(
            "asymmetric-no-band",
            recovery_confirm_days=1,
            neutral_confirm_days=2,
            strong_bull_confirm_days=1,
            recovery_reentry_lockout_days=0,
            fast_entry_gap_pct=0.0,
            fast_exit_gap_pct=0.0,
            slow_bull_mixed_to_recovery=False,
        ),
        r(
            "asymmetric-mixed",
            recovery_confirm_days=1,
            neutral_confirm_days=2,
            strong_bull_confirm_days=1,
            recovery_reentry_lockout_days=0,
            fast_entry_gap_pct=0.0,
            fast_exit_gap_pct=0.0,
            slow_bull_mixed_to_recovery=True,
        ),
        r("band-003", fast_entry_gap_pct=0.03, fast_exit_gap_pct=-0.03),
        r("band-005", fast_entry_gap_pct=0.05, fast_exit_gap_pct=-0.05),
        r("band-008", fast_entry_gap_pct=0.08, fast_exit_gap_pct=-0.08),
        r("band-010", fast_entry_gap_pct=0.10, fast_exit_gap_pct=-0.10),
        r("band-entry008-exit005", fast_entry_gap_pct=0.08, fast_exit_gap_pct=-0.05),
        r("band-entry010-exit005", fast_entry_gap_pct=0.10, fast_exit_gap_pct=-0.05),
        r("band-entry005-exit008", fast_entry_gap_pct=0.05, fast_exit_gap_pct=-0.08),
        r("band-005-neutral3", fast_entry_gap_pct=0.05, fast_exit_gap_pct=-0.05, neutral_confirm_days=3),
        r("band-008-neutral3", fast_entry_gap_pct=0.08, fast_exit_gap_pct=-0.08, neutral_confirm_days=3),
        r(
            "band-005-no-mixed",
            fast_entry_gap_pct=0.05,
            fast_exit_gap_pct=-0.05,
            slow_bull_mixed_to_recovery=False,
        ),
    )


def _fold_summary(folds) -> tuple[int, int, float, float, float, float]:
    rows = list(folds)
    if not rows:
        return 0, 0, 0.0, 0.0, 0.0, 0.0
    returns = [f.result.return_pct for f in rows]
    compounded = (prod(1.0 + x / 100.0 for x in returns) - 1.0) * 100.0
    return (
        len(rows),
        sum(x > 0 for x in returns),
        compounded,
        mean(returns),
        min(returns),
        min(f.result.max_drawdown_pct for f in rows),
    )


def run_allocator_tuning(
    candles_by_symbol: dict[str, list[Candle]],
    btc_candles: list[Candle],
    *,
    initial_cash: float = 10.0,
    fee_rate: float = 0.001,
    slippage_rate: float = 0.0005,
    warmup_days: int = 240,
    fold_days: int = 90,
    development_folds: int = 4,
    base_rules: AllocationRules | None = None,
) -> tuple[tuple[CandidateResult, ...], CandidateResult]:
    results: list[CandidateResult] = []
    for name, rules in default_candidate_rules(base_rules):
        report = run_allocator_evaluation(
            candles_by_symbol,
            btc_candles,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            warmup_days=warmup_days,
            fold_days=fold_days,
            rules=rules,
        )
        folds = report.walk_forward.folds
        split = min(max(1, development_folds), max(1, len(folds) - 1))
        dev = folds[:split]
        holdout = folds[split:]
        d = _fold_summary(dev)
        h = _fold_summary(holdout)
        results.append(
            CandidateResult(
                name=name,
                rules=rules,
                report=report,
                dev_folds=d[0],
                dev_profitable_folds=d[1],
                dev_compounded_return_pct=d[2],
                dev_mean_return_pct=d[3],
                dev_worst_return_pct=d[4],
                dev_worst_drawdown_pct=d[5],
                holdout_folds=h[0],
                holdout_profitable_folds=h[1],
                holdout_compounded_return_pct=h[2],
                holdout_mean_return_pct=h[3],
                holdout_worst_return_pct=h[4],
                holdout_worst_drawdown_pct=h[5],
            )
        )

    ordered = tuple(sorted(results, key=lambda x: x.dev_sort_key, reverse=True))
    return ordered, ordered[0]
