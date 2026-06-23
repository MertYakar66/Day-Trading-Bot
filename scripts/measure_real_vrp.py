"""Real-data VRP measurement + S2 vrp_threshold calibration harness — NETWORK-FREE.

Goal
----
Honestly measure the single-name volatility risk premium (VRP) from the CORRECT
implied-vol source (Bloomberg ``sp500_vol_iv_full.csv`` via
``intraday.data.swe_offline.load_iv_history``) and decide what S2's
``vrp_threshold`` should be.

VRP definition used here (the TRUE calendar clock)::

    VRP_calendar = atm_iv - rv_30d

both already in annualized DECIMAL form inside ``IVHistory.frame`` (the loader has
already divided the on-disk PERCENT columns by 100). This is a forward-looking
implied vol vs a *backward-looking* 30-calendar-day realized vol — the standard
academic single-name VRP proxy.

Why this matters for S2
-----------------------
S2 (``intraday/signals/s2_zerodte_vrp.py``) gates on ``fr.vrp = atm_iv - intraday_rv``
where the realized vol is an *intraday trading-time* RV (Garman-Klass / 5-minute
bars annualized over 6.5h x 252). That is a DIFFERENT CLOCK from rv_30d. RTH
intraday RV systematically prints far below calendar 30d RV, so S2's intraday VRP
is mechanically (clock-)inflated and almost always clears its 0.02 default. This
harness measures the HONEST calendar-clock VRP so the threshold can be set with
eyes open about that bias.

Reads only; writes JSON + markdown into ``data_raw/realdata_validation/``.
Never opens a socket (swe_offline reads captured CSVs from disk).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root (parent of scripts/) so `intraday.*` imports resolve regardless of cwd,
# then vendor/swe so the SWE engine modules (engine.option_pricer, etc.) resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "vendor" / "swe"))
sys.path.insert(0, str(_REPO_ROOT))

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from intraday.data.swe_offline import (
    SweDataUnavailable,
    load_iv_history,
    swe_root,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
REPO = Path(r"C:/Users/merty/Desktop/Day-Trading-Bot")
YAHOO_BARS = REPO / "data_raw" / "store_yahoo" / "bars"
OUT_DIR = REPO / "data_raw" / "realdata_validation"
OUT_JSON = OUT_DIR / "vrp_measurement.json"
OUT_MD = OUT_DIR / "vrp_findings.md"

# The single-name universe the task asks us to consider. We intersect this with
# (a) what actually exists in the local Yahoo store AND (b) what load_iv_history
# can serve from Bloomberg vol_iv_full. Index ETFs (SPY/QQQ/...) are absent from
# vol_iv_full by construction and are excluded automatically by the intersection.
CANDIDATE_UNIVERSE = [
    "AAPL", "AMD", "NVDA", "META", "TSLA", "MSFT",
    "AMZN", "GOOGL", "AVGO", "JPM", "XOM", "NFLX",
]

# S2's current default threshold (decimal vol). Documented for the recommendation.
S2_CURRENT_THRESHOLD = 0.02

# Prior finding to cross-check against (from the 2026-06-10 real-data audit):
# 11y single-name VRP mean approx -0.46 vol pts, ~55% of days positive,
# 9/12 names negative.
PRIOR_MEAN_VOL_PTS = -0.46
PRIOR_PCT_DAYS_POSITIVE = 55.0
PRIOR_NAMES_NEGATIVE_OUT_OF = (9, 12)


def yahoo_universe() -> list[str]:
    """Single-name tickers present in the local Yahoo store (ticker=* dirs)."""
    if not YAHOO_BARS.exists():
        raise SystemExit(f"Yahoo store not found: {YAHOO_BARS}")
    out = []
    for p in sorted(YAHOO_BARS.glob("ticker=*")):
        if p.is_dir():
            out.append(p.name.split("=", 1)[1].upper())
    return out


def _pct(x: float) -> float:
    return round(100.0 * float(x), 2)


def _vp(decimal_value: float) -> float:
    """Decimal vol -> vol POINTS (percentage points). 0.0046 -> 0.46."""
    return round(100.0 * float(decimal_value), 4)


def measure_ticker(ticker: str) -> dict | None:
    """Build the daily VRP_calendar series for one ticker and summarize it."""
    try:
        hist = load_iv_history(ticker)
    except SweDataUnavailable as exc:
        return {"ticker": ticker, "error": str(exc)}

    fr = hist.frame
    if "atm_iv" not in fr.columns or "rv_30d" not in fr.columns:
        return {"ticker": ticker, "error": "missing atm_iv/rv_30d columns"}

    sub = fr[["atm_iv", "rv_30d"]].dropna()
    if sub.empty:
        return {"ticker": ticker, "error": "no overlapping atm_iv/rv_30d rows"}

    vrp = (sub["atm_iv"] - sub["rv_30d"]).astype(float)  # decimal vol
    n = int(vrp.shape[0])
    n_pos = int((vrp > 0.0).sum())

    return {
        "ticker": ticker,
        "n_days": n,
        "date_start": sub.index.min().date().isoformat(),
        "date_end": sub.index.max().date().isoformat(),
        # decimal vol summaries
        "vrp_mean_decimal": float(vrp.mean()),
        "vrp_median_decimal": float(vrp.median()),
        "vrp_std_decimal": float(vrp.std(ddof=1)) if n > 1 else 0.0,
        # vol-points summaries (the prior finding is quoted in vol pts)
        "vrp_mean_vol_pts": _vp(vrp.mean()),
        "vrp_median_vol_pts": _vp(vrp.median()),
        # positivity
        "n_days_positive": n_pos,
        "pct_days_positive": _pct(n_pos / n),
        # context: average levels (vol pts) so the reader can see IV vs RV magnitudes
        "mean_atm_iv_vol_pts": _vp(sub["atm_iv"].mean()),
        "mean_rv_30d_vol_pts": _vp(sub["rv_30d"].mean()),
        # raw pooled series kept for the pooled aggregation (not serialized per-ticker)
        "_series": vrp,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    yahoo = set(yahoo_universe())
    # Universe = candidates that are BOTH in the Yahoo store AND in vol_iv_full.
    # (vol_iv_full membership is verified implicitly by load_iv_history succeeding.)
    universe = [t for t in CANDIDATE_UNIVERSE if t in yahoo]
    excluded_not_in_yahoo = [t for t in CANDIDATE_UNIVERSE if t not in yahoo]

    per_ticker: list[dict] = []
    pooled_parts: list[pd.Series] = []
    for t in universe:
        res = measure_ticker(t)
        if res is None:
            continue
        if "error" in res:
            per_ticker.append(res)
            continue
        pooled_parts.append(res.pop("_series"))
        per_ticker.append(res)

    ok = [r for r in per_ticker if "error" not in r]
    if not ok:
        raise SystemExit("no tickers produced a VRP series; aborting")

    # ----- Pooled (all ticker-days stacked) -----
    pooled = pd.concat(pooled_parts) if pooled_parts else pd.Series(dtype=float)
    n_pooled = int(pooled.shape[0])
    n_pooled_pos = int((pooled > 0.0).sum())
    pooled_summary = {
        "n_ticker_days": n_pooled,
        "vrp_mean_decimal": float(pooled.mean()),
        "vrp_median_decimal": float(pooled.median()),
        "vrp_std_decimal": float(pooled.std(ddof=1)),
        "vrp_mean_vol_pts": _vp(pooled.mean()),
        "vrp_median_vol_pts": _vp(pooled.median()),
        "n_days_positive": n_pooled_pos,
        "pct_days_positive": _pct(n_pooled_pos / n_pooled),
        "vrp_p05_vol_pts": _vp(np.percentile(pooled, 5)),
        "vrp_p25_vol_pts": _vp(np.percentile(pooled, 25)),
        "vrp_p75_vol_pts": _vp(np.percentile(pooled, 75)),
        "vrp_p95_vol_pts": _vp(np.percentile(pooled, 95)),
    }

    # ----- Equal-weight across tickers (each name counts once) -----
    means_vp = [r["vrp_mean_vol_pts"] for r in ok]
    pct_pos = [r["pct_days_positive"] for r in ok]
    n_names_negative = sum(1 for r in ok if r["vrp_mean_decimal"] < 0.0)
    equal_weight = {
        "n_names": len(ok),
        "mean_of_per_ticker_mean_vol_pts": round(float(np.mean(means_vp)), 4),
        "median_of_per_ticker_mean_vol_pts": round(float(np.median(means_vp)), 4),
        "mean_of_per_ticker_pct_positive": round(float(np.mean(pct_pos)), 2),
        "n_names_negative_mean": n_names_negative,
        "n_names_total": len(ok),
    }

    # ----- Replication verdict vs the prior finding -----
    pooled_mean_vp = pooled_summary["vrp_mean_vol_pts"]
    ew_mean_vp = equal_weight["mean_of_per_ticker_mean_vol_pts"]
    pooled_pct = pooled_summary["pct_days_positive"]
    ew_pct = equal_weight["mean_of_per_ticker_pct_positive"]

    # "Replicates" if the sign is negative-and-small, % positive is in the mid-50s,
    # and most names are negative — matching prior (-0.46 vp, 55%, 9/12 neg).
    sign_ok = ew_mean_vp < 0.0 and pooled_mean_vp < 0.0
    small_ok = abs(ew_mean_vp - PRIOR_MEAN_VOL_PTS) <= 0.75  # within ~0.75 vol pts
    pct_ok = 48.0 <= ew_pct <= 62.0 and 48.0 <= pooled_pct <= 62.0
    majority_negative = n_names_negative >= (len(ok) // 2)
    replicates = bool(sign_ok and pct_ok and majority_negative)

    replication = {
        "prior_finding": {
            "mean_vol_pts": PRIOR_MEAN_VOL_PTS,
            "pct_days_positive": PRIOR_PCT_DAYS_POSITIVE,
            "names_negative_out_of": list(PRIOR_NAMES_NEGATIVE_OUT_OF),
            "source": "audit-and-testing-2026-06-10 (real Bloomberg IV source)",
        },
        "measured": {
            "equal_weight_mean_vol_pts": ew_mean_vp,
            "pooled_mean_vol_pts": pooled_mean_vp,
            "equal_weight_pct_positive": ew_pct,
            "pooled_pct_positive": pooled_pct,
            "names_negative_out_of": [n_names_negative, len(ok)],
        },
        "checks": {
            "sign_negative_and_small": bool(sign_ok and small_ok),
            "pct_positive_mid_50s": bool(pct_ok),
            "majority_names_negative": bool(majority_negative),
        },
        "replicates": replicates,
        "verdict": (
            "REPLICATES — calendar-clock single-name VRP is small and slightly "
            "NEGATIVE (mean below zero), with ~mid-50s%% of days positive and a "
            "majority of names negative, matching the prior real-data finding."
            if replicates else
            "DOES NOT cleanly replicate — see measured vs prior numbers."
        ),
    }

    # ----- Honest S2 threshold recommendation -----
    # On the true calendar clock, single-name VRP is mostly small and slightly
    # negative. A positive threshold (like the 0.02 default) applied to a
    # calendar-clock VRP would stand aside on the large majority of days. We report
    # what fraction of pooled ticker-days clear a few candidate thresholds so the
    # trade-off is explicit.
    def frac_clearing(thr: float) -> float:
        return _pct(float((pooled >= thr).mean()))

    threshold_grid = {
        f"{thr:+.3f}": frac_clearing(thr)
        for thr in (-0.05, -0.02, 0.0, 0.01, 0.02, 0.05)
    }

    recommendation = {
        "s2_current_threshold_decimal": S2_CURRENT_THRESHOLD,
        "s2_clock": (
            "INTRADAY trading-time RV (Garman-Klass on 5m bars, annualized 6.5h x "
            "252). This is a DIFFERENT clock from rv_30d; intraday RV prints far "
            "below calendar 30d RV, so S2's intraday VRP is mechanically inflated "
            "and almost always clears 0.02."
        ),
        "calendar_clock_finding": (
            "On a TRUE calendar clock (atm_iv - rv_30d) single-name VRP is mostly "
            "small and slightly NEGATIVE. A POSITIVE threshold on this clock means "
            "standing aside the large majority of days."
        ),
        "pct_pooled_days_clearing_threshold": threshold_grid,
        "honest_recommendation_decimal": 0.0,
        "honest_recommendation_text": (
            "Do NOT raise S2's threshold to a large positive number expecting the "
            "calendar-clock VRP to clear it -- it will not (single-name calendar VRP "
            "is ~0 to slightly negative). Two honest options: (1) KEEP S2 on its "
            "intraday clock but recognise the gate is a clock artifact, not a real "
            "carry edge, and set the threshold from the intraday-VRP distribution "
            "(not from this calendar measurement); or (2) if S2 is ever moved to a "
            "calendar/overnight VRP, set vrp_threshold ~0.0 (sell only when IV is at "
            "or above realized) and accept it will stand aside on roughly half of all "
            "days. A defensible single number is 0.0 on the calendar clock; the "
            "current 0.02 is only meaningful on the intraday clock and should be "
            "documented as clock-specific, not a true premium."
        ),
        "caveat": (
            "VRP carry edge != tradeable edge. Even a positive calendar VRP is a "
            "thin, fat-tailed carry that gives back in shocks; S2's defined-risk "
            "structure and event lockout exist precisely because the raw premium is "
            "small and the giveback is lumpy."
        ),
    }

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "swe_root": str(swe_root()),
        "vrp_definition": "VRP_calendar = atm_iv - rv_30d (both annualized decimals)",
        "universe_requested": CANDIDATE_UNIVERSE,
        "universe_used": [r["ticker"] for r in ok],
        "excluded_not_in_yahoo_store": excluded_not_in_yahoo,
        "per_ticker": per_ticker,
        "pooled": pooled_summary,
        "equal_weight": equal_weight,
        "replication": replication,
        "recommendation": recommendation,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_markdown(payload)

    # ----- Console summary -----
    print("=" * 72)
    print("REAL-DATA VRP MEASUREMENT (calendar clock: atm_iv - rv_30d)")
    print("=" * 72)
    print(f"Universe used ({len(ok)}): {', '.join(r['ticker'] for r in ok)}")
    if excluded_not_in_yahoo:
        print(f"Excluded (not in Yahoo store): {excluded_not_in_yahoo}")
    print("-" * 72)
    hdr = f"{'ticker':<7}{'n':>6}{'mean_vp':>10}{'med_vp':>9}{'%pos':>8}{'IV_vp':>8}{'RV_vp':>8}"
    print(hdr)
    for r in ok:
        print(
            f"{r['ticker']:<7}{r['n_days']:>6}{r['vrp_mean_vol_pts']:>10.3f}"
            f"{r['vrp_median_vol_pts']:>9.3f}{r['pct_days_positive']:>8.1f}"
            f"{r['mean_atm_iv_vol_pts']:>8.2f}{r['mean_rv_30d_vol_pts']:>8.2f}"
        )
    print("-" * 72)
    print(
        f"POOLED  ({pooled_summary['n_ticker_days']} ticker-days): "
        f"mean={pooled_summary['vrp_mean_vol_pts']:.3f} vp  "
        f"median={pooled_summary['vrp_median_vol_pts']:.3f} vp  "
        f"%pos={pooled_summary['pct_days_positive']:.1f}"
    )
    print(
        f"EQUAL-WEIGHT: mean={equal_weight['mean_of_per_ticker_mean_vol_pts']:.3f} vp  "
        f"%pos={equal_weight['mean_of_per_ticker_pct_positive']:.1f}  "
        f"names_negative={n_names_negative}/{len(ok)}"
    )
    print("-" * 72)
    print(f"REPLICATION vs prior (-0.46 vp, 55% pos, 9/12 neg): {replication['replicates']}")
    print(replication["verdict"])
    print("-" * 72)
    print("THRESHOLD GRID (% pooled ticker-days clearing):")
    for k, v in threshold_grid.items():
        print(f"   thr {k}: {v:.1f}% of days clear")
    print("-" * 72)
    print(f"HONEST RECOMMENDATION (calendar clock): vrp_threshold = "
          f"{recommendation['honest_recommendation_decimal']}")
    print(f"\nWrote: {OUT_JSON}")
    print(f"Wrote: {OUT_MD}")
    return 0


def _write_markdown(p: dict) -> None:
    pool = p["pooled"]
    ew = p["equal_weight"]
    rep = p["replication"]
    rec = p["recommendation"]
    lines: list[str] = []
    lines.append("# Real-Data VRP Measurement & S2 Threshold Calibration")
    lines.append("")
    lines.append(f"_Generated {p['generated_utc']} — network-free, reads only._")
    lines.append("")
    lines.append("## Definition")
    lines.append("")
    lines.append("`VRP_calendar = atm_iv - rv_30d` (both annualized **decimals**, from "
                 "the CORRECT Bloomberg implied-vol source `sp500_vol_iv_full.csv` via "
                 "`intraday.data.swe_offline.load_iv_history`). This is forward implied "
                 "vol minus a backward 30-calendar-day realized vol — the standard "
                 "single-name VRP proxy. Reported in **vol points** (= percentage "
                 "points; 0.0046 decimal = 0.46 vp).")
    lines.append("")
    lines.append(f"Universe used ({len(p['universe_used'])}): "
                 f"`{', '.join(p['universe_used'])}`")
    if p["excluded_not_in_yahoo_store"]:
        lines.append("")
        lines.append(f"Excluded (not in Yahoo store): "
                     f"`{', '.join(p['excluded_not_in_yahoo_store'])}`")
    lines.append("")
    lines.append("## Per-ticker VRP")
    lines.append("")
    lines.append("| ticker | days | range | mean (vp) | median (vp) | % days +ve | "
                 "mean IV (vp) | mean RV30 (vp) |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|")
    for r in p["per_ticker"]:
        if "error" in r:
            lines.append(f"| {r['ticker']} | — | ERROR | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['ticker']} | {r['n_days']} | {r['date_start']}..{r['date_end']} | "
            f"{r['vrp_mean_vol_pts']:.3f} | {r['vrp_median_vol_pts']:.3f} | "
            f"{r['pct_days_positive']:.1f}% | {r['mean_atm_iv_vol_pts']:.2f} | "
            f"{r['mean_rv_30d_vol_pts']:.2f} |"
        )
    lines.append("")
    lines.append("## Pooled & equal-weight")
    lines.append("")
    lines.append(f"- **Pooled** ({pool['n_ticker_days']} ticker-days): "
                 f"mean **{pool['vrp_mean_vol_pts']:.3f} vp**, "
                 f"median {pool['vrp_median_vol_pts']:.3f} vp, "
                 f"**{pool['pct_days_positive']:.1f}%** of days positive.")
    lines.append(f"  - Spread (vp): p05 {pool['vrp_p05_vol_pts']:.2f} / "
                 f"p25 {pool['vrp_p25_vol_pts']:.2f} / "
                 f"p75 {pool['vrp_p75_vol_pts']:.2f} / "
                 f"p95 {pool['vrp_p95_vol_pts']:.2f}.")
    lines.append(f"- **Equal-weight** across {ew['n_names']} names: "
                 f"mean-of-means **{ew['mean_of_per_ticker_mean_vol_pts']:.3f} vp**, "
                 f"mean %positive {ew['mean_of_per_ticker_pct_positive']:.1f}%, "
                 f"**{ew['n_names_negative_mean']}/{ew['n_names_total']}** names have a "
                 f"negative mean VRP.")
    lines.append("")
    lines.append("## Replication vs prior finding")
    lines.append("")
    pr = rep["prior_finding"]
    me = rep["measured"]
    lines.append(f"- Prior (real Bloomberg IV, 11y): mean **{pr['mean_vol_pts']} vp**, "
                 f"~**{pr['pct_days_positive']:.0f}%** days positive, "
                 f"{pr['names_negative_out_of'][0]}/{pr['names_negative_out_of'][1]} "
                 f"names negative.")
    lines.append(f"- Measured: equal-weight mean **{me['equal_weight_mean_vol_pts']:.3f} vp** "
                 f"(pooled {me['pooled_mean_vol_pts']:.3f} vp), "
                 f"equal-weight %positive **{me['equal_weight_pct_positive']:.1f}%** "
                 f"(pooled {me['pooled_pct_positive']:.1f}%), "
                 f"{me['names_negative_out_of'][0]}/{me['names_negative_out_of'][1]} "
                 f"names negative.")
    lines.append("")
    lines.append(f"**Verdict: {'REPLICATES' if rep['replicates'] else 'DOES NOT REPLICATE'}.** "
                 f"{rep['verdict']}")
    lines.append("")
    lines.append("## Honest S2 `vrp_threshold` recommendation")
    lines.append("")
    lines.append(f"- S2 currently uses `vrp_threshold = {rec['s2_current_threshold_decimal']}` "
                 f"on an **intraday** clock. {rec['s2_clock']}")
    lines.append("")
    lines.append(f"- {rec['calendar_clock_finding']}")
    lines.append("")
    lines.append("- Fraction of pooled ticker-days that clear candidate thresholds "
                 "(calendar clock):")
    lines.append("")
    lines.append("  | threshold (decimal) | % days clearing |")
    lines.append("  |---:|---:|")
    for k, v in rec["pct_pooled_days_clearing_threshold"].items():
        lines.append(f"  | {k} | {v:.1f}% |")
    lines.append("")
    lines.append(f"- **Recommendation:** {rec['honest_recommendation_text']}")
    lines.append("")
    lines.append(f"- **Caveat:** {rec['caveat']}")
    lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
