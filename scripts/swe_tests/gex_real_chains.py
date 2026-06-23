"""REAL-DATA GEX workstream: run intraday.features.gex (the bot's gamma spine)
on real Theta EOD option chains from the read-only smart-wheel-engine repo.

What this does, per (ticker, snapshot):
  1. Loads the raw Theta chain parquet (SPX / SPXW / NDX index chains, AAPL
     single-name chain).
  2. Builds the bot's PIT contract ``OptionChainSeries`` with the DATA-MAP
     column mapping; ``available_ts`` = next NYSE session open AFTER the
     snapshot's data day (point-in-time discipline).
  3. Pre-filters to the single nearest expiry >= the trading day (audit
     caveat #1: the analyzer blends expiries if handed a multi-expiry frame).
  4. Calls ``intraday.features.gex.gamma_structure_at`` at as_of =
     trading-day 10:00 ET.
  5. VALIDATES: (a) recomputes net GEX manually from raw iv/strike/OI with an
     independent Black-Scholes gamma; (b) compares analyzer walls with the
     argmax-OI strikes in the raw file; (c) sanity-checks the GEX sign against
     the (gamma-dollar-weighted) put/call OI balance.
  6. MEASURES the multi-expiry blend bug by concatenating SPX+SPXW (different
     expiries) and re-running without the expiry pre-filter.
  7. Reports OI data quality per file (zero-OI fraction, OI excluded by the
     analyzer's iv<=0 filter, single-strike concentration).

UNITS (from vendor/swe engine/dealer_positioning.py, byte-identical to
smart-wheel-engine): gex_total = sum over strikes of
    sign * gamma_BS(per-share) * OI * 100 * spot^2 * 0.01
i.e. DOLLARS OF DEALER DELTA-HEDGE NOTIONAL PER 1% SPOT MOVE (SpotGamma
convention), under DealerAssumption.LONG_CALLS_SHORT_PUTS (call sign +1,
put sign -1). Positive => dealers long gamma => vol-dampening.

PIT mapping (data day -> first tradable session), with evidence:
  snapshot_date 20260423 -> data day 2026-04-22 (underlying_timestamp in file:
      2026-04-22T16:05 index / T19:59 equity) -> trade day 2026-04-23
  snapshot_date 20260524 -> data day 2026-05-22 (Sunday snapshot; last session
      was Fri 5/22; AAPL spot 307.27 consistent with 5/22 after-hours vs RTH
      close 308.88) -> trade day 2026-05-26 (Mon 5/25 = Memorial Day)
  snapshot_date 20260601 -> data day 2026-06-01 (SAME-DAY close: AAPL spot
      306.43 matches 6/1 close 306.32, NOT 5/29 close 312.07; SPX 7596.05
      matches 6/1 close ~7600, not 5/29 ~7581) -> trade day 2026-06-02
  snapshot_date 20260605 -> data day 2026-06-05 ASSUMED (same-day convention
      as 20260601; no on-disk bars past 6/2 to verify; conservative = latest
      possible data day) -> trade day 2026-06-08

Run from the repo root with the project venv:
    .venv/Scripts/python.exe scripts/swe_tests/gex_real_chains.py
Outputs:
    data_raw/swe_tests/gex_real_chains_results.json
    data_raw/swe_tests/gex_real_chains_summary.csv
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
sys.path.insert(0, str(REPO))

import intraday  # noqa: F401  (side effect: puts vendor/swe on sys.path)
from intraday.contracts import CHAIN_COLUMNS, DataSource, OptionChainSeries
from intraday.features.gex import gamma_structure_at

from engine.dealer_positioning import DealerPositioningAnalyzer  # vendor/swe

THETA = Path("C:/Users/merty/Desktop/smart-wheel-engine/data_processed/theta")
OUT = REPO / "data_raw" / "swe_tests"
OUT.mkdir(parents=True, exist_ok=True)

RISK_FREE = 0.04  # gamma_structure_at default — manual recompute must match
ET = "America/New_York"

NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}

# snapshot_date key -> (data day of the EOD chain, evidence)
DATA_DAY: dict[str, tuple[date, str]] = {
    "20260423": (date(2026, 4, 22), "underlying_timestamp in file (2026-04-22T16:05 / T19:59)"),
    "20260524": (date(2026, 5, 22), "Sunday snapshot; last session Fri 5/22; AAPL spot 307.27 ~ 5/22 after-hours"),
    "20260601": (date(2026, 6, 1), "spot matches 6/1 close (AAPL 306.43 vs 306.32; 5/29 close was 312.07) — SAME-DAY EOD"),
    "20260605": (date(2026, 6, 5), "ASSUMED same-day convention (like 20260601); no bars on disk to verify"),
}

RUNS = [
    ("SPX", "index_options_chains/SPX_20260423.parquet", "20260423"),
    ("SPX", "index_options_chains/SPX_20260524.parquet", "20260524"),
    ("SPX", "index_options_chains/SPX_20260601.parquet", "20260601"),
    ("SPXW", "index_options_chains/SPXW_20260423.parquet", "20260423"),
    ("SPXW", "index_options_chains/SPXW_20260524.parquet", "20260524"),
    ("SPXW", "index_options_chains/SPXW_20260601.parquet", "20260601"),
    # NDX_20260423 has a raw-quote schema (no iv, no underlying_price) — skipped, see data_quality.
    ("NDX", "index_options_chains/NDX_20260524.parquet", "20260524"),
    ("NDX", "index_options_chains/NDX_20260601.parquet", "20260601"),
    ("AAPL", "chains/AAPL_20260423.parquet", "20260423"),
    ("AAPL", "chains/AAPL_20260524.parquet", "20260524"),
    ("AAPL", "chains/AAPL_20260601.parquet", "20260601"),
    ("AAPL", "chains/AAPL_20260605.parquet", "20260605"),
]

SKIPPED = [
    {
        "ticker": "NDX",
        "file": "index_options_chains/NDX_20260423.parquet",
        "reason": "raw NBBO-quote schema: no iv, no greeks, no underlying_price/underlying_timestamp — "
                  "the bot's chain contract requires implied_vol and spot; cannot run the gamma spine "
                  "without inventing an IV solver + parity-implied spot (out of scope).",
    }
]


def next_session(d: date) -> date:
    """First NYSE session strictly after calendar day d."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5 or nxt in NYSE_HOLIDAYS_2026:
        nxt += timedelta(days=1)
    return nxt


