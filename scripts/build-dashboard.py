from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path("data/paper.db")
STATUS = Path("data/cloud-status.txt")
OUT = Path("site/data.json")

REGIME_META = {
    "STRONG_BULL": {
        "label": "ALCISTA FUERTE",
        "exposure": "100%",
        "detail": "Tendencia diaria y de 4 horas alineadas al alza. Máxima exposición en los 2 líderes.",
    },
    "RECOVERY": {
        "label": "RECUPERACIÓN",
        "exposure": "70%",
        "detail": "El mercado mejora, pero todavía no confirma. Exposición parcial en los 2 líderes.",
    },
    "NEUTRAL": {
        "label": "ZONA MIXTA",
        "exposure": "hasta 40%",
        "detail": "Señales encontradas. Compra solo si hay un líder con tendencia positiva; si no, espera en efectivo.",
    },
    "BEAR": {
        "label": "BAJISTA",
        "exposure": "0%",
        "detail": "Tendencia bajista confirmada. Protección total: todo a efectivo.",
    },
}

REASON_TEXT = {
    "STABLE": "Sin cambio de régimen: mantiene la asignación actual.",
    "START": "Primera decisión de la cuenta: asignó capital según la señal.",
    "RISK_ON_CONFIRMED": "Se confirmó una señal de compra: el mercado volvió a modo riesgo.",
    "BULL_CONFIRMATION": "Se confirmó el régimen alcista: subió la exposición.",
    "DE_RISK_TO_RECOVERY": "El impulso de corto plazo se debilitó: redujo riesgo.",
    "EXIT_DEADBAND_HOLD": "La señal de salida todavía es débil: mantiene la posición.",
    "NEUTRAL_CONFIRMED": "Se confirmó el cambio a zona mixta: pasó a efectivo.",
    "ENTRY_DEADBAND": "La señal de compra no supera el filtro mínimo: espera.",
    "BEAR_IMMEDIATE": "Señal bajista: salida inmediata a efectivo.",
    "BEAR_TO_NEUTRAL": "El mercado dejó de ser bajista: espera confirmación antes de comprar.",
    "INITIAL_RISK_OFF": "La cuenta arrancó sin una señal clara de compra.",
    "REGIME_CONFIRMED": "Se confirmó un cambio de régimen.",
    "REENTRY_LOCKOUT": "Bloqueo temporal de reentrada después de una salida.",
}

TRIGGER_TEXT = {
    "START": "Arranque",
    "DEFENSIVE": "Protección",
    "REGIME_CHANGE": "Cambio de régimen",
    "WEEKLY": "Revisión semanal",
    "NONE": "Revisión diaria",
}


def iso(ms):
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if ms
        else None
    )


def date_only(ms):
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
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

    regime_match = re.search(r"^Regime\s*:\s*([^\s]+)(?:\s*\(raw=([^)]+)\))?", text, re.M)
    updated_match = re.search(r"^Market mark\s*:\s*(.+)$", text, re.M)
    started_match = re.search(r"^Started\s*:\s*(.+)$", text, re.M)
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

    benchmarks = {}
    in_benchmarks = False
    for line in text.splitlines():
        if "Forward benchmarks" in line:
            in_benchmarks = True
            continue
        if not in_benchmarks:
            continue
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        m = re.match(r"\s*(\S+)\s+([-+]?\d+(?:\.\d+)?)%", line)
        if m:
            benchmarks[m.group(1)] = float(m.group(2))

    return {
        "equity": number("Equity"),
        "return_pct": percent("Return"),
        "cash": number("Cash"),
        "invested": number("Invested"),
        "max_drawdown_pct": percent("Max DD"),
        "costs": number("Costs"),
        "regime": regime_match.group(1) if regime_match else None,
        "raw_regime": regime_match.group(2) if regime_match else None,
        "updated_at": updated_match.group(1).strip() if updated_match else None,
        "started_at": started_match.group(1).strip() if started_match else None,
        "trades": int(trades_match.group(1)) if trades_match else None,
        "positions": positions,
        "benchmarks": benchmarks,
    }


