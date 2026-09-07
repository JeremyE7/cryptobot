from __future__ import annotations

from crypto_bot.backtest.allocator_engine import AllocatorBacktestEngine
from crypto_bot.market.binance import Candle
from crypto_bot.strategy.allocator import AllocationRules

DAY = 24 * 60 * 60 * 1000
STEP = 15 * 60 * 1000


def market(symbol: str, days: int, daily_fn) -> list[Candle]:
    rows: list[Candle] = []
    for d in range(days):
        base = daily_fn(d)
        for slot in range(96):
            t = d * DAY + slot * STEP
            # Tiny intraday slope; daily close remains near base and buckets are complete.
            p = base * (1 + (slot - 48) * 0.000002)
            rows.append(Candle(symbol, "15m", t, p, p * 1.001, p * 0.999, p, 10, t + STEP - 1, 100, 1, 5, 50))
    return rows


def test_allocator_trades_weekly_and_exits_in_bear():
    days = 330
    # BTC strong bull until day 260, then sustained decline below its long-term trend.
    btc = market("BTCUSDT", days, lambda d: 100 + d * 0.35 if d < 260 else 191 - (d - 260) * 2.0)
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.25)
    bbb = market("BBBUSDT", days, lambda d: 80 + d * 0.08)
    engine = AllocatorBacktestEngine(
        initial_cash=10,
        fee_rate=0.001,
        slippage_rate=0.0005,
        rules=AllocationRules(top_k=1, min_order_notional=5, emergency_bear_exit=True),
        trade_start_time=240 * DAY,
        trade_end_time=(days - 1) * DAY,
    )
    result = engine.run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)
    assert result.trade_legs > 0
    assert result.rebalance_events > 0
    assert result.total_costs > 0
    assert result.average_exposure_pct < 100
    # The dual-speed model is intentionally allowed to go risk-off in NEUTRAL
    # before the slow daily structure has fully crossed into BEAR.
    assert any(leg.side == "SELL" for leg in result.legs)
    risk_off_days = dict(result.regime_days).get("NEUTRAL", 0) + dict(result.regime_days).get("BEAR", 0)
    assert risk_off_days > 0
    assert any(event.trigger in {"DEFENSIVE", "REGIME_CHANGE"} for event in result.events)


def test_allocator_does_not_rebalance_every_day_when_leader_unchanged():
    days = 300
    btc = market("BTCUSDT", days, lambda d: 100 + d * 0.4)
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.3)
    bbb = market("BBBUSDT", days, lambda d: 80 + d * 0.05)
    engine = AllocatorBacktestEngine(
        initial_cash=10,
        rules=AllocationRules(top_k=1, min_order_notional=5, rebalance_band=0.10),
        trade_start_time=240 * DAY,
        trade_end_time=(days - 1) * DAY,
    )
    result = engine.run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)
    # One buy may be enough when the same leader stays near 100% weight.
    assert result.trade_legs < 10


def test_allocator_default_min_order_allows_two_near_half_positions_with_ten_dollars():
    days = 320
    btc = market("BTCUSDT", days, lambda d: 100 + d * 0.5)
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.35)
    bbb = market("BBBUSDT", days, lambda d: 60 + d * 0.25)
    engine = AllocatorBacktestEngine(
        initial_cash=10,
        fee_rate=0.001,
        slippage_rate=0.0005,
        rules=AllocationRules(top_k=2, min_order_notional=4.90),
        trade_start_time=240 * DAY,
        trade_end_time=(days - 1) * DAY,
    )
    result = engine.run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)
    buy_symbols = {leg.symbol for leg in result.legs if leg.side == "BUY"}
    assert {"AAAUSDT", "BBBUSDT"}.issubset(buy_symbols)


def test_regime_transition_can_rebalance_off_weekly_schedule():
    days = 360
    # Long rise, then a fast/slow breakdown. The transition should cause a defensive event,
    # not wait for the configured Sunday rebalance.
    btc = market("BTCUSDT", days, lambda d: 100 + d * 0.45 if d < 300 else 235 - (d - 300) * 3.0)
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.25)
    bbb = market("BBBUSDT", days, lambda d: 80 + d * 0.15)
    rules = AllocationRules(
        top_k=2,
        min_order_notional=4.90,
        rebalance_weekday=6,
        emergency_bear_exit=True,
        rebalance_on_regime_change=True,
    )
    result = AllocatorBacktestEngine(
        initial_cash=10,
        rules=rules,
        trade_start_time=240 * DAY,
        trade_end_time=(days - 1) * DAY,
    ).run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)
    assert any(event.trigger in {"DEFENSIVE", "REGIME_CHANGE"} for event in result.events)
    assert result.regime_change_events > 0


def test_allocator_reports_raw_vs_stabilized_regime_diagnostics():
    days = 340
    # Add a wavy component around an uptrend so the fast layer has mixed states.
    btc = market(
        "BTCUSDT",
        days,
        lambda d: 100 + d * 0.35 + (5 if (d // 2) % 2 == 0 else -5),
    )
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.25)
    bbb = market("BBBUSDT", days, lambda d: 80 + d * 0.12)
    result = AllocatorBacktestEngine(
        initial_cash=10,
        rules=AllocationRules(
            top_k=2,
            min_order_notional=4.90,
            recovery_confirm_days=2,
            neutral_confirm_days=2,
            strong_bull_confirm_days=2,
            recovery_reentry_lockout_days=2,
        ),
        trade_start_time=240 * DAY,
        trade_end_time=(days - 1) * DAY,
    ).run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)

    assert result.raw_regime_changes >= result.stabilized_regime_changes
    assert result.filtered_regime_changes == max(0, result.raw_regime_changes - result.stabilized_regime_changes)
    assert sum(dict(result.raw_regime_days).values()) == sum(dict(result.regime_days).values())
    assert result.hysteresis_hold_days >= 0
    assert result.reentry_blocked_days >= 0


def test_fold_start_warms_regime_state_and_aligns_portfolio_on_first_day():
    days = 330
    btc = market("BTCUSDT", days, lambda d: 100 + d * 0.5)
    aaa = market("AAAUSDT", days, lambda d: 50 + d * 0.35)
    bbb = market("BBBUSDT", days, lambda d: 70 + d * 0.15)
    # Day 242 is a non-Monday in the synthetic Unix-epoch calendar. With a
    # 2-day risk-on confirmation, a fold-local state reset would miss the first
    # day. The engine must warm the state on prior completed candles and align
    # the fresh cash portfolio immediately at the fold/evaluation start.
    start = 242 * DAY
    result = AllocatorBacktestEngine(
        initial_cash=10,
        rules=AllocationRules(
            top_k=1,
            min_order_notional=4.90,
            recovery_confirm_days=2,
            strong_bull_confirm_days=2,
        ),
        trade_start_time=start,
        trade_end_time=(days - 1) * DAY,
    ).run({"AAAUSDT": aaa, "BBBUSDT": bbb}, btc)

    assert result.effective_start_time == start
    assert result.events
    assert result.events[0].time == start
    assert result.events[0].trigger == "START"
    assert any(leg.side == "BUY" for leg in result.events[0].legs)
