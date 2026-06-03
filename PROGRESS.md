# PROGRESS — Intraday Engine build log

> Running log of what shipped, decisions + rationale, assumptions, data source,
> open items, and the exact next step. Newest section at the top. Companion to
> `TASKS.md` (the backlog) and `DESIGN.md` (the spec).

---

## Session 2026-06-02 — Report suite: comparison, index, JSON (feat/report-suite)

Autonomous session. Extended the dashboard into a small **report suite** and did a
review/optimization pass on the existing module.

### What shipped

- `intraday/report/theme.py` — extracted the shared **CSS + `document()` shell**
  out of `dashboard.py` (was locked inside it). One stylesheet, one document
  wrapper, reused by every page — DRY and visually consistent.
- `intraday/report/svg.py` — new **`multi_line_chart`** (overlay N series) +
  `SERIES_PALETTE`; robust to uneven lengths and NaN/inf.
- `intraday/report/comparison.py` — **`build_comparison`**: S3/S4/S5 (or any subset)
  on one page — normalised equity curves overlaid (cumulative % return) with a
  matching legend, plus a ranked metrics table (best Sharpe highlighted). Comparing
  N strategies = N trials, so each Deflated Sharpe carries that penalty.
- `intraday/report/index_page.py` — **`build_index`**: a static index linking every
  report in a directory; titles pulled via a stdlib `HTMLParser` (robust to
  attributes/entities/nesting), excludes itself.
- `intraday/report/export.py` — **`build_summary`/`summary_dict`**: a machine-readable
  JSON sibling of the dashboard (non-finite -> null, JSON-safe; verdict ties to
  `significant`).
- `intraday/cli.py` — new **`compare`** and **`report-index`** subcommands; **`--emit-json`**
  on `report`/`compare`; shared `_write_html` helper.
- `tests/test_report_pages.py` (+ additions to `test_report.py`) — comparison overlay
  /ranking/escaping/determinism, JSON faithfulness + non-finite safety, index title
  extraction (incl. nested/malformed) + round-trip, multi-line robustness, CLI smoke.
- `docs/sample_{dashboard,comparison,index}.html` — pre-rendered illustrative examples.

### Review / fixes

Adversarial multi-agent review (52 findings, mostly confirmed-positive). One real
HIGH bug fixed: `_extract_title` used `str.find` and leaked literal markup on
nested/malformed `<title>` — replaced with an `HTMLParser`. Added the missing
coverage the review flagged (cost-attribution, exit-reasons, SVG-primitive edge
cases, title round-trip). Full suite: **419 passed** (was 378). `main` releasable.

---

## Session 2026-06-02 — Self-contained HTML dashboard (feat/html-dashboard)

Autonomous session. Goal: a genuinely useful operator utility on top of the
existing rigor — a **single offline HTML dashboard** that turns a finished
backtest + its honesty eval into something you can actually look at. No Theta, no
new heavy dependencies (charts are pure-Python inline SVG), fully tested.

### What shipped

- `intraday/report/svg.py` — dependency-free inline-SVG chart primitives
  (`line_chart`, `area_chart`, `bar_chart`, `waterfall`, `heatmap`, `sparkline`).
  Pure functions, deterministic (no clock/RNG), no external refs.
- `intraday/report/dashboard.py` — `build_dashboard(result)` / `render_dashboard(...)`:
  a one-file HTML doc with the SYNTHETIC-vs-REAL banner, the EDGE/NO-EDGE verdict
  band (driven solely by the Deflated Sharpe), KPI cards, equity curve (vs capital
  baseline), underwater drawdown, signed daily-PnL bars, the full honesty scorecard,
  a gross→costs→net cost-attribution waterfall, exit-reason breakdown, a trade
  blotter, and an optional cross-sectional **universe** section (per-strategy table
  + per-symbol Sharpe heatmap) from `eval_real_universe.py` JSON.
- `intraday/cli.py` — new `report` subcommand (shares run-args with `backtest` via
  `_add_run_args`): `--out`, `--title`, `--n-trials`, `--universe-json`, `--open`.
