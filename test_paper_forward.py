from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from crypto_bot.backtest.allocator_engine import AllocatorBacktestEngine, DAY_MS
from crypto_bot.market.binance import Candle
from crypto_bot.paper import PaperRepository, PaperTrader, next_utc_day
from crypto_bot.storage.sqlite import CandleRepository
from crypto_bot.strategy.allocator import AllocationRules

STEP = 15 * 60 * 1000


def market(symbol: str, days: int, daily_fn) -> list[Candle]:
    rows: list[Candle] = []
    for d in range(days):
        base = daily_fn(d)
        for slot in range(96):
            t = d * DAY_MS + slot * STEP
            p = base * (1 + (slot - 48) * 0.000002)
            rows.append(
                Candle(
                    symbol,
                    "15m",
                    t,
                    p,
                    p * 1.001,
                    p * 0.999,
                    p,
                    10,
                    t + STEP - 1,
                    100,
                    1,
                    5,
                    50,
                )
            )
    return rows


@pytest.fixture(scope="module")
def synthetic_market(tmp_path_factory) -> tuple[Path, dict[str, list[Candle]]]:
    path = tmp_path_factory.mktemp("paper-market") / "market.db"
    days = 280
    data = {
        "BTCUSDT": market("BTCUSDT", days, lambda d: 100 + d * 0.50),
        "BNBUSDT": market("BNBUSDT", days, lambda d: 40 + d * 0.35),
        "ETHUSDT": market("ETHUSDT", days, lambda d: 60 + d * 0.25),
        "LINKUSDT": market("LINKUSDT", days, lambda d: 25 + d * 0.08),
    }
    with CandleRepository(path) as repo:
        for rows in data.values():
            repo.upsert_many(rows)
    return path, data


def trader(market_db: Path, paper_db: Path) -> PaperTrader:
    return PaperTrader(market_db=market_db, paper_db=paper_db)


