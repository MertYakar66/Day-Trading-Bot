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

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/activate          # Windows; use bin/activate on POSIX
pip install -r requirements.txt
pytest                                                   # full suite (network-free, synthetic)
python -m intraday backtest --start 2026-05-01 --end 2026-05-29   # Phase-0 NET-of-costs report
```

> **Status:** Phase 0 complete and green on `build/phase-0` (224 tests). **All
> data is SYNTHETIC** (deterministic) — the Theta tier is FREE (no real intraday
> data) and Theta is not touched during build sessions. See
> [`docs/THETA_TIER_PROBE.md`](docs/THETA_TIER_PROBE.md). Synthetic results are a
> harness validation, **not an edge**.

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

Live data requires **Theta Terminal running on the laptop** (`127.0.0.1:25503`).
A cloud sandbox has no Terminal and no data-science deps, so all real data
pulls, streaming, and backtests run laptop-side. Before any data work, confirm
the subscription tier by running `scripts/probe_theta_capabilities.py` **in the
smart-wheel-engine checkout** (that script ships with SWE).

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
