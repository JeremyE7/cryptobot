from __future__ import annotations

from crypto_bot.backtest import BacktestEngine, FeatureFilter
from crypto_bot.market.binance import Candle
from crypto_bot.research import compare_winners_losers, generate_single_feature_filters, run_research


def make_market(symbol: str, phase: float = 0.0, n: int = 360) -> list[Candle]:
    import math

    rows: list[Candle] = []
    price = 100.0 + phase
    step_ms = 15 * 60 * 1000
    for i in range(n):
        # Deterministic alternating regimes with enough noise/volume variation
        # to exercise TAKE and STOP paths without randomness.
        regime = 0.22 if (i // 45) % 2 == 0 else -0.08
        wave = math.sin((i + phase) / 5.0) * 0.18
        move = regime + wave
        open_price = max(1.0, price)
        close_price = max(1.0, open_price + move)
        high = max(open_price, close_price) * (1.008 + (i % 7) * 0.0005)
        low = min(open_price, close_price) * (0.992 - (i % 5) * 0.0003)
        volume = 900.0 + (i % 11) * 140.0 + (900.0 if i % 17 == 0 else 0.0)
        rows.append(
            Candle(
                symbol=symbol,
                interval="15m",
                open_time=i * step_ms,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=volume,
                close_time=(i + 1) * step_ms - 1,
                quote_asset_volume=volume * close_price,
                number_of_trades=100 + i % 20,
                taker_buy_base_volume=volume / 2,
                taker_buy_quote_volume=volume * close_price / 2,
            )
        )
        price = close_price
    return rows


def data() -> dict[str, list[Candle]]:
    return {
        "AAAUSDT": make_market("AAAUSDT", 0.0),
        "BBBUSDT": make_market("BBBUSDT", 2.0),
        "CCCUSDT": make_market("CCCUSDT", 4.0),
    }


def test_trade_stores_entry_features_and_filter_can_apply() -> None:
    base = BacktestEngine(
        trade_size=None,
        trade_percent=50,
        min_score=60,
        stop_loss_pct=0.02,
        take_profit_pct=0.03,
    ).run(data())
    assert base.trades
    t = base.trades[0]
    assert t.rsi14 > 0
    assert t.volume_ratio > 0
    assert t.atr_pct >= 0

    strict = BacktestEngine(
        trade_size=None,
        trade_percent=50,
        min_score=60,
        stop_loss_pct=0.02,
        take_profit_pct=0.03,
        feature_filter=FeatureFilter(name="low-volume", max_volume_ratio=1.5),
    ).run(data())
    assert all(t.volume_ratio <= 1.5 + 1e-12 for t in strict.trades)


def test_winner_loser_comparison_and_filter_generation() -> None:
    result = BacktestEngine(
        trade_size=None,
        trade_percent=50,
        min_score=60,
        stop_loss_pct=0.01,
        take_profit_pct=0.01,
    ).run(data())
    assert result.trades
    comparisons = compare_winners_losers(result.trades)
    # Synthetic data should normally contain both outcomes; if so, all five
    # entry features must be diagnosed.
    if result.wins and result.losses:
        assert {row.feature for row in comparisons} == {
            "volume_ratio", "momentum_3", "ema_gap_pct", "rsi14", "atr_pct"
        }
    filters = generate_single_feature_filters(result.trades)
    assert filters


def test_research_uses_chronological_train_test_and_runs() -> None:
    report = run_research(
        data(),
        initial_cash=10,
        trade_percent=50,
        min_score=60,
        stop_loss_pct=0.01,
        take_profit_pct=0.01,
        train_ratio=0.70,
        top_n=4,
    )
    assert report.split_time > 0
    assert report.baseline_train.final_equity > 0
    assert report.baseline_test.final_equity > 0
    assert len(report.experiments) <= 4
    assert all(exp.train_trades > 0 for exp in report.experiments)
