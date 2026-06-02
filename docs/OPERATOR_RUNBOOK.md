# Operator Runbook — Scoped Theta Options Pull (live, billed, gated)

This is the **operator-only** procedure to pull real option data from Theta
(STANDARD). It opens sockets to the Theta Terminal and consumes the paid
subscription, so it is **never run by an agent or in CI**. The intraday engine
only ever READS the resulting parquet.

> Cost reality: Options-STANDARD is ~$80/mo, recurring (not owned). The pull MUST
> fit one billing month at the tier's concurrency. Cancel after if desired.

## 0. Preconditions

- Theta **Terminal running** locally and reachable at `127.0.0.1:25503`.
- Subscription = **Options-STANDARD** (intraday option tape + IV + greeks).
- You are **not** mid-pull elsewhere — coordinate concurrency; do not collide with
  your own interactive use.
- `vendor/swe` present (the puller is `vendor/swe/scripts/pull_theta_option_tape.py`).

## 1. Review the scoped plan (no socket opened)

```bash
python -m scripts.pull_theta_options_scoped \
    --symbols SPX SPY QQQ --start 2022-01-03 --end 2026-06-01 \
    --strikes-each-side 10 --max-concurrency 4
```

This prints the work-list (symbols × sessions), an order-of-magnitude
disk/time estimate, and the recommended SWE puller invocations. **Scope (do not
widen — it blows the month):**

- symbols: **SPX, SPY, QQQ**  · strikes: **ATM ± 10**  · expiries: **0DTE + near-dated**
- window: **2022 → today** (the 0DTE era; do NOT pull pre-2022 or full-chain wings)
- concurrency: **≤ tier limit** (start at 4; back off if throttled)
- plus a small, fast pull of broad EOD option greeks/IV for context (optional)

## 2. Confirm the SWE CLI flags (one-time)

The planner's commands use **placeholder** flag names (`# TODO(operator)`). Open
`vendor/swe/scripts/pull_theta_option_tape.py` and confirm the real `argparse`
flags (symbol/date/strike-window/expiry selection). The puller writes:

```
data_processed/theta/option_tape/ticker=<SYM>/date=<YYYY-MM-DD>/
    trades.parquet   # ts, expiration, strike, right, price, size, exchange,
                     # condition, nbbo_bid, nbbo_ask, side_inferred
    quotes.parquet   # ts, expiration, strike, right, bid, ask, bid_size, ask_size, mid
```

(Prices are per-share option premium — apply ×100 for notional. `side_inferred`:
"buy" = customer buy-initiated = dealer sells.)

## 3. Run the pull (gated, day-by-day, low concurrency)

Validate on a **few recent days first**, measure size/time, then backfill. Respect
Theta concurrency — if the throughput chokes your interactive pulls, lower it.

```bash
# Per (symbol, day), at ≤4 concurrency. Example shape (confirm flags in step 2):
python vendor/swe/scripts/pull_theta_option_tape.py --symbol SPY --date 2026-05-29 ...
```

The scoped planner refuses to execute even with `THETA_OPERATOR_CONFIRM=1` and
`--i-understand-this-pulls-live-theta` — by design it will not fabricate the SWE
flags or open a socket. You run the verified invocations yourself.

## 4. Ingest into the engine store (network-free)

Once `trades.parquet`/`quotes.parquet` exist, load them into the engine's
`ticker=/date=` store as `DataSource.THETA` (option tape/chain), then reconstruct
deep-history **underlying** via put-call parity
(`intraday.data.parity.ParityUnderlyingProvider`). See
[`REAL_DATA.md`](REAL_DATA.md) §4.

## 5. Backtest on real data (paper only, net of costs, OOS)

```bash
# Options replay (S1/S2) + parity/IBKR underlying, all behind the one gate:
python -m intraday backtest --provider theta-store --symbols SPX SPY QQQ \
    --interval 5m --strategy s1 s2 s3 --start <first> --end <last>
```

Report results **net of costs**, out-of-sample, with an honest per-strategy
verdict. No parameter is fit on the evaluation window. **Paper only** — no broker
orders, ever, until a separate, explicit go-live decision.

## Guardrails (non-negotiable)

- PAPER ONLY. No broker orders. IBKR/Theta are **data reads only**.
- Never modify `vendor/swe`.
- Respect Theta concurrency; small validation pull first; full backfill incremental.
- Label real vs synthetic; never fabricate edge or data.
