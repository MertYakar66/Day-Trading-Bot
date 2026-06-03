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

  data/                # T0.1/T0.2 + real-data path — the data layer
    provider.py        #   DataProvider ABC + asset-kind registry + exceptions
    synthetic.py       #   SyntheticDataProvider (deterministic; the test fixture)
    _remap.py          #   SHARED start→close canonical-grid remap (ffill-only; no-look-ahead)
    ibkr.py            #   IBKRDataProvider — REAL underlying bars (reads only)
    yahoo.py           #   YahooDataProvider — FREE real intraday underlying (reads only)
    parity.py          #   ParityUnderlyingProvider — underlying via put-call parity (deep hist)
    store_provider.py  #   StoreBackedProvider — offline replay of captured data (CI-safe)
    fused.py           #   FusedDataProvider — underlying fallbacks + Theta options
    theta_adapter.py   #   ThetaDataProvider (OPTIONS-only; disconnected this session)
    store.py           #   ParquetStore (ticker=/date= layout, DESIGN §2.3)
    quality.py         #   liquidity / staleness predicates
    # corrected data tiers + how-to: docs/REAL_DATA.md, docs/OPERATOR_RUNBOOK.md

  eval/                # honesty harness (anti-overfitting)
    stats.py           #   clustered-t, bootstrap CI, Probabilistic/Deflated Sharpe (Bailey-LdP)
    walkforward.py     #   chronological / walk-forward splits (leakage-free OOS)

  live/                # read-only single-shot paper poll (NO orders)
    poller.py          #   LivePoller: snapshot → PIT features → gate → reviewers → paper decision

  report/              # self-contained OFFLINE HTML reports (no deps, no CDN)
    theme.py           #   shared CSS + document() shell (one stylesheet for all pages)
    svg.py             #   pure-Python inline-SVG charts (line/multi-line/area/bars/waterfall/heatmap)
    dashboard.py       #   render_dashboard/build_dashboard: KPIs + charts + honesty scorecard
    comparison.py      #   build_comparison: overlay strategies (equity curves + ranked table)
    index_page.py      #   build_index: static index linking every report in a directory
    export.py          #   summary_dict/build_summary: machine-readable JSON sibling

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

  signals/             # strategies (all behind the one gate)
    base.py            #   Strategy protocol (needs_options/needs_rv capability flags)
    s3_vwap_orb.py     #   T0.8 — S3 VWAP-reversion control
    s1_gamma_regime.py #   T1.1 — S1 gamma-regime (fade/ride by GEX sign + OFI)
    s2_zerodte_vrp.py  #   T1.2 — S2 0DTE VRP defined-risk iron condor
    s4_orb_breakout.py #   S4 — opening-range breakout (momentum; underlying-only)
    s5_vwap_momentum.py#   S5 — VWAP momentum / trend (mirror of S3; underlying-only)

  backtest/            # T0.4 — event-driven replay
    fills.py           #   conservative next-bar fill model
    engine.py          #   IntradayBacktester → BacktestResult (directional + structured)

  execution/           # T1.3 — PAPER ONLY paper ledger
    records.py         #   canonical fill/trade/signal serializers (shared w/ backtest)
    paper_ledger.py    #   PaperLedger (record parity; persists to store)

  cli.py / __main__.py # `python -m intraday backtest|report|compare|report-index ...`
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
python -m intraday backtest --start 2026-05-01 --end 2026-05-29              # S3 control (NET metrics)
python -m intraday backtest --symbols SPY --strategy s1 --start 2026-05-04 --end 2026-05-08
python -m intraday backtest --symbols SPY --strategy s2 --start 2026-05-04 --end 2026-05-08
pytest                                                                       # full suite (528 tests, network-free)
ruff check intraday tests scripts && mypy                                    # lint + type-check (CI gates)
```

S1/S2 load option features (GEX/OFI/RV) per tick, so they are slower than the
S3-only path; the expensive GEX solve is recomputed on a slow cadence
(`DataConfig.gex_recompute_min`).

## Defined-risk structures (S2)

S2 emits a *structured* proposal (`SignalProposal.win_amount`/`loss_amount`/
`cost_override`) rather than a directional price geometry. The gate/sizing use the
per-unit dollar economics; the engine settles such positions binary at the 0DTE
close. To add another defined-risk structure, extend the structured branch in
`backtest/engine.py` (currently shaped for the iron condor's center ± short_width).

## Extending (Phase 2)

Add single-name strategies implementing the `Strategy` protocol; they consume the
same features and flow through the same gate + reviewers. Deferred items (see
PROGRESS.md): thread `open_interest` into option proposals; an open-MTM daily
kill-switch for the live path.
