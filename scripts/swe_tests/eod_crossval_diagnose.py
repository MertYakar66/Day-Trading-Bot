"""Follow-up diagnostics for the EOD cross-validation workstream.

Two questions raised by eod_cross_validation.py:

1. 2026-03-20 (final date in both EOD sources, triple-witching Friday) shows a
   cross-sectional blowout: median |close diff| 32 bps vs ~1 bps on other days,
   and store lows pierce EOD lows on nearly every ticker. Hypothesis: the EOD
   row for 2026-03-20 was captured INTRADAY (partial day), so its "close" is a
   mid-session print and its high/low only cover part of the session.
   Test: per ticker, find the ET time of the 5m bar whose close best matches
   the EOD close, and check whether EOD high/low equal the store's RUNNING
   high/low up to that bar.

2. A handful of non-03-20 containment violations (e.g. XOM 2026-03-11 low
   -57 bps). Are these Yahoo bad ticks (one-bar spikes) or real prints the EOD
   source missed? Dump the offending bar and its neighbours.

Output: data_raw/swe_tests/eod_crossval_diagnostics.json
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path("C:/Users/merty/Desktop/Day-Trading-Bot")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from intraday.data.store import ParquetStore  # noqa: E402

SWE = Path("C:/Users/merty/Desktop/smart-wheel-engine")
OUT = REPO_ROOT / "data_raw" / "swe_tests" / "eod_crossval_diagnostics.json"
STORE = ParquetStore(REPO_ROOT / "data_raw" / "store_yahoo")
TICKERS = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "JPM",
           "META", "MSFT", "NFLX", "NVDA", "TSLA", "XOM"]
BPS = 1e4


def bars(tic: str, day: date) -> pd.DataFrame:
    f = STORE.read_bars(tic, day, "5m").frame.copy()
    f.index = f.index.tz_convert("America/New_York")
    return f


def eod_row(tic: str, day: date) -> pd.Series:
    df = pd.read_parquet(SWE / "data_processed" / "theta" / "stocks_eod" / f"{tic}.parquet")
    df.index = pd.to_datetime(df.index).date
    return df.loc[day]


def partial_day_test(day: date) -> list[dict]:
    out = []
    for tic in TICKERS:
        f = bars(tic, day)
        e = eod_row(tic, day)
        # bar whose close best matches the EOD "close"
        diff = (f["close"] / float(e["close"]) - 1.0).abs() * BPS
        i = int(diff.to_numpy().argmin())
        run_high = float(f["high"].iloc[: i + 1].max())
        run_low = float(f["low"].iloc[: i + 1].min())
        out.append(
            {
                "ticker": tic,
                "best_match_bar_et": str(f.index[i]),
                "best_match_diff_bps": float(diff.iloc[i]),
                "eod_close": float(e["close"]),
                "true_session_close": float(f["close"].iloc[-1]),
                "eod_high_minus_running_high_bps": (float(e["high"]) / run_high - 1.0) * BPS,
                "eod_low_minus_running_low_bps": (float(e["low"]) / run_low - 1.0) * BPS,
                "eod_high_minus_fullday_high_bps": (float(e["high"]) / float(f["high"].max()) - 1.0) * BPS,
                "eod_low_minus_fullday_low_bps": (float(e["low"]) / float(f["low"].min()) - 1.0) * BPS,
            }
        )
    return out


def bad_tick_dump(cases: list[tuple[str, str, str]]) -> list[dict]:
    """For each (ticker, date, side) violation, show the extreme bar vs neighbours."""
    out = []
    for tic, dstr, side in cases:
        day = date.fromisoformat(dstr)
        f = bars(tic, day)
        e = eod_row(tic, day)
        col = "low" if side == "low" else "high"
        i = int(f[col].to_numpy().argmin() if side == "low" else f[col].to_numpy().argmax())
        lo, hi = max(0, i - 2), min(len(f), i + 3)
        window = f.iloc[lo:hi][["open", "high", "low", "close", "volume"]]
        ext = float(f[col].iloc[i])
        # spike score: how far the extreme sticks out beyond the bar's own body
        body = float(min(f["open"].iloc[i], f["close"].iloc[i])) if side == "low" \
            else float(max(f["open"].iloc[i], f["close"].iloc[i]))
        out.append(
            {
                "ticker": tic, "date": dstr, "side": side,
                "extreme_bar_et": str(f.index[i]), "extreme_price": ext,
                "eod_extreme": float(e[col]),
                "excess_vs_eod_bps": (float(e[col]) / ext - 1.0) * BPS if side == "low"
                else (ext / float(e[col]) - 1.0) * BPS,
                "extreme_vs_bar_body_bps": abs(body / ext - 1.0) * BPS,
                "window": json.loads(window.round(4).to_json(orient="index", date_format="iso")),
            }
        )
    return out


def main() -> None:
    res = {
        "partial_day_test_2026_03_20": partial_day_test(date(2026, 3, 20)),
        "non_0320_violation_dumps": bad_tick_dump(
            [
                ("XOM", "2026-03-11", "low"),
                ("XOM", "2026-03-13", "low"),
                ("XOM", "2026-03-18", "high"),
                ("AMD", "2026-03-09", "low"),
                ("AAPL", "2026-03-12", "high"),
                ("TSLA", "2026-03-19", "high"),
                ("NVDA", "2026-03-17", "high"),
            ]
        ),
    }
    OUT.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
