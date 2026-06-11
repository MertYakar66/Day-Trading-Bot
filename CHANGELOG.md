# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Post-0.1.0 hardening from a second audit pass (no version bump yet; the **NO-EDGE
headline is unchanged**). Tests 487 → 529 → 544.

### Fixed (2026-06-10 adversarially-verified audit, PR fix/audit-backlog)
- **Risk metrics dropped the inception point**: the equity curve handed to SWE's
  performance report started at day 1's CLOSE, so the first day's return was
  silently excluded from Sharpe/Sortino/volatility and max drawdown was censored
  against a day-1 peak (a 10% day-1 loss reported `max_drawdown == 0.0`).
  `build_report` now prepends the initial-capital row, re-annualizes over the true
  session count, and keeps Calmar consistent; the dashboard's underwater curve
  seeds its peak at inception capital. (The DSR-based EDGE/NO-EDGE verdict path
  was always constructed correctly and is unchanged.)
- **Fills-ledger cost double-count**: CLOSE fill records carried the full
  round-trip cost while the OPEN fill already carried the entry leg, so the
  persisted ledger (the designated live-vs-backtest reconciliation record)
  overstated costs ~150%. `Fill.cost` is now strictly per-leg (structured settles
  book 0 on the close leg), and the EOD safety-flatten path now appends its CLOSE
  fill. Trade/equity accounting was never affected. New invariant test:
  `sum(fill.cost) == sum(trade.costs)` and every OPEN has a CLOSE.
- **Expiry discipline in the options features**: `gamma_structure_at` and
  `atm_iv_at` read the whole PIT snapshot, so a real multi-expiry capture would
  blend expiries (measured on real SPX+SPXW chains at ~3,000x GEX distortion).
  Both now restrict to the requested expiry and return `None` when the snapshot
  does not carry it — an absent tenor is unknowable, not approximated.
- **Verdict floor is a validity floor, not a degeneracy guard**:
  `INSUFFICIENT_DATA_MIN_DAYS` raised 2 → 20. Below ~a month of sessions the
  clustered-t / bootstrap / DSR machinery has too few daily observations to mean
  much, so every surface now abstains (INSUFFICIENT_DATA) instead of printing a
  definitive verdict on, e.g., a 5-day run. Documented explicitly as not a power
  guarantee: NO_EDGE always means "no evidence at this sample size".
- **Comparison rows now use the three-state verdict**: per-row badges route
  through `eval.edge_verdict` (new neutral `badge--nodata` style), so a too-short
  run reads "insufficient data" in the table exactly as the page band does,
  instead of a definitive binary "no edge".
- **Adversarial review pass on the above** (own findings, fixed before merge):
  1-day runs no longer leak NaN volatility/Sharpe/Sortino through SWE's guards
  (the pre-inception `0.0` contract is kept, and the dashboard Sharpe KPI uses
  the NaN-safe formatter); `FeaturePipeline.row` passes the expiry through to
  `vrp_at` so the pipeline path agrees with the engine path on multi-expiry
  chains; the comparison page-level band keys off the same centralized verdict
  as its rows; `pd.Timestamp` expiries are normalized (they pass
  `isinstance(date)` but never equal a `date` elementwise, silently filtering
  every row); a tenor-mismatched chain capture now logs a loud warning instead
  of masquerading as a quiet no-trade session; samples regenerated.

### Fixed
- **Packaging (shipping defect)**: the wheel shipped only the top-level modules
  (`packages = ["intraday"]`), dropping every subpackage — a clean `pip install`'d
  `intraday` console script crashed on the first import. Switched to setuptools
  auto-discovery; CI now builds the wheel and smoke-imports every subpackage + the
  entry point from outside the source tree.
- **No-data honesty**: a zero-trading-day run (reversed dates / un-ingested store) no
  longer prints "NO demonstrated edge" and exits 0 — `backtest`/`report`/`compare`
  warn on stderr, exit non-zero, and suppress the verdict (and write no misleading
  HTML). The dashboard shows an INSUFFICIENT DATA band (not NO-EDGE) when `n_days < 2`.
- **Verdict consistency**: a single three-state verdict (`EDGE` / `NO_EDGE` /
  `INSUFFICIENT_DATA`) is centralized in `eval.edge_verdict`, so the CLI scorecard,
  the JSON export, and the HTML dashboard/comparison agree — a 1-day run now reads
  INSUFFICIENT_DATA on every surface instead of a definitive NO_EDGE on some.
- **Operator-script console output**: ASCII-hardened the print/log/help strings in
  the operator scripts (`live_paper_poll`, `fetch_yahoo_universe`,
  `ingest_ibkr_underlying`, `pull_theta_options_scoped`) so they don't mojibake on a
  cp1252 Windows console.
- **Windows console**: the synthetic-data banner used em-dashes that rendered as
  mojibake on cp1252; now ASCII (with a runtime-output ASCII test).
- **Parity provenance**: the put-call-parity underlying path enforces a grid
  `min_coverage` floor (0.8) before forward-filling, so a heavily-reconstructed
  session is never mislabelled solid `[REAL DATA: parity]`.

