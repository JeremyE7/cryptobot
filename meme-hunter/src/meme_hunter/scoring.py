from __future__ import annotations

from dataclasses import dataclass
from time import time


@dataclass(frozen=True)
class ScoreResult:
    score: float
    risk: str
    vetoes: tuple[str, ...]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_pair(pair: dict, concentration: dict | None = None, now_ms: int | None = None) -> ScoreResult:
    concentration = concentration or {}
    now_ms = now_ms or int(time() * 1000)

    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx = pair.get("txns") or {}
    m5_tx = tx.get("m5") or {}
    buys, sells = int(m5_tx.get("buys") or 0), int(m5_tx.get("sells") or 0)
    created = int(pair.get("pairCreatedAt") or now_ms)
    age_min = max(0.0, (now_ms - created) / 60000.0)

    m5 = float(chg.get("m5") or 0)
    h1 = float(chg.get("h1") or 0)
    v5 = float(vol.get("m5") or 0)
    v1h = float(vol.get("h1") or 0)

    momentum = _clamp(max(m5, 0) / 25 * 18 + max(h1, 0) / 80 * 12, 0, 30)
    volume_accel = (v5 * 12 / v1h) if v1h > 0 else (2.0 if v5 > 0 else 0.0)
    volume_score = _clamp(volume_accel / 2.0 * 15, 0, 15) + _clamp(v5 / max(liq, 1) * 10, 0, 10)
    flow_ratio = buys / max(sells, 1)
    flow = _clamp((flow_ratio - 1) / 2 * 20, 0, 20)
    liquidity_score = _clamp((liq - 20_000) / 180_000 * 15, 0, 15)
    age_score = 0 if age_min < 5 else _clamp(age_min / 60 * 10, 0, 10)

    top1 = concentration.get("top1_pct")
    top5 = concentration.get("top5_pct")
    penalty = 0.0
    vetoes: list[str] = []

    if liq < 25_000:
        vetoes.append("liquidity<25k")
    if age_min < 5:
        vetoes.append("age<5m")
    if buys + sells < 10:
        vetoes.append("thin-orderflow")
    if top1 is not None and top1 > 25:
        vetoes.append("top1>25%")
        penalty += 20
    if top5 is not None and top5 > 50:
        vetoes.append("top5>50%")
        penalty += 15

    raw = momentum + volume_score + flow + liquidity_score + age_score - penalty
    score = round(_clamp(raw, 0, 100), 2)
    risk = "REJECT" if vetoes else ("LOW" if score >= 80 else "MED" if score >= 60 else "HIGH")
    return ScoreResult(score=score, risk=risk, vetoes=tuple(vetoes))
