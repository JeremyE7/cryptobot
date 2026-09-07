# Meme Hunter 0.1

Research-only Solana memecoin radar. It does **not** place orders and does not require a wallet or API key.

## What it does
- Discovers recent Solana token profiles from DEX Screener.
- Pulls pair-level price, volume, liquidity and buy/sell transaction data.
- Uses Solana RPC to estimate concentration among the 20 largest token accounts.
- Computes a transparent 0-100 radar score plus explicit veto reasons.
- Stores every observation in SQLite so we can build our own forward dataset, including failed tokens.

## Install
```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
python -m pip install -e ".[dev]"
```

## Run one scan
```bash
meme-hunter scan --db data/meme-radar.db --limit 30
```

## Watch continuously
```bash
meme-hunter watch --db data/meme-radar.db --limit 30 --interval 30
```

## Inspect the hottest observations
```bash
meme-hunter top --db data/meme-radar.db --limit 20
```

## Important
The thresholds in 0.1 are research defaults, not a trading strategy. We will record forward data first and only later test whether score bands predict future returns after realistic slippage and latency.
