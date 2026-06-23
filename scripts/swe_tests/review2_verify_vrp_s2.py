"""Adversarial review #2 of the VRP S2 workstream (adjudication/pilot/e2e).

Independent re-derivations, NOT reruns of the workstream code paths:
- trap identity iv_atm vs volatility_30d/100 (3 tickers, full join);
- hist_put_imp_vol == hist_call_imp_vol global identity check (data-quality);
- long-run VRP recompute for AAPL + pooled per-year 2020/2022 cross-check;
- pilot summary stats recomputed from vrp_pilot_rows.csv;
- fully independent GK RV (own numpy impl, direct parquet read, own ET->UTC
  slice with the store's recorded latency) for 3 ticker-sessions at 15:00 ET
  vs the workstream CSV;
- SPY/VIX9D rows re-derived for 3 dates from vix_family.parquet + own GK;
- e2e atm_iv/spot/closes re-derived from iv_surface/chains/store directly;
- session-count audits (120 rows, 10/ticker, SPY n=32, descriptive n=108).

READ-ONLY on smart-wheel-engine. No network.
Output: data_raw/swe_tests/review2_verify_vrp_s2.json

Run:  .venv/Scripts/python.exe scripts/swe_tests/review2_verify_vrp_s2.py
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
OUT = REPO / "data_raw" / "swe_tests"
ET = ZoneInfo("America/New_York")

report: dict = {}

# ---------------------------------------------------------------- trap identity
vf = pd.read_csv(SWE / "data/bloomberg/sp500_vol_iv_full.csv")
vf["date"] = pd.to_datetime(vf["date"])
vf["base"] = vf["ticker"].str.split(" ").str[0]

trap = {}
for t in ["AAPL", "AMD", "NVDA"]:
    ih = pd.read_parquet(SWE / f"data_processed/theta/iv_history/{t}.parquet").reset_index()
    j = ih[["date", "iv_atm"]].merge(vf.loc[vf["base"] == t], on="date").dropna(
        subset=["iv_atm", "volatility_30d"])
    d = (j["iv_atm"] - j["volatility_30d"] / 100.0).abs()
    trap[t] = {"n": int(len(j)), "max_abs_diff": float(d.max()),
               "all_match_1e-6": bool((d <= 1e-6).all())}
report["trap_identity"] = trap

# put IV vs call IV: are they the same column twice?
pc = vf.dropna(subset=["hist_put_imp_vol", "hist_call_imp_vol"])
diff_pc = (pc["hist_put_imp_vol"] - pc["hist_call_imp_vol"]).abs()
report["put_call_iv_identity"] = {
    "n_rows_all_tickers": int(len(pc)),
    "n_tickers": int(pc["base"].nunique()),
    "frac_exactly_equal": float((diff_pc == 0).mean()),
    "max_abs_diff": float(diff_pc.max()),
}

# ------------------------------------------------- long-run VRP recompute (AAPL)
sub = vf.loc[vf["base"] == "AAPL"].dropna(
    subset=["hist_put_imp_vol", "hist_call_imp_vol", "volatility_30d"]).sort_values("date")
vrp = (sub["hist_put_imp_vol"] + sub["hist_call_imp_vol"]) / 200.0 - sub["volatility_30d"] / 100.0
report["longrun_aapl"] = {
    "n": int(len(sub)), "vrp_mean": float(vrp.mean()),
    "pct_pos": float((vrp > 0).mean() * 100)}

# pooled per-year recompute (all 12 tickers, day-weighted)
T12 = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
       "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM"]
allr = []
for t in T12:
    s = vf.loc[vf["base"] == t].dropna(
        subset=["hist_put_imp_vol", "hist_call_imp_vol", "volatility_30d"])
    v = (s["hist_put_imp_vol"] + s["hist_call_imp_vol"]) / 200.0 - s["volatility_30d"] / 100.0
    allr.append(pd.DataFrame({"year": s["date"].dt.year, "vrp": v, "ticker": t}))
ar = pd.concat(allr)
per_year = ar.groupby("year")["vrp"].mean()
per_ticker_mean = ar.groupby("ticker")["vrp"].mean()
report["pooled_per_year_2020_2022"] = {
    "2020": float(per_year.loc[2020]), "2022": float(per_year.loc[2022])}
report["per_ticker_n_negative"] = int((per_ticker_mean < 0).sum())
report["pooled_mean_of_ticker_means"] = float(per_ticker_mean.mean())

# ------------------------------------------- pilot rows: recompute summary stats
rows = pd.read_csv(OUT / "vrp_pilot_rows.csv")
report["pilot_counts"] = {
    "n_rows": int(len(rows)),
    "n_tickers": int(rows["ticker"].nunique()),
    "sessions_per_ticker": rows.groupby("ticker").size().agg(["min", "max"]).tolist(),
    "rv_1000_nonnull": int(rows["rv_1000_s2default"].notna().sum()),
    "vrp_1000_descr_nonnull": int(rows["vrp_1000_descriptive"].notna().sum()),
}
v15 = rows["vrp_1500"].dropna()
report["pilot_vrp_1500"] = {
    "n": int(len(v15)), "mean": float(v15.mean()), "median": float(v15.median()),
    "std": float(v15.std(ddof=1)), "min": float(v15.min()),
    "pct_pos": float((v15 > 0).mean() * 100),
    "pct_above_0.02": float((v15 > 0.02).mean() * 100)}
v10 = rows["vrp_1000_descriptive"].dropna()
report["pilot_vrp_1000_descr"] = {
    "n": int(len(v10)), "mean": float(v10.mean()),
    "pct_pos": float((v10 > 0).mean() * 100)}
gap = rows.dropna(subset=["clock_gap_rv1500_minus_vol30"])
gt = gap.groupby("ticker")["clock_gap_rv1500_minus_vol30"].mean()
report["clock_bias"] = {
    "pooled_mean_of_ticker_means": float(gt.mean()),
    "n_tickers_negative": int((gt < 0).sum()),
    "googl": float(gt.loc["GOOGL"]), "amd": float(gt.loc["AMD"])}

# decomposition: vrp_1500 ~= (prior IV - same-day vol30) + (vol30 - bot rv)
dec = rows.dropna(subset=["vrp_1500", "bbg_vol30_same_day"])
cal_spread = (dec["iv_prior_close"] - dec["bbg_vol30_same_day"]).mean()
report["decomposition"] = {
    "mean_calendar_iv_minus_vol30": float(cal_spread),
    "mean_vrp_1500": float(dec["vrp_1500"].mean()),
    "clock_gap_share": float(-gap["clock_gap_rv1500_minus_vol30"].mean()
                             / dec["vrp_1500"].mean())}

# ------------------- independent GK RV at 15:00 ET (own impl, direct parquet) --
LN2 = math.log(2.0)


def gk_rv(frame: pd.DataFrame, window: int = 30) -> float | None:
    if len(frame) < window + 1:
        return None
    t = frame.tail(window)
    hl = np.log(t["high"].values / t["low"].values) ** 2
    co = np.log(t["close"].values / t["open"].values) ** 2
    var = np.mean(0.5 * hl - (2 * LN2 - 1) * co)
    return float(math.sqrt(max(var, 0.0) * 252) * math.sqrt(78))


def pit_slice(ticker: str, d: date, hh: int, mm: int) -> pd.DataFrame:
    part = REPO / "data_raw" / "store_yahoo" / "bars" / f"ticker={ticker}" / f"date={d}"
    frame = pd.read_parquet(part / "bars_5m.parquet")
    meta = json.loads((part / "bars_5m_meta.json").read_text())
    lat = pd.Timedelta(meta.get("latency", "0s")) if isinstance(meta.get("latency"), str) \
        else pd.Timedelta(meta.get("latency_ms", meta.get("latency", 0)), unit="ms")
    as_of = pd.Timestamp(datetime.combine(d, time(hh, mm)), tz=ET).tz_convert("UTC")
    return frame.loc[frame.index <= as_of - lat], meta


checks = []
for t, d in [("AAPL", date(2026, 3, 16)), ("NVDA", date(2026, 3, 12)),
             ("XOM", date(2026, 3, 20))]:
    sl, meta = pit_slice(t, d, 15, 0)
    mine = gk_rv(sl)
    row = rows.loc[(rows["ticker"] == t) & (rows["date"] == str(d))].iloc[0]
    theirs = float(row["rv_1500_s2default"])
    # prior-close IV recompute
    s = vf.loc[(vf["base"] == t) & (vf["date"] < pd.Timestamp(d))].sort_values("date")
    iv_prev = float((s["hist_put_imp_vol"].iloc[-1] + s["hist_call_imp_vol"].iloc[-1]) / 200.0)
    checks.append({
        "ticker": t, "date": str(d), "n_bars_pit_1500": int(len(sl)),
        "meta": meta, "rv_mine": mine, "rv_theirs": theirs,
        "rv_abs_diff": abs(mine - theirs) if mine is not None else None,
        "iv_prior_mine": iv_prev, "iv_prior_theirs": float(row["iv_prior_close"]),
        "vrp_mine": iv_prev - mine, "vrp_theirs": float(row["vrp_1500"]),
    })
report["independent_rv_checks"] = checks

# 10:00 slice bar count + structural-None confirmation (window=30 -> needs 31)
sl10, _ = pit_slice("AAPL", date(2026, 3, 16), 10, 0)
report["bars_available_at_1000"] = int(len(sl10))
report["rv_none_at_1000_structurally"] = bool(len(sl10) < 31)
# first non-None RV time: 31st bar closes 09:30 + 31*5m = 12:05 ET
sl1205, _ = pit_slice("AAPL", date(2026, 3, 16), 12, 6)
report["bars_available_at_1206"] = int(len(sl1205))

# --------------------------------------------------- SPY / VIX9D re-derivation
vix = pd.read_parquet(SWE / "data_processed/theta/vix_family/vix_family.parquet")
v9 = vix.loc[vix["symbol"] == "VIX9D", "close"].sort_index()
v9.index = pd.to_datetime(v9.index).date
spy_csv = pd.read_csv(OUT / "vrp_spy_vix9d_timeseries.csv")
spy_checks = []
for ds in ["2026-03-09", "2026-04-01", "2026-04-22"]:
    d = date.fromisoformat(ds)
    prior = max(k for k in v9.index if k < d)
    sl, _ = pit_slice("SPY", d, 15, 0)
    mine_rv = gk_rv(sl)
    row = spy_csv.loc[spy_csv["date"] == ds].iloc[0]
    spy_checks.append({
        "date": ds, "vix9d_prior_mine": float(v9.loc[prior]) / 100.0,
        "vix9d_prior_theirs": float(row["vix9d_prior_close_div100"]),
        "prior_date_mine": str(prior), "prior_date_theirs": row["vix9d_prior_date"],
        "rv_mine": mine_rv, "rv_theirs": float(row["spy_rv_1500_s2default"]),
        "vrp_mine": float(v9.loc[prior]) / 100.0 - mine_rv,
        "vrp_theirs": float(row["vrp_proxy"])})
report["spy_vix9d_checks"] = spy_checks
report["spy_n_sessions_csv"] = int(len(spy_csv))
vp = spy_csv["vrp_proxy"].dropna()
report["spy_vrp_stats"] = {"n": int(len(vp)), "mean": float(vp.mean()),
                           "min": float(vp.min()), "max": float(vp.max()),
                           "pct_pos": float((vp > 0).mean() * 100)}

# --------------------------------------------------------------- e2e re-derive
surf = pd.read_parquet(SWE / "data_processed/theta/iv_surface/AAPL_20260601.parquet")
chains = pd.read_parquet(SWE / "data_processed/theta/chains/AAPL_20260601.parquet")
spot_vals = chains["underlying_price"].unique()
nearest_exp = surf["expiration"].min()
one = surf.loc[surf["expiration"] == nearest_exp]
spot = float(spot_vals[0])
strikes = np.sort(one["strike"].unique())
near = strikes[np.argmin(np.abs(strikes - spot))]
atm = one.loc[one["strike"] == near]
ivs = atm["iv"].astype(float)
ivs = ivs[ivs > 0]
aapl_0601 = pd.read_parquet(
    REPO / "data_raw/store_yahoo/bars/ticker=AAPL/date=2026-06-01/bars_5m.parquet")
aapl_0529 = pd.read_parquet(
    REPO / "data_raw/store_yahoo/bars/ticker=AAPL/date=2026-05-29/bars_5m.parquet")
report["e2e"] = {
    "n_expiries_in_surface": int(surf["expiration"].nunique()),
    "expiries": sorted(str(pd.Timestamp(e).date()) for e in surf["expiration"].unique())[:6],
    "nearest_expiry": str(pd.Timestamp(nearest_exp).date()),
    "n_rows_nearest": int(len(one)),
    "spot_unique_in_chains": [float(s) for s in spot_vals[:3]],
    "atm_strike_mine": float(near),
    "atm_iv_mine": float(ivs.mean()),
    "atm_n_quotes": int(len(atm)),
    "dte_nearest": int(one["dte"].iloc[0]),
    "store_close_0601": float(aapl_0601["close"].iloc[-1]),
    "store_close_0529": float(aapl_0529["close"].iloc[-1]),
    "has_underlying_timestamp_col": "underlying_timestamp" in chains.columns,
}
# e2e RV: full 06-01 session, own GK
report["e2e_rv_mine"] = gk_rv(aapl_0601)

with open(OUT / "review2_verify_vrp_s2.json", "w") as f:
    json.dump(report, f, indent=2, default=str)
print(json.dumps(report, indent=2, default=str))