def et_ts(d: date, hh: int, mm: int, ss: int = 0) -> pd.Timestamp:
    return pd.Timestamp(f"{d}T{hh:02d}:{mm:02d}:{ss:02d}", tz=ET).tz_convert("UTC")


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Independent Black-Scholes gamma (q=0). gamma = phi(d1) / (S sigma sqrt(T))."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return phi / (S * sigma * sqrt_T)


def manual_net_gex(raw: pd.DataFrame, spot: float, expiry: date, as_of_day: date,
                   r: float = RISK_FREE) -> dict:
    """Recompute net GEX from the raw chain rows, replicating the analyzer's
    validity filters and T convention, but with an independent gamma.

    Analyzer filters: strike>0, iv notna, 0<iv<5, OI notna. T = max(days,1)/365.
    Per-strike the analyzer averages IV across rows within (strike, type) and
    multiplies one gamma by the summed OI; the files have no duplicate
    (strike, right) rows, so rowwise == grouped here (we compute both).
    """
    T = max((expiry - as_of_day).days, 1) / 365.0
    df = raw.copy()
    df["right"] = df["right"].astype(str).str.lower().str.strip()
    valid = df[(df["strike"] > 0) & df["iv"].notna() & (df["iv"] > 0)
               & (df["iv"] < 5.0) & df["open_interest"].notna()]

    def gex_rows(sub: pd.DataFrame) -> float:
        total = 0.0
        for _, row in sub.iterrows():
            sign = 1.0 if row["right"].startswith("c") else -1.0
            g = bs_gamma(spot, float(row["strike"]), T, r, float(row["iv"]))
            total += sign * g * float(row["open_interest"]) * 100.0 * spot * spot * 0.01
        return total

    rowwise = gex_rows(valid)
    grp = (valid.groupby(["strike", "right"], as_index=False)
                .agg(iv=("iv", "mean"), open_interest=("open_interest", "sum")))
    grouped = gex_rows(grp)
    return {
        "T_years": T,
        "n_valid_rows": int(len(valid)),
        "manual_gex_rowwise": rowwise,
        "manual_gex_grouped": grouped,
    }


