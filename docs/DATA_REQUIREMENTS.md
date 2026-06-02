# Day-Trading-Bot — What This Project Does and What Data It Needs

**Status date:** 2026-06-02 · **Scope:** the intraday engine in this repo
(`intraday/`), paper-only. This document explains, in full, (1) what the project
is and how it makes decisions, (2) what data it consumes today and where, (3) the
honest result that data has produced so far, and (4) the complete, prioritized set
of data that would make it a solid day-trading bot — mapped to the exact place in
the code each input is read.

It is written to be read on its own. It assumes no prior knowledge of the
codebase. Companion documents: `DESIGN.md` (the spec), `docs/ARCHITECTURE.md` (the
code map), `docs/REAL_DATA.md` (the real-data wiring), `docs/FINAL_REPORT.md` (the
results).

---

## Part 1 — What this project is

### 1.1 The product

This is a **standalone intraday ("day-trading") decision engine**. It makes
decisions on a **minutes-to-hours** timescale on liquid index/options/equity
underlyings (the Phase-1 universe is SPX, SPY, QQQ). It is a *separate product*
from the **Smart Wheel Engine (SWE)** — which decides 30–45-day option wheels
end-of-day — but it **reuses SWE's quant math as a read-only dependency**
(`vendor/swe`): the option pricer, dealer-positioning (GEX) model, realized-vol
estimators, transaction-cost model, risk manager, event calendar, and performance
metrics. It shares none of SWE's decision path or data cadence.

### 1.2 Three hard rules that define the project

1. **Paper only.** There is no broker, no order-routing, no live-trade code
   anywhere in the repository, by design. `execution/` is a *paper ledger* that
   records what *would* have been filled. Going live is a separate, explicit
   future decision with its own broker integration — out of scope here.
2. **No look-ahead (point-in-time, "PIT").** Every data row and every computed
   feature carries the timestamp at which it *first became usable from a live
   feed*. A decision made at time `T` may only consume data whose
   `available_ts <= T`. This is enforced structurally (see §2.4) and proven by
   property tests that assert appending future data never changes a past decision.
3. **Net-of-cost expectancy gate is mandatory.** No signal becomes a (paper)
   trade unless its **expected dollar PnL, after transaction costs, is strictly
   positive**. This single gate is the only place a candidate becomes tradeable.

The governing philosophy is **honesty over hype**: the goal is not to "win
trades" but to demonstrate *positive expectancy after costs with statistical
confidence*. The engine is built to report "no edge" when that is the truth, and
it currently does (see §3).

### 1.3 The decision pipeline (end to end)

```
Data (PIT)  →  Features (causal)  →  Strategy  →  Position sizing
            →  EXPECTANCY GATE (the one authority)
            →  Downgrade-only reviewers  →  Conservative fills  →  Trades  →  Metrics
```

Stage by stage:

- **Data layer** (`intraday/data/`). A `DataProvider` abstract interface returns
  three PIT-stamped container types (defined in `intraday/contracts.py`):
  - `BarSeries` — OHLCV bars indexed by bar **close** timestamp, with an arrival
    latency so a bar closing at `t` is only usable at `t + latency`.
  - `OptionChainSeries` — time series of option-chain snapshots (one row per
    `snapshot_ts × expiration × strike × option_type`) carrying open interest,
    implied vol, and spot.
  - `OptionTape` — trade-by-trade option prints with NBBO and a buy/sell
    classification (`side_inferred`).
  Concrete providers: `SyntheticDataProvider` (deterministic test fixture),
  `IBKRDataProvider` and `YahooDataProvider` (real underlying bars, read-only),
  `ParityUnderlyingProvider` (underlying reconstructed from option quotes via
  put-call parity), `ThetaDataProvider` (real options, currently disconnected),
  `StoreBackedProvider` (offline replay from the parquet store), and
  `FusedDataProvider` (composes underlying fallbacks + options).

