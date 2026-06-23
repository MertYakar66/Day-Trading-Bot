"""EOD cross-validation of the bot's intraday Yahoo store (workstream: data trust).

Compares store_yahoo 5m sessions, aggregated to daily OHLCV, against two
independent EOD sources for 12 overlapping single names:
  * Theta processed stocks_eod parquet (smart-wheel-engine, READ-ONLY)
  * Bloomberg sp500_ohlcv.csv (smart-wheel-engine, READ-ONLY)

Also re-verifies the documented IBKR-vs-Yahoo close agreement for SPY/QQQ from
the on-disk IBKR JSON captures. Everything runs offline from disk — no network.

Outputs (machine-readable) under data_raw/swe_tests/:
  eod_crossval_per_session.csv     one row per ticker x session x source
  eod_crossval_summary.json        per-ticker metrics + verdicts + diagnoses
  ibkr_vs_store_closes.csv         SPY/QQQ daily close comparison

Run:  .venv/Scripts/python.exe scripts/swe_tests/eod_cross_validation.py
(cwd = repo root so `intraday` imports)
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from intraday.data.store import ParquetStore  # noqa: E402

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
OUT_DIR = REPO / "data_raw" / "swe_tests"
OUT_DIR.mkdir(parents=True, exist_ok=True)

STORE_ROOT = REPO / "data_raw" / "store_yahoo"
TICKERS = [
    "AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
    "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM",
]
BPS = 1e4
CONTAINMENT_TOL_BPS = 5.0  # store high/low may exceed EOD high/low by <= 5 bps


# --------------------------------------------------------------------------- #
# 1. Store aggregation: 5m bars -> daily OHLC + summed volume
# --------------------------------------------------------------------------- #
def store_sessions(ticker: str) -> list[date]:
    base = STORE_ROOT / "bars" / f"ticker={ticker}"
    out = []
    for p in sorted(base.glob("date=*")):
        if (p / "bars_5m.parquet").exists():
            out.append(date.fromisoformat(p.name.split("=", 1)[1]))
    return out


def aggregate_store_daily(ticker: str) -> pd.DataFrame:
    """One row per session: daily OHLC + summed volume from 5m bars.

    Uses the bot's own ParquetStore reader so the provenance sidecar is
    enforced (source must round-trip as 'yahoo').
    """
    store = ParquetStore(STORE_ROOT)
    rows = []
    for day in store_sessions(ticker):
        bars = store.read_bars(ticker, day, "5m")
        assert bars.source.value == "yahoo", f"unexpected source {bars.source}"
        f = bars.frame
        if f.empty:
            continue
        # index is UTC bar-close ts; the partition date is the session date.
        rows.append(
            {
                "date": day,
                "open": float(f["open"].iloc[0]),
                "high": float(f["high"].max()),
                "low": float(f["low"].min()),
                "close": float(f["close"].iloc[-1]),
                # Yahoo's first 5m bar can contain opening-auction/pre-market
                # prints outside the official RTH range; track ex-first-bar
                # extremes separately for the containment test.
                "high_xfb": float(f["high"].iloc[1:].max()) if len(f) > 1 else np.nan,
                "low_xfb": float(f["low"].iloc[1:].min()) if len(f) > 1 else np.nan,
                "volume": float(f["volume"].sum()),
                "n_bars": int(len(f)),
                "last_bar_utc": str(f.index[-1]),
            }
        )
    df = pd.DataFrame(rows).set_index("date")
    return df


# --------------------------------------------------------------------------- #
# 2. EOD sources
# --------------------------------------------------------------------------- #
def load_theta_eod(ticker: str) -> pd.DataFrame:
    df = pd.read_parquet(SWE / "data_processed" / "theta" / "stocks_eod" / f"{ticker}.parquet")
    df = df.copy()
    df.index = pd.to_datetime(df.index).date
    df.index.name = "date"
    return df[["open", "high", "low", "close", "volume"]]


def load_bloomberg() -> pd.DataFrame:
    df = pd.read_csv(SWE / "data" / "bloomberg" / "sp500_ohlcv.csv")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    # 'A UN Equity' / 'AAPL UW Equity' etc -> first token
    df["sym"] = df["ticker"].str.split(" ").str[0]
    return df


def diagnose_bbg_columns(bbg: pd.DataFrame) -> dict:
    """Empirically map Bloomberg CSV columns onto true OHLC using Theta EOD.

    The raw CSV shows impossible rows (open > high), suggesting the columns
    were written under wrong labels. For every (bbg column, theta column)
    pair, compute the median |rel diff| in bps over the pooled 12-ticker
    overlap and pick the best assignment per bbg column.
    """
    fields = ["open", "high", "low", "close"]
    pooled: dict[tuple[str, str], list[np.ndarray]] = {
        (b, t): [] for b in fields for t in fields
    }
    n_obs = 0
    for tic in TICKERS:
        theta = load_theta_eod(tic)
        sub = bbg[bbg["sym"] == tic].set_index("date")
        common = sorted(set(theta.index) & set(sub.index))
        if not common:
            continue
        n_obs += len(common)
        th = theta.loc[common]
        bb = sub.loc[common]
        for b in fields:
            for t in fields:
                rel = (bb[b].to_numpy() / th[t].to_numpy() - 1.0) * BPS
                pooled[(b, t)].append(np.abs(rel))
    matrix = {
        f"bbg_{b}->theta_{t}": float(np.median(np.concatenate(v)))
        for (b, t), v in pooled.items()
        if v
    }
    mapping = {}
    for b in fields:
        best = min(fields, key=lambda t: matrix[f"bbg_{b}->theta_{t}"])
        mapping[b] = {"true_field": best, "median_abs_diff_bps": matrix[f"bbg_{b}->theta_{t}"]}
    return {"n_pooled_obs": n_obs, "matrix_bps": matrix, "mapping": mapping}


def corrected_bbg_frame(bbg: pd.DataFrame, ticker: str, mapping: dict) -> pd.DataFrame:
    sub = bbg[bbg["sym"] == ticker].set_index("date").sort_index()
    out = pd.DataFrame(index=sub.index)
    for raw_col, info in mapping.items():
        out[info["true_field"]] = sub[raw_col]
    out["volume"] = sub["volume"]
    return out


# --------------------------------------------------------------------------- #
# 3. Comparison metrics
# --------------------------------------------------------------------------- #
def compare(store_daily: pd.DataFrame, eod: pd.DataFrame, source: str, ticker: str):
    common = sorted(set(store_daily.index) & set(eod.index))
    rows = []
    for d in common:
        s, e = store_daily.loc[d], eod.loc[d]
        if e[["open", "high", "low", "close"]].isna().any():
            continue
        close_bps = (s["close"] / e["close"] - 1.0) * BPS
        open_bps = (s["open"] / e["open"] - 1.0) * BPS
        high_exc = (s["high"] / e["high"] - 1.0) * BPS   # >0: store high above EOD high
        low_exc = (e["low"] / s["low"] - 1.0) * BPS      # >0: store low below EOD low
        high_exc_x = (s["high_xfb"] / e["high"] - 1.0) * BPS
        low_exc_x = (e["low"] / s["low_xfb"] - 1.0) * BPS
        vol_ratio = s["volume"] / e["volume"] if e["volume"] else np.nan
        rows.append(
            {
                "ticker": ticker, "source": source, "date": str(d),
                "store_close": s["close"], "eod_close": e["close"],
                "close_diff_bps": close_bps, "open_diff_bps": open_bps,
                "high_excess_bps": high_exc, "low_excess_bps": low_exc,
                "high_violation": bool(high_exc > CONTAINMENT_TOL_BPS),
                "low_violation": bool(low_exc > CONTAINMENT_TOL_BPS),
                "high_violation_ex_first_bar": bool(high_exc_x > CONTAINMENT_TOL_BPS),
                "low_violation_ex_first_bar": bool(low_exc_x > CONTAINMENT_TOL_BPS),
                "store_volume": s["volume"], "eod_volume": e["volume"],
                "volume_ratio_store_over_eod": vol_ratio,
                "n_bars": int(s["n_bars"]),
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n_sessions": 0}
    df = pd.DataFrame(rows)
    vr = df["volume_ratio_store_over_eod"].dropna()
    return {
        "n_sessions": int(len(df)),
        "median_abs_close_diff_bps": float(df["close_diff_bps"].abs().median()),
        "max_abs_close_diff_bps": float(df["close_diff_bps"].abs().max()),
        "median_abs_open_diff_bps": float(df["open_diff_bps"].abs().median()),
        "high_violations_gt5bps": int(df["high_violation"].sum()),
        "low_violations_gt5bps": int(df["low_violation"].sum()),
        "high_violations_gt5bps_ex_first_bar": int(df["high_violation_ex_first_bar"].sum()),
        "low_violations_gt5bps_ex_first_bar": int(df["low_violation_ex_first_bar"].sum()),
        "worst_high_excess_bps": float(df["high_excess_bps"].max()),
        "worst_low_excess_bps": float(df["low_excess_bps"].max()),
        "volume_ratio_median": float(vr.median()) if len(vr) else None,
        "volume_ratio_min": float(vr.min()) if len(vr) else None,
        "volume_ratio_max": float(vr.max()) if len(vr) else None,
    }


def scale_breaks_store_vs_eod(rows: list[dict], jump_bps: float = 50.0) -> list[dict]:
    """Day-over-day jumps in the store/EOD close ratio (adjustment breaks)."""
    if not rows:
        return []
    df = pd.DataFrame(rows).sort_values("date")
    ratio = df["store_close"].to_numpy() / df["eod_close"].to_numpy()
    jumps = np.abs(np.diff(np.log(ratio))) * BPS
    out = []
    for i, j in enumerate(jumps):
        if j > jump_bps:
            out.append({"date": df["date"].iloc[i + 1], "ratio_jump_bps": float(j)})
    return out


def scale_breaks_theta_vs_bbg(ticker: str, theta: pd.DataFrame, bbg_c: pd.DataFrame,
                              thresh_bps: float = 100.0) -> dict:
    """Long-history Theta-vs-Bloomberg close diff; flags unadjusted splits/divs."""
    common = sorted(set(theta.index) & set(bbg_c.index))
    th = theta.loc[common, "close"].astype(float)
    bb = bbg_c.loc[common, "close"].astype(float)
    diff_bps = ((bb / th) - 1.0).abs() * BPS
    bad = diff_bps[diff_bps > thresh_bps]
    return {
        "n_sessions": len(common),
        "median_abs_close_diff_bps": float(diff_bps.median()) if len(common) else None,
        "n_days_gt_100bps": int(len(bad)),
        "worst_dates": [
            {"date": str(d), "abs_diff_bps": float(v)}
            for d, v in bad.sort_values(ascending=False).head(5).items()
        ],
    }


# --------------------------------------------------------------------------- #
# 4. IBKR vs store closes for SPY/QQQ
# --------------------------------------------------------------------------- #
def ibkr_daily_closes(path: Path) -> pd.DataFrame:
    d = json.loads(path.read_text())
    ts = pd.to_datetime(pd.Series(d["time"]), utc=True)
    f = pd.DataFrame(
        {"ts": ts, "open": d["open"], "high": d["high"], "low": d["low"],
         "close": d["close"], "volume": d["volume"]}
    )
    et = f["ts"].dt.tz_convert("America/New_York")
    f["session"] = et.dt.date
    # RTH only; IBKR stamps are bar starts. Keep bars starting 09:30-15:55 ET,
    # plus any 16:00-stamped closing print bar.
    hm = et.dt.hour * 100 + et.dt.minute
    f = f[(hm >= 930) & (hm <= 1600)]
    rows = []
    for day, g in f.groupby("session"):
        g = g.sort_values("ts")
        rows.append(
            {"date": day, "ibkr_close": float(g["close"].iloc[-1]),
             "ibkr_high": float(g["high"].max()), "ibkr_low": float(g["low"].min()),
             "n_bars": int(len(g)),
             "first_bar_et": str(g["ts"].iloc[0].tz_convert("America/New_York")),
             "last_bar_et": str(g["ts"].iloc[-1].tz_convert("America/New_York"))}
        )
    return pd.DataFrame(rows).set_index("date")


def ibkr_check(symbol: str) -> tuple[list[dict], dict]:
    ib = ibkr_daily_closes(REPO / "data_raw" / "ibkr_primary" / f"{symbol}_5m.json")
    full_days = ib[ib["n_bars"] >= 78]  # exclude partial first/last capture days
    sd = aggregate_store_daily(symbol)
    common = sorted(set(full_days.index) & set(sd.index))
    rows = []
    for d in common:
        diff = (sd.loc[d, "close"] / full_days.loc[d, "ibkr_close"] - 1.0) * BPS
        rows.append(
            {"symbol": symbol, "date": str(d),
             "store_close": sd.loc[d, "close"],
             "ibkr_close": float(full_days.loc[d, "ibkr_close"]),
             "close_diff_bps": float(diff),
             "ibkr_n_bars": int(full_days.loc[d, "n_bars"])}
        )
    df = pd.DataFrame(rows)
    summ = {
        "n_sessions": len(df),
        "median_abs_close_diff_bps": float(df["close_diff_bps"].abs().median()) if len(df) else None,
        "max_abs_close_diff_bps": float(df["close_diff_bps"].abs().max()) if len(df) else None,
        "agrees_lt_2bps_all_sessions": bool((df["close_diff_bps"].abs() < 2.0).all()) if len(df) else None,
    }
    return rows, summ


# --------------------------------------------------------------------------- #
# 5. Verdicts
# --------------------------------------------------------------------------- #
# 2026-03-20 (final date in BOTH EOD sources) was shown by
# scripts/swe_tests/eod_crossval_diagnose.py to be a PARTIAL-DAY intraday
# snapshot (~14:50-15:15 ET): the EOD "close" matches a mid-afternoon store
# bar to <1 bps for 12/12 tickers and the EOD low equals the running (not
# full-day) low. The defect is in the EOD sources, not the store, so final
# verdicts are computed excluding that date; raw metrics are still reported.
PARTIAL_EOD_DATE = "2026-03-20"


def verdict(theta_s: dict, bbg_s: dict, breaks_store: list, breaks_hist: dict) -> dict:
    reasons = []
    if theta_s.get("n_sessions", 0) == 0:
        return {"verdict": "INSUFFICIENT DATA", "reasons": ["no overlapping sessions vs Theta EOD"]}
    if theta_s["median_abs_close_diff_bps"] > 10:
        reasons.append(f"median close diff vs Theta {theta_s['median_abs_close_diff_bps']:.1f} bps > 10")
    if theta_s["max_abs_close_diff_bps"] > 30:
        reasons.append(f"max close diff vs Theta {theta_s['max_abs_close_diff_bps']:.1f} bps > 30")
    nviol = (theta_s["high_violations_gt5bps_ex_first_bar"]
             + theta_s["low_violations_gt5bps_ex_first_bar"])
    if nviol > 1:
        reasons.append(f"{nviol} ex-first-bar high/low containment violations vs Theta")
    if bbg_s.get("n_sessions", 0) and bbg_s["median_abs_close_diff_bps"] > 10:
        reasons.append(f"median close diff vs Bloomberg {bbg_s['median_abs_close_diff_bps']:.1f} bps > 10")
    if breaks_store:
        reasons.append(f"store/EOD close-ratio jump(s) >50bps on {[b['date'] for b in breaks_store]}")
    if breaks_hist.get("n_days_gt_100bps", 0) > 2:
        reasons.append(
            f"Theta-vs-Bloomberg history has {breaks_hist['n_days_gt_100bps']} days >100bps "
            f"(possible adjustment breaks)"
        )
    return {"verdict": "SUSPECT" if reasons else "CLEAN", "reasons": reasons}


def main() -> None:
    bbg = load_bloomberg()
    bbg_diag = diagnose_bbg_columns(bbg)
    mapping = bbg_diag["mapping"]

    all_rows: list[dict] = []
    per_ticker: dict[str, dict] = {}
    for tic in TICKERS:
        sd = aggregate_store_daily(tic)
        theta = load_theta_eod(tic)
        bbg_c = corrected_bbg_frame(bbg, tic, mapping)

        rows_t = compare(sd, theta, "theta_stocks_eod", tic)
        rows_b = compare(sd, bbg_c, "bloomberg_ohlcv_corrected", tic)
        all_rows += rows_t + rows_b

        rows_t_x = [r for r in rows_t if r["date"] != PARTIAL_EOD_DATE]
        rows_b_x = [r for r in rows_b if r["date"] != PARTIAL_EOD_DATE]
        theta_s = summarize(rows_t)
        bbg_s = summarize(rows_b)
        theta_sx = summarize(rows_t_x)
        bbg_sx = summarize(rows_b_x)
        breaks_store = scale_breaks_store_vs_eod(rows_t_x)
        breaks_hist = scale_breaks_theta_vs_bbg(tic, theta, bbg_c)
        v = verdict(theta_sx, bbg_sx, breaks_store, breaks_hist)
        per_ticker[tic] = {
            "store_sessions_total": len(sd),
            "store_first_session": str(sd.index.min()),
            "store_last_session": str(sd.index.max()),
            "sessions_with_missing_bars": int((sd["n_bars"] != 78).sum()),
            "vs_theta_raw_incl_partial_eod_date": theta_s,
            "vs_bloomberg_raw_incl_partial_eod_date": bbg_s,
            "vs_theta_ex_partial_eod_date": theta_sx,
            "vs_bloomberg_ex_partial_eod_date": bbg_sx,
            "store_vs_theta_ratio_breaks_ex_partial": breaks_store,
            "theta_vs_bbg_history": breaks_hist,
            **v,
        }

    ibkr_rows_all, ibkr_summ = [], {}
    for sym in ["SPY", "QQQ"]:
        r, s = ibkr_check(sym)
        ibkr_rows_all += r
        ibkr_summ[sym] = s

    pd.DataFrame(all_rows).to_csv(OUT_DIR / "eod_crossval_per_session.csv", index=False)
    pd.DataFrame(ibkr_rows_all).to_csv(OUT_DIR / "ibkr_vs_store_closes.csv", index=False)
    summary = {
        "run_date": "2026-06-10",
        "tickers": TICKERS,
        "bloomberg_column_diagnosis": bbg_diag,
        "per_ticker": per_ticker,
        "ibkr_vs_store": ibkr_summ,
        "notes": [
            "store_yahoo 5m aggregated per session partition; provenance sidecar verified ('yahoo').",
            f"containment tolerance {CONTAINMENT_TOL_BPS} bps; ratio-break threshold 50 bps day-over-day.",
            "Bloomberg OHLC used AFTER empirical column re-mapping (see bloomberg_column_diagnosis).",
            "Theta stocks_eod and corrected Bloomberg OHLCV are numerically identical (same upstream); "
            "they are NOT independent cross-checks of each other.",
            f"{PARTIAL_EOD_DATE} excluded from verdicts: diagnosed as a partial-day (~15:00 ET) snapshot "
            "in BOTH EOD sources (see eod_crossval_diagnostics.json).",
            "Containment verdicts use ex-first-bar extremes: Yahoo's 09:30-09:35 bar includes "
            "opening-auction/pre-market prints outside the official range.",
            "IBKR daily closes built from on-disk 5m JSON captures (RTH bars, ET session grouping); "
            "partial capture days (<78 bars) excluded.",
        ],
    }
    (OUT_DIR / "eod_crossval_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
