# Final Report — Intraday Engine (Phase 0 + Phase 1 + Real-Data Path + Powered Eval)

**Date:** 2026-06-02 · **Status:** Phase 0 + Phase 1 complete; real-data path wired
(IBKR + Yahoo underlying); powered, multiple-testing-honest real-data evaluation
run. 356 tests passing. · **Verdict: on 24 symbols × 59 real 5-min sessions, net of
costs, NO underlying-only strategy (reversion/breakout/momentum) shows an edge —
all deflated-Sharpe ≈ 0, OOS fails. The engine reports the truth, not a fabricated
edge.** See §0.

## 0. Powered real-data evaluation (2026-06-02) — the honest verdict

Data sourced from **anywhere except Theta** (Theta actively in use). The unlock:
free **Yahoo** intraday (≈60 sessions of 5-min, with a browser UA) across a wide
universe — far deeper than IBKR's ~1000-bar (~13-session) cap. Ingested a
**24-symbol cross-section** (broad/sector ETFs + large-caps), **1,416 sessions**
(2026-03-09..06-01). **Data integrity:** IBKR (ARCA) vs Yahoo (consolidated) 5-min
closes agreed to **< 2 bps** (mean 0.12–0.20 bps) over 936 SPY/QQQ bars each.

Each strategy was run standalone per symbol (no concurrency-cap distortion), then
aggregated to an equal-weight portfolio and scored with the honesty harness
(`intraday.eval`): clustered t-stat (one obs per trading day), bootstrap-CI Sharpe,
and the **Deflated Sharpe Ratio** discounting all 72 strategy×symbol trials.

| strategy | portfolio Sharpe (ann) | 95% CI | day t (p) | **DSR / 72 trials** |
|---|---|---|---|---|
| S3 VWAP reversion | −2.83 | [−5.39, 0.14] | −1.37 (0.18) | **0.000** |
| S4 ORB breakout | −2.76 | [−6.85, 0.77] | −1.34 (0.19) | **0.000** |
| S5 VWAP momentum | −4.89 | [−9.94, −1.07] | −2.37 (0.02) | **0.000** |

**The best single trial of 72** (S3 on SMH) had an annualized Sharpe of **2.42** —
but t = 1.17 (insignificant) and, deflated for the 72-trial search, **DSR = 0.13**:
exactly the "winner" a search over 72 zero-edge strategies throws up by luck. This
is the harness earning its keep — it refuses to certify the best-looking fluke.

**Cost attribution (whole 24-symbol, $2.4M book):** S3 gross ≈ **−$348 (flat — no
predictive edge)** bled by **$12.4k** costs → net −$12.7k; S4/S5 are gross-*negative*
(−$5.7k/−$3.8k: breakouts fade, 5-min momentum reverts) plus costs. ~3.2k–3.9k
trades each.

**Out-of-sample:** picking the best strategy on the train half (S4, train Sharpe
−0.91) and testing on the held-out half gave Sharpe **−7.12** — the in-sample
"winner" fails OOS.

**Verdict: no demonstrated edge.** After multiple-testing deflation and OOS, none of
these simple underlying-only intraday strategies beats costs on real data — exactly
what efficient prices + transaction costs predict. A naive read would have noted
that an earlier **12-session** IBKR S3 run was net **+$306.88** (Sharpe 2.84); the
deeper 59-session test (t and DSR above) shows that was **noise**. This is the value
of the harness: it refuses to let a small-sample fluke masquerade as alpha.

**S1 / S2** (gamma-regime, 0DTE VRP) need real **option** data → still blocked on the
operator's scoped Theta pull ([`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)); built and
proven by tests, not run.

**What this validates (proven):** the real-data plumbing end-to-end — IBKR
start→close grid remap (feed-gap-clean, coverage-honest), provenance enforcement
(no relabeling), the SAME cost model on real data (2.95 bps), and no-look-ahead on
remapped real bars. It says nothing about edge.

## 1. What was delivered

A complete, paper-only, event-driven intraday engine (`intraday/`) that takes
SPX/SPY/QQQ data through **PIT features → strategy → fractional-Kelly sizing →
net-of-cost expectancy gate → downgrade-only reviewers → conservative fills →
metrics**, with three strategies behind the one gate and a paper ledger:

| Task | Deliverable |
|---|---|
| T0.0–T0.2 | SWE dependency wired; `DataProvider` interface; deterministic synthetic provider; real-Theta adapter (never connected); `ticker=/date=` parquet store |
| T0.3 | Feature builders: GEX/flip/walls, OFI, intraday RV, VRP, VWAP+bands, opening range — all causal + PIT |
| T0.4–T0.5 | Event-driven backtester (no look-ahead, conservative fills) + metrics (net Sharpe/Sortino/DD/expectancy/cost) |
| T0.6–T0.7 | Expectancy gate (sole authority) + downgrade-only reviewers (event/regime/liquidity/daily-kill) |
| T0.8 | S3 VWAP-reversion control |
| T1.1 | S1 gamma-regime (fade in long-gamma / ride in short-gamma, by GEX + OFI) |
| T1.2 | S2 0DTE VRP **defined-risk** iron condor (no naked short gamma) |
| T1.3 | Paper ledger with backtest record-shape parity |

