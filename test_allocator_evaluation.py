from __future__ import annotations

from crypto_bot.evaluation.allocator import run_allocator_evaluation
from crypto_bot.market.binance import Candle
from crypto_bot.strategy.allocator import AllocationRules

DAY = 24 * 60 * 60 * 1000
STEP = 15 * 60 * 1000


def market(symbol: str, days: int, growth: float) -> list[Candle]:
    rows = []
    for d in range(days):
        base = 100 * ((1 + growth) ** d)
        for slot in range(96):
            t = d * DAY + slot * STEP
            rows.append(Candle(symbol, "15m", t, base, base * 1.001, base * 0.999, base, 10, t + STEP - 1, 100, 1, 5, 50))
    return rows


def test_allocator_evaluation_creates_walkforward_folds():
    days = 520
    btc = market("BTCUSDT", days, 0.001)
    a = market("AAAUSDT", days, 0.0012)
    b = market("BBBUSDT", days, 0.0006)
    report = run_allocator_evaluation(
        {"AAAUSDT": a, "BBBUSDT": b},
        btc,
        initial_cash=10,
        warmup_days=240,
        fold_days=90,
        rules=AllocationRules(top_k=1, min_order_notional=5),
    )
    assert report.walk_forward.total_folds >= 3
    assert len(report.full_benchmarks) == 5  # cash, BTC, two symbols, equal-weight
    assert report.full_result.effective_start_time >= 240 * DAY
