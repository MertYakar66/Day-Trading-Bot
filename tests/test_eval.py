"""Tests for the honesty/evaluation harness (intraday.eval).

Pins the statistics that keep the engine honest: a clustered t-stat that scales
with effect size, a bootstrap CI that brackets the point estimate, and — most
importantly — the Deflated Sharpe Ratio that PENALIZES multiple testing so a
best-of-N selection cannot masquerade as an edge.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from intraday.eval import (
    annualized_sharpe,
    chronological_split,
    clustered_t_stat,
    daily_pnl_from_result,
    deflated_sharpe_ratio,
    evaluate_daily_pnl,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_ci,
    walk_forward_windows,
)
from intraday.eval.stats import _EULER_GAMMA  # noqa: F401 (existence check)


def _series(mean, sd, n, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, sd, n))


# --------------------------------------------------------------------------- #
# Clustered t-stat
# --------------------------------------------------------------------------- #
def test_t_stat_detects_positive_drift():
    t, p = clustered_t_stat(_series(50.0, 100.0, 150))
    assert t > 3.0 and p < 0.01


def test_t_stat_zero_mean_insignificant():
    t, p = clustered_t_stat(_series(0.0, 100.0, 150, seed=3))
    assert abs(t) < 2.0 and p > 0.05


def test_t_stat_degenerate_inputs():
    assert clustered_t_stat(pd.Series([1.0])) == (0.0, 1.0)
    assert clustered_t_stat(pd.Series([5.0, 5.0, 5.0])) == (0.0, 1.0)  # zero variance


# --------------------------------------------------------------------------- #
# Sharpe + bootstrap CI
# --------------------------------------------------------------------------- #
def test_annualized_sharpe_sign_and_scale():
    pos = annualized_sharpe(_series(10.0, 100.0, 252, seed=1))
    assert pos > 0
    neg = annualized_sharpe(_series(-10.0, 100.0, 252, seed=1))
    assert neg < 0


def test_bootstrap_ci_brackets_point_estimate():
    s = _series(8.0, 100.0, 200, seed=2)
    point = annualized_sharpe(s)
    lo, hi = stationary_bootstrap_ci(s, annualized_sharpe, n_boot=800, rng_seed=2)
    assert lo < point < hi
    assert lo < hi


# --------------------------------------------------------------------------- #
# PSR / DSR multiple-testing penalty (the crown jewel)
# --------------------------------------------------------------------------- #
def test_psr_extremes():
    # SR=0 → coin flip; strong positive SR over many obs → ~1.
    assert probabilistic_sharpe_ratio(0.0, 200, 0.0, 3.0) == pytest.approx(0.5, abs=1e-6)
    assert probabilistic_sharpe_ratio(0.4, 200, 0.0, 3.0) > 0.99


def test_expected_max_sharpe_grows_with_trials():
    a = expected_max_sharpe(2, 0.01)
    b = expected_max_sharpe(100, 0.01)
    assert 0.0 < a < b
    assert expected_max_sharpe(1, 0.01) == 0.0  # single trial → no deflation


def test_deflated_sharpe_penalizes_multiple_testing():
    sr, n, skew, kurt, var = 0.18, 120, 0.0, 3.0, 0.01
    single = deflated_sharpe_ratio(sr, n, skew, kurt, n_trials=1, var_sr=var)
    many = deflated_sharpe_ratio(sr, n, skew, kurt, n_trials=200, var_sr=var)
    assert single > many  # more trials ⇒ harder to be "real"
    assert single == pytest.approx(
        probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_star=0.0)
    )  # n_trials=1 reduces to PSR vs 0


def test_evaluate_daily_pnl_scorecard():
    strong = _series(40.0, 100.0, 150, seed=5)
    ev1 = evaluate_daily_pnl(strong, n_trials=1)
    assert ev1.n_days == 150
    assert ev1.t_stat > 3.0
    assert ev1.sharpe_ann > 0
    assert ev1.deflated_sharpe == pytest.approx(ev1.psr_vs_zero)  # 1 trial
    # Under heavy multiple testing the same series is no longer "significant".
    ev_many = evaluate_daily_pnl(strong, n_trials=5000)
    assert ev_many.deflated_sharpe < ev1.deflated_sharpe


def test_evaluate_noise_is_not_significant():
    noise = _series(0.0, 100.0, 150, seed=9)
    ev = evaluate_daily_pnl(noise, n_trials=20)
    assert ev.significant is False


def test_edge_verdict_is_three_state_with_insufficient_precedence():
    import dataclasses

    from intraday.eval import edge_verdict

    # < 2 days => INSUFFICIENT_DATA, regardless of significant
    assert edge_verdict(evaluate_daily_pnl(pd.Series([100.0]))) == "INSUFFICIENT_DATA"
    ev = evaluate_daily_pnl(_series(0.0, 100.0, 150, seed=9), n_trials=20)
    assert edge_verdict(ev) == "NO_EDGE"                                   # enough days, not significant
    assert edge_verdict(dataclasses.replace(ev, significant=True)) == "EDGE"
    # insufficient overrides a (fabricated) significant flag
    assert edge_verdict(dataclasses.replace(ev, n_days=1, significant=True)) == "INSUFFICIENT_DATA"


def test_evaluate_explicit_var_sr_is_monotone_and_differs_from_default():
    """A larger cross-trial Sharpe variance raises the DSR benchmark (SR*), so the
    deflated Sharpe FALLS; and an explicit var_sr differs from the single-series
    fallback (var_sr=None)."""
    s = _series(15.0, 100.0, 200, seed=11)
    small_var = evaluate_daily_pnl(s, n_trials=20, var_sr=1e-4)
    big_var = evaluate_daily_pnl(s, n_trials=20, var_sr=1.0)
    default = evaluate_daily_pnl(s, n_trials=20)  # var_sr=None -> single-series estimate (small)
    assert big_var.deflated_sharpe < small_var.deflated_sharpe   # more spread => harder
    assert default.deflated_sharpe > big_var.deflated_sharpe     # default var_sr << 1.0 => less deflated


# --------------------------------------------------------------------------- #
# Walk-forward / chronological split (leakage-free)
# --------------------------------------------------------------------------- #
def test_chronological_split_is_ordered_and_disjoint():
    days = [date(2026, 3, d) for d in range(1, 21)]
    train, test = chronological_split(days, train_frac=0.6)
    assert len(train) == 12 and len(test) == 8
    assert max(train) < min(test)  # strictly earlier
    assert set(train).isdisjoint(test)


def test_walk_forward_windows_are_sequential():
    days = [date(2026, 3, 1) + pd.Timedelta(days=i) for i in range(40)]
    days = [d.date() if hasattr(d, "date") else d for d in days]
    folds = walk_forward_windows(days, n_folds=3, min_train=10)
    assert len(folds) >= 1
    for w in folds:
        assert max(w.train) < min(w.test)  # test strictly follows train
        assert set(w.train).isdisjoint(w.test)


def test_chronological_split_rejects_degenerate_fracs():
    days = [date(2026, 3, d) for d in range(1, 11)]
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="train_frac"):
            chronological_split(days, train_frac=bad)


def test_walk_forward_windows_empty_when_underpowered():
    # n < min_train + n_folds => no fold can be formed (don't fabricate folds from too little data)
    few = [d.date() for d in pd.date_range("2026-03-01", periods=8)]
    assert walk_forward_windows(few, n_folds=4, min_train=10) == []


def test_walk_forward_rolling_window_moves_forward_no_lookahead():
    days = [d.date() for d in pd.date_range("2026-03-01", periods=40)]
    folds = walk_forward_windows(days, n_folds=3, expanding=False, min_train=10)
    assert len(folds) >= 2
    for w in folds:
        assert max(w.train) < min(w.test)          # leakage-free: test strictly follows train
        assert set(w.train).isdisjoint(w.test)
    # rolling (not expanding): a later fold's train starts AFTER an earlier fold's
    assert min(folds[-1].train) > min(folds[0].train)


def test_daily_pnl_from_result_diffs_equity_curve():
    class _R:
        initial_capital = 100_000.0
        equity_curve = [
            {"date": "2026-03-02", "portfolio_value": 100_100.0},
            {"date": "2026-03-03", "portfolio_value": 100_050.0},
            {"date": "2026-03-04", "portfolio_value": 100_400.0},
        ]
    daily = daily_pnl_from_result(_R())
    assert list(daily.to_numpy()) == [100.0, -50.0, 350.0]