**One documented command** (prints a NET-of-costs report):
`python -m intraday backtest --start 2026-05-01 --end 2026-05-29`
(`--strategy {s1,s2,s3}` selects strategies; default S3.)

## 2. Results — NET OF COSTS (synthetic; harness validation, NOT an edge)

**Integrated run (S1+S2+S3, SPX/SPY/QQQ, 2 synthetic weeks):** 71 trades (S3 42,
S1 20, S2 9); gate verdicts 71 PROCEED / 284 BLOCKED / 586 SKIP (the event-lockout
reviewer vetoing macro-day signals). Net **−2.97%** of NAV, cost ~2.8 bps of
notional. All three strategies lose to costs — the honest, expected outcome on
efficient (random-walk) synthetic prices.

**Per-strategy honesty checks:**
- **S3** at `--edge 0` (no edge claim): the gate **refuses 100% of signals** —
  the single cleanest proof the net-of-cost gate works. With the default edge it
  trades and loses to costs on a random walk (gross ≈ 0 → no look-ahead).
- **S1** trades (fade/ride by GEX regime) and loses to costs — the synthetic
  gamma regime doesn't predict the random-walk path (no fabricated edge).
- **S2** places **defined-risk** condors with losses correctly **capped**, and
  loses to costs (the synthetic VRP signal doesn't predict realized vol). On a
  $100k account at the 1% per-trade risk cap, a wide **SPX** condor's max loss
  exceeds the budget, so S2 correctly places **no SPX trade** (a real retail
  constraint, DESIGN §1); demonstrated on SPY.

These numbers validate plumbing, costs, and discipline — they say nothing about a
real edge, which can only be shown out-of-sample on real data.

## 3. What is PROVEN vs ASSUMED

**Proven (tests + runs):** no look-ahead (gross ≈ 0 on a driftless walk;
truncate/append invariance; positional==PIT equivalence; feed-gap HALT); the
expectancy gate is the sole authority and refuses negative/non-finite EV; reviewers
only downgrade (exhaustive); costs subtracted correctly and never double-counted /
understated (incl. 4-leg structures); accounting closes; positions intraday-only;
defined-risk losses capped; the SWE dependency is reused read-only and unmodified;
synthetic data is always labelled and can never be relabelled real on store
read-back.

**Assumed (NOT confirmed live-money params — DESIGN §11):** paper NAV ($100k); risk
caps (1%/trade, 20% notional, 3 concurrent, 3% daily kill, half-Kelly); EV
threshold (0); arrival latencies; cost params (SWE defaults + 5 bps fallback
spread; option commission $0.65/contract); each strategy's `edge`/structure
parameters; and — the big one — **all market data is synthetic** (Theta is FREE
tier; not used this session).

## 4. Adversarial review (two rounds, addressed)

Multi-lens reviews found **no critical issues**; guardrails hold. Fixed: the
win-probability honesty bug in S3/S1 (fair baseline + explicit edge) and in S2
(realized P-inside, not an inflated geometry baseline — the gate now blocks ~95% of
S2 signals); the feed-gap HALT wiring; tape/chain provenance round-trip; a slippage
double-count; commission config; eod-safety structured settlement; plus documented
minors (gate EV is a pre-fill estimate; kill-switch is realized-PnL based).

## 5. What a human must decide next

1. **Theta options backfill** — run the scoped Theta-STANDARD OPTIONS pull
   ([`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)) to unlock S1/S2 and the
   parity-reconstructed deep-history underlying. (Theta serves OPTIONS only —
   underlying is IBKR/parity; the providers are built and tested.)
2. **Paper NAV & PDT** — the account size paper results should model (drives
   sizing and which structures are affordable — see the SPX-S2 constraint).
3. **Acceptance thresholds** (DESIGN §8) — Sharpe/expectancy/DD bars, calibrated on
   real-data noise.
4. **Strategy calibration** — each strategy's `edge`, entry/stop, and S2 structure,
   against realized hit-rates (only meaningful on real data).

## 6. Recommended next steps (Phase 2+)

Single-name equities (T2.1) reuse the same gate/harness. Deferred hardening:
thread `open_interest` into option proposals (so the gate and fills price option
liquidity identically); an open-MTM daily kill-switch for the live path; a live
streaming loop feeding the paper ledger (needs real data + a go-live decision).
**Do not promote anything to live** without clearing DESIGN §8 acceptance criteria
out-of-sample on real data — a separate, explicit decision with its own broker
design (out of scope here).

## 7. Honesty caveats

Synthetic performance validates plumbing, not edge. A real edge must be
demonstrated out-of-sample on real data, net of costs, with statistical
confidence, before any capital — paper or real — is committed. The cost model is
conservative but its parameters are SWE defaults; real fills will differ and must
be measured.
