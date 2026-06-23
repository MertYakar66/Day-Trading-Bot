# Real-Data Wiring & Quant-Hardening Report

_Author: autonomous engineering session, 2026-06-22. Branch: `feat/real-data-wiring`._

This documents an end-to-end pass to (1) inventory the data we pulled and the
broader Smart-Wheel-Engine (SWE) dataset, (2) wire that real data into the engine
network-free, (3) harden the quant layer, and (4) stress-test the engine on real
data to see whether it produces realistic and reliable outputs.

> **The honest headline is unchanged and was never the target to "beat":** there
> is still **no demonstrated edge** on the real data we can reach. This work made
> the engine *more correct, better-instrumented, and honestly calibrated* — and
> the stress tests show it produces realistic, machine-precision-consistent
> outputs. Where real data revealed the strategies are net-negative, that result
> is reported plainly, not engineered away.

---

## 1. Executive summary

| Area | Delivered |
|---|---|
| **Inventory** | `docs/DATA_INVENTORY.md` — what we pulled (66-session × 24-name Yahoo store + IBKR + audit outputs) and the full SWE offline dataset (11 GB Theta options, 611 MB Bloomberg, vol-index panel) with schemas, ranges, and every data defect. |
| **Wiring (new)** | `intraday/data/swe_offline.py` (offline, defect-corrected loaders) + `intraday/data/daily_context.py` (prior-session real-data context) → real risk-free curve + vol regime now flow into every `FeatureRow`, reachable via `--daily-context`. |
| **Correctness** | Merged the reviewed audit-backlog fixes: metrics inception (Sharpe/DD were wrong), GEX multi-expiry blending (~3000× error on real chains), CLOSE-fill double-count, honest 20-session validity floor. |
| **New capability** | `VolRegimeReviewer` (opt-in, downgrade-only) stands aside in stress vol regimes (backwardation / extreme vvix). |
| **Validation** | Real GEX reconciles to an independent BSM recompute to **2.3e-15**; real single-name VRP **replicates** the −0.46 vol-pt finding; **33/33** numerical/PIT stress cases pass. |
| **Tests** | +82 hermetic tests (network-free, no dependence on the 11 GB data). Suite **626 passing**, ruff + mypy clean. |

---

## 2. What was pulled & what's available

See **`docs/DATA_INVENTORY.md`** for the full table. Key points:

- **Local (what we pulled):** a real *underlying* panel — 24 tickers × 66 sessions
  of 5-minute bars (2026-03-09 → 2026-06-10, Yahoo) plus shallow IBKR. **No
  options data locally**, so every options feature (GEX, ATM IV, VRP, OFI) was
  previously synthetic or absent on real runs.
- **SWE broader dataset (offline, reachable):** the missing options + macro layer
  — real index option chains (`index_options_chains`, real IV+OI), the deep
  per-expiration index option history (`index_reference/option_history`, SPX/SPY/
  QQQ 2016→2026), the **correct** daily IV source (`sp500_vol_iv_full`, 503 names,
  11 y), the treasury curve, and the daily VIX complex (`vol_indices_wide`).

---

## 3. What I wired (network-free)

**`intraday/data/swe_offline.py`** — reads SWE's captured files directly from disk
(no Theta socket, no Bloomberg API; the autouse socket-block test fixture stays
green = proof), with every documented defect corrected:

| Loader | Source | Correction applied |
|---|---|---|
| `load_risk_free_curve` | `treasury_yields.csv` | percent→decimal; PIT prior-session `rate_on` |
| `load_vol_regime` | `vol_indices_wide.parquet` | term slope (vix/vix3m), front slope (vix9d/vix), vvix, skew |
| `load_iv_history` | `sp500_vol_iv_full.csv` | the **correct** IV source; percent→decimal; 2026-03-20 partial-day trap excluded; index tickers refused |
| `load_index_chain` | `theta/index_options_chains` | single-expiry only; iv>0; real greeks/OI/underlying; returns the **real** expiry for GEX |

**`intraday/data/daily_context.py`** — `DailyContextProvider` bundles the
prior-session daily context (risk-free rate + VIX regime) into a `DailyContext`
the backtester stamps onto every `FeatureRow`. PIT by construction (strictly
prior-session daily close), purely additive (no provider ⇒ every field `None`,
behavior unchanged), reachable from the CLI via `--daily-context`.

**Effect:** the engine's hardcoded `risk_free_rate = 0.04` is replaced (when
enabled) by the real prior-session 3-month treasury rate feeding the GEX analyzer
and S2 option pricing; six new vol-regime fields are available to strategies and
reviewers.

---

## 4. Quant hardening

