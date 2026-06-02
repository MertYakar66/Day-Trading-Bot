# `intraday/signals/` — strategy modules

Each strategy is thin, independently testable, and must pass the
`intraday/authority/` expectancy gate before emitting a paper trade.

- **S1 Gamma-regime** (SPX/SPY) — the spine. `../DESIGN.md` §5 S1.
- **S2 0DTE vol relative-value** (SPX) — defined-risk only. §5 S2.
- **S3 VWAP-reversion / opening-range** — the control/benchmark. **Build S3
  first** (Phase 0): if it misbehaves, the harness is wrong, not the market.

Tasks: `../TASKS.md` T0.8 (S3), then T1.1/T1.2 (S1/S2).
