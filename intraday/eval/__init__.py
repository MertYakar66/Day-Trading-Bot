"""Rigorous, honesty-first strategy evaluation (the anti-overfitting layer).

A net-of-cost backtest number means nothing without (a) the right effective sample
size, (b) confidence intervals, and (c) a multiple-testing penalty for the number
of strategies/symbols tried. This package provides:

- :mod:`intraday.eval.stats` — daily-PnL extraction, clustered t-stat (each
  trading day is one observation, so correlated intraday trades are not
  double-counted), annualized Sharpe, a stationary-bootstrap CI, and the
  Probabilistic / Deflated Sharpe Ratio (Bailey & López de Prado) that discounts
  the best-of-N-trials selection bias.
- :mod:`intraday.eval.walkforward` — chronological (leakage-free) train/test
  splits and walk-forward windows.

The discipline: report the DEFLATED Sharpe across the full set of trials, evaluate
out-of-sample, and let "no edge" be the answer when it is the truth.
"""

from __future__ import annotations

from .stats import (
    StrategyEval,
    annualized_sharpe,
    clustered_t_stat,
    daily_pnl_from_result,
    deflated_sharpe_ratio,
    evaluate_daily_pnl,
    evaluate_result,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_ci,
)
from .walkforward import chronological_split, walk_forward_windows

__all__ = [
    "StrategyEval",
    "daily_pnl_from_result",
    "clustered_t_stat",
    "annualized_sharpe",
    "stationary_bootstrap_ci",
    "probabilistic_sharpe_ratio",
    "expected_max_sharpe",
    "deflated_sharpe_ratio",
    "evaluate_daily_pnl",
    "evaluate_result",
    "chronological_split",
    "walk_forward_windows",
]
