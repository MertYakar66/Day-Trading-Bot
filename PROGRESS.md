# PROGRESS — Intraday Engine build log

> Running log of what shipped, decisions + rationale, assumptions, data source,
> open items, and the exact next step. Newest section at the top. Companion to
> `TASKS.md` (the backlog) and `DESIGN.md` (the spec).

---

## Session 2026-06-01/02 — Phase 0 foundation (build/phase-0)

**Data source: SYNTHETIC (deterministic, seeded). No real market data.** The
Theta Terminal was off-limits this session (the operator uses the subscription
concurrently) and is FREE-tier anyway (only `/v3/stock/history/eod` is unlocked —
no SPX/VIX index, no intraday stock, no option tape/chain). The whole engine is
built and tested against `SyntheticDataProvider`, labelled unmistakably as
synthetic everywhere it surfaces.

### What shipped (Phase 0, TASKS.md T0.0–T0.8)

A complete, tested, paper-only intraday engine under the `intraday/` package:

- **T0.0 SWE dependency wired.** `vendor/swe` on path via `pyproject.toml`
  (`pythonpath`), root `conftest.py`, and `intraday/_vendor.py`. `import
  engine.*` / `import backtests.*` resolve; verified all consumed modules import
  under numpy 2.4 / pandas 3.0.
- **P0 tier probe.** Recorded in `data/README.md` and `docs/THETA_TIER_PROBE.md`:
  FREE tier, intraday data gated. (Probe run once before the no-Theta instruction
  arrived; not repeated.)
- **T0.1 DataProvider interface + providers.** `intraday/data/provider.py`
  (abstract), `synthetic.py` (deterministic workhorse), `theta_adapter.py`
  (real path, wired to SWE `theta_connector`, **never connected** this session).
- **T0.2 Parquet store.** `intraday/data/store.py` — DESIGN §2.3
  `ticker=/date=` layout, lossless round-trip.
- **T0.3 Feature builders.** GEX/flip/walls (`features/gex.py` → SWE
  `dealer_positioning`), OFI (`features/ofi.py`), intraday RV
  (`features/realized_vol.py` → SWE `realized_vol`, rescaled), VRP
  (`features/vrp.py`), VWAP+bands (`features/vwap.py`), opening range
  (`features/opening_range.py`), assembled by `features/pipeline.py`. All causal +
  PIT-sampled.
- **T0.4 Event-driven backtest.** `intraday/backtest/engine.py` — timestamp
  order, no look-ahead, conservative next-bar fills, costs from SWE.
- **T0.5 Metrics.** `intraday/metrics.py` → SWE `performance_metrics` + intraday
  cost/turnover/expectancy stats. NET of costs.
- **T0.6 Expectancy gate.** `intraday/authority/gate.py` — the sole authority;
  net-of-cost EV; negative/non-finite → blocked.
- **T0.7 Downgrade-only reviewers.** `intraday/authority/reviewers.py` — event
  lockout, regime filter, liquidity gate, daily kill-switch; proven downgrade-only.
- **T0.8 S3 control.** `intraday/signals/s3_vwap_orb.py` — VWAP-reversion /
  opening-range; runs end-to-end through the gate.

**One documented command** (prints a NET-of-costs metrics report):

```
python -m intraday backtest --start 2026-05-01 --end 2026-05-29
```

### Headline Phase-0 result (SYNTHETIC — not an edge)

On random-walk-dominant synthetic SPY/QQQ over a synthetic month, S3's **gross
PnL ≈ $0** (≈+$73 over 301 trades / ~$6M turnover) and the **net result is
negative entirely due to transaction costs** (≈ −1.8% of NAV; ~3 bps of
notional/round-trip). This is the honest, expected outcome for a naive control on
efficient prices and validates the harness three ways:
1. **No look-ahead** — gross ≈ 0 on a driftless walk (leakage would make it
   systematically positive); win rate ≈ 1/3 matches gambler's-ruin for fading a
   2σ stretch with a 1σ stop.
2. **Costs have real, dominant bite** — the whole loss is frictions.
3. **No fabricated edge** — an integration test confirms the engine *captures*
   gross edge when the synthetic world is mean-reverting and *manufactures none*
   when it is a random walk.

### Key decisions + rationale

