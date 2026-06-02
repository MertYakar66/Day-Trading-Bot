# Day-Trading-Bot — Intraday Engine

A standalone intraday ("day-trading") decision engine. It is a **separate
product** from the Smart Wheel Engine (SWE), but reuses SWE's quant math as a
*dependency* (see [Dependencies](#dependencies)). It shares none of SWE's
decision path: SWE decides 30–45 DTE option wheels end-of-day; this engine makes
minutes-to-hours intraday decisions.

- **Spec:** [`DESIGN.md`](DESIGN.md) — read this first.
- **Build backlog:** [`TASKS.md`](TASKS.md) — ordered Phase 0 tasks with
  done-criteria. **Start here if you are an automated agent.**
- **Code map:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the `intraday/`
  package layout and where each invariant lives.
- **Build log:** [`PROGRESS.md`](PROGRESS.md) — decisions, assumptions, data
  source, results, open items.
- **Final report:** [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) — results net of
  costs, proven vs assumed, and what a human must decide next.
- **Real-data path:** [`docs/REAL_DATA.md`](docs/REAL_DATA.md) +
  [`docs/OPERATOR_RUNBOOK.md`](docs/OPERATOR_RUNBOOK.md) — the corrected data-tier
  wiring (IBKR/parity underlying, Theta OPTIONS) and the scoped operator Theta pull.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/activate          # Windows; use bin/activate on POSIX
pip install -r requirements.txt
pytest                                                   # full suite (network-free)
python -m intraday backtest --start 2026-05-01 --end 2026-05-29              # S3 control, SYNTHETIC
python -m intraday backtest --symbols SPY --strategy s1 --start 2026-05-04 --end 2026-05-08  # S1 (synthetic)
python -m intraday backtest --symbols SPY --strategy s2 --start 2026-05-04 --end 2026-05-08  # S2 (synthetic)

# Real data (after ingesting IBKR underlying — see docs/REAL_DATA.md):
python -m scripts.ingest_ibkr_underlying --raw-dir data_raw/ibkr --store-root data_store
python -m intraday backtest --provider ibkr-store --symbols SPY QQQ --interval 5m \
    --strategy s3 --start <first-session> --end <last-session>               # S3 on REAL IBKR data
```

> **Status:** Phase 0 + Phase 1 complete and green (**312 tests**), plus a wired
> **real-data path**. The gamma spine — S1 (gamma-regime), S2 (0DTE VRP,
> **defined-risk only**), S3 (VWAP control) — runs behind the one net-of-cost
> expectancy gate, plus a paper ledger. **Corrected data tiers:** options come from
> **Theta STANDARD**; the **underlying** comes from **IBKR** (intraday, reads only)
> or **put-call parity** (deep history) — never Theta. A first REAL-data S3 run on
> ~13 IBKR sessions was net-positive after costs but **statistically insignificant
> (not an edge)**. Theta is not touched in build sessions. See
> [`docs/REAL_DATA.md`](docs/REAL_DATA.md), [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) §0.

## Dependencies

The reused quant modules — `engine/*` (e.g. `theta_connector`, `option_pricer`,
`dealer_positioning`, `volatility_surface`, `realized_vol`, `transaction_costs`,
`risk_manager`, `event_gate`, `performance_metrics`), `backtests/simulator.py`,
and `scripts/pull_theta_option_tape.py` — live in the **smart-wheel-engine**
repo, not here. Make them importable before any reuse task (`TASKS.md` T0.0).
Recommended options, easiest first:

1. **Git submodule** — `git submodule add <swe-repo-url> vendor/swe`, then add
   `vendor/swe` to `PYTHONPATH`. Keeps SWE read-only and pinned to a commit.
2. **Editable install** — `pip install -e <path-or-git+url-to-swe>` if SWE
   exposes a package.
3. **Vendor** — copy the specific modules in (last resort; hardest to keep in
   sync).

> `CLAUDE.md §2` references in the docs are conceptual — they point to SWE's
> "one authority + downgrade-only reviewers" discipline, which we re-implement
> here (`DESIGN.md` §6). You do not need SWE's `CLAUDE.md` to build this.

## Environment

Real data (corrected tiers — see [`docs/REAL_DATA.md`](docs/REAL_DATA.md)):

- **Underlying** (SPY/QQQ stock; SPX/VIX index) → **IBKR** intraday bars/snapshots,
  **reads only** (operator: `ib_insync`/IB Gateway; dev: the IBKR MCP). For deep
  history, **put-call parity** from ATM option quotes. Never any order/account call.
- **Options** (tape/IV/greeks) → **Theta STANDARD**, via the scoped operator pull
  ([`docs/OPERATOR_RUNBOOK.md`](docs/OPERATOR_RUNBOOK.md)); the Terminal runs
  laptop-side at `127.0.0.1:25503`. Theta STOCK is FREE/EOD-only and Theta INDEX is
  unavailable, so **Theta never serves the underlying**.

Backtests replay captured data from the parquet store (`--provider ibkr-store` /
`theta-store`) — network-free. Theta is **not touched** during build sessions (the
operator uses the subscription concurrently).

## Operating rules (hard guardrails)

Non-negotiable. Full detail in [`TASKS.md`](TASKS.md).

1. **PAPER ONLY.** No broker / live-order code. `execution/` is a paper ledger
   until an edge is proven and live trading is a separate, explicit decision.
2. **Never modify the SWE dependency.** Consume `engine/*` etc. read-only; if a
   change is needed upstream, propose it in the smart-wheel-engine repo, not by
   editing the vendored copy.
3. **No look-ahead.** Every feature/backtest row is stamped with the timestamp
   at which it was computable from the live feed. Point-in-time or it's a lie.
4. **Net-of-cost expectancy gate is mandatory.** No signal becomes a paper trade
   without passing it (one authority + downgrade-only reviewers — `DESIGN.md`
   §6). Costs come from SWE's `engine/transaction_costs.py`.
5. **Tests for every module**, and keep `main` releasable — work on a branch and
   merge via PR; don't commit half-built code straight to `main`.
6. **If blocked on an open question** (tier, paper NAV, acceptance thresholds —
   `DESIGN.md` §11): stub it, add a `TODO`, document the assumption. **Do not
   guess live-money parameters.**
