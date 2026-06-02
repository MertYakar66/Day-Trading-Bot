# Phase 0 — Final Report

**Date:** 2026-06-02 · **Branch:** `build/phase-0` · **Status:** complete, all
tests green · **Data source: SYNTHETIC (deterministic). Not a real edge.**

## 1. What was delivered

A complete, paper-only, event-driven intraday engine (`intraday/`) that takes
SPX/SPY/QQQ data through **PIT features → strategy → fractional-Kelly sizing →
net-of-cost expectancy gate → downgrade-only reviewers → conservative fills →
metrics**, with the S3 VWAP-reversion control wired end-to-end. All Phase-0 tasks
(T0.0–T0.8) are done with tests; one documented command runs it:

```
python -m intraday backtest --start 2026-05-01 --end 2026-05-29
```

Test suite: **229 tests, all passing**, network-free and deterministic, including
no-look-ahead property tests, cost-model correctness, "gate refuses negative-EV",
"reviewers only downgrade", and a full Phase-0 integration test.

## 2. Results — NET OF COSTS (synthetic month, SPY/QQQ via S3)

The S3 control's win probability is the **gambler's-ruin fair baseline**
(`p_fair = risk/(reward+risk)`, exactly 0 net-EV) plus an explicit, falsifiable
`edge` (its mean-reversion thesis). Two regimes make the point:

| `--edge` | Trades | Gross | Net | Reading |
|---|---|---|---|---|
| **0.0** (no edge claim) | **0** | $0 | $0 | the gate **refuses every trade** — a no-edge control cannot beat costs |
| **0.10** (default thesis) | 244 | ≈ −$475 | −$1,926 (−1.9% NAV) | thesis is FALSE on a random walk → realized gross ≈ 0, net = costs |

Cost per round-trip ≈ **3.0 bps of notional / ~$6 per trade** (realistic for liquid
ETFs). Win rate ≈ 29% (near the ~1/3 gambler's-ruin value for fading a 2σ stretch
with a 1σ stop). Sharpe is negative — correct for a control with no real edge.

**These numbers are NOT an edge.** They are a *harness validation*. The synthetic
price process is random-walk dominant (realistic minute-bar autocorrelation ≈ 0).
The `edge=0` result is the strongest single check: the net-of-cost gate, fed an
honest no-edge probability, **blocks 100% of signals** — exactly its job. With an
explicit edge thesis the engine trades; on efficient data the thesis is falsified
and it loses to costs (the paper-first lesson), while on mean-reverting synthetic
data an integration test confirms it turns a gross profit.

## 3. What is PROVEN vs ASSUMED

**Proven (by tests + the run):**
- No look-ahead: gross ≈ 0 on a driftless random walk; truncating/appending future
  data never changes a decision; the backtest's fast feature read equals the
  general PIT read. (An integration test also shows the engine *captures* gross
  edge when the synthetic world is made mean-reverting — so it isn't blind, it's
  honest.)
- Costs are correctly modelled and dominant; slippage is not double-counted; the
  expectancy gate refuses negative/non-finite EV; reviewers can only downgrade.
- Accounting closes (final equity = NAV + Σ net PnL); positions are intraday-only;
  the daily kill-switch latches.
- The SWE dependency is reused read-only and unmodified.

**Assumed (NOT confirmed; must be set by a human — DESIGN §11):**
- Paper NAV ($100k), risk caps (1%/trade, 20% notional, 3 concurrent, 3% daily
  kill, half-Kelly), EV threshold (0), arrival latencies, cost parameters
  (SWE defaults + 5 bps fallback spread), and S3's neutral `win_prob` (0.50).
- All market data (this is the biggest one): **synthetic**, because the Theta tier
  is FREE and Theta was not used this session.

## 4. What a human must decide next

1. **Theta tier** — upgrade to STANDARD to unlock real intraday data (then the
   already-built `ThetaDataProvider` becomes the live path), or stay synthetic for
   harness development.
2. **Paper NAV & PDT** — the account size paper results should model.
3. **Acceptance thresholds** (DESIGN §8) — Sharpe / expectancy / drawdown bars,
   calibrated once real-data noise is visible.
4. **S3 calibration** — `win_prob`, `entry_z`, `stop_k` against realized hit-rate
   (only meaningful on real data).

## 5. Recommended next build step

Phase 1: S1 (gamma-regime) and S2 (0DTE VRP, **defined-risk only**), both behind
the same gate, reusing the GEX/VRP features already built and tested; then a paper
ledger mirroring the backtest record shape. Do **not** promote anything to live —
that is a separate, explicit decision with its own broker design (out of scope).

## 6. Adversarial review (addressed)

A 6-lens adversarial review (look-ahead, costs, gate, accounting, honesty,
completeness) found **no critical issues** and confirmed the guardrails hold. Items
fixed in this branch:
- **HIGH — win_prob defeated the gate**: replaced the flat 0.5 prior with the
  gambler's-ruin fair baseline + explicit `edge` (above).
- **HIGH — freshness/feed-gap HALT was stubbed**: wired `assert_no_feed_gap` into
  the backtest (halts on a gapped feed, DESIGN §2.4) + tests.
- **MED — tape/chain provenance not round-tripped**: store now persists + reads a
  source sidecar and refuses to guess (real data can't be relabelled synthetic).
- **MED — latent gate-vs-fill open_interest divergence**: threaded `open_interest`
  through the fill path (consistent for Phase-1 options).
- **LOW — dead/mis-scaled commission config, eod exit-cost omission, snapshot spot
  edge, kill-switch budget guard, EV pre-fill basis**: fixed or documented.

Remaining documented (non-blocking) limitations: the daily kill-switch trips on
realized PnL (open-MTM kill is a Phase-1 paper-ledger enhancement); the gate's EV
is a pre-fill estimate (unbiased on a driftless tape); turnover/cost-bps use
entry-side notional.

## 7. Honesty caveats (read before trusting anything here)

- Synthetic performance says nothing about a real edge; it validates plumbing.
- A real edge must be demonstrated out-of-sample on real data, net of costs, with
  statistical confidence, before any capital — paper or real — is committed.
- The cost model is conservative but its parameters are SWE defaults; real fills
  will differ and must be measured.
