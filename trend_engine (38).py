from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from typing import Iterable

from crypto_bot.backtest.engine import BenchmarkResult
from crypto_bot.indicators import build_snapshots
from crypto_bot.market.binance import Candle
from crypto_bot.market.resample import resample_candles
from crypto_bot.simulation.broker import SimulatedBroker
from crypto_bot.strategy.trend_pullback import PullbackCandidate, PullbackRules, TrendPullbackStrategy, TrendSeries

D = Decimal


@dataclass(frozen=True, slots=True)
class TrendTrade:
    symbol: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    trade_budget: float
    stop_pct: float
    take_pct: float
    atr_pct: float
    ema_gap_pct: float
    momentum_3: float
    volume_ratio: float
    rsi14: float
    pnl: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    return_pct: float
    reason: str


@dataclass(frozen=True, slots=True)
class TrendBacktestResult:
    initial_cash: float
    final_equity: float
    return_pct: float
    trades: tuple[TrendTrade, ...]
    wins: int
    losses: int
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float
    sharpe_like: float | None
    total_fees: float
    total_slippage_cost: float
    gross_pnl: float
    benchmarks: tuple[BenchmarkResult, ...]
    gate_counts: tuple[tuple[str, int], ...]
    effective_start_time: int
    effective_end_time: int

    @property
    def total_costs(self) -> float:
        return self.total_fees + self.total_slippage_cost

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.initial_cash


