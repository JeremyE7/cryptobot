from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("data/paper.db")
STATUS = Path("data/cloud-status.txt")
OUT = Path("site/data.json")


def iso(ms):
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if ms
        else None
    )


def parse_status(text: str) -> dict[str, object]:
    def number(label: str) -> float | None:
        m = re.search(rf"^{re.escape(label)}\s*:\s*\$?([-+]?\d+(?:\.\d+)?)", text, re.M)
        return float(m.group(1)) if m else None

    def percent(label: str) -> float | None:
        m = re.search(rf"^{re.escape(label)}\s*:\s*([-+]?\d+(?:\.\d+)?)%", text, re.M)
        return float(m.group(1)) if m else None

    regime_match = re.search(r"^Regime\s*:\s*([^\s]+)", text, re.M)
    updated_match = re.search(r"^Market mark\s*:\s*(.+)$", text, re.M)
    trades_match = re.search(r"^Trades\s*:\s*(\d+) legs", text, re.M)

    positions = []
    in_positions = False
    for line in text.splitlines():
        if line.strip() == "Positions:":
            in_positions = True
            continue
        if in_positions and not line.startswith("  "):
            break
        if not in_positions or not line.strip() or line.strip() == "CASH":
            continue
        m = re.match(
            r"\s*(\S+)\s+qty=([0-9.eE+-]+)\s+value=\$([0-9.eE+-]+)",
            line,
        )
        if m:
            positions.append(
                {
                    "symbol": m.group(1),
                    "quantity": float(m.group(2)),
                    "value": float(m.group(3)),
                }
            )

    return {
        "equity": number("Equity"),
        "return_pct": percent("Return"),
        "cash": number("Cash"),
        "invested": number("Invested"),
        "max_drawdown_pct": percent("Max DD"),
        "costs": number("Costs"),
        "regime": regime_match.group(1) if regime_match else None,
        "updated_at": updated_match.group(1).strip() if updated_match else None,
        "trades": int(trades_match.group(1)) if trades_match else None,
        "positions": positions,
    }


with sqlite3.connect(DB) as c:
    c.row_factory = sqlite3.Row
    a = c.execute("select * from account where id=1").fetchone()
    snaps = c.execute(
        "select snapshot_time,equity,cash,invested,regime from snapshots where kind='DAY_CLOSE' order by snapshot_time"
    ).fetchall()
    if not snaps:
        snaps = c.execute(
            "select snapshot_time,equity,cash,invested,regime from snapshots order by snapshot_time"
        ).fetchall()

    latest = snaps[-1] if snaps else None
    position_rows = c.execute(
        "select symbol,quantity,avg_cost from positions order by symbol"
    ).fetchall()
    decisions = c.execute(
        "select decision_time,regime,trigger from decisions order by decision_time desc limit 12"
    ).fetchall()
    trade_count = c.execute("select count(*) n from trades").fetchone()["n"]

    initial = float(a["initial_cash"])
    equity = float(latest["equity"]) if latest else float(a["cash"])
    cash = float(a["cash"])
    invested = float(latest["invested"]) if latest else max(0.0, equity - cash)

    peak = initial
    mdd = 0.0
    for s in snaps:
        e = float(s["equity"])
        peak = max(peak, e)
        mdd = min(mdd, (e / peak - 1) * 100)

    status = parse_status(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    equity = float(status.get("equity") or equity)
    cash = float(status.get("cash") if status.get("cash") is not None else cash)
    invested = float(
        status.get("invested") if status.get("invested") is not None else invested
    )
    mdd = float(
        status.get("max_drawdown_pct")
        if status.get("max_drawdown_pct") is not None
        else mdd
    )

    parsed_positions = status.get("positions") or []
    if parsed_positions:
        positions = [
            {
                "symbol": p["symbol"],
                "value": round(float(p["value"]), 6),
                "weight_pct": round(float(p["value"]) / equity * 100, 2)
                if equity
                else 0,
            }
            for p in parsed_positions
        ]
    else:
        positions = []
        for p in position_rows:
            value = float(p["quantity"]) * float(p["avg_cost"])
            positions.append(
                {
                    "symbol": p["symbol"],
                    "value": round(value, 6),
                    "weight_pct": round(value / equity * 100, 2) if equity else 0,
                }
            )

    payload = {
        "strategy": f"{a['strategy_name']} {a['strategy_version']}",
        "started_at": iso(a["initialized_at"]),
        "updated_at": status.get("updated_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "equity": round(equity, 8),
        "return_pct": round(
            float(status.get("return_pct"))
            if status.get("return_pct") is not None
            else (equity / initial - 1) * 100,
            4,
        ),
        "cash": round(cash, 8),
        "invested": round(invested, 8),
        "max_drawdown_pct": round(mdd, 4),
        "regime": status.get("regime") or a["last_regime"],
        "trades": int(status.get("trades") if status.get("trades") is not None else trade_count),
        "costs": round(
            float(status.get("costs"))
            if status.get("costs") is not None
            else float(a["total_fees"]) + float(a["total_slippage"]),
            8,
        ),
        "integrity": True,
        "positions": positions,
        "equity_curve": [
            {"time": iso(s["snapshot_time"]), "equity": round(float(s["equity"]), 8)}
            for s in snaps
        ]
        + ([{"time": status.get("updated_at"), "equity": round(equity, 8)}] if status else []),
        "decisions": [
            {
                "date": iso(d["decision_time"]).split(" ")[0],
                "regime": d["regime"],
                "trigger": d["trigger"],
            }
            for d in decisions
        ],
    }

OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"Wrote {OUT}: equity=${equity:.4f}, {len(snaps)} snapshots, {trade_count} trades")
