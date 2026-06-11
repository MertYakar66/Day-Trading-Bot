# Operator Runbook — Scoped Theta Options Pull (live, billed, gated)

This is the **operator-only** procedure to pull real option data from Theta
(STANDARD). It opens sockets to the Theta Terminal and consumes the paid
subscription, so it is **never run by an agent or in CI**. The intraday engine
only ever READS the resulting parquet.

> Cost reality: Options-STANDARD is ~$80/mo, recurring (not owned). The pull MUST
> fit one billing month at the tier's concurrency. Cancel after if desired.

The pull/ingest/replay path is **bot-side and fully wired** (the older SWE
puller cannot pull historical dates and writes inside read-only `vendor/`):

| step | tool | network |
| --- | --- | --- |
| capture | `scripts/pull_theta_tape_scoped.py` (gated) | Theta Terminal only |
| ingest  | `scripts/ingest_theta_options.py` | none |
| replay  | `python -m intraday backtest --provider fused-store` | none |

## 0. Preconditions

- Theta **Terminal running** locally and reachable at `127.0.0.1:25503`.
- Subscription = **Options-STANDARD** (intraday option tape + quotes + daily OI).
- You are **not** mid-pull elsewhere — coordinate concurrency; do not collide with
  your own interactive use.

## 1. Review the plan (no socket opened — the default is a dry run)

```bash
python -m scripts.pull_theta_tape_scoped --symbols SPY QQQ \
    --start 2022-01-03 --end 2026-06-01 --strikes-each-side 10
```

Without the double gate this prints the work-list and never constructs an HTTP
client. **Scope (do not widen — it blows the month):**

- symbols: **SPY, QQQ** (the GEX spine; SPX weeklies live under root SPXW and
  prior SPXW captures carried junk OI — pull SPX only as context, never for GEX)
- strikes: **ATM ± 10** (server-side `strike_range`) · quote cadence: **1m**
- window: **2022 → today** (the 0DTE era; expirations resolve **per session** —
  0DTE when listed, else nearest on-or-after)
- per session: tick trades **with prevailing NBBO** (`trade_quote`), 1m quote
  bars, and the daily open-interest file

## 2. Probe ONE session first (mandatory)

The `trade_quote` response shape and the strike unit are unverified on this
account tier. Probe exactly one recent session and read its manifest before any
backfill:

```bash
set THETA_OPERATOR_CONFIRM=1
python -m scripts.pull_theta_tape_scoped --probe-day 2026-06-08 --symbols SPY QQQ \
    --i-understand-this-pulls-live-theta
```

Then inspect `data_raw/theta/ticker=SPY/date=2026-06-08/_manifest.json`:

- `endpoints.trades` should be `/v3/option/history/trade_quote`; if it fell back
  to `/trade`, every `side_inferred` is `"mid"` and options order-flow is dead —
  investigate before pulling a month of tape.
- `strike_divisor` records the unit auto-detection (1 = dollars, 1000 = millis).
- `rows` + file sizes calibrate the real cost; planner estimates can be 10–100×
  low. Budget the backfill from the probe, not the estimate.

## 3. Run the backfill (gated, resumable, loud failures)

```bash
python -m scripts.pull_theta_tape_scoped --symbols SPY QQQ \
    --start 2022-01-03 --end 2026-06-01 --strikes-each-side 10 \
    --workers 2 --resume --i-understand-this-pulls-live-theta
```

Failures are per-partition and recorded in `_runs.json` + the exit code; re-run
with `--resume` to retry only what failed (a failed partition is never marked
complete). Output: `data_raw/theta/ticker=<SYM>/date=<D>/{trades,quotes,oi}.parquet`
+ `_manifest.json`. (Prices are per-share option premium — ×100 for notional.
`side_inferred`: "buy" = customer buy-initiated = dealer sells.)

## 4. Ingest into the engine store (network-free, idempotent)

```bash
python -m scripts.ingest_theta_options --raw-root data_raw/theta \
    --store-root data_store --symbols SPY QQQ --parity-bars
```

This writes, per session: the tape (`DataSource.THETA`, PIT-stamped), the raw
quotes (`option_quotes` slot), **synthesized chain snapshots** at 5m cadence
(`DataSource.THETA_DERIVED`: per-contract quote mids, locally BS-inverted IV,
put-call-parity spot, PRIOR-session OI — see the module docstring for the
binding PIT rules), and optionally parity underlying bars (never clobbering
IBKR partitions). A session whose chain has no same-day-expiry rows is refused
(the engine's option features key on `expiry == session day`).

## 5. Backtest on real data (paper only, net of costs, OOS)

```bash
# Bars (IBKR -> parity -> yahoo fallback) + Theta options from ONE store:
python -m intraday backtest --provider fused-store --store-root data_store \
    --symbols SPY QQQ --interval 5m --strategy s1 s3 --start <first> --end <last>
```

(`--provider theta-store` cannot serve this store — the underlying bars are
IBKR/PARITY by policy, and the single-source provider rightly refuses mixed
provenance. `fused-store` is the replay mode for capture stores.)

Report results **net of costs**, out-of-sample, with an honest per-strategy
verdict. No parameter is fit on the evaluation window. **S2 stays disabled until
its VRP definition is reworked** (the measured +VRP was ~98% clock artifact).
**Paper only** — no broker orders, ever, until a separate, explicit go-live
decision.

## Guardrails (non-negotiable)

- PAPER ONLY. No broker orders. IBKR/Theta are **data reads only**.
- Never modify `vendor/swe`; the puller writes only under `data_raw/theta/`.
- Respect Theta concurrency (client caps at 4); probe first; backfill incremental
  with `--resume`.
- Label real vs synthetic; never fabricate edge or data. Synthesized chains are
  `THETA_DERIVED`, not `THETA` — the IV is our inversion, not Theta's.
