from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from statistics import mean, pstdev

from crypto_bot.market.binance import Candle
from crypto_bot.market.resample import resample_candles
from crypto_bot.strategy.allocator import (
    AllocationRules,
    DailyFeature,
    FastFeature,
    MarketRegime,
    RankedAsset,
    RegimeHysteresis,
    build_daily_features,
    build_fast_features,
    classify_dual_regime,
    rank_assets,
    target_weights,
)

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class AllocationLeg:
    time: int
    side: str
    symbol: str
    market_price: float
    execution_price: float
    market_notional: float
    fee: float
    slippage_cost: float


@dataclass(frozen=True, slots=True)
class AllocationEvent:
    time: int
    regime: str
    raw_regime: str
    previous_regime: str | None
    trigger: str
    selected: tuple[str, ...]
    target_exposure: float
    ranked: tuple[RankedAsset, ...]
    legs: tuple[AllocationLeg, ...]
    equity_before: float
    equity_after_trades: float


@dataclass(frozen=True, slots=True)
class AllocatorBacktestResult:
    initial_cash: float
    final_equity: float
    return_pct: float
    annualized_return_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float
    calmar_ratio: float | None
    daily_profit_factor: float | None
    total_fees: float
    total_slippage_cost: float
    total_costs: float
    total_turnover: float
    rebalance_events: int
    regime_change_events: int
    raw_regime_changes: int
    stabilized_regime_changes: int
    filtered_regime_changes: int
    hysteresis_hold_days: int
    reentry_blocked_days: int
    trade_legs: int
    average_exposure_pct: float
    effective_start_time: int
    effective_end_time: int
    events: tuple[AllocationEvent, ...]
    legs: tuple[AllocationLeg, ...]
    equity_curve: tuple[tuple[int, float], ...]
    regime_days: tuple[tuple[str, int], ...]
    raw_regime_days: tuple[tuple[str, int], ...]


