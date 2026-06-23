# Data Inventory & Wiring Plan

_Prepared 2026-06-22. Documents (1) what data we pulled locally, (2) the broader
Smart-Wheel-Engine (SWE) dataset reachable offline, (3) which engine placeholders
real data replaces, and (4) the prioritized wiring — what is now wired and what
remains. All figures below were verified first-hand against the files on disk._

> **Honesty guardrail.** Wiring real data in does NOT change the published
> headline: there is still **no demonstrated edge** on the real data we can reach.
> The point of this work is a *more correct, better-instrumented, honestly
> calibrated* engine — never a manufactured edge. Where real data is too shallow
> to prove something, this document says so.

---

## A. What we pulled (local — `Day-Trading-Bot/data_raw`, `data_store`)

| Store | Contents | Coverage | Notes |
|---|---|---|---|
| `data_raw/store_yahoo/` | 24-ticker 5-minute bar store (partitioned parquet `ticker=/date=`) | **66 sessions, 2026-03-09 → 2026-06-10**, every ticker | The deepest underlying source. Universe: SPY QQQ DIA IWM + AAPL AMD AMZN AVGO GOOGL JPM META MSFT NFLX NVDA TSLA XOM + sector/asset ETFs (SMH XLE XLF XLK XLV XLY GLD TLT). |
| `data_raw/yahoo/`, `yahoo_daily/2026-06-11/` | Raw Yahoo 5m/60d JSON pulls (24 tickers) | one-shot 2026-06-11 | Source for the store ingest. |
| `data_raw/ibkr/`, `ibkr_primary/` | IBKR underlying JSON: SPY/QQQ stock, SPX/VIX index | shallow — `SPX_5m_3mo.json` is really ~1 week (IBKR 1000-bar cap) | Real but capped; cross-validated <2 bps vs Yahoo closes. |
| `data_raw/swe_tests/` | Real-data test outputs (uncommitted, machine-local) | 2026-06-10 audit | GEX spine run, VRP clock-bias study, EOD cross-val, S1 pilot, regime stratification. The only prior real-chain S1/S2 evidence. |
| `data_store/` | Committed QQQ/SPY 5m bars | May–Jun 2026 | Small fixture set used by tests/samples. |

**Bottom line on local data:** we have a solid *underlying* panel (66 sessions ×
24 names, 5m) but **no options data locally** — every options-dependent feature
(GEX, ATM IV, VRP, OFI) was previously synthetic or absent on real runs.

---

## B. SWE broader dataset (offline — read directly from `smart-wheel-engine/`)

All of the below is read **from disk, network-free** — no Theta Terminal socket,
no Bloomberg API. The new `intraday/data/swe_offline.py` bridge exposes the
cleanest of these with every documented defect corrected.

### B.1 Bloomberg (`data/bloomberg/`, ~611 MB)

