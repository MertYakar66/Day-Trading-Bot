# Intraday Engine — Design

> **Status:** Phase 0 + Phase 1 built, green, and launch-ready (see `CHANGELOG.md` /
> `PROGRESS.md`). This document is the spec the build followed; the implementation lives
> in the `intraday/` package (`docs/ARCHITECTURE.md` maps it).
>
> **What this is:** a *separate* intraday ("day-trading") decision engine. It is
> its own product, distinct from the Smart Wheel Engine (SWE), and reuses SWE's
> quant math as a **dependency** (see `README.md` → Dependencies) while sharing
> none of SWE's decision path, data cadence, or scope.

---

## 0. Relationship to the wheel engine

The wheel engine is an **end-of-day** expected-value ranker for 30–45 DTE
cash-secured puts → covered calls. This project is its opposite number on the
time axis: **minutes-to-hours** decisions on liquid index/options/equity
underlyings. They share *math*, not *decisions*.

**What we reuse (as a read-only dependency — see `README.md` → Dependencies):**
see §3.

**What we copy as a pattern, not as code:** the wheel engine's single hardest
rule — *no tradeable candidate bypasses the one authority, and reviewers can
only downgrade, never rescue* (`CLAUDE.md` §2). We rebuild that discipline for
intraday in §6. It is the most valuable idea in the parent repo and the reason
the wheel engine is trustworthy.

**What we must NOT do:**

- Never modify the wheel engine, its decision-layer trio
  (`ev_engine` / `wheel_runner` / `candidate_dossier`), or its tests. We are a
  consumer of its libraries only.
- Never route a signal to a live order in the early phases. **Paper-first** is a
  hard gate (the operator chose "signal + paper only").
- Never build latency-arbitrage / sub-second strategies. We will lose that race
  to colocated firms; our edge is statistical, not speed (§1).

---

## 1. Scope & non-goals

### In scope (decided with the operator)

| Dimension | Decision |
|---|---|
| **Data tier** | Theta **STANDARD** — real-time tick options (+ IV, 1st-order Greeks), 1-min/tick stock, 1-second SPX/VIX. |
| **Instruments (Phase 1)** | SPX index + **SPX / SPY / QQQ options** (the "gamma spine"). |
| **Instruments (Phase 2)** | ~20 liquid single-name equities + sector ETFs. |
| **Futures (ES/NQ)** | **Deferred.** Theta carries no CME futures at any tier. Use SPX/SPY/QQQ as the S&P/Nasdaq proxy now; revisit a dedicated feed (Databento / IBKR) only in Phase 3 if the proxy proves insufficient. |
| **Horizon** | **Intraday swing** — minutes to hours, a handful of trades per session. |
| **Execution** | **Signal + paper only** to start: alerts + a paper-trading ledger. No broker, no live orders, until an edge is proven on paper. |

### Out of scope / non-goals

- High-frequency / latency-sensitive trading.
- Real CME futures (until Phase 3, and only with a new feed).
- A broker/OMS/order-routing surface (until paper validation passes).
- Any non-liquid underlying where the bid/ask eats the edge.

### Honest reality check (the operator is new to day trading)

This is recorded here so it survives in the design, not just in chat:

1. **"Win the trades" is the wrong target.** No system wins every trade. The
   goal is **positive expectancy after costs, with strict risk control.** Even
   good intraday edges are thin and decay over time.
2. **Costs dominate at frequency.** Spread + slippage + fees can erase a
   nominal edge. Every signal is gated on *net* expectancy (§6), using the
   parent repo's `transaction_costs.py`.
3. **Retail constraints are real.** US Pattern-Day-Trader rule requires ≥ $25k
   equity for >3 day-trades/week in a margin account. Size and cadence must
   respect this.
4. **Paper-first is not optional.** If an edge does not survive on paper with a
   realistic fill model, it will not survive live. Paper is the cheap experiment
   that prevents the expensive one.

---

## 2. Data architecture

### 2.1 Provider & endpoints (Theta STANDARD)

All live data comes from Theta Terminal (`http://127.0.0.1:25503`) on the
**laptop**. A cloud sandbox has no Terminal (and no data-science deps) — all data
pulls, streaming, and backtests run laptop-side. The connector to reuse is SWE's
`engine/theta_connector.py` (tier-aware, chunked history, semaphore-bounded
concurrency).

| Need | Theta endpoint(s) | Notes |
|---|---|---|
| SPX/VIX intraday | index price / OHLC (CGIF) | 1-second venue-lowest, real-time, from 2022 |
| SPY/QQQ intraday | stock OHLC + Trade-Quote | 1-min bars + tick prints + NBBO |
| Option chain snapshots | option quote / OHLC / OI / IV / greeks (1st) | for GEX / surface / VRP |
| Option tape | option trade + trade-quote | **`scripts/pull_theta_option_tape.py` already exists** — trade-by-trade prints with NBBO and buy/sell-initiated classification (`side_inferred`) |