1. **Single `intraday/` package** (not top-level `data/`, `signals/`, …). Avoids a
   real namespace collision with SWE's top-level `data/` and `backtests/` packages
   on `PYTHONPATH`. Matches the existing scaffold READMEs (`# intraday/data/`).
2. **Purpose-built event-driven backtester instead of subclassing SWE
   `backtests.simulator.WheelBacktester`.** The API map confirmed it is a daily,
   wheel-specific, documented *placeholder* coupled to `WheelTracker` — wrong base
   for intraday multi-instrument event replay. We reuse SWE's *quant* pieces
   (costs, pricer, metrics, dealer, RV) and its discipline patterns
   (profit-target/stop/MTM) and never modify SWE. (Deviation from TASKS.md
   wording "extend backtests/simulator.py", taken as the technical owner; it
   honors the "never modify SWE" guardrail and is more correct.)
3. **Own `ticker=/date=` parquet store** instead of SWE `data.feature_store`
   (which only partitions by `ticker=` and triggers a heavy `data/__init__`). Ours
   matches DESIGN §2.3 exactly and stays light.
4. **Intraday RV rescaling.** SWE `realized_vol` hard-codes 252-day (daily)
   annualization; intraday RV is rescaled by `sqrt(bars_per_day)` (derivation in
   `intraday/features/realized_vol.py` and `docs/SWE_API_REFERENCE.md`).
5. **Costs as an explicit line, fills at the conservative reference price.** Fills
   occur at the next bar's open (one-bar delay); spread + sqrt impact + commission
   are accounted in `costs`, NOT folded into the fill price — so gross (price PnL)
   and frictions stay separate and slippage is never double-counted. (Caught and
   fixed a double-count during integration.)
6. **Synthetic price process is random-walk dominant** (near-zero minute-bar
   autocorrelation, like real index ETFs). An earlier strongly-reverting OU made
   the naive control look fabulous (Sharpe ~14) — a synthetic artifact. The shape
   is a *realism* choice set independently of any strategy's PnL; the reversion
   strength is a `DataConfig` knob so tests can create both worlds.
7. **S3 `win_prob` is a deliberately neutral prior (0.50).** The control makes no
   predictive claim; its expectancy is pure reward/risk geometry, and the gate
   decides tradeability net of costs. The obvious calibration knob once real data
   exists.

### Assumptions (NOT confirmed live-money parameters — DESIGN §11)

- **Paper NAV = $100,000** (`RiskConfig.paper_nav`) — sizing math only.
- **Daily loss limit = 3% of NAV**, **per-trade risk cap = 1%**, **max position
  notional = 20% NAV**, **max 3 concurrent positions**, **half-Kelly**.
- **EV threshold = 0.0** (require strictly positive net EV).
- **Arrival latency**: bars 250 ms, chain 1 s, tape 250 ms (drives PIT availability).
- **Cost model**: SWE defaults (commission $0.65/option contract, $0 stock;
  base slippage 15% of spread; sqrt-impact coef 0.10); fallback spread 5 bps.
- **Synthetic anchors/IV/ADV** are fictional but order-of-magnitude plausible.

### SWE upstream changes wanted (NONE applied — submodule is read-only)

- None required. `realized_vol` could optionally expose a `periods_per_year`
  argument to avoid the intraday rescale, and `transaction_costs` aggregate
  helpers hard-code the ×100 multiplier (we call `calculate_slippage` directly to
  size stock correctly) — both are *nice-to-haves to propose upstream*, not blockers.

### Open items / what a human must decide next

1. **Theta tier** — upgrade to STANDARD to unlock real intraday data, or keep
   synthetic-only for harness development.
2. **Paper NAV & PDT** — confirm the account size paper results should model.
3. **Acceptance thresholds** (DESIGN §8) — calibrate Sharpe/expectancy/DD bars
   once real-data noise is visible.
4. **S3 `win_prob`** and entry_z/stop_k — calibrate against realized hit-rate.

### Next step

Phase 0 is green. Next: Phase 1 S1 (gamma-regime) and S2 (0DTE VRP, defined-risk
only), both behind the same gate, reusing the GEX/VRP features already built and
tested. Then a paper ledger (`execution/`) matching the backtest record shape.
