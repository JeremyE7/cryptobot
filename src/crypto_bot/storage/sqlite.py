from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from crypto_bot.market.binance import Candle


SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    close_time INTEGER NOT NULL,
    quote_asset_volume REAL NOT NULL,
    number_of_trades INTEGER NOT NULL,
    taker_buy_base_volume REAL NOT NULL,
    taker_buy_quote_volume REAL NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_time
ON candles(symbol, interval, open_time);
"""


class CandleRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CandleRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_many(self, candles: Iterable[Candle]) -> int:
        rows = [
            (
                c.symbol,
                c.interval,
                c.open_time,
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
                c.close_time,
                c.quote_asset_volume,
                c.number_of_trades,
                c.taker_buy_base_volume,
                c.taker_buy_quote_volume,
            )
            for c in candles
        ]
        if not rows:
            return 0

        self._conn.executemany(
            """
            INSERT INTO candles (
                symbol, interval, open_time, open, high, low, close, volume,
                close_time, quote_asset_volume, number_of_trades,
                taker_buy_base_volume, taker_buy_quote_volume
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                close_time=excluded.close_time,
                quote_asset_volume=excluded.quote_asset_volume,
                number_of_trades=excluded.number_of_trades,
                taker_buy_base_volume=excluded.taker_buy_base_volume,
                taker_buy_quote_volume=excluded.taker_buy_quote_volume
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def count(self, symbol: str, interval: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol = ? AND interval = ?",
            (symbol.upper(), interval),
        ).fetchone()
        return int(row[0])

    def first_last_open_time(self, symbol: str, interval: str) -> tuple[int | None, int | None]:
        row = self._conn.execute(
            """
            SELECT MIN(open_time), MAX(open_time)
            FROM candles
            WHERE symbol = ? AND interval = ?
            """,
            (symbol.upper(), interval),
        ).fetchone()
        return row[0], row[1]

    def load(self, symbol: str, interval: str) -> list[Candle]:
        rows = self._conn.execute(
            """
            SELECT symbol, interval, open_time, open, high, low, close, volume,
                   close_time, quote_asset_volume, number_of_trades,
                   taker_buy_base_volume, taker_buy_quote_volume
            FROM candles
            WHERE symbol = ? AND interval = ?
            ORDER BY open_time ASC
            """,
            (symbol.upper(), interval),
        ).fetchall()
        return [
            Candle(
                symbol=row[0],
                interval=row[1],
                open_time=int(row[2]),
                open=float(row[3]),
                high=float(row[4]),
                low=float(row[5]),
                close=float(row[6]),
                volume=float(row[7]),
                close_time=int(row[8]),
                quote_asset_volume=float(row[9]),
                number_of_trades=int(row[10]),
                taker_buy_base_volume=float(row[11]),
                taker_buy_quote_volume=float(row[12]),
            )
            for row in rows
        ]
