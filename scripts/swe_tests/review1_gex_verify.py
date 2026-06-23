"""Adversarial review #1 of scripts/swe_tests/gex_real_chains.py.

Independent checks (no reuse of the harness's helper code):
  1. sha256 byte-compare vendor/swe engine files vs smart-wheel-engine (read-only).
  2. Interrogate each parquet directly: underlying_timestamp, underlying_price,
     n expiries, OI totals / zero-OI fraction / OI lost to iv<=0 filter, iv scale.
  3. Cross-check claimed data-day spots against on-disk bars:
     AAPL closes (store_yahoo) on 5/22, 5/29, 6/1; SPX closes (IBKR json) 5/29, 6/1.
  4. Fully independent vectorized GEX recompute for ALL 12 runs (own BS gamma,
     own filters, numpy) vs the JSON's spine_gex_total.
  5. Independent check of the regime multiplier formula 1 + 0.05*confidence.

Writes: data_raw/swe_tests/review1_gex_verify.json
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

BOT = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
THETA = SWE / "data_processed/theta"
OUT = BOT / "data_raw/swe_tests"

report: dict = {}

# ---------------------------------------------------------------- 1. vendor identity
def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def sha_norm(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

vend = {}
for name in ("dealer_positioning.py", "option_pricer.py"):
    a = BOT / "vendor/swe/engine" / name
    b = SWE / "engine" / name
    vend[name] = {
        "byte_identical": sha(a) == sha(b),
        "identical_mod_crlf": sha_norm(a) == sha_norm(b),
    }
report["vendor_identity"] = vend

# ---------------------------------------------------------------- 2. parquet interrogation
FILES = {
    ("SPX", "20260423"): "index_options_chains/SPX_20260423.parquet",
    ("SPX", "20260524"): "index_options_chains/SPX_20260524.parquet",
    ("SPX", "20260601"): "index_options_chains/SPX_20260601.parquet",
    ("SPXW", "20260423"): "index_options_chains/SPXW_20260423.parquet",
    ("SPXW", "20260524"): "index_options_chains/SPXW_20260524.parquet",
    ("SPXW", "20260601"): "index_options_chains/SPXW_20260601.parquet",
    ("NDX", "20260524"): "index_options_chains/NDX_20260524.parquet",
    ("NDX", "20260601"): "index_options_chains/NDX_20260601.parquet",
    ("AAPL", "20260423"): "chains/AAPL_20260423.parquet",
    ("AAPL", "20260524"): "chains/AAPL_20260524.parquet",
    ("AAPL", "20260601"): "chains/AAPL_20260601.parquet",
    ("AAPL", "20260605"): "chains/AAPL_20260605.parquet",
}

pq = {}
for (tkr, key), rel in FILES.items():
    df = pd.read_parquet(THETA / rel)
    if "underlying_timestamp" in df.columns:
        uts = sorted(df["underlying_timestamp"].astype(str).unique())
    else:
        uts = [f"MISSING_COLUMN (cols={sorted(df.columns.tolist())})"]
    oi = df["open_interest"].fillna(0).astype(float)
    bad_iv = df["iv"].isna() | (df["iv"] <= 0) | (df["iv"] >= 5.0)
    pq[f"{tkr}_{key}"] = {
        "underlying_timestamp_min": uts[0],
        "underlying_timestamp_max": uts[-1],
        "spot_unique": sorted(df["underlying_price"].astype(float).unique().tolist()),
        "n_expiries": int(pd.to_datetime(df["expiration"]).dt.date.nunique()),
        "expiries": sorted({str(e) for e in pd.to_datetime(df["expiration"]).dt.date.unique()}),
        "rows": int(len(df)),
        "total_oi": int(oi.sum()),
        "zero_oi_row_frac": float((oi == 0).mean()),
        "oi_lost_to_iv_filter_frac": float(oi[bad_iv].sum() / oi.sum()) if oi.sum() else None,
        "iv_median_of_valid": float(df.loc[~bad_iv, "iv"].median()),
        "iv_max": float(df["iv"].max()),
        "n_iv_le0": int((df["iv"] <= 0).sum()),
        "n_iv_ge5": int((df["iv"] >= 5).sum()),
    }
report["parquet_facts"] = pq

ndx0423 = pd.read_parquet(THETA / "index_options_chains/NDX_20260423.parquet")
report["ndx_20260423_columns"] = sorted(ndx0423.columns.tolist())

# ---------------------------------------------------------------- 3. bar cross-checks
aapl_files = sorted((BOT / "data_raw/store_yahoo/bars/ticker=AAPL").rglob("*.parquet"))
aapl = pd.concat([pd.read_parquet(p) for p in aapl_files], ignore_index=False)
if isinstance(aapl.index, pd.DatetimeIndex):
    aapl = aapl.reset_index()
    aapl.columns = [str(c) if str(c) != "index" else "ts" for c in aapl.columns]
ts_col = None
for c in ("ts", "timestamp", "datetime", "date", "time"):
    if c in aapl.columns:
        ts_col = c
        break
if ts_col is None and isinstance(aapl.index, pd.DatetimeIndex):
    aapl = aapl.reset_index().rename(columns={"index": "ts"})
    ts_col = "ts"
t = pd.to_datetime(aapl[ts_col], utc=True).dt.tz_convert("America/New_York")
aapl = aapl.assign(_et=t)
rth = aapl[(t.dt.time >= pd.Timestamp("09:30").time()) & (t.dt.time < pd.Timestamp("16:00").time())]
aapl_closes = {}
for d in (date(2026, 5, 22), date(2026, 5, 29), date(2026, 6, 1)):
    day = rth[rth["_et"].dt.date == d].sort_values("_et")
    aapl_closes[str(d)] = float(day["close"].iloc[-1]) if len(day) else None
report["aapl_rth_closes_store_yahoo"] = aapl_closes

spx_raw = json.loads((BOT / "data_raw/ibkr/SPX_5m_3mo.json").read_text())
def extract_bars(obj):
    if isinstance(obj, list):
        return obj
    for k in ("bars", "data", "candles"):
        if isinstance(obj, dict) and k in obj:
            return obj[k]
    raise ValueError(f"unknown json shape: {type(obj)} keys={list(obj)[:10] if isinstance(obj, dict) else None}")
if isinstance(spx_raw, dict) and "time" in spx_raw and "close" in spx_raw:
    cols = {k: v for k, v in spx_raw.items() if isinstance(v, list)}
    sdf = pd.DataFrame(cols)
else:
    sdf = pd.DataFrame(extract_bars(spx_raw))
tk = [c for c in sdf.columns if c.lower() in ("t", "ts", "time", "timestamp", "date", "datetime")][0]
numeric_time = pd.api.types.is_numeric_dtype(sdf[tk])
tt = pd.to_datetime(sdf[tk], utc=True, unit="s" if numeric_time else None)
sdf["_et"] = tt.dt.tz_convert("America/New_York")
ck = [c for c in sdf.columns if c.lower() in ("c", "close")][0]
spx_closes = {}
for d in (date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 2)):
    day = sdf[(sdf["_et"].dt.date == d)
              & (sdf["_et"].dt.time >= pd.Timestamp("09:30").time())
              & (sdf["_et"].dt.time < pd.Timestamp("16:00").time())].sort_values("_et")
    spx_closes[str(d)] = float(day[ck].iloc[-1]) if len(day) else None
report["spx_rth_closes_ibkr"] = spx_closes
report["spx_ibkr_range_et"] = [str(sdf["_et"].min()), str(sdf["_et"].max())]

# ---------------------------------------------------------------- 4. independent GEX recompute
saved = json.loads((OUT / "gex_real_chains_results.json").read_text().replace("NaN", "null"))
runs = {(r["ticker"], r["snapshot_date_key"]): r for r in saved["runs"]}

def my_gex(df: pd.DataFrame, spot: float, T: float, r: float = 0.04) -> float:
    d = df[(df["strike"] > 0) & df["iv"].notna() & (df["iv"] > 0) & (df["iv"] < 5.0)
           & df["open_interest"].notna()].copy()
    K = d["strike"].to_numpy(float)
    iv = d["iv"].to_numpy(float)
    oi = d["open_interest"].to_numpy(float)
    sign = np.where(d["right"].astype(str).str.lower().str.startswith("c"), 1.0, -1.0)
    sq = np.sqrt(T)
    d1 = (np.log(spot / K) + (r + 0.5 * iv * iv) * T) / (iv * sq)
    gamma = np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) / (spot * iv * sq)
    return float(np.sum(sign * gamma * oi * 100.0 * spot * spot * 0.01))

recheck = {}
for (tkr, key), rel in FILES.items():
    saved_run = runs[(tkr, key)]
    df = pd.read_parquet(THETA / rel)
    expiry = pd.Timestamp(saved_run["expiry"]).date()
    df = df[pd.to_datetime(df["expiration"]).dt.date == expiry]
    trade_day = pd.Timestamp(saved_run["trade_day"]).date()
    T = max((expiry - trade_day).days, 1) / 365.0
    spot = float(df["underlying_price"].iloc[0])
    mine = my_gex(df, spot, T)
    theirs = saved_run["spine"]["gex_total_usd_per_1pct"]
    recheck[f"{tkr}_{key}"] = {
        "spot_file": spot,
        "spot_saved": saved_run["spine"]["spot"],
        "T_years": T,
        "my_gex": mine,
        "saved_spine_gex": theirs,
        "rel_gap": abs(mine - theirs) / max(1.0, abs(theirs)),
        "regime_mult_formula_ok": abs(
            saved_run["spine"]["regime_multiplier"]
            - (1 + 0.05 * saved_run["spine"]["confidence"])) < 1e-12,
    }
report["independent_gex_recompute"] = recheck
report["max_rel_gap"] = max(v["rel_gap"] for v in recheck.values())

out = OUT / "review1_gex_verify.json"
out.write_text(json.dumps(report, indent=2, default=str))
print(json.dumps(report, indent=2, default=str))
print(f"\nwrote {out}", file=sys.stderr)
