from __future__ import annotations

from crypto_bot.market.binance import Candle
from crypto_bot.market.resample import resample_candles
from crypto_bot.strategy.allocator import (
    AllocationRules,
    DailyFeature,
    FastFeature,
    MarketRegime,
    classify_btc_regime,
    classify_dual_regime,
    rank_assets,
    target_weights,
)


def test_daily_resample_emits_only_complete_day():
    step = 15 * 60 * 1000
    rows = []
    for i in range(97):
        t = i * step
        price = 100 + i / 100
        rows.append(Candle("BTCUSDT", "15m", t, price, price + 1, price - 1, price + 0.2, 1, t + step - 1, 1, 1, 1, 1))
    out = resample_candles(rows, "1d")
    assert len(out) == 1
    assert out[0].interval == "1d"
    assert out[0].open_time == 0


def f(*, close=120, ema50=110, ema200=100, slope=0.01, r30=0.1, r90=0.2, strength=0.1):
    return DailyFeature(0, close, ema50, ema200, slope, r30, r90, strength)


def ff(*, close=120, ema20=115, ema50=110, slope=0.01, mom=0.02):
    return FastFeature(0, close, ema20, ema50, slope, mom)


def test_slow_regime_classification_kept_for_compatibility():
    assert classify_btc_regime(f()) == MarketRegime.STRONG_BULL
    assert classify_btc_regime(f(close=90, ema50=95, ema200=100, slope=-0.01)) == MarketRegime.BEAR
    assert classify_btc_regime(f(close=105, ema50=95, ema200=100, slope=0.01)) == MarketRegime.NEUTRAL


def test_dual_regime_strong_bull_requires_both_layers():
    assert classify_dual_regime(f(), ff()) == MarketRegime.STRONG_BULL
    assert classify_dual_regime(f(), ff(close=100, ema20=105, ema50=110, slope=-0.01, mom=-0.01)) == MarketRegime.NEUTRAL


def test_dual_regime_recovery_enters_before_daily_ema50_ema200_cross():
    daily = f(close=105, ema50=102, ema200=110, slope=0.01, strength=-0.07)
    assert classify_dual_regime(daily, ff()) == MarketRegime.RECOVERY


def test_dual_regime_bear_needs_slow_and_fast_confirmation():
    daily = f(close=90, ema50=95, ema200=100, slope=-0.01)
    bearish_fast = ff(close=90, ema20=94, ema50=96, slope=-0.01, mom=-0.02)
    assert classify_dual_regime(daily, bearish_fast) == MarketRegime.BEAR
    assert classify_dual_regime(daily, ff()) == MarketRegime.NEUTRAL


def test_ranking_and_target_weights_use_top2_in_strong_bull_and_half_top1_in_recovery():
    features = {
        "AAA": f(r30=0.30, r90=0.50, strength=0.20),
        "BBB": f(r30=0.10, r90=0.15, strength=0.05),
        "CCC": f(close=90, ema50=95, ema200=100, r30=-0.1, r90=-0.2, strength=-0.05),
    }
    ranked = rank_assets(features, MarketRegime.STRONG_BULL)
    assert [x.symbol for x in ranked] == ["AAA", "BBB"]
    rules = AllocationRules(top_k=2, recovery_top_k=1, bull_exposure=1.0, recovery_exposure=0.5)
    assert target_weights(MarketRegime.STRONG_BULL, ranked, rules) == {"AAA": 0.5, "BBB": 0.5}

    recovery_features = {
        "AAA": f(close=105, ema50=100, ema200=110, slope=0.01, r30=0.2, r90=0.1, strength=-0.05),
        "BBB": f(close=104, ema50=101, ema200=110, slope=0.01, r30=0.1, r90=0.05, strength=-0.08),
    }
    recovery_ranked = rank_assets(recovery_features, MarketRegime.RECOVERY)
    weights = target_weights(MarketRegime.RECOVERY, recovery_ranked, rules)
    assert len(weights) == 1
    assert abs(sum(weights.values()) - 0.5) < 1e-12
    assert target_weights(MarketRegime.NEUTRAL, ranked, rules) == {}
    assert target_weights(MarketRegime.BEAR, ranked, rules) == {}


def test_hysteresis_requires_two_recovery_confirmations():
    from crypto_bot.strategy.allocator import RegimeHysteresis

    h = RegimeHysteresis(AllocationRules(recovery_confirm_days=2, neutral_confirm_days=2, strong_bull_confirm_days=2))
    d1 = h.update(MarketRegime.RECOVERY)
    assert d1.effective_regime == MarketRegime.NEUTRAL
    assert d1.suppressed
    d2 = h.update(MarketRegime.RECOVERY)
    assert d2.effective_regime == MarketRegime.RECOVERY
    assert d2.changed


