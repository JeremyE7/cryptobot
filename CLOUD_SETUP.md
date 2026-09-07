# Cloud paper-forward deployment (GitHub Actions)

This package keeps the **6.0 trading strategy frozen** and moves only the 7.0 paper-forward infrastructure to GitHub-hosted automation.

## Architecture

- GitHub Actions wakes once per day at **19:17 America/Guayaquil**.
- Binance public market data uses `https://data-api.binance.vision`; no Binance account or API key is used.
- `data/paper.db` is the canonical persistent fictitious account and immutable trade/decision ledger.
- `data/paper.db` and `data/cloud-status.txt` are committed back to the **private** repository after a state change.
- Mutable `data/market.db` is kept in GitHub Actions cache and is not committed daily.
- `data/market-seed.db.gz` is an immutable recovery snapshot. If the cache disappears, the job restores the snapshot and catches up from Binance.
- The workflow is idempotent and serialized with a concurrency group.

## One-time setup

1. Create an empty **private** GitHub repository.
2. Push this folder to its default branch (`main` recommended).
3. In GitHub open **Actions → Crypto Paper Forward → Run workflow** once.
4. The first run creates `$10` fictitious cash only. It does not import a historical position. The following eligible UTC day can generate the first forward decision.

No repository secrets are required. No Turso account is required. No Binance API key is required.

## What is persisted

`data/paper.db` is deliberately tracked. Do not delete or rewrite it after the forward experiment starts.

`data/cloud-status.txt` is a convenience snapshot. Git history is also an audit trail of each paper tick.

`data/market.db` is disposable market cache. Losing it does **not** lose the paper account.

## Manual recovery

If Actions cache is lost, the workflow automatically expands `data/market-seed.db.gz`, synchronizes all missing Binance candles, and performs catch-up days exactly once.

## Viewing the bot

Open the latest `Crypto Paper Forward` run, or open `data/cloud-status.txt` in the repository.

Do not tune strategy parameters during the forward test. Code/infrastructure bug fixes should be documented separately from strategy changes.
