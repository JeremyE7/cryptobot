# Crypto Bot — Persistent Forward Paper Trader 7.0

Version 7.0 keeps the **6.0 trading strategy frozen** and adds the missing operational layer needed for a real forward paper test. It still uses only Binance public Spot market data and **never sends real orders**.

> **Status:** ready for forward paper observation, not real-money automation.

## What changed from 6.0

The historical strategy itself did **not** change. Version 7.0 adds:

- `data/paper.db`: persistent fictitious account, separate from market data.
- `crypto-bot paper init`: starts a new forward-only account from cash.
- `crypto-bot paper tick`: synchronizes Binance and processes every new UTC day exactly once.
- `crypto-bot paper status`: equity, positions, costs, benchmarks, drawdown and recent trades.
- `crypto-bot paper doctor`: integrity/frozen-strategy checks.
- Persistent positions and cash across restarts.
- Catch-up after missed/offline days.
- SQLite transaction locking and idempotency against duplicate scheduled runs.
- Immutable `decisions` and `trades` ledgers enforced by SQLite triggers.
- Frozen strategy fingerprint: changing the 6.0 rules mid-forward causes a hard failure instead of silently contaminating the experiment.
- Daily equity snapshots and forward BTC/BNB/ETH/LINK/equal-weight benchmarks from the exact paper start.
- Automatic Linux `systemd` and Windows Task Scheduler installers.

## Frozen strategy

Same validated 6.0 defaults:

- BTC 1D structural regime + BTC 4H fast regime.
- Deadband `+0.10% / -0.10%` around EMA20/EMA50.
- `STRONG_BULL`: 100% in top 2.
- `RECOVERY`: 50% in top 1, 50% cash.
- `NEUTRAL` / `BEAR`: cash.
- Risk-on confirmation: 1 day.
- NEUTRAL risk-off confirmation: 2 days.
- BEAR exit: immediate.
- Weekly leader rebalance Monday UTC plus regime-transition rebalances.
- Fee simulation: 0.10%/side.
- Slippage simulation: 0.05%/side.
- Minimum simulated order: $4.90.

The included historical allocator remains unchanged at **$10 → $12.7519 (+27.52%)**, max drawdown **-11.38%**, with **4/6** profitable 90-day folds. This is historical evidence only.

## Why `paper.db` is separate

`data/market.db` is replaceable market history. `data/paper.db` is the forward experiment and should be treated as valuable state:

```text
market.db                 paper.db
─────────                 ────────
Binance candles           cash
replaceable               positions
can be rebuilt            decisions
                          immutable trades
                          equity snapshots
                          forward start prices
```

Do not delete `paper.db` during the forward test. Back it up periodically.

## First setup

The ZIP already includes the 2-year `data/market.db`.

### Git Bash / Windows

```bash
python -m venv .venv && source .venv/Scripts/activate && python -m pip install -e ".[dev]" && pytest
```

### Linux server

```bash
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e ".[dev]" && pytest
```

Expected test result for this package: **51 passed**.

## Start the forward experiment — once

Run this only once on the machine that will own the forward account:

```bash
crypto-bot paper init --cash 10
```

Important: initialization **does not import any historical position**. It starts at `$10 cash`, and the first eligible decision is the next UTC day. That keeps the forward experiment genuinely out-of-sample.

Then inspect it:

```bash
crypto-bot paper status
```

## Daily operation

Normally you do not need to run this manually after automation is installed:

```bash
crypto-bot paper tick
```

A tick:

1. refreshes public Binance 15m data;
2. finalizes previously partial stored candles by re-fetching the latest kline;
3. finds each unprocessed UTC day;
4. rebuilds the frozen regime from only information available before that day open;
5. simulates the required buy/sell at that day's first 15m open;
6. writes the decision/trades/account atomically to `paper.db`;
7. refuses to process the same day twice.

If the machine was off for several days, the next tick catches those paper days up sequentially and labels them `CATCHUP`.

For offline testing only:

```bash
crypto-bot paper tick --no-sync
```

## Status and health

```bash
crypto-bot paper status
crypto-bot paper doctor
```

`status` includes current fictitious equity, cash, positions, exposure, costs, max drawdown, frozen regime, trade ledger and forward benchmarks.

`doctor` verifies sufficient market history, strategy fingerprint, SQLite integrity, last processed day and ledger/account fee/slippage/turnover totals.

## Automate it on the $10 laptop

### Linux / systemd — recommended

After installing the venv/project:

```bash
./scripts/install-paper-systemd.sh
```

This installs a persistent timer for **00:10 UTC every day** (19:10 in Ecuador). `Persistent=true` means systemd runs a missed timer when the laptop comes back online; the paper engine also catches up any missing days itself.

Useful commands:

```bash
systemctl status crypto-bot-paper.timer
journalctl -u crypto-bot-paper.service
```

### Windows

From PowerShell:

```powershell
.\scripts\install-paper-windows.ps1
```

It schedules the same daily tick at 19:10 local time with `StartWhenAvailable`.

## Validation performed before packaging

- **51/51 tests passing.**
- Historical 6.0 allocator result unchanged: **+27.52%, DD -11.38%, Calmar 1.75, 4/6 profitable folds**.
- Persistent engine replayed real `market.db` from Aug 2 through Sep 6, 2026.
- Persistent paper day-close equity: `$11.259903012929811`.
- Frozen backtest equity for the exact same interval: `$11.259903012929811`.
- Difference: **exactly 0.0** at stored precision.
- Trade-leg count also matched exactly.
- See `data/paper-replay-validation.txt`.

## Forward-test rule

Do **not** tune the strategy while this `paper.db` is running. Software bugs may be fixed; strategy parameters should remain frozen. Observe for at least ~90 days and preferably until enough actual forward regime transitions/trades have accumulated to make the result informative.

No API key, withdrawal permission, or real trading permission is used anywhere in 7.0.

---

## GitHub Cloud Paper Forward (8.0 infrastructure)

For the zero-server deployment, see [`CLOUD_SETUP.md`](CLOUD_SETUP.md). The cloud package does **not** change the frozen 6.0 allocator; it automates the persistent paper-forward engine from 7.0 using GitHub Actions. No Binance API key, Turso account, or paid VM is required.