- `tests/test_report.py` — 22 tests: well-formed offline HTML (no external
  resources), one-SVG-per-chart, determinism, banner/verdict driven by data+eval,
  HTML-escaping (a backtest can't inject markup), drawdown math, defensive universe
  rendering, every SVG primitive, and a CLI smoke test.
- `docs/sample_dashboard.html` — a pre-rendered illustrative (synthetic) dashboard
  to open without running anything. Generated `dashboard.html` is gitignored.

### Properties locked

- **Offline**: all CSS inlined, all charts inline SVG — the only `http://` in the
  file is the inert SVG `xmlns`. No script/link/CDN. A test asserts this.
- **Honest by construction**: the verdict is the eval's (`significant` ⇔ deflated
  Sharpe ≥ 0.95), and SYNTHETIC is shouted, mirroring `metrics.render()`.
- **Deterministic** given the same data + `generated_at` string.

Full suite: **378 passed** (was 356; +22). No regressions; `main` releasable.

---

## Session 2026-06-02 — Powered real-data evaluation (feat/real-data-eval)

Autonomous session. Goal: get real data deep enough for a **statistically powered,
multiple-testing-honest** verdict (the prior blocker was IBKR's ~1000-bar cap),
build the evaluation rigor to support it, and answer "does any strategy have an
edge, net of costs, out-of-sample?" honestly. Paper-only; IBKR read-only; Theta
untouched; data pulled from anywhere except Theta.

### Data unlock — free Yahoo intraday (no Theta, no cost)

Yahoo's chart API (with a browser UA) serves ~**60 sessions of 5-min** bars free,
across a wide universe. Fetched + ingested a **24-symbol** cross-section (broad/
sector ETFs + large-caps) → **1,416 session-partitions** (2026-03-09..06-01),
`DataSource.YAHOO`. Two independent vendors now corroborate: IBKR (ARCA) vs Yahoo
(consolidated) 5-min closes agreed to **mean 0.12/0.20 bps, max 1.3/2.0 bps** over
936 SPY/QQQ bars each — strong data integrity.

### What shipped

- `intraday/data/yahoo.py` — `YahooDataProvider` (read-only `YahooClient` +
  default urllib client) + pure mapper + `ingest_payload`. `DataSource.YAHOO`.
- `intraday/data/_remap.py` — extracted the START→CLOSE canonical-grid remap
  (ffill-only, leading-gap skip) into ONE shared module used by IBKR and Yahoo, so
  the no-look-ahead discipline lives in a single place.
- `intraday/eval/` — the honesty harness: `clustered_t_stat` (one obs per trading
  day), `annualized_sharpe`, `stationary_bootstrap_ci`, `probabilistic_sharpe_ratio`
  + `deflated_sharpe_ratio` (Bailey & López de Prado — multiple-testing penalty),
  and chronological / walk-forward splits.
- `intraday/signals/s4_orb_breakout.py` (S4, momentum) and
  `s5_vwap_momentum.py` (S5, the mirror of S3 reversion) — underlying-only,
  testable on real data, each with an explicit falsifiable edge.
- `intraday/live/poller.py` — read-only single-shot `LivePoller` (snapshot →
  PIT features → gate → reviewers → paper decision; **no orders**).
- CLI: `--provider yahoo-store`, `--strategy s4/s5`, and a default **honesty
  scorecard** (clustered-t, bootstrap-CI Sharpe, deflated Sharpe) on every report.
- Scripts: `fetch_yahoo_universe`, `ingest_yahoo_universe`, `eval_real_universe`
  (powered cross-sectional eval), `live_paper_poll`.
- Tests: +`test_data_yahoo`, `test_eval`, `test_s4_s5`, `test_live_poller`,
  `test_data_quality_xvendor`. Full suite **356 passed**.

### Headline result — NO EDGE (real data, net of costs, multiple-testing-honest)

24 symbols × 59 real 5-min sessions, each strategy run standalone per symbol then
aggregated to an equal-weight portfolio:

| strat | portfolio Sharpe (ann) | 95% CI | day t | DSR over 72 trials |
|---|---|---|---|---|
| S3 reversion | −2.83 | [−5.39, 0.14] | −1.37 | **0.000** |
| S4 ORB breakout | −2.76 | [−6.85, 0.77] | −1.34 | **0.000** |
| S5 VWAP momentum | −4.89 | [−9.94, −1.07] | −2.37 | **0.000** |

- **Deflated Sharpe ≈ 0 for all** — after discounting 72 strategy×symbol trials,
  none has any probability of a true positive Sharpe.
- **OOS:** best-on-train (S4) did *worse* on the held-out half (test Sharpe −7.12).
- **Cost attribution (whole 24-symbol book):** S3 gross ≈ **−$348 (flat — no
  predictive edge)**, bled by **$12.4k** costs; S4/S5 are gross-*negative* too
  (breakouts fade, 5-min momentum reverts) plus costs. ~3.3k–3.9k trades each.
- **Verdict: no demonstrated edge.** This is the trustworthy outcome — the engine
  reports the truth (no edge) rather than a fabricated one. Earlier the 12-session
  IBKR S3 +$306 looked positive; the deeper 59-session test shows it was noise.

### Decisions + rationale

- **Cross-section over depth:** IBKR caps intraday history at ~13 sessions, so the
  statistical power comes from breadth (24 symbols × 59 Yahoo sessions), with a
  per-day clustered t and a portfolio series that respects same-day correlation.
- **Deflated Sharpe is mandatory:** testing reversion AND its mirror (momentum) on
  the same data is multiple testing; the DSR + OOS split keep it honest.
- **Per-symbol standalone runs** (then aggregate) avoid the 3-position concurrency
  cap distorting a cross-sectional test.
- **Read-only everywhere:** Yahoo/IBKR are data reads only; the LivePoller and all
  scripts place no orders; Theta never touched.

---

## Session 2026-06-02 — Real-data integration (feat/real-data-path)

First honest backtest on REAL intraday data, and the corrected real-data wiring.
Paper-only. Theta NOT touched this session (operator uses it concurrently).

### Real-data tier correction (the central audit)

The prior real-Theta adapter assumed Theta served intraday stock/index. **It does
not** at the operator's tiers. Corrected reality (now encoded in code + docs):

- **Theta OPTIONS = STANDARD** → intraday option tape + IV + 1st greeks (the only
  real Theta data here). **Theta STOCK = FREE/EOD-only; Theta INDEX = no access.**
- **Underlying** (SPY/QQQ stock; SPX/VIX index) comes from **IBKR** (recent/live,
  reads only) or **put-call parity** (deep history); free-daily for context.

`ThetaDataProvider.get_bars` now raises a *structural* `DataUnavailable` (wrong
source), not a tier error; its option methods are the real STANDARD path but stay
disconnected (guard → `NotImplementedError`; never opens a socket). Tests updated.

### What shipped

- `intraday/data/ibkr.py` — `IBKRDataProvider` over a read-only `IBKRClient`
  protocol (operator wires `ib_insync`; dev uses the IBKR MCP) + pure
  `payload_to_frame`/`bars_by_day` mappers + `ingest_payload`. Maps IBKR's
  **START-labelled** bars onto the engine's canonical **CLOSE** grid, ffill +
  coverage-counted, sparse/half-day sessions skipped. Contract registry resolved
  via IBKR search (SPY 756733/ARCA, QQQ 320227571/NASDAQ, SPX 416904/CBOE,
  VIX 13455763/CBOE). Underlying only — option methods raise.
- `intraday/data/store_provider.py` — `StoreBackedProvider`: offline, network-free
  replay from the parquet store; `trading_days` from partitions (multi-symbol
  intersection); refuses to serve a frame under a source it wasn't written with.
- `intraday/data/parity.py` — `ParityUnderlyingProvider` + parity math
  (`F = K + e^{rT}(C−P)`, `S = F·e^{−(r−q)T}`); proven by tests (recover a known
  spot path). The deep-history underlying path for when Theta options exist.
- `intraday/data/fused.py` — `FusedDataProvider`: underlying fallbacks
  (IBKR → parity → free-daily) + Theta options; each frame keeps its own source.
- `DataSource` += IBKR/PARITY/FREE_DAILY/FUSED (+ `is_real`); `metrics.render`
  prints `[REAL DATA: <src>]` vs the synthetic banner; CLI `--provider
  {synthetic,ibkr-store,theta-store}` + `--store-root`.
- `scripts/ingest_ibkr_underlying.py` (raw IBKR JSON → store) and
  `scripts/pull_theta_options_scoped.py` (scoped Theta PLANNER, safety-gated, never
  pulls here). Docs: `docs/REAL_DATA.md`, `docs/OPERATOR_RUNBOOK.md`.
- Tests: +`test_data_ibkr`, `test_data_parity`, `test_data_store_provider`,
  `test_data_fused`, `test_integration_real_data` (end-to-end real-path, PIT
  invariant on remapped bars), rewrote `test_theta_adapter`, +`test_scripts_theta_plan`.
  Full suite **312 passed**.

### Data reality found: IBKR intraday history is shallow

IBKR's data endpoint caps at **1000 bars/request, back-from-now** (no start date).
`period=ONE_YEAR` for 5-min returned only ~1 week. Practical max real 5-min history
= 1000 bars ≈ **~13 recent sessions**. Deep 2022→today intraday genuinely requires
the Theta options pull + parity (operator backfill) — exactly as the brief
anticipated. So this session's real test is necessarily **small-sample**.

### Headline real-data result (S3 control — REAL, but UNDERPOWERED, not an edge)

Ingested 12 real sessions (2026-05-14..06-01) of SPY+QQQ 5-min via IBKR. S3
(VWAP-reversion, `edge=0.10`), net of costs:

- **65 trades, NET +$306.88 (+0.31%)**, net Sharpe 2.84, win 46.2%, payoff 1.48,
  profit factor 1.27, cost 2.95 bps of notional (same model as synthetic).
- Controls: **`edge=0.0` → 0 trades** (the gate refuses everything with no edge
  claim — positive PnL only appears once an edge is assumed that lets trades pass).
  Balanced across symbols (SPY +$147 / QQQ +$160) and halves (+$154 / +$153).
- **Statistical honesty: daily-PnL t-stat = 0.80 (n=12) — NOT significant.** Best
  day +$182 is ~60% of the total. This is consistent with BOTH a real short-horizon
  mean-reversion tendency AND with noise. **No edge is claimed.** A powered,
  out-of-sample verdict needs the Theta+parity deep-history backfill.

### Decisions + rationale

- **Pull once → store → replay**: the bulky IBKR fetch (isolated to a background
  agent / saved tool-result files to keep context lean) is separated from the
  deterministic, network-free `StoreBackedProvider` replay the backtest runs on.
- **Grid remap over the synthetic-style ffill**: IBKR start→close (+interval) then
  reindex to `bar_close_index`; missing bars ffilled and **counted**; sessions
  below `--min-coverage` (half-days/sparse) skipped, never padded with a fake
  afternoon. Keeps the feed-gap guard and multi-symbol alignment intact.
- **Provenance is enforced, not assumed** — `StoreBackedProvider` won't relabel.
- **Theta stays untouched**: providers/scripts are delivered + proven by tests; the
  live pull is the operator's gated step. S1/S2 (need real options) remain pending
  that backfill — Phase 2 stays on hold per the brief.

---

## Session 2026-06-02 — Phase 1 begins (build/phase-1)

Phase 0 is green and merged to `main`, so Phase 1 started (DESIGN §5, behind the
same gate). All still SYNTHETIC, paper-only.

### T1.1 S1 gamma-regime (shipped)

- `intraday/signals/s1_gamma_regime.py` — the spine. Two regime-conditioned modes:
  **fade** toward VWAP in long-gamma when OFI is exhausting; **ride** a gamma-flip
  break toward the nearest wall in short-gamma when OFI confirms; stand aside
  near-flip/neutral. Trades the underlying (SPY/QQQ). Honest win_prob = fair
  baseline + edge (same discipline as S3).
- **Engine extension**: `IntradayBacktester` now computes option features
  (GEX/flip/walls + OFI, and RV/VRP for S2) per tick *only when a strategy needs
  them* (`needs_options`/`needs_rv` flags) — the S3-only path is unchanged and
  fast. The expensive dealer-GEX solve is recomputed on a slow cadence
  (`DataConfig.gex_recompute_min`, default 30 min; the regime is slow-moving),
  PIT-stamped by snapshot `available_ts`.
- **Synthetic realism**: added a slowly-varying dealer-gamma skew
  (`DataConfig.chain_gamma_skew`) so the GEX regime actually swings between
  long/short gamma instead of sitting on the flip (the chain was symmetric before,
  so S1 stood aside on every tick). A realism choice, set independently of S1 PnL.
- Result (1 synthetic week, SPY/QQQ): S1 trades (42 trades; gate blocked ~35% of
  signals) and **loses to costs on random-walk data** — honest: the synthetic GEX
  regime does not predict the random-walk path, so the gamma thesis is falsified
  and the loss is ≈ costs, exactly like S3. S1's value here is proving the
  gamma-spine plumbing end-to-end, not alpha on synthetic data.
- Tests: `tests/test_s1.py` (geometry, both modes, stand-aside, fair-baseline) +
  `tests/test_phase1.py` (end-to-end gated). Suite: 248 tests.

### T1.2 S2 0DTE VRP — defined-risk (shipped)

- `intraday/signals/s2_zerodte_vrp.py` — a **defined-risk short iron condor** sold
  when VRP is rich (IV > realized) AND net GEX is positive (vol-suppressing). The
  structure is priced from the chain's ATM IV via SWE BSM (credit + capped max
  loss); **no naked short gamma** (wings cap the loss); event days vetoed by the
  event-lockout reviewer.