### Added
- **The Theta capture path is now executable end-to-end** (audit gaps G1–G11; it
  previously existed only as docs with fabricated flags):
  - `scripts/pull_theta_tape_scoped.py` — operator-only, doubly-gated backfill
    puller with historical `--start/--end`, PER-SESSION expiration resolution
    (0DTE when listed, else nearest on-or-after), trades via `trade_quote`
    (prevailing NBBO → real, PIT-safe `side_inferred`; `/trade` fallback
    degrades honestly to `"mid"`), 1m quote bars + daily OI, per-partition
    manifests, `--resume`, `--probe-day`, and loud per-partition failures.
    Output under `data_raw/theta/` (never inside read-only `vendor/`).
  - `scripts/ingest_theta_options.py` — network-free ingest: tape (THETA,
    `available_ts` stamped), quotes (new `option_quotes` store slot), and
    **synthesized chain snapshots** (`DataSource.THETA_DERIVED`): quote mids +
    locally BS-inverted IV (at the GEX consumer's `max(days,1)/365` clock — a
    decaying T would diverge toward the close) + parity spot + PRIOR-session OI
    (day-D EOD OI is a future fact intraday). Refuses non-0DTE-only chains and
    SPX/SPXW synthesis; failed inversions are dropped and counted, never
    placeholdered. Optional `--parity-bars` never clobbers IBKR partitions.
  - `--provider fused-store` — composes IBKR→parity→yahoo underlying bars with
    Theta options from one store (the single-source `theta-store` provider
    rightly refuses such mixed-provenance stores); `FusedDataProvider`'s
    calendar is now the UNION of its underlying calendars (a shallow primary no
    longer hides a deep parity backfill); `StoreBackedProvider` gained a
    `chain_source` override so raw THETA tape and THETA_DERIVED chains serve
    from one provider without relabelling.
  - `docs/OPERATOR_RUNBOOK.md` rewritten around the real, verified commands
    (probe-first discipline; budget from the probe, not the estimate).
  - **Adversarially reviewed pre-PR** (5-lens panel): the look-ahead lens
    returned zero findings; every confirmed finding elsewhere was fixed —
    per-partition ingest resilience (an incomplete raw partition is skipped
    loudly, never a run-aborting crash), atomic per-session ingest (a refused
    chain writes nothing), OI join keys rounded (a strike-representation drift
    can no longer silently zero all gamma — and matching ZERO contracts against
    a non-empty OI table now raises), strike-unit mismatches across endpoints
    are loud, expiration-listing failures are journaled per-symbol, the
    fused-store preflight fails options runs up front on chain-less sessions,
    and the pure synthesis core moved into `intraday/data/chain_synthesis.py`
    (the `ibkr.py` layering precedent).
- Eval honesty disclosures: an i.i.d. serial-dependence caveat (the stationary
  bootstrap CI is named as the short-range-robust counterweight) and a small-`n_trials`
  caveat on a green EDGE verdict.
- Tests for `sigma_stop_target`, OOS split bounds + rolling walk-forward, DSR `var_sr`
  monotonicity, and the live paper-ledger `log_*` hooks.
- **Network-free is now enforced, not assumed**: an autouse test fixture blocks
  outbound sockets, so the engine's "never connects" promise fails the suite loudly
  if any test ever reaches the network (opt out with `@pytest.mark.allow_network`).
- **Report accessibility**: every inline-SVG chart now carries an accessible name
  (`aria-label` + `<title>`) so the offline reports are navigable by screen readers.

### Changed
- `doctor` verifies the core scientific stack (numpy/pandas/scipy/pyarrow); duplicate
  `--strategy` keys are de-duped; chart money formatting matches the KPI convention
  (minus before the `$`).
- Docs: `THETA_TIER_PROBE.md` marked superseded/scoped with the adapter's actual
  behaviour (`DataUnavailable` / `ThetaNotConnectedThisSession`, not a blanket
  `TierUnavailable`); test counts and the S4/S5/OOS figures synced to the committed
  eval JSON.

## [0.1.0] - 2026-06-03

First tagged, launch-ready release of the paper-only intraday research engine.

### Added
- **CLI UX**: `version` / `--version`, `strategies` (catalogue), and `doctor`
  (environment health — Python, read-only `vendor/swe`, local data store; never
  touches the network/Theta). A shared strategy registry is the single source of
  truth for `--strategy` choices and builders.
- **Report aesthetics/UX**: Sortino & Calmar KPIs, a colour-blind up/down chevron
  on signed values, a rolling-Sharpe section, a per-symbol breakdown for
  multi-symbol runs, and a print/PDF stylesheet (legible on white paper).
  `scripts/gen_samples.py` regenerates the illustrative `docs/sample_*.html`
  deterministically.
- **Tooling**: `ruff` (lint) and `mypy` (type-check) configured and enforced in CI
  (pinned versions); `pre-commit` config; packaging metadata, a console-script
  entry point (`intraday`), and a `[dev]` extra.
- **Docs**: `CONTRIBUTING.md`, `CODEOWNERS`, this changelog, a README reading guide.

### Changed
- **Backend robustness**: `read_bars` refuses a missing provenance sidecar (no
  silent `SYNTHETIC` relabelling); empty/NaN/inf bar frames are rejected; the
  bar-grid check compares timestamps, not just counts; `EngineConfig` validates its
  knobs at construction. S3/S4/S5 use S1's explicit strictly-positive geometry guard.
- Report charts degrade non-finite colour inputs to neutral; `SERIES_PALETTE`
  rebalanced for luminance.

### Notes
- **Headline result is unchanged and honest: NO demonstrated edge** on real data
  (all deflated-Sharpe ≈ 0; OOS fails). This release improves the *product around*
  that result; it does not manufacture an edge.
- Licence is a conservative proprietary default (see `LICENSE`); swap for an OSI
  licence if open-sourcing.

## Prior milestones (pre-changelog)

- Phase 0 + Phase 1 engine (S1–S5 behind one net-of-cost gate, paper ledger).
- Real-data path (IBKR / put-call parity underlying, Theta options-only; never
  Theta for the underlying) and a powered, multiple-testing-honest evaluation.
- Self-contained offline HTML report suite (dashboard / comparison / index / JSON).
