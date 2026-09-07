#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f data/market.db ]]; then
  echo "Restoring data/market.db from data/market-seed.db.gz"
  gzip -dc data/market-seed.db.gz > data/market.db
fi

python -m pip install -e ".[dev]"
pytest -q
crypto-bot paper doctor 2>/dev/null || true
printf '\nCloud package check complete.\n'
