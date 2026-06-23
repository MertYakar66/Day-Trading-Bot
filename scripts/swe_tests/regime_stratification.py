"""Regime stratification of real-data per-session strategy PnL (EXPLORATORY).

Question: the pooled 59-session verdict for S3/S4/S5 is 'no edge'. Is that verdict
regime-uniform, or does conditional behavior differ by volatility regime?

Method
------
1. Per-session, per-strategy NET PnL: rerun the bot's own backtester (the same path
   ``scripts/eval_real_universe.py`` uses — IntradayBacktester + daily_pnl_from_result,
   one independently-capitalized $100k book per symbol, equal-weight portfolio = mean
   of per-symbol daily PnL) on a liquid subset over 2026-03-09 -> 2026-04-22 (the
   window where VIX-family daily closes exist on disk).
2. PIT regime labels from PRIOR-SESSION closes of the (read-only) SWE vix_family
   parquet — the label for session D uses only data with date < D, hence tradable:
     a. VIX level terciles (breakpoints computed WITHIN the window — in-sample
        descriptive choice, disclosed);
     b. VIX9D/VIX >= 1 (short-end backwardation) vs < 1 (contango);
     c. sign of VIX3M - VIX (term-structure slope).
3. Conditional mean net PnL/day +/- SE and n per bucket, per strategy; Welch-t
   contrasts between extreme buckets; unconditional baseline over the same window.

HONESTY: 3 strategies x 7 buckets = 21 bucket tests + 3x3 = 9 contrasts = 30 implied
hypotheses; Bonferroni bar = 0.05/30 ~= 0.00167. n per bucket ~5-16 sessions =>
INSUFFICIENT DATA for any definitive split. Nothing here is an edge claim.

Run (from repo root, project venv)::

    .venv/Scripts/python.exe scripts/swe_tests/regime_stratification.py
"""

from __future__ import annotations

import argparse
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

VIX_FAMILY_PARQUET = (
    "C:/Users/merty/Desktop/smart-wheel-engine/data_processed/theta/"
    "vix_family/vix_family.parquet"
)
DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "GLD", "TLT")
STRATEGIES = ("s3", "s4", "s5")


def _strategy(name: str, edge: float):
    return {
        "s3": lambda: S3VwapOrb(edge=edge),
        "s4": lambda: S4OpeningRangeBreakout(edge=edge),
        "s5": lambda: S5VwapMomentum(edge=edge),
    }[name]()


def per_strategy_portfolio_daily(store_root: str, symbols, start, end, interval, edge):
    """Equal-weight portfolio daily NET PnL series per strategy (the eval-harness path)."""
    cfg = EngineConfig.default()
    reviewers = default_reviewers(cfg)
    store = ParquetStore(store_root)
    out: dict[str, pd.Series] = {}
    per_symbol: dict[str, dict[str, pd.Series]] = {}
    for name in STRATEGIES:
        cols = []
        per_symbol[name] = {}
        for sym in symbols:
            prov = StoreBackedProvider(store, DataSource.YAHOO, symbols=[sym], interval=interval)
            if not prov.trading_days(start, end):
                continue
            bt = IntradayBacktester(cfg, prov, [_strategy(name, edge)], reviewers=reviewers)
            res = bt.run([sym], start, end, interval)
            s = daily_pnl_from_result(res)
            per_symbol[name][sym] = s
            cols.append(s.rename(sym))
        df = pd.concat(cols, axis=1).sort_index().fillna(0.0)
        out[name] = df.mean(axis=1)
    return out, per_symbol


