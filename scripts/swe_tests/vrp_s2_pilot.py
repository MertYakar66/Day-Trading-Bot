"""S2 VRP workstream — parts (b) pilot VRP, (c) clock-bias, (d) SPY vs VIX9D.

(b) PILOT (2026-03-09 -> 2026-03-20, 12 names): prior-close IV (PIT, lag 1
    trading day; source = mean(hist_put_imp_vol, hist_call_imp_vol)/100 from
    Bloomberg sp500_vol_iv_full.csv — iv_history.iv_atm was adjudicated to be
    REALIZED vol, see vrp_s2_adjudication.py) minus the bot's own intraday RV
    (intraday.features.realized_vol.intraday_rv, garman_klass, window=30 — the
    S2/pipeline defaults) on store_yahoo 5m bars, PIT-sliced at 10:00 and
    15:00 ET. With window=30 the same-session RV needs 31 closed bars
    (~12:05 ET), so the 10:00 S2-default RV is structurally None; a
    DESCRIPTIVE cross-session variant (trailing 31 bars spanning the prior
    session; Garman-Klass uses only within-bar OHLC so no overnight-gap term)
    is reported alongside.

(c) CLOCK-BIAS (audit caveat #4): same sessions, bot 15:00 RV (RTH clock,
    sqrt(78)-bar annualization) vs Bloomberg volatility_30d/100 (calendar
    clock) on the SAME date — DESCRIPTIVE (same-day EOD value), it measures
    estimator-clock bias, it is not a tradable signal.

(d) SPY market-level proxy (2026-03-09 -> 2026-04-22): prior-close VIX9D/100
    (PIT, vix_family.parquet) minus bot intraday RV on SPY at 15:00 ET.

READ-ONLY on smart-wheel-engine. No network. Outputs under data_raw/swe_tests/:
vrp_pilot_rows.csv, vrp_clock_bias_per_ticker.csv, vrp_spy_vix9d_timeseries.csv,
vrp_pilot_summary.json.

Run:  .venv/Scripts/python.exe scripts/swe_tests/vrp_s2_pilot.py
(cwd = repo root)
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
for p in (str(REPO), str(REPO / "vendor" / "swe")):
    if p not in sys.path:
        sys.path.insert(0, p)

from intraday.data.store import ParquetStore  # noqa: E402
from intraday.features.realized_vol import intraday_rv  # noqa: E402

OUT = REPO / "data_raw" / "swe_tests"
OUT.mkdir(parents=True, exist_ok=True)
STORE = ParquetStore(REPO / "data_raw" / "store_yahoo")
ET = ZoneInfo("America/New_York")

TICKERS = [
    "AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
    "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM",
]
INTERVAL = "5m"
RV_WINDOW = 30          # S2 / FeaturePipeline default
RV_ESTIMATOR = "garman_klass"
PILOT_START, PILOT_END = date(2026, 3, 9), date(2026, 3, 20)
SPY_START, SPY_END = date(2026, 3, 9), date(2026, 4, 22)


def store_sessions(ticker: str, start: date, end: date) -> list[date]:
    base = STORE.root / "bars" / f"ticker={ticker}"
    out = []
    for p in sorted(base.glob("date=*")):
        if (p / f"bars_{INTERVAL}.parquet").exists():
            d = date.fromisoformat(p.name.split("=", 1)[1])
            if start <= d <= end:
                out.append(d)
    return out


def as_of_utc(day: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(hh, mm)), tz=ET).tz_convert("UTC")


def rv_at(bars, as_of: pd.Timestamp) -> float | None:
    """S2-default intraday RV on the PIT slice of the session's bars."""
    pit = bars.available_at(as_of)
    return intraday_rv(pit, INTERVAL, window=RV_WINDOW, estimator=RV_ESTIMATOR)


def rv_cross_session(prev_frame: pd.DataFrame | None, bars, as_of: pd.Timestamp) -> float | None:
    """DESCRIPTIVE: trailing RV_WINDOW+1 bars spanning the prior session."""
    pit = bars.available_at(as_of)
    if prev_frame is None:
        return None
    joined = pd.concat([prev_frame, pit]).iloc[-(RV_WINDOW + 1):]
    return intraday_rv(joined, INTERVAL, window=RV_WINDOW, estimator=RV_ESTIMATOR)


