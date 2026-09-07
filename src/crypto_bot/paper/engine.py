from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from crypto_bot.backtest.allocator_engine import AllocatorBacktestEngine, AllocationLeg, DAY_MS
from crypto_bot.market.binance import BinanceMarketDataClient, Candle
from crypto_bot.market.resample import resample_candles
from crypto_bot.paper.storage import PaperAccount, PaperRepository
from crypto_bot.storage.sqlite import CandleRepository
from crypto_bot.strategy.allocator import (
    AllocationRules,
    HysteresisDecision,
    MarketRegime,
    RankedAsset,
    RegimeHysteresis,
    build_daily_features,
    build_fast_features,
    classify_dual_regime,
    rank_assets,
    target_weights,
)

STRATEGY_NAME = "dual_regime_deadband_allocator"
STRATEGY_VERSION = "6.0-frozen"
DEFAULT_PAPER_DB = "data/paper.db"
DEFAULT_FEE_RATE = 0.001
DEFAULT_SLIPPAGE_RATE = 0.0005


def floor_utc_day(timestamp_ms: int) -> int:
    return timestamp_ms - (timestamp_ms % DAY_MS)


def next_utc_day(timestamp_ms: int) -> int:
    return floor_utc_day(timestamp_ms) + DAY_MS


def utc_text(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "-"
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def frozen_strategy_config(
    *,
    btc_symbol: str,
    symbols: Iterable[str],
    rules: AllocationRules,
    fee_rate: float,
    slippage_rate: float,
) -> dict[str, object]:
    return {
        "strategy_name": STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "btc_symbol": btc_symbol.upper(),
        "symbols": [s.upper() for s in symbols],
        "fee_rate": float(fee_rate),
        "slippage_rate": float(slippage_rate),
        "rules": asdict(rules),
    }


def config_json_and_hash(config: dict[str, object]) -> tuple[str, str]:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def rules_from_config(config: dict[str, object]) -> AllocationRules:
    rules = config.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("Invalid frozen strategy config: missing rules")
    return AllocationRules(**rules)


@dataclass(frozen=True, slots=True)
class PaperSignal:
    decision_time: int
    signal_time: int
    raw_regime: MarketRegime
    regime: MarketRegime
    previous_regime: MarketRegime | None
    decision: HysteresisDecision
    ranked: tuple[RankedAsset, ...]
    weights: dict[str, float]


@dataclass(frozen=True, slots=True)
class ProcessedDay:
    decision_time: int
    raw_regime: str
    regime: str
    trigger: str
    trades: int
    equity_after: float
    is_catchup: bool


@dataclass(frozen=True, slots=True)
class TickResult:
    processed: tuple[ProcessedDay, ...]
    skipped_already_processed: int
    waiting_reason: str | None


class PaperSignalEngine:
    """Rebuild deterministic regime state from completed historical candles.

    Recomputing state makes the paper ledger recoverable and independently
    auditable. No future candle is consumed: a decision at UTC day T uses the
    completed daily candle T-1 and the latest completed 4H candle before T.
    """

    def __init__(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        btc_candles: list[Candle],
        rules: AllocationRules,
    ) -> None:
        if not candles_by_symbol:
            raise ValueError("At least one paper-traded symbol is required")
        self.rules = rules
        self.daily_by_symbol = {s: resample_candles(rows, "1d") for s, rows in candles_by_symbol.items()}
        self.btc_daily = resample_candles(btc_candles, "1d")
        self.btc_4h = resample_candles(btc_candles, "4h")
        if len(self.btc_daily) < 205 or any(len(v) < 205 for v in self.daily_by_symbol.values()):
            raise ValueError("Paper trading needs at least 205 complete daily candles")
        self.btc_features = build_daily_features(self.btc_daily)
        self.fast_features = build_fast_features(self.btc_4h)
        self.fast_times = sorted(self.fast_features)
        self.symbol_features = {s: build_daily_features(rows) for s, rows in self.daily_by_symbol.items()}
        self.daily_candle_maps = {s: {c.open_time: c for c in rows} for s, rows in self.daily_by_symbol.items()}
        self.btc_daily_map = {c.open_time: c for c in self.btc_daily}

    def latest_fast_before(self, decision_time: int):
        pos = bisect_left(self.fast_times, decision_time) - 1
        if pos < 0:
            return None
        return self.fast_features[self.fast_times[pos]]

    def signal_at(self, decision_time: int) -> PaperSignal:
        signal_time = decision_time - DAY_MS
        if signal_time not in self.btc_features:
            raise ValueError(f"No completed BTC daily feature for {utc_text(signal_time)}")
        if any(signal_time not in fmap for fmap in self.symbol_features.values()):
            raise ValueError(f"No completed symbol daily feature for {utc_text(signal_time)}")
        if self.latest_fast_before(decision_time) is None:
            raise ValueError(f"No completed BTC 4H feature before {utc_text(decision_time)}")

        hysteresis = RegimeHysteresis(self.rules)
        previous_effective: MarketRegime | None = None
        current_decision: HysteresisDecision | None = None

        # Each completed slow feature S becomes observable for a decision at S+1d.
        for slow_time in sorted(t for t in self.btc_features if t + DAY_MS <= decision_time):
            day = slow_time + DAY_MS
            fast = self.latest_fast_before(day)
            if fast is None:
                continue
            raw = classify_dual_regime(self.btc_features[slow_time], fast, self.rules)
            before = hysteresis.current if hysteresis.initialized else None
            d = hysteresis.update(raw, fast=fast)
            if day == decision_time:
                previous_effective = before
                current_decision = d
                break

        if current_decision is None:
            raise ValueError(f"Could not build regime state for {utc_text(decision_time)}")

        symbol_features = {s: fmap[signal_time] for s, fmap in self.symbol_features.items()}
        ranked = rank_assets(symbol_features, current_decision.effective_regime)
        weights = target_weights(current_decision.effective_regime, ranked, self.rules)
        return PaperSignal(
            decision_time=decision_time,
            signal_time=signal_time,
            raw_regime=current_decision.raw_regime,
            regime=current_decision.effective_regime,
            previous_regime=previous_effective,
            decision=current_decision,
            ranked=ranked,
            weights=weights,
        )

    def previous_close_prices(self, decision_time: int, symbols: Iterable[str]) -> dict[str, float]:
        signal_time = decision_time - DAY_MS
        out: dict[str, float] = {}
        for symbol in symbols:
            if symbol in self.daily_candle_maps and signal_time in self.daily_candle_maps[symbol]:
                out[symbol] = self.daily_candle_maps[symbol][signal_time].close
            elif signal_time in self.btc_daily_map and symbol == self.btc_daily_map[signal_time].symbol:
                out[symbol] = self.btc_daily_map[signal_time].close
        return out


def exact_open_prices(candles: dict[str, list[Candle]], decision_time: int) -> dict[str, float] | None:
    prices: dict[str, float] = {}
    for symbol, rows in candles.items():
        # Daily execution is defined at the first 15m candle open of UTC day T.
        match = next((c for c in rows if c.open_time == decision_time), None)
        if match is None:
            return None
        prices[symbol] = match.open
    return prices


def latest_closed_prices(candles: dict[str, list[Candle]], now_ms: int) -> tuple[dict[str, float], int | None]:
    prices: dict[str, float] = {}
    timestamps: list[int] = []
    for symbol, rows in candles.items():
        eligible = [c for c in rows if c.close_time <= now_ms]
        if not eligible:
            continue
        last = eligible[-1]
        prices[symbol] = last.close
        timestamps.append(last.close_time)
    return prices, min(timestamps) if timestamps else None


def max_drawdown_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1.0) * 100.0)
    return worst


