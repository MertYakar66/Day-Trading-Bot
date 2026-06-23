"""Rigorously re-test the gamma-regime thesis on a real GEX series, CONTROLLING
for the obvious confound: volatility clustering.

The naive test (short-gamma days precede larger next-day moves) is confounded —
GEX turns negative in high-vol regimes, and high vol auto-correlates, so larger
next-day moves could be pure vol clustering, NOT dealer gamma hedging. This script
asks the honest question: does the gamma regime predict the next-day move *after*
controlling for the recent realized-vol level?

Two controls:
1. **Multivariate OLS** ``|ret_next| ~ const + short_gamma + |ret_recent|`` — does
   the short_gamma coefficient stay positive & significant once recent vol is in?
2. **Within-vol-tercile** Mann-Whitney — inside each recent-vol tercile, do
   short-gamma days still precede larger next-day moves?

Reads ``data_raw/realdata_validation/real_gex_series_<SYM>.json`` (from
``build_real_gex_series.py``). Pure numpy/scipy OLS — no statsmodels dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data_raw", "realdata_validation")


def _ols_with_tstats(y: np.ndarray, X: np.ndarray, names: list[str]) -> dict:
    """OLS with classical t-stats. X includes the intercept column."""
    n, k = X.shape
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = n - k
    sigma2 = float(resid @ resid) / dof
    xtx_inv = np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(sigma2 * xtx_inv))
    tvals = beta / se
    pvals = 2 * (1 - stats.t.cdf(np.abs(tvals), dof))
    return {
        nm: {"coef": float(b), "se": float(s), "t": float(t), "p": float(p)}
        for nm, b, s, t, p in zip(names, beta, se, tvals, pvals)
    }


def analyze(symbol: str) -> dict:
    path = os.path.join(OUT_DIR, f"real_gex_series_{symbol}.json")
    with open(path) as fh:
        blob = json.load(fh)
    df = pd.DataFrame(blob["series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df["logspot"] = np.log(df["spot"])
    df["ret"] = df["logspot"].diff()                      # same-day (D-1 -> D) return
    df["ret_next"] = df["logspot"].shift(-1) - df["logspot"]  # D -> D+1 (forward)
    df["abs_ret_next"] = df["ret_next"].abs()
    # Recent realized-vol proxy available AT D's close (PIT): rolling std of the
    # last 5 same-day returns (no forward data).
    df["recent_vol"] = df["ret"].rolling(5).std()
    df["short_gamma"] = (df["gex_total"] < 0).astype(float)
    df["gap"] = (df["date"].shift(-1) - df["date"]).dt.days

    s = df[(df["gap"] <= 5) & df["abs_ret_next"].notna() & df["recent_vol"].notna()].copy()
    n = len(s)
    out: dict = {"symbol": symbol, "n_usable": int(n),
                 "window": blob.get("window"), "n_series": blob.get("n_points")}
    if n < 40:
        out["verdict"] = "INSUFFICIENT_DATA"
        return out

    # 1) Naive (replicate)
    lg = s[s["short_gamma"] == 0]["abs_ret_next"]
    sg = s[s["short_gamma"] == 1]["abs_ret_next"]
    u, p_naive = stats.mannwhitneyu(sg, lg, alternative="greater")
    out["naive"] = {
        "mean_next_long_bps": float(lg.mean() * 1e4),
        "mean_next_short_bps": float(sg.mean() * 1e4),
        "ratio": float(sg.mean() / lg.mean()) if lg.mean() else None,
        "mannwhitney_p": float(p_naive),
        "n_long": int(len(lg)), "n_short": int(len(sg)),
    }

    # 2) Multivariate OLS controlling for recent vol
    y = s["abs_ret_next"].to_numpy()
    X = np.column_stack([np.ones(n), s["short_gamma"].to_numpy(), s["recent_vol"].to_numpy()])
    ols = _ols_with_tstats(y, X, ["const", "short_gamma", "recent_vol"])
    out["ols_controlled"] = ols

    # 3) Within-vol-tercile Mann-Whitney
    s = s.copy()
    s["vol_tercile"] = pd.qcut(s["recent_vol"], 3, labels=["low", "mid", "high"], duplicates="drop")
    terciles = {}
    for t in s["vol_tercile"].cat.categories:
        sub = s[s["vol_tercile"] == t]
        lg_t = sub[sub["short_gamma"] == 0]["abs_ret_next"]
        sg_t = sub[sub["short_gamma"] == 1]["abs_ret_next"]
        rec = {"n_long": int(len(lg_t)), "n_short": int(len(sg_t)),
               "mean_long_bps": float(lg_t.mean() * 1e4) if len(lg_t) else None,
               "mean_short_bps": float(sg_t.mean() * 1e4) if len(sg_t) else None}
        if len(lg_t) >= 8 and len(sg_t) >= 8:
            _, p_t = stats.mannwhitneyu(sg_t, lg_t, alternative="greater")
            rec["mannwhitney_p"] = float(p_t)
        terciles[str(t)] = rec
    out["within_vol_terciles"] = terciles

    # Honest verdict: the gamma signal is REAL only if it survives the vol control.
    sg_coef = ols["short_gamma"]
    survives = sg_coef["coef"] > 0 and sg_coef["p"] < 0.05
    if survives:
        out["verdict"] = (
            f"SURVIVES vol-clustering control: short_gamma OLS coef "
            f"{sg_coef['coef']*1e4:+.1f} bps (p={sg_coef['p']:.3f}) with recent_vol "
            "in the model — the gamma regime adds predictive power beyond vol "
            "clustering. Thesis supported (still a realized-vol relationship, not a "
            "proven tradeable edge)."
        )
    elif sg_coef["coef"] > 0:
        out["verdict"] = (
            f"WEAKENED by vol control: short_gamma coef positive ({sg_coef['coef']*1e4:+.1f} "
            f"bps) but NOT significant (p={sg_coef['p']:.3f}) once recent_vol is "
            "controlled — the naive result is largely vol clustering, not an "
            "independent gamma effect."
        )
    else:
        out["verdict"] = (
            "DOES NOT SURVIVE: after controlling for recent vol the gamma sign has "
            "no positive effect — the naive finding is confounded by vol clustering."
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    args = ap.parse_args()
    results = {}
    for sym in args.symbols:
        try:
            r = analyze(sym)
        except FileNotFoundError:
            print(f"{sym}: no series JSON (run build_real_gex_series.py --symbol {sym})")
            continue
        results[sym] = r
        print(f"\n===== {sym} ({r.get('n_series')} series pts, {r['n_usable']} usable forward pairs) =====")
        if "naive" in r:
            nv = r["naive"]
            print(f"  naive: short {nv['mean_next_short_bps']:.1f} vs long "
                  f"{nv['mean_next_long_bps']:.1f} bps, ratio {nv['ratio']:.2f}x, p={nv['mannwhitney_p']:.4f}")
            sg = r["ols_controlled"]["short_gamma"]
            rv = r["ols_controlled"]["recent_vol"]
            print(f"  OLS (control recent_vol): short_gamma coef {sg['coef']*1e4:+.1f} bps "
                  f"(t={sg['t']:.2f}, p={sg['p']:.3f}); recent_vol t={rv['t']:.2f}")
            for t, rec in r["within_vol_terciles"].items():
                pp = rec.get("mannwhitney_p")
                pp_s = f"p={pp:.3f}" if pp is not None else "n/a"
                print(f"    vol tercile {t:>4}: short {rec['mean_short_bps']} vs long {rec['mean_long_bps']} bps ({pp_s})")
        print(f"  VERDICT: {r['verdict']}")

    path = os.path.join(OUT_DIR, "gex_thesis_controlled.json")
    with open(path, "w") as fh:
        json.dump(results, fh, indent=1)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
