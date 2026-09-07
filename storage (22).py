from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    strategy_config_json TEXT NOT NULL,
    initialized_at INTEGER NOT NULL,
    first_eligible_day INTEGER NOT NULL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    last_processed_day INTEGER,
    last_regime TEXT,
    last_raw_regime TEXT,
    pending_regime TEXT,
    pending_days INTEGER NOT NULL DEFAULT 0,
    lockout_remaining INTEGER NOT NULL DEFAULT 0,
    benchmark_start_time INTEGER,
    benchmark_start_prices_json TEXT,
    total_fees REAL NOT NULL DEFAULT 0,
    total_slippage REAL NOT NULL DEFAULT 0,
    total_turnover REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity REAL NOT NULL CHECK (quantity >= 0),
    avg_cost REAL NOT NULL CHECK (avg_cost >= 0),
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_time INTEGER PRIMARY KEY,
    signal_time INTEGER NOT NULL,
    raw_regime TEXT NOT NULL,
    regime TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger TEXT NOT NULL,
    selected_json TEXT NOT NULL,
    ranked_json TEXT NOT NULL,
    target_weights_json TEXT NOT NULL,
    target_exposure REAL NOT NULL,
    equity_before REAL NOT NULL,
    equity_after REAL NOT NULL,
    fees REAL NOT NULL,
    slippage REAL NOT NULL,
    turnover REAL NOT NULL,
    pending_regime TEXT,
    pending_days INTEGER NOT NULL,
    lockout_remaining INTEGER NOT NULL,
    is_catchup INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_time INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    symbol TEXT NOT NULL,
    market_price REAL NOT NULL,
    execution_price REAL NOT NULL,
    quantity REAL NOT NULL,
    market_notional REAL NOT NULL,
    fee REAL NOT NULL,
    slippage REAL NOT NULL,
    trigger TEXT NOT NULL,
    raw_regime TEXT NOT NULL,
    regime TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(decision_time, sequence),
    FOREIGN KEY(decision_time) REFERENCES decisions(decision_time)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_time INTEGER NOT NULL,
    kind TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    invested REAL NOT NULL,
    regime TEXT,
    raw_regime TEXT,
    prices_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(snapshot_time, kind)
);

CREATE INDEX IF NOT EXISTS idx_trades_decision_time ON trades(decision_time);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(snapshot_time);