def reason_text(reason: str) -> str:
    if reason.startswith("WAIT_NEUTRAL_"):
        return "Esperando confirmación para salir a efectivo."
    if reason.startswith("WAIT_RECOVERY_") or reason.startswith("WAIT_STRONG_BULL_"):
        return "Esperando confirmación para aumentar la compra."
    if reason.startswith("WAIT_"):
        return "Esperando confirmación antes de cambiar la posición."
    return REASON_TEXT.get(reason, reason.replace("_", " ").capitalize())


def decision_action(decision, trades) -> tuple[str, str]:
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]
    if buys and sells:
        return "REBALANCEÓ", "rebalance"
    if buys:
        return "COMPRÓ", "buy"
    if sells:
        return "VENDIÓ", "sell"
    if float(decision["target_exposure"] or 0) > 0:
        return "MANTIENE", "hold"
    return "ESPERÓ", "cash"


def decision_summary(action: str, decision, trades) -> str:
    selected = json.loads(decision["selected_json"] or "[]")
    target_pct = float(decision["target_exposure"] or 0) * 100.0
    turnover = float(decision["turnover"] or 0)
    if action == "COMPRÓ":
        return f"Compró {', '.join(selected) or 'activos'} por ${turnover:.2f}. Exposición objetivo: {target_pct:.0f}%."
    if action == "VENDIÓ":
        sold = sorted({t["symbol"] for t in trades if t["side"] == "SELL"})
        return f"Vendió {', '.join(sold) or 'activos'} por ${turnover:.2f}. Exposición objetivo: {target_pct:.0f}%."
    if action == "REBALANCEÓ":
        return f"Rotó posiciones por ${turnover:.2f}. Exposición objetivo: {target_pct:.0f}%."
    summary = reason_text(str(decision["reason"]))
    if target_pct > 0:
        summary += f" Exposición objetivo: {target_pct:.0f}%."
    else:
        summary += " Sin exposición: 100% en efectivo."
    return summary