> **Confirm the tier before building.** `python scripts/probe_theta_capabilities.py`
> on the laptop writes `data_processed/theta_capabilities.json` — the ground
> truth for which endpoints are unlocked. Re-run after any plan change.

### 2.2 What to pull/stream in Phase 1 (scoped tight)

Tick option tape is **hundreds of GB** at full universe × full chain. Phase 1
deliberately stays tractable:

- **Symbols:** SPX, SPY, QQQ only.
- **Strikes:** ATM ± 10 (the `--atm-only` / windowed mode of the tape puller).
- **Expiries:** near-dated, including **0DTE** for SPX/SPY/QQQ.
- **Bars:** 1-second SPX index; 1-minute SPY/QQQ; option quotes at 1-minute,
  option trades at tick.
- **Session:** RTH 09:30–16:00 ET first; add SPX GTH/ETH later if a strategy needs it.

### 2.3 Storage schema

Reuse SWE's parquet feature-store conventions (`data/feature_store.py`,
partitioned by `ticker=/date=`). Layout (gitignored — regenerable, large):

```
data_store/
  bars/        ticker=<SYM>/date=<YYYY-MM-DD>/bars_<interval>.parquet
  option_tape/ ticker=<SYM>/date=<YYYY-MM-DD>/{trades,quotes}.parquet
  features/    ticker=<SYM>/date=<YYYY-MM-DD>/<feature_group>.parquet
  signals/     date=<YYYY-MM-DD>/signals.parquet      # what fired, with full context
  paper_ledger/date=<YYYY-MM-DD>/fills.parquet        # paper "executions"
```

- **Retention:** raw tape for the rolling research window (e.g. 1 year for
  SPX/SPY/QQQ ATM-window is tens of GB, not hundreds); derived features kept
  longer. Tune once measured.
- **Point-in-time discipline:** every feature row is stamped with the timestamp
  at which it was *computable* from the live feed — no look-ahead. This mirrors
  the parent repo's PIT rule (`DATA_POLICY.md` §4) and is the single most
  common way intraday backtests lie to you.

### 2.4 Freshness

Unlike the wheel engine (which tolerates day-stale EOD data), this engine is
worthless on stale data. The runtime must verify the Theta stream is live and
within latency budget before emitting any signal, and halt (not guess) if the
feed gaps.

---

## 3. Reused components (harvest map)

These parent-repo modules are strategy-agnostic and imported as a library. We do
not fork them; if one needs a change, we propose it upstream on its own branch.

| Module | Intraday use |
|---|---|
| `engine/theta_connector.py` | Live data pulls + streaming, tier-aware |
| `scripts/pull_theta_option_tape.py` | Intraday option trades + NBBO + side-inference |
| `engine/option_pricer.py` | BSM, Greeks (1st–3rd), IV solver |
| `engine/dealer_positioning.py` | **GEX / put-wall / gamma-flip → `MarketStructure`** (the spine) |
| `engine/volatility_surface.py` | SVI intraday surface |
| `engine/skew_dynamics.py` | Nelson-Siegel intraday skew/term-structure shifts |
| `engine/realized_vol.py` | Yang-Zhang / Garman-Klass RV on intraday bars |
| `engine/regime_detector.py`, `engine/regime_hmm.py` | Regime gating |
| `engine/transaction_costs.py` | **Net-of-cost expectancy gate** (commissions, slippage, sqrt-impact) |
| `engine/risk_manager.py` | Fractional-Kelly sizing, portfolio Greeks |
| `engine/event_calendar.py`, `engine/event_gate.py` | Earnings/FOMC lockout windows |
| `engine/performance_metrics.py` | Sharpe / Sortino / drawdown / hit-rate |
| `backtests/simulator.py` | Base to extend into an intraday event-driven sim |
| `news_pipeline/` (impact scorer) | Catalyst tagging (later phase) |
| `data/feature_store.py`, `feature_pipeline.py` | Parquet store + provenance |

---

## 4. Feature layer

All features are recomputed on intraday data; the math already exists upstream.

- **Dealer GEX & gamma-flip** (`dealer_positioning.py`): net dealer gamma from
  the live chain (Γ × OI × 100 × S² × 1%, signed by dealer convention),
  summed across strikes; the **gamma-flip** spot is where net GEX crosses zero,
  and **put/call walls** are the largest-gamma strikes. *Positive net GEX →
  dealers long gamma → they fade moves → vol-suppressing, mean-reverting tape.
  Negative net GEX → dealers short gamma → they chase moves → vol-amplifying,
  trending tape.* This regime sign is the master switch for §5.