- **Honest win_prob** (same discipline): `p_fair = max_loss/(credit+max_loss)`
  (gross EV exactly 0 under fair pricing) **plus** the genuine VRP edge (realized
  vs implied P(inside short strikes)). At zero VRP the gate refuses it.
- **Costs not understated**: an explicit 4-leg round-trip `cost_override` (≈$13.75
  per SPY spread). Generalized `SignalProposal`/gate/sizing to carry direct
  credit/max-loss economics (backward-compatible — directional S1/S3 unchanged).
- **Engine**: structured positions settle binary at the 0DTE close (keep credit
  inside short strikes, else the capped max loss — conservatively overstating loss
  in the short-to-long zone). One condor per symbol/day (held to close).
- **Honest retail constraint surfaced**: a wide SPX condor's max loss exceeds the
  1% per-trade risk budget on a $100k account → S2 correctly places NO SPX trade
  (DESIGN §1 "retail constraints are real"). Demonstrated on SPY: 5-7 defined-risk
  trades/2wk; net negative on random-walk synthetic (the synthetic VRP signal does
  not predict the path's realized vol → thesis falsified, like S1/S3 — honest).
- **Synthetic realism**: ATM IV anchored to the realized-vol scale with a vol-risk
  premium COUPLED to the gamma skew (positive gamma ⇒ rich VRP — a real market
  linkage), so S2's entry condition can actually occur. Set independently of PnL.
- Tests: `tests/test_s2.py` + S2 cases in `tests/test_phase1.py`.

**Adversarial review of S2 (4-lens) — addressed.** Found one HIGH and fixed it:
`win_prob` was `p_fair + edge`, but `p_fair` (the credit/max-loss geometry
break-even) is NOT the implied P(inside short strikes), so adding the edge
*inflated* win_prob and EV (gate certified +$40 where the honest realized-model EV
was −$46). Fixed: **`win_prob = p_real_inside`** — the realized-vol P(close inside
the short strikes), the honest probability for the binary settlement. This is
conservative (the binary overstates loss vs a real condor), so the gate now blocks
unless the VRP edge is genuinely large (it blocked 106/112 signals on synthetic).
Also fixed (LOW): eod-safety loop now settles a structured leftover correctly;
removed a redundant net computation. Known forward-compat note: the structured
entry/settlement path currently assumes the condor (center ± short_width) shape —
a future structure type would extend it.

### T1.3 paper ledger (shipped)

- `intraday/execution/records.py` — canonical fill/trade/signal serializers used by
  BOTH the backtest and the paper ledger, guaranteeing identical record shapes.
- `intraday/execution/paper_ledger.py` — `PaperLedger` records gated signals "as if
  filled" + resulting fills/trades/equity; `from_backtest` proves parity; `persist`
  writes to the `signals/` + `paper_ledger/` store partitions (by day). PAPER ONLY,
  no broker. A live streaming loop would feed the same `log_*` hooks tick-by-tick
  (out of scope until real data + a go-live decision).
- Tests: `tests/test_paper_ledger.py`.

**Phase 1 complete (S1 + S2 + paper ledger), all behind the one expectancy gate.**

### Next: harden / Phase 2 (equities) when desired; await operator decisions
(Theta tier, paper NAV/PDT, acceptance thresholds).

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
- **P0 tier probe.** Recorded in `intraday/data/README.md` and `docs/THETA_TIER_PROBE.md`:
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

S3's win probability is the gambler's-ruin **fair baseline** (`risk/(reward+risk)`,
0 net-EV) plus an explicit, falsifiable `edge` thesis:
- `--edge 0.0` (no edge claim): the gate **refuses all 244 candidate signals → 0
  trades, $0 PnL.** The single cleanest validation of the net-of-cost gate.
- `--edge 0.10` (default thesis): 244 trades, gross ≈ −$475 (≈0 vs ~$5M turnover),
  net **−$1,926 (−1.9% NAV)** — the thesis is false on a random walk, so the loss is
  essentially all transaction costs (~3.0 bps notional / ~$6 per round-trip).

This validates the harness four ways: (1) **no look-ahead** — gross ≈ 0 on a
driftless walk; (2) **the gate works** — a no-edge control is fully blocked;
(3) **costs dominate** and are correctly subtracted; (4) **no fabricated edge** — an
integration test confirms the engine captures gross edge only when the synthetic
world is genuinely mean-reverting.

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

### Adversarial review (6-lens) — addressed

Ran a 6-lens adversarial review (look-ahead, costs, gate, accounting, honesty,
completeness). **No critical issues; guardrails hold.** Fixed: win_prob → fair
baseline + explicit edge (was a flat 0.5 that rubber-stamped the gate); freshness
feed-gap HALT wired into the backtest (was stubbed); tape/chain provenance now
round-trips (can't relabel real data as synthetic); open_interest threaded through
fills; commission now config-driven (fixed dead config + stock-unit mis-scale);
eod safety exit-cost; synthetic snapshot spot; defensive bar-index alignment guard.
Documented (non-blocking): kill-switch is realized-PnL based (open-MTM is Phase 1),
gate EV is a pre-fill estimate, turnover/cost-bps are entry-side. Test count: 234.

### Next step

Phase 0 is green. Next: Phase 1 S1 (gamma-regime) and S2 (0DTE VRP, defined-risk
only), both behind the same gate, reusing the GEX/VRP features already built and
tested. Then a paper ledger (`execution/`) matching the backtest record shape.
Phase-1 also: add open_interest to the proposal (so options price OI in both gate
and fills), and an open-MTM daily kill-switch in the live/paper path.

---

## Session 2026-06-03 — launch-readiness pass (v0.1.0; PRs #9–#14)

A six-PR pass to make the product launch-ready (paper-only research; the **NO-EDGE
headline is unchanged** — this improved the product *around* the result, never
manufactured an edge). Driven by a 12-dimension multi-agent audit (2 critical /
5 high / 17 medium / rest low-nit); **every PR's diff was adversarially re-reviewed**
(findings verified-or-refuted), and a final holistic QA pass over merged `main`
confirmed guardrails + artifacts clean. Tests **419 → 487**; added ruff + mypy CI gates.

- **#9 backend robustness** — `read_bars` refuses a missing provenance sidecar;
  `assert_finite_bars` (empty/NaN/inf) wired into the engine; bar-grid check compares
  timestamps not just counts; `EngineConfig` `__post_init__` validators; S3/S4/S5 use
  S1's explicit strictly-positive geometry guard; SVG non-finite-colour safety;
  named `DEFLATED_SHARPE_SIGNIFICANCE_THRESHOLD`. (+29 tests)
- **#10 CLI/dev UX** — strategy registry (single source of truth); `version`,
  `strategies`, `doctor` (env health; never probes Theta/network); Windows `--help`
  Unicode→ASCII fix. (+20 tests)
- **#11 report aesthetics** — Sortino/Calmar KPIs; colour-blind ▲/▼ chevrons; rolling
  Sharpe; per-symbol breakdown; `@media print`; `scripts/gen_samples.py`. (+7 tests)
- **#12 repo/packaging/CI/docs** — pyproject metadata + PEP 639 license + console
  script + `[dev]` extra; ruff + mypy CI-gated (pinned); CI lint job + 3.11/3.12
  matrix; LICENSE/CONTRIBUTING/CHANGELOG/CODEOWNERS/pre-commit; doc-drift fixes.
- **#13 test hardening** — kill-switch latch, feed-gap boundary, degenerate eval,
  zero-trade metrics, SVG extremes, JSON determinism, poller-unavailable, multi-day
  ledger. (+11 tests)
- **#14 S2 honesty guard** — stand aside when the realized-vol estimate is degenerate
  (`sigma_real <= 0`) rather than emit an over-confident trade; + DESIGN S4/S5 note;
  + backtest cost-assumption help note. (+1 test)

**LICENSE** is a deliberately reversible **proprietary** default — swap for MIT/Apache
to open-source (see `LICENSE` / `CHANGELOG.md`).

---

## Session 2026-06-03 — launch-readiness round 2 (PRs #16–#21)

A second audit→fix→adversarial-review pass after a fresh 10-dimension audit of the
already-launch-ready engine. The audit found one outright shipping defect plus a
cluster of honesty/polish gaps; **the NO-EDGE headline is unchanged**. Tests
**487 → 517**.

- **#16 packaging (the real bug)** — `[tool.setuptools] packages = ["intraday"]`
  shipped only the 10 top-level modules and DROPPED every subpackage, so a clean
  `pip install`'d `intraday` console script crashed on the first import (verified by
  building the wheel: 10 files / 0 subpackages → 63 files / 10 subpackages after the
  fix). Switched to auto-discovery; added a network-free CI `package` job that builds
  the wheel, installs it in a throwaway venv, and smoke-imports every subpackage +
  the entry point from outside the source tree. `.gitignore` build/ dist/.
- **#17 CLI honesty/robustness** — a zero-trading-day run (reversed dates / un-ingested
  store) used to print "NO demonstrated edge" and exit 0; now `backtest`/`report`/
  `compare` warn on stderr, exit 2, and suppress the verdict (report/compare write no
  misleading file). ASCII fix for the synthetic banner em-dashes (cp1252 mojibake);
  `doctor` checks numpy/pandas/scipy/pyarrow; duplicate `--strategy` keys de-duped.
