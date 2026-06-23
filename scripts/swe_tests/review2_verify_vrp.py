"""Reviewer #2 — independent verification of the VRP S2 workstream.

Recomputes, with INDEPENDENT code (own Garman-Klass, own PIT slicing, raw
parquet/CSV reads — no reuse of the workstream's helper functions):
  0. data-trap identity iv_atm == volatility_30d/100 (2 spot tickers full-join
     + all-12 match rates),
  a. long-run VRP pooled/per-ticker/per-year stats,
  b. pilot rv@15:00 / vrp@15:00 for all 120 ticker-sessions,
  c. clock gap rv1500 - same-day vol30/100,
  d. SPY vs prior-close VIX9D/100 (32 sessions),
  e. e2e AAPL numbers (ATM IV, RV from 06-01 session, VRP).
Compares each against the workstream's saved outputs in data_raw/swe_tests/.

Run:  .venv/Scripts/python.exe scripts/swe_tests/review2_verify_vrp.py
(cwd = repo root). READ-ONLY on smart-wheel-engine.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
OUT = REPO / "data_raw" / "swe_tests"
ET = ZoneInfo("America/New_York")
TICKERS = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
           "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM"]
LATENCY = pd.Timedelta(milliseconds=250)  # store sidecar latency_ms=250

report: dict = {}


def my_gk_annualized(bars: pd.DataFrame, window: int = 30) -> float | None:
    """Independent GK: last `window` bars, ann = sqrt(var * 252 * 78)."""
    if bars is None or len(bars) < window + 1:  # mirror intraday_rv gate
        return None
    t = bars.tail(window)
    hl = np.log(t["high"].values / t["low"].values) ** 2
    co = np.log(t["close"].values / t["open"].values) ** 2
    var = np.mean(0.5 * hl - (2 * math.log(2) - 1) * co)
    return float(math.sqrt(max(var, 0.0) * 252.0 * 78.0))


def bars_for(ticker: str, d: date) -> pd.DataFrame | None:
    p = (REPO / "data_raw" / "store_yahoo" / "bars" / f"ticker={ticker}"
         / f"date={d.isoformat()}" / "bars_5m.parquet")
    return pd.read_parquet(p) if p.exists() else None


def pit_slice(bars: pd.DataFrame, d: date, hh: int, mm: int) -> pd.DataFrame:
    as_of = pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=ET).tz_convert("UTC")
    return bars.loc[bars.index <= as_of - LATENCY]


# ---------- 0. trap identity, independent ---------- #
vf = pd.read_csv(SWE / "data/bloomberg/sp500_vol_iv_full.csv")
vf["date"] = pd.to_datetime(vf["date"])
vf["base"] = vf["ticker"].str.split(" ").str[0]

trap = {}
for t in TICKERS:
    ih = pd.read_parquet(SWE / f"data_processed/theta/iv_history/{t}.parquet")
    ihr = ih.reset_index()
    dcol = "date" if "date" in ihr.columns else ihr.columns[0]
    j = ihr[[dcol, "iv_atm"]].merge(
        vf.loc[vf["base"] == t], left_on=dcol, right_on="date", how="inner"
    ).dropna(subset=["iv_atm", "volatility_30d"])
    diff = (j["iv_atm"] - j["volatility_30d"] / 100.0).abs()
    ivd = (j["iv_atm"] - (j["hist_put_imp_vol"] + j["hist_call_imp_vol"]) / 200.0).abs()
    trap[t] = {"n": int(len(j)), "match_rate_vol30": float((diff <= 1e-6).mean()),
               "max_abs_diff_vol30": float(diff.max()),
               "match_rate_meanIV": float((ivd <= 1e-6).mean())}
report["trap"] = trap

# ---------- a. long-run, independent ---------- #
lr = {}
peryear_pool = []
for t in TICKERS:
    sub = vf.loc[vf["base"] == t].dropna(
        subset=["hist_put_imp_vol", "hist_call_imp_vol", "volatility_30d"]
    ).sort_values("date").copy()
    iv = (sub["hist_put_imp_vol"] + sub["hist_call_imp_vol"]) / 200.0
    rv = sub["volatility_30d"] / 100.0
    v = iv - rv
    lr[t] = {"n": int(len(sub)), "mean": float(v.mean()),
             "pct_pos": float((v > 0).mean() * 100),
             "fwd_mean": float((iv - rv.shift(-21)).mean())}
    g = pd.DataFrame({"y": sub["date"].dt.year, "v": v})
    peryear_pool.append(g)
allg = pd.concat(peryear_pool)
pooled_year = allg.groupby("y")["v"].mean()
report["longrun"] = {
    "pooled_mean": float(np.mean([lr[t]["mean"] for t in TICKERS])),
    "n_neg_tickers": int(sum(lr[t]["mean"] < 0 for t in TICKERS)),
    "pct_pos_range": [min(lr[t]["pct_pos"] for t in TICKERS),
                      max(lr[t]["pct_pos"] for t in TICKERS)],
    "pooled_fwd_mean": float(np.mean([lr[t]["fwd_mean"] for t in TICKERS])),
    "per_year": {str(k): round(float(x), 5) for k, x in pooled_year.items()},
    "n_years_pos": int((pooled_year > 0).sum()),
    "per_ticker": lr,
}

# ---------- b/c. pilot, independent ---------- #
vfd = vf.copy()
vfd["d"] = vfd["date"].dt.date
bbg = {t: vfd.loc[vfd["base"] == t].set_index("d").sort_index() for t in TICKERS}
sessions = [date(2026, 3, d) for d in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]

rows = []
for t in TICKERS:
    sub = bbg[t]
    for d in sessions:
        bars = bars_for(t, d)
        if bars is None:
            continue
        prior_idx = [k for k in sub.index if k < d]
        iv_prev = float((sub.loc[prior_idx[-1], "hist_put_imp_vol"]
                         + sub.loc[prior_idx[-1], "hist_call_imp_vol"]) / 200.0)
        s10 = pit_slice(bars, d, 10, 0)
        r10 = my_gk_annualized(s10)
        s15 = pit_slice(bars, d, 15, 0)
        r15 = my_gk_annualized(s15)
        vol30_same = (float(sub.loc[d, "volatility_30d"]) / 100.0
                      if d in sub.index else None)
        rows.append({"ticker": t, "date": str(d), "n10": len(s10), "n15": len(s15),
                     "iv_prev": iv_prev, "rv10": r10, "rv15": r15,
                     "vrp15": iv_prev - r15 if r15 is not None else None,
                     "gap": (r15 - vol30_same) if (r15 is not None and vol30_same is not None) else None})
mine = pd.DataFrame(rows)
theirs = pd.read_csv(OUT / "vrp_pilot_rows.csv")
m = mine.merge(theirs, on=["ticker", "date"], how="outer", indicator=True)
report["pilot"] = {
    "n_mine": int(len(mine)), "n_theirs": int(len(theirs)),
    "merge_all_matched": bool((m["_merge"] == "both").all()),
    "rv10_all_none_mine": bool(mine["rv10"].isna().all()),
    "rv10_all_none_theirs": bool(theirs["rv_1000_s2default"].isna().all()),
    "max_abs_diff_rv15": float((m["rv15"] - m["rv_1500_s2default"]).abs().max()),
    "max_abs_diff_iv_prev": float((m["iv_prev"] - m["iv_prior_close"]).abs().max()),
    "max_abs_diff_vrp15": float((m["vrp15"] - m["vrp_1500"]).abs().max()),
    "max_abs_diff_gap": float((m["gap"] - m["clock_gap_rv1500_minus_vol30"]).abs().max()),
    "vrp15_mean_mine": float(mine["vrp15"].mean()),
    "vrp15_pct_pos_mine": float((mine["vrp15"] > 0).mean() * 100),
    "vrp15_pct_above_002_mine": float((mine["vrp15"] > 0.02).mean() * 100),
    "vrp15_min_mine": float(mine["vrp15"].min()),
    "gap_pooled_mean_mine": float(mine.groupby("ticker")["gap"].mean().mean()),
    "gap_n_tickers_neg_mine": int((mine.groupby("ticker")["gap"].mean() < 0).sum()),
    "n15_bars_used": sorted(mine["n15"].unique().tolist()),
    "iv_prior_dates_lag1_ok": bool(
        (pd.to_datetime(theirs["iv_prior_date"]).dt.date
         < pd.to_datetime(theirs["date"]).dt.date).all()),
}

# ---------- d. SPY vs VIX9D, independent ---------- #
vix = pd.read_parquet(SWE / "data_processed/theta/vix_family/vix_family.parquet")
v9 = vix.loc[vix["symbol"] == "VIX9D", "close"].sort_index()
v9.index = pd.to_datetime(v9.index).date
spy_dir = REPO / "data_raw" / "store_yahoo" / "bars" / "ticker=SPY"
spy_days = sorted(date.fromisoformat(p.name.split("=")[1]) for p in spy_dir.glob("date=*")
                  if (p / "bars_5m.parquet").exists()
                  and date(2026, 3, 9) <= date.fromisoformat(p.name.split("=")[1]) <= date(2026, 4, 22))
srows = []
for d in spy_days:
    bars = bars_for("SPY", d)
    prior = [k for k in v9.index if k < d]
    r15 = my_gk_annualized(pit_slice(bars, d, 15, 0))
    srows.append({"date": str(d), "v9_prior": float(v9.loc[prior[-1]]) / 100.0,
                  "v9_date": str(prior[-1]), "rv15": r15,
                  "vrp": float(v9.loc[prior[-1]]) / 100.0 - r15})
smine = pd.DataFrame(srows)
stheirs = pd.read_csv(OUT / "vrp_spy_vix9d_timeseries.csv")
sm = smine.merge(stheirs, on="date", how="outer", indicator=True)
report["spy_vix9d"] = {
    "n_mine": int(len(smine)), "n_theirs": int(len(stheirs)),
    "merge_all_matched": bool((sm["_merge"] == "both").all()),
    "max_abs_diff_vrp": float((sm["vrp"] - sm["vrp_proxy"]).abs().max()),
    "max_abs_diff_v9": float((sm["v9_prior"] - sm["vix9d_prior_close_div100"]).abs().max()),
    "prior_dates_strictly_before": bool(
        (pd.to_datetime(smine["v9_date"]).dt.date < pd.to_datetime(smine["date"]).dt.date).all()),
    "mean_vrp_mine": float(smine["vrp"].mean()),
    "min_vrp_mine": float(smine["vrp"].min()),
    "max_vrp_mine": float(smine["vrp"].max()),
    "pct_pos_mine": float((smine["vrp"] > 0).mean() * 100),
    "vix9d_last_date_on_disk": str(max(v9.index)),
}

# ---------- e. e2e, independent ---------- #
surf = pd.read_parquet(SWE / "data_processed/theta/iv_surface/AAPL_20260601.parquet")
one = surf.loc[surf["expiration"] == surf["expiration"].min()]
spot = 306.43
near = (one["strike"].astype(float) - spot).abs().min()
atm = one.loc[(one["strike"].astype(float) - spot).abs() <= near + 1e-9]
ivs = atm["iv"].astype(float)
my_atm_iv = float(ivs[ivs > 0].mean())
b0601 = bars_for("AAPL", date(2026, 6, 1))
# at 06-02 10:00 ET every 06-01 bar (last close 16:00 ET 06-01) is available
my_rv = my_gk_annualized(b0601)
e2e = json.load(open(OUT / "vrp_e2e_result.json"))
b0529 = bars_for("AAPL", date(2026, 5, 29))
report["e2e"] = {
    "my_atm_iv": my_atm_iv, "their_atm_iv": e2e["atm_iv"],
    "my_rv": my_rv, "their_rv": e2e["rv"],
    "my_vrp": my_atm_iv - my_rv, "their_vrp": e2e["vrp"],
    "store_close_0601": float(b0601["close"].iloc[-1]),
    "store_close_0529": float(b0529["close"].iloc[-1]),
    "spot": spot,
    "n_bars_0601": int(len(b0601)),
}

with open(OUT / "review2_verify_vrp.json", "w") as f:
    json.dump(report, f, indent=2, default=str)
print(json.dumps(report, indent=2, default=str))