| File | Schema (key cols) | Range | Usability / defect |
|---|---|---|---|
| **`sp500_vol_iv_full.csv`** (81 MB) | `date, ticker, hist_put_imp_vol, hist_call_imp_vol, volatility_30d/60d/90d/260d` | **2015-01-02 → 2026-03-20**, 503 names, 1.36 M rows | **The correct daily ATM IV source.** IV in **percent** (÷100). `hist_put == hist_call` exactly (Bloomberg writes one ATM series into both — no skew). `volatility_Nd` are realized vols. **Index tickers absent** (no SPY/SPX/QQQ). Bloomberg suffix (`AAPL UW`, `JPM UN`). Last row 2026-03-20 is a partial day — exclude. |
| `treasury_yields.csv` | `date, rate_3m/6m/2y/10y` (**percent**) | 2021-05-07 → 2026-05-05 | Real risk-free curve. ÷100 → decimal. `rate_3m` is the short-rate proxy for 0DTE/short options. |
| `vix_term_structure.csv` | `date, vix, vix_3m, vix_6m` | 2018 → 2026-03-20 | Term-structure regime. (Superseded for our use by the richer `vol_indices_wide`.) |
| `sp500_vix_full.csv` | stacked `date, close, instrument∈{vix,vix3m,vix6m,vvix,vx1,vx2}` | 2015 → 2026-03-20 | VVIX + VX futures basis. |
| `sp500_macro.csv` | stacked OHLC `instrument∈{us_10y,us_2y,wti_oil,gold,dxy,spx}` | 2015 → 2026-03-20 | Macro regime (yield curve, oil, gold, DXY). |
| `sp500_sector_etfs.csv` | OHLCV per `etf` (11 SPDR sectors) | 2015 → 2026-03-20 | Sector rotation / breadth. OHLC correctly labelled here. |
| `sp500_liquidity.csv` (69 MB) | `date, avg_vol_30d, turnover, shares_out, ticker` | 2015 → 2026-03-20, 503 names | **Real ADV** → replaces the hardcoded `_ADV_SHARES` for single names (index ETFs absent). |
| `sp500_earnings.csv` | `announcement_date, announcement_time, eps...` | 1980 → 2028 (fwd) | **Earnings event mask** for the event-lockout reviewer. |
| `sp500_ohlcv.csv` (60 MB) | OHLCV per `ticker` | 2018 → 2026-03-20 | ⚠ **OHLC columns mislabeled**: `open`=HIGH, `high`=CLOSE, `close`=OPEN (alphabetical PX_* artifact). Remap before use. After remap it is byte-identical to Theta `stocks_eod` (shared upstream — NOT independent). |

### B.2 Theta options (`data_processed/theta/`, ~11 GB)

| Subdir | What | Coverage | Usability |
|---|---|---|---|
| **`index_options_chains/`** | Greek chains w/ real `iv`, `open_interest`, `underlying_price` | SPX/SPXW/NDX/RUT/VIX/XSP/DJX × **3 EOD snapshots** (4/23, 5/24, 6/1) | **Cleanest real GEX input** for indices — no IV inversion needed. NDX is a quote-only snapshot (no iv/OI). **No SPY/QQQ.** |
| `index_options_surfaces/` | `(strike,right,delta,iv,mid,dte)` IV surface | same symbols × 3 snapshots | ATM IV / skew for indices. |
| **`iv_surface/`** | IV surface incl. **SPY + QQQ** | 502 names + SPY/QQQ × 3 snapshots, 8 expiries each | **Only source of real SPY/QQQ option IV** (the gamma-spine ETFs). |
| **`index_reference/option_history/`** (705 MB) | Per-contract daily OHLC + **NBBO bid/ask** + `open_interest`, partitioned `ticker=/expiration=` | **SPX/SPY/QQQ/NDX/RUT/XSP, 2016 → 2026** (122 SPX expiries) | **The deep asset:** a real, multi-year, per-date index option chain — enough for a real GEX/VRP *time series*. Has **no `iv` column** → IV must be BS-inverted from the mid (heavier; see §E). |
| `chains/` (32 MB) | Greek chains, single names | 495 names × 4 snapshots | Single-name GEX. |
| `option_history/` (7.7 GB), `option_history_deep365/` | Deep single-name option OHLC+OI tape | 154 names 2016→2026 (8 mega-caps deep) | Real OFI / single-name backtests (heavy). |
| `stocks_eod/` | Single-name EOD bars | 493 names, 2024-04 → 2026-03 | Underlying reference. |
| `vix_family/`, `iv_history/` | VIX complex (cboe); single-name *realized* vol mislabeled `iv_atm` | — | `iv_history.iv_atm` is **realized**, not implied — do **not** use as IV. |

### B.3 Vol indices (`data_processed/vol_indices_wide.parquet`)

`date + vix vix3m vix6m vix9d vvix skew move ovx gvz vxn` (points), **2011-05-31
→ 2026-05-22**, 3 783 rows. The single richest vol-regime panel — feeds term
structure (vix/vix3m), front slope (vix9d/vix), vol-of-vol (vvix), tail (skew).