- **#18 report/eval honesty** — INSUFFICIENT-DATA verdict (n_days<2) distinct from
  NO-EDGE; small-`n_trials` caveat on a green EDGE; i.i.d. serial-dependence
  disclosure (scorecard / comparison / `stats` docstrings / `eval_real_universe`),
  qualified as short-range; shared `_chart_money` (minus before the `$`) so charts and
  KPIs format negatives identically. No computation changed.
- **#19 parity coverage gate** — `spot_to_bars` now measures grid coverage by real
  reconstructed quotes before ffill and refuses below `min_coverage` (0.8, matching
  `_remap`), so a heavily-ffilled parity session is never mislabelled solid
  `[REAL DATA: parity]`.
- **#20 test hardening** — `tests/test_stops.py` (the exported `sigma_stop_target` had
  0% behavioural coverage); OOS split-fraction bounds + rolling walk-forward; explicit
  `var_sr` DSR monotonicity; live `PaperLedger` `log_*` hooks tick-by-tick.
- **#21 docs accuracy** — this entry; test count 487→517 across README/TASKS/
  ARCHITECTURE/FINAL_REPORT; `THETA_TIER_PROBE.md` superseded banner + corrected
  adapter behaviour (`DataUnavailable`/`ThetaNotConnectedThisSession`, not a blanket
  `TierUnavailable`); PROGRESS S4/S5/OOS figures aligned to the committed eval JSON;
  `intraday/data/README.md` path fix.

Process note: each substantive PR's diff was adversarially re-reviewed (a 3-agent
skeptical panel verified PR #17/#18 — guardrails confirmed intact, two wording nits
fixed before merge). One operational lesson: a background review agent running a
working-tree git command transiently clobbered uncommitted work — commit before
launching background workflows that touch the shared tree.

### Next step

Unchanged: S1/S2 still await the Theta options backfill (operator, offline) before a
real-data verdict; Phase 2 (equities) and acceptance thresholds remain operator
decisions. The reversible proprietary LICENSE is the one open product decision.