def test_paper_init_is_forward_only_and_next_utc_day(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    now = 245 * DAY_MS + 12 * 60 * 60 * 1000
    account = trader(market_db, paper_db).init(initial_cash=10, now_ms=now)
    assert account.cash == 10
    assert account.last_processed_day is None
    assert account.first_eligible_day == next_utc_day(now) == 246 * DAY_MS
    with PaperRepository(paper_db) as repo:
        assert repo.trade_count() == 0
        assert repo.decision_count() == 0


def test_paper_tick_persists_positions_and_is_idempotent(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)

    now = 246 * DAY_MS + 10 * 60 * 1000
    first = t.tick(now_ms=now, sync=False)
    assert len(first.processed) == 1
    assert first.processed[0].decision_time == 246 * DAY_MS
    assert first.processed[0].trigger == "START"

    with PaperRepository(paper_db) as repo:
        trades_before = repo.trade_count()
        decisions_before = repo.decision_count()
        account = repo.get_account()
        assert account.last_processed_day == 246 * DAY_MS
        assert repo.get_positions()  # rising synthetic market should be risk-on

    second = t.tick(now_ms=now, sync=False)
    assert not second.processed
    assert "already processed" in (second.waiting_reason or "").lower()
    with PaperRepository(paper_db) as repo:
        assert repo.trade_count() == trades_before
        assert repo.decision_count() == decisions_before
        assert not repo.integrity_check()


def test_paper_tick_catches_up_missed_days_exactly_once(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)

    result = t.tick(now_ms=250 * DAY_MS + 10 * 60 * 1000, sync=False)
    assert [x.decision_time for x in result.processed] == [d * DAY_MS for d in range(246, 251)]
    assert all(x.is_catchup for x in result.processed[:-1])
    assert result.processed[-1].is_catchup is False

    again = t.tick(now_ms=250 * DAY_MS + 10 * 60 * 1000, sync=False)
    assert not again.processed
    with PaperRepository(paper_db) as repo:
        assert repo.decision_count() == 5
        assert not repo.integrity_check()


def test_frozen_strategy_fingerprint_rejects_tuning_mid_forward(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    trader(market_db, paper_db).init(initial_cash=10, now_ms=245 * DAY_MS + 1)
    altered = PaperTrader(
        market_db=market_db,
        paper_db=paper_db,
        rules=AllocationRules(fast_entry_gap_pct=0.12, fast_exit_gap_pct=-0.12),
    )
    with pytest.raises(ValueError, match="Frozen strategy mismatch"):
        altered.tick(now_ms=247 * DAY_MS + 10 * 60 * 1000, sync=False)


def test_trade_ledger_is_immutable_and_totals_match_account(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)
    t.tick(now_ms=260 * DAY_MS + 10 * 60 * 1000, sync=False)
    with PaperRepository(paper_db) as repo:
        account = repo.get_account()
        fees = repo.conn.execute("SELECT COALESCE(SUM(fee),0) FROM trades").fetchone()[0]
        slip = repo.conn.execute("SELECT COALESCE(SUM(slippage),0) FROM trades").fetchone()[0]
        turnover = repo.conn.execute("SELECT COALESCE(SUM(market_notional),0) FROM trades").fetchone()[0]
        assert float(fees) == pytest.approx(account.total_fees)
        assert float(slip) == pytest.approx(account.total_slippage)
        assert float(turnover) == pytest.approx(account.total_turnover)
        assert not repo.integrity_check()


def test_status_reports_forward_benchmarks_and_integrity(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)
    t.tick(now_ms=250 * DAY_MS + 10 * 60 * 1000, sync=False)
    status = t.status(now_ms=250 * DAY_MS + 10 * 60 * 1000)
    assert status["equity"] > 0
    assert "BTCUSDT" in status["benchmarks"]
    assert "EQUAL_WEIGHT" in status["benchmarks"]
    assert not status["integrity_issues"]


def test_persistent_paper_matches_backtest_day_close_equity(synthetic_market, tmp_path):
    market_db, data = synthetic_market
    paper_db = tmp_path / "paper.db"
    start_day = 246
    compare_end_day = 258
    # Initialize during the prior day so the first forward decision is start_day.
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=(start_day - 1) * DAY_MS + 12 * 60 * 60 * 1000)
    # Processing day end+1 first records end_day's DAY_CLOSE snapshot, then may
    # make a later trade. We compare the immutable snapshot before that later trade.
    t.tick(now_ms=(compare_end_day + 1) * DAY_MS + 10 * 60 * 1000, sync=False)

    result = AllocatorBacktestEngine(
        initial_cash=10,
        fee_rate=0.001,
        slippage_rate=0.0005,
        rules=AllocationRules(),
        trade_start_time=start_day * DAY_MS,
        trade_end_time=compare_end_day * DAY_MS,
    ).run(
        {s: data[s] for s in ("BNBUSDT", "ETHUSDT", "LINKUSDT")},
        data["BTCUSDT"],
    )

    with PaperRepository(paper_db) as repo:
        row = repo.conn.execute(
            "SELECT equity FROM snapshots WHERE snapshot_time=? AND kind='DAY_CLOSE'",
            ((compare_end_day + 1) * DAY_MS - 1,),
        ).fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(result.final_equity, rel=0, abs=1e-10)

        paper_legs = repo.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE decision_time <= ?",
            (compare_end_day * DAY_MS,),
        ).fetchone()[0]
        assert int(paper_legs) == result.trade_legs


def test_sqlite_primary_key_prevents_duplicate_decision_ledger(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)
    t.tick(now_ms=246 * DAY_MS + 10 * 60 * 1000, sync=False)
    with PaperRepository(paper_db) as repo:
        row = repo.conn.execute("SELECT * FROM decisions LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            repo.conn.execute(
                """
                INSERT INTO decisions (
                    decision_time, signal_time, raw_regime, regime, reason, trigger,
                    selected_json, ranked_json, target_weights_json, target_exposure,
                    equity_before, equity_after, fees, slippage, turnover,
                    pending_regime, pending_days, lockout_remaining, is_catchup, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row),
            )


def test_trade_and_decision_rows_cannot_be_mutated(synthetic_market, tmp_path):
    market_db, _ = synthetic_market
    paper_db = tmp_path / "paper.db"
    t = trader(market_db, paper_db)
    t.init(initial_cash=10, now_ms=245 * DAY_MS + 12 * 60 * 60 * 1000)
    t.tick(now_ms=246 * DAY_MS + 10 * 60 * 1000, sync=False)
    with PaperRepository(paper_db) as repo:
        with pytest.raises(sqlite3.IntegrityError, match="decision ledger is immutable"):
            repo.conn.execute("UPDATE decisions SET trigger='HACK' WHERE decision_time=?", (246 * DAY_MS,))
        repo.conn.rollback()
        if repo.trade_count():
            trade_id = repo.conn.execute("SELECT id FROM trades LIMIT 1").fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="trade ledger is immutable"):
                repo.conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
            repo.conn.rollback()
