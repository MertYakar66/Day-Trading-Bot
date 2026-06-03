# Day-Trading-Bot — build backlog

> **Status (2026-06-03): Phase 0 + Phase 1 COMPLETE and green** (synthetic + a wired
> real-data path; 487 tests, lint + type-checked — see `PROGRESS.md` / `CHANGELOG.md`).
> Phase 1 tasks T1.1–T1.3 below are done (`[x]`). Run the engine with
> `python -m intraday backtest --start 2026-05-01 --end 2026-05-29`.

Ordered tasks for building the intraday engine. **Do tasks in order**; each has
explicit done-criteria. Read [`DESIGN.md`](DESIGN.md) before starting and
[`README.md`](README.md) for the hard guardrails (repeated in §0 below).

> **Path convention:** `engine/*`, `scripts/*`, and `backtests/*` refer to the
> **smart-wheel-engine (SWE)** dependency (see README → Dependencies). All other
> paths (`data/`, `features/`, `signals/`, …) are in *this* repo.

---

## 0. Guardrails (read before writing any code)

- **PAPER ONLY** — no broker / live-order placement. `execution/` = paper ledger.
- **Never modify the SWE dependency** (`engine/*` etc.). Consume read-only;
  propose upstream changes in the smart-wheel-engine repo, not the vendored copy.
- **No look-ahead** — PIT timestamps on every feature/backtest row.
- **Mandatory net-of-cost expectancy gate** before any paper trade (one
  authority + downgrade-only reviewers — `DESIGN.md` §6).
- **Tests for every module.** Add `tests/` and run `pytest` on it.
- **Keep `main` releasable** — work on a branch, merge via PR; no half-built
  code on `main`.
- **Blocked on an open question?** Stub + `TODO` + document the assumption. Never
  guess live-money parameters.

---

## Prerequisites

- [ ] **T0.0 Wire the SWE dependency.** Make SWE's quant modules importable
      (git submodule into `vendor/swe` + `PYTHONPATH`, or editable install — see
      README → Dependencies). *Done:* `import engine.option_pricer`,
      `import engine.dealer_positioning`, and `import backtests.simulator`
      resolve in the project venv; the chosen mechanism is documented.
- [ ] **P0. Confirm the Theta tier** *(human / laptop — not doable in a
      sandbox)*. Run `scripts/probe_theta_capabilities.py` in the SWE checkout
      with the Terminal up. Confirm real-time intraday **stock** and **option
      tape** are unlocked. Record the result in `data/README.md`. *Blocks all
      data tasks.*

---

## Phase 0 — Foundation

Goal: replay one month of SPX/SPY/QQQ and produce a **cost-correct equity
curve** for the S3 control strategy behind the expectancy gate.

- [ ] **T0.1 Data adapter** (`data/`). Wrap `engine/theta_connector.py` for
      intraday pulls: SPX 1-second index, SPY/QQQ 1-minute bars + tick
      trades/NBBO, option-chain snapshots, and the option tape via the existing
      `scripts/pull_theta_option_tape.py`. *Done:* fetch one trading day for
      SPX/SPY/QQQ into the parquet store, every row PIT-stamped; unit-tested
      against a recorded fixture.
- [ ] **T0.2 Parquet store** (`data/`). Helpers for the layout in `DESIGN.md`
      §2.3 (`bars/`, `option_tape/`, `features/`), partitioned `ticker=/date=`.
      *Done:* write/read round-trip; schemas documented.
- [ ] **T0.3 Feature builders v0** (`features/`): GEX + gamma-flip + walls (wrap
      `engine/dealer_positioning.py`); order-flow imbalance from the tape's
      `side_inferred`; intraday realized vol (`engine/realized_vol.py`); VRP
      (ATM IV − intraday RV); VWAP + bands. *Done:* each builder produces a
      tested feature frame for one day, no look-ahead.
- [ ] **T0.4 Backtest skeleton** (`backtest/`). Extend `backtests/simulator.py`
      into an event-driven intraday replay: timestamp order, no look-ahead,
      conservative fill (next-bar/NBBO + modelled slippage), costs from
      `engine/transaction_costs.py`. *Done:* replays one month for a trivial
      strategy and produces a cost-correct equity curve.
