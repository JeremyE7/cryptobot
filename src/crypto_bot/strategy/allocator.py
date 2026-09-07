from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from crypto_bot.indicators.core import ema
from crypto_bot.market.binance import Candle


class MarketRegime(str, Enum):
    """Dual-speed market state.

    BULL remains as a backwards-compatible alias for STRONG_BULL.
    """

    STRONG_BULL = "STRONG_BULL"
    BULL = "STRONG_BULL"
    RECOVERY = "RECOVERY"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"


@dataclass(frozen=True, slots=True)
class DailyFeature:
    time: int
    close: float
    ema50: float
    ema200: float
    ema50_slope_5: float
    return_30: float
    return_90: float
    trend_strength: float


@dataclass(frozen=True, slots=True)
class FastFeature:
    time: int
    close: float
    ema20: float
    ema50: float
    ema50_slope_3: float
    momentum_3: float

    @property
    def ema_gap_pct(self) -> float:
        """EMA20/EMA50 separation in percentage points (0.10 == 0.10%)."""
        if self.ema50 <= 0:
            return 0.0
        return (self.ema20 / self.ema50 - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class RankedAsset:
    symbol: str
    score: float
    return_30: float
    return_90: float
    trend_strength: float


@dataclass(frozen=True, slots=True)
class AllocationRules:
    # STRONG_BULL: diversify between two leaders. RECOVERY: half-size in one.
    top_k: int = 2
    recovery_top_k: int = 1
    bull_exposure: float = 1.0
    recovery_exposure: float = 0.5
    neutral_exposure: float = 0.0
    bear_exposure: float = 0.0
    rebalance_weekday: int = 0  # Monday UTC
    rebalance_band: float = 0.10
    # Slightly below $5 so a $10 account can actually fund two ~50/50 legs
    # after conservative fee/slippage debits.
    min_order_notional: float = 4.90
    emergency_bear_exit: bool = True
    rebalance_on_regime_change: bool = True
    # Asymmetric hysteresis. Risk-on is intentionally fast; ambiguous risk-off
    # requires persistence. BEAR always bypasses confirmation.
    recovery_confirm_days: int = 1
    neutral_confirm_days: int = 2
    strong_bull_confirm_days: int = 1
    recovery_reentry_lockout_days: int = 0
    # Fast EMA deadband in percentage points. A new risk-on state must clear the
    # positive entry gap. An already-active RECOVERY state is not forced out by
    # tiny fast-layer oscillations until the gap clears the negative exit side.
    fast_entry_gap_pct: float = 0.10
    fast_exit_gap_pct: float = -0.10
    # When the slow daily trend is still structurally bullish but the 4H layer
    # loses full confirmation, de-risk to RECOVERY (50%) instead of jumping from
    # 100% exposure straight to cash.
    slow_bull_mixed_to_recovery: bool = True

    def __post_init__(self) -> None:
        if self.top_k <= 0:
            raise ValueError("top_k must be > 0")
        if self.recovery_top_k <= 0:
            raise ValueError("recovery_top_k must be > 0")
        for name, value in (
            ("bull_exposure", self.bull_exposure),
            ("recovery_exposure", self.recovery_exposure),
            ("neutral_exposure", self.neutral_exposure),
            ("bear_exposure", self.bear_exposure),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if not 0 <= self.rebalance_weekday <= 6:
            raise ValueError("rebalance_weekday must be 0..6")
        if not 0.0 <= self.rebalance_band <= 1.0:
            raise ValueError("rebalance_band must be between 0 and 1")
        if self.min_order_notional < 0:
            raise ValueError("min_order_notional cannot be negative")
        for name, value in (
            ("recovery_confirm_days", self.recovery_confirm_days),
            ("neutral_confirm_days", self.neutral_confirm_days),
            ("strong_bull_confirm_days", self.strong_bull_confirm_days),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.recovery_reentry_lockout_days < 0:
            raise ValueError("recovery_reentry_lockout_days cannot be negative")
        if self.fast_exit_gap_pct > self.fast_entry_gap_pct:
            raise ValueError("fast_exit_gap_pct must be <= fast_entry_gap_pct")
        if not -5.0 <= self.fast_exit_gap_pct <= 5.0:
            raise ValueError("fast_exit_gap_pct looks unreasonable")
        if not -5.0 <= self.fast_entry_gap_pct <= 5.0:
            raise ValueError("fast_entry_gap_pct looks unreasonable")


@dataclass(frozen=True, slots=True)
class HysteresisDecision:
    raw_regime: MarketRegime
    effective_regime: MarketRegime
    changed: bool
    suppressed: bool
    reentry_blocked: bool
    reason: str
    pending_regime: MarketRegime | None
    pending_days: int
    lockout_remaining: int


class RegimeHysteresis:
    """Stateful confirmation layer for the raw dual-speed regime.

    Design goals:
    - BEAR is immediate: never delay a strong defensive signal.
    - RECOVERY/STRONG_BULL entries need persistence before adding risk.
    - RECOVERY/STRONG_BULL -> NEUTRAL needs persistence so one noisy day does
      not force a sell/rebuy cycle.
    - After a confirmed risk-off exit, a short lockout prevents immediate
      RECOVERY re-entry during boundary noise.

    The controller sees one raw regime per completed UTC day and therefore does
    not introduce look-ahead.
    """

    def __init__(self, rules: AllocationRules) -> None:
        self.rules = rules
        self.current = MarketRegime.NEUTRAL
        self.pending: MarketRegime | None = None
        self.pending_days = 0
        self.lockout_remaining = 0
        self.initialized = False

    def update(self, raw: MarketRegime, *, fast: FastFeature | None = None) -> HysteresisDecision:
        previous = self.current
        reentry_blocked = False

        # Lockout counts full observation days after a confirmed risk-off exit.
        if self.lockout_remaining > 0:
            self.lockout_remaining -= 1

        if not self.initialized:
            self.initialized = True
            if raw in {MarketRegime.NEUTRAL, MarketRegime.BEAR}:
                self.current = raw
                self._reset_pending()
                return self._decision(raw, previous, False, False, "INITIAL_RISK_OFF")
            # Conservative startup: require normal risk-on confirmation.
            self.current = MarketRegime.NEUTRAL

        # Deadband is deliberately asymmetric: it can delay adding risk when the
        # EMA20/EMA50 fast gap is barely positive, and can keep an existing
        # RECOVERY allocation alive through tiny negative/positive oscillations.
        # It never delays a raw BEAR.
        if fast is not None and raw in {MarketRegime.RECOVERY, MarketRegime.STRONG_BULL}:
            if self.current in {MarketRegime.NEUTRAL, MarketRegime.BEAR} and fast.ema_gap_pct < self.rules.fast_entry_gap_pct:
                self._reset_pending()
                return self._decision(raw, previous, True, False, "ENTRY_DEADBAND")

        if (
            fast is not None
            and self.current == MarketRegime.RECOVERY
            and raw == MarketRegime.NEUTRAL
            and fast.ema_gap_pct > self.rules.fast_exit_gap_pct
        ):
            self._reset_pending()
            return self._decision(raw, previous, True, False, "EXIT_DEADBAND_HOLD")

        if raw == MarketRegime.BEAR:
            self.current = MarketRegime.BEAR
            self._reset_pending()
            # A BEAR exit is itself enough of a reset; subsequent risk-on still
            # requires confirmation, so no extra lockout is necessary here.
            self.lockout_remaining = 0
            return self._decision(raw, previous, False, False, "BEAR_IMMEDIATE")

        # Moving from BEAR to NEUTRAL changes the label but keeps exposure at 0%.
        # Do it immediately so the state machine can begin collecting recovery
        # confirmations without pretending the raw market is still BEAR.
        if self.current == MarketRegime.BEAR and raw == MarketRegime.NEUTRAL:
            self.current = MarketRegime.NEUTRAL
            self._reset_pending()
            return self._decision(raw, previous, False, False, "BEAR_TO_NEUTRAL")

        if raw == self.current:
            self._reset_pending()
            return self._decision(raw, previous, False, False, "STABLE")

        # Faster de-risking: if a structural STRONG_BULL has already degraded to
        # a genuine RECOVERY state, cut to partial exposure immediately.
        if self.current == MarketRegime.STRONG_BULL and raw == MarketRegime.RECOVERY:
            self.current = MarketRegime.RECOVERY
            self._reset_pending()
            return self._decision(raw, previous, False, False, "DE_RISK_TO_RECOVERY")

        # Risk-on re-entry can be temporarily blocked after a confirmed exit.
        if self.current == MarketRegime.NEUTRAL and raw in {MarketRegime.RECOVERY, MarketRegime.STRONG_BULL}:
            if self.lockout_remaining > 0:
                reentry_blocked = True
                self._reset_pending()
                return self._decision(raw, previous, True, True, "REENTRY_LOCKOUT")
            required = (
                self.rules.recovery_confirm_days
                if raw == MarketRegime.RECOVERY
                else self.rules.strong_bull_confirm_days
            )
            return self._confirm(raw, required, previous, "RISK_ON_CONFIRMED")

        if self.current == MarketRegime.BEAR and raw in {MarketRegime.RECOVERY, MarketRegime.STRONG_BULL}:
            required = (
                self.rules.recovery_confirm_days
                if raw == MarketRegime.RECOVERY
                else self.rules.strong_bull_confirm_days
            )
            return self._confirm(raw, required, previous, "BEAR_RECOVERY_CONFIRMED")

        # RECOVERY -> STRONG_BULL adds risk, so require confirmation.
        if self.current == MarketRegime.RECOVERY and raw == MarketRegime.STRONG_BULL:
            return self._confirm(raw, self.rules.strong_bull_confirm_days, previous, "BULL_CONFIRMATION")

        # Any risk-on state -> NEUTRAL: tolerate one noisy raw day by default.
        if self.current in {MarketRegime.RECOVERY, MarketRegime.STRONG_BULL} and raw == MarketRegime.NEUTRAL:
            decision = self._confirm(raw, self.rules.neutral_confirm_days, previous, "NEUTRAL_CONFIRMED")
            if decision.changed and decision.effective_regime == MarketRegime.NEUTRAL:
                self.lockout_remaining = self.rules.recovery_reentry_lockout_days
                decision = HysteresisDecision(
                    raw_regime=decision.raw_regime,
                    effective_regime=decision.effective_regime,
                    changed=decision.changed,
                    suppressed=decision.suppressed,
                    reentry_blocked=decision.reentry_blocked,
                    reason=decision.reason,
                    pending_regime=decision.pending_regime,
                    pending_days=decision.pending_days,
                    lockout_remaining=self.lockout_remaining,
                )
            return decision

        # Fallback for uncommon mixed transitions: confirm the target according
        # to whether it adds or removes risk.
        if raw == MarketRegime.NEUTRAL:
            return self._confirm(raw, self.rules.neutral_confirm_days, previous, "NEUTRAL_CONFIRMED")
        required = self.rules.recovery_confirm_days if raw == MarketRegime.RECOVERY else self.rules.strong_bull_confirm_days
        return self._confirm(raw, required, previous, "REGIME_CONFIRMED")

    def _confirm(
        self,
        target: MarketRegime,
        required: int,
        previous: MarketRegime,
        reason: str,
    ) -> HysteresisDecision:
        if self.pending == target:
            self.pending_days += 1
        else:
            self.pending = target
            self.pending_days = 1

        if self.pending_days >= required:
            self.current = target
            self._reset_pending()
            return self._decision(target, previous, False, False, reason)

        return self._decision(target, previous, True, False, f"WAIT_{target.value}_{self.pending_days}/{required}")

    def _reset_pending(self) -> None:
        self.pending = None
        self.pending_days = 0

    def _decision(
        self,
        raw: MarketRegime,
        previous: MarketRegime,
        suppressed: bool,
        reentry_blocked: bool,
        reason: str,
    ) -> HysteresisDecision:
        return HysteresisDecision(
            raw_regime=raw,
            effective_regime=self.current,
            changed=self.current != previous,
            suppressed=suppressed or self.current != raw,
            reentry_blocked=reentry_blocked,
            reason=reason,
            pending_regime=self.pending,
            pending_days=self.pending_days,
            lockout_remaining=self.lockout_remaining,
        )


def build_daily_features(candles: list[Candle]) -> dict[int, DailyFeature]:
    """Daily features built only from completed candles."""
    ordered = sorted(candles, key=lambda c: c.open_time)
    closes = [c.close for c in ordered]
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    out: dict[int, DailyFeature] = {}

    for i, candle in enumerate(ordered):
        if i < 200 or i < 90 or i < 5:
            continue
        if e50[i] is None or e200[i] is None or e50[i - 5] is None:
            continue
        close = closes[i]
        prev30 = closes[i - 30]
        prev90 = closes[i - 90]
        if min(close, prev30, prev90) <= 0:
            continue
        ema50_now = float(e50[i])
        ema200_now = float(e200[i])
        ema50_prev = float(e50[i - 5])
        if ema50_prev <= 0 or ema200_now <= 0:
            continue
        values = (
            close,
            ema50_now,
            ema200_now,
            ema50_now / ema50_prev - 1.0,
            close / prev30 - 1.0,
            close / prev90 - 1.0,
            ema50_now / ema200_now - 1.0,
        )
        if not all(isfinite(v) for v in values):
            continue
        out[candle.open_time] = DailyFeature(
            time=candle.open_time,
            close=close,
            ema50=ema50_now,
            ema200=ema200_now,
            ema50_slope_5=values[3],
            return_30=values[4],
            return_90=values[5],
            trend_strength=values[6],
        )
    return out


def build_fast_features(candles_4h: list[Candle]) -> dict[int, FastFeature]:
    """4H features used as the fast risk/recovery layer.

    The engine only consumes a feature after its 4H candle has completed, so the
    current day's allocation never sees a partial/future 4H candle.
    """
    ordered = sorted(candles_4h, key=lambda c: c.open_time)
    closes = [c.close for c in ordered]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    out: dict[int, FastFeature] = {}
    for i, candle in enumerate(ordered):
        if i < 50 or i < 3:
            continue
        if e20[i] is None or e50[i] is None or e50[i - 3] is None:
            continue
        close = closes[i]
        prev3 = closes[i - 3]
        ema50_now = float(e50[i])
        ema50_prev = float(e50[i - 3])
        if min(close, prev3, ema50_now, ema50_prev) <= 0:
            continue
        vals = (
            close,
            float(e20[i]),
            ema50_now,
            ema50_now / ema50_prev - 1.0,
            close / prev3 - 1.0,
        )
        if not all(isfinite(v) for v in vals):
            continue
        out[candle.open_time] = FastFeature(
            time=candle.open_time,
            close=close,
            ema20=vals[1],
            ema50=vals[2],
            ema50_slope_3=vals[3],
            momentum_3=vals[4],
        )
    return out


def classify_btc_regime(feature: DailyFeature) -> MarketRegime:
    """Legacy slow-only regime classification kept for compatibility."""
    if feature.close > feature.ema200 and feature.ema50 > feature.ema200 and feature.ema50_slope_5 > 0:
        return MarketRegime.STRONG_BULL
    if feature.close < feature.ema200 and feature.ema50 < feature.ema200:
        return MarketRegime.BEAR
    return MarketRegime.NEUTRAL


def is_fast_bull(feature: FastFeature, min_gap_pct: float = 0.0) -> bool:
    return (
        feature.close > feature.ema50
        and feature.ema20 > feature.ema50
        and feature.ema_gap_pct >= min_gap_pct
        and feature.ema50_slope_3 > 0
        and feature.momentum_3 > 0
    )


def is_fast_bear(feature: FastFeature, max_gap_pct: float = 0.0) -> bool:
    return (
        feature.close < feature.ema50
        and feature.ema20 < feature.ema50
        and feature.ema_gap_pct <= max_gap_pct
        and feature.ema50_slope_3 < 0
        and feature.momentum_3 < 0
    )


def classify_dual_regime(
    daily: DailyFeature,
    fast: FastFeature,
    rules: AllocationRules | None = None,
) -> MarketRegime:
    """Combine slow structural trend with a faster 4H confirmation/recovery layer.

    - STRONG_BULL requires both layers bullish.
    - RECOVERY deliberately permits partial exposure before the very slow
      EMA50/EMA200 daily confirmation, but only after price has recovered above
      the daily EMA50 and the 4H layer is bullish.
    - BEAR requires a structurally weak daily market and fast bearish confirmation.
    - Mixed signals are NEUTRAL (cash by default), providing a faster risk-off
      response than the old daily-only allocator.
    """
    rules = rules or AllocationRules()
    slow = classify_btc_regime(daily)
    fast_bull = is_fast_bull(fast, rules.fast_entry_gap_pct)
    fast_bear = is_fast_bear(fast, rules.fast_exit_gap_pct)

    if slow == MarketRegime.STRONG_BULL and fast_bull:
        return MarketRegime.STRONG_BULL

    # The slow bull remains valuable information even when the fast layer is
    # mixed. Reduce exposure rather than binary-switching from 100% to cash.
    if (
        slow == MarketRegime.STRONG_BULL
        and rules.slow_bull_mixed_to_recovery
        and not fast_bear
    ):
        return MarketRegime.RECOVERY

    recovery = (
        slow != MarketRegime.STRONG_BULL
        and fast_bull
        and daily.close > daily.ema50
        and daily.ema50_slope_5 > 0
    )
    if recovery:
        return MarketRegime.RECOVERY

    if slow == MarketRegime.BEAR and fast_bear:
        return MarketRegime.BEAR

    return MarketRegime.NEUTRAL


def is_trade_candidate(feature: DailyFeature) -> bool:
    return (
        feature.close > feature.ema200
        and feature.ema50 > feature.ema200
        and feature.ema50_slope_5 > 0
        and feature.return_30 > 0
    )


def is_recovery_candidate(feature: DailyFeature) -> bool:
    return (
        feature.close > feature.ema50
        and feature.ema50_slope_5 > 0
        and feature.return_30 > 0
    )


def _percentile_scores(items: list[tuple[str, float]]) -> dict[str, float]:
    if not items:
        return {}
    if len(items) == 1:
        return {items[0][0]: 100.0}

    sorted_items = sorted(items, key=lambda x: x[1])
    scores: dict[str, float] = {}
    n = len(sorted_items)
    i = 0
    while i < n:
        j = i + 1
        while j < n and sorted_items[j][1] == sorted_items[i][1]:
            j += 1
        avg_index = (i + (j - 1)) / 2.0
        percentile = avg_index / (n - 1) * 100.0
        for k in range(i, j):
            scores[sorted_items[k][0]] = percentile
        i = j
    return scores


def rank_assets(
    features: dict[str, DailyFeature],
    regime: MarketRegime = MarketRegime.STRONG_BULL,
) -> tuple[RankedAsset, ...]:
    predicate = is_recovery_candidate if regime == MarketRegime.RECOVERY else is_trade_candidate
    eligible = {symbol: f for symbol, f in features.items() if predicate(f)}
    if not eligible:
        return ()

    s30 = _percentile_scores([(s, f.return_30) for s, f in eligible.items()])
    s90 = _percentile_scores([(s, f.return_90) for s, f in eligible.items()])
    strend = _percentile_scores([(s, f.trend_strength) for s, f in eligible.items()])

    ranked = [
        RankedAsset(
            symbol=symbol,
            score=(s30[symbol] + s90[symbol] + strend[symbol]) / 3.0,
            return_30=f.return_30,
            return_90=f.return_90,
            trend_strength=f.trend_strength,
        )
        for symbol, f in eligible.items()
    ]
    ranked.sort(key=lambda x: (-x.score, -x.return_30, -x.return_90, x.symbol))
    return tuple(ranked)


def target_weights(
    regime: MarketRegime,
    ranked: tuple[RankedAsset, ...],
    rules: AllocationRules,
) -> dict[str, float]:
    if regime == MarketRegime.STRONG_BULL:
        exposure = rules.bull_exposure
        k = rules.top_k
    elif regime == MarketRegime.RECOVERY:
        exposure = rules.recovery_exposure
        k = rules.recovery_top_k
    elif regime == MarketRegime.NEUTRAL:
        exposure = rules.neutral_exposure
        k = 1
    else:
        exposure = rules.bear_exposure
        k = 0

    if exposure <= 0 or k <= 0 or not ranked:
        return {}
    selected = ranked[:k]
    weight = exposure / len(selected)
    return {asset.symbol: weight for asset in selected}
