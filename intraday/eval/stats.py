"""Honesty statistics: clustered-t, bootstrap CIs, and Deflated Sharpe.

All inputs are a **daily PnL** (or daily return) series — one observation per
trading day. Using the day as the unit of observation clusters the many correlated
intraday trades within a day, so significance is not overstated by trade count.

Multiple-testing: when you try ``N`` strategy/symbol combinations and report the
best, its Sharpe is upward-biased. The Deflated Sharpe Ratio (Bailey &
López de Prado, 2014) gives the probability the TRUE Sharpe exceeds 0 after
discounting that selection over ``N`` trials and the series' non-normality.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy import stats as _ss

_EULER_GAMMA = 0.5772156649015329

# A strategy is credited with a demonstrated edge only if its Deflated Sharpe Ratio
# (the multiple-testing-honest P(true Sharpe > 0)) clears this one-sided 5% bar.
# This is the SOLE authority for the EDGE / NO-EDGE verdict everywhere (the report
# banner, the CLI scorecard) — defined once here so the threshold is never a magic
# number scattered across modules.
DEFLATED_SHARPE_SIGNIFICANCE_THRESHOLD = 0.95


def daily_pnl_from_result(result) -> pd.Series:
    """Portfolio daily PnL ($) from a BacktestResult's equity curve.

    The equity curve is already aggregated across symbols (one portfolio value per
    day), so its first difference is the portfolio daily PnL — the correct unit for
    a clustered, cross-sectionally-aware test.
    """
    pv = [row["portfolio_value"] for row in result.equity_curve]
    dates = [pd.Timestamp(row["date"]) for row in result.equity_curve]
    if not pv:
        return pd.Series(dtype="float64")
    prev = result.initial_capital
    daily = []
    for v in pv:
        daily.append(v - prev)
        prev = v
    return pd.Series(daily, index=pd.DatetimeIndex(dates), name="daily_pnl")


def clustered_t_stat(daily: pd.Series) -> tuple[float, float]:
    """One-sample t-stat (and two-sided p) of mean daily PnL vs 0.

    Each trading day is one observation (intraday trades are clustered into their
    day), so this does not inflate significance by trade count.
    """
    x = np.asarray(daily, dtype="float64")
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return (0.0, 1.0)
    sd = x.std(ddof=1)
    if sd == 0:
        return (0.0, 1.0)
    t = x.mean() / (sd / math.sqrt(n))
    p = 2.0 * _ss.t.sf(abs(t), df=n - 1)
    return (float(t), float(p))


def annualized_sharpe(daily: pd.Series, *, periods_per_year: int = 252) -> float:
    """Annualized Sharpe from a daily PnL/return series (0 if undefined)."""
    x = np.asarray(daily, dtype="float64")
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(x.mean() / sd * math.sqrt(periods_per_year))


def stationary_bootstrap_ci(
    daily: pd.Series,
    statistic,
    *,
    n_boot: int = 2000,
    mean_block: float = 5.0,
    alpha: float = 0.05,
    rng_seed: int = 7,
) -> tuple[float, float]:
    """Stationary-bootstrap (Politis-Romano) CI for ``statistic`` on a daily series.

    The stationary bootstrap resamples geometric-length blocks (mean ``mean_block``)
    with wrap-around, preserving short-range autocorrelation — appropriate for
    serially dependent daily PnL. Returns the (alpha/2, 1-alpha/2) percentile interval.
    """
    x = np.asarray(daily, dtype="float64")
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(rng_seed)
    p = 1.0 / max(mean_block, 1.0)
    stats_out = np.empty(n_boot)
    for b in range(n_boot):
        idx = np.empty(n, dtype=np.int64)
        i = rng.integers(0, n)
        for k in range(n):
            idx[k] = i
            if rng.random() < p:
                i = rng.integers(0, n)  # start a new block
            else:
                i = (i + 1) % n          # continue the block (wrap)
        stats_out[b] = statistic(pd.Series(x[idx]))
    lo = float(np.percentile(stats_out, 100 * alpha / 2))
    hi = float(np.percentile(stats_out, 100 * (1 - alpha / 2)))
    return (lo, hi)


def probabilistic_sharpe_ratio(
    sr_hat: float, n: int, skew: float, kurt: float, *, sr_star: float = 0.0
) -> float:
    """PSR: P(true Sharpe > ``sr_star``) given the observed per-period Sharpe.

    ``sr_hat``/``sr_star`` are PER-OBSERVATION (daily) Sharpes; ``kurt`` is the
    non-excess kurtosis (normal = 3). Bailey & López de Prado (2012).
    """
    if n < 2:
        return float("nan")
    denom = 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * sr_hat * sr_hat
    if denom <= 0:
        return float("nan")
    z = (sr_hat - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(_ss.norm.cdf(z))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected maximum per-period Sharpe under ``n_trials`` independent trials with
    cross-trial Sharpe variance ``var_sr`` (the DSR deflation benchmark, SR*).

    Follows Bailey & López de Prado: the trials are assumed ZERO-CENTERED (the
    no-skill null E[SR]=0), so SR* = sqrt(var_sr)·Gumbel(N). If the live trial pool
    is negatively centered (e.g. all strategies lose), this SR* is slightly
    over-stated, which only makes DSR HARDER to pass — i.e. the multiple-testing
    test is conservative, never lenient.
    """
    n_trials = max(int(n_trials), 1)
    if n_trials == 1 or var_sr <= 0:
        return 0.0
    sigma = math.sqrt(var_sr)
    z1 = _ss.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = _ss.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sigma * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2))