def main() -> None:
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        a = c.execute("select * from account where id=1").fetchone()
        snaps = c.execute(
            "select snapshot_time,equity,cash,invested,regime,raw_regime,prices_json "
            "from snapshots where kind='DAY_CLOSE' order by snapshot_time"
        ).fetchall()
        if not snaps:
            snaps = c.execute(
                "select snapshot_time,equity,cash,invested,regime,raw_regime,prices_json "
                "from snapshots order by snapshot_time"
            ).fetchall()

        latest = snaps[-1] if snaps else None
        position_rows = c.execute(
            "select symbol,quantity,avg_cost from positions order by symbol"
        ).fetchall()
        decision_rows = c.execute(
            """
            select decision_time,raw_regime,regime,reason,trigger,selected_json,
                   target_exposure,equity_before,equity_after,turnover,is_catchup
            from decisions order by decision_time desc limit 14
            """
        ).fetchall()
        trade_rows = c.execute(
            """
            select decision_time,side,symbol,market_price,execution_price,quantity,
                   market_notional,fee,slippage,trigger,regime
            from trades order by decision_time desc, sequence desc limit 200
            """
        ).fetchall()
        trade_count = c.execute("select count(*) n from trades").fetchone()["n"]

    trades_by_decision: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for t in trade_rows:
        trades_by_decision[int(t["decision_time"])].append(t)

    initial = float(a["initial_cash"])
    equity = float(latest["equity"]) if latest else float(a["cash"])
    cash = float(a["cash"])
    invested = float(latest["invested"]) if latest else max(0.0, equity - cash)
    regime = a["last_regime"]

    peak = initial
    mdd = 0.0
    for s in snaps:
        e = float(s["equity"])
        peak = max(peak, e)
        mdd = min(mdd, (e / peak - 1) * 100)

    status = parse_status(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    if status.get("equity") is not None:
        equity = float(status["equity"])
    if status.get("cash") is not None:
        cash = float(status["cash"])
    if status.get("invested") is not None:
        invested = float(status["invested"])
    if status.get("max_drawdown_pct") is not None:
        mdd = float(status["max_drawdown_pct"])
    if status.get("regime"):
        regime = str(status["regime"])

    latest_prices = {}
    if latest is not None and latest["prices_json"]:
        try:
            latest_prices = json.loads(latest["prices_json"])
        except (TypeError, ValueError):
            latest_prices = {}

    parsed_positions = status.get("positions") or []
    positions = []
    if parsed_positions:
        db_positions = {
            str(p["symbol"]): (float(p["quantity"]), float(p["avg_cost"]))
            for p in position_rows
        }
        for p in parsed_positions:
            symbol = str(p["symbol"])
            qty, avg = db_positions.get(symbol, (0.0, 0.0))
            value = float(p["value"])
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": qty,
                    "avg_cost": round(avg, 6),
                    "value": round(value, 6),
                    "weight_pct": round(value / equity * 100, 2) if equity else 0,
                }
            )
    else:
        for p in position_rows:
            qty = float(p["quantity"])
            avg = float(p["avg_cost"])
            price = float(latest_prices.get(p["symbol"], avg))
            value = qty * price
            positions.append(
                {
                    "symbol": str(p["symbol"]),
                    "quantity": qty,
                    "avg_cost": round(avg, 6),
                    "value": round(value, 6),
                    "weight_pct": round(value / equity * 100, 2) if equity else 0,
                }
            )

    decisions = []
    for d in decision_rows:
        dtime = int(d["decision_time"])
        trs = trades_by_decision.get(dtime, [])
        action, kind = decision_action(d, trs)
        selected = json.loads(d["selected_json"] or "[]")
        decisions.append(
            {
                "date": date_only(dtime),
                "datetime": iso(dtime),
                "regime": d["regime"],
                "raw_regime": d["raw_regime"],
                "trigger": d["trigger"],
                "trigger_label": TRIGGER_TEXT.get(d["trigger"], d["trigger"]),
                "reason": d["reason"],
                "reason_text": reason_text(str(d["reason"])),
                "action": action,
                "action_kind": kind,
                "selected": selected,
                "target_exposure_pct": round(float(d["target_exposure"] or 0) * 100, 2),
                "equity": round(float(d["equity_after"] or 0), 6),
                "turnover": round(float(d["turnover"] or 0), 6),
                "summary": decision_summary(action, d, trs),
                "trades": [
                    {
                        "side": t["side"],
                        "action": "COMPRÓ" if t["side"] == "BUY" else "VENDIÓ",
                        "symbol": t["symbol"],
                        "quantity": float(t["quantity"]),
                        "notional": round(float(t["market_notional"]), 6),
                        "price": float(t["execution_price"]),
                    }
                    for t in trs
                ],
            }
        )

    recent_trades = [
        {
            "date": date_only(int(t["decision_time"])),
            "datetime": iso(int(t["decision_time"])),
            "side": t["side"],
            "action": "COMPRÓ" if t["side"] == "BUY" else "VENDIÓ",
            "symbol": t["symbol"],
            "quantity": float(t["quantity"]),
            "notional": round(float(t["market_notional"]), 6),
            "price": float(t["execution_price"]),
            "fee": round(float(t["fee"]), 8),
            "trigger": t["trigger"],
            "regime": t["regime"],
        }
        for t in trade_rows
    ]

    regime_meta = REGIME_META.get(str(regime), REGIME_META["NEUTRAL"])
    exposure_pct = invested / equity * 100 if equity else 0.0
    latest_decision = decisions[0] if decisions else None

    symbols_text = ", ".join(p["symbol"] for p in positions) if positions else ""
    if positions:
        current = {
            "kind": "invested",
            "action": "INVERTIDO",
            "headline": f"Tiene compras abiertas en {symbols_text}",
            "explanation": (
                f"El bot está dentro del mercado con {exposure_pct:.1f}% de la cuenta. "
                f"El resto sigue en efectivo. Revisa cada día si mantiene, compra más o vende."
            ),
            "target_exposure_pct": round(exposure_pct, 2),
            "selected": [p["symbol"] for p in positions],
        }
    elif latest_decision and latest_decision["target_exposure_pct"] > 0 and latest_decision["selected"]:
        current = {
            "kind": "waiting",
            "action": "QUIERE COMPRAR",
            "headline": "Hay una señal de compra, pero todavía no hay posición",
            "explanation": (
                "La última señal pedía exposición, pero la orden no se ejecutó. "
                "La próxima revisión volverá a intentarlo."
            ),
            "target_exposure_pct": latest_decision["target_exposure_pct"],
            "selected": latest_decision["selected"],
        }
    else:
        if str(regime) == "NEUTRAL":
            explanation = (
                "El mercado está en zona mixta. La regla permite hasta 40% en el líder, "
                "pero solo compra si algún activo cumple el filtro de tendencia. "
                "Hoy no lo cumple, así que espera en efectivo."
            )
        elif str(regime) == "BEAR":
            explanation = (
                "El mercado está en modo bajista confirmado. La regla de protección "
                "manda el 100% a efectivo para evitar la caída."
            )
        else:
            explanation = (
                f"La señal es {regime_meta['label']}, pero ningún activo cumple el filtro "
                "de entrada. El bot espera una confirmación antes de comprar."
            )
        current = {
            "kind": "cash",
            "action": "EN EFECTIVO",
            "headline": "Ahora mismo no tiene compras abiertas",
            "explanation": explanation,
            "target_exposure_pct": 0.0,
            "selected": [],
        }

    current["regime"] = str(regime)
    current["regime_label"] = regime_meta["label"]
    current["regime_detail"] = regime_meta["detail"]
    current["regime_exposure"] = regime_meta["exposure"]
    current["next_check"] = "Cada día a las 00:17 UTC (19:17 Ecuador). Los lunes además rebalancea."

    benchmark_order = ["BTCUSDT", "BNBUSDT", "ETHUSDT", "LINKUSDT", "EQUAL_WEIGHT"]
    raw_benchmarks = status.get("benchmarks") or {}
    benchmarks = [
        {"name": name, "return_pct": round(float(raw_benchmarks[name]), 2)}
        for name in benchmark_order
        if name in raw_benchmarks
    ]

    payload = {
        "strategy": f"{a['strategy_name']} {a['strategy_version']}",
        "strategy_name": a["strategy_name"],
        "strategy_version": a["strategy_version"],
        "started_at": iso(a["initialized_at"]),
        "updated_at": status.get("updated_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "equity": round(equity, 8),
        "return_pct": round(
            float(status["return_pct"])
            if status.get("return_pct") is not None
            else (equity / initial - 1) * 100,
            4,
        ),
        "cash": round(cash, 8),
        "invested": round(invested, 8),
        "max_drawdown_pct": round(mdd, 4),
        "regime": str(regime) if regime else "NEUTRAL",
        "raw_regime": status.get("raw_regime") or a["last_raw_regime"],
        "regime_meta": regime_meta,
        "trades": int(status.get("trades") if status.get("trades") is not None else trade_count),
        "costs": round(
            float(status["costs"])
            if status.get("costs") is not None
            else float(a["total_fees"]) + float(a["total_slippage"]),
            8,
        ),
        "integrity": True,
        "current": current,
        "positions": positions,
        "benchmarks": benchmarks,
        "equity_curve": [
            {"time": iso(s["snapshot_time"]), "equity": round(float(s["equity"]), 8)}
            for s in snaps
        ]
        + ([{"time": status.get("updated_at"), "equity": round(equity, 8)}] if status else []),
        "decisions": decisions,
        "recent_trades": recent_trades,
    }

    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT}: equity=${equity:.4f}, {len(snaps)} snapshots, {trade_count} trades")


if __name__ == "__main__":
    main()
