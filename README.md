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
- **Contributing & changes:** [`CONTRIBUTING.md`](CONTRIBUTING.md) (guardrails + dev
  setup), [`CHANGELOG.md`](CHANGELOG.md).

### Reading guide

Pick a path by what you want:

- **Just the results?** → [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) §0 (headline +
  powered evaluation), or open the pre-rendered [`docs/sample_dashboard.html`](docs/sample_dashboard.html).
- **Building or reviewing code?** → [`DESIGN.md`](DESIGN.md) (spec) →
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (layout) → [`PROGRESS.md`](PROGRESS.md) (decisions).
- **Running data pulls (operator)?** → [`docs/OPERATOR_RUNBOOK.md`](docs/OPERATOR_RUNBOOK.md).
- **Automated agent?** → [`TASKS.md`](TASKS.md) (ordered tasks with done-criteria).
- **First time on this machine?** → `python -m intraday doctor` (checks Python, the
  read-only SWE dependency, and available data; never touches the network).

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/activate          # Windows; use bin/activate on POSIX
pip install -r requirements.txt
pytest                                                   # full suite (network-free)
python -m intraday backtest --start 2026-05-01 --end 2026-05-29              # S3 control, SYNTHETIC
python -m intraday backtest --symbols SPY --strategy s1 --start 2026-05-04 --end 2026-05-08  # S1 (synthetic)
python -m intraday backtest --symbols SPY --strategy s2 --start 2026-05-04 --end 2026-05-08  # S2 (synthetic)

# Real data — free Yahoo intraday universe (no Theta; see docs/REAL_DATA.md):
python -m scripts.fetch_yahoo_universe                                        # 24 symbols, 60d/5m → data_raw/yahoo
python -m scripts.ingest_yahoo_universe --store-root data_raw/store_yahoo     # → parquet (DataSource.YAHOO)
python -m intraday backtest --provider yahoo-store --store-root data_raw/store_yahoo \
    --symbols SPY QQQ --interval 5m --strategy s3 s4 s5 --start 2026-03-09 --end 2026-06-01

# Powered, multiple-testing-honest evaluation across the universe:
python -m scripts.eval_real_universe --store-root data_raw/store_yahoo        # clustered-t, bootstrap CI, deflated Sharpe

# Self-contained HTML reports (offline; charts, honesty scorecard, cost waterfall,
# blotter) — open the file in any browser, no server, no deps:
python -m intraday report --start 2026-05-01 --end 2026-05-29 --out dashboard.html --open
python -m intraday report --provider yahoo-store --store-root data_raw/store_yahoo \
    --symbols SPY QQQ --interval 5m --strategy s3 s4 s5 --start 2026-03-09 --end 2026-06-01 \
    --universe-json data_raw/real_eval_results.json --out dashboard.html   # embeds the cross-section
python -m intraday report --start 2026-05-01 --end 2026-05-29 --out d.html --emit-json d.json  # + machine-readable summary

# Compare strategies side-by-side (overlaid equity curves + ranked metrics table):
python -m intraday compare --strategy s3 s4 s5 --start 2026-05-01 --end 2026-05-29 --out compare.html

# Build a static index linking every report in a directory:
python -m intraday report-index --dir reports/ --out reports/index.html

# Read-only paper poll (NO orders) at the latest stored session:
python -m scripts.live_paper_poll --provider yahoo-store --store-root data_raw/store_yahoo --symbols SPY QQQ

# Utilities:
python -m intraday doctor        # environment health (no network; never probes Theta)
python -m intraday strategies    # list strategies and what each does
python -m intraday version       # engine + Python versions
```

> Pre-rendered, illustrative (synthetic) examples to open without running anything:
> [`docs/sample_dashboard.html`](docs/sample_dashboard.html),
> [`docs/sample_comparison.html`](docs/sample_comparison.html), and
> [`docs/sample_index.html`](docs/sample_index.html).

> **Status:** Phase 0 + Phase 1 complete and green (**487 tests**, lint + type-checked),
> plus a wired **real-data path**, a **powered, multiple-testing-honest evaluation**, and a
> **self-contained HTML report suite** — `report` (dashboard), `compare`
> (multi-strategy), `report-index`, and a JSON summary (`--emit-json`).
> Strategies behind the one net-of-cost gate: S1 (gamma-regime), S2 (0DTE VRP,
> **defined-risk only**), S3 (VWAP reversion), **S4 (ORB breakout)**, **S5 (VWAP
> momentum)**, plus a paper ledger and a read-only live poller. **Data tiers:**
> options from **Theta STANDARD**; **underlying** from **IBKR** or **Yahoo** (free,
> reads only) or **put-call parity** — never Theta. **Headline result:** on 24
> symbols × 59 real 5-min sessions, net of costs, **no underlying-only strategy
> shows an edge** (all deflated-Sharpe ≈ 0; OOS fails) — the engine reports the
> truth, not a fabricated edge. See [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md) §0
> and [`docs/REAL_DATA.md`](docs/REAL_DATA.md).

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
