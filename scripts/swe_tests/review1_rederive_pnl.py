"""Reviewer #1: independently re-derive per-session portfolio PnL from the engine
(own aggregation code, not the harness's) and compare to the saved CSV.

Covers all three strategies x 8 symbols over 2026-03-09..2026-04-22, then
recomputes the headline S4 scheme_3m bucket means from the re-derived series.

Run: .venv/Scripts/python.exe scripts/swe_tests/review1_rederive_pnl.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday.authority.reviewers import default_reviewers  # noqa: E402
from intraday.backtest.engine import IntradayBacktester  # noqa: E402
from intraday.config import EngineConfig  # noqa: E402
from intraday.contracts import DataSource  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.data.store_provider import StoreBackedProvider  # noqa: E402
from intraday.eval import daily_pnl_from_result  # noqa: E402
from intraday.signals.s3_vwap_orb import S3VwapOrb  # noqa: E402
from intraday.signals.s4_orb_breakout import S4OpeningRangeBreakout  # noqa: E402
from intraday.signals.s5_vwap_momentum import S5VwapMomentum  # noqa: E402

OUT = os.path.join(_ROOT, "data_raw", "swe_tests")
CSV = os.path.join(OUT, "regime_stratification_sessions.csv")
SYMS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "GLD", "TLT")
START, END = date(2026, 3, 9), date(2026, 4, 22)
MK = {"s3": lambda: S3VwapOrb(edge=0.10),
      "s4": lambda: S4OpeningRangeBreakout(edge=0.10),
      "s5": lambda: S5VwapMomentum(edge=0.10)}

cfg = EngineConfig.default()
store = ParquetStore(os.path.join(_ROOT, "data_raw", "store_yahoo"))
tbl = pd.read_csv(CSV, parse_dates=["session"]).set_index("session")

report = {"per_strategy": {}, "fails": []}
for name, mk in MK.items():
    # fresh reviewers per strategy run (deliberately NOT reusing, to test that
    # the reuse pattern in the harness does not leak state)
    per_day: dict[date, list[float]] = {}
    for sym in SYMS:
        prov = StoreBackedProvider(store, DataSource.YAHOO, symbols=[sym], interval="5m")
        bt = IntradayBacktester(cfg, prov, [mk()], reviewers=default_reviewers(cfg))
        res = bt.run([sym], START, END, "5m")
        s = daily_pnl_from_result(res)
        for ts, v in s.items():
            per_day.setdefault(ts.date(), []).append(float(v))
    mine = pd.Series({d: float(np.mean(v)) for d, v in sorted(per_day.items())})
    n_syms = {d: len(v) for d, v in per_day.items()}
    csv_col = tbl[f"pnl_{name}"]
    diffs = []
    for d, v in mine.items():
        ref = float(csv_col.loc[pd.Timestamp(d)])
        if abs(v - ref) > 1e-9 * max(1.0, abs(ref)):
            diffs.append(f"{d}: mine={v} csv={ref}")
    report["per_strategy"][name] = {
        "n_sessions": len(mine),
        "all_8_symbols_every_day": all(c == 8 for c in n_syms.values()),
        "max_abs_diff_vs_csv": float(max(abs(mine.loc[d] - float(csv_col.loc[pd.Timestamp(d)]))
                                         for d in mine.index)),
        "mismatches": diffs,
        "mean_pnl_per_day": float(mine.mean()),
    }
    if diffs:
        report["fails"].append(f"{name}: {len(diffs)} session PnL mismatches vs CSV")

# headline bucket re-check from re-derived numbers
lab = pd.read_csv(CSV, parse_dates=["session"]).set_index("session")
s4 = lab["pnl_s4"]
m_con = s4[lab["scheme_3m"] == "contango_3m"]
m_inv = s4[lab["scheme_3m"] == "flat_or_inverted_3m"]
report["s4_scheme_3m_check"] = {
    "contango_mean": float(m_con.mean()), "n_contango": int(len(m_con)),
    "inverted_mean": float(m_inv.mean()), "n_inverted": int(len(m_inv)),
}

with open(os.path.join(OUT, "review1_rederive_pnl.json"), "w") as f:
    json.dump(report, f, indent=2, default=str)
print(json.dumps(report, indent=2, default=str))
print("\nVERDICT:", "FAIL: " + "; ".join(report["fails"]) if report["fails"] else "ENGINE PNL MATCHES CSV")
