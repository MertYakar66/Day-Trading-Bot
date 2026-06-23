"""S2 VRP workstream — part 0 (data-trap adjudication) + part (a) long-run premise.

Part 0 — adjudicate the suspected mislabel: is Theta-processed
``iv_history/{T}.parquet`` column ``iv_atm`` actually Bloomberg's REALIZED
``volatility_30d`` (mislabeled as IV), or a true implied vol (put/call), or a
mix? Tested systematically across the 12 bot-universe single names over every
overlapping date with ``data/bloomberg/sp500_vol_iv_full.csv``.

Part (a) — long-run VRP premise check (2015 -> 2026-03-20):
    daily VRP = mean(hist_put_imp_vol, hist_call_imp_vol)/100 - volatility_30d/100
per ticker: mean, % days positive, per-year means.

READ-ONLY on smart-wheel-engine. No network. Outputs under
data_raw/swe_tests/: vrp_trap_adjudication.json, vrp_longrun_per_ticker.csv,
vrp_longrun_per_year.csv.

Run:  .venv/Scripts/python.exe scripts/swe_tests/vrp_s2_adjudication.py
(cwd = repo root)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
OUT = Path("C:/Users/merty/Desktop/Day-Trading-Bot/data_raw/swe_tests")
OUT.mkdir(parents=True, exist_ok=True)

TICKERS = [
    "AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
    "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM",
]
TOL = 1e-6  # iv_history values carry 5 decimals; exact-source match is ~1e-16


def load_vol_iv_full() -> pd.DataFrame:
    vf = pd.read_csv(SWE / "data/bloomberg/sp500_vol_iv_full.csv")
    vf["date"] = pd.to_datetime(vf["date"])
    vf["base"] = vf["ticker"].str.split(" ").str[0]
    return vf


def adjudicate(ticker: str, vf: pd.DataFrame) -> dict:
    ih = pd.read_parquet(SWE / f"data_processed/theta/iv_history/{ticker}.parquet")
    ih = ih.reset_index()[["date", "iv_atm"]]
    sub = vf.loc[vf["base"] == ticker]
    j = ih.merge(sub, on="date", how="inner").dropna(
        subset=["iv_atm", "volatility_30d", "hist_put_imp_vol", "hist_call_imp_vol"]
    )
    n = len(j)
    candidates = {
        "volatility_30d_div100": j["volatility_30d"] / 100.0,
        "volatility_60d_div100": j["volatility_60d"] / 100.0,
        "volatility_90d_div100": j["volatility_90d"] / 100.0,
        "hist_put_imp_vol_div100": j["hist_put_imp_vol"] / 100.0,
        "hist_call_imp_vol_div100": j["hist_call_imp_vol"] / 100.0,
        "mean_put_call_iv_div100": (j["hist_put_imp_vol"] + j["hist_call_imp_vol"]) / 200.0,
    }
    res: dict = {"ticker": ticker, "n_days_joined": n, "candidates": {}}
    for name, series in candidates.items():
        diff = (j["iv_atm"] - series).abs()
        res["candidates"][name] = {
            "match_rate_tol1e-6": float((diff <= TOL).mean()),
            "mean_abs_diff": float(diff.mean()),
            "max_abs_diff": float(diff.max()),
            "corr": float(np.corrcoef(j["iv_atm"], series)[0, 1]) if n > 2 else None,
        }
    best = max(res["candidates"], key=lambda k: res["candidates"][k]["match_rate_tol1e-6"])
    best_rate = res["candidates"][best]["match_rate_tol1e-6"]
    res["best_match"] = best
    res["best_match_rate"] = best_rate
    res["verdict"] = (
        f"iv_atm == {best}" if best_rate >= 0.99
        else f"MIXED/UNRESOLVED (best={best} at {best_rate:.3f})"
    )
    return res


def long_run_vrp(ticker: str, vf: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    sub = vf.loc[vf["base"] == ticker].dropna(
        subset=["hist_put_imp_vol", "hist_call_imp_vol", "volatility_30d"]
    ).copy()
    sub = sub.sort_values("date")
    iv = (sub["hist_put_imp_vol"] + sub["hist_call_imp_vol"]) / 200.0
    rv = sub["volatility_30d"] / 100.0
    sub["vrp"] = iv - rv
    # Supplement (the economically meaningful test): IV today vs vol REALIZED over
    # the NEXT ~30 calendar days = volatility_30d observed 21 trading days later.
    sub["vrp_fwd"] = iv - rv.shift(-21)
    sub["year"] = sub["date"].dt.year
    per_year = sub.groupby("year")["vrp"].agg(["mean", "count"]).reset_index()
    per_year["ticker"] = ticker
    summary = {
        "ticker": ticker,
        "n_days": int(len(sub)),
        "date_min": str(sub["date"].min().date()),
        "date_max": str(sub["date"].max().date()),
        "vrp_mean": float(sub["vrp"].mean()),
        "vrp_median": float(sub["vrp"].median()),
        "vrp_std": float(sub["vrp"].std(ddof=1)),
        "pct_days_positive": float((sub["vrp"] > 0).mean() * 100.0),
        "vrp_fwd_mean": float(sub["vrp_fwd"].mean()),
        "vrp_fwd_pct_days_positive": float((sub["vrp_fwd"].dropna() > 0).mean() * 100.0),
        "vrp_fwd_n_days": int(sub["vrp_fwd"].notna().sum()),
        "n_years_positive_mean": int((per_year["mean"] > 0).sum()),
        "n_years_total": int(len(per_year)),
    }
    return summary, per_year[["ticker", "year", "mean", "count"]]


def main() -> None:
    vf = load_vol_iv_full()

    adj = [adjudicate(t, vf) for t in TICKERS]
    n_realized = sum(1 for a in adj if a["best_match"] == "volatility_30d_div100"
                     and a["best_match_rate"] >= 0.99)
    overall = {
        "n_tickers": len(TICKERS),
        "n_tickers_iv_atm_equals_realized_vol30": n_realized,
        "verdict": (
            "iv_history.iv_atm IS Bloomberg volatility_30d/100 (REALIZED vol, "
            "MISLABELED as IV) for all tested tickers"
            if n_realized == len(TICKERS) else "mixed - inspect per-ticker detail"
        ),
    }

    longrun = []
    per_year_frames = []
    for t in TICKERS:
        s, py = long_run_vrp(t, vf)
        longrun.append(s)
        per_year_frames.append(py)
    lr_df = pd.DataFrame(longrun)
    py_df = pd.concat(per_year_frames, ignore_index=True)

    # pooled: equal-weight daily mean across tickers, then per-year
    pooled_year = py_df.groupby("year").apply(
        lambda g: float(np.average(g["mean"], weights=g["count"])), include_groups=False
    )

    lr_df.to_csv(OUT / "vrp_longrun_per_ticker.csv", index=False)
    py_df.to_csv(OUT / "vrp_longrun_per_year.csv", index=False)

    payload = {
        "trap_adjudication": {"per_ticker": adj, "overall": overall},
        "long_run_vrp": {
            "definition": "mean(hist_put_imp_vol,hist_call_imp_vol)/100 - volatility_30d/100",
            "per_ticker": longrun,
            "pooled_mean_vrp": float(lr_df["vrp_mean"].mean()),
            "pooled_pct_days_positive": float(lr_df["pct_days_positive"].mean()),
            "pooled_mean_vrp_fwd": float(lr_df["vrp_fwd_mean"].mean()),
            "pooled_pct_days_positive_fwd": float(lr_df["vrp_fwd_pct_days_positive"].mean()),
            "pooled_per_year_mean": {str(k): v for k, v in pooled_year.items()},
            "n_years_pooled_positive": int((pooled_year > 0).sum()),
            "n_years_pooled_total": int(len(pooled_year)),
        },
    }
    with open(OUT / "vrp_trap_adjudication.json", "w") as f:
        json.dump(payload, f, indent=2)

    print("=== TRAP ADJUDICATION ===")
    for a in adj:
        print(f"{a['ticker']:6} n={a['n_days_joined']:5}  {a['verdict']}"
              f"  (rate={a['best_match_rate']:.4f})")
    print(overall["verdict"])
    print("\n=== LONG-RUN VRP (mean put/call IV - realized 30d) ===")
    print(lr_df[["ticker", "n_days", "vrp_mean", "pct_days_positive",
                 "vrp_fwd_mean", "vrp_fwd_pct_days_positive",
                 "n_years_positive_mean", "n_years_total"]].to_string(index=False))
    print("\npooled per-year mean VRP:")
    for y, v in pooled_year.items():
        print(f"  {y}: {v:+.4f}")
    print(f"\n(written to {OUT})")


if __name__ == "__main__":
    main()
