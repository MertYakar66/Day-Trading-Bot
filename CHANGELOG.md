# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
