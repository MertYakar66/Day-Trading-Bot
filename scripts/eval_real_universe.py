"""Powered real-data evaluation across the universe — honest, multiple-testing-aware.

Runs each underlying-only strategy (S3 reversion / S4 ORB breakout / S5 momentum)
on every symbol in the ingested real universe, net of costs, then scores them with
the honesty harness (:mod:`intraday.eval`):

- per-strategy EQUAL-WEIGHT portfolio daily PnL → clustered-t, annualized Sharpe +
  stationary-bootstrap CI;
- the **Deflated Sharpe Ratio** over ALL strategy×symbol trials (so the best of N
  cannot masquerade as an edge);
- an out-of-sample check: pick the best strategy on the TRAIN half, report its
  performance on the held-out TEST half.

Each symbol is run standalone (no cross-symbol concurrency cap), then aggregated —
the correct cross-sectional treatment. Prints a human summary and writes JSON.

Run::

    python -m scripts.eval_real_universe --store-root data_raw/store_yahoo \
        --start 2026-03-09 --end 2026-06-01 --interval 5m
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "vendor", "swe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday.authority.reviewers import default_reviewers  # noqa: E402
from intraday.backtest.engine import IntradayBacktester  # noqa: E402
from intraday.config import EngineConfig  # noqa: E402
from intraday.contracts import DataSource  # noqa: E402
from intraday.data.store import ParquetStore  # noqa: E402
from intraday.data.store_provider import StoreBackedProvider  # noqa: E402
from intraday.eval import (  # noqa: E402
    annualized_sharpe,
    chronological_split,
    clustered_t_stat,
    daily_pnl_from_result,
    evaluate_daily_pnl,
)
from intraday.signals.s3_vwap_orb import S3VwapOrb  # noqa: E402
from intraday.signals.s4_orb_breakout import S4OpeningRangeBreakout  # noqa: E402
from intraday.signals.s5_vwap_momentum import S5VwapMomentum  # noqa: E402

DEFAULT_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "SMH", "GLD", "TLT",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "JPM", "XOM", "AVGO",
)


def _strategy(name: str, edge: float):
    return {
        "s3": lambda: S3VwapOrb(edge=edge),
        "s4": lambda: S4OpeningRangeBreakout(edge=edge),
        "s5": lambda: S5VwapMomentum(edge=edge),
    }[name]()


def _daily_sharpe(daily: pd.Series) -> float:
    x = np.asarray(daily, dtype="float64")
    x = x[np.isfinite(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return 0.0
    return float(x.mean() / x.std(ddof=1))


def run(store_root: str, symbols, strategies, start, end, interval, edge, train_frac):
    cfg = EngineConfig.default()
    reviewers = default_reviewers(cfg)  # build once; reuse across all runs
    store = ParquetStore(store_root)

    # per-(strategy, symbol) daily PnL series + aggregate gross/net/trades (cost attribution)
    daily_by: dict[str, dict[str, pd.Series]] = {s: {} for s in strategies}
    agg: dict[str, dict[str, float]] = {s: {"gross": 0.0, "net": 0.0, "trades": 0} for s in strategies}
    for name in strategies:
        for sym in symbols:
            prov = StoreBackedProvider(store, DataSource.YAHOO, symbols=[sym], interval=interval)
            days = prov.trading_days(start, end)
            if not days:
                continue
            bt = IntradayBacktester(cfg, prov, [_strategy(name, edge)], reviewers=reviewers)
            res = bt.run([sym], start, end, interval)
            daily_by[name][sym] = daily_pnl_from_result(res)
            agg[name]["gross"] += sum(t.gross_pnl for t in res.trades)
            agg[name]["net"] += sum(t.net_pnl for t in res.trades)
            agg[name]["trades"] += len(res.trades)

    # Trial pool for multiple-testing: every strategy×symbol daily Sharpe. var_sr is
    # the cross-trial DISPERSION feeding the Deflated-Sharpe benchmark (the benchmark
    # assumes a zero-centered no-skill null per Bailey-LdP; our live pool is
    # negatively centered, which only makes the test MORE conservative — see
    # intraday.eval.stats.expected_max_sharpe). Each per-symbol run is its OWN $100k
    # book (its own daily kill-switch), so the equal-weight portfolio below is the
    # average of N independently-capitalized single-symbol books, not one account —
    # the Sharpe/t verdict is scale-invariant either way; book_$ totals are a sum.
    trial_sharpes = [
        _daily_sharpe(s) for name in strategies for s in daily_by[name].values() if len(s) >= 2
    ]
    n_trials = max(len(trial_sharpes), 1)
    var_sr = float(np.var(trial_sharpes, ddof=1)) if len(trial_sharpes) > 1 else 1e-6

    # equal-weight portfolio daily PnL per strategy (mean across symbols per day)
    def portfolio(name: str, day_filter=None) -> pd.Series:
        cols = []
        for sym, s in daily_by[name].items():
            ss = s if day_filter is None else s[[d in day_filter for d in s.index.date]]
            cols.append(ss.rename(sym))
        if not cols:
            return pd.Series(dtype="float64")
        df = pd.concat(cols, axis=1).sort_index().fillna(0.0)
        return df.mean(axis=1)

    all_days = sorted({d for name in strategies for s in daily_by[name].values() for d in s.index.date})
    train_days, test_days = chronological_split(all_days, train_frac)

    out = {"n_trials": n_trials, "var_sr": var_sr, "n_symbols": len(symbols),
           "n_days": len(all_days), "strategies": {}}
    train_ports: dict[str, pd.Series] = {}
    test_ports: dict[str, pd.Series] = {}
    for name in strategies:
        port = portfolio(name)
        ev = evaluate_daily_pnl(port, n_trials=len(strategies), var_sr=var_sr)
        # DSR of this strategy's portfolio against the FULL trial pool (the honest number)
        ev_full = evaluate_daily_pnl(port, n_trials=n_trials, var_sr=var_sr)
        tr = train_ports[name] = portfolio(name, set(train_days))
        te = test_ports[name] = portfolio(name, set(test_days))
        out["strategies"][name] = {
            "total_net": float(port.sum()),
            "book_gross": agg[name]["gross"],          # summed across all symbols
            "book_net": agg[name]["net"],
            "book_costs": agg[name]["gross"] - agg[name]["net"],
            "n_trades": agg[name]["trades"],
            "sharpe_ann": ev.sharpe_ann,
            "sharpe_ci": [ev.sharpe_ann_ci_lo, ev.sharpe_ann_ci_hi],
            "t_stat": ev.t_stat, "p_value": ev.p_value,
            "psr_vs_zero": ev.psr_vs_zero,
            "dsr_vs_strategies": ev.deflated_sharpe,
            "dsr_vs_all_trials": ev_full.deflated_sharpe,
            "train_sharpe_ann": annualized_sharpe(tr),
            "test_sharpe_ann": annualized_sharpe(te),
            "test_t": clustered_t_stat(te)[0],
            "n_symbols_traded": sum(1 for s in daily_by[name].values() if s.abs().sum() > 0),
        }

    # Per-symbol annualized Sharpe matrix (additive; feeds the dashboard heatmap via
    # `intraday report --universe-json`). Per-symbol single-book Sharpe, net of costs.
    out["per_symbol"] = {
        name: {sym: annualized_sharpe(s) for sym, s in daily_by[name].items() if len(s) >= 2}
        for name in strategies
    }

    # Cleanest DSR statement: the SINGLE BEST of all strategy×symbol trials,
    # deflated by the full trial count (this is the canonical Bailey-LdP target).
    combos = [(name, sym, s) for name in strategies for sym, s in daily_by[name].items()
              if len(s) >= 2]
    if combos:
        bn, bs, bser = max(combos, key=lambda c: annualized_sharpe(c[2]))
        bev = evaluate_daily_pnl(bser, n_trials=n_trials, var_sr=var_sr)
        out["best_trial"] = {
            "strategy": bn, "symbol": bs, "sharpe_ann": bev.sharpe_ann,
            "t_stat": bev.t_stat, "deflated_sharpe": bev.deflated_sharpe,
            "total_net": float(bser.sum()),
        }

    # OOS: pick best strategy by TRAIN Sharpe, report its TEST eval (reuse cached ports)
    best = max(strategies, key=lambda n: annualized_sharpe(train_ports[n]))
    te_best = test_ports[best]
    out["oos_pick"] = {
        "selected_on_train": best,
        "train_sharpe_ann": annualized_sharpe(train_ports[best]),
        "test_sharpe_ann": annualized_sharpe(te_best),
        "test_t_stat": clustered_t_stat(te_best)[0],
        "test_total_net": float(te_best.sum()),
    }
    return out


def _render(out: dict) -> str:
    L = ["=" * 78,
         " REAL-DATA UNIVERSE EVALUATION - net of costs, multiple-testing-aware",
         "=" * 78,
         f" symbols={out['n_symbols']}  sessions={out['n_days']}  "
         f"trials(strategy x symbol)={out['n_trials']}",
         "-" * 78,
         f" {'strat':6} {'net$':>10} {'Sharpe':>7} {'Sharpe 95% CI':>18} {'t':>6} "
         f"{'p':>6} {'DSR(all)':>9}",
         "-" * 78]
    for name, s in out["strategies"].items():
        ci = f"[{s['sharpe_ci'][0]:.2f},{s['sharpe_ci'][1]:.2f}]"
        L.append(f" {name:6} {s['total_net']:>10.0f} {s['sharpe_ann']:>7.2f} {ci:>18} "
                 f"{s['t_stat']:>6.2f} {s['p_value']:>6.3f} {s['dsr_vs_all_trials']:>9.3f}")
    L.append("-" * 78)
    L.append(f" cost attribution (whole 24-symbol book): {'gross$':>10} {'costs$':>10} {'net$':>10} {'trades':>8}")
    for name, s in out["strategies"].items():
        L.append(f" {name:6}{'':36}{s['book_gross']:>10.0f} {s['book_costs']:>10.0f} "
                 f"{s['book_net']:>10.0f} {s['n_trades']:>8}")
    if "best_trial" in out:
        b = out["best_trial"]
        L += ["-" * 78,
              f" BEST single trial of {out['n_trials']}: {b['strategy']}/{b['symbol']} "
              f"Sharpe {b['sharpe_ann']:.2f} t={b['t_stat']:.2f} "
              f"net ${b['total_net']:.0f}  DSR={b['deflated_sharpe']:.3f}"]
    o = out["oos_pick"]
    L += ["-" * 78,
          f" OOS: best-on-train = {o['selected_on_train']} "
          f"(train SR {o['train_sharpe_ann']:.2f}) -> TEST SR {o['test_sharpe_ann']:.2f}, "
          f"t={o['test_t_stat']:.2f}, net ${o['test_total_net']:.0f}",
          " Verdict: DSR(all) >= 0.95 is the formal edge criterion (the sole authority,"
          " as on every other surface, subject to the INSUFFICIENT_DATA_MIN_DAYS validity"
          " floor); OOS test SR > 0 with t ~>= 2 is corroborating.",
          " Note: t/PSR/DSR assume serially-independent daily PnL; positive day-to-day",
          "       autocorrelation biases them upward - the bootstrap Sharpe CI is the",
          "       autocorrelation-robust counterweight.",
          "=" * 78]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Powered real-data universe evaluation.")
    p.add_argument("--store-root", default="data_raw/store_yahoo")
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_UNIVERSE))
    p.add_argument("--strategies", nargs="+", default=["s3", "s4", "s5"])
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 3, 9))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 1))
    p.add_argument("--interval", default="5m")
    p.add_argument("--edge", type=float, default=0.10)
    p.add_argument("--train-frac", type=float, default=0.6)
    p.add_argument("--out", default="data_raw/real_eval_results.json")
    args = p.parse_args(argv)

    out = run(args.store_root, args.symbols, args.strategies, args.start, args.end,
              args.interval, args.edge, args.train_frac)
    with open(args.out, "w") as f:  # persist first (robust to console encoding)
        json.dump(out, f, indent=2)
    print(_render(out))
    print(f"\n(JSON written to {args.out})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
