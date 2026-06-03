"""Machine-readable summary of a backtest (the JSON sibling of the HTML dashboard).

``summary_dict`` flattens a :class:`~intraday.backtest.engine.BacktestResult`, its
:class:`~intraday.metrics.MetricsReport`, and its honesty
:class:`~intraday.eval.stats.StrategyEval` into a small, stable JSON-serialisable
dict — handy for diffing runs, feeding a notebook, or wiring CI gates. Like the
dashboard, the EDGE/NO-EDGE verdict is the eval's (``significant``), and the data
source (real vs synthetic) is always carried so a number can't be misattributed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from ..backtest.engine import BacktestResult
from ..eval import evaluate_result
from ..eval.stats import StrategyEval
from ..metrics import MetricsReport, build_report


def _num(x) -> float | None:
    """JSON-safe float: non-finite (NaN/±inf) becomes ``None`` (valid JSON)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def summary_dict(
    result: BacktestResult,
    metrics: MetricsReport,
    ev: StrategyEval,
    *,
    run_meta: Mapping | None = None,
) -> dict:
    """A compact, stable, JSON-serialisable summary of one backtest."""
    rm = run_meta or {}
    return {
        "schema": "intraday.report.summary/1",
        "data_source": result.data_source.value,
        "is_real": bool(result.data_source.is_real),
        "symbols": list(result.symbols),
        "interval": result.interval,
        "window": {"start": rm.get("start"), "end": rm.get("end")},
        "strategies": rm.get("strategies"),
        "n_days": result.n_days,
        "initial_capital": _num(result.initial_capital),
        "final_equity": _num(result.final_equity),
        "pnl": {
            "gross": _num(metrics.gross_pnl),
            "net": _num(metrics.net_pnl),
            "costs": _num(metrics.total_costs),
        },
        "metrics": {
            "sharpe": _num(metrics.sharpe_ratio),
            "sortino": _num(metrics.sortino_ratio),
            "max_drawdown": _num(metrics.max_drawdown),
            "calmar": _num(metrics.calmar_ratio),
            "total_return": _num(metrics.total_return),
            "annualized_return": _num(metrics.annualized_return),
            "win_rate": _num(metrics.win_rate),
            "profit_factor": _num(metrics.profit_factor),
            "expectancy_per_trade": _num(metrics.expectancy_per_trade),
            "payoff_ratio": _num(metrics.payoff_ratio),
            "turnover": _num(metrics.turnover),
            "total_trades": metrics.total_trades,
            "cost_bps_of_notional": _num(metrics.cost_bps_of_notional),
            "cost_pct_of_nav": _num(metrics.cost_pct_of_nav),
        },
        "eval": {
            "n_days": ev.n_days,
            "t_stat": _num(ev.t_stat),
            "p_value": _num(ev.p_value),
            "sharpe_ann": _num(ev.sharpe_ann),
            "sharpe_ann_ci": [_num(ev.sharpe_ann_ci_lo), _num(ev.sharpe_ann_ci_hi)],
            "psr_vs_zero": _num(ev.psr_vs_zero),
            "deflated_sharpe": _num(ev.deflated_sharpe),
            "n_trials": ev.n_trials,
            "significant": bool(ev.significant),
        },
        "verdict": "EDGE" if ev.significant else "NO_EDGE",
    }


def build_summary(
    result: BacktestResult, *, n_trials: int = 1, run_meta: Mapping | None = None
) -> dict:
    """Convenience: compute the metrics + honesty eval, then build the summary dict."""
    metrics = build_report(result)
    ev = evaluate_result(result, n_trials=n_trials)
    return summary_dict(result, metrics, ev, run_meta=run_meta)