---

## C. Engine placeholders that real data replaces

| Placeholder | Location | Real replacement | Status |
|---|---|---|---|
| `risk_free_rate = 0.04` | `config.py` GateConfig | treasury 3m curve, prior-session | ✅ **wired** (`DailyContextProvider` → `fr.risk_free_rate` → GEX + S2) |
| (no vol regime at all) | — | vix/term-slope/vvix/skew | ✅ **wired** (new `FeatureRow` fields, additive) |
| ATM IV / GEX on indices | `features/vrp.py`, `features/gex.py` | `index_options_chains` real iv+OI | ✅ **validated** (real SPX GEX reproduces the known spine) |
| GEX multi-expiry blending | `features/gex.py` | filter to one expiry at its real T | ✅ **fixed** (merged audit-backlog; real chains carry the true expiry) |
| metrics inception drop | `metrics.py` | prepend nav0 row | ✅ **fixed** (merged audit-backlog) |
| `win_prob = p_fair + 0.10` | S1/S3/S4/S5 | measured per-strategy hit-rate | ⚠ **documented, not changed** — requires real intraday option tape we lack (see §E). Lowering it only makes the honest result more conservative. |
| `_ADV_SHARES` (SPY/QQQ) | `s1_gamma_regime.py` | `sp500_liquidity` ADV | ⚠ single-names only (index ETFs absent from Bloomberg liquidity) |
| `fallback_spread_pct` | `config.py` | real NBBO half-spread | ❌ needs a quote feed |
| `paper_nav = 100k` | `config.py` | operator decision | ❌ open (DESIGN §11) |

---

## D. Prioritized wiring (value × feasibility)

1. ✅ **Real risk-free curve** — done. Clean, low-risk, removes a hardcoded assumption from pricing + metrics.
2. ✅ **Vol-regime context layer** — done. New, additive instrumentation the engine entirely lacked; enables regime-aware risk control.
3. ✅ **Real GEX validation on index chains** — done. Reproduces SPX +$7.2–8.7B long-gamma, flip 2.4–3.5% below spot, walls on round strikes (matches the prior independent run).
4. ✅ **Correctness fixes** (GEX expiry, metrics inception, CLOSE-fill double-count, honest 20-session validity floor) — merged.
5. **Real VRP measurement + honest S2 calibration** — in progress (Bloomberg IV vs realized across the universe).
6. **Real ADV** (single names) — available via `sp500_liquidity`; low priority since the spine trades SPY/QQQ.

---

## E. Data we still lack (the honest gaps)

- **Intraday option tape/quotes for the gamma spine (SPX/SPY/QQQ).** Without it
  there is no real OFI and no real intraday ATM-IV path, so S1/S2 cannot be
  backtested on real options across many sessions, and the fabricated
  `win_prob` edge cannot be replaced with a measured one. The deep
  `index_reference/option_history` (B.2) is the closest asset, but it needs a
  BS IV-inversion + parity-spot reconstruction layer (the `chain_synthesis.py`
  pattern) before it is engine-ready — deliberately kept out of the hot path for
  now to avoid shipping unvetted look-ahead/IV bugs.
- **Real index option chains beyond 3 EOD snapshots.** GEX is validated, not
  backtested, on real index data.
- **Real underlying intraday history >66 sessions** for the spine (IBKR is
  capped; deep history needs the put-call-parity path).

---

## F. Reuse vs build

- **Reused / merged:** `fix/audit-backlog` correctness fixes; the GEX/VRP expiry
  plumbing; SWE quant math (`dealer_positioning`, `option_pricer`, RV, metrics).
- **Built new:** `intraday/data/swe_offline.py` (offline defect-corrected
  loaders) and `intraday/data/daily_context.py` (prior-session context wiring).
- **Available to build later:** a real index-chain provider over
  `index_reference/option_history` (the deep asset) for a multi-year real GEX/VRP
  backtest — the single highest-value next step, deferred for correctness reasons.
