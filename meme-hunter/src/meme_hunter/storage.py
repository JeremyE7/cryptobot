from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  token_address TEXT NOT NULL,
  pair_address TEXT NOT NULL,
  symbol TEXT,
  name TEXT,
  price_usd REAL,
  liquidity_usd REAL,
  volume_m5 REAL,
  volume_h1 REAL,
  price_change_m5 REAL,
  price_change_h1 REAL,
  buys_m5 INTEGER,
  sells_m5 INTEGER,
  pair_created_at INTEGER,
  top1_pct REAL,
  top5_pct REAL,
  top20_pct REAL,
  score REAL NOT NULL,
  risk TEXT NOT NULL,
  vetoes_json TEXT NOT NULL,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_token_time ON observations(token_address, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_score_time ON observations(score DESC, observed_at DESC);
"""


def connect(path: str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    return conn


def save_observation(conn: sqlite3.Connection, pair: dict, concentration: dict, score) -> None:
    base = pair.get("baseToken") or {}
    conn.execute(
        """INSERT INTO observations (
          token_address,pair_address,symbol,name,price_usd,liquidity_usd,volume_m5,volume_h1,
          price_change_m5,price_change_h1,buys_m5,sells_m5,pair_created_at,top1_pct,top5_pct,top20_pct,
          score,risk,vetoes_json,raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            base.get("address"), pair.get("pairAddress"), base.get("symbol"), base.get("name"),
            float(pair.get("priceUsd") or 0), float((pair.get("liquidity") or {}).get("usd") or 0),
            float((pair.get("volume") or {}).get("m5") or 0), float((pair.get("volume") or {}).get("h1") or 0),
            float((pair.get("priceChange") or {}).get("m5") or 0), float((pair.get("priceChange") or {}).get("h1") or 0),
            int(((pair.get("txns") or {}).get("m5") or {}).get("buys") or 0),
            int(((pair.get("txns") or {}).get("m5") or {}).get("sells") or 0),
            pair.get("pairCreatedAt"), concentration.get("top1_pct"), concentration.get("top5_pct"), concentration.get("top20_pct"),
            score.score, score.risk, json.dumps(score.vetoes), json.dumps(pair, separators=(",", ":")),
        ),
    )
    conn.commit()