def oi_quality(raw: pd.DataFrame) -> dict:
    """Per-file OI data-quality stats (computed on the raw, unfiltered rows)."""
    oi = raw["open_interest"].fillna(0).astype(float)
    total = float(oi.sum())
    zero_frac = float((oi == 0).mean())
    if "iv" in raw.columns:
        bad_iv = raw["iv"].isna() | (raw["iv"] <= 0) | (raw["iv"] >= 5.0)
        oi_lost = float(oi[bad_iv].sum())
        oi_lost_frac = oi_lost / total if total > 0 else float("nan")
    else:
        oi_lost_frac = float("nan")
    by_strike = raw.groupby("strike")["open_interest"].sum()
    top_share = float(by_strike.max() / total) if total > 0 else float("nan")
    return {
        "rows": int(len(raw)),
        "zero_oi_row_frac": zero_frac,
        "total_oi_contracts": int(total),
        "oi_excluded_by_iv_filter_frac": oi_lost_frac,
        "top_strike_oi_share": top_share,
    }


def wall_vs_oi_check(raw: pd.DataFrame, ms) -> dict:
    """Compare analyzer walls (net-GEX extrema) with raw argmax-OI strikes."""
    calls = raw[raw["right"].astype(str).str.lower().str.startswith("c")]
    puts = raw[raw["right"].astype(str).str.lower().str.startswith("p")]
    top_call_oi = (calls.groupby("strike")["open_interest"].sum()
                        .sort_values(ascending=False).head(3))
    top_put_oi = (puts.groupby("strike")["open_interest"].sum()
                       .sort_values(ascending=False).head(3))
    call_wall_strikes = [w.strike for w in ms.call_walls]
    put_wall_strikes = [w.strike for w in ms.put_walls]
    return {
        "top3_call_oi_strikes": [float(k) for k in top_call_oi.index],
        "top3_call_oi_values": [int(v) for v in top_call_oi.values],
        "top3_put_oi_strikes": [float(k) for k in top_put_oi.index],
        "top3_put_oi_values": [int(v) for v in top_put_oi.values],
        "analyzer_call_walls": [float(s) for s in call_wall_strikes],
        "analyzer_put_walls": [float(s) for s in put_wall_strikes],
        "top_call_wall_in_top3_oi": bool(call_wall_strikes
                                         and call_wall_strikes[0] in set(top_call_oi.index)),
        "top_put_wall_in_top3_oi": bool(put_wall_strikes
                                        and put_wall_strikes[0] in set(top_put_oi.index)),
    }


def sign_sanity(raw: pd.DataFrame, spot: float, expiry: date, as_of_day: date,
                gex_total: float) -> dict:
    """GEX sign vs put/call balance. Under LONG_CALLS_SHORT_PUTS the sign of
    gex_total must equal sign(sum call gamma-dollars - sum put gamma-dollars)."""
    T = max((expiry - as_of_day).days, 1) / 365.0
    df = raw.copy()
    df["right"] = df["right"].astype(str).str.lower().str.strip()
    valid = df[(df["strike"] > 0) & df["iv"].notna() & (df["iv"] > 0)
               & (df["iv"] < 5.0) & df["open_interest"].notna()]
    gd = {}
    for side in ("c", "p"):
        sub = valid[valid["right"].str.startswith(side)]
        tot = 0.0
        for _, row in sub.iterrows():
            tot += (bs_gamma(spot, float(row["strike"]), T, RISK_FREE, float(row["iv"]))
                    * float(row["open_interest"]) * 100.0 * spot * spot * 0.01)
        gd[side] = tot
    call_oi = int(valid[valid["right"].str.startswith("c")]["open_interest"].sum())
    put_oi = int(valid[valid["right"].str.startswith("p")]["open_interest"].sum())
    expected_sign = float(np.sign(gd["c"] - gd["p"]))
    return {
        "call_oi": call_oi,
        "put_oi": put_oi,
        "put_call_oi_ratio": (put_oi / call_oi) if call_oi else float("nan"),
        "call_gamma_dollars": gd["c"],
        "put_gamma_dollars": gd["p"],
        "expected_gex_sign": expected_sign,
        "actual_gex_sign": float(np.sign(gex_total)),
        "sign_consistent": bool(np.sign(gex_total) == expected_sign),
    }


