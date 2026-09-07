from __future__ import annotations

import httpx

DEX_BASE = "https://api.dexscreener.com"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


class DexScreenerClient:
    def __init__(self, timeout: float = 15.0):
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "meme-hunter/0.1"})

    def latest_solana_profiles(self, limit: int = 30) -> list[dict]:
        r = self.client.get(f"{DEX_BASE}/token-profiles/latest/v1")
        r.raise_for_status()
        rows = r.json() or []
        return [x for x in rows if x.get("chainId") == "solana"][:limit]

    def token_pairs(self, token_address: str) -> list[dict]:
        r = self.client.get(f"{DEX_BASE}/token-pairs/v1/solana/{token_address}")
        r.raise_for_status()
        data = r.json() or []
        return data if isinstance(data, list) else data.get("pairs", []) or []


class SolanaClient:
    def __init__(self, endpoint: str = SOLANA_RPC, timeout: float = 15.0):
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "meme-hunter/0.1"})

    def _rpc(self, method: str, params: list) -> dict:
        r = self.client.post(self.endpoint, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"Solana RPC {method}: {payload['error']}")
        return payload["result"]

    def holder_concentration(self, mint: str) -> dict:
        supply_result = self._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        largest_result = self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        supply = float(supply_result["value"]["uiAmountString"])
        balances = [float(x["uiAmountString"]) for x in largest_result["value"]]
        if supply <= 0:
            return {"top1_pct": None, "top5_pct": None, "top20_pct": None}
        pct = lambda xs: 100.0 * sum(xs) / supply
        return {
            "top1_pct": pct(balances[:1]),
            "top5_pct": pct(balances[:5]),
            "top20_pct": pct(balances[:20]),
        }