- **Feature layer** (`intraday/features/`). Causal, PIT-sampled builders turn raw
  data into a `FeatureRow` at each decision timestamp:
  - `vwap.py` → session VWAP, VWAP sigma, and standardized deviation
    `vwap_dev_sigma = (close − vwap) / sigma`.
  - `opening_range.py` → first-N-minute high/low/volume (known only after the
    window closes).
  - `realized_vol.py` → intraday realized volatility (Garman-Klass by default,
    rescaled from SWE's daily estimators by `sqrt(bars_per_day)`).
  - `vrp.py` → volatility risk premium = ATM implied vol − intraday realized vol.
  - `gex.py` → dealer gamma exposure, gamma-flip level, put/call walls, and the
    gamma **regime** (long-gamma = mean-reverting tape; short-gamma = trending),
    via SWE's `dealer_positioning`.
  - `ofi.py` → order-flow imbalance `(buy_vol − sell_vol)/(buy_vol + sell_vol)`
    from the option tape's `side_inferred`.
  Every value is sampled through `latest_value(...)`, which respects arrival
  latency, so look-ahead is structurally impossible.

- **Strategies** (`intraday/signals/`). Each turns a `FeatureRow` into at most one
  `SignalProposal` (or stands aside). A strategy never decides tradeability:
  - **S1 gamma-regime** — in positive-gamma (mean-reverting) regimes, *fades* a
    stretch from VWAP when order flow is exhausting; in negative-gamma (trending)
    regimes, *rides* a break of the gamma-flip toward the next wall when OFI
    confirms. Needs the option chain (GEX/flip/walls) and tape (OFI).
  - **S2 0DTE VRP** — when implied vol is rich vs realized AND net gamma is
    positive, sells a **defined-risk iron condor** (capped loss, no naked short
    gamma), priced from the chain via Black-Scholes. Needs chain + tape + RV.
  - **S3 VWAP-reversion** — the *control*: fades extreme deviations from VWAP back
    toward it. Underlying bars only.
  - **S4 ORB breakout** — momentum: trades breaks of the opening range. Underlying
    bars only.
  - **S5 VWAP momentum** — the mirror of S3: *rides* stretches from VWAP.
    Underlying bars only.
  Every strategy's win probability is the **gambler's-ruin fair baseline**
  `p_fair = risk/(reward+risk)` (for which gross EV is exactly 0) plus an
  **explicit, falsifiable `edge`** — so the gate is never rubber-stamped and a
  no-edge claim (`edge = 0`) is correctly refused.

- **Sizing** (`intraday/risk/sizing.py`). Fractional (half-)Kelly via SWE's risk
  manager, hard-capped by per-trade risk (1% of NAV), position notional (20% of
  NAV), and max size. Sizes the *risk*, not the notional.

- **The expectancy gate** (`intraday/authority/gate.py`). The single authority:
  `ev_gross = p·win − (1−p)·loss`; `ev_net = ev_gross − round_trip_cost`.
  Non-finite EV → BLOCKED; EV < 0 → BLOCKED; 0 ≤ EV ≤ threshold → REVIEW;
  EV > threshold → PROCEED. Nothing else can make a candidate tradeable.

- **Downgrade-only reviewers** (`intraday/authority/reviewers.py`). Each may only
  *demote* a verdict (PROCEED → REVIEW → SKIP → BLOCKED), never rescue one:
  - **Event lockout** — vetoes signals inside earnings/FOMC/CPI/NFP windows.
  - **Regime filter** — disables a strategy in a hostile gamma regime.
  - **Liquidity gate** — skips when the quoted spread is too wide.
  - **Daily kill-switch** — blocks all further signals once the day's loss budget
    (3% of NAV) is hit.

- **Backtest / fills** (`intraday/backtest/`). Event-driven replay in strict
  timestamp order. Fills occur at the **next bar's open** with adverse slippage
  (you never fill at the price you "saw"); stops/targets detected on the
  just-closed bar execute one bar later; positions flatten before the close (no
  overnight risk); a feed gap **halts** rather than interpolating.

- **Metrics + honesty harness** (`intraday/metrics.py`, `intraday/eval/`). Every
  figure is net of costs. The eval module adds the anti-overfitting statistics
  that make a verdict trustworthy: a **clustered t-stat** (one observation per
  trading day, so correlated intraday trades aren't double-counted), a
  **stationary-bootstrap Sharpe confidence interval**, and the **Deflated Sharpe
  Ratio** (Bailey & López de Prado), which discounts the best result across all
  strategy×symbol trials, plus chronological out-of-sample splits.

### 1.4 The cost model (why it dominates everything)

Costs are computed in `intraday/costs.py` over SWE's `transaction_costs`:
round-trip = entry slippage + exit slippage + commission, where each slippage term
is spread-based **plus Almgren-Chriss square-root market impact**. Key current
*assumptions* (not yet calibrated to real microstructure): a **5 bps fallback
spread** when no quote is available, an **impact coefficient of 0.10**, **$0.65 per
option contract / $0 per share** commissions. At intraday frequency these
frictions are the difference between a real edge and a mirage — which is exactly
what the results below show.

---

## Part 2 — What data the engine consumes today

### 2.1 The three data types it actually reads

| Container (`contracts.py`) | Columns | Feeds which features | Used by |
|---|---|---|---|
| `BarSeries` | open, high, low, close, volume (indexed by bar close) | VWAP, VWAP sigma, opening range, realized vol | every strategy |
| `OptionChainSeries` | snapshot_ts, expiration, strike, option_type, open_interest, implied_vol, spot | GEX / flip / walls, ATM IV, VRP | S1, S2 |
| `OptionTape` | ts, expiration, strike, right, price, size, nbbo_bid, nbbo_ask, side_inferred | order-flow imbalance (OFI) | S1 |

Each `FeatureRow` field is therefore traceable to a specific raw input:
`vwap*`/`orb_*`/`rv` ← bars; `atm_iv`/`vrp`/`gex_total`/`gamma_regime`/
`flip_level`/`nearest_call_wall`/`nearest_put_wall` ← chain; `ofi` ← tape.

### 2.2 Where each kind of data actually comes from (the corrected tier reality)

| Need | Real source | Status in repo |
|---|---|---|
| **Underlying** intraday bars (SPY/QQQ stock; SPX/VIX index) | **IBKR** (read-only, ~1000-bar / ~13-session cap) · **Yahoo** (free, ~60 sessions of 5-min) · **put-call parity** (deep history) | Built + tested |
| **Options** (chain + tape + IV + greeks) | **Theta STANDARD** (intraday option tick tape, 2016→today) | Built + tested, **never pulled** (operator step) |
| Theta stock / index | FREE / EOD or no access | **Cannot serve intraday** |
| Daily underlying for context | free daily (yfinance/Stooq) | regime/tail context only |

The load-bearing correction: **options come from Theta; the underlying never
does.** Backtests replay from the parquet store (`StoreBackedProvider`), which is
network-free, deterministic, and enforces provenance (it will not relabel a
captured source).

### 2.3 What the engine has actually been run on

- **Synthetic** (deterministic, seeded) — the Phase-0/Phase-1 workhorse; validated
  the plumbing, costs, and discipline, never an edge.
- **Real underlying** — 24-symbol × ~59-session Yahoo 5-min cross-section, plus a
  12-session IBKR slice. Cross-vendor integrity check: IBKR (ARCA) vs Yahoo
  (consolidated) 5-min closes agreed to < 2 bps.
- **Real options** — **none.** Theta has not been pulled; S1 and S2 have therefore
  never run on real data.

### 2.4 How no-look-ahead and freshness are enforced (so any new data must comply)

- PIT slicing lives in the containers: `BarSeries.available_at`,
  `OptionChainSeries.latest_available`, `OptionTape.window_available_at`, and the
  feature sampler `features/base.latest_value` — all gated by `available_ts`
  (= source timestamp + arrival latency).
- The vendor remap (`data/_remap.py`) shifts START-labelled vendor bars to the
  CLOSE grid, **forward-fills only** (never back-fills, which would pull a future
  bar into the open), counts coverage, and **skips** sessions below a coverage
  threshold or with a leading gap.
- Freshness: `data/quality.py` (`is_stale`, `assert_no_feed_gap`) **halts** rather
  than guessing across a gap. **Any new feed must carry the same arrival-latency
  stamp and the same freshness assertion**, or it silently reintroduces
  look-ahead/staleness.

---

## Part 3 — The honest result so far (what the data has shown)

On 24 symbols × 59 real 5-min sessions, net of costs, each strategy run standalone
per symbol then aggregated to an equal-weight portfolio and scored with the
honesty harness:

| Strategy | Portfolio Sharpe (ann) | 95% CI | Day t (p) | Deflated Sharpe / 72 trials |
|---|---|---|---|---|
| S3 VWAP reversion | −2.83 | [−5.39, 0.14] | −1.37 (0.18) | **0.000** |
| S4 ORB breakout | −2.76 | [−6.85, 0.77] | −1.34 (0.19) | **0.000** |
| S5 VWAP momentum | −4.89 | [−9.94, −1.07] | −2.37 (0.02) | **0.000** |

**Verdict: no demonstrated edge.** After multiple-testing deflation and an
out-of-sample split, no underlying-only intraday strategy beats costs on real
data — exactly what efficient prices plus transaction costs predict. The best
single trial (S3 on SMH, Sharpe 2.42) deflated to 0.13: the "winner" a 72-trial
search throws up by luck. An earlier 12-session IBKR run that looked positive
(+$306) was shown to be noise by the deeper test. Cost attribution: S3 gross was
roughly flat (≈ −$348) and bled ≈ $12.4k by costs over ~3.2k trades.

**The two conclusions that drive the data plan:**

1. **The underlying-only strategies are answered: no edge.** More underlying data
   will not change that; only *better cost realism* could (it might confirm "no
   edge" even harder, or reveal edge the conservative 5 bps stub was hiding).
2. **The strategies with a real structural thesis — S1 (dealer gamma) and S2
   (0DTE VRP) — have never been tested**, because there is no real options feed.
   This is the single biggest gap. The most valuable data is whatever unlocks
   them.

---

## Part 4 — The data this project needs (complete, prioritized, mapped to code)

Storage is not the constraint; **granularity + point-in-time correctness** are.
Two cross-cutting requirements apply to *everything* below: (a) each row must carry
the timestamp at which it was knowable (PIT — §2.4); (b) each new feed must be
wired with the freshness assertion. Where a data set can change *tradeability*, it
must enter as a **downgrade-only reviewer**, never as a signal/EV booster (this is
the design rule that keeps the honesty harness meaningful).

A note on Bloomberg specifically (relevant because data is being pulled from a
Terminal): Bloomberg **can** supply intraday via BLPAPI — `IntradayBarRequest`
(1-min OHLCV + volume + tick count) and `IntradayTickRequest` (TRADE/BID/ASK
ticks) — but only **~140 calendar days back**, which is deeper and cleaner than
Yahoo (60 days) *with real volume and quotes*. Bloomberg is **weak** at exactly
what S1/S2 need most — trade-by-trade **option tape with buy/sell-side inference**
and high-frequency **full-chain snapshots** — which remain **Theta's** job.
Bloomberg complements Theta/IBKR; it does not replace them for intraday options.

### Tier 1 — Options microstructure (unlocks S1 + S2 — highest value)

| Data | Fields / granularity | Source | Code consumer |
|---|---|---|---|
| **Intraday option-chain snapshots** | per strike/expiry: bid, ask, mid, IV, Δ Γ ν Θ, open_interest, volume, spot; ATM±10; 0DTE + near-dated; SPX/SPY/QQQ; ≤ 1-min cadence | **Theta** (BBG EOD only) | `OptionChainSeries` → `features/gex.py` (S1) + `features/vrp.py` (S2) → `FeatureRow.gex_total/gamma_regime/flip_level/walls/atm_iv/vrp` |
| **Option trade tape** | ts, strike, right, price, size, NBBO bid/ask, **side_inferred** | **Theta** (BBG no) | `OptionTape` → `features/ofi.py` → `FeatureRow.ofi` (S1) |
| **EOD option greeks / IV surface / OI** | full surface daily; skew + term structure | **Bloomberg** (shared with wheel) | VRP baseline, surface/skew context, OI for walls |

Without Tier 1, the bot's central question — does a dealer-gamma or VRP edge exist
intraday? — is literally unanswered.

### Tier 2 — Execution / cost realism (highest leverage for trustworthiness)

The "no edge" verdict is cost-dominated, and costs are currently modeled with a
5 bps fallback spread and a guessed impact coefficient. Real microstructure makes
the verdict trustworthy either way.

| Data | Fields / granularity | Source | Code consumer |
|---|---|---|---|
| **Intraday NBBO / quoted spread** | bid, ask, bid_size, ask_size; tick-level | **Bloomberg `IntradayTick` (BID/ASK)** ~140d; Theta for options | replaces `CostConfig.fallback_spread_pct` in `costs.py`; real input to `LiquidityGateReviewer` |
| **Trade prints vs prevailing NBBO** | TRADE ticks aligned to NBBO | Bloomberg / Theta | slippage calibration for `backtest/fills.py` (conservative-fill model) |
| **Intraday volume profile + true ADV** | per-minute volume U-shape; ADV per name | Bloomberg intraday bars | calibrates the sqrt-impact coefficient (now 0.10, a guess) and feeds `SignalProposal.adv` |
| **L2 depth (if entitled)** | top-of-book sizes; ideally full book | Bloomberg / IBKR | impact model beyond the sqrt approximation |

### Tier 3 — Real underlying intraday, deepened with true volume

| Data | Fields / granularity | Source | Code consumer |
|---|---|---|---|
| **1-min underlying bars w/ real volume + tick count** | OHLCV + numEvents; SPX/SPY/QQQ + universe; ~140d | **Bloomberg `IntradayBar`** | `BarSeries` (real volume → correct VWAP, vs Yahoo's) via a new provider mirroring `ibkr.py`/`yahoo.py` |
| **Deep-history intraday (> 140d, 2022→today)** | 1–5 min | **Theta options + put-call parity** (already built) | `ParityUnderlyingProvider` → powered out-of-sample test |
| **Third-vendor anchor** | same series from another vendor | Bloomberg (vs IBKR/Yahoo) | cross-vendor integrity check (`test_data_quality_xvendor.py`) |

### Tier 4 — Regime & event conditioning (wire as downgrade-only reviewers)

| Data | Fields / granularity | Source | Code consumer |
|---|---|---|---|
| **Event calendar w/ exact timestamps + importance** | FOMC/CPI/NFP release *times* (e.g. 08:30 ET), survey/actual, OPEX/quad-witch, single-name earnings | **Bloomberg ECO** (shared with wheel) | upgrades `EventLockoutReviewer` from date-grained to intraday-correct timing |
| **VIX term structure + futures curve** | VIX/VIX3M/VIX6M, UX1–7, VVIX, VIX9D, VXN, RVX | Bloomberg (keep it *fresh* — a prior pull was stale) | a vol-regime reviewer (contango/backwardation gate) |
| **Index implied correlation** | COR1M/3M (verify the units — single-digit values look wrong) | Bloomberg | dispersion-regime reviewer |
| **Market internals / breadth** | TICK, ADD, advancers/decliners, sector-ETF intraday | Bloomberg / IBKR | trend-vs-chop regime input for S1/S4/S5 |

### Tier 5 — Universe expansion (Phase 2) + point-in-time integrity

| Data | Fields / granularity | Source | Code consumer |
|---|---|---|---|
| **~20 liquid single names + sector ETFs: intraday bars + options** | as Tiers 1–3 | Theta + Bloomberg | Phase 2 (T2.1) single-name gamma/flow variant |
| **Borrow rate / short interest / hard-to-borrow** | per name, time series | Bloomberg (needs BQL/Excel entitlement) | short-feasibility + squeeze-risk reviewer |
| **Corporate actions** | splits, special dividends, spinoffs | Bloomberg | clean adjustment of intraday history before ingest |
| **Point-in-time index membership** | as-of-date constituents | Bloomberg | removes survivorship bias from the cross-sectional test |
| **Static / reference** | tick size, lot size, contract multiplier | Bloomberg | exact per-contract cost math in `costs.py` |

---

## Part 5 — Bloomberg vs Theta/IBKR, and the shared-with-wheel overlap

**Bloomberg is strong for:** ~140 days of intraday bars (with real volume + tick
count) and intraday ticks (trades + BID/ASK quotes); EOD option greeks/IV
surface/OI; vol indices and the VIX futures curve; the economic calendar (ECO)
with exact times and survey/actual; corporate actions; fundamentals; short
interest; index membership; sector/macro context.

**Bloomberg is weak / unavailable for:** deep-history intraday (> ~140 days);
high-frequency full option-chain snapshots across all strikes; trade-by-trade
**option tape with buy/sell-side inference**; 0DTE option tick-tape history. These
remain **Theta STANDARD** (and, for deep underlying history, **put-call parity**).

**Pull once, serve both projects.** These inputs are shared with the Smart Wheel
Engine, so a single pull benefits both: EOD option greeks/IV surface + OI, VIX
term structure, the event calendar (ECO), dividends, earnings, borrow/short
interest, corporate actions, fundamentals, and index membership. The
**day-trading-specific** pulls the wheel engine will not use are: intraday bars,
intraday NBBO/tick, intraday option chain, and the option trade tape.

---

## Part 6 — Recommended sequencing

1. **Real intraday NBBO/spreads for SPY/QQQ.** Cheapest, and it tells you whether
   *any* intraday edge can survive real costs before building anything else — it
   directly replaces the engine's biggest assumption (the 5 bps fallback spread).
2. **Theta intraday option chain + tape for SPX/SPY/QQQ.** Unlocks S1 and S2 — the
   only two strategies with a genuine structural thesis. This is the gap that
   actually decides whether the project has a reason to exist.
3. **Event calendar with exact times (Bloomberg ECO).** Turns the event-lockout
   reviewer from date-grained to intraday-correct (CPI at 08:30 ET matters).

Everything else in Tiers 3–5 is conditioning and expansion that adds value only
once those three exist. No data set should be wired into the EV path as anything
other than a downgrade-only reviewer, and nothing is promoted from paper toward
live without clearing the `DESIGN.md` §8 acceptance criteria out-of-sample on real
data — a separate, explicit decision.