class PaperTrader:
    def __init__(
        self,
        *,
        market_db: str | Path,
        paper_db: str | Path = DEFAULT_PAPER_DB,
        btc_symbol: str = "BTCUSDT",
        symbols: tuple[str, ...] = ("BNBUSDT", "ETHUSDT", "LINKUSDT"),
        rules: AllocationRules | None = None,
        fee_rate: float = DEFAULT_FEE_RATE,
        slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    ) -> None:
        self.market_db = Path(market_db)
        self.paper_db = Path(paper_db)
        self.btc_symbol = btc_symbol.upper()
        self.symbols = tuple(s.upper() for s in symbols)
        self.rules = rules or AllocationRules()
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.config = frozen_strategy_config(
            btc_symbol=self.btc_symbol,
            symbols=self.symbols,
            rules=self.rules,
            fee_rate=self.fee_rate,
            slippage_rate=self.slippage_rate,
        )
        self.config_json, self.config_hash = config_json_and_hash(self.config)

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.btc_symbol, *self.symbols)))

    def _load_market(self) -> dict[str, list[Candle]]:
        with CandleRepository(self.market_db) as repo:
            return {s: repo.load(s, "15m") for s in self.all_symbols}

    def validate_market_history(self) -> None:
        data = self._load_market()
        missing = [s for s, rows in data.items() if len(rows) < 25_000]
        if missing:
            raise ValueError(f"Not enough 15m history for: {', '.join(missing)}; download about 730 days first")
        PaperSignalEngine({s: data[s] for s in self.symbols}, data[self.btc_symbol], self.rules)

    def init(self, *, initial_cash: float, now_ms: int) -> PaperAccount:
        self.validate_market_history()
        with PaperRepository(self.paper_db) as repo:
            repo.initialize(
                strategy_name=STRATEGY_NAME,
                strategy_version=STRATEGY_VERSION,
                strategy_hash=self.config_hash,
                strategy_config_json=self.config_json,
                initialized_at=now_ms,
                first_eligible_day=next_utc_day(now_ms),
                initial_cash=initial_cash,
            )
            return repo.get_account()

    def _assert_frozen(self, account: PaperAccount) -> None:
        # Use the exact stored config, and fail loudly if code defaults changed.
        if account.strategy_hash != self.config_hash or account.strategy_config_json != self.config_json:
            raise ValueError(
                "Frozen strategy mismatch. The paper account was created with different rules. "
                "Do not tune a running forward test; use the original 7.0 package or start a separate paper DB."
            )

    def sync_market(self, *, now_ms: int) -> dict[str, int]:
        received_by_symbol: dict[str, int] = {}
        with BinanceMarketDataClient() as market, CandleRepository(self.market_db) as repo:
            for symbol in self.all_symbols:
                _, last = repo.first_last_open_time(symbol, "15m")
                if last is None:
                    raise ValueError(f"{symbol} has no local history; initialize market.db first")
                # Re-fetch the latest stored candle because a previous sync may
                # have persisted it while still open. UPSERT then finalizes OHLCV.
                start_ms = int(last)
                received = 0
                if start_ms <= now_ms:
                    for page in market.iter_klines(symbol, "15m", start_time_ms=start_ms, end_time_ms=now_ms):
                        repo.upsert_many(page)
                        received += len(page)
                received_by_symbol[symbol] = received
        return received_by_symbol

    def tick(self, *, now_ms: int, sync: bool = True) -> TickResult:
        if sync:
            self.sync_market(now_ms=now_ms)

        data = self._load_market()
        signal_engine = PaperSignalEngine({s: data[s] for s in self.symbols}, data[self.btc_symbol], self.rules)
        today = floor_utc_day(now_ms)
        processed: list[ProcessedDay] = []
        skipped = 0

        with PaperRepository(self.paper_db) as repo:
            account = repo.get_account()
            self._assert_frozen(account)
            start = account.first_eligible_day if account.last_processed_day is None else account.last_processed_day + DAY_MS
            if start > today:
                if account.last_processed_day == today:
                    reason = f"UTC day {utc_text(today)} already processed; idempotent no-op"
                else:
                    reason = f"Waiting for next eligible UTC day: {utc_text(start)}"
                return TickResult((), 0, reason)

            for t in range(start, today + 1, DAY_MS):
                opens = exact_open_prices({s: data[s] for s in self.all_symbols}, t)
                if opens is None:
                    # Current day is not processable until its first 15m candle exists.
                    break
                try:
                    signal = signal_engine.signal_at(t)
                except ValueError:
                    break

                with repo.immediate_transaction() as conn:
                    if repo.decision_exists(t, conn):
                        skipped += 1
                        continue
                    account = repo.get_account(conn)
                    self._assert_frozen(account)
                    position_rows = repo.get_positions(conn)
                    positions = {s: qty for s, (qty, _) in position_rows.items() if qty > 0}
                    avg_costs = {s: avg for s, (_, avg) in position_rows.items()}

                    # Integrity guard: once started, reconstructed previous regime must
                    # agree with the persisted forward ledger.
                    if account.last_processed_day is not None and account.last_regime is not None:
                        if signal.previous_regime is not None and signal.previous_regime.value != account.last_regime:
                            raise RuntimeError(
                                f"Regime continuity mismatch at {utc_text(t)}: ledger={account.last_regime}, "
                                f"reconstructed={signal.previous_regime.value}"
                            )

                    # Finalize the day that just closed before today's open trades.
                    if account.last_processed_day is not None:
                        prev_prices = signal_engine.previous_close_prices(t, self.symbols)
                        if all(s in prev_prices for s in positions):
                            prev_equity = AllocatorBacktestEngine._equity(account.cash, positions, prev_prices)
                            invested = sum(positions.get(s, 0.0) * prev_prices[s] for s in positions)
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO snapshots
                                (snapshot_time, kind, equity, cash, invested, regime, raw_regime, prices_json, created_at)
                                VALUES (?, 'DAY_CLOSE', ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    t - 1,
                                    prev_equity,
                                    account.cash,
                                    invested,
                                    account.last_regime,
                                    account.last_raw_regime,
                                    json.dumps(prev_prices, sort_keys=True),
                                    now_ms,
                                ),
                            )

                    regime_changed = account.last_regime is not None and signal.regime.value != account.last_regime
                    defensive_exit = (
                        self.rules.emergency_bear_exit
                        and signal.regime in {MarketRegime.NEUTRAL, MarketRegime.BEAR}
                        and bool(positions)
                    )
                    transition_rebalance = self.rules.rebalance_on_regime_change and regime_changed
                    weekday = datetime.fromtimestamp(t / 1000, tz=timezone.utc).weekday()
                    is_weekly = weekday == self.rules.rebalance_weekday
                    if account.last_processed_day is None:
                        trigger = "START"
                    elif defensive_exit:
                        trigger = "DEFENSIVE"
                    elif transition_rebalance:
                        trigger = "REGIME_CHANGE"
                    elif is_weekly:
                        trigger = "WEEKLY"
                    else:
                        trigger = "NONE"

                    equity_before = AllocatorBacktestEngine._equity(account.cash, positions, {s: opens[s] for s in self.symbols})
                    legs: list[AllocationLeg] = []
                    new_cash = account.cash
                    new_positions = dict(positions)
                    if trigger != "NONE":
                        executor = AllocatorBacktestEngine(
                            initial_cash=account.initial_cash,
                            fee_rate=self.fee_rate,
                            slippage_rate=self.slippage_rate,
                            rules=self.rules,
                        )
                        legs, new_cash, new_positions = executor._rebalance(
                            t=t,
                            regime=signal.regime,
                            weights=signal.weights,
                            ranked=signal.ranked,
                            cash=account.cash,
                            positions=positions,
                            prices={s: opens[s] for s in self.symbols},
                        )
                    equity_after = AllocatorBacktestEngine._equity(new_cash, new_positions, {s: opens[s] for s in self.symbols})
                    fees = sum(x.fee for x in legs)
                    slip = sum(x.slippage_cost for x in legs)
                    turnover = sum(x.market_notional for x in legs)
                    catchup = t < today

                    ranked_json = json.dumps(
                        [
                            {
                                "symbol": r.symbol,
                                "score": r.score,
                                "return_30": r.return_30,
                                "return_90": r.return_90,
                                "trend_strength": r.trend_strength,
                            }
                            for r in signal.ranked
                        ],
                        sort_keys=True,
                    )
                    selected = list(signal.weights)
                    conn.execute(
                        """
                        INSERT INTO decisions (
                            decision_time, signal_time, raw_regime, regime, reason, trigger,
                            selected_json, ranked_json, target_weights_json, target_exposure,
                            equity_before, equity_after, fees, slippage, turnover,
                            pending_regime, pending_days, lockout_remaining, is_catchup, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t,
                            signal.signal_time,
                            signal.raw_regime.value,
                            signal.regime.value,
                            signal.decision.reason,
                            trigger,
                            json.dumps(selected),
                            ranked_json,
                            json.dumps(signal.weights, sort_keys=True),
                            sum(signal.weights.values()),
                            equity_before,
                            equity_after,
                            fees,
                            slip,
                            turnover,
                            signal.decision.pending_regime.value if signal.decision.pending_regime else None,
                            signal.decision.pending_days,
                            signal.decision.lockout_remaining,
                            1 if catchup else 0,
                            now_ms,
                        ),
                    )

                    # Update avg costs from immutable leg data, then persist exact quantities.
                    running_qty = dict(positions)
                    for sequence, leg in enumerate(legs, start=1):
                        qty = leg.market_notional / leg.market_price if leg.market_price > 0 else 0.0
                        conn.execute(
                            """
                            INSERT INTO trades (
                                decision_time, sequence, side, symbol, market_price, execution_price,
                                quantity, market_notional, fee, slippage, trigger, raw_regime, regime, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                t,
                                sequence,
                                leg.side,
                                leg.symbol,
                                leg.market_price,
                                leg.execution_price,
                                qty,
                                leg.market_notional,
                                leg.fee,
                                leg.slippage_cost,
                                trigger,
                                signal.raw_regime.value,
                                signal.regime.value,
                                now_ms,
                            ),
                        )
                        if leg.side == "BUY":
                            old_qty = running_qty.get(leg.symbol, 0.0)
                            old_cost = avg_costs.get(leg.symbol, 0.0)
                            total_cost = old_qty * old_cost + qty * leg.execution_price + leg.fee
                            running_qty[leg.symbol] = old_qty + qty
                            avg_costs[leg.symbol] = total_cost / running_qty[leg.symbol] if running_qty[leg.symbol] > 0 else 0.0
                        else:
                            running_qty[leg.symbol] = max(0.0, running_qty.get(leg.symbol, 0.0) - qty)

                    conn.execute("DELETE FROM positions")
                    for symbol, qty in sorted(new_positions.items()):
                        if qty <= 1e-14:
                            continue
                        conn.execute(
                            "INSERT INTO positions(symbol, quantity, avg_cost, updated_at) VALUES (?, ?, ?, ?)",
                            (symbol, qty, max(0.0, avg_costs.get(symbol, opens[symbol])), now_ms),
                        )

                    benchmark_time = account.benchmark_start_time
                    benchmark_prices = account.benchmark_start_prices
                    if benchmark_time is None:
                        benchmark_time = t
                        benchmark_prices = {s: opens[s] for s in self.all_symbols}
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO snapshots
                            (snapshot_time, kind, equity, cash, invested, regime, raw_regime, prices_json, created_at)
                            VALUES (?, 'START', ?, ?, 0, ?, ?, ?, ?)
                            """,
                            (
                                t,
                                account.initial_cash,
                                account.initial_cash,
                                signal.regime.value,
                                signal.raw_regime.value,
                                json.dumps({s: opens[s] for s in self.symbols}, sort_keys=True),
                                now_ms,
                            ),
                        )

                    invested_after = sum(new_positions.get(s, 0.0) * opens[s] for s in new_positions)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO snapshots
                        (snapshot_time, kind, equity, cash, invested, regime, raw_regime, prices_json, created_at)
                        VALUES (?, 'POST_TRADE', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t,
                            equity_after,
                            new_cash,
                            invested_after,
                            signal.regime.value,
                            signal.raw_regime.value,
                            json.dumps({s: opens[s] for s in self.symbols}, sort_keys=True),
                            now_ms,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE account SET
                            cash=?, last_processed_day=?, last_regime=?, last_raw_regime=?,
                            pending_regime=?, pending_days=?, lockout_remaining=?,
                            benchmark_start_time=?, benchmark_start_prices_json=?,
                            total_fees=total_fees+?, total_slippage=total_slippage+?,
                            total_turnover=total_turnover+?, updated_at=?
                        WHERE id=1
                        """,
                        (
                            new_cash,
                            t,
                            signal.regime.value,
                            signal.raw_regime.value,
                            signal.decision.pending_regime.value if signal.decision.pending_regime else None,
                            signal.decision.pending_days,
                            signal.decision.lockout_remaining,
                            benchmark_time,
                            json.dumps(benchmark_prices, sort_keys=True) if benchmark_prices else None,
                            fees,
                            slip,
                            turnover,
                            now_ms,
                        ),
                    )

                processed.append(
                    ProcessedDay(
                        decision_time=t,
                        raw_regime=signal.raw_regime.value,
                        regime=signal.regime.value,
                        trigger=trigger,
                        trades=len(legs),
                        equity_after=equity_after,
                        is_catchup=catchup,
                    )
                )

            waiting = None
            if not processed:
                account = repo.get_account()
                if account.last_processed_day == today:
                    waiting = f"UTC day {utc_text(today)} already processed; idempotent no-op"
                elif today < account.first_eligible_day:
                    waiting = f"Waiting for first eligible UTC day: {utc_text(account.first_eligible_day)}"
                else:
                    waiting = "No new processable UTC day yet (waiting for complete prior-day data / current day open)"
            return TickResult(tuple(processed), skipped, waiting)

    def status(self, *, now_ms: int) -> dict[str, object]:
        data = self._load_market()
        prices, price_time = latest_closed_prices(data, now_ms)
        with PaperRepository(self.paper_db) as repo:
            account = repo.get_account()
            self._assert_frozen(account)
            position_rows = repo.get_positions()
            positions = {s: qty for s, (qty, _) in position_rows.items()}
            avg_costs = {s: avg for s, (_, avg) in position_rows.items()}
            position_prices = {s: prices[s] for s in positions if s in prices}
            if len(position_prices) != len(positions):
                equity = account.cash
                for s, qty in positions.items():
                    if s in prices:
                        equity += qty * prices[s]
            else:
                equity = AllocatorBacktestEngine._equity(account.cash, positions, position_prices)
            invested = sum(qty * prices.get(s, 0.0) for s, qty in positions.items())
            return_pct = (equity / account.initial_cash - 1.0) * 100.0
            curve = [account.initial_cash, *repo.snapshot_equities(), equity]
            dd = max_drawdown_pct(curve)
            benchmarks: dict[str, float] = {}
            if account.benchmark_start_prices:
                for symbol, start_price in account.benchmark_start_prices.items():
                    if start_price > 0 and symbol in prices:
                        benchmarks[symbol] = (prices[symbol] / start_price - 1.0) * 100.0
                eq_symbols = [s for s in self.symbols if s in benchmarks]
                if eq_symbols:
                    final_multiple = sum(1.0 + benchmarks[s] / 100.0 for s in eq_symbols) / len(eq_symbols)
                    benchmarks["EQUAL_WEIGHT"] = (final_multiple - 1.0) * 100.0
            latest = repo.latest_decision()
            return {
                "account": account,
                "equity": equity,
                "return_pct": return_pct,
                "max_drawdown_pct": dd,
                "invested": invested,
                "exposure_pct": invested / equity * 100.0 if equity > 0 else 0.0,
                "positions": positions,
                "avg_costs": avg_costs,
                "prices": prices,
                "price_time": price_time,
                "benchmarks": benchmarks,
                "trade_count": repo.trade_count(),
                "decision_count": repo.decision_count(),
                "latest_decision": dict(latest) if latest is not None else None,
                "recent_trades": [dict(x) for x in repo.recent_trades(8)],
                "integrity_issues": repo.integrity_check(),
            }
