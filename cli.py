from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crypto_bot.backtest import BacktestEngine
from crypto_bot.config import DEFAULT_DB_PATH, DEFAULT_INTERVAL, DEFAULT_SYMBOLS, RESEARCH_SYMBOLS
from crypto_bot.market.binance import BinanceMarketDataClient
from crypto_bot.storage.sqlite import CandleRepository
from crypto_bot.research import report_to_json, run_research
from crypto_bot.evaluation import (
    allocator_evaluation_to_json,
    evaluation_to_json,
    run_allocator_evaluation,
    run_evaluation,
)
from crypto_bot.strategy.allocator import AllocationRules
from crypto_bot.strategy.trend_pullback import PullbackRules
from crypto_bot.paper import DEFAULT_PAPER_DB, PaperTrader
from crypto_bot.paper.engine import utc_text as _paper_utc_text
from crypto_bot.paper.storage import PaperRepository


def _parse_symbols(value: str) -> list[str]:
    symbols = [part.strip().upper() for part in value.split(",") if part.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("At least one symbol is required")
    return symbols


def _utc_text(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "-"
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def cmd_download(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print(f"Database : {args.db}")
    print(f"Interval : {args.interval}")
    print(f"Range    : {start:%Y-%m-%d} -> {now:%Y-%m-%d} UTC")
    print()

    with BinanceMarketDataClient() as market, CandleRepository(args.db) as repo:
        for symbol in args.symbols:
            total_received = 0
            print(f"{symbol}: downloading...", flush=True)

            for page in market.iter_klines(
                symbol,
                args.interval,
                start_time_ms=start_ms,
                end_time_ms=end_ms,
            ):
                repo.upsert_many(page)
                total_received += len(page)
                print(f"  received {total_received:,} candles", end="\r", flush=True)

            total_db = repo.count(symbol, args.interval)
            print(f"  stored   {total_db:,} candles{' ' * 20}")

    print("\nDone.")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    with CandleRepository(args.db) as repo:
        print(f"Database: {Path(args.db).resolve()}")
        print(f"Interval: {args.interval}\n")
        for symbol in args.symbols:
            count = repo.count(symbol, args.interval)
            first, last = repo.first_last_open_time(symbol, args.interval)
            print(
                f"{symbol:10} {count:>10,} candles  "
                f"{_utc_text(first)} -> {_utc_text(last)}"
            )
    return 0



def _pf_text(value: float | None) -> str:
    if value == float("inf"):
        return "inf"
    if value is None:
        return "-"
    return f"{value:.2f}"


def _print_diagnostic_table(title: str, rows) -> None:
    print(f"\n{title}")
    print("  Group        Trades   Wins  Losses   Win rate   Gross PnL     Fees   Slippage    Net PnL     PF")
    print("  -----------  ------  -----  ------  ---------  ----------  -------  ---------  ---------  -----")
    for row in rows:
        print(
            f"  {row.label:11}  {row.trades:6d}  {row.wins:5d}  {row.losses:6d}  "
            f"{row.win_rate:8.2f}%  ${row.gross_pnl:+9.4f}  ${row.fees:6.4f}  "
            f"${row.slippage_cost:8.4f}  ${row.net_pnl:+8.4f}  {_pf_text(row.profit_factor):>5}"
        )


def cmd_backtest(args: argparse.Namespace) -> int:
    with CandleRepository(args.db) as repo:
        candles_by_symbol = {
            symbol: repo.load(symbol, args.interval)
            for symbol in args.symbols
        }

    usable = {symbol: candles for symbol, candles in candles_by_symbol.items() if len(candles) >= 60}
    missing = [symbol for symbol, candles in candles_by_symbol.items() if len(candles) < 60]
    if missing:
        print(f"Skipping (<60 candles): {', '.join(missing)}")
    if not usable:
        raise SystemExit("No symbols have enough data. Run crypto-bot download first.")

    engine = BacktestEngine(
        initial_cash=args.cash,
        trade_size=args.trade_size,
        trade_percent=args.trade_percent,
        min_score=args.min_score,
        min_quality=args.min_quality,
        stop_loss_pct=args.stop_loss / 100.0,
        take_profit_pct=args.take_profit / 100.0,
        fee_rate=args.fee / 100.0,
        slippage_rate=args.slippage / 100.0,
        cooldown_candles=args.cooldown,
    )
    result = engine.run(usable)

    print("\n=== CRYPTO BOT BACKTEST ===")
    print(f"Symbols       : {', '.join(sorted(usable))}")
    print(f"Interval      : {args.interval}")
    print(f"Initial cash  : ${result.initial_cash:.4f}")
    print(f"Final equity  : ${result.final_equity:.4f}")
    print(f"Return        : {result.return_pct:+.2f}%")
    print(f"Trades        : {len(result.trades)}")
    print(f"Wins / Losses : {result.wins} / {result.losses}")
    print(f"Win rate      : {result.win_rate:.2f}%")
    print(f"Avg win       : ${result.avg_win:.4f}")
    print(f"Avg loss      : ${result.avg_loss:.4f}")
    print(f"Profit factor : {_pf_text(result.profit_factor)}")
    print(f"Max drawdown  : {result.max_drawdown_pct:.2f}%")
    print(f"Sharpe-like   : {'-' if result.sharpe_like is None else f'{result.sharpe_like:.2f}'}")
    sizing = (
        f"{result.trade_percent:.2f}% of current cash"
        if result.trade_percent is not None
        else f"${result.trade_size:.4f} fixed"
    )
    print(f"Trade sizing  : {sizing}")
    print(f"Cooldown      : {result.cooldown_candles} candles")
    print(f"Min quality   : {result.min_quality:.2f}")
    print(f"Selections    : {len(result.selections)} ({result.tied_selections} classic-score ties)")

    sa = result.signal_analysis
    print("\n=== SIGNAL QUALITY ANALYSIS ===")
    print(f"Base candidates (score gate) : {sa.base_candidates}")
    print(f"Passed min quality           : {sa.passed_quality}")
    print(f"Rejected by quality          : {sa.rejected_quality}")
    print(f"Quality >= 60                : {sa.quality_ge_60}")
    print(f"Quality >= 70                : {sa.quality_ge_70}")
    print(f"Quality >= 80                : {sa.quality_ge_80}")
    print(f"Quality >= 90                : {sa.quality_ge_90}")

    print("\n=== COST ANALYSIS ===")
    print(f"Gross PnL (no costs) : ${result.gross_pnl:+.4f}")
    print(f"Trading fees         : ${result.total_fees:.4f}")
    print(f"Slippage drag        : ${result.total_slippage_cost:.4f}")
    print(f"Total trading costs  : ${result.total_costs:.4f}")
    print(f"Net PnL              : ${result.net_pnl:+.4f}")

    _print_diagnostic_table("=== RESULTS BY SYMBOL ===", result.by_symbol)
    _print_diagnostic_table("=== RESULTS BY SCORE ===", result.by_score)
    _print_diagnostic_table("=== RESULTS BY QUALITY ===", result.by_quality)
    _print_diagnostic_table("=== EXIT REASONS ===", result.by_exit)

    print(f"\nBuy & hold benchmarks (same ${result.initial_cash:.2f}):")
    for benchmark in result.benchmarks:
        print(
            f"  {benchmark.symbol:10} "
            f"{benchmark.return_pct:+8.2f}% -> ${benchmark.final_value:.4f}"
        )

    if result.selections:
        print("\nLast signal selections:")
        for decision in result.selections[-10:]:
            selected = next(c for c in decision.candidates if c.symbol == decision.selected_symbol)
            print(
                f"  {decision.selected_symbol:10} score={decision.selected_score:3d} "
                f"quality={decision.selected_quality:5.1f} "
                f"candidates={len(decision.candidates):2d} score-ties={decision.tie_count:2d} "
                f"vol={selected.volume_ratio:.2f}x "
                f"mom={selected.momentum_3 * 100:+.2f}% "
                f"ema-gap={selected.ema_gap_pct:+.3f}% "
                f"atr={selected.atr_pct:.3f}%"
            )

    if result.trades:
        print("\nLast trades:")
        for trade in result.trades[-10:]:
            print(
                f"  {trade.symbol:10} score={trade.score:3d} quality={trade.quality:5.1f} "
                f"{trade.reason:4} gross=${trade.gross_pnl:+.4f} "
                f"cost=${(trade.fees + trade.slippage_cost):.4f} "
                f"net=${trade.pnl:+.4f} ({trade.return_pct:+.2f}%)"
            )
    print()
    return 0



def _result_line(label: str, result) -> str:
    pf = _pf_text(result.profit_factor)
    return (
        f"{label:12} return={result.return_pct:+7.2f}%  PF={pf:>5}  "
        f"DD={result.max_drawdown_pct:7.2f}%  trades={len(result.trades):4d}  "
        f"win={result.win_rate:6.2f}%  costs=${result.total_costs:.4f}"
    )


def cmd_research(args: argparse.Namespace) -> int:
    with CandleRepository(args.db) as repo:
        candles_by_symbol = {symbol: repo.load(symbol, args.interval) for symbol in args.symbols}

    usable = {symbol: candles for symbol, candles in candles_by_symbol.items() if len(candles) >= 120}
    missing = [symbol for symbol, candles in candles_by_symbol.items() if len(candles) < 120]
    if missing:
        print(f"Skipping (<120 candles): {', '.join(missing)}")
    if not usable:
        raise SystemExit("No symbols have enough data. Run crypto-bot download first.")

    print("\n=== CRYPTO BOT RESEARCH v1.0 ===")
    print(f"Symbols       : {', '.join(sorted(usable))}")
    print(f"Interval      : {args.interval}")
    print(f"Train / test  : {args.train_ratio * 100:.0f}% / {(1-args.train_ratio) * 100:.0f}% chronological")
    print(f"Capital       : ${args.cash:.2f}")
    print(f"Trade sizing  : {args.trade_percent:.2f}% of current cash")
    print(f"Classic gate  : score >= {args.min_score}")
    print("Quality gate  : DISABLED (v0.5 showed it was not predictive)")
    print("Searching     : one-feature filters learned on TRAIN only")

    report = run_research(
        usable,
        initial_cash=args.cash,
        trade_percent=args.trade_percent,
        min_score=args.min_score,
        stop_loss_pct=args.stop_loss / 100.0,
        take_profit_pct=args.take_profit / 100.0,
        fee_rate=args.fee / 100.0,
        slippage_rate=args.slippage / 100.0,
        train_ratio=args.train_ratio,
        top_n=args.top,
    )

    print(f"\nSplit time    : {_utc_text(report.split_time)}")
    print("\n=== BASELINE (NO LEARNED FILTER) ===")
    print(_result_line("TRAIN", report.baseline_train))
    print(_result_line("TEST", report.baseline_test))

    print("\n=== WINNERS VS LOSERS ON TRAIN ===")
    print("  Feature          Win mean     Loss mean    Win median   Loss median    Mean gap")
    print("  ---------------  -----------  -----------  -----------  -----------  ----------")
    for row in report.feature_comparisons:
        if row.feature == "momentum_3":
            scale, suffix = 100.0, "%"
        elif row.feature in {"ema_gap_pct", "atr_pct"}:
            scale, suffix = 1.0, "%"
        elif row.feature == "volume_ratio":
            scale, suffix = 1.0, "x"
        else:
            scale, suffix = 1.0, ""
        print(
            f"  {row.feature:15}  {row.winner_mean*scale:10.4f}{suffix:<1}  "
            f"{row.loser_mean*scale:10.4f}{suffix:<1}  "
            f"{row.winner_median*scale:10.4f}{suffix:<1}  "
            f"{row.loser_median*scale:10.4f}{suffix:<1}  "
            f"{row.relative_gap_pct:+8.2f}%"
        )

    print("\n=== TOP FILTERS CHOSEN ON TRAIN, THEN EVALUATED ON TEST ===")
    print("  Filter                                      TRAIN ret  PF    DD      n   TEST ret   PF    DD      n   BEAT")
    print("  ------------------------------------------  ---------  ----  -------  ---  ---------  ----  -------  ---  ----")
    baseline_test_pf = report.baseline_test.profit_factor or 0.0
    for row in report.experiments:
        train_pf = _pf_text(row.train_pf)
        test_pf = _pf_text(row.test_pf)
        beats = (
            row.train_profitable
            and row.test_profitable
            and row.test_return > report.baseline_test.return_pct
            and (row.test_pf or 0.0) >= baseline_test_pf
        )
        oos = "YES" if beats else "no"
        print(
            f"  {row.name[:42]:42}  {row.train_return:+8.2f}%  {train_pf:>4}  "
            f"{row.train_drawdown:7.2f}%  {row.train_trades:3d}  "
            f"{row.test_return:+8.2f}%  {test_pf:>4}  {row.test_drawdown:7.2f}%  {row.test_trades:3d}  {oos:>3}"
        )

    robust = report.robust_candidates
    print("\n=== INTERPRETATION ===")
    if robust:
        print(f"{len(robust)} candidate filter(s) beat the baseline on unseen TEST while remaining profitable on TRAIN.")
        print("That is a real research lead, not proof; validate it on more history / another market regime before real money.")
    else:
        print("No learned filter beat the baseline on unseen TEST.")
        print("That is useful: keep the baseline rather than overfitting a historical threshold.")

    output = report_to_json(report, args.output)
    print(f"\nMachine-readable report: {output.resolve()}")
    print()
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    if args.interval != "15m":
        raise SystemExit("The trend/pullback evaluator currently requires --interval 15m; it resamples 1H/4H internally.")

    with CandleRepository(args.db) as repo:
        candles_by_symbol = {symbol: repo.load(symbol, args.interval) for symbol in args.symbols}
        btc_candles = repo.load(args.btc_symbol, args.interval)

    missing = [symbol for symbol, rows in candles_by_symbol.items() if len(rows) < 4000]
    if missing:
        raise SystemExit(f"Not enough 15m history for: {', '.join(missing)}. Download ~730 days first.")
    if len(btc_candles) < 4000:
        raise SystemExit(f"Not enough 15m history for {args.btc_symbol}. Include it in the download command.")

    rules = PullbackRules(
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
        max_momentum_3=args.max_momentum / 100.0,
        max_ema_gap_pct=args.max_ema_gap,
        min_atr_pct=args.min_atr,
        max_atr_pct=args.max_atr,
        pullback_tolerance_pct=args.pullback_tolerance,
    )
    report = run_evaluation(
        candles_by_symbol,
        btc_candles,
        initial_cash=args.cash,
        trade_percent=args.trade_percent,
        fee_rate=args.fee / 100.0,
        slippage_rate=args.slippage / 100.0,
        warmup_days=args.warmup_days,
        fold_days=args.fold_days,
        atr_stop_mult=args.atr_stop_mult,
        risk_reward=args.risk_reward,
        min_stop_pct=args.min_stop / 100.0,
        max_stop_pct=args.max_stop / 100.0,
        rules=rules,
    )

    r = report.full_result
    print("\n=== CRYPTO BOT STABLE — TREND/PULLBACK EVALUATION ===")
    print(f"Symbols       : {', '.join(sorted(args.symbols))}")
    print(f"Regime        : {args.btc_symbol} 4H close/EMA50/EMA200 + EMA50 slope")
    print("Confirmation  : symbol 1H close > EMA200 and EMA50 > EMA200 with positive slope")
    print("Entry         : 15m EMA trend + pullback toward EMA21 + recovery above EMA9")
    print(f"Risk          : ATR x {args.atr_stop_mult:.2f}, stop {args.min_stop:.2f}%..{args.max_stop:.2f}%, R:R {args.risk_reward:.2f}")
    print(f"Costs         : fee {args.fee:.3f}%/side + slippage {args.slippage:.3f}%/side")
    print(f"Window        : {_utc_text(r.effective_start_time)} -> {_utc_text(r.effective_end_time)}")

    print("\n=== FULL EVALUATION PERIOD ===")
    print(f"Initial cash  : ${r.initial_cash:.4f}")
    print(f"Final equity  : ${r.final_equity:.4f}")
    print(f"Return        : {r.return_pct:+.2f}%")
    print(f"Trades        : {len(r.trades)}")
    print(f"Wins / Losses : {r.wins} / {r.losses}")
    print(f"Win rate      : {r.win_rate:.2f}%")
    print(f"Profit factor : {_pf_text(r.profit_factor)}")
    print(f"Max drawdown  : {r.max_drawdown_pct:.2f}%")
    print(f"Costs         : ${r.total_costs:.4f}")

    print("\n=== BENCHMARKS (SAME PERIOD / SAME STARTING CASH) ===")
    for b in report.full_benchmarks:
        print(f"  {b.name:28} {b.return_pct:+8.2f}% -> ${b.final_value:.4f}")

    print("\n=== ENTRY GATE DIAGNOSTICS ===")
    for name, count in sorted(r.gate_counts, key=lambda item: item[1], reverse=True):
        print(f"  {name:24} {count:8d}")

    wf = report.walk_forward
    print(f"\n=== WALK-FORWARD ({report.fold_days}-DAY FOLDS, {report.warmup_days}-DAY INITIAL WARMUP) ===")
    print("  #  Period                      Strategy    PF     DD      Trades   BTC hold   Equal-wt")
    print("  -  --------------------------  --------  -----  -------  ------  ---------  ---------")
    for fold in wf.folds:
        fr = fold.result
        start = datetime.fromtimestamp(fold.start_time/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end = datetime.fromtimestamp(fold.end_time/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(
            f"  {fold.number:>1}  {start} -> {end}  {fr.return_pct:+7.2f}%  {_pf_text(fr.profit_factor):>5}  "
            f"{fr.max_drawdown_pct:7.2f}%  {len(fr.trades):6d}  {fold.btc_hold_return:+8.2f}%  {fold.equal_weight_return:+8.2f}%"
        )

    print("\n=== ROBUSTNESS SUMMARY ===")
    print(f"Profitable folds    : {wf.profitable_folds}/{wf.total_folds}")
    print(f"Mean fold return    : {wf.mean_return_pct:+.2f}%")
    print(f"Median fold return  : {wf.median_return_pct:+.2f}%")
    print(f"Compounded folds    : {wf.compounded_return_pct:+.2f}%")
    print(f"Aggregate PF        : {_pf_text(wf.aggregate_profit_factor)}")
    print(f"Worst fold return   : {wf.worst_fold_return_pct:+.2f}%")
    print(f"Worst fold drawdown : {wf.worst_drawdown_pct:.2f}%")
    print(f"Total fold trades   : {wf.total_trades}")

    fold_ratio = wf.profitable_folds / wf.total_folds if wf.total_folds else 0.0
    checks = {
        "positive compounded walk-forward": wf.compounded_return_pct > 0,
        "aggregate PF >= 1.20": (wf.aggregate_profit_factor or 0.0) >= 1.20,
        "profitable folds >= 60%": fold_ratio >= 0.60,
        "worst DD better than -20%": wf.worst_drawdown_pct > -20.0,
        "at least 100 fold trades": wf.total_trades >= 100,
    }
    print("\n=== RESEARCH GATE ===")
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4}  {label}")
    print(f"Overall: {'RESEARCH-READY FOR PAPER FORWARD TEST' if all(checks.values()) else 'NOT READY FOR REAL MONEY'}")

    output = evaluation_to_json(report, args.output)
    print(f"\nMachine-readable report: {output.resolve()}\n")
    return 0


def _ratio_text(value: float | None) -> str:
    if value is None:
        return "-"
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def _allocator_rules_from_args(args: argparse.Namespace) -> AllocationRules:
    return AllocationRules(
        top_k=args.top_k,
        recovery_top_k=args.recovery_top_k,
        bull_exposure=args.bull_exposure / 100.0,
        recovery_exposure=args.recovery_exposure / 100.0,
        neutral_exposure=args.neutral_exposure / 100.0,
        bear_exposure=args.bear_exposure / 100.0,
        rebalance_weekday=args.rebalance_weekday,
        rebalance_band=args.rebalance_band / 100.0,
        min_order_notional=args.min_order,
        emergency_bear_exit=not args.no_emergency_bear_exit,
        rebalance_on_regime_change=not args.no_regime_change_rebalance,
        recovery_confirm_days=args.recovery_confirm_days,
        neutral_confirm_days=args.neutral_confirm_days,
        strong_bull_confirm_days=args.strong_bull_confirm_days,
        recovery_reentry_lockout_days=args.reentry_lockout_days,
        fast_entry_gap_pct=args.fast_entry_gap,
        fast_exit_gap_pct=args.fast_exit_gap,
        slow_bull_mixed_to_recovery=not args.no_slow_bull_mixed_recovery,
    )


def cmd_allocator(args: argparse.Namespace) -> int:
    if args.interval != "15m":
        raise SystemExit("Allocator expects --interval 15m and resamples complete UTC daily candles internally.")

    with CandleRepository(args.db) as repo:
        candles_by_symbol = {symbol: repo.load(symbol, args.interval) for symbol in args.symbols}
        btc_candles = repo.load(args.btc_symbol, args.interval)

    missing = [s for s, rows in candles_by_symbol.items() if len(rows) < 25_000]
    if missing:
        raise SystemExit(f"Not enough 15m history for: {', '.join(missing)}. Run crypto-bot run --days 730.")
    if len(btc_candles) < 25_000:
        raise SystemExit(f"Not enough 15m history for {args.btc_symbol}. Run crypto-bot run --days 730.")

    rules = _allocator_rules_from_args(args)
    target_per_asset = args.cash * (args.bull_exposure / 100.0) / max(args.top_k, 1)
    if 0 < target_per_asset < args.min_order:
        print(
            f"WARNING: BULL target per asset is about ${target_per_asset:.2f}, below --min-order ${args.min_order:.2f}. "
            "Some target positions may be skipped; reduce --top-k or --min-order, or simulate more capital."
        )
    report = run_allocator_evaluation(
        candles_by_symbol,
        btc_candles,
        initial_cash=args.cash,
        fee_rate=args.fee / 100.0,
        slippage_rate=args.slippage / 100.0,
        warmup_days=args.warmup_days,
        fold_days=args.fold_days,
        rules=rules,
    )
    r = report.full_result
    eq = next(b for b in report.full_benchmarks if b.name == "EQUAL-WEIGHT HOLD")

    print("\n=== CRYPTO BOT — FINAL DUAL-REGIME DEADBAND ALLOCATOR 6.0 ===")
    print(f"Symbols       : {', '.join(sorted(args.symbols))}")
    print(f"Regime slow   : {args.btc_symbol} 1D close/EMA50/EMA200 + 5-day EMA50 slope")
    print(f"Regime fast   : {args.btc_symbol} 4H close/EMA20/EMA50 + 12H EMA50 slope/momentum")
    print("Ranking       : cross-sectional 30D return + 90D return + EMA50/EMA200 strength")
    print(f"Allocation    : STRONG_BULL {args.bull_exposure:.0f}% top-{args.top_k}; RECOVERY {args.recovery_exposure:.0f}% top-{args.recovery_top_k}; NEUTRAL {args.neutral_exposure:.0f}%; BEAR {args.bear_exposure:.0f}%")
    print(f"Rebalance     : weekly weekday={args.rebalance_weekday} UTC, drift band={args.rebalance_band:.1f}%")
    print(f"Min order     : ${args.min_order:.2f}; defensive exit={'OFF' if args.no_emergency_bear_exit else 'ON'}; regime-transition rebalance={'OFF' if args.no_regime_change_rebalance else 'ON'}")
    print(f"Hysteresis    : RECOVERY {args.recovery_confirm_days}d; NEUTRAL {args.neutral_confirm_days}d; STRONG_BULL {args.strong_bull_confirm_days}d; re-entry lockout {args.reentry_lockout_days}d; BEAR immediate")
    print(f"Fast deadband : enter >= {args.fast_entry_gap:+.2f}% EMA20/50 gap; hold RECOVERY until <= {args.fast_exit_gap:+.2f}%")
    print(f"Slow-bull mix : {'50% RECOVERY' if not args.no_slow_bull_mixed_recovery else 'disabled'} when 1D stays bullish but 4H is mixed")
    print(f"Costs         : fee {args.fee:.3f}%/side + slippage {args.slippage:.3f}%/side")
    print(f"Window        : {_utc_text(r.effective_start_time)} -> {_utc_text(r.effective_end_time)}")

    print("\n=== FULL EVALUATION PERIOD ===")
    print(f"Initial cash  : ${r.initial_cash:.4f}")
    print(f"Final equity  : ${r.final_equity:.4f}")
    print(f"Return        : {r.return_pct:+.2f}%")
    print(f"Annualized    : {r.annualized_return_pct:+.2f}%")
    print(f"Max drawdown  : {r.max_drawdown_pct:.2f}%")
    print(f"Volatility    : {r.annualized_volatility_pct:.2f}% annualized")
    print(f"Calmar        : {_ratio_text(r.calmar_ratio)}")
    print(f"Daily PF      : {_ratio_text(r.daily_profit_factor)}")
    print(f"Avg exposure  : {r.average_exposure_pct:.2f}%")
    print(f"Rebalances    : {r.rebalance_events} ({r.regime_change_events} on regime changes)")
    print(f"Trade legs    : {r.trade_legs}")
    print(f"Turnover      : ${r.total_turnover:.4f}")
    print(f"Costs         : ${r.total_costs:.4f} (fees ${r.total_fees:.4f} + slippage ${r.total_slippage_cost:.4f})")

    c = report.raw_control_result
    print("\n=== IMPACT VS SIMPLE DUAL-REGIME CONTROL ===")
    print("  Metric                 Raw control    Hysteresis      Delta")
    print("  ---------------------  -----------    ----------    --------")
    print(f"  Return                 {c.return_pct:+9.2f}%    {r.return_pct:+8.2f}%    {r.return_pct-c.return_pct:+7.2f}pp")
    print(f"  Max drawdown           {c.max_drawdown_pct:+9.2f}%    {r.max_drawdown_pct:+8.2f}%    {r.max_drawdown_pct-c.max_drawdown_pct:+7.2f}pp")
    print(f"  Trade legs             {c.trade_legs:11d}    {r.trade_legs:10d}    {r.trade_legs-c.trade_legs:+8d}")
    print(f"  Regime changes         {c.stabilized_regime_changes:11d}    {r.stabilized_regime_changes:10d}    {r.stabilized_regime_changes-c.stabilized_regime_changes:+8d}")
    print(f"  Turnover               ${c.total_turnover:10.4f}    ${r.total_turnover:9.4f}    ${r.total_turnover-c.total_turnover:+8.4f}")
    print(f"  Costs                  ${c.total_costs:10.4f}    ${r.total_costs:9.4f}    ${r.total_costs-c.total_costs:+8.4f}")

    print("\n=== STABILIZED REGIME DAYS ===")
    total_regime = sum(n for _, n in r.regime_days) or 1
    for name, count in r.regime_days:
        print(f"  {name:11} {count:4d} days  {count/total_regime*100:6.2f}%")

    print("\n=== HYSTERESIS DIAGNOSTICS ===")
    print(f"Raw regime changes       : {r.raw_regime_changes}")
    print(f"Stabilized regime changes: {r.stabilized_regime_changes}")
    print(f"Filtered regime changes  : {r.filtered_regime_changes}")
    print(f"Held/suppressed days     : {r.hysteresis_hold_days}")
    print(f"Re-entry lockout days    : {r.reentry_blocked_days}")

    print("\n=== BENCHMARKS (SAME PERIOD / SAME STARTING CASH) ===")
    for b in report.full_benchmarks:
        print(
            f"  {b.name:28} {b.return_pct:+8.2f}%  DD={b.max_drawdown_pct:7.2f}%  "
            f"Calmar={_ratio_text(b.calmar_ratio):>5} -> ${b.final_value:.4f}"
        )

    wf = report.walk_forward
    print(f"\n=== WALK-FORWARD ({report.fold_days}-DAY FOLDS, {report.warmup_days}-DAY WARMUP) ===")
    print("  #  Period                      Allocator    DD      Rebal  BTC hold   Equal-wt   Equal DD")
    print("  -  --------------------------  ---------  -------  -----  ---------  ---------  --------")
    for fold in wf.folds:
        start = datetime.fromtimestamp(fold.start_time/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end = datetime.fromtimestamp(fold.end_time/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(
            f"  {fold.number:>1}  {start} -> {end}  {fold.result.return_pct:+8.2f}%  "
            f"{fold.result.max_drawdown_pct:7.2f}%  {fold.result.rebalance_events:5d}  "
            f"{fold.btc_hold.return_pct:+8.2f}%  {fold.equal_weight.return_pct:+8.2f}%  {fold.equal_weight.max_drawdown_pct:8.2f}%"
        )

    print("\n=== ROBUSTNESS SUMMARY ===")
    print(f"Profitable folds       : {wf.profitable_folds}/{wf.total_folds}")
    print(f"Beat equal-weight folds: {wf.beat_equal_weight_folds}/{wf.total_folds}")
    print(f"Mean fold return       : {wf.mean_return_pct:+.2f}%")
    print(f"Median fold return     : {wf.median_return_pct:+.2f}%")
    print(f"Compounded folds       : {wf.compounded_return_pct:+.2f}%")
    print(f"Worst fold return      : {wf.worst_fold_return_pct:+.2f}%")
    print(f"Worst fold drawdown    : {wf.worst_drawdown_pct:.2f}%")
    print(f"Mean rebalance events  : {wf.mean_rebalance_events:.1f}")
    print(f"Total fold trade legs  : {wf.total_trade_legs}")

    fold_ratio = wf.profitable_folds / wf.total_folds if wf.total_folds else 0.0
    eq_calmar = eq.calmar_ratio if eq.calmar_ratio is not None else float("-inf")
    allocator_calmar = r.calmar_ratio if r.calmar_ratio is not None else float("-inf")
    checks = {
        "positive compounded walk-forward": wf.compounded_return_pct > 0,
        "profitable folds >= 60%": fold_ratio >= 0.60,
        "worst DD better than -20%": wf.worst_drawdown_pct > -20.0,
        "full-period return positive": r.return_pct > 0,
        "Calmar >= equal-weight hold": allocator_calmar >= eq_calmar,
        "costs <= 10% of initial capital": r.total_costs <= r.initial_cash * 0.10,
    }
    print("\n=== RESEARCH GATE ===")
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL':4}  {label}")
    print(f"Overall: {'RESEARCH-READY FOR PAPER FORWARD TEST' if all(checks.values()) else 'NOT READY FOR REAL MONEY'}")

    if r.events:
        print("\n=== LAST ALLOCATION EVENTS ===")
        for e in r.events[-10:]:
            leaders = ", ".join(f"{x.symbol}:{x.score:.0f}" for x in e.ranked[:3]) or "none"
            print(
                f"  {_utc_text(e.time)} raw={e.raw_regime:11} stable={e.regime:11} trigger={e.trigger:13} selected={','.join(e.selected) or 'CASH':18} "
                f"legs={len(e.legs):2d} leaders=[{leaders}]"
            )

    output = allocator_evaluation_to_json(report, args.output)
    print(f"\nMachine-readable report: {output.resolve()}\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.days <= 0:
        raise SystemExit("--days must be > 0")
    now = datetime.now(timezone.utc)
    desired_start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    symbols = list(dict.fromkeys([args.btc_symbol.upper(), *args.symbols]))

    print("=== SYNC PUBLIC BINANCE HISTORY ===")
    with BinanceMarketDataClient() as market, CandleRepository(args.db) as repo:
        for symbol in symbols:
            first, last = repo.first_last_open_time(symbol, "15m")
            needs_backfill = first is None or first > desired_start_ms + 15 * 60 * 1000
            if needs_backfill:
                start_ms = desired_start_ms
                mode = "backfill"
            else:
                start_ms = max(desired_start_ms, int(last or desired_start_ms) + 1)
                mode = "update"
            if start_ms > end_ms:
                print(f"{symbol:10} up to date")
                continue
            received = 0
            for page in market.iter_klines(symbol, "15m", start_time_ms=start_ms, end_time_ms=end_ms):
                repo.upsert_many(page)
                received += len(page)
            total = repo.count(symbol, "15m")
            print(f"{symbol:10} {mode:8} +{received:,}  stored={total:,}")

    return cmd_allocator(args)



def _paper_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _paper_trader(args: argparse.Namespace) -> PaperTrader:
    return PaperTrader(market_db=args.db, paper_db=args.paper_db)


def cmd_paper_init(args: argparse.Namespace) -> int:
    trader = _paper_trader(args)
    try:
        account = trader.init(initial_cash=args.cash, now_ms=_paper_now_ms())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("\n=== PAPER FORWARD ACCOUNT INITIALIZED ===")
    print(f"Market DB      : {Path(args.db).resolve()}")
    print(f"Paper DB       : {Path(args.paper_db).resolve()}")
    print(f"Initial cash   : ${account.initial_cash:.4f}")
    print(f"Strategy       : {account.strategy_name} {account.strategy_version}")
    print(f"Frozen hash    : {account.strategy_hash[:16]}...")
    print(f"Initialized    : {_paper_utc_text(account.initialized_at)}")
    print(f"First decision : {_paper_utc_text(account.first_eligible_day)}")
    print("\nNo historical trade was imported. The forward account starts from cash and can only act on future UTC days.")
    print("Next: crypto-bot paper tick")
    return 0


def cmd_paper_tick(args: argparse.Namespace) -> int:
    trader = _paper_trader(args)
    now_ms = _paper_now_ms()
    if not args.no_sync:
        print("=== PAPER TICK — SYNC BINANCE PUBLIC DATA ===")
    try:
        result = trader.tick(now_ms=now_ms, sync=not args.no_sync)
    except Exception as exc:
        raise SystemExit(f"Paper tick failed safely: {exc}") from exc

    if result.processed:
        print("\n=== PROCESSED UTC DAYS ===")
        for day in result.processed:
            catchup = " CATCHUP" if day.is_catchup else ""
            print(
                f"{_paper_utc_text(day.decision_time)} raw={day.raw_regime:11} "
                f"stable={day.regime:11} trigger={day.trigger:13} "
                f"legs={day.trades:2d} equity=${day.equity_after:.4f}{catchup}"
            )
    else:
        print(result.waiting_reason or "No new UTC day to process.")
    if result.skipped_already_processed:
        print(f"Idempotency: skipped {result.skipped_already_processed} already-recorded day(s).")
    print("\nUse: crypto-bot paper status")
    return 0


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def cmd_paper_status(args: argparse.Namespace) -> int:
    trader = _paper_trader(args)
    try:
        s = trader.status(now_ms=_paper_now_ms())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    a = s["account"]
    print("\n=== CRYPTO BOT — FORWARD PAPER STATUS ===")
    print(f"Strategy       : {a.strategy_name} {a.strategy_version} (FROZEN)")
    print(f"Started        : {_paper_utc_text(a.initialized_at)}")
    print(f"First eligible : {_paper_utc_text(a.first_eligible_day)}")
    print(f"Last processed : {_paper_utc_text(a.last_processed_day)}")
    print(f"Market mark    : {_paper_utc_text(s['price_time'])}")
    print(f"Initial        : ${a.initial_cash:.4f}")
    print(f"Equity         : ${s['equity']:.4f}")
    print(f"Return         : {_fmt_pct(s['return_pct'])}")
    print(f"Max DD         : {s['max_drawdown_pct']:.2f}%")
    print(f"Cash           : ${a.cash:.4f}")
    print(f"Invested       : ${s['invested']:.4f} ({s['exposure_pct']:.2f}%)")
    print(f"Regime         : {a.last_regime or '-'} (raw={a.last_raw_regime or '-'})")
    print(f"Trades         : {s['trade_count']} legs across {s['decision_count']} processed day(s)")
    print(f"Costs          : ${(a.total_fees + a.total_slippage):.4f} (fees ${a.total_fees:.4f} + slip ${a.total_slippage:.4f})")
    print(f"Turnover       : ${a.total_turnover:.4f}")

    print("\nPositions:")
    if not s["positions"]:
        print("  CASH")
    else:
        for symbol, qty in sorted(s["positions"].items()):
            price = s["prices"].get(symbol, 0.0)
            avg = s["avg_costs"].get(symbol, 0.0)
            value = qty * price
            pnl = ((price / avg - 1.0) * 100.0) if avg > 0 else 0.0
            print(f"  {symbol:10} qty={qty:.8f}  value=${value:.4f}  avg=${avg:.4f}  mark={pnl:+.2f}%")

    if s["benchmarks"]:
        print("\nForward benchmarks from the exact paper start:")
        order = ["BTCUSDT", "BNBUSDT", "ETHUSDT", "LINKUSDT", "EQUAL_WEIGHT"]
        for name in order:
            if name in s["benchmarks"]:
                print(f"  {name:14} {_fmt_pct(s['benchmarks'][name])}")

    print("\nIntegrity:")
    if s["integrity_issues"]:
        for issue in s["integrity_issues"]:
            print(f"  FAIL {issue}")
    else:
        print("  PASS SQLite + ledger/account totals consistent")

    if s["recent_trades"]:
        print("\nRecent immutable trade ledger:")
        for trade in reversed(s["recent_trades"]):
            print(
                f"  {_paper_utc_text(trade['decision_time'])} {trade['side']:4} {trade['symbol']:10} "
                f"notional=${trade['market_notional']:.4f} exec=${trade['execution_price']:.4f} "
                f"fee=${trade['fee']:.4f} slip=${trade['slippage']:.4f} {trade['trigger']}"
            )
    return 0


def cmd_paper_doctor(args: argparse.Namespace) -> int:
    trader = _paper_trader(args)
    try:
        trader.validate_market_history()
        with PaperRepository(args.paper_db) as repo:
            account = repo.get_account()
            trader._assert_frozen(account)
            issues = repo.integrity_check()
    except Exception as exc:
        print(f"FAIL {exc}")
        return 2
    print("PASS market history sufficient")
    print("PASS frozen strategy fingerprint matches")
    if issues:
        for issue in issues:
            print(f"FAIL {issue}")
        return 2
    print("PASS paper SQLite integrity and immutable ledger totals")
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-bot",
        description="Crypto paper-trading experiment using public Binance data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common_symbols = ",".join(DEFAULT_SYMBOLS)

    download = sub.add_parser("download", help="Download historical Binance Spot candles")
    download.add_argument("--symbols", type=_parse_symbols, default=list(DEFAULT_SYMBOLS), help=f"Comma-separated symbols (default: {common_symbols})")
    download.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"Kline interval (default: {DEFAULT_INTERVAL})")
    download.add_argument("--days", type=int, default=730, help="Number of historical days (default: 730)")
    download.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    download.set_defaults(func=cmd_download)

    inspect = sub.add_parser("inspect", help="Inspect candles already stored in SQLite")
    inspect.add_argument("--symbols", type=_parse_symbols, default=list(DEFAULT_SYMBOLS), help=f"Comma-separated symbols (default: {common_symbols})")
    inspect.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"Kline interval (default: {DEFAULT_INTERVAL})")
    inspect.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    inspect.set_defaults(func=cmd_inspect)

    backtest = sub.add_parser("backtest", help="Run the simulated $10 strategy over stored candles")
    backtest.add_argument("--symbols", type=_parse_symbols, default=list(RESEARCH_SYMBOLS), help="Comma-separated symbols (default: BNBUSDT,ETHUSDT,LINKUSDT)")
    backtest.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"Kline interval (default: {DEFAULT_INTERVAL})")
    backtest.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    backtest.add_argument("--cash", type=float, default=10.0, help="Initial simulated cash (default: 10)")
    sizing = backtest.add_mutually_exclusive_group()
    sizing.add_argument("--trade-size", type=float, default=None, help="Fixed cash budget per trade")
    sizing.add_argument("--trade-percent", type=float, default=None, help="Percent of CURRENT cash to use per trade (default: 50)")
    backtest.add_argument("--min-score", type=int, default=100, help="Minimum classic strategy score to become a candidate (default: 100)")
    backtest.add_argument("--min-quality", type=float, default=0.0, help="Legacy experimental quality gate; keep 0 unless explicitly researching it (default: 0)")
    backtest.add_argument("--stop-loss", type=float, default=2.0, help="Stop loss percent (default: 2)")
    backtest.add_argument("--take-profit", type=float, default=4.0, help="Take profit percent (default: 4)")
    backtest.add_argument("--fee", type=float, default=0.10, help="Simulated fee percent per side (default: 0.10)")
    backtest.add_argument("--slippage", type=float, default=0.05, help="Simulated slippage percent per side (default: 0.05)")
    backtest.add_argument("--cooldown", type=int, default=0, help="Full signal candles to skip after closing a trade (default: 0)")
    backtest.set_defaults(func=cmd_backtest)

    research = sub.add_parser("research", help="Run train/test diagnostics and automatic feature-filter research")
    research.add_argument("--symbols", type=_parse_symbols, default=list(RESEARCH_SYMBOLS), help="Comma-separated symbols (default: BNBUSDT,ETHUSDT,LINKUSDT)")
    research.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"Kline interval (default: {DEFAULT_INTERVAL})")
    research.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    research.add_argument("--cash", type=float, default=10.0, help="Initial simulated cash (default: 10)")
    research.add_argument("--trade-percent", type=float, default=50.0, help="Percent of current cash per trade (default: 50)")
    research.add_argument("--min-score", type=int, default=100, help="Classic strategy score gate (default: 100)")
    research.add_argument("--stop-loss", type=float, default=2.0, help="Stop loss percent (default: 2)")
    research.add_argument("--take-profit", type=float, default=4.0, help="Take profit percent (default: 4)")
    research.add_argument("--fee", type=float, default=0.10, help="Simulated fee percent per side (default: 0.10)")
    research.add_argument("--slippage", type=float, default=0.05, help="Simulated slippage percent per side (default: 0.05)")
    research.add_argument("--train-ratio", type=float, default=0.70, help="Chronological training fraction, 0.50-0.90 (default: 0.70)")
    research.add_argument("--top", type=int, default=8, help="Top TRAIN filters to evaluate on TEST (default: 8)")
    research.add_argument("--output", default="data/research-latest.json", help="JSON report path (default: data/research-latest.json)")
    research.set_defaults(func=cmd_research)

    evaluate = sub.add_parser("evaluate", help="Evaluate the regime + multi-timeframe pullback strategy with rolling walk-forward folds")
    evaluate.add_argument("--symbols", type=_parse_symbols, default=list(RESEARCH_SYMBOLS), help="Trade symbols (default: BNBUSDT,ETHUSDT,LINKUSDT)")
    evaluate.add_argument("--btc-symbol", default="BTCUSDT", help="BTC regime symbol (default: BTCUSDT)")
    evaluate.add_argument("--interval", default="15m", help="Base interval; must be 15m")
    evaluate.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    evaluate.add_argument("--cash", type=float, default=10.0, help="Initial simulated cash (default: 10)")
    evaluate.add_argument("--trade-percent", type=float, default=50.0, help="Percent of current cash per trade (default: 50)")
    evaluate.add_argument("--fee", type=float, default=0.10, help="Simulated fee percent per side (default: 0.10)")
    evaluate.add_argument("--slippage", type=float, default=0.05, help="Simulated slippage percent per side (default: 0.05)")
    evaluate.add_argument("--warmup-days", type=int, default=240, help="Initial history reserved before walk-forward evaluation (default: 240)")
    evaluate.add_argument("--fold-days", type=int, default=90, help="Walk-forward fold length in days (default: 90)")
    evaluate.add_argument("--atr-stop-mult", type=float, default=2.0, help="ATR multiplier for stop distance (default: 2.0)")
    evaluate.add_argument("--risk-reward", type=float, default=2.0, help="Take distance / stop distance (default: 2.0)")
    evaluate.add_argument("--min-stop", type=float, default=1.0, help="Minimum stop percent (default: 1.0)")
    evaluate.add_argument("--max-stop", type=float, default=3.0, help="Maximum stop percent (default: 3.0)")
    evaluate.add_argument("--rsi-min", type=float, default=45.0, help="Minimum 15m RSI (default: 45)")
    evaluate.add_argument("--rsi-max", type=float, default=65.0, help="Maximum 15m RSI (default: 65)")
    evaluate.add_argument("--max-momentum", type=float, default=1.5, help="Maximum 3-candle momentum percent (default: 1.5)")
    evaluate.add_argument("--max-ema-gap", type=float, default=0.60, help="Maximum EMA9/EMA21 gap percent (default: 0.60)")
    evaluate.add_argument("--min-atr", type=float, default=0.15, help="Minimum ATR percent (default: 0.15)")
    evaluate.add_argument("--max-atr", type=float, default=2.0, help="Maximum ATR percent (default: 2.0)")
    evaluate.add_argument("--pullback-tolerance", type=float, default=0.30, help="How far above EMA21 previous candle may be and still count as pullback, percent (default: 0.30)")
    evaluate.add_argument("--output", default="data/evaluate-latest.json", help="JSON report path")
    evaluate.set_defaults(func=cmd_evaluate)

    allocator = sub.add_parser("allocator", help="Evaluate the regime-aware weekly crypto allocator on stored history")
    allocator.add_argument("--symbols", type=_parse_symbols, default=list(RESEARCH_SYMBOLS), help="Trade symbols (default: BNBUSDT,ETHUSDT,LINKUSDT)")
    allocator.add_argument("--btc-symbol", default="BTCUSDT", help="BTC regime symbol (default: BTCUSDT)")
    allocator.add_argument("--interval", default="15m", help="Stored base interval; must be 15m")
    allocator.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    allocator.add_argument("--cash", type=float, default=10.0, help="Initial simulated cash (default: 10)")
    allocator.add_argument("--fee", type=float, default=0.10, help="Simulated fee percent per side (default: 0.10)")
    allocator.add_argument("--slippage", type=float, default=0.05, help="Simulated slippage percent per side (default: 0.05)")
    allocator.add_argument("--warmup-days", type=int, default=240, help="Initial daily indicator warmup (default: 240)")
    allocator.add_argument("--fold-days", type=int, default=90, help="Walk-forward fold length (default: 90)")
    allocator.add_argument("--top-k", type=int, default=2, help="Number of leaders held in STRONG_BULL (default: 2)")
    allocator.add_argument("--recovery-top-k", type=int, default=1, help="Number of leaders held in RECOVERY (default: 1)")
    allocator.add_argument("--bull-exposure", type=float, default=100.0, help="Percent invested in STRONG_BULL (default: 100)")
    allocator.add_argument("--recovery-exposure", type=float, default=50.0, help="Percent invested in RECOVERY (default: 50)")
    allocator.add_argument("--neutral-exposure", type=float, default=0.0, help="Percent invested in NEUTRAL regime (default: 0)")
    allocator.add_argument("--bear-exposure", type=float, default=0.0, help="Percent invested in BEAR regime (default: 0)")
    allocator.add_argument("--rebalance-weekday", type=int, default=0, help="UTC weekday for weekly rebalance: Monday=0..Sunday=6 (default: 0)")
    allocator.add_argument("--rebalance-band", type=float, default=10.0, help="Absolute allocation drift percent tolerated before trading (default: 10)")
    allocator.add_argument("--min-order", type=float, default=4.90, help="Minimum simulated order notional USDT (default: 4.90; allows two near-$5 legs after costs)")
    allocator.add_argument("--no-emergency-bear-exit", action="store_true", help="Disable daily defensive cash exit in NEUTRAL/BEAR")
    allocator.add_argument("--no-regime-change-rebalance", action="store_true", help="Only rebalance weekly; ignore daily regime transitions")
    allocator.add_argument("--recovery-confirm-days", type=int, default=1, help="Consecutive raw RECOVERY days required before adding risk (default: 1)")
    allocator.add_argument("--neutral-confirm-days", type=int, default=2, help="Consecutive raw NEUTRAL days required before exiting ambiguous risk-on state (default: 2)")
    allocator.add_argument("--strong-bull-confirm-days", type=int, default=1, help="Consecutive raw STRONG_BULL days required before full exposure (default: 1)")
    allocator.add_argument("--reentry-lockout-days", type=int, default=0, help="Days to block RECOVERY re-entry after confirmed risk-off exit (default: 0)")
    allocator.add_argument("--fast-entry-gap", type=float, default=0.10, help="Minimum positive 4H EMA20/EMA50 gap percent for risk-on (default: 0.10)")
    allocator.add_argument("--fast-exit-gap", type=float, default=-0.10, help="Negative 4H EMA20/EMA50 gap percent that ends RECOVERY deadband hold (default: -0.10)")
    allocator.add_argument("--no-slow-bull-mixed-recovery", action="store_true", help="Disable 50% RECOVERY exposure when daily BTC trend remains bullish but 4H is mixed")
    allocator.add_argument("--output", default="data/allocator-latest.json", help="JSON report path")
    allocator.set_defaults(func=cmd_allocator)

    run = sub.add_parser("run", help="One-command allocator pipeline: sync Binance history and evaluate")
    run.add_argument("--symbols", type=_parse_symbols, default=list(RESEARCH_SYMBOLS), help="Trade symbols (default: BNBUSDT,ETHUSDT,LINKUSDT)")
    run.add_argument("--btc-symbol", default="BTCUSDT", help="BTC regime symbol (default: BTCUSDT)")
    run.add_argument("--interval", default="15m", help="Stored base interval; must be 15m")
    run.add_argument("--days", type=int, default=730, help="History target in days (default: 730)")
    run.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")
    run.add_argument("--cash", type=float, default=10.0, help="Initial simulated cash (default: 10)")
    run.add_argument("--fee", type=float, default=0.10, help="Simulated fee percent per side (default: 0.10)")
    run.add_argument("--slippage", type=float, default=0.05, help="Simulated slippage percent per side (default: 0.05)")
    run.add_argument("--warmup-days", type=int, default=240, help="Initial daily indicator warmup (default: 240)")
    run.add_argument("--fold-days", type=int, default=90, help="Walk-forward fold length (default: 90)")
    run.add_argument("--top-k", type=int, default=2, help="Number of leaders held in STRONG_BULL (default: 2)")
    run.add_argument("--recovery-top-k", type=int, default=1, help="Number of leaders held in RECOVERY (default: 1)")
    run.add_argument("--bull-exposure", type=float, default=100.0, help="Percent invested in STRONG_BULL (default: 100)")
    run.add_argument("--recovery-exposure", type=float, default=50.0, help="Percent invested in RECOVERY (default: 50)")
    run.add_argument("--neutral-exposure", type=float, default=0.0, help="Percent invested in NEUTRAL regime (default: 0)")
    run.add_argument("--bear-exposure", type=float, default=0.0, help="Percent invested in BEAR regime (default: 0)")
    run.add_argument("--rebalance-weekday", type=int, default=0, help="UTC weekday for weekly rebalance (default: Monday=0)")
    run.add_argument("--rebalance-band", type=float, default=10.0, help="Allocation drift percent tolerated before trading (default: 10)")
    run.add_argument("--min-order", type=float, default=4.90, help="Minimum simulated order notional USDT (default: 4.90)")
    run.add_argument("--no-emergency-bear-exit", action="store_true")
    run.add_argument("--no-regime-change-rebalance", action="store_true")
    run.add_argument("--recovery-confirm-days", type=int, default=1)
    run.add_argument("--neutral-confirm-days", type=int, default=2)
    run.add_argument("--strong-bull-confirm-days", type=int, default=1)
    run.add_argument("--reentry-lockout-days", type=int, default=0)
    run.add_argument("--fast-entry-gap", type=float, default=0.10)
    run.add_argument("--fast-exit-gap", type=float, default=-0.10)
    run.add_argument("--no-slow-bull-mixed-recovery", action="store_true")
    run.add_argument("--output", default="data/allocator-latest.json")
    run.set_defaults(func=cmd_run)

    paper = sub.add_parser("paper", help="Persistent forward paper trading using the frozen 6.0 allocator")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)

    paper_init = paper_sub.add_parser("init", help="Create a new forward-only paper account")
    paper_init.add_argument("--db", default=DEFAULT_DB_PATH, help=f"Market SQLite path (default: {DEFAULT_DB_PATH})")
    paper_init.add_argument("--paper-db", default=DEFAULT_PAPER_DB, help=f"Persistent paper account path (default: {DEFAULT_PAPER_DB})")
    paper_init.add_argument("--cash", type=float, default=10.0, help="Initial fictitious USDT cash (default: 10)")
    paper_init.set_defaults(func=cmd_paper_init)

    paper_tick = paper_sub.add_parser("tick", help="Sync data and process each new UTC day exactly once")
    paper_tick.add_argument("--db", default=DEFAULT_DB_PATH, help=f"Market SQLite path (default: {DEFAULT_DB_PATH})")
    paper_tick.add_argument("--paper-db", default=DEFAULT_PAPER_DB, help=f"Persistent paper account path (default: {DEFAULT_PAPER_DB})")
    paper_tick.add_argument("--no-sync", action="store_true", help="Do not call Binance; process only already-stored market data")
    paper_tick.set_defaults(func=cmd_paper_tick)

    paper_status = paper_sub.add_parser("status", help="Show persistent paper equity, positions, benchmarks and ledger health")
    paper_status.add_argument("--db", default=DEFAULT_DB_PATH, help=f"Market SQLite path (default: {DEFAULT_DB_PATH})")
    paper_status.add_argument("--paper-db", default=DEFAULT_PAPER_DB, help=f"Persistent paper account path (default: {DEFAULT_PAPER_DB})")
    paper_status.set_defaults(func=cmd_paper_status)

    paper_doctor = paper_sub.add_parser("doctor", help="Validate market history, frozen config and paper ledger integrity")
    paper_doctor.add_argument("--db", default=DEFAULT_DB_PATH, help=f"Market SQLite path (default: {DEFAULT_DB_PATH})")
    paper_doctor.add_argument("--paper-db", default=DEFAULT_PAPER_DB, help=f"Persistent paper account path (default: {DEFAULT_PAPER_DB})")
    paper_doctor.set_defaults(func=cmd_paper_doctor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "days", 1) <= 0:
        parser.error("--days must be > 0")
    if getattr(args, "trade_size", None) is None and getattr(args, "trade_percent", None) is None and getattr(args, "command", None) == "backtest":
        args.trade_percent = 50.0
    if getattr(args, "trade_percent", None) is not None and not 0 < args.trade_percent <= 100:
        parser.error("--trade-percent must be > 0 and <= 100")
    if getattr(args, "trade_size", None) is not None and args.trade_size <= 0:
        parser.error("--trade-size must be > 0")
    if getattr(args, "cooldown", 0) < 0:
        parser.error("--cooldown cannot be negative")
    if getattr(args, "min_quality", 0.0) < 0 or getattr(args, "min_quality", 0.0) > 100:
        parser.error("--min-quality must be between 0 and 100")
    if getattr(args, "command", None) == "research":
        if not 0 < args.trade_percent <= 100:
            parser.error("--trade-percent must be > 0 and <= 100")
        if not 0.50 <= args.train_ratio <= 0.90:
            parser.error("--train-ratio must be between 0.50 and 0.90")
        if args.top <= 0:
            parser.error("--top must be > 0")
    if getattr(args, "command", None) in {"allocator", "run"}:
        if args.top_k <= 0:
            parser.error("--top-k must be > 0")
        if args.recovery_top_k <= 0:
            parser.error("--recovery-top-k must be > 0")
        for name in ("bull_exposure", "recovery_exposure", "neutral_exposure", "bear_exposure"):
            value = getattr(args, name)
            if not 0 <= value <= 100:
                parser.error(f"--{name.replace('_','-')} must be between 0 and 100")
        if not 0 <= args.rebalance_weekday <= 6:
            parser.error("--rebalance-weekday must be 0..6")
        if not 0 <= args.rebalance_band <= 100:
            parser.error("--rebalance-band must be between 0 and 100")
        if args.min_order < 0:
            parser.error("--min-order cannot be negative")
        if args.warmup_days < 205:
            parser.error("--warmup-days must be at least 205 for daily EMA200")
        if args.fold_days < 30:
            parser.error("--fold-days must be at least 30")
        for name in ("recovery_confirm_days", "neutral_confirm_days", "strong_bull_confirm_days"):
            if getattr(args, name) < 1:
                parser.error(f"--{name.replace('_','-')} must be >= 1")
        if args.reentry_lockout_days < 0:
            parser.error("--reentry-lockout-days cannot be negative")
        if args.fast_exit_gap > args.fast_entry_gap:
            parser.error("--fast-exit-gap must be <= --fast-entry-gap")

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