def regime_labels(sessions: list[date]) -> pd.DataFrame:
    """PIT labels: each session is tagged with the LATEST vix_family close strictly
    before it (prior close => tradable at that session's open)."""
    vf = pd.read_parquet(VIX_FAMILY_PARQUET)
    closes = vf.pivot_table(index=vf.index, columns="symbol", values="close").sort_index()
    rows = []
    for d in sessions:
        ts = pd.Timestamp(d)
        prior = closes[closes.index < ts]
        if prior.empty:
            continue
        last = prior.iloc[-1]
        if last[["VIX", "VIX9D", "VIX3M"]].isna().any():
            continue
        rows.append({
            "session": d,
            "prior_close_date": prior.index[-1].date(),
            "vix": float(last["VIX"]),
            "vix9d": float(last["VIX9D"]),
            "vix3m": float(last["VIX3M"]),
        })
    lab = pd.DataFrame(rows).set_index("session")
    lab["ratio_9d"] = lab["vix9d"] / lab["vix"]
    lab["slope_3m"] = lab["vix3m"] - lab["vix"]
    # scheme 1: VIX terciles, breakpoints WITHIN window (in-sample, descriptive)
    q1, q2 = np.quantile(lab["vix"], [1 / 3, 2 / 3])
    lab["vix_tercile"] = np.where(lab["vix"] <= q1, "low",
                                  np.where(lab["vix"] <= q2, "mid", "high"))
    lab.attrs["tercile_breaks"] = (float(q1), float(q2))
    # scheme 2: 9D/spot ratio (>=1 = short-end inverted/backwardation)
    lab["scheme_9d"] = np.where(lab["ratio_9d"] >= 1.0, "backwardation_9d", "contango_9d")
    # scheme 3: 3M-spot slope sign
    lab["scheme_3m"] = np.where(lab["slope_3m"] > 0.0, "contango_3m", "flat_or_inverted_3m")
    return lab


def _bucket_stats(x: np.ndarray) -> dict:
    n = len(x)
    if n == 0:
        return {"n": 0}
    mean = float(np.mean(x))
    sd = float(np.std(x, ddof=1)) if n > 1 else float("nan")
    se = sd / np.sqrt(n) if n > 1 else float("nan")
    t = mean / se if (n > 1 and se > 0) else float("nan")
    p = float(2 * _ss.t.sf(abs(t), df=n - 1)) if np.isfinite(t) else float("nan")
    return {"n": n, "mean_pnl_per_day": mean, "se": float(se), "sd": sd,
            "t": float(t), "p": p, "total_pnl": float(np.sum(x)),
            "win_days": int((x > 0).sum())}


