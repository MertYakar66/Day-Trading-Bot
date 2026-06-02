# Final Report — Intraday Engine (Phase 0 + Phase 1 + Real-Data Path)

**Date:** 2026-06-02 · **Status:** Phase 0 + Phase 1 complete; the real-data path is
now wired and run on a first REAL sample. 312 tests passing. · **Data: SYNTHETIC
validates the harness; a first REAL-data S3 backtest (IBKR underlying) was net-
positive on a tiny window but is STATISTICALLY INSIGNIFICANT — NOT a demonstrated
edge.** See §0.

## 0. Real-data validation (2026-06-02) — the first honest real-data run

The corrected data-tier reality was wired (options=Theta STANDARD;
underlying=IBKR/parity — Theta never serves the underlying; see
[`REAL_DATA.md`](REAL_DATA.md)) and the **first backtest on REAL intraday data** was
run. Theta was **not touched** (operator uses it concurrently); the underlying came
from **IBKR** (reads only).

**Data reality:** IBKR's data endpoint caps at **1000 bars/request, back-from-now**.
The practical maximum real 5-min history is ~**13 recent sessions** — deep
2022→today intraday genuinely needs the Theta options pull + put-call parity
(operator backfill, [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)). So this run is a
**small-sample plumbing validation**, by necessity.

**S3 (VWAP-reversion control) on 12 real SPY+QQQ 5-min sessions (2026-05-14..06-01),
net of costs:**

| metric | value |
|---|---|
| trades | 65 |
| NET PnL | **+$306.88 (+0.31% of $100k)** |
| net Sharpe / Sortino | 2.84 / 7.66 |
| win rate / payoff | 46.2% / 1.48 |
| cost | $376.88 (2.95 bps of notional — same model as synthetic) |
| **daily-PnL t-stat (n=12)** | **0.80 — NOT significant** |

**Verdict (honest): no edge is claimed.** The result is net-positive after costs and
internally consistent (balanced across SPY/QQQ and across both halves; the no-edge
control `edge=0.0` correctly produces **0 trades** — the gate refuses everything
absent an edge assumption). But **t = 0.80 over 12 sessions cannot reject "no
edge"** — it is equally consistent with a genuine short-horizon mean-reversion
tendency and with noise, and the best single day is ~60% of the total. A credible
go/no-go requires a powered, out-of-sample test on the deep-history backfill.

**S1 / S2:** require real **option** data → blocked on the operator's scoped Theta
pull. The path is built and proven by tests; not run this session.

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
