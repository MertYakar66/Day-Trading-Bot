# Real-Data Path — Architecture & How-To

This engine was built and validated against a deterministic SYNTHETIC provider.
This document describes the **corrected real-data path**: where each kind of data
actually comes from, how to ingest it, and how to run a real-data backtest.

> **One-line truth:** options come from **Theta (STANDARD)**; the **underlying**
> comes from **IBKR** (recent/live) or **put-call parity** (deep history) — never
> from Theta. Synthetic remains the test fixture.

## 1. The data-tier reality (the correction)

The earlier real-Theta adapter assumed Theta provided intraday stock + index. It
does **not** at the operator's tiers:

| Source | Tier | What it provides |
|---|---|---|
| **Theta OPTIONS** | STANDARD | intraday option **tick tape + IV + 1st greeks**, 2016→today. The only real Theta data here. |
| Theta STOCK | FREE | EOD only — **no intraday stock** |
| Theta INDEX (SPX/VIX) | FREE | **no access at all** |
| **IBKR** | — | **underlying** intraday bars + live snapshots (SPY/QQQ stock; SPX/VIX index), **reads only** |
| Put-call **parity** | — | **underlying** reconstructed from synchronized ATM call/put quotes (deep history) |
| Free daily (yfinance/Stooq) | — | daily underlying for regime/tail **context** only |

So the underlying is sourced from IBKR/parity and **options from Theta**. The
`ThetaDataProvider.get_bars` now raises a *structural* error (wrong source).

## 2. Providers (all behind `intraday.data.DataProvider`)

| Provider | Source | Role |
|---|---|---|
| `IBKRDataProvider` | `IBKR` | underlying intraday bars via a read-only `IBKRClient` (operator: `ib_insync`; dev: IBKR MCP). Underlying only — option methods raise. |
| `YahooDataProvider` | `YAHOO` | **free** underlying intraday bars (~60 sessions of 5-min, reads only) via a read-only `YahooClient`. The deepest no-Theta source; powers the cross-sectional evaluation. Underlying only. |
| `ParityUnderlyingProvider` | `PARITY` | underlying reconstructed from ATM call/put quotes (deep history). Proven by tests; consumes Theta option quotes when available. |
| `StoreBackedProvider` | (declared) | replays previously-ingested data from the parquet store — **network-free, deterministic, CI-safe**. This is what real backtests run on. Enforces provenance (never relabels). |
| `FusedDataProvider` | `FUSED` | composite: underlying across `[IBKR → parity → free-daily]`, options from Theta. Each frame keeps its own source. |
| `ThetaDataProvider` | `THETA` | OPTIONS-only adapter; disconnected this session. |
| `SyntheticDataProvider` | `SYNTHETIC` | deterministic test fixture. |

The clean workflow real desks use, and the one here: **pull once → store → backtest
many times.** The bulky fetch is separate from the deterministic replay.

## 3. IBKR underlying: fetch → ingest → backtest

IBKR intraday history via the data API is **shallow**: ≤ **1000 bars per request**,
fetched **back-from-now** (no arbitrary start date). At 5-min that is ~13 recent
sessions — enough for a *plumbing* validation on real prices, **not** a
statistically powered edge test (that needs the Theta+parity backfill, §4).

```bash
# 1) FETCH (network): the operator's ib_insync client, or in dev the IBKR MCP
#    get_price_history tool, writes raw JSON per symbol/interval to data_raw/ibkr/.
#    NEVER call any IBKR order/account endpoint — data reads only.

# 2) INGEST (deterministic, network-free): remap onto the canonical RTH close grid
#    and write per-session parquet partitions with DataSource.IBKR provenance.
python -m scripts.ingest_ibkr_underlying --raw-dir data_raw/ibkr --store-root data_store

# 3) BACKTEST on the real data (S3, the underlying-only control):
python -m intraday backtest --provider ibkr-store --symbols SPY QQQ \
    --interval 5m --strategy s3 --start <first-session> --end <last-session>
```

Contracts are pre-resolved in `intraday.data.ibkr.IBKR_CONTRACTS`
(SPY=756733/ARCA, QQQ=320227571/NASDAQ, SPX=416904/CBOE, VIX=13455763/CBOE).

**Grid remap (correctness):** IBKR labels bars at the interval START; the engine
indexes by the CLOSE. Ingestion shifts start→close and reindexes onto
`bar_close_index`, so the feed-gap guard and multi-symbol alignment hold. Missing
intraday bars are forward-filled and **counted**; sessions below `--min-coverage`
(half-days / sparse names) are **skipped**, never padded.

**Why only SPY/QQQ for S3:** S3 is VWAP/ORB and needs volume. SPX/VIX are indices
(no volume) — useful as context and for parity, not for the VWAP control.

## 3b. Free Yahoo universe + powered evaluation (no Theta)

Yahoo's chart API serves ~60 sessions of 5-min bars free (browser UA), across a
wide universe — deeper than IBKR's ~1000-bar cap, so a **statistically powered,
cross-sectional** real-data test is feasible without Theta:

```bash
python -m scripts.fetch_yahoo_universe                                    # 24 symbols, 60d/5m → data_raw/yahoo
python -m scripts.ingest_yahoo_universe --store-root data_raw/store_yahoo # → parquet (DataSource.YAHOO)
python -m scripts.eval_real_universe --store-root data_raw/store_yahoo    # honest, multiple-testing-aware verdict
```

The evaluator runs each strategy standalone per symbol (no concurrency-cap
distortion), aggregates to an equal-weight portfolio, and scores it with
`intraday.eval`: a **clustered t-stat** (one observation per trading day — correlated
intraday trades are not double-counted), a **stationary-bootstrap Sharpe CI**, and
the **Deflated Sharpe Ratio** (Bailey & López de Prado) that discounts the best of
all strategy×symbol trials, plus a chronological **out-of-sample** split. The
standard `python -m intraday backtest` also prints a per-run honesty scorecard.

Cross-vendor integrity: ingest the IBKR slice too and compare — Yahoo (consolidated)
vs IBKR (ARCA) 5-min closes agree to < 2 bps, corroborating the data.

## 4. Deep history (2022→today) & options: the Theta backfill

S1 (gamma regime) and S2 (0DTE VRP) need real **option** data, and a powered S3
test needs deep **underlying** history. Both come from the scoped Theta options
pull — an **operator** step (see [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md)):

1. Operator runs the scoped pull (SPX/SPY/QQQ, ATM±10, 0DTE+near, 2022→today) →
   option `trades.parquet` / `quotes.parquet`.
2. Ingest the option tape/chain into the store as `DataSource.THETA`.
3. Reconstruct deep-history **underlying** from ATM call/put quotes via
   `ParityUnderlyingProvider` (`F = K + e^{rT}(C−P)`, `S = F·e^{−(r−q)T}`).
4. Backtest S1/S2/S3 on the real options + parity underlying, net of costs, OOS.

This session does **not** touch Theta (the operator uses the subscription
concurrently); the path is delivered + proven by tests, and executed by the operator.

## 5. Provenance & honesty invariants

- Every stored frame carries its `DataSource`; `StoreBackedProvider` refuses to
  serve a frame under a different source than it was written with.
- Reports render `*** SYNTHETIC DATA — NOT A REAL EDGE ***` for synthetic and
  `[REAL DATA: <source>]` for real — a real result can never be mistaken for
  synthetic or vice-versa.
- All results are **net of costs**; no parameter is fit on the evaluation data.
