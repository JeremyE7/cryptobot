from __future__ import annotations

from .clients import DexScreenerClient, SolanaClient
from .scoring import score_pair
from .storage import connect, save_observation


def _best_pair(pairs: list[dict]) -> dict | None:
    sol = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol:
        return None
    return max(sol, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def scan(db_path: str, limit: int = 30) -> list[dict]:
    dex, sol = DexScreenerClient(), SolanaClient()
    conn = connect(db_path)
    output: list[dict] = []
    seen: set[str] = set()

    for profile in dex.latest_solana_profiles(limit=limit):
        mint = profile.get("tokenAddress")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        try:
            pair = _best_pair(dex.token_pairs(mint))
            if not pair:
                continue
            try:
                concentration = sol.holder_concentration(mint)
            except Exception:
                concentration = {"top1_pct": None, "top5_pct": None, "top20_pct": None}
            result = score_pair(pair, concentration)
            save_observation(conn, pair, concentration, result)
            base = pair.get("baseToken") or {}
            output.append({
                "symbol": base.get("symbol") or "?", "address": mint, "score": result.score,
                "risk": result.risk, "liquidity": float((pair.get("liquidity") or {}).get("usd") or 0),
                "m5": float((pair.get("priceChange") or {}).get("m5") or 0), "vetoes": result.vetoes,
            })
        except Exception as exc:
            output.append({"symbol": "?", "address": mint, "score": 0, "risk": "ERROR", "liquidity": 0, "m5": 0, "vetoes": (str(exc),)})

    return sorted(output, key=lambda x: x["score"], reverse=True)
