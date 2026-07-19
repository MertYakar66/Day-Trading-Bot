# Daily Prospective Routine (no Theta)

A once-per-trading-day operator loop that builds the project's most honest
dataset: a **prospective, point-in-time paper record**. Unlike any backtest,
this record cannot contain look-ahead by construction — each day's bars are
fetched after the close and each day's gated decisions are stamped and persisted
before tomorrow exists. It uses only free read-only sources (Yahoo); it never
touches Theta or any broker.

Why it matters: the powered evaluation (`scripts/eval_real_universe.py`) found
NO demonstrated edge for the underlying-only strategies. The prospective record
is the clean out-of-sample stream that either keeps confirming that verdict or
surfaces genuine divergence between paper decisions and backtest replays
(DESIGN §8 record parity).

## When to run

Once per trading day, **after ~16:15 ET** (the session must be complete — the
ingest guard auto-skips sessions below 80% grid coverage, so a too-early run
admits nothing rather than something partial; early-close half-days are skipped
by design).

## The four commands (PowerShell, from the repo root)

```powershell
$D = Get-Date -Format yyyy-MM-dd

# 1. FETCH (network: Yahoo only) — dated raw dir = immutable daily snapshot
.venv\Scripts\python.exe -m scripts.fetch_yahoo_universe --out-dir data_raw/yahoo_daily/$D

# 2. INGEST (offline, idempotent; provenance sidecars; partial sessions auto-skipped)
.venv\Scripts\python.exe -m scripts.ingest_yahoo_universe --raw-dir data_raw/yahoo_daily/$D --store-root data_raw/store_yahoo

# 3. PAPER POLL (read-only decisions at the latest stored session) + persisted record
.venv\Scripts\python.exe -m scripts.live_paper_poll --provider yahoo-store --store-root data_raw/store_yahoo `
    --symbols SPY QQQ --persist-root data_store/paper_poll

# 4. DURABLE LEDGER — single-day backtest over today's session (signals + paper fills)
.venv\Scripts\python.exe -m intraday backtest --provider yahoo-store --store-root data_raw/store_yahoo `
    --symbols SPY QQQ --interval 5m --strategy s3 s4 s5 --start $D --end $D --store
```

Notes:

- Step 1 fetches the full 24-symbol default universe with a 60-trading-day
  window; the overlap with prior days is harmless (ingest overwrites each
  per-day partition deterministically) and repairs any gap from a missed day —
  but only within the 60-day lookback, so a missed *month* is unrecoverable.
  That is the point: run it daily.
- Step 3's `--persist-root` writes the gated decisions in the
  **backtest-identical signal schema** to
  `data_store/paper_poll/signals/date=<session>/signals.parquet`. Re-running
  the same session overwrites that day's poll record — keep this root separate
  from the backtest `--store` root (`data_store/`).
- Step 4 must stay single-day (`--start $D --end $D`): the CLI persists all
  signals under the `--start` date partition.

## What accumulates where

| Path | Contents |
| --- | --- |
| `data_raw/yahoo_daily/<DATE>/` | immutable raw fetch snapshots + manifest (PIT provenance) |
| `data_raw/store_yahoo/bars/...` | the growing 5m bar store (provenance sidecars, 80% coverage guard) |
| `data_store/paper_poll/signals/date=<D>/` | the day's gated poll decisions (backtest schema) |
| `data_store/signals/`, `data_store/paper_ledger/` | the day's backtest-replay signals + paper fills |

After ~2 weeks this yields ~10 sessions of prospective record; the powered eval
can then be rerun over the extended store
(`python -m scripts.eval_real_universe --end <latest>`), with the new sessions
acting as true out-of-sample data.

## Guardrails (unchanged, non-negotiable)

- Reads only — no orders, no broker calls anywhere in this path. PAPER ONLY.
- Never touches Theta (`127.0.0.1:25503`) — Yahoo is the only network call.
- Provenance is stamped on every partition; reads refuse missing sidecars.
