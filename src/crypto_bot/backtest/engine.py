from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from typing import Callable, Iterable

from crypto_bot.indicators import IndicatorSnapshot, build_snapshots
from crypto_bot.market.binance import Candle
from crypto_bot.simulation.broker import SimulatedBroker
from crypto_bot.strategy import MomentumScoreStrategy

D = Decimal


@dataclass(frozen=True, slots=True)
class CandidateSignal:
    symbol: str
    score: int
    quality: float
    reasons: tuple[str, ...]
    volume_ratio: float
    momentum_3: float
    ema_gap_pct: float
    rsi14: float
    atr_pct: float
    quality_trend: float
    quality_momentum: float
    quality_volume: float
    quality_rsi: float
    quality_atr: float


@dataclass(frozen=True, slots=True)
class FeatureFilter:
    """Optional simple entry filter used by the research engine.

    All thresholds are applied to the fully closed signal candle.  A None
    bound means that side is unconstrained.
    """

    name: str = "none"
    min_volume_ratio: float | None = None
    max_volume_ratio: float | None = None
    min_momentum_3: float | None = None
    max_momentum_3: float | None = None
    min_ema_gap_pct: float | None = None
    max_ema_gap_pct: float | None = None
    min_rsi14: float | None = None
    max_rsi14: float | None = None
    min_atr_pct: float | None = None
    max_atr_pct: float | None = None

    def matches(self, candidate: "CandidateSignal") -> bool:
        checks = (
            (candidate.volume_ratio, self.min_volume_ratio, self.max_volume_ratio),
            (candidate.momentum_3, self.min_momentum_3, self.max_momentum_3),
            (candidate.ema_gap_pct, self.min_ema_gap_pct, self.max_ema_gap_pct),
            (candidate.rsi14, self.min_rsi14, self.max_rsi14),
            (candidate.atr_pct, self.min_atr_pct, self.max_atr_pct),
        )
        for value, low, high in checks:
            if low is not None and value < low:
                return False
            if high is not None and value > high:
                return False
        return True


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    signal_time: int
    selected_symbol: str
    selected_score: int
    selected_quality: float
    candidates: tuple[CandidateSignal, ...]

    @property
    def tie_count(self) -> int:
        return sum(1 for item in self.candidates if item.score == self.selected_score)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    entry_market_price: float
    exit_market_price: float
    score: int
    quality: float
    volume_ratio: float
    momentum_3: float
    ema_gap_pct: float
    rsi14: float
    atr_pct: float
    trade_budget: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    pnl: float
    return_pct: float
    reason: str


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    symbol: str
    start_price: float
    end_price: float
    return_pct: float
    final_value: float


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    label: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    profit_factor: float | None


@dataclass(frozen=True, slots=True)
class SignalAnalysis:
    base_candidates: int
    passed_quality: int
    rejected_quality: int
    quality_ge_60: int
    quality_ge_70: int
    quality_ge_80: int
    quality_ge_90: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_cash: float
    final_equity: float
    return_pct: float
    trades: tuple[ClosedTrade, ...]
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None
    max_drawdown_pct: float
    gross_pnl: float
    total_fees: float
    total_slippage_cost: float
    sharpe_like: float | None
    benchmarks: tuple[BenchmarkResult, ...]
    by_symbol: tuple[DiagnosticRow, ...]
    by_score: tuple[DiagnosticRow, ...]
    by_quality: tuple[DiagnosticRow, ...]
    by_exit: tuple[DiagnosticRow, ...]
    selections: tuple[SelectionDecision, ...]
    signal_analysis: SignalAnalysis
    cooldown_candles: int
    trade_size: float | None
    trade_percent: float | None
    min_quality: float

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.initial_cash

    @property
    def total_costs(self) -> float:
        return self.total_fees + self.total_slippage_cost

    @property
    def tied_selections(self) -> int:
        return sum(1 for decision in self.selections if decision.tie_count > 1)