def main() -> None:
    # ---- Bloomberg IV (percent) + realized vol_30d, per ticker by date ----- #
    vf = pd.read_csv(SWE / "data/bloomberg/sp500_vol_iv_full.csv")
    vf["date"] = pd.to_datetime(vf["date"]).dt.date
    vf["base"] = vf["ticker"].str.split(" ").str[0]
    vf["iv_mean"] = (vf["hist_put_imp_vol"] + vf["hist_call_imp_vol"]) / 200.0
    vf["rv30"] = vf["volatility_30d"] / 100.0
    bbg = {t: vf.loc[vf["base"] == t].set_index("date").sort_index() for t in TICKERS}

    def prior_iv(ticker: str, day: date) -> tuple[float | None, str | None]:
        sub = bbg[ticker]
        prior = sub.loc[[d for d in sub.index if d < day]]
        if prior.empty:
            return None, None
        return float(prior["iv_mean"].iloc[-1]), str(prior.index[-1])

    # ------------------- (b) + (c): per-ticker pilot rows ------------------- #
    rows = []
    for t in TICKERS:
        sessions = store_sessions(t, PILOT_START, PILOT_END)
        prev_frame = None
        # the session before the pilot start, for the cross-session 10:00 RV
        before = store_sessions(t, date(2026, 1, 1), PILOT_START - pd.Timedelta(days=1).to_pytimedelta())
        if before:
            prev_frame = STORE.read_bars(t, before[-1], INTERVAL).frame
        for d in sessions:
            bars = STORE.read_bars(t, d, INTERVAL)
            assert bars.source.value == "yahoo"
            iv_prev, iv_date = prior_iv(t, d)
            r10 = rv_at(bars, as_of_utc(d, 10, 0))
            r10x = rv_cross_session(prev_frame, bars, as_of_utc(d, 10, 0))
            r15 = rv_at(bars, as_of_utc(d, 15, 0))
            same_day = bbg[t]["rv30"].get(d)
            rows.append({
                "ticker": t, "date": str(d),
                "iv_prior_close": iv_prev, "iv_prior_date": iv_date,
                "rv_1000_s2default": r10,
                "rv_1000_cross_session_descriptive": r10x,
                "rv_1500_s2default": r15,
                "vrp_1000_descriptive": (iv_prev - r10x) if (iv_prev is not None and r10x is not None) else None,
                "vrp_1500": (iv_prev - r15) if (iv_prev is not None and r15 is not None) else None,
                "bbg_vol30_same_day": (float(same_day) if pd.notna(same_day) else None),
                "clock_gap_rv1500_minus_vol30": (
                    (r15 - float(same_day)) if (r15 is not None and pd.notna(same_day)) else None
                ),
            })
            prev_frame = bars.frame
    pilot = pd.DataFrame(rows)
    pilot.to_csv(OUT / "vrp_pilot_rows.csv", index=False)

    def dist(s: pd.Series) -> dict:
        s = s.dropna().astype(float)
        if s.empty:
            return {"n": 0}
        return {
            "n": int(len(s)), "mean": float(s.mean()), "median": float(s.median()),
            "std": float(s.std(ddof=1)) if len(s) > 1 else None,
            "min": float(s.min()), "max": float(s.max()),
            "pct_positive": float((s > 0).mean() * 100.0),
            "pct_above_s2_threshold_0.02": float((s > 0.02).mean() * 100.0),
        }

    # ------------------------ (c) per-ticker clock bias ---------------------- #
    cb = (
        pilot.dropna(subset=["clock_gap_rv1500_minus_vol30"])
        .groupby("ticker")["clock_gap_rv1500_minus_vol30"]
        .agg(["count", "mean", "std", "min", "max"])
        .reset_index()
        .rename(columns={"count": "n_sessions", "mean": "mean_gap"})
    )
    cb["sign"] = np.where(cb["mean_gap"] > 0, "bot_RV_higher", "bot_RV_lower")
    cb.to_csv(OUT / "vrp_clock_bias_per_ticker.csv", index=False)

    # ------------------------- (d) SPY vs VIX9D ------------------------------ #
    vix = pd.read_parquet(SWE / "data_processed/theta/vix_family/vix_family.parquet")
    v9 = vix.loc[vix["symbol"] == "VIX9D", "close"].sort_index()
    v9.index = pd.to_datetime(v9.index).date

    spy_rows = []
    for d in store_sessions("SPY", SPY_START, SPY_END):
        bars = STORE.read_bars("SPY", d, INTERVAL)
        prior = [k for k in v9.index if k < d]
        if not prior:
            continue
        vix9d_prior = float(v9.loc[prior[-1]]) / 100.0
        r15 = rv_at(bars, as_of_utc(d, 15, 0))
        spy_rows.append({
            "date": str(d), "vix9d_prior_close_div100": vix9d_prior,
            "vix9d_prior_date": str(prior[-1]),
            "spy_rv_1500_s2default": r15,
            "vrp_proxy": (vix9d_prior - r15) if r15 is not None else None,
        })
    spy = pd.DataFrame(spy_rows)
    spy.to_csv(OUT / "vrp_spy_vix9d_timeseries.csv", index=False)

    summary = {
        "pilot_window": [str(PILOT_START), str(PILOT_END)],
        "n_tickers": len(TICKERS),
        "n_ticker_sessions": int(len(pilot)),
        "rv_1000_s2default_available_n": int(pilot["rv_1000_s2default"].notna().sum()),
        "note_1000": (
            "With the S2 default window=30 on 5m session bars the first RV needs 31 "
            "closed bars (~12:05 ET); at 10:00 ET the bot's RV (and hence VRP) is "
            "None on every session — S2 cannot see a VRP before midday at these "
            "defaults. vrp_1000_descriptive uses a cross-session trailing window "
            "(DESCRIPTIVE, not the engine path)."
        ),
        "vrp_1500_distribution": dist(pilot["vrp_1500"]),
        "vrp_1000_descriptive_distribution": dist(pilot["vrp_1000_descriptive"]),
        "rv_1500_distribution": dist(pilot["rv_1500_s2default"]),
        "iv_prior_distribution": dist(pilot["iv_prior_close"]),
        "clock_bias": {
            "definition": "bot intraday RV (15:00 ET, RTH clock) - Bloomberg volatility_30d/100 (same day, calendar clock); DESCRIPTIVE",
            "per_ticker": cb.to_dict(orient="records"),
            "pooled_mean_gap": float(cb["mean_gap"].mean()),
            "n_tickers_bot_rv_lower": int((cb["mean_gap"] < 0).sum()),
        },
        "spy_vix9d": {
            "window": [str(SPY_START), str(SPY_END)],
            "n_sessions": int(len(spy)),
            "vrp_proxy_distribution": dist(spy["vrp_proxy"]),
            "spy_rv_distribution": dist(spy["spy_rv_1500_s2default"]),
            "vix9d_distribution": dist(spy["vix9d_prior_close_div100"]),
        },
    }
    with open(OUT / "vrp_pilot_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\n(written to {OUT})")


if __name__ == "__main__":
    main()