- [ ] **T0.5 Metrics** (`backtest/`). Wire `engine/performance_metrics.py`: net
      Sharpe/Sortino, max drawdown, hit-rate, payoff ratio, expectancy/trade,
      turnover, cost drag. *Done:* a report object from a backtest run.
- [ ] **T0.6 Expectancy gate (authority)** (`authority/`). The single gate:
      `E[net PnL] > threshold` using `engine/transaction_costs.py`; negative or
      non-finite → blocked. *Done:* unit-tested gate that refuses negative-EV
      signals (the intraday analogue of SWE's `EVEngine.evaluate`).
- [ ] **T0.7 Downgrade-only reviewers** (`authority/`): event lockout
      (`engine/event_gate.py`), regime filter (`engine/regime_*`), liquidity
      gate, daily kill-switch (`engine/risk_manager.py`). *Done:* reviewers can
      only demote (proceed→review→skip→blocked); unit-tested they never upgrade.
- [ ] **T0.8 S3 control strategy** (`signals/`): VWAP-reversion / opening-range
      — the simple, well-understood benchmark. *Done:* S3 runs through the gate +
      reviewers and produces sane backtest behaviour. **If S3 misbehaves, the
      harness is wrong — fix that before trusting exotic signals.**

**Phase 0 exit criteria:** one month of SPX/SPY/QQQ replays end-to-end → S3
produces a cost-correct equity curve, gated, with a full metrics report.

---

## Phase 1 — The gamma spine (only after Phase 0 passes)

- [x] **T1.1 S1 Gamma-regime** (SPX/SPY) — `DESIGN.md` §5 S1. *Done:*
      `intraday/signals/s1_gamma_regime.py`; engine computes GEX/OFI features per
      tick (only when a strategy needs them); runs end-to-end through the gate;
      tested (`tests/test_s1.py`, `tests/test_phase1.py`). See `PROGRESS.md`.
- [x] **T1.2 S2 0DTE vol relative-value** — `DESIGN.md` §5 S2.
      *Defined-risk structures only; no naked short gamma.* *Done:*
      `intraday/signals/s2_zerodte_vrp.py` — a defined-risk short iron condor gated
      by VRP-rich + positive net GEX; priced from the chain via BSM; honest
      win_prob = fair baseline + VRP edge; explicit 4-leg round-trip cost; settled
      binary at the 0DTE close by the engine. Tested (`tests/test_s2.py`,
      `tests/test_phase1.py`). NOTE: on a $100k account at the 1% per-trade risk
      cap, a *wide SPX* condor's max loss exceeds the budget so S2 correctly sizes
      to 0 on SPX (a real retail constraint — DESIGN §1); demonstrated on SPY.
- [x] **T1.3 Paper ledger** (`execution/`) — log each gated signal as if filled,
      mark-to-market on subsequent ticks; same record shape as the backtest so
      live-vs-backtest divergence is measurable. *Done:*
      `intraday/execution/paper_ledger.py` + `records.py` (canonical schemas shared
      with the backtest); `from_backtest` proves record parity; persists to the
      `signals/` + `paper_ledger/` store partitions. PAPER ONLY — no broker. The
      live streaming loop awaits real data + an explicit go-live decision.

**Phase 1 exit:** all three strategies pass the §8 acceptance bar on paper
(positive net-of-cost expectancy with confidence, drawdown within budget,
paper consistent with backtest).

---

## Phase 2 — Equities

- [ ] **T2.1** ~20 liquid single names + sector ETFs: S3 + a single-name
      gamma/flow variant, same gate, same acceptance bar.

## Phase 3 — Real futures (optional)

- [ ] **T3.1** ES/NQ via a new feed (Databento / IBKR) — only if the
      SPX/SPY/QQQ proxy is shown insufficient.

---

## Open questions (need operator input — do not guess)

See `DESIGN.md` §11: (1) Theta tier confirmation, (2) how to wire the SWE
dependency (submodule vs editable install vs vendor), (3) paper NAV & PDT
assumptions, (4) acceptance thresholds.