class TrendPullbackBacktestEngine:
    def __init__(
        self,
        *,
        initial_cash: float = 10.0,
        trade_percent: float = 50.0,
        fee_rate: float = 0.001,
        slippage_rate: float = 0.0005,
        atr_stop_mult: float = 2.0,
        risk_reward: float = 2.0,
        min_stop_pct: float = 0.01,
        max_stop_pct: float = 0.03,
        rules: PullbackRules | None = None,
        trade_start_time: int | None = None,
        trade_end_time: int | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if not 0 < trade_percent <= 100:
            raise ValueError("trade_percent must be in (0, 100]")
        if atr_stop_mult <= 0 or risk_reward <= 0:
            raise ValueError("ATR multiplier and risk/reward must be > 0")
        if not 0 < min_stop_pct <= max_stop_pct:
            raise ValueError("invalid stop bounds")
        self.initial_cash = initial_cash
        self.trade_percent = trade_percent
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.atr_stop_mult = atr_stop_mult
        self.risk_reward = risk_reward
        self.min_stop_pct = min_stop_pct
        self.max_stop_pct = max_stop_pct
        self.trade_start_time = trade_start_time
        self.trade_end_time = trade_end_time
        self.strategy = TrendPullbackStrategy(rules)

    def run(self, candles_by_symbol: dict[str, list[Candle]], btc_candles: list[Candle]) -> TrendBacktestResult:
        prepared = self._prepare_trade_symbols(candles_by_symbol)
        if not prepared:
            raise ValueError("No trade symbols have enough 15m data")
        if len(btc_candles) < 3200:
            raise ValueError("BTCUSDT needs enough 15m data for the 4H EMA200 regime")

        btc_4h = TrendSeries(resample_candles(sorted(btc_candles, key=lambda c: c.open_time), "4h"))
        symbol_1h = {
            symbol: TrendSeries(resample_candles(data["candles"], "1h"))
            for symbol, data in prepared.items()
        }

        common_times = sorted(set.intersection(*(set(data["by_time"]) for data in prepared.values())))
        if self.trade_end_time is not None:
            common_times = [t for t in common_times if t <= self.trade_end_time]
        if len(common_times) < 200:
            raise ValueError("Not enough aligned 15m candles")

        natural_start = self._natural_start(common_times, prepared, symbol_1h, btc_4h)
        effective_start = max(natural_start, self.trade_start_time or natural_start)
        effective_times = [t for t in common_times if t >= effective_start]
        if len(effective_times) < 2:
            raise ValueError("No usable candles inside requested trading window")
        effective_end = effective_times[-1]

        broker = SimulatedBroker(
            initial_cash=str(self.initial_cash),
            fee_rate=str(self.fee_rate),
            slippage_rate=str(self.slippage_rate),
        )
        trades: list[TrendTrade] = []
        equity_curve: list[float] = [self.initial_cash]
        gates: Counter[str] = Counter()
        pending: PullbackCandidate | None = None
        open_meta: dict[str, float | int | str] | None = None

        time_to_idx = {t: i for i, t in enumerate(common_times)}
        start_idx = time_to_idx[effective_start]

        for idx in range(start_idx, len(common_times)):
            open_time = common_times[idx]
            candles_now = {symbol: data["by_time"][open_time] for symbol, data in prepared.items()}

            if broker.position is None and pending is not None:
                candle = candles_now[pending.symbol]
                budget = float(broker.cash) * self.trade_percent / 100.0
                if budget > 0:
                    buy = broker.buy(pending.symbol, str(candle.open), str(budget))
                    entry_exec = float(buy.execution_price)
                    raw_stop = (pending.atr_pct / 100.0) * self.atr_stop_mult
                    stop_pct = min(self.max_stop_pct, max(self.min_stop_pct, raw_stop))
                    take_pct = stop_pct * self.risk_reward
                    open_meta = {
                        "symbol": pending.symbol,
                        "entry_time": open_time,
                        "entry_market": float(candle.open),
                        "entry_exec": entry_exec,
                        "budget": budget,
                        "buy_fee": float(buy.fee),
                        "stop_pct": stop_pct,
                        "take_pct": take_pct,
                        "atr_pct": pending.atr_pct,
                        "ema_gap_pct": pending.ema_gap_pct,
                        "momentum_3": pending.momentum_3,
                        "volume_ratio": pending.volume_ratio,
                        "rsi14": pending.rsi14,
                    }
                pending = None

            if broker.position is not None and open_meta is not None:
                symbol = broker.position.symbol
                candle = candles_now[symbol]
                entry = float(broker.position.entry_price)
                stop = entry * (1.0 - float(open_meta["stop_pct"]))
                take = entry * (1.0 + float(open_meta["take_pct"]))
                exit_price: float | None = None
                reason: str | None = None
                if candle.low <= stop:
                    exit_price, reason = stop, "STOP"
                elif candle.high >= take:
                    exit_price, reason = take, "TAKE"
                if exit_price is not None:
                    trades.append(self._close(broker, open_meta, open_time, exit_price, reason))
                    open_meta = None

            if open_time >= effective_start:
                if broker.position is None:
                    equity_curve.append(float(broker.cash))
                else:
                    equity_curve.append(float(broker.equity(str(candles_now[broker.position.symbol].close))))

            if broker.position is None and idx < len(common_times) - 1:
                candidates: list[PullbackCandidate] = []
                for symbol, data in prepared.items():
                    candle_index = data["index_by_time"][open_time]
                    if candle_index < 1:
                        continue
                    current_snapshot = data["snapshots"][candle_index]
                    previous_snapshot = data["snapshots"][candle_index - 1]
                    if current_snapshot is None or previous_snapshot is None:
                        continue
                    current_candle = data["candles"][candle_index]
                    previous_candle = data["candles"][candle_index - 1]
                    available_at = current_candle.close_time
                    decision = self.strategy.evaluate(
                        symbol=symbol,
                        previous_candle=previous_candle,
                        current_candle=current_candle,
                        previous_snapshot=previous_snapshot,
                        current_snapshot=current_snapshot,
                        one_hour=symbol_1h[symbol].latest(available_at),
                        btc_four_hour=btc_4h.latest(available_at),
                    )
                    gates[decision.reason] += 1
                    if decision.candidate is not None:
                        candidates.append(decision.candidate)
                if candidates:
                    pending = sorted(candidates, key=self.strategy.rank)[0]
                    gates["SELECTED"] += 1

        if broker.position is not None and open_meta is not None:
            symbol = broker.position.symbol
            final_candle = prepared[symbol]["by_time"][effective_end]
            trades.append(self._close(broker, open_meta, effective_end, float(final_candle.close), "END"))
            equity_curve.append(float(broker.cash))

        final_equity = float(broker.cash)
        wins = sum(t.pnl > 0 for t in trades)
        losses = sum(t.pnl < 0 for t in trades)
        returns = [t.return_pct / 100.0 for t in trades]
        sharpe_like: float | None = None
        if len(returns) >= 2:
            sigma = pstdev(returns)
            if sigma > 0:
                sharpe_like = mean(returns) / sigma * sqrt(len(returns))

        return TrendBacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            return_pct=(final_equity / self.initial_cash - 1.0) * 100.0,
            trades=tuple(trades),
            wins=int(wins),
            losses=int(losses),
            win_rate=(wins / len(trades) * 100.0) if trades else 0.0,
            profit_factor=self._profit_factor(trades),
            max_drawdown_pct=self._max_drawdown(equity_curve),
            sharpe_like=sharpe_like,
            total_fees=sum(t.fees for t in trades),
            total_slippage_cost=sum(t.slippage_cost for t in trades),
            gross_pnl=sum(t.gross_pnl for t in trades),
            benchmarks=self._benchmarks(prepared, effective_start, effective_end),
            gate_counts=tuple(sorted(gates.items())),
            effective_start_time=effective_start,
            effective_end_time=effective_end,
        )

    def _prepare_trade_symbols(self, candles_by_symbol: dict[str, list[Candle]]) -> dict[str, dict[str, object]]:
        prepared: dict[str, dict[str, object]] = {}
        for raw_symbol, candles in candles_by_symbol.items():
            ordered = sorted((c for c in candles if c.interval == "15m"), key=lambda c: c.open_time)
            if len(ordered) < 1000:
                continue
            snapshots = build_snapshots(
                [c.close for c in ordered], [c.volume for c in ordered], [c.high for c in ordered], [c.low for c in ordered]
            )
            prepared[raw_symbol.upper()] = {
                "candles": ordered,
                "snapshots": snapshots,
                "by_time": {c.open_time: c for c in ordered},
                "index_by_time": {c.open_time: i for i, c in enumerate(ordered)},
            }
        return prepared

    @staticmethod
    def _natural_start(common_times, prepared, symbol_1h, btc_4h) -> int:
        for t in common_times:
            # All Binance intervals close at open + interval - 1ms. The current
            # 15m candle is fully known at this close_time.
            first_symbol = next(iter(prepared))
            candle = prepared[first_symbol]["by_time"][t]
            available_at = candle.close_time
            if btc_4h.latest(available_at) is None:
                continue
            if all(series.latest(available_at) is not None for series in symbol_1h.values()):
                return t
        raise ValueError("Higher-timeframe warmup never completed")

    def _close(self, broker, meta, exit_time, exit_market_price, reason) -> TrendTrade:
        budget = float(meta["budget"])
        entry_market = float(meta["entry_market"])
        buy_fee = float(meta["buy_fee"])
        sell = broker.sell_all(str(exit_market_price))
        pnl = float(sell.realized_pnl or D("0"))
        fees = buy_fee + float(sell.fee)
        gross_pnl = budget / entry_market * exit_market_price - budget
        slippage = gross_pnl - pnl - fees
        return TrendTrade(
            symbol=str(meta["symbol"]),
            entry_time=int(meta["entry_time"]),
            exit_time=exit_time,
            entry_price=float(meta["entry_exec"]),
            exit_price=float(sell.execution_price),
            trade_budget=budget,
            stop_pct=float(meta["stop_pct"]) * 100.0,
            take_pct=float(meta["take_pct"]) * 100.0,
            atr_pct=float(meta["atr_pct"]),
            ema_gap_pct=float(meta["ema_gap_pct"]),
            momentum_3=float(meta["momentum_3"]),
            volume_ratio=float(meta["volume_ratio"]),
            rsi14=float(meta["rsi14"]),
            pnl=pnl,
            gross_pnl=gross_pnl,
            fees=fees,
            slippage_cost=slippage,
            return_pct=pnl / budget * 100.0,
            reason=reason,
        )

    def _benchmarks(self, prepared, start_time, end_time) -> tuple[BenchmarkResult, ...]:
        out: list[BenchmarkResult] = []
        for symbol, data in sorted(prepared.items()):
            first = data["by_time"][start_time]
            last = data["by_time"][end_time]
            ret = (last.close / first.open - 1.0) * 100.0
            out.append(BenchmarkResult(symbol, first.open, last.close, ret, self.initial_cash * (1 + ret / 100.0)))
        return tuple(out)

    @staticmethod
    def _profit_factor(trades: Iterable[TrendTrade]) -> float | None:
        pnls = [t.pnl for t in trades]
        gp = sum(x for x in pnls if x > 0)
        gl = abs(sum(x for x in pnls if x < 0))
        if gl > 0:
            return gp / gl
        if gp > 0:
            return float("inf")
        return None

    @staticmethod
    def _max_drawdown(curve: list[float]) -> float:
        peak = curve[0]
        worst = 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, (value / peak - 1.0) * 100.0)
        return worst
