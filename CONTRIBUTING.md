# Contributing

This is a **paper-only research engine**. Contributions are welcome, but the hard
guardrails below are non-negotiable — they are what makes the project trustworthy.

## Hard guardrails (read first)

These are enforced by code, tests, and review — see [`README.md`](README.md) §Operating rules
and [`DESIGN.md`](DESIGN.md) §6.

1. **PAPER ONLY.** No broker / live-order placement. `execution/` is a paper ledger.
2. **No look-ahead.** Every feature/decision uses only data available at or before its
   timestamp (point-in-time). The `no_lookahead`-marked tests must keep passing.
3. **The net-of-cost expectancy gate is the sole authority**; reviewers may only *downgrade*
   a verdict, never upgrade it.
4. **Never fabricate or mislabel data or edge.** Real vs `SYNTHETIC` is always labelled; the
   EDGE / NO-EDGE verdict is driven solely by the Deflated Sharpe Ratio, never by author choice.
5. **The SWE dependency (`vendor/swe`) is read-only.** Propose changes upstream, never edit the
   vendored copy.
6. **Keep `main` releasable.** Work on a branch, open a PR, merge only when CI is green.

## Local setup

```bash
python -m venv .venv && .venv/Scripts/activate     # bin/activate on POSIX
pip install -r requirements.txt
pip install -e ".[dev]"                             # ruff, mypy, pre-commit (pinned)
pre-commit install                                  # optional: run checks on commit
```

## Before opening a PR

Every PR must pass the same gates CI runs:

```bash
ruff check intraday tests scripts     # lint (no formatting churn)
mypy                                  # type-check the package
pytest                                # full suite, network-free
```

Then sanity-check the engine and your environment:

```bash
python -m intraday doctor             # env health (never touches the network)
python -m intraday backtest --start 2026-05-01 --end 2026-05-29
```

## Review checklist

Reviewers (human or agent) should confirm each change is: paper-only, point-in-time,
cost-aware, honestly labelled, and accompanied by tests. A change that weakens any
guardrail is rejected regardless of how useful it looks.