def build_series(raw: pd.DataFrame, ticker: str, snap_key: str) -> tuple[OptionChainSeries, date, pd.Timestamp]:
    """Map the Theta parquet into the bot's OptionChainSeries (PIT-correct)."""
    data_day, _ = DATA_DAY[snap_key]
    if "underlying_timestamp" in raw.columns:
        uts = pd.Timestamp(str(raw["underlying_timestamp"].iloc[0]))
        # Timestamps like 2026-04-22T16:05 (index) / T19:59 (equity) are
        # consistent with ET wall-clock around/after the close; treat as
        # ET-naive. (If they were UTC the PIT mapping is unchanged: either
        # way the data is post-close of data_day.)
        snapshot_ts = uts.tz_localize(ET).tz_convert("UTC")
        assert snapshot_ts.tz_convert(ET).date() == data_day, (ticker, snap_key)
    else:
        snapshot_ts = et_ts(data_day, 16, 0)
    trade_day = next_session(data_day)
    available_ts = et_ts(trade_day, 9, 30)  # next session open after snapshot

    frame = pd.DataFrame({
        "snapshot_ts": snapshot_ts,
        "available_ts": available_ts,
        "expiration": pd.to_datetime(raw["expiration"]).dt.date,
        "strike": raw["strike"].astype(float),
        "option_type": raw["right"].astype(str),
        "open_interest": raw["open_interest"].fillna(0).astype(float),
        "implied_vol": raw["iv"].astype(float),
        "spot": raw["underlying_price"].astype(float),
    })[list(CHAIN_COLUMNS)]
    return OptionChainSeries(symbol=ticker, frame=frame, source=DataSource.THETA), trade_day, snapshot_ts


def gs_to_dict(gs) -> dict:
    return {
        "as_of_utc": str(gs.as_of),
        "spot": gs.spot,
        "gex_total_usd_per_1pct": gs.gex_total,
        "regime": gs.regime.value,
        "confidence": gs.confidence,
        "regime_multiplier": gs.regime_multiplier,
        "flip_level": gs.flip_level,
        "flip_distance_pct": (gs.flip_distance_pct * 100.0
                              if gs.flip_distance_pct is not None else None),
        "nearest_call_wall": gs.nearest_call_wall,
        "nearest_put_wall": gs.nearest_put_wall,
    }


