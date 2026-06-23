"""Build a real multi-year dealer-GEX / gamma-regime series for SPY/QQQ and test
the engine's CORE THESIS: does the dealer-gamma regime condition subsequent
realized behavior (long-gamma -> dampening / smaller next-day moves; short-gamma
-> amplifying / larger next-day moves)?

Data: SWE ``theta/index_reference/option_history`` (per-contract daily EOD OHLC +
NBBO + OI, SPY/QQQ 2018->2026), reconstructed into PIT chains with BS-inverted IV
by :mod:`intraday.data.index_chain_history`, GEX via the SWE dealer analyzer, real
prior-session 3m risk-free rate. Network-free (reads captured files only).

This is a RESEARCH harness (read-only; writes JSON under data_raw/). It needs no
option tape (no OFI) and no separate underlying feed — the parity spot reconstructed
per day IS a real EOD underlying close, so daily returns come for free and the
thesis test uses only GEX-regime + those returns.

Usage:
  python scripts/build_real_gex_series.py --symbol SPY --start 2022-01-01 --end 2026-06-10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from intraday.data import index_chain_history as ich  # noqa: E402
from intraday.data import swe_offline as so  # noqa: E402
from intraday.features.gex import gamma_structure_at  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "data_raw", "realdata_validation")


def _third_friday_expiries(expiries: list[date], start: date, end: date) -> list[date]:
    """Standard monthly expiries (3rd Friday) in [start, end] — stable-tenor anchors."""
    out = []
    for e in expiries:
        if start <= e <= end and e.weekday() == 4 and 15 <= e.day <= 21:
            out.append(e)
    return sorted(out)


def build_series(
    symbol: str, start: date, end: date, *, dte_lo: int, dte_hi: int,
    dte_target: int, strike_window: float, rfc: so.RiskFreeCurve,
) -> pd.DataFrame:
    expiries = ich.list_history_expiries(symbol)
    exp_map = dict(expiries)
    monthlies = _third_friday_expiries([e for e, _ in expiries], start, end)
    rows: list[dict] = []
    for E in monthlies:
        part = ich._read_partition(exp_map[E])
        if part.empty:
            continue
        days = sorted(
            d for d in part["created_date"].unique()
            if start <= d <= end and dte_lo <= (pd.Timestamp(E) - pd.Timestamp(d)).days <= dte_hi
        )
        for d in days:
            try:
                r = rfc.rate_on(d)
            except so.SweDataUnavailable:
                continue
            out = ich.reconstruct_chain(
                symbol, d, E, r=r, strike_window_pct=strike_window,
                min_strikes=8, _partition=part,
            )
            if out is None:
                continue
            chain, st = out
            as_of = pd.Timestamp(chain.frame["available_ts"].iloc[0]) + pd.Timedelta(seconds=1)
            gs = gamma_structure_at(chain, as_of, expiry=E, ticker=symbol, risk_free_rate=r)
            if gs is None:
                continue
            rows.append({
                "date": d.isoformat(), "expiry": E.isoformat(), "dte": st.dte,
                "spot": st.spot, "gex_total": float(gs.gex_total),
                "regime": gs.regime.value, "flip_level": gs.flip_level,
                "flip_distance_pct": gs.flip_distance_pct,
                "nearest_put_wall": gs.nearest_put_wall,
                "nearest_call_wall": gs.nearest_call_wall,
                "n_inverted": st.rows_inverted, "rate": r,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    # Dedupe a calendar day to the read closest to the target tenor.
    df["dte_err"] = (df["dte"] - dte_target).abs()
    df = df.sort_values(["date", "dte_err"]).drop_duplicates("date", keep="first")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def thesis_test(df: pd.DataFrame) -> dict:
    """Does the EOD gamma regime condition the NEXT day's realized move?

    Long-gamma (GEX>0, dampening) should precede smaller |return|; short-gamma
    (GEX<0, amplifying) larger. PIT: regime at close of D conditions D+1's return.
    """
    from scipy import stats

    s = df.copy()
    s["logspot"] = np.log(s["spot"])
    s["ret_next"] = s["logspot"].shift(-1) - s["logspot"]  # D -> D+1 log return
    s["abs_ret_next"] = s["ret_next"].abs()
    s["long_gamma"] = s["gex_total"] > 0
    # Only use consecutive-trading-day pairs (<= 5 calendar days apart) for forward return.
    s["gap_days"] = (s["date"].shift(-1) - s["date"]).dt.days
    valid = s[(s["gap_days"] <= 5) & s["abs_ret_next"].notna()].copy()

    lg = valid[valid["long_gamma"]]["abs_ret_next"]
    sg = valid[~valid["long_gamma"]]["abs_ret_next"]
    res: dict = {
        "n_total": int(len(df)),
        "n_forward_pairs": int(len(valid)),
        "n_long_gamma_days": int(len(lg)),
        "n_short_gamma_days": int(len(sg)),
        "mean_abs_ret_next_long_gamma_bps": float(lg.mean() * 1e4) if len(lg) else None,
        "mean_abs_ret_next_short_gamma_bps": float(sg.mean() * 1e4) if len(sg) else None,
        "median_abs_ret_next_long_gamma_bps": float(lg.median() * 1e4) if len(lg) else None,
        "median_abs_ret_next_short_gamma_bps": float(sg.median() * 1e4) if len(sg) else None,
    }
    if len(lg) >= 10 and len(sg) >= 10:
        u, p = stats.mannwhitneyu(sg, lg, alternative="greater")  # H1: short-gamma > long-gamma
        res["mannwhitney_p_shortgamma_gt_longgamma"] = float(p)
        res["effect_ratio_short_over_long"] = float(sg.mean() / lg.mean()) if lg.mean() else None
        # Honest interpretation
        if p < 0.05 and sg.mean() > lg.mean():
            res["verdict"] = (
                "SUPPORTS thesis: short-gamma days precede significantly larger "
                "next-day moves than long-gamma days (amplifying vs dampening)."
            )
        elif p > 0.5:
            res["verdict"] = (
                "DOES NOT SUPPORT thesis: no evidence short-gamma precedes larger "
                "next-day moves (regime does not condition realized vol here)."
            )
        else:
            res["verdict"] = (
                "INCONCLUSIVE: direction as hypothesized but not significant at 0.05."
            )
    else:
        res["verdict"] = "INSUFFICIENT_DATA: too few days in one regime to test."
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SPY", choices=["SPY", "QQQ"])
    ap.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 10))
    ap.add_argument("--dte-lo", type=int, default=20)
    ap.add_argument("--dte-hi", type=int, default=45)
    ap.add_argument("--dte-target", type=int, default=30)
    ap.add_argument("--strike-window", type=float, default=0.15)
    args = ap.parse_args()

    rfc = so.load_risk_free_curve()
    print(f"building real GEX series: {args.symbol} {args.start}..{args.end} "
          f"(tenor ~{args.dte_target}DTE, strike window +/-{args.strike_window:.0%})")
    df = build_series(
        args.symbol, args.start, args.end, dte_lo=args.dte_lo, dte_hi=args.dte_hi,
        dte_target=args.dte_target, strike_window=args.strike_window, rfc=rfc,
    )
    if df.empty:
        print("no GEX points produced (check data coverage / window)")
        return 1

    pct_long = float((df["gex_total"] > 0).mean())
    print(f"\nGEX series: {len(df)} daily points, {df['date'].min().date()}..{df['date'].max().date()}")
    print(f"  spot range {df['spot'].min():.0f}..{df['spot'].max():.0f}")
    print(f"  GEX_total range {df['gex_total'].min()/1e9:+.2f}B .. {df['gex_total'].max()/1e9:+.2f}B; "
          f"mean {df['gex_total'].mean()/1e9:+.2f}B")
    print(f"  long-gamma days: {pct_long:.0%}; regime mix: "
          f"{df['regime'].value_counts().to_dict()}")

    test = thesis_test(df)
    print("\n--- THESIS TEST: does gamma regime condition next-day realized move? ---")
    print(f"  forward pairs: {test['n_forward_pairs']} "
          f"(long-gamma {test['n_long_gamma_days']}, short-gamma {test['n_short_gamma_days']})")
    print(f"  mean |next-day ret|: long-gamma {test['mean_abs_ret_next_long_gamma_bps']:.1f} bps "
          f"vs short-gamma {test['mean_abs_ret_next_short_gamma_bps']:.1f} bps")
    if "mannwhitney_p_shortgamma_gt_longgamma" in test:
        print(f"  Mann-Whitney p (short>long): {test['mannwhitney_p_shortgamma_gt_longgamma']:.4f}; "
              f"effect ratio {test['effect_ratio_short_over_long']:.2f}x")
    print(f"  VERDICT: {test['verdict']}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = {
        "symbol": args.symbol,
        "window": [args.start.isoformat(), args.end.isoformat()],
        "params": {"dte_lo": args.dte_lo, "dte_hi": args.dte_hi,
                   "dte_target": args.dte_target, "strike_window": args.strike_window},
        "n_points": int(len(df)),
        "pct_long_gamma": pct_long,
        "gex_mean_bn": float(df["gex_total"].mean() / 1e9),
        "thesis_test": test,
        "series": json.loads(df.assign(date=df["date"].dt.strftime("%Y-%m-%d")).to_json(orient="records")),
    }
    path = os.path.join(OUT_DIR, f"real_gex_series_{args.symbol}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