class BacktestEngine:
    def __init__(
        self,
        *,
        initial_cash: float = 10.0,
        trade_size: float | None = 5.0,
        trade_percent: float | None = None,
        min_score: int = 80,
        min_quality: float = 0.0,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        fee_rate: float = 0.001,
        slippage_rate: float = 0.0005,
        cooldown_candles: int = 0,
        feature_filter: FeatureFilter | None = None,
        trade_start_time: int | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if trade_size is not None and trade_percent is not None:
            raise ValueError("Use either trade_size or trade_percent, not both")
        if trade_size is None and trade_percent is None:
            trade_size = 5.0
        if trade_size is not None:
            if trade_size <= 0:
                raise ValueError("trade_size must be > 0")
            if trade_size > initial_cash:
                raise ValueError("trade_size cannot exceed initial_cash")
        if trade_percent is not None and not 0 < trade_percent <= 100:
            raise ValueError("trade_percent must be > 0 and <= 100")
        if not 0 <= min_score <= 100:
            raise ValueError("min_score must be between 0 and 100")
        if not 0 <= min_quality <= 100:
            raise ValueError("min_quality must be between 0 and 100")
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("stop/take percentages must be > 0")
        if fee_rate < 0 or slippage_rate < 0:
            raise ValueError("fee/slippage rates cannot be negative")
        if cooldown_candles < 0:
            raise ValueError("cooldown_candles cannot be negative")

        self.initial_cash = initial_cash
        self.trade_size = trade_size
        self.trade_percent = trade_percent
        self.min_score = min_score
        self.min_quality = min_quality
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.cooldown_candles = cooldown_candles
        self.feature_filter = feature_filter
        self.trade_start_time = trade_start_time
        self.strategy = MomentumScoreStrategy()

    def run(self, candles_by_symbol: dict[str, list[Candle]]) -> BacktestResult:
        prepared = self._prepare(candles_by_symbol)
        if not prepared:
            raise ValueError("No symbols have enough candle data for backtesting")

        common_times = sorted(set.intersection(*(set(item["by_time"]) for item in prepared.values())))
        if len(common_times) < 60:
            raise ValueError("Need at least 60 aligned candles")

        broker = SimulatedBroker(
            initial_cash=str(self.initial_cash),
            fee_rate=str(self.fee_rate),
            slippage_rate=str(self.slippage_rate),
        )
        trades: list[ClosedTrade] = []
        selections: list[SelectionDecision] = []
        quality_values: list[float] = []
        equity_curve: list[float] = [self.initial_cash]
        open_meta: dict[str, object] | None = None
        pending_signal: CandidateSignal | None = None
        pending_signal_time: int | None = None
        last_exit_idx: int | None = None

        for idx, open_time in enumerate(common_times):
            candles_now = {symbol: data["by_time"][open_time] for symbol, data in prepared.items()}

            # Execute a signal from the PREVIOUS fully closed candle at this candle's open.
            if broker.position is None and pending_signal is not None:
                symbol = pending_signal.symbol
                candle = candles_now[symbol]
                budget = self._trade_budget(float(broker.cash))
                if budget > 0 and broker.cash >= D(str(budget)):
                    buy = broker.buy(symbol, str(candle.open), str(budget))
                    open_meta = {
                        "symbol": symbol,
                        "entry_time": open_time,
                        "signal_time": pending_signal_time,
                        "entry_market_price": float(candle.open),
                        "entry_price": float(buy.execution_price),
                        "buy_fee": float(buy.fee),
                        "score": pending_signal.score,
                        "quality": pending_signal.quality,
                        "volume_ratio": pending_signal.volume_ratio,
                        "momentum_3": pending_signal.momentum_3,
                        "ema_gap_pct": pending_signal.ema_gap_pct,
                        "rsi14": pending_signal.rsi14,
                        "atr_pct": pending_signal.atr_pct,
                        "reasons": pending_signal.reasons,
                        "trade_budget": budget,
                    }
                pending_signal = None
                pending_signal_time = None

            # Manage an existing position using only this candle's OHLC.
            if broker.position is not None and open_meta is not None:
                symbol = broker.position.symbol
                candle = candles_now[symbol]
                entry_execution = float(broker.position.entry_price)
                stop = entry_execution * (1.0 - self.stop_loss_pct)
                take = entry_execution * (1.0 + self.take_profit_pct)

                exit_market_price: float | None = None
                reason: str | None = None

                # Conservative assumption if both levels were crossed inside one candle:
                # assume the stop was hit first because intrabar ordering is unknown.
                if candle.low <= stop:
                    exit_market_price = stop
                    reason = "STOP"
                elif candle.high >= take:
                    exit_market_price = take
                    reason = "TAKE"

                if exit_market_price is not None:
                    trade = self._close_trade(
                        broker=broker,
                        open_meta=open_meta,
                        exit_time=open_time,
                        exit_market_price=exit_market_price,
                        reason=reason,
                    )
                    trades.append(trade)
                    open_meta = None
                    last_exit_idx = idx

            # Mark equity at candle close.
            if broker.position is None:
                equity_curve.append(float(broker.cash))
            else:
                mark = candles_now[broker.position.symbol].close
                equity_curve.append(float(broker.equity(str(mark))))

            # Create the next-candle signal from THIS fully closed candle.
            cooldown_ready = (
                last_exit_idx is None
                or idx - last_exit_idx > self.cooldown_candles
            )
            trading_window_open = self.trade_start_time is None or open_time >= self.trade_start_time
            if broker.position is None and cooldown_ready and trading_window_open and idx < len(common_times) - 1:
                candidates: list[CandidateSignal] = []
                for symbol, data in prepared.items():
                    candle_index = data["index_by_time"][open_time]
                    snapshot = data["snapshots"][candle_index]
                    if snapshot is None:
                        continue
                    signal = self.strategy.score(snapshot)
                    if signal.score >= self.min_score:
                        candidate = self._candidate(symbol, signal.score, signal.reasons, snapshot)
                        quality_values.append(candidate.quality)
                        if candidate.quality >= self.min_quality:
                            if self.feature_filter is None or self.feature_filter.matches(candidate):
                                candidates.append(candidate)

                if candidates:
                    ranked = tuple(sorted(candidates, key=self._candidate_rank))
                    selected = ranked[0]
                    selections.append(
                        SelectionDecision(
                            signal_time=open_time,
                            selected_symbol=selected.symbol,
                            selected_score=selected.score,
                            selected_quality=selected.quality,
                            candidates=ranked,
                        )
                    )
                    pending_signal = selected
                    pending_signal_time = open_time

        # Liquidate at the last available close so final equity is cash.
        if broker.position is not None and open_meta is not None:
            symbol = broker.position.symbol
            final_candle = prepared[symbol]["by_time"][common_times[-1]]
            trades.append(
                self._close_trade(
                    broker=broker,
                    open_meta=open_meta,
                    exit_time=common_times[-1],
                    exit_market_price=float(final_candle.close),
                    reason="END",
                )
            )
            equity_curve.append(float(broker.cash))

        final_equity = float(broker.cash)
        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl < 0)
        win_rate = (wins / len(trades) * 100.0) if trades else 0.0
        win_values = [trade.pnl for trade in trades if trade.pnl > 0]
        loss_values = [trade.pnl for trade in trades if trade.pnl < 0]
        avg_win = mean(win_values) if win_values else 0.0
        avg_loss = mean(loss_values) if loss_values else 0.0
        profit_factor = self._profit_factor(trades)

        trade_returns = [trade.return_pct / 100.0 for trade in trades]
        sharpe_like: float | None = None
        if len(trade_returns) >= 2:
            sigma = pstdev(trade_returns)
            if sigma > 0:
                sharpe_like = mean(trade_returns) / sigma * sqrt(len(trade_returns))

        gross_pnl = sum(trade.gross_pnl for trade in trades)
        total_fees = sum(trade.fees for trade in trades)
        total_slippage_cost = sum(trade.slippage_cost for trade in trades)
        benchmarks = self._benchmarks(prepared, common_times)

        return BacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            return_pct=(final_equity / self.initial_cash - 1.0) * 100.0,
            trades=tuple(trades),
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            max_drawdown_pct=self._max_drawdown(equity_curve),
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            total_slippage_cost=total_slippage_cost,
            sharpe_like=sharpe_like,
            benchmarks=benchmarks,
            by_symbol=self._aggregate(trades, key=lambda trade: trade.symbol),
            by_score=self._aggregate(trades, key=lambda trade: self._score_bucket(trade.score), score_sort=True),
            by_quality=self._aggregate(trades, key=lambda trade: self._quality_bucket(trade.quality), quality_sort=True),
            by_exit=self._aggregate(trades, key=lambda trade: trade.reason, exit_sort=True),
            selections=tuple(selections),
            signal_analysis=self._signal_analysis(quality_values),
            cooldown_candles=self.cooldown_candles,
            trade_size=self.trade_size,
            trade_percent=self.trade_percent,
            min_quality=self.min_quality,
        )

    def _trade_budget(self, cash: float) -> float:
        if self.trade_percent is not None:
            return min(cash, cash * self.trade_percent / 100.0)
        assert self.trade_size is not None
        return min(cash, self.trade_size)

    @staticmethod
    def _candidate(
        symbol: str,
        score: int,
        reasons: tuple[str, ...],
        snapshot: IndicatorSnapshot,
    ) -> CandidateSignal:
        ema_gap_pct = (snapshot.ema9 / snapshot.ema21 - 1.0) * 100.0 if snapshot.ema21 else 0.0
        quality = MomentumScoreStrategy().quality(snapshot)
        return CandidateSignal(
            symbol=symbol,
            score=score,
            quality=quality.total,
            reasons=reasons,
            volume_ratio=snapshot.volume_ratio,
            momentum_3=snapshot.momentum_3,
            ema_gap_pct=ema_gap_pct,
            rsi14=snapshot.rsi14,
            atr_pct=snapshot.atr_pct,
            quality_trend=quality.trend,
            quality_momentum=quality.momentum,
            quality_volume=quality.volume,
            quality_rsi=quality.rsi,
            quality_atr=quality.atr,
        )

    @staticmethod
    def _candidate_rank(candidate: CandidateSignal) -> tuple[float, float, float, float, str]:
        # v1.0 deliberately restores the v0.4 selector. The experimental
        # continuous quality score was useful diagnostically but was NOT
        # monotonic with profitability in our 180-day study, so it must not
        # influence live candidate selection.
        return (
            -float(candidate.score),
            -candidate.volume_ratio,
            -candidate.momentum_3,
            -candidate.ema_gap_pct,
            candidate.symbol,
        )

    def _close_trade(
        self,
        *,
        broker: SimulatedBroker,
        open_meta: dict[str, object],
        exit_time: int,
        exit_market_price: float,
        reason: str,
    ) -> ClosedTrade:
        symbol = broker.position.symbol if broker.position is not None else str(open_meta["symbol"])
        entry_market_price = float(open_meta["entry_market_price"])
        buy_fee = float(open_meta["buy_fee"])
        trade_budget = float(open_meta["trade_budget"])

        sell = broker.sell_all(str(exit_market_price))
        net_pnl = float(sell.realized_pnl or D("0"))
        sell_fee = float(sell.fee)
        fees = buy_fee + sell_fee

        # Counterfactual PnL using the exact budget used for this trade,
        # perfect fills and zero fees.
        ideal_quantity = trade_budget / entry_market_price
        gross_pnl = ideal_quantity * exit_market_price - trade_budget
        slippage_cost = gross_pnl - net_pnl - fees
        if abs(slippage_cost) < 1e-12:
            slippage_cost = 0.0

        return ClosedTrade(
            symbol=symbol,
            entry_time=int(open_meta["entry_time"]),
            exit_time=exit_time,
            entry_price=float(open_meta["entry_price"]),
            exit_price=float(sell.execution_price),
            entry_market_price=entry_market_price,
            exit_market_price=exit_market_price,
            score=int(open_meta["score"]),
            quality=float(open_meta["quality"]),
            volume_ratio=float(open_meta["volume_ratio"]),
            momentum_3=float(open_meta["momentum_3"]),
            ema_gap_pct=float(open_meta["ema_gap_pct"]),
            rsi14=float(open_meta["rsi14"]),
            atr_pct=float(open_meta["atr_pct"]),
            trade_budget=trade_budget,
            gross_pnl=gross_pnl,
            fees=fees,
            slippage_cost=slippage_cost,
            pnl=net_pnl,
            return_pct=(net_pnl / trade_budget) * 100.0,
            reason=reason,
        )

    def _prepare(self, candles_by_symbol: dict[str, list[Candle]]) -> dict[str, dict[str, object]]:
        prepared: dict[str, dict[str, object]] = {}
        for raw_symbol, candles in candles_by_symbol.items():
            if len(candles) < 60:
                continue
            symbol = raw_symbol.upper()
            ordered = sorted(candles, key=lambda c: c.open_time)
            snapshots = build_snapshots(
                [c.close for c in ordered],
                [c.volume for c in ordered],
                [c.high for c in ordered],
                [c.low for c in ordered],
            )
            by_time = {c.open_time: c for c in ordered}
            index_by_time = {c.open_time: i for i, c in enumerate(ordered)}
            prepared[symbol] = {
                "candles": ordered,
                "snapshots": snapshots,
                "by_time": by_time,
                "index_by_time": index_by_time,
            }
        return prepared

    def _benchmarks(self, prepared: dict[str, dict[str, object]], common_times: list[int]) -> tuple[BenchmarkResult, ...]:
        results: list[BenchmarkResult] = []
        first_time = next((t for t in common_times if self.trade_start_time is None or t >= self.trade_start_time), common_times[0])
        last_time = common_times[-1]
        for symbol, data in sorted(prepared.items()):
            first = data["by_time"][first_time]
            last = data["by_time"][last_time]
            start = float(first.open)
            end = float(last.close)
            return_pct = (end / start - 1.0) * 100.0
            results.append(
                BenchmarkResult(
                    symbol=symbol,
                    start_price=start,
                    end_price=end,
                    return_pct=return_pct,
                    final_value=self.initial_cash * (1.0 + return_pct / 100.0),
                )
            )
        return tuple(results)

    @staticmethod
    def _profit_factor(trades: Iterable[ClosedTrade]) -> float | None:
        values = [trade.pnl for trade in trades]
        gross_profit = sum(value for value in values if value > 0)
        gross_loss = abs(sum(value for value in values if value < 0))
        if gross_loss > 0:
            return gross_profit / gross_loss
        if gross_profit > 0:
            return float("inf")
        return None

    @classmethod
    def _aggregate(
        cls,
        trades: list[ClosedTrade],
        *,
        key: Callable[[ClosedTrade], str],
        score_sort: bool = False,
        quality_sort: bool = False,
        exit_sort: bool = False,
    ) -> tuple[DiagnosticRow, ...]:
        groups: dict[str, list[ClosedTrade]] = {}
        for trade in trades:
            groups.setdefault(key(trade), []).append(trade)

        rows: list[DiagnosticRow] = []
        for label, group in groups.items():
            wins = sum(1 for trade in group if trade.pnl > 0)
            losses = sum(1 for trade in group if trade.pnl < 0)
            rows.append(
                DiagnosticRow(
                    label=label,
                    trades=len(group),
                    wins=wins,
                    losses=losses,
                    win_rate=(wins / len(group) * 100.0) if group else 0.0,
                    gross_pnl=sum(trade.gross_pnl for trade in group),
                    fees=sum(trade.fees for trade in group),
                    slippage_cost=sum(trade.slippage_cost for trade in group),
                    net_pnl=sum(trade.pnl for trade in group),
                    profit_factor=cls._profit_factor(group),
                )
            )

        if score_sort:
            rows.sort(key=lambda row: cls._score_bucket_lower(row.label))
        elif quality_sort:
            rows.sort(key=lambda row: cls._quality_bucket_lower(row.label))
        elif exit_sort:
            order = {"STOP": 0, "TAKE": 1, "END": 2}
            rows.sort(key=lambda row: (order.get(row.label, 99), row.label))
        else:
            rows.sort(key=lambda row: row.label)
        return tuple(rows)

    @staticmethod
    def _score_bucket(score: int) -> str:
        if score >= 100:
            return "100"
        lower = max(0, (score // 5) * 5)
        upper = min(99, lower + 4)
        return f"{lower}-{upper}"

    @staticmethod
    def _score_bucket_lower(label: str) -> int:
        return int(label.split("-", 1)[0])

    @staticmethod
    def _quality_bucket(quality: float) -> str:
        if quality >= 100:
            return "100"
        lower = max(0, int(quality // 10) * 10)
        upper = min(99, lower + 9)
        return f"{lower}-{upper}"

    @staticmethod
    def _quality_bucket_lower(label: str) -> int:
        return int(label.split("-", 1)[0])

    def _signal_analysis(self, quality_values: list[float]) -> SignalAnalysis:
        passed = sum(1 for value in quality_values if value >= self.min_quality)
        return SignalAnalysis(
            base_candidates=len(quality_values),
            passed_quality=passed,
            rejected_quality=len(quality_values) - passed,
            quality_ge_60=sum(1 for value in quality_values if value >= 60),
            quality_ge_70=sum(1 for value in quality_values if value >= 70),
            quality_ge_80=sum(1 for value in quality_values if value >= 80),
            quality_ge_90=sum(1 for value in quality_values if value >= 90),
        )

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        peak = equity_curve[0]
        max_dd = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                drawdown = (equity / peak - 1.0) * 100.0
                max_dd = min(max_dd, drawdown)
        return max_dd