- **Order-flow imbalance (OFI):** from the tape's `side_inferred`,
  `OFI = (buy_vol − sell_vol) / (buy_vol + sell_vol)` over a rolling window.
- **Volatility risk premium (VRP):** ATM IV (chain) − intraday realized vol
  (Yang-Zhang on 1–5 min bars), per horizon.
- **Skew / term-structure dislocation:** deviation of current skew/term slope
  from its intraday trailing mean (`skew_dynamics.py`).
- **VWAP & bands:** session VWAP and ±k·σ_intraday envelopes.
- **Opening range:** first 15–30 min high/low + volume profile.

---

## 5. Strategy specs (Phase 1)

Three thin, independently-testable strategies. Each must pass the §6 gate before
it can emit a paper trade. Each is chosen to be **latency-tolerant** (decisions
on minute-to-hours timescales, not microseconds).

### S1 — Gamma-regime (SPX/SPY) — *the spine*

- **Thesis:** the dealer gamma regime conditions the character of the tape.
- **Signal:** sign and magnitude of net GEX; distance of spot from the
  gamma-flip and from the nearest wall.
- **Entry (positive-gamma):** fade extensions toward the gamma-pin / wall when
  price stretches and OFI is exhausting.
- **Entry (negative-gamma):** ride accelerations away from the flip when a level
  breaks on rising OFI.
- **Exit:** target = pin/wall (positive) or next level (negative); stop on
  regime flip or fixed intraday sigma; hard time-stop into the close.
- **Why it fits us:** reuses the most existing code; the edge is structural, not
  speed-based.
- **Failure modes:** OI/GEX is stale intraday unless refreshed; 0DTE flow can
  swamp the static OI picture — refresh the chain and weight near-dated gamma.

### S2 — 0DTE volatility relative-value (SPX)

- **Thesis:** 0DTE IV is frequently mispriced vs realized intraday vol,
  conditional on regime.
- **Signal:** VRP (ATM IV − intraday RV) and skew dislocation, gated by GEX sign.
- **Entry:** when VRP is richly positive *and* gamma is positive (vol-suppressing
  regime), lean short premium via a defined-risk structure; when VRP is negative
  *and* gamma negative, lean the other way. Defined-risk only (no naked short
  gamma in a paper-to-live path).
- **Exit:** VRP mean-reversion target, IV-crush capture, or time-stop.
- **Failure modes:** event days (FOMC/CPI) break the VRP relationship — the
  event-lockout reviewer (§6) must veto these.

### S3 — VWAP-reversion / opening-range — *benchmark/control*

- **Thesis:** liquid names mean-revert to VWAP intraday outside of trends.
- **Signal:** deviation from VWAP in intraday-sigma units; opening-range breaks.
- **Purpose:** a simple, well-understood control to validate the harness,
  cost model, and metrics before trusting the exotic signals. If S3 doesn't
  behave sanely in backtest, the harness is wrong, not the market.

---

## 6. The discipline layer (mirror of the EV authority)

Every candidate signal flows through one gate; reviewers can only **downgrade**.

1. **Expectancy gate (the authority).** A signal is tradeable only if
   `E[net PnL] = p·win − (1−p)·loss − costs > threshold`, where `costs` come
   from `transaction_costs.py` (spread + slippage + sqrt-impact + fees) on the
   actual contemplated size. Negative or non-finite expectancy → **blocked**.
   This is the intraday analogue of `EVEngine.evaluate`; nothing reaches the
   paper ledger without it.
2. **Downgrade-only reviewers** (each can demote proceed → review → skip →
   blocked, never the reverse):
   - **Event lockout** (`event_gate.py`): inside an earnings/FOMC/CPI window → skip.
   - **Regime filter** (`regime_*`): strategy disabled in a hostile regime.
   - **Liquidity gate** (`data/quality.py` analogue): spread too wide / depth too
     thin for the size → skip.
   - **Daily kill-switch** (`risk_manager.py`): once the day's loss budget is hit,
     all further signals → blocked for the session.

---

## 7. Risk management

- **Sizing:** fractional Kelly (`risk_manager.py`), capped per-trade and
  per-underlying.
- **Per-trade stop:** fixed intraday-sigma or structural-level stop, set at entry.
- **Daily loss limit:** hard kill-switch (mirrors the wheel engine's
  `MAX_DAILY_LOSS` knob; default 3% of paper NAV).
- **Concurrency cap:** max simultaneous positions to bound correlated risk.
- **PDT awareness:** trade count and account-equity assumptions logged so the
  paper results map to a real-account constraint.

---

## 8. Paper execution & backtest harness

- **Backtest:** event-driven intraday simulator (extend `backtests/simulator.py`),
  replaying stored bars + tape in timestamp order with **no look-ahead**.