- **Correctness fixes** (merged from the reviewed `fix/audit-backlog`):
  - *Metrics inception* — the equity curve had no `nav0` row, so day-1 return and
    inception drawdown were dropped from Sharpe/Sortino/max-DD. Fixed; real Sharpes
    are now computed correctly (and are honestly negative — see §6).
  - *GEX multi-expiry blending* — real chains carry several expiries; blending them
    mispriced gamma by ~3000×. Now filtered to one expiry at its real tenor.
  - *CLOSE-fill double-count* in the persisted ledger; *honest 20-session validity
    floor* for the deflated-Sharpe verdict.
- **New, honest instrumentation:** `VolRegimeReviewer` (opt-in) — stand aside when
  the VIX term structure inverts or vol-of-vol spikes; both thresholds default to
  `None` (no-op), so the default stack and the published headline are unchanged
  unless the operator opts in.
- **What I deliberately did NOT change:** the fabricated `win_prob = p_fair + 0.10`
  edge in S1/S3/S4/S5. Replacing it with a *measured* hit-rate needs real intraday
  option tape we do not have; lowering it would only make the (already negative)
  result more conservative. It is documented as a known assumption, not silently
  altered.

---

## 5. Validation — does the engine produce realistic, reliable outputs?

Harnesses (read-only, network-free) under `scripts/`, outputs under
`data_raw/realdata_validation/`:

- **GEX (`validate_real_gex.py`):** across 14 real index-chain cells
  (SPX/RUT/VIX/XSP/DJX × 3 EOD snapshots), the engine's `gex_total` reconciles to
  an **independent** Black-Scholes gamma recompute (scipy *and* the SWE pricer) to a
  **max relative error of 2.3e-15** — machine precision. SPX is textbook: +$7.2–8.7B
  long-gamma, flip 2.4–3.5% **below** spot, walls on the top-OI round strikes; VIX
  options short-gamma. Signs match regime labels 14/14. _Honest caveat: the
  reconciliation shares the BSM formula and inputs, so it proves the engine's math
  and unit convention, not the dealer-positioning assumption or a third-party
  ground truth._
- **VRP (`measure_real_vrp.py`):** real single-name VRP (Bloomberg ATM IV − 30-day
  realized) over 11 years **replicates** the prior finding exactly — pooled mean
  **−0.465 vol pts**, ~**55%** of days positive, 9/12 names negative. Honest S2
  takeaway: single-name calendar VRP is ≈0/slightly negative, so S2's 0.02 *intraday*
  threshold is a clock artifact, not a real carry edge — documented, not "fixed" to
  manufacture premium.
- **Numerical stress (`stress_engine.py`):** **33/33** cases pass — GEX on
  pathological chains (single-strike, all-iv-zero, huge IV/OI), S2 across extreme
  IV / near-zero time, sizing bounds, degenerate metrics, and PIT no-look-ahead.
  The engine never emits NaN/inf/crash and degrades gracefully.

---

## 6. The honest real-data result

Full 66-session backtest on the gamma spine (SPY+QQQ, real Yahoo underlying) with
the new wiring (`--daily-context`) and the corrected metrics:

| Strategy | Net PnL | Sharpe (net) | Trades | Deflated Sharpe | Verdict |
|---|---:|---:|---:|---:|---|
| S3 (VWAP control) | −$1,737 | −4.50 | 275 | 0.063 | **NO demonstrated edge** |
| S4 (ORB breakout) | −$2,445 | −5.19 | 408 | 0.034 | **NO demonstrated edge** |
| S5 (VWAP momentum) | −$937 | −5.30 | 159 | 0.100 | **NO demonstrated edge** |

The corrected inception metric now shows the true (negative) Sharpe that the old
bug masked. The wiring reinforced the honest finding rather than undermining it.

---

## 7. What we still lack (honest gaps)

- **Intraday option tape/quotes for SPX/SPY/QQQ** — without it there is no real
  OFI, no real intraday ATM-IV path, and no way to replace the fabricated
  `win_prob` with a measured one. S1/S2 cannot be backtested on real options across
  many sessions.
- **The deep asset to build next:** `theta/index_reference/option_history`
  (SPX/SPY/QQQ, 2016→2026, OHLC+NBBO+OI) supports a real multi-year GEX/VRP series,
  but needs a Black-Scholes IV-inversion + parity-spot reconstruction layer (the
  `chain_synthesis.py` pattern) before it is engine-ready. Kept out of the hot path
  here to avoid shipping unvetted look-ahead/IV bugs.

---

## 8. Reproduce

```bash
# Real-data backtest with the new wiring (network-free)
python -m intraday backtest --provider yahoo-store --store-root data_raw/store_yahoo \
  --symbols SPY QQQ --start 2026-03-09 --end 2026-06-10 --interval 5m \
  --strategy s3 --daily-context

# Validation harnesses (read SWE data read-only; set SWE_ROOT to override the path)
python scripts/validate_real_gex.py
python scripts/measure_real_vrp.py
python scripts/stress_engine.py

# Gates
python -m ruff check intraday tests scripts && python -m mypy intraday && python -m pytest -q
```