def run_one(ticker: str, rel: str, snap_key: str) -> dict:
    raw = pd.read_parquet(THETA / rel)
    series, trade_day, snapshot_ts = build_series(raw, ticker, snap_key)
    as_of = et_ts(trade_day, 10, 0)  # trading-day 10:00 ET

    # Pre-filter to the nearest expiry >= trading day (audit caveat #1).
    exps = sorted({e for e in series.frame["expiration"].unique() if e >= trade_day})
    if not exps:
        raise RuntimeError(f"{ticker} {snap_key}: no expiry >= {trade_day}")
    expiry = exps[0]
    filt = series.frame[series.frame["expiration"] == expiry]
    chain = OptionChainSeries(symbol=ticker, frame=filt, source=DataSource.THETA)

    t0 = time.time()
    gs = gamma_structure_at(chain, as_of, expiry=expiry, ticker=ticker)
    spine_secs = time.time() - t0
    assert gs is not None, f"{ticker} {snap_key}: spine returned None (PIT gate failed?)"

    # Direct analyzer call on the identical inputs (the spine's own code path)
    # to expose full walls / diagnostics for validation.
    snap = chain.latest_available(as_of)
    cdf = snap[["strike", "option_type", "open_interest", "implied_vol"]].copy()
    as_of_naive = as_of.tz_convert("UTC").tz_localize(None).to_pydatetime()
    ms = DealerPositioningAnalyzer(risk_free_rate=RISK_FREE).analyze(
        chain=cdf, spot=gs.spot, expiry=expiry, ticker=ticker, as_of=as_of_naive)
    assert abs(ms.gex_total - gs.gex_total) < 1e-6 * max(1.0, abs(gs.gex_total))

    raw_exp = raw[pd.to_datetime(raw["expiration"]).dt.date == expiry]
    manual = manual_net_gex(raw_exp, gs.spot, expiry, trade_day)
    gap_abs = gs.gex_total - manual["manual_gex_rowwise"]
    gap_pct = (gap_abs / abs(manual["manual_gex_rowwise"]) * 100.0
               if manual["manual_gex_rowwise"] else float("nan"))

    res = {
        "ticker": ticker,
        "file": rel,
        "snapshot_date_key": snap_key,
        "data_day": str(DATA_DAY[snap_key][0]),
        "data_day_evidence": DATA_DAY[snap_key][1],
        "snapshot_ts_utc": str(snapshot_ts),
        "trade_day": str(trade_day),
        "available_ts_utc": str(et_ts(trade_day, 9, 30)),
        "expiry": str(expiry),
        "dte_calendar_days": (expiry - trade_day).days,
        "n_expiries_in_file": int(pd.to_datetime(raw["expiration"]).dt.date.nunique()),
        "spine": gs_to_dict(gs),
        "spine_wall_note": "nearest_*_wall is None unless a wall lies within 5% of spot (analyzer near_wall_pct)",
        "analyzer_diag": {
            "n_strikes": ms.n_strikes, "n_calls": ms.n_calls, "n_puts": ms.n_puts,
            "dex_total_usd": ms.dex_total,
            "call_walls": [{"strike": w.strike, "net_gex": w.net_gex,
                            "dist_pct": w.distance_pct * 100} for w in ms.call_walls],
            "put_walls": [{"strike": w.strike, "net_gex": w.net_gex,
                           "dist_pct": w.distance_pct * 100} for w in ms.put_walls],
        },
        "validation_gex_recompute": {
            **manual,
            "spine_gex_total": gs.gex_total,
            "gap_abs_usd": gap_abs,
            "gap_pct": gap_pct,
        },
        "validation_walls_vs_oi": wall_vs_oi_check(raw_exp, ms),
        "validation_sign": sign_sanity(raw_exp, gs.spot, expiry, trade_day, gs.gex_total),
        "oi_quality": oi_quality(raw_exp),
        "spine_runtime_secs": spine_secs,
    }
    return res


def blend_test(date_key: str) -> dict:
    """Audit caveat #1 on real data: concatenate SPX + SPXW (two different
    expiries) and run gamma_structure_at WITHOUT expiry pre-filtering, passing
    expiry = the nearest of the two (what a naive caller would do). Compare
    with the correctly filtered nearest-expiry-only run."""
    raws = {}
    for tkr in ("SPX", "SPXW"):
        raw = pd.read_parquet(THETA / f"index_options_chains/{tkr}_{date_key}.parquet")
        series, trade_day, _ = build_series(raw, tkr, date_key)
        raws[tkr] = series.frame
    as_of = et_ts(trade_day, 10, 0)

    combined = pd.concat([raws["SPX"], raws["SPXW"]], ignore_index=True)
    # Identical snapshot_ts so latest_available keeps every row.
    combined["snapshot_ts"] = raws["SPX"]["snapshot_ts"].iloc[0]
    expiries = sorted({e for e in combined["expiration"].unique() if e >= trade_day})
    nearest = expiries[0]

    filt = combined[combined["expiration"] == nearest]
    chain_f = OptionChainSeries(symbol="SPX+SPXW", frame=filt, source=DataSource.THETA)
    gs_single = gamma_structure_at(chain_f, as_of, expiry=nearest, ticker="SPX/SPXW")

    chain_b = OptionChainSeries(symbol="SPX+SPXW", frame=combined, source=DataSource.THETA)
    gs_blend = gamma_structure_at(chain_b, as_of, expiry=nearest, ticker="SPX/SPXW")

    spot = gs_single.spot
    flip_move_pct = (None if gs_single.flip_level is None or gs_blend.flip_level is None
                     else (gs_blend.flip_level - gs_single.flip_level) / spot * 100.0)
    return {
        "date_key": date_key,
        "trade_day": str(trade_day),
        "expiries_blended": [str(e) for e in expiries],
        "nearest_expiry_passed_as_T": str(nearest),
        "single_expiry": gs_to_dict(gs_single),
        "blended_no_filter": gs_to_dict(gs_blend),
        "gex_total_change_pct": ((gs_blend.gex_total - gs_single.gex_total)
                                 / abs(gs_single.gex_total) * 100.0
                                 if gs_single.gex_total else float("nan")),
        "flip_level_move_pct_of_spot": flip_move_pct,
        "regime_changed": gs_single.regime != gs_blend.regime,
    }