- **Fill model:** conservative — fills at next-bar / NBBO with modelled slippage,
  never at the touch you "saw." Costs from `transaction_costs.py`.
- **Paper ledger:** live mode logs each gated signal as if filled, then marks it
  to market on subsequent ticks; produces the same record shape as the backtest
  so live-vs-backtest divergence is measurable.
- **Metrics** (`performance_metrics.py`): net Sharpe/Sortino, max drawdown,
  hit-rate, payoff ratio, expectancy per trade, turnover, cost drag.
- **Acceptance criteria (a strategy "passes" paper) — proposed, to calibrate:**
  over a meaningful out-of-sample window (≥ ~60 trading days), **positive
  net-of-cost expectancy with statistical confidence**, drawdown within budget,
  and **live paper results consistent with the backtest** (no regime-fit
  illusion). Failing any of these keeps it in paper, not live.

---

## 9. Project layout

```
.                      # repo root
  README.md            # entry point + dependencies + guardrails
  DESIGN.md            # this file
  TASKS.md             # ordered build backlog
  data/                # Theta pull/stream adapters + parquet store helpers
  features/            # intraday feature builders (GEX, OFI, VRP, VWAP…)
  signals/             # S1 gamma, S2 0DTE-VRP, S3 VWAP/ORB
  authority/           # the expectancy gate + downgrade-only reviewers
  risk/                # sizing, stops, daily kill-switch
  execution/           # paper ledger now; broker adapter later (Alpaca/IBKR)
  backtest/            # event-driven intraday simulator
  tests/               # pytest suite
  data_store/          # gitignored parquet (bars, tape, features, ledger)
  vendor/swe/          # smart-wheel-engine dependency (git submodule)
```

> **Implementation note (Phase 0).** The code lives under a single **`intraday/`**
> package (`intraday/data/`, `intraday/features/`, …) rather than top-level
> directories, to avoid a `PYTHONPATH` namespace collision with SWE's own
> top-level `data/` and `backtests/` packages. The full module map is in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The event-driven backtester is
> **purpose-built** (`intraday/backtest/engine.py`) rather than a subclass of
> SWE's `backtests/simulator.py` — that one is a daily, wheel-specific placeholder
> coupled to `WheelTracker`; we reuse SWE's *quant* pieces (costs, pricer,
> metrics, dealer, RV) and discipline patterns instead, and never modify SWE.
> Rationale and the FREE-tier data reality are in [`PROGRESS.md`](PROGRESS.md).

---

## 10. Phased roadmap

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0 — Foundation** | Intraday puller/streamer → parquet store; event-driven backtest skeleton with realistic costs; metrics wired | Can replay one month of SPX/SPY/QQQ and produce a cost-correct equity curve for a trivial strategy |
| **1 — Gamma spine** | S1 + S2 + S3 behind the §6 gate; paper ledger live | All three pass the harness sanity check; S3 (control) behaves; paper ledger matches backtest shape |
| **2 — Equities** | ~20 single names: S3 + a single-name gamma/flow variant | Same gate, same acceptance bar, on equities |
| **3 — (optional) Real futures** | ES/NQ via Databento/IBKR feed | Only if the SPX/SPY/QQQ proxy is shown insufficient |

A strategy only graduates from paper to (a future) live phase by clearing §8's
acceptance criteria. Promotion to live is a separate, explicit decision with its
own broker-integration design — out of scope here.

---

## 11. Open questions / decisions still needed

1. **Confirm STANDARD tier empirically** (`probe_theta_capabilities.py`) before
   Phase 0 — especially that real-time intraday *stock* is unlocked, since the
   parent repo's docs were ambiguous (a wheel-scope guard vs. an actual limit).
2. **How to wire the SWE dependency** (`TASKS.md` T0.0) — git submodule vs
   editable install vs vendor. Recommend a pinned git submodule under
   `vendor/swe` so SWE stays read-only and reproducible.
3. **Paper NAV & PDT assumptions** — what account size should paper results
   model (drives sizing and the PDT constraint)?
4. **Acceptance thresholds (§8)** — calibrate the exact Sharpe/expectancy/DD
   bars once we see Phase-0 backtest noise levels.

---

## 12. References

All paths below are in the **smart-wheel-engine** repo (the SWE dependency),
not this one:

- Data layer: `docs/DATA_POLICY.md`, `docs/DATA_SPECIFICATION.md`
- Theta capabilities: `docs/THETA_USAGE.md` §2 (tiers), `docs/THETA_INSTRUCTIONS.md`
- Reusable modules: `MODULE_INDEX.md`
- Discipline pattern mirrored in §6: `CLAUDE.md` §2 (EV authority + downgrade-only reviewers)
- Existing intraday puller: `scripts/pull_theta_option_tape.py`