def deflated_sharpe_ratio(
    sr_hat: float, n: int, skew: float, kurt: float, *, n_trials: int, var_sr: float
) -> float:
    """DSR: PSR with the benchmark set to the expected max Sharpe over ``n_trials``.

    The probability the strategy's true Sharpe beats what the BEST of ``n_trials``
    random strategies would show by luck — the multiple-testing-honest statistic.
    """
    sr_star = expected_max_sharpe(n_trials, var_sr)
    return probabilistic_sharpe_ratio(sr_hat, n, skew, kurt, sr_star=sr_star)


def evaluate_result(result, **kwargs) -> StrategyEval:
    """Convenience: honesty scorecard straight from a BacktestResult."""
    return evaluate_daily_pnl(daily_pnl_from_result(result), **kwargs)


@dataclass(frozen=True)
class StrategyEval:
    """Honesty scorecard for one daily-PnL series."""

    n_days: int
    total_pnl: float
    mean_daily_pnl: float
    win_days: int
    t_stat: float
    p_value: float
    sharpe_ann: float
    sharpe_ann_ci_lo: float
    sharpe_ann_ci_hi: float
    skew: float
    kurtosis: float            # non-excess (normal = 3)
    psr_vs_zero: float         # P(true Sharpe > 0), single-trial
    n_trials: int
    deflated_sharpe: float     # P(true Sharpe > 0) after multiple-testing penalty
    significant: bool          # DSR >= 0.95 (one-sided 5%)

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_daily_pnl(
    daily: pd.Series,
    *,
    n_trials: int = 1,
    var_sr: float | None = None,
    periods_per_year: int = 252,
    n_boot: int = 2000,
    rng_seed: int = 7,
) -> StrategyEval:
    """Full honesty scorecard for a daily PnL series.

    ``n_trials`` is the number of strategy/symbol combinations searched; ``var_sr``
    is the cross-trial variance of the per-period Sharpe (pass the variance of all
    trials' daily Sharpes for a proper DSR; defaults to the single-series estimator
    variance, which is conservative-ish but less ideal).
    """
    x = np.asarray(daily, dtype="float64")
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return StrategyEval(n, float(x.sum()) if n else 0.0, 0.0, int((x > 0).sum()),
                            0.0, 1.0, 0.0, float("nan"), float("nan"), 0.0, 3.0,
                            float("nan"), n_trials, float("nan"), False)

    sd = x.std(ddof=1)
    sr_daily = 0.0 if sd == 0 else x.mean() / sd
    skew = float(_ss.skew(x)) if sd > 0 else 0.0
    kurt = float(_ss.kurtosis(x, fisher=False)) if sd > 0 else 3.0
    t, p = clustered_t_stat(x)
    sr_ann = annualized_sharpe(x, periods_per_year=periods_per_year)
    ci_lo, ci_hi = stationary_bootstrap_ci(
        x, lambda s: annualized_sharpe(s, periods_per_year=periods_per_year),
        n_boot=n_boot, rng_seed=rng_seed,
    )
    psr0 = probabilistic_sharpe_ratio(sr_daily, n, skew, kurt, sr_star=0.0)
    # DSR deflation input. The IDEAL var_sr is the cross-trial variance of the
    # per-period Sharpe (pass it explicitly). When absent we fall back to the
    # single-series estimator variance of this Sharpe — conservative-ish, but not a
    # true cross-trial spread (see the docstring).
    if var_sr is None:
        var_sr = (1.0 - skew * sr_daily + ((kurt - 1.0) / 4.0) * sr_daily ** 2) / (n - 1)
    dsr = deflated_sharpe_ratio(sr_daily, n, skew, kurt, n_trials=n_trials, var_sr=var_sr)
    return StrategyEval(
        n_days=n,
        total_pnl=float(x.sum()),
        mean_daily_pnl=float(x.mean()),
        win_days=int((x > 0).sum()),
        t_stat=t,
        p_value=p,
        sharpe_ann=sr_ann,
        sharpe_ann_ci_lo=ci_lo,
        sharpe_ann_ci_hi=ci_hi,
        skew=skew,
        kurtosis=kurt,
        psr_vs_zero=psr0,
        n_trials=n_trials,
        deflated_sharpe=dsr,
        significant=(
            bool(dsr >= DEFLATED_SHARPE_SIGNIFICANCE_THRESHOLD)
            if math.isfinite(dsr) else False
        ),
    )