def main() -> None:
    results = {
        "meta": {
            "generated_utc": pd.Timestamp.utcnow().isoformat(),
            "script": "scripts/swe_tests/gex_real_chains.py",
            "risk_free_rate": RISK_FREE,
            "dealer_assumption": "long_calls_short_puts (call sign +1, put sign -1; SpotGamma convention)",
            "gex_units": "USD of dealer delta-hedge notional per 1% spot move: "
                         "sign * gamma_BS_per_share * OI * 100 * spot^2 * 0.01",
            "pit_note": "available_ts = next NYSE session open after the snapshot's data day; "
                        "as_of = trading-day 10:00 ET. All runs tradable-PIT, none same-day.",
            "engine_note": "vendor/swe/engine/dealer_positioning.py is byte-identical to "
                           "smart-wheel-engine/engine/dealer_positioning.py (verified by diff); "
                           "T floored at 1 calendar day (irrelevant here: min DTE = 22d).",
        },
        "skipped": SKIPPED,
        "runs": [],
        "blend_tests": [],
        "ndx_20260423_oi_quality": None,
    }

    for ticker, rel, snap_key in RUNS:
        print(f"--- {ticker} {snap_key} ...", flush=True)
        res = run_one(ticker, rel, snap_key)
        results["runs"].append(res)
        s = res["spine"]
        print(f"    spot={s['spot']:.2f} gex={s['gex_total_usd_per_1pct']:+.3e} "
              f"regime={s['regime']} conf={s['confidence']:.2f} "
              f"flip={s['flip_level']} gap%={res['validation_gex_recompute']['gap_pct']:.4f}",
              flush=True)

    # OI quality of the skipped NDX file (it does carry open_interest).
    ndx_raw = pd.read_parquet(THETA / "index_options_chains/NDX_20260423.parquet")
    results["ndx_20260423_oi_quality"] = oi_quality(ndx_raw)

    for date_key in ("20260423", "20260524", "20260601"):
        print(f"--- blend test {date_key} ...", flush=True)
        results["blend_tests"].append(blend_test(date_key))

    def _default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        return str(o)

    out_json = OUT / "gex_real_chains_results.json"
    out_json.write_text(json.dumps(results, indent=2, default=_default))

    rows = []
    for r in results["runs"]:
        s = r["spine"]
        rows.append({
            "ticker": r["ticker"], "snapshot": r["snapshot_date_key"],
            "data_day": r["data_day"], "trade_day": r["trade_day"],
            "expiry": r["expiry"], "dte_d": r["dte_calendar_days"],
            "spot": s["spot"], "gex_total_usd_per_1pct": s["gex_total_usd_per_1pct"],
            "regime": s["regime"], "confidence": s["confidence"],
            "regime_multiplier": s["regime_multiplier"],
            "flip_level": s["flip_level"], "flip_distance_pct": s["flip_distance_pct"],
            "nearest_call_wall": s["nearest_call_wall"],
            "nearest_put_wall": s["nearest_put_wall"],
            "manual_gex_gap_pct": r["validation_gex_recompute"]["gap_pct"],
            "sign_consistent": r["validation_sign"]["sign_consistent"],
            "call_wall_in_top3_oi": r["validation_walls_vs_oi"]["top_call_wall_in_top3_oi"],
            "put_wall_in_top3_oi": r["validation_walls_vs_oi"]["top_put_wall_in_top3_oi"],
            "zero_oi_row_frac": r["oi_quality"]["zero_oi_row_frac"],
            "total_oi": r["oi_quality"]["total_oi_contracts"],
            "oi_lost_to_iv_filter_frac": r["oi_quality"]["oi_excluded_by_iv_filter_frac"],
        })
    pd.DataFrame(rows).to_csv(OUT / "gex_real_chains_summary.csv", index=False)
    print(f"\nwrote {out_json}")
    print(f"wrote {OUT / 'gex_real_chains_summary.csv'}")


if __name__ == "__main__":
    main()
