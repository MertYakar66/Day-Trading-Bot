# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Post-0.1.0 hardening from a second audit pass (no version bump yet; the **NO-EDGE
headline is unchanged**). Tests 487 → 529.

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
- **Persisted paper polls + a daily prospective routine**: `live_paper_poll`
  gained `--persist-root`, writing the gated decisions in the
  **backtest-identical signal schema** (`LiveDecision.to_signal_row`, record
  parity per DESIGN §8) so paper polls and backtest replays are directly
  diffable; `LiveDecision` now carries `ev_gross` for exact parity. New
  [`docs/DAILY_ROUTINE.md`](docs/DAILY_ROUTINE.md) documents the once-a-day,
  no-Theta collection loop (fetch → ingest → persisted poll → single-day
  `backtest --store`).
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
