"""Reviewer #1 independent verification of regime_stratification workstream.

Checks (no network; SWE read-only):
 A. Hash-compare saved outputs vs the workstream's rerun dirs.
 B. Recompute every bucket stat + Welch contrast from the saved sessions CSV and
    compare to the saved JSON (tolerance 1e-9 relative).
 C. Recompute VIX tercile breaks; recount bucket n's.
 D. PIT check: re-derive every session label straight from the vix_family parquet
    (last close strictly BEFORE the session date); report index dtype/tz.
 E. Store bar coverage for the 10 zero-PnL sessions (all 8 symbols) and the
    session calendar (expected 32 sessions, Good Friday 2026-04-03 absent).
 F. Sanity: vix_family max date; could session 2026-04-23 have been labeled?

Run: .venv/Scripts/python.exe scripts/swe_tests/review1_verify_regime_stratification.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
from scipy import stats as _ss

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT = os.path.join(_ROOT, "data_raw", "swe_tests")
CSV = os.path.join(OUT, "regime_stratification_sessions.csv")
JSN = os.path.join(OUT, "regime_stratification_results.json")
VIX_FAMILY = ("C:/Users/merty/Desktop/smart-wheel-engine/data_processed/theta/"
              "vix_family/vix_family.parquet")
SYMS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "GLD", "TLT")

report: dict = {"checks": {}}
fails: list[str] = []


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# A. hash compare
pairs = {}
for sub in ("rerun_20260610", "review_rerun"):
    for fn in ("regime_stratification_results.json", "regime_stratification_sessions.csv"):
        a, b = os.path.join(OUT, fn), os.path.join(OUT, sub, fn)
        pairs[f"{sub}/{fn}"] = (sha(a) == sha(b)) if os.path.exists(b) else None
report["checks"]["A_hash_identical"] = pairs
if not all(v for v in pairs.values() if v is not None):
    fails.append("A: rerun outputs not byte-identical")

# B/C. recompute stats from CSV
tbl = pd.read_csv(CSV, parse_dates=["session", "prior_close_date"])
with open(JSN) as f:
    saved = json.load(f)

STRATS = ("s3", "s4", "s5")
schemes = {"vix_tercile": ["low", "mid", "high"],
           "scheme_9d": ["contango_9d", "backwardation_9d"],
           "scheme_3m": ["contango_3m", "flat_or_inverted_3m"]}


def stats_of(x):
    n = len(x)
    mean = float(np.mean(x)); sd = float(np.std(x, ddof=1))
    se = sd / np.sqrt(n); t = mean / se
    p = float(2 * _ss.t.sf(abs(t), df=n - 1))
    return dict(n=n, mean=mean, se=se, t=t, p=p, total=float(np.sum(x)),
                win=int((np.asarray(x) > 0).sum()))


mism = []
def close(a, b, tol=1e-9):
    if a is None or b is None:
        return a == b
    if isinstance(a, (int,)) and isinstance(b, (int,)):
        return a == b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


for s in STRATS:
    x = tbl[f"pnl_{s}"].to_numpy(float)
    mine = stats_of(x); ref = saved["unconditional"][s]
    for k_m, k_r in (("n", "n"), ("mean", "mean_pnl_per_day"), ("se", "se"),
                     ("t", "t"), ("p", "p"), ("total", "total_pnl"), ("win", "win_days")):
        if not close(mine[k_m], ref[k_r]):
            mism.append(f"uncond {s}.{k_r}: mine={mine[k_m]} saved={ref[k_r]}")
    for sc, bks in schemes.items():
        arrs = {}
        for b in bks:
            xb = tbl.loc[tbl[sc] == b, f"pnl_{s}"].to_numpy(float)
            arrs[b] = xb
            mine = stats_of(xb); ref = saved["conditional"][s][sc][b]
            for k_m, k_r in (("n", "n"), ("mean", "mean_pnl_per_day"), ("se", "se"),
                             ("t", "t"), ("p", "p")):
                if not close(mine[k_m], ref[k_r]):
                    mism.append(f"cond {s}.{sc}.{b}.{k_r}: mine={mine[k_m]} saved={ref[k_r]}")
        a, b = bks[0], bks[-1]
        tt, pp = _ss.ttest_ind(arrs[a], arrs[b], equal_var=False)
        refc = saved["contrasts"][s][sc]
        if not (close(float(tt), refc["t"]) and close(float(pp), refc["p"])
                and close(float(np.mean(arrs[a]) - np.mean(arrs[b])), refc["mean_diff"])):
            mism.append(f"contrast {s}.{sc}: mine t={tt} p={pp} saved={refc}")
report["checks"]["B_stat_mismatches"] = mism
if mism:
    fails.append(f"B: {len(mism)} stat mismatches")

q1, q2 = np.quantile(tbl["vix"], [1 / 3, 2 / 3])
report["checks"]["C_tercile_breaks"] = {
    "mine": [float(q1), float(q2)], "saved": saved["tercile_breaks_vix"],
    "match": close(float(q1), saved["tercile_breaks_vix"][0])
             and close(float(q2), saved["tercile_breaks_vix"][1]),
    "bucket_counts": tbl["vix_tercile"].value_counts().to_dict(),
}
if not report["checks"]["C_tercile_breaks"]["match"]:
    fails.append("C: tercile breaks mismatch")

# D. PIT re-derivation from parquet
vf = pd.read_parquet(VIX_FAMILY)
report["checks"]["D_vf_index"] = {
    "dtype": str(vf.index.dtype), "tz": str(getattr(vf.index, "tz", None)),
    "min": str(vf.index.min()), "max": str(vf.index.max()),
    "dupes_per_day_symbol": int(vf.reset_index().duplicated(
        subset=[vf.index.name or "index", "symbol"]).sum()),
}
closes = vf.pivot_table(index=vf.index, columns="symbol", values="close").sort_index()
pit_bad = []
for _, r in tbl.iterrows():
    d = pd.Timestamp(r["session"])
    prior = closes[closes.index < d]
    last = prior.iloc[-1]
    if prior.index[-1].date() != r["prior_close_date"].date():
        pit_bad.append(f"{d.date()}: prior date {prior.index[-1].date()} vs csv {r['prior_close_date'].date()}")
    for col, csvcol in (("VIX", "vix"), ("VIX9D", "vix9d"), ("VIX3M", "vix3m")):
        if abs(float(last[col]) - float(r[csvcol])) > 1e-9:
            pit_bad.append(f"{d.date()}: {col} {last[col]} vs csv {r[csvcol]}")
    # PIT hard check: label date strictly before session
    if not prior.index[-1] < d:
        pit_bad.append(f"{d.date()}: label not strictly prior")
report["checks"]["D_pit_violations"] = pit_bad
if pit_bad:
    fails.append(f"D: {len(pit_bad)} PIT/label mismatches")

# E. store coverage
from intraday.contracts import DataSource  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.data.store_provider import StoreBackedProvider  # noqa: E402

store = ParquetStore(os.path.join(_ROOT, "data_raw", "store_yahoo"))
start, end = date(2026, 3, 9), date(2026, 4, 22)
days_by_sym = {}
for sym in SYMS:
    prov = StoreBackedProvider(store, DataSource.YAHOO, symbols=[sym], interval="5m")
    days_by_sym[sym] = [str(d) for d in prov.trading_days(start, end)]
n_days = {s: len(v) for s, v in days_by_sym.items()}
union_days = sorted(set().union(*[set(v) for v in days_by_sym.values()]))
zero_sessions = tbl.loc[(tbl[[f"pnl_{s}" for s in STRATS]] == 0).all(axis=1), "session"]
zero_sessions = [str(d.date()) for d in zero_sessions]
zero_cov = {d: all(d in days_by_sym[s] for s in SYMS) for d in zero_sessions}
report["checks"]["E_store"] = {
    "n_days_per_symbol": n_days,
    "n_union_days": len(union_days),
    "good_friday_absent": "2026-04-03" not in union_days,
    "zero_pnl_sessions": zero_sessions,
    "zero_sessions_bars_all8": zero_cov,
    "zero_in_contango_3m": int((tbl.loc[(tbl[[f"pnl_{s}" for s in STRATS]] == 0).all(axis=1),
                                        "scheme_3m"] == "contango_3m").sum()),
}
if len(union_days) != len(tbl):
    fails.append(f"E: store union days {len(union_days)} != labeled sessions {len(tbl)}")
if not all(zero_cov.values()):
    fails.append("E: some zero-PnL session lacks bars for all 8 symbols")

# F. could 2026-04-23 have been labeled?
report["checks"]["F_next_labelable"] = {
    "vf_max_date": str(closes.index.max().date()),
    "store_has_20260423": "2026-04-23" in [str(d) for d in
        StoreBackedProvider(store, DataSource.YAHOO, symbols=["SPY"], interval="5m")
        .trading_days(date(2026, 4, 23), date(2026, 4, 23))],
}

report["fails"] = fails
out_path = os.path.join(OUT, "review1_verify_regime_stratification.json")
with open(out_path, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(json.dumps(report, indent=2, default=str))
print("\nVERDICT:", "FAIL: " + "; ".join(fails) if fails else "ALL CHECKS PASS")