CREATE TRIGGER IF NOT EXISTS trg_trades_no_update
BEFORE UPDATE ON trades BEGIN
    SELECT RAISE(ABORT, 'trade ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_trades_no_delete
BEFORE DELETE ON trades BEGIN
    SELECT RAISE(ABORT, 'trade ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update
BEFORE UPDATE ON decisions BEGIN
    SELECT RAISE(ABORT, 'decision ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete
BEFORE DELETE ON decisions BEGIN
    SELECT RAISE(ABORT, 'decision ledger is immutable');
END;
"""


@dataclass(frozen=True, slots=True)
class PaperAccount:
    strategy_name: str
    strategy_version: str
    strategy_hash: str
    strategy_config_json: str
    initialized_at: int
    first_eligible_day: int
    initial_cash: float
    cash: float
    last_processed_day: int | None
    last_regime: str | None
    last_raw_regime: str | None
    pending_regime: str | None
    pending_days: int
    lockout_remaining: int
    benchmark_start_time: int | None
    benchmark_start_prices: dict[str, float] | None
    total_fees: float
    total_slippage: float
    total_turnover: float


class PaperRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(PAPER_SCHEMA)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PaperRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def is_initialized(self) -> bool:
        return self._conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() is not None

    def initialize(
        self,
        *,
        strategy_name: str,
        strategy_version: str,
        strategy_hash: str,
        strategy_config_json: str,
        initialized_at: int,
        first_eligible_day: int,
        initial_cash: float,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        with self.immediate_transaction() as conn:
            if conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() is not None:
                raise ValueError("Paper account is already initialized")
            conn.execute(
                """
                INSERT INTO account (
                    id, strategy_name, strategy_version, strategy_hash, strategy_config_json,
                    initialized_at, first_eligible_day, initial_cash, cash, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    strategy_version,
                    strategy_hash,
                    strategy_config_json,
                    initialized_at,
                    first_eligible_day,
                    float(initial_cash),
                    float(initial_cash),
                    initialized_at,
                    initialized_at,
                ),
            )

    def get_account(self, conn: sqlite3.Connection | None = None) -> PaperAccount:
        c = conn or self._conn
        row = c.execute("SELECT * FROM account WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Paper account is not initialized. Run: crypto-bot paper init")
        prices = json.loads(row["benchmark_start_prices_json"]) if row["benchmark_start_prices_json"] else None
        return PaperAccount(
            strategy_name=row["strategy_name"],
            strategy_version=row["strategy_version"],
            strategy_hash=row["strategy_hash"],
            strategy_config_json=row["strategy_config_json"],
            initialized_at=int(row["initialized_at"]),
            first_eligible_day=int(row["first_eligible_day"]),
            initial_cash=float(row["initial_cash"]),
            cash=float(row["cash"]),
            last_processed_day=int(row["last_processed_day"]) if row["last_processed_day"] is not None else None,
            last_regime=row["last_regime"],
            last_raw_regime=row["last_raw_regime"],
            pending_regime=row["pending_regime"],
            pending_days=int(row["pending_days"]),
            lockout_remaining=int(row["lockout_remaining"]),
            benchmark_start_time=int(row["benchmark_start_time"]) if row["benchmark_start_time"] is not None else None,
            benchmark_start_prices={str(k): float(v) for k, v in prices.items()} if prices else None,
            total_fees=float(row["total_fees"]),
            total_slippage=float(row["total_slippage"]),
            total_turnover=float(row["total_turnover"]),
        )

    def get_positions(self, conn: sqlite3.Connection | None = None) -> dict[str, tuple[float, float]]:
        c = conn or self._conn
        rows = c.execute("SELECT symbol, quantity, avg_cost FROM positions ORDER BY symbol").fetchall()
        return {str(r["symbol"]): (float(r["quantity"]), float(r["avg_cost"])) for r in rows}

    def decision_exists(self, decision_time: int, conn: sqlite3.Connection | None = None) -> bool:
        c = conn or self._conn
        return c.execute("SELECT 1 FROM decisions WHERE decision_time=?", (decision_time,)).fetchone() is not None

    def trade_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def decision_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def recent_trades(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM trades ORDER BY decision_time DESC, sequence DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        )

    def snapshot_equities(self) -> list[float]:
        rows = self._conn.execute(
            "SELECT equity FROM snapshots WHERE kind IN ('DAY_CLOSE','POST_TRADE') ORDER BY snapshot_time, CASE kind WHEN 'DAY_CLOSE' THEN 0 ELSE 1 END"
        ).fetchall()
        return [float(r[0]) for r in rows]

    def latest_decision(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM decisions ORDER BY decision_time DESC LIMIT 1").fetchone()

    def integrity_check(self) -> list[str]:
        issues: list[str] = []
        result = self._conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            issues.append(f"SQLite integrity_check: {result[0] if result else 'no result'}")
        if not self.is_initialized():
            issues.append("Paper account is not initialized")
            return issues
        account = self.get_account()
        if account.cash < -1e-9:
            issues.append(f"Negative cash: {account.cash}")
        if any(qty < -1e-12 for qty, _ in self.get_positions().values()):
            issues.append("Negative position quantity")
        max_day = self._conn.execute("SELECT MAX(decision_time) FROM decisions").fetchone()[0]
        if (int(max_day) if max_day is not None else None) != account.last_processed_day:
            issues.append("account.last_processed_day does not match decisions ledger")
        fees = float(self._conn.execute("SELECT COALESCE(SUM(fee),0) FROM trades").fetchone()[0])
        slip = float(self._conn.execute("SELECT COALESCE(SUM(slippage),0) FROM trades").fetchone()[0])
        turnover = float(self._conn.execute("SELECT COALESCE(SUM(market_notional),0) FROM trades").fetchone()[0])
        if abs(fees - account.total_fees) > 1e-8:
            issues.append("Account fee total does not match trade ledger")
        if abs(slip - account.total_slippage) > 1e-8:
            issues.append("Account slippage total does not match trade ledger")
        if abs(turnover - account.total_turnover) > 1e-8:
            issues.append("Account turnover does not match trade ledger")
        return issues
