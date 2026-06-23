"""Adversarial review #1 of the EOD cross-validation workstream.

Independently recomputes the headline numbers from raw inputs (store parquet
read directly, Theta stocks_eod parquet, raw Bloomberg CSV, IBKR JSON) and
cross-checks them against the workstream's claims. Offline only; SWE read-only.

Output: data_raw/swe_tests/review1_verify_eod_crossval.json
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
OUT = REPO / "data_raw" / "swe_tests" / "review1_verify_eod_crossval.json"
STORE = REPO / "data_raw" / "store_yahoo" / "bars"
TICKERS = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
           "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM"]
BPS = 1e4
PARTIAL = "2026-03-20"
res: dict = {}

# ---------------------------------------------------------------- raw store --
def store_daily_raw(tic: str) -> pd.DataFrame:
    """Aggregate 5m parquet to daily WITHOUT using intraday.* code paths."""
    rows = []
    for p in sorted((STORE / f"ticker={tic}").glob("date=*")):
        f = pd.read_parquet(p / "bars_5m.parquet")
        if f.empty:
            continue
        idx = pd.to_datetime(f.index)
        et = idx.tz_convert("America/New_York")
        rows.append({
            "date": p.name.split("=", 1)[1],
            "open": float(f["open"].iloc[0]),
            "high": float(f["high"].max()),
            "low": float(f["low"].min()),
            "close": float(f["close"].iloc[-1]),
            "high_xfb": float(f["high"].iloc[1:].max()),
            "low_xfb": float(f["low"].iloc[1:].min()),
            "volume": float(f["volume"].sum()),
            "n_bars": len(f),
            "first_bar_et": str(et[0]), "last_bar_et": str(et[-1]),
        })
    return pd.DataFrame(rows).set_index("date")


def theta_eod(tic: str) -> pd.DataFrame:
    df = pd.read_parquet(SWE / "data_processed" / "theta" / "stocks_eod" / f"{tic}.parquet")
    df = df.copy()
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    return df

# 1. Recompute store-vs-theta per-session diffs from scratch ------------------
per_session = []
grid_check = {}
for tic in TICKERS:
    sd = store_daily_raw(tic)
    th = theta_eod(tic)
    grid_check[tic] = {
        "n_sessions": len(sd),
        "n_not_78_bars": int((sd["n_bars"] != 78).sum()),
        "first_bar_et_set": sorted(set(s[11:19] for s in sd["first_bar_et"])),
        "last_bar_et_set": sorted(set(s[11:19] for s in sd["last_bar_et"])),
    }
    common = sorted(set(sd.index) & set(th.index))
    for d in common:
        s, e = sd.loc[d], th.loc[d]
        per_session.append({
            "ticker": tic, "date": d,
            "close_bps": (s["close"] / e["close"] - 1.0) * BPS,
            "open_bps": (s["open"] / e["open"] - 1.0) * BPS,
            "high_exc": (s["high"] / e["high"] - 1.0) * BPS,
            "low_exc": (e["low"] / s["low"] - 1.0) * BPS,
            "high_exc_x": (s["high_xfb"] / e["high"] - 1.0) * BPS,
            "low_exc_x": (e["low"] / s["low_xfb"] - 1.0) * BPS,
            "vr": s["volume"] / e["volume"],
        })
ps = pd.DataFrame(per_session)
ex = ps[ps["date"] != PARTIAL]
on = ps[ps["date"] == PARTIAL]
TOL = 5.0
res["recomputed_store_vs_theta"] = {
    "n_overlap_ticker_sessions": len(ps),
    "n_ex_partial": len(ex),
    "pooled_median_abs_close_bps_ex": float(ex["close_bps"].abs().median()),
    "pooled_max_abs_close_bps_ex": float(ex["close_bps"].abs().max()),
    "pooled_median_abs_open_bps_ex": float(ex["open_bps"].abs().median()),
    "per_ticker_median_abs_close_bps_ex": {
        t: float(g["close_bps"].abs().median()) for t, g in ex.groupby("ticker")},
    "raw_violation_flags_total": int((ps["high_exc"] > TOL).sum() + (ps["low_exc"] > TOL).sum()),
    "raw_violation_flags_on_0320": int((on["high_exc"] > TOL).sum() + (on["low_exc"] > TOL).sum()),
    "raw_violation_flags_ex_0320": int((ex["high_exc"] > TOL).sum() + (ex["low_exc"] > TOL).sum()),
    "raw_violation_sessions_total": int(((ps["high_exc"] > TOL) | (ps["low_exc"] > TOL)).sum()),
    "raw_violation_sessions_on_0320": int(((on["high_exc"] > TOL) | (on["low_exc"] > TOL)).sum()),
    "exfb_ex0320_violations": ex[(ex["high_exc_x"] > TOL) | (ex["low_exc_x"] > TOL)][
        ["ticker", "date", "high_exc_x", "low_exc_x"]].to_dict("records"),
    "median_abs_close_bps_on_0320": float(on["close_bps"].abs().median()),
    "max_abs_close_bps_on_0320": float(on["close_bps"].abs().max()),
    "vol_ratio_median_ex": float(ex["vr"].median()),
    "vol_ratio_p5_ex": float(ex["vr"].quantile(0.05)),
    "vol_ratio_p95_ex": float(ex["vr"].quantile(0.95)),
    "per_ticker_vol_ratio_median_ex": {
        t: float(g["vr"].median()) for t, g in ex.groupby("ticker")},
    "close_ratio_jumps_gt50bps_ex": int(sum(
        (np.abs(np.diff(np.log(
            (g.sort_values("date")["close_bps"] / BPS + 1.0).to_numpy()))) * BPS > 50).sum()
        for _, g in ex.groupby("ticker"))),
}
res["store_grid_check"] = grid_check

# 2. Bloomberg raw CSV vs Theta: verify mislabel + identity -------------------
bbg = pd.read_csv(SWE / "data" / "bloomberg" / "sp500_ohlcv.csv")
bbg["date"] = pd.to_datetime(bbg["date"]).dt.strftime("%Y-%m-%d")
bbg["sym"] = bbg["ticker"].str.split(" ").str[0]
MAP = {"open": "high", "high": "close", "low": "low", "close": "open"}
ident = {}
n_open_gt_high = 0
n_rows = 0
for tic in TICKERS:
    th = theta_eod(tic)
    sub = bbg[bbg["sym"] == tic].set_index("date")
    n_open_gt_high += int((sub["open"] > sub["high"]).sum())
    n_rows += len(sub)
    common = sorted(set(th.index) & set(sub.index))
    t, b = th.loc[common], sub.loc[common]
    diffs = {f"bbg_{k}_vs_theta_{v}_max_abs_bps":
             float(((b[k] / t[v] - 1.0).abs() * BPS).max()) for k, v in MAP.items()}
    diffs["volume_equal_frac"] = float((b["volume"].to_numpy() == t["volume"].to_numpy()).mean())
    diffs["n_common"] = len(common)
    # labeled (uncorrected) mapping error, for contrast
    diffs["labeled_close_vs_theta_close_median_abs_bps"] = float(
        ((b["close"] / t["close"] - 1.0).abs() * BPS).median())
    ident[tic] = diffs
res["bbg_vs_theta"] = {
    "claimed_mapping": MAP,
    "bbg_rows_open_gt_high_labelled": n_open_gt_high,
    "bbg_rows_total": n_rows,
    "per_ticker": ident,
}

# 3. 2026-03-20 partial-day check, independent (TSLA + AAPL) ------------------
def bar_match(tic: str) -> dict:
    f = pd.read_parquet(STORE / f"ticker={tic}" / f"date={PARTIAL}" / "bars_5m.parquet")
    et = pd.to_datetime(f.index).tz_convert("America/New_York")
    e = theta_eod(tic).loc[PARTIAL]
    d = (f["close"] / float(e["close"]) - 1.0).abs() * BPS
    i = int(d.to_numpy().argmin())
    return {
        "best_bar_et": str(et[i]), "best_diff_bps": float(d.iloc[i]),
        "eod_close": float(e["close"]), "session_close": float(f["close"].iloc[-1]),
        "eod_low_vs_fullday_low_bps": (float(e["low"]) / float(f["low"].min()) - 1.0) * BPS,
        "eod_high_vs_running_high_bps": (float(e["high"]) / float(f["high"].iloc[:i + 1].max()) - 1.0) * BPS,
    }
res["partial_day_0320"] = {t: bar_match(t) for t in ["TSLA", "NVDA", "AAPL", "JPM"]}
res["theta_eod_last_dates"] = {t: theta_eod(t).index.max() for t in TICKERS[:3]}

# 4. IBKR SPY/QQQ recheck ------------------------------------------------------
def ibkr_recheck(sym: str) -> dict:
    d = json.loads((REPO / "data_raw" / "ibkr_primary" / f"{sym}_5m.json").read_text())
    ts = pd.to_datetime(pd.Series(d["time"]), utc=True).dt.tz_convert("America/New_York")
    f = pd.DataFrame({"ts": ts, "close": d["close"]})
    hm = f["ts"].dt.hour * 100 + f["ts"].dt.minute
    f = f[(hm >= 930) & (hm < 1600)]
    f["session"] = f["ts"].dt.date
    rows = []
    for day, g in f.groupby("session"):
        if len(g) < 78:
            continue
        sd = store_daily_raw(sym) if False else None
        rows.append({"date": str(day), "ibkr_close": float(g.sort_values("ts")["close"].iloc[-1]),
                     "n_bars": len(g)})
    ib = pd.DataFrame(rows).set_index("date")
    sd = store_daily_raw(sym)
    common = sorted(set(ib.index) & set(sd.index))
    diffs = [(sd.loc[d, "close"] / ib.loc[d, "ibkr_close"] - 1.0) * BPS for d in common]
    diffs = pd.Series(np.abs(diffs))
    return {"n_full_sessions": len(common), "dates": [common[0], common[-1]],
            "median_abs_bps": float(diffs.median()), "max_abs_bps": float(diffs.max()),
            "all_lt_2bps": bool((diffs < 2).all())}
res["ibkr"] = {s: ibkr_recheck(s) for s in ["SPY", "QQQ"]}

OUT.write_text(json.dumps(res, indent=2))
print(json.dumps(res, indent=2))
