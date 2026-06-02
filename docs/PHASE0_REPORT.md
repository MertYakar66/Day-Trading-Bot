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

| Metric | Value | Reading |
|---|---|---|
| Gross PnL | ≈ **+$73** (over 301 trades, ~$6M turnover) | ≈ 0 — *no edge, as expected* |
| Total costs | **−$1,807** | the dominant term |
| **Net PnL** | **−$1,734 (−1.8% of NAV)** | a naive control loses to costs |
| Cost / round-trip | ~3 bps of notional, ~$6/trade | realistic for liquid ETFs |
| Win rate | ~33% | matches gambler's-ruin for fading a 2σ stretch w/ 1σ stop |
| Sharpe (net) | negative | unprofitable after costs (correct for a control) |

**These numbers are NOT an edge.** They are a *harness validation*. The synthetic
price process is random-walk dominant (realistic minute-bar autocorrelation ≈ 0),
so a naive VWAP-reversion control should — and does — produce ≈ 0 gross and a net
loss equal to transaction costs.

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

## 6. Honesty caveats (read before trusting anything here)

- Synthetic performance says nothing about a real edge; it validates plumbing.
- A real edge must be demonstrated out-of-sample on real data, net of costs, with
  statistical confidence, before any capital — paper or real — is committed.
- The cost model is conservative but its parameters are SWE defaults; real fills
  will differ and must be measured.