class AllocatorBacktestEngine:
    def __init__(
        self,
        *,
        initial_cash: float = 10.0,
        fee_rate: float = 0.001,
        slippage_rate: float = 0.0005,
        rules: AllocationRules | None = None,
        trade_start_time: int | None = None,
        trade_end_time: int | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if fee_rate < 0 or slippage_rate < 0:
            raise ValueError("fee/slippage cannot be negative")
        self.initial_cash = float(initial_cash)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.rules = rules or AllocationRules()
        self.trade_start_time = trade_start_time
        self.trade_end_time = trade_end_time

    def run(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        btc_candles: list[Candle],
    ) -> AllocatorBacktestResult:
        if not candles_by_symbol:
            raise ValueError("At least one trade symbol is required")

        daily_by_symbol = {
            symbol: resample_candles(rows, "1d")
            for symbol, rows in candles_by_symbol.items()
        }
        btc_daily = resample_candles(btc_candles, "1d")
        btc_4h = resample_candles(btc_candles, "4h")
        if len(btc_daily) < 205:
            raise ValueError("BTC needs at least 205 complete daily candles")
        if len(btc_4h) < 55:
            raise ValueError("BTC needs at least 55 complete 4H candles")
        if any(len(rows) < 205 for rows in daily_by_symbol.values()):
            raise ValueError("Every trade symbol needs at least 205 complete daily candles")

        by_time = {
            symbol: {c.open_time: c for c in rows}
            for symbol, rows in daily_by_symbol.items()
        }
        btc_by_time = {c.open_time: c for c in btc_daily}
        common_times = sorted(
            set(btc_by_time).intersection(*(set(rows) for rows in by_time.values()))
        )
        if len(common_times) < 205:
            raise ValueError("Not enough aligned complete daily candles")

        btc_features = build_daily_features(btc_daily)
        fast_features = build_fast_features(btc_4h)
        fast_times = sorted(fast_features)
        features_by_symbol = {
            symbol: build_daily_features(rows)
            for symbol, rows in daily_by_symbol.items()
        }

        time_index = {t: i for i, t in enumerate(common_times)}
        # Run the regime state machine through all available pre-evaluation days.
        # Walk-forward folds must not restart the market regime from NEUTRAL at
        # each fold boundary; that creates artificial boundary sensitivity.
        # Pre-start days update only state, never portfolio cash/positions.
        state_times = [
            t for t in common_times[1:]
            if self.trade_end_time is None or t <= self.trade_end_time
        ]
        state_times = [
            t for t in state_times
            if common_times[time_index[t] - 1] in btc_features
            and self._latest_fast_feature_before(t, fast_times, fast_features) is not None
        ]
        candidate_times = [
            t for t in state_times
            if self.trade_start_time is None or t >= self.trade_start_time
        ]
        if not candidate_times:
            raise ValueError("No allocator evaluation days after indicator warmup")

        cash = self.initial_cash
        positions: dict[str, float] = {}
        legs: list[AllocationLeg] = []
        events: list[AllocationEvent] = []
        equity_curve: list[tuple[int, float]] = []
        regime_counts: Counter[str] = Counter()
        raw_regime_counts: Counter[str] = Counter()
        exposure_samples: list[float] = []
        total_fees = 0.0
        total_slippage = 0.0
        total_turnover = 0.0
        previous_regime: MarketRegime | None = None
        previous_raw_regime: MarketRegime | None = None
        regime_change_events = 0
        raw_regime_changes = 0
        stabilized_regime_changes = 0
        hysteresis_hold_days = 0
        reentry_blocked_days = 0
        hysteresis = RegimeHysteresis(self.rules)

        effective_start = candidate_times[0]
        effective_end = candidate_times[-1]

        for t in state_times:
            idx = time_index[t]
            signal_time = common_times[idx - 1]
            btc_feature = btc_features.get(signal_time)
            fast_feature = self._latest_fast_feature_before(t, fast_times, fast_features)
            if btc_feature is None or fast_feature is None:
                continue

            symbol_features: dict[str, DailyFeature] = {}
            for symbol, fmap in features_by_symbol.items():
                feature = fmap.get(signal_time)
                if feature is not None:
                    symbol_features[symbol] = feature

            raw_regime = classify_dual_regime(btc_feature, fast_feature, self.rules)
            decision = hysteresis.update(raw_regime, fast=fast_feature)
            regime = decision.effective_regime

            # State-only warmup before a fold/evaluation window. This preserves
            # regime continuity without leaking pre-window PnL or positions.
            in_trade_window = self.trade_start_time is None or t >= self.trade_start_time
            if not in_trade_window:
                previous_regime = regime
                previous_raw_regime = raw_regime
                continue

            raw_regime_counts[raw_regime.value] += 1
            regime_counts[regime.value] += 1
            if previous_raw_regime is not None and raw_regime != previous_raw_regime:
                raw_regime_changes += 1
            if decision.changed:
                stabilized_regime_changes += 1
            if decision.suppressed:
                hysteresis_hold_days += 1
            if decision.reentry_blocked:
                reentry_blocked_days += 1
            ranked = rank_assets(symbol_features, regime)

            day = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
            is_weekly_rebalance = day.weekday() == self.rules.rebalance_weekday
            regime_changed = previous_regime is not None and regime != previous_regime
            defensive_exit = (
                self.rules.emergency_bear_exit
                and regime in {MarketRegime.NEUTRAL, MarketRegime.BEAR}
                and bool(positions)
            )
            transition_rebalance = self.rules.rebalance_on_regime_change and regime_changed

            open_prices = {symbol: by_time[symbol][t].open for symbol in by_time}
            close_prices = {symbol: by_time[symbol][t].close for symbol in by_time}

            trigger: str | None = None
            if t == effective_start:
                # A fold starts from cash, but the regime itself was warmed using
                # prior completed candles. Align the fresh portfolio to that
                # already-known state on the first evaluation open.
                trigger = "START"
            elif defensive_exit:
                trigger = "DEFENSIVE"
            elif transition_rebalance:
                trigger = "REGIME_CHANGE"
            elif is_weekly_rebalance:
                trigger = "WEEKLY"

            if trigger is not None:
                weights = target_weights(regime, ranked, self.rules)
                equity_before = self._equity(cash, positions, open_prices)
                event_legs, cash, positions = self._rebalance(
                    t=t,
                    regime=regime,
                    weights=weights,
                    ranked=ranked,
                    cash=cash,
                    positions=positions,
                    prices=open_prices,
                )
                if event_legs:
                    equity_after = self._equity(cash, positions, open_prices)
                    selected = tuple(weights.keys())
                    events.append(
                        AllocationEvent(
                            time=t,
                            regime=regime.value,
                            raw_regime=raw_regime.value,
                            previous_regime=previous_regime.value if previous_regime is not None else None,
                            trigger=trigger,
                            selected=selected,
                            target_exposure=sum(weights.values()),
                            ranked=ranked,
                            legs=tuple(event_legs),
                            equity_before=equity_before,
                            equity_after_trades=equity_after,
                        )
                    )
                    if regime_changed:
                        regime_change_events += 1
                    legs.extend(event_legs)
                    total_fees += sum(x.fee for x in event_legs)
                    total_slippage += sum(x.slippage_cost for x in event_legs)
                    total_turnover += sum(x.market_notional for x in event_legs)

            close_equity = self._equity(cash, positions, close_prices)
            invested = sum(positions.get(s, 0.0) * close_prices[s] for s in positions)
            exposure_samples.append((invested / close_equity) if close_equity > 0 else 0.0)
            equity_curve.append((t, close_equity))
            previous_regime = regime
            previous_raw_regime = raw_regime

        if not equity_curve:
            raise ValueError("Allocator produced no equity observations")

        final_equity = equity_curve[-1][1]
        return_pct = (final_equity / self.initial_cash - 1.0) * 100.0
        days = max(1.0, (effective_end - effective_start) / DAY_MS + 1.0)
        annualized = ((final_equity / self.initial_cash) ** (365.0 / days) - 1.0) * 100.0
        curve_values = [self.initial_cash, *[v for _, v in equity_curve]]
        max_dd = self._max_drawdown(curve_values)
        daily_returns = [
            curve_values[i] / curve_values[i - 1] - 1.0
            for i in range(1, len(curve_values))
            if curve_values[i - 1] > 0
        ]
        volatility = pstdev(daily_returns) * sqrt(365.0) * 100.0 if len(daily_returns) >= 2 else 0.0
        calmar = annualized / abs(max_dd) if max_dd < 0 else None
        daily_pf = self._profit_factor_from_changes(curve_values)

        return AllocatorBacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            return_pct=return_pct,
            annualized_return_pct=annualized,
            max_drawdown_pct=max_dd,
            annualized_volatility_pct=volatility,
            calmar_ratio=calmar,
            daily_profit_factor=daily_pf,
            total_fees=total_fees,
            total_slippage_cost=total_slippage,
            total_costs=total_fees + total_slippage,
            total_turnover=total_turnover,
            rebalance_events=len(events),
            regime_change_events=regime_change_events,
            raw_regime_changes=raw_regime_changes,
            stabilized_regime_changes=stabilized_regime_changes,
            filtered_regime_changes=max(0, raw_regime_changes - stabilized_regime_changes),
            hysteresis_hold_days=hysteresis_hold_days,
            reentry_blocked_days=reentry_blocked_days,
            trade_legs=len(legs),
            average_exposure_pct=mean(exposure_samples) * 100.0 if exposure_samples else 0.0,
            effective_start_time=effective_start,
            effective_end_time=effective_end,
            events=tuple(events),
            legs=tuple(legs),
            equity_curve=tuple(equity_curve),
            regime_days=tuple(sorted(regime_counts.items())),
            raw_regime_days=tuple(sorted(raw_regime_counts.items())),
        )

    @staticmethod
    def _latest_fast_feature_before(
        t: int,
        fast_times: list[int],
        fast_features: dict[int, FastFeature],
    ) -> FastFeature | None:
        pos = bisect_left(fast_times, t) - 1
        if pos < 0:
            return None
        return fast_features[fast_times[pos]]

    def _rebalance(
        self,
        *,
        t: int,
        regime: MarketRegime,
        weights: dict[str, float],
        ranked: tuple[RankedAsset, ...],
        cash: float,
        positions: dict[str, float],
        prices: dict[str, float],
    ) -> tuple[list[AllocationLeg], float, dict[str, float]]:
        positions = dict(positions)
        equity = self._equity(cash, positions, prices)
        if equity <= 0:
            return [], cash, positions

        current_weights = {
            s: positions.get(s, 0.0) * prices[s] / equity
            for s in positions
            if positions.get(s, 0.0) > 0
        }
        union = set(current_weights) | set(weights)
        if union and all(
            abs(current_weights.get(s, 0.0) - weights.get(s, 0.0)) <= self.rules.rebalance_band
            for s in union
        ):
            return [], cash, positions
        if not union:
            return [], cash, positions

        out: list[AllocationLeg] = []
        target_values = {s: equity * w for s, w in weights.items()}

        # Sell first so rotations and defensive reductions are funded.
        for symbol in list(positions):
            qty = positions.get(symbol, 0.0)
            if qty <= 0:
                continue
            market_price = prices[symbol]
            current_value = qty * market_price
            target_value = target_values.get(symbol, 0.0)
            excess = max(0.0, current_value - target_value)
            sell_all = target_value == 0.0
            if not sell_all and excess < self.rules.min_order_notional:
                continue
            sell_market_notional = current_value if sell_all else excess
            sell_qty = min(qty, sell_market_notional / market_price)
            if sell_qty <= 0:
                continue
            exec_price = market_price * (1.0 - self.slippage_rate)
            exec_notional = sell_qty * exec_price
            fee = exec_notional * self.fee_rate
            slippage = sell_qty * (market_price - exec_price)
            cash += exec_notional - fee
            remaining = qty - sell_qty
            if remaining * market_price < 1e-10:
                positions.pop(symbol, None)
            else:
                positions[symbol] = remaining
            out.append(
                AllocationLeg(
                    t,
                    "SELL",
                    symbol,
                    market_price,
                    exec_price,
                    sell_qty * market_price,
                    fee,
                    slippage,
                )
            )

        # Buy strongest targets first. Conservative cash accounting includes
        # slippage + fee, so the default $4.90 minimum leaves room for two legs
        # from a $10 account.
        rank_order = {asset.symbol: i for i, asset in enumerate(ranked)}
        for symbol in sorted(weights, key=lambda s: (rank_order.get(s, 10_000), s)):
            market_price = prices[symbol]
            target_value = target_values[symbol]
            current_value = positions.get(symbol, 0.0) * market_price
            missing = max(0.0, target_value - current_value)
            if missing < self.rules.min_order_notional:
                continue
            max_market_notional = cash / ((1.0 + self.slippage_rate) * (1.0 + self.fee_rate))
            buy_market_notional = min(missing, max_market_notional)
            if buy_market_notional < self.rules.min_order_notional:
                continue
            qty = buy_market_notional / market_price
            exec_price = market_price * (1.0 + self.slippage_rate)
            exec_notional = qty * exec_price
            fee = exec_notional * self.fee_rate
            total_cash = exec_notional + fee
            if total_cash > cash + 1e-10:
                continue
            slippage = qty * (exec_price - market_price)
            cash -= total_cash
            positions[symbol] = positions.get(symbol, 0.0) + qty
            out.append(
                AllocationLeg(
                    t,
                    "BUY",
                    symbol,
                    market_price,
                    exec_price,
                    buy_market_notional,
                    fee,
                    slippage,
                )
            )

        cash = max(cash, 0.0)
        return out, cash, positions

    @staticmethod
    def _equity(cash: float, positions: dict[str, float], prices: dict[str, float]) -> float:
        return cash + sum(qty * prices[symbol] for symbol, qty in positions.items())

    @staticmethod
    def _max_drawdown(curve: list[float]) -> float:
        peak = curve[0]
        worst = 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, (value / peak - 1.0) * 100.0)
        return worst

    @staticmethod
    def _profit_factor_from_changes(curve: list[float]) -> float | None:
        changes = [curve[i] - curve[i - 1] for i in range(1, len(curve))]
        gains = sum(x for x in changes if x > 0)
        losses = abs(sum(x for x in changes if x < 0))
        if losses > 0:
            return gains / losses
        if gains > 0:
            return float("inf")
        return None
