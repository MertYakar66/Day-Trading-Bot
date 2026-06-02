# Architecture — Intraday Engine

A map of the codebase for humans and agents. Read `DESIGN.md` for the *why* and
`docs/SWE_API_REFERENCE.md` for the read-only dependency's API.

## One-liner

Data (PIT) → Features (causal) → Strategy → Sizing → **Expectancy gate
(authority)** → Downgrade-only reviewers → Conservative fills → Trades → Metrics.
Everything is paper-only, no look-ahead, net of costs.

## Package layout (`intraday/`)

```
intraday/
  config.py            # EngineConfig: all knobs (cost, risk, session, gate, data)
  contracts.py         # enums + PIT data containers + Signal/Gate/Trade dataclasses
  timeutils.py         # RTH calendar, bar-close index, interval parsing, seeds
  costs.py             # net-of-cost model over SWE transaction_costs (stock & option)
  metrics.py           # MetricsReport over SWE performance_metrics + intraday stats
  logging_config.py    # structured logging
  _vendor.py           # puts vendor/swe on sys.path (no SWE import, no network)

  data/                # T0.1/T0.2 — the data layer
    provider.py        #   DataProvider ABC + asset-kind registry + exceptions
    synthetic.py       #   SyntheticDataProvider (deterministic; THE workhorse)
    theta_adapter.py   #   ThetaDataProvider (real path; never connects this session)
    store.py           #   ParquetStore (ticker=/date= layout, DESIGN §2.3)
    quality.py         #   liquidity / staleness predicates

  features/            # T0.3 — causal, PIT-sampled feature builders
    base.py            #   FeatureRow + latest_value PIT sampler
    vwap.py opening_range.py ofi.py realized_vol.py vrp.py gex.py
    pipeline.py        #   FeaturePipeline → FeatureRow at a decision time

  authority/           # T0.6/T0.7 — the discipline layer (DESIGN §6)
    gate.py            #   ExpectancyGate — the ONE authority (net-of-cost EV)
    reviewers.py       #   downgrade-only reviewers + default wiring

  risk/                # T0.* — sizing + stops
    sizing.py          #   fractional-Kelly (SWE) with hard caps
    stops.py           #   sigma/structural stop+target

  signals/             # strategies
    base.py            #   Strategy protocol
    s3_vwap_orb.py     #   T0.8 — S3 VWAP-reversion control

  backtest/            # T0.4 — event-driven replay
    fills.py           #   conservative next-bar fill model
    engine.py          #   IntradayBacktester → BacktestResult

  cli.py / __main__.py # `python -m intraday backtest ...`
```

## The hard invariants (where they live)

| Invariant | Enforced in | Proven by |
|---|---|---|
| No look-ahead (PIT) | `contracts.py` containers' `available_at`/`latest_available`; `features/base.latest_value`; engine positional read | `tests/test_no_lookahead.py` |
| Net-of-cost EV is the sole authority | `authority/gate.py` | `tests/test_gate.py` |
| Reviewers only downgrade | `contracts.Verdict.downgraded_to`, `GateResult.with_downgrade` | `tests/test_reviewers.py` |
| Costs subtracted correctly | `costs.py`, `backtest/fills.py` | `tests/test_costs.py` |
| Paper-only, intraday-only | `backtest/engine.py` (flatten before close; no broker) | `tests/test_integration_phase0.py` |

## Reused SWE modules (read-only, via thin adapters)

`transaction_costs` (costs.py), `dealer_positioning` (features/gex.py),
`realized_vol` (features/realized_vol.py), `option_pricer` (synthetic IV/greeks
indirectly), `performance_metrics` (metrics.py), `event_gate` + `event_calendar`
(authority/reviewers.py), `risk_manager` (risk/sizing.py). Never modified.

## Run it

```
python -m intraday backtest --start 2026-05-01 --end 2026-05-29   # prints NET metrics
pytest                                                            # full suite
```

## Extending (Phase 1)

Add S1/S2 as `signals/s1_gamma_regime.py` / `signals/s2_0dte_vrp.py` implementing
the `Strategy` protocol; they consume the existing GEX/VRP features and flow
through the same gate + reviewers. Wire a `RegimeFilterReviewer` hostile map for
S1. Add `execution/paper_ledger.py` mirroring `BacktestResult` record shape.