def _welch(a: np.ndarray, b: np.ndarray) -> dict:
    if len(a) < 2 or len(b) < 2:
        return {"t": float("nan"), "p": float("nan"), "n_a": len(a), "n_b": len(b)}
    t, p = _ss.ttest_ind(a, b, equal_var=False)
    return {"t": float(t), "p": float(p), "n_a": len(a), "n_b": len(b),
            "mean_diff": float(np.mean(a) - np.mean(b))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store-root", default="data_raw/store_yahoo")
    ap.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 3, 9))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 4, 22))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--edge", type=float, default=0.10)
    ap.add_argument("--out-dir", default="data_raw/swe_tests")
    args = ap.parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)

    daily, per_symbol = per_strategy_portfolio_daily(
        args.store_root, args.symbols, args.start, args.end, args.interval, args.edge)
    sessions = sorted({d.date() for s in daily.values() for d in s.index})
    lab = regime_labels(sessions)

    # session-level table: pnl per strategy + labels (saved as CSV)
    tbl = pd.DataFrame(index=pd.Index(sessions, name="session"))
    for name, s in daily.items():
        tbl[f"pnl_{name}"] = pd.Series({d.date(): v for d, v in s.items()})
    tbl = tbl.join(lab, how="inner")  # keep only sessions with a PIT label
    csv_path = os.path.join(args.out_dir, "regime_stratification_sessions.csv")
    tbl.to_csv(csv_path)

    schemes = {"vix_tercile": ["low", "mid", "high"],
               "scheme_9d": ["contango_9d", "backwardation_9d"],
               "scheme_3m": ["contango_3m", "flat_or_inverted_3m"]}
    n_bucket_tests = len(STRATEGIES) * sum(len(v) for v in schemes.values())
    n_contrasts = len(STRATEGIES) * len(schemes)
    n_hypotheses = n_bucket_tests + n_contrasts
    bonf = 0.05 / n_hypotheses

    results: dict = {
        "window": [str(args.start), str(args.end)],
        "symbols": list(args.symbols),
        "n_sessions_labeled": int(len(tbl)),
        "interval": args.interval,
        "edge_param": args.edge,
        "tercile_breaks_vix": list(lab.attrs["tercile_breaks"]),
        "pnl_units": "USD per day, equal-weight mean across per-symbol $100k books, NET of costs",
        "labeling": "prior-session vix_family close (PIT, tradable next open); "
                    "tercile breakpoints computed within window (in-sample, descriptive)",
        "multiple_testing": {
            "n_bucket_tests": n_bucket_tests, "n_contrasts": n_contrasts,
            "n_hypotheses": n_hypotheses, "bonferroni_alpha": bonf,
            "note": "exploratory conditioning; NOT an edge claim under any outcome",
        },
        "unconditional": {}, "conditional": {}, "contrasts": {},
    }

    for name in STRATEGIES:
        x_all = tbl[f"pnl_{name}"].to_numpy(dtype="float64")
        results["unconditional"][name] = _bucket_stats(x_all)
        results["conditional"][name] = {}
        results["contrasts"][name] = {}
        for scheme, buckets in schemes.items():
            results["conditional"][name][scheme] = {}
            arrays = {}
            for b in buckets:
                x = tbl.loc[tbl[scheme] == b, f"pnl_{name}"].to_numpy(dtype="float64")
                arrays[b] = x
                results["conditional"][name][scheme][b] = _bucket_stats(x)
            a, b = buckets[0], buckets[-1]  # extreme-bucket contrast
            results["contrasts"][name][scheme] = {
                "contrast": f"{a} vs {b}", **_welch(arrays[a], arrays[b])}

    json_path = os.path.join(args.out_dir, "regime_stratification_results.json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # human summary
    print(f"window {args.start}..{args.end}  sessions={len(tbl)}  symbols={len(args.symbols)}")
    print(f"hypotheses={n_hypotheses}  Bonferroni alpha={bonf:.5f}")
    print(f"VIX tercile breaks (prior close): {lab.attrs['tercile_breaks']}")
    for name in STRATEGIES:
        u = results["unconditional"][name]
        print(f"\n[{name}] unconditional: n={u['n']} mean={u['mean_pnl_per_day']:+.2f} "
              f"+/-{u['se']:.2f} $/day  total={u['total_pnl']:+.0f}  t={u['t']:.2f} p={u['p']:.3f}")
        for scheme, buckets in schemes.items():
            for b in buckets:
                s = results["conditional"][name][scheme][b]
                if s["n"] == 0:
                    print(f"  {scheme:12} {b:22} n=0"); continue
                print(f"  {scheme:12} {b:22} n={s['n']:>2} mean={s['mean_pnl_per_day']:+8.2f} "
                      f"+/-{s['se']:6.2f}  t={s['t']:+5.2f} p={s['p']:.3f}")
            c = results["contrasts"][name][scheme]
            print(f"  {scheme:12} CONTRAST {c['contrast']}: "
                  f"diff={c.get('mean_diff', float('nan')):+.2f} t={c['t']:+.2f} p={c['p']:.3f}")
    print(f"\nJSON -> {json_path}\nCSV  -> {csv_path}")
    print("VERDICT FRAME: exploratory; all buckets n<{:.0f} sessions => INSUFFICIENT DATA "
          "for a definitive regime split.".format(max(
              results["conditional"][n][sc][b]["n"]
              for n in STRATEGIES for sc in schemes for b in schemes[sc]) + 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