def test_hysteresis_neutral_requires_two_days_but_bear_is_immediate():
    from crypto_bot.strategy.allocator import RegimeHysteresis

    h = RegimeHysteresis(AllocationRules(recovery_confirm_days=2, neutral_confirm_days=2))
    h.update(MarketRegime.RECOVERY)
    h.update(MarketRegime.RECOVERY)
    first_neutral = h.update(MarketRegime.NEUTRAL)
    assert first_neutral.effective_regime == MarketRegime.RECOVERY
    assert first_neutral.suppressed
    second_neutral = h.update(MarketRegime.NEUTRAL)
    assert second_neutral.effective_regime == MarketRegime.NEUTRAL
    assert second_neutral.changed

    # A true raw BEAR bypasses neutral confirmation/hysteresis entirely.
    h2 = RegimeHysteresis(AllocationRules())
    h2.update(MarketRegime.RECOVERY)
    h2.update(MarketRegime.RECOVERY)
    bear = h2.update(MarketRegime.BEAR)
    assert bear.effective_regime == MarketRegime.BEAR
    assert bear.changed
    assert bear.reason == "BEAR_IMMEDIATE"


def test_hysteresis_reentry_lockout_breaks_neutral_recovery_pingpong():
    from crypto_bot.strategy.allocator import RegimeHysteresis

    rules = AllocationRules(
        recovery_confirm_days=2,
        neutral_confirm_days=2,
        recovery_reentry_lockout_days=2,
    )
    h = RegimeHysteresis(rules)
    h.update(MarketRegime.RECOVERY)
    h.update(MarketRegime.RECOVERY)
    h.update(MarketRegime.NEUTRAL)
    exit_decision = h.update(MarketRegime.NEUTRAL)
    assert exit_decision.effective_regime == MarketRegime.NEUTRAL
    assert exit_decision.lockout_remaining == 2

    blocked = h.update(MarketRegime.RECOVERY)
    assert blocked.effective_regime == MarketRegime.NEUTRAL
    assert blocked.reentry_blocked

    confirm1 = h.update(MarketRegime.RECOVERY)
    assert confirm1.effective_regime == MarketRegime.NEUTRAL
    confirm2 = h.update(MarketRegime.RECOVERY)
    assert confirm2.effective_regime == MarketRegime.RECOVERY


def test_v6_defaults_are_asymmetric_not_symmetric_lockout():
    rules = AllocationRules()
    assert rules.recovery_confirm_days == 1
    assert rules.strong_bull_confirm_days == 1
    assert rules.neutral_confirm_days == 2
    assert rules.recovery_reentry_lockout_days == 0
    assert rules.fast_entry_gap_pct == 0.10
    assert rules.fast_exit_gap_pct == -0.10
    assert rules.slow_bull_mixed_to_recovery is True


def test_entry_deadband_blocks_tiny_fast_gap_but_allows_clear_recovery():
    from crypto_bot.strategy.allocator import RegimeHysteresis

    rules = AllocationRules(fast_entry_gap_pct=0.05, fast_exit_gap_pct=-0.05)
    h = RegimeHysteresis(rules)
    tiny_gap = ff(ema20=110.02, ema50=110.0)
    d1 = h.update(MarketRegime.RECOVERY, fast=tiny_gap)
    assert d1.effective_regime == MarketRegime.NEUTRAL
    assert d1.reason == "ENTRY_DEADBAND"

    clear_gap = ff(ema20=110.20, ema50=110.0)
    d2 = h.update(MarketRegime.RECOVERY, fast=clear_gap)
    assert d2.effective_regime == MarketRegime.RECOVERY
    assert d2.changed


def test_exit_deadband_holds_recovery_then_neutral_confirmation_can_exit():
    from crypto_bot.strategy.allocator import RegimeHysteresis

    rules = AllocationRules(
        recovery_confirm_days=1,
        neutral_confirm_days=2,
        fast_entry_gap_pct=0.05,
        fast_exit_gap_pct=-0.05,
    )
    h = RegimeHysteresis(rules)
    h.update(MarketRegime.RECOVERY, fast=ff(ema20=110.20, ema50=110.0))

    boundary = ff(ema20=109.98, ema50=110.0, slope=-0.001, mom=-0.001)
    held = h.update(MarketRegime.NEUTRAL, fast=boundary)
    assert held.effective_regime == MarketRegime.RECOVERY
    assert held.reason == "EXIT_DEADBAND_HOLD"

    clear_below = ff(ema20=109.80, ema50=110.0, slope=-0.001, mom=-0.001)
    first = h.update(MarketRegime.NEUTRAL, fast=clear_below)
    assert first.effective_regime == MarketRegime.RECOVERY
    second = h.update(MarketRegime.NEUTRAL, fast=clear_below)
    assert second.effective_regime == MarketRegime.NEUTRAL


def test_slow_bull_with_mixed_fast_layer_derisks_to_recovery_not_cash():
    rules = AllocationRules(fast_entry_gap_pct=0.05, fast_exit_gap_pct=-0.05)
    slow_bull = f()
    mixed_fast = ff(close=111, ema20=110.01, ema50=110, slope=0.0001, mom=0.0001)
    assert classify_dual_regime(slow_bull, mixed_fast, rules) == MarketRegime.RECOVERY
