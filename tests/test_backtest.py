from crypto_bot.backtest import BacktestEngine
from crypto_bot.market.binance import Candle


def make_candles(symbol: str, *, rising: bool) -> list[Candle]:
    rows: list[Candle] = []
    price = 100.0
    step_ms = 15 * 60 * 1000
    for i in range(160):
        # Smooth trend + periodic volume spikes to trigger score candidates.
        move = 0.25 if rising else -0.03
        open_price = price
        close_price = max(1.0, price + move)
        high = max(open_price, close_price) * 1.006
        low = min(open_price, close_price) * 0.994
        volume = 1800.0 if i % 7 == 0 else 1000.0
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
                number_of_trades=100,
                taker_buy_base_volume=volume / 2,
                taker_buy_quote_volume=volume * close_price / 2,
            )
        )
        price = close_price
    return rows


def test_backtest_runs_and_returns_metrics() -> None:
    engine = BacktestEngine(min_score=60, stop_loss_pct=0.02, take_profit_pct=0.02)
    result = engine.run(
        {
            "AAAUSDT": make_candles("AAAUSDT", rising=True),
            "BBBUSDT": make_candles("BBBUSDT", rising=False),
        }
    )

    assert result.initial_cash == 10.0
    assert result.final_equity > 0
    assert result.benchmarks
    assert result.max_drawdown_pct <= 0


def test_diagnostics_reconcile_to_net_pnl() -> None:
    engine = BacktestEngine(min_score=60, stop_loss_pct=0.02, take_profit_pct=0.02)
    result = engine.run(
        {
            "AAAUSDT": make_candles("AAAUSDT", rising=True),
            "BBBUSDT": make_candles("BBBUSDT", rising=False),
        }
    )

    assert result.trades
    assert abs(
        result.gross_pnl
        - result.total_fees
        - result.total_slippage_cost
        - result.net_pnl
    ) < 1e-9

    assert sum(row.trades for row in result.by_symbol) == len(result.trades)
    assert sum(row.trades for row in result.by_score) == len(result.trades)
    assert sum(row.trades for row in result.by_exit) == len(result.trades)
    assert abs(sum(row.net_pnl for row in result.by_symbol) - result.net_pnl) < 1e-9


def test_zero_cost_backtest_has_no_cost_drag() -> None:
    engine = BacktestEngine(
        min_score=60,
        stop_loss_pct=0.02,
        take_profit_pct=0.02,
        fee_rate=0.0,
        slippage_rate=0.0,
    )
    result = engine.run({"AAAUSDT": make_candles("AAAUSDT", rising=True)})

    assert result.trades
    assert result.total_fees == 0.0
    assert result.total_slippage_cost == 0.0
    assert abs(result.gross_pnl - result.net_pnl) < 1e-9


def test_trade_percent_uses_current_cash() -> None:
    engine = BacktestEngine(
        trade_size=None,
        trade_percent=50.0,
        min_score=60,
        stop_loss_pct=0.02,
        take_profit_pct=0.02,
    )
    result = engine.run({"AAAUSDT": make_candles("AAAUSDT", rising=True)})

    assert result.trades
    assert abs(result.trades[0].trade_budget - 5.0) < 1e-9
    # Dynamic sizing should change at least once after realized PnL changes cash.
    assert any(abs(t.trade_budget - result.trades[0].trade_budget) > 1e-6 for t in result.trades[1:])


def test_cooldown_reduces_or_preserves_trade_count() -> None:
    data = {"AAAUSDT": make_candles("AAAUSDT", rising=True)}
    fast = BacktestEngine(min_score=60, stop_loss_pct=0.02, take_profit_pct=0.02, cooldown_candles=0).run(data)
    slow = BacktestEngine(min_score=60, stop_loss_pct=0.02, take_profit_pct=0.02, cooldown_candles=4).run(data)

    assert len(slow.trades) <= len(fast.trades)


def test_candidate_tie_break_prefers_market_quality_over_symbol() -> None:
    from crypto_bot.backtest import CandidateSignal

    weak_alpha = CandidateSignal(
        symbol="AAAUSDT",
        score=100,
        quality=55.0,
        reasons=(),
        volume_ratio=1.2,
        momentum_3=0.004,
        ema_gap_pct=0.1,
        rsi14=55.0,
        atr_pct=0.5,
        quality_trend=10.0,
        quality_momentum=10.0,
        quality_volume=10.0,
        quality_rsi=10.0,
        quality_atr=15.0,
    )
    strong_zulu = CandidateSignal(
        symbol="ZZZUSDT",
        score=100,
        quality=75.0,
        reasons=(),
        volume_ratio=1.8,
        momentum_3=0.006,
        ema_gap_pct=0.2,
        rsi14=55.0,
        atr_pct=0.8,
        quality_trend=15.0,
        quality_momentum=15.0,
        quality_volume=15.0,
        quality_rsi=15.0,
        quality_atr=15.0,
    )

    ranked = sorted([weak_alpha, strong_zulu], key=BacktestEngine._candidate_rank)
    assert ranked[0].symbol == "ZZZUSDT"


def test_min_quality_filters_candidates_and_reports_analysis() -> None:
    data = {
        "AAAUSDT": make_candles("AAAUSDT", rising=True),
        "BBBUSDT": make_candles("BBBUSDT", rising=True),
    }
    loose = BacktestEngine(
        min_score=60,
        min_quality=0,
        stop_loss_pct=0.02,
        take_profit_pct=0.02,
    ).run(data)
    strict = BacktestEngine(
        min_score=60,
        min_quality=90,
        stop_loss_pct=0.02,
        take_profit_pct=0.02,
    ).run(data)

    assert loose.signal_analysis.base_candidates >= loose.signal_analysis.passed_quality
    assert strict.signal_analysis.rejected_quality >= 0
    assert strict.signal_analysis.passed_quality <= strict.signal_analysis.base_candidates
    assert len(strict.selections) <= len(loose.selections)


def test_quality_diagnostics_reconcile_trade_count() -> None:
    result = BacktestEngine(
        min_score=60,
        min_quality=0,
        stop_loss_pct=0.02,
        take_profit_pct=0.02,
    ).run({"AAAUSDT": make_candles("AAAUSDT", rising=True)})

    assert sum(row.trades for row in result.by_quality) == len(result.trades)
    assert all(0 <= trade.quality <= 100 for trade in result.trades)
