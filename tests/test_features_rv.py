"""Tests for intraday.features.realized_vol (SWE-backed RV estimators).

Covers:
- bars_per_day for 1m/5m intervals (390 / 78 bars per RTH session);
- annualization_factor(1m) == sqrt(390);
- intraday_rv: finite-positive on a full synthetic SPY day, None for too-few bars;
- the SWE rescale relationship: intraday_rv == swe.<estimator>_vol * annualization_factor.

All values are deterministic (synthetic provider) and network-free.
"""

from __future__ import annotations

import math

import pytest

from intraday.features.realized_vol import (
    annualization_factor,
    bars_per_day,
    intraday_rv,
)

pytestmark = pytest.mark.swe


def test_bars_per_day_1m_is_390():
    # RTH = 6.5h = 390 minutes -> 390 one-minute bars.
    assert bars_per_day("1m") == pytest.approx(390.0)


def test_bars_per_day_5m_is_78():
    # 390 minutes / 5 = 78 five-minute bars.
    assert bars_per_day("5m") == pytest.approx(78.0)


def test_bars_per_day_scales_inversely_with_interval():
    # A 1m session has exactly 5x the bars of a 5m session.
    assert bars_per_day("1m") == pytest.approx(5.0 * bars_per_day("5m"))


def test_annualization_factor_1m_is_sqrt_390():
    assert annualization_factor("1m") == pytest.approx(math.sqrt(390.0))


def test_annualization_factor_equals_sqrt_bars_per_day():
    for interval in ("1m", "5m"):
        assert annualization_factor(interval) == pytest.approx(
            math.sqrt(bars_per_day(interval))
        )


def test_intraday_rv_finite_positive_on_full_day(spy_bars):
    rv = intraday_rv(spy_bars.frame, "1m", window=30, estimator="garman_klass")
    assert rv is not None
    assert math.isfinite(rv)
    assert rv > 0.0
    # Annualized intraday vol for a liquid ETF should be a sane decimal,
    # comfortably under 1000% even with intraday rescaling.
    assert 0.0 < rv < 10.0


def test_intraday_rv_none_for_too_few_bars(spy_bars):
    # window=30 requires at least 31 bars; a 5-row frame is far too short.
    short = spy_bars.frame.head(5)
    assert len(short) == 5
    assert intraday_rv(short, "1m", window=30, estimator="garman_klass") is None


def test_intraday_rv_none_when_exactly_window_bars(spy_bars):
    # Boundary: need window+1 bars; exactly `window` is still insufficient.
    exactly_window = spy_bars.frame.head(30)
    assert len(exactly_window) == 30
    assert intraday_rv(exactly_window, "1m", window=30) is None


def test_intraday_rv_present_at_window_plus_one(spy_bars):
    # One more bar than the window crosses the threshold to a real estimate.
    just_enough = spy_bars.frame.head(31)
    assert len(just_enough) == 31
    rv = intraday_rv(just_enough, "1m", window=30, estimator="garman_klass")
    assert rv is not None
    assert math.isfinite(rv) and rv > 0.0


def test_intraday_rv_none_for_none_input():
    assert intraday_rv(None, "1m", window=30) is None


def test_intraday_rv_unknown_estimator_raises(spy_bars):
    with pytest.raises(ValueError):
        intraday_rv(spy_bars.frame, "1m", estimator="not_a_real_estimator")


def test_intraday_rv_rescale_close_to_close(spy_bars):
    # The documented invariant: intraday_rv on the full frame equals the raw
    # 252-day SWE estimator (which internally consumes tail(window+1)) times the
    # intraday annualization factor sqrt(bars_per_day).
    from engine import realized_vol as swe_rv

    window = 30
    interval = "1m"
    swe_daily = swe_rv.close_to_close_vol(spy_bars.frame, window=window)
    assert math.isfinite(swe_daily) and swe_daily > 0.0

    expected = swe_daily * annualization_factor(interval)
    got = intraday_rv(
        spy_bars.frame, interval, window=window, estimator="close_to_close"
    )
    assert got is not None
    assert got == pytest.approx(expected, rel=1e-12)


def test_intraday_rv_rescale_garman_klass(spy_bars):
    # Same rescale identity for the default OHLC estimator.
    from engine import realized_vol as swe_rv

    window = 30
    swe_daily = swe_rv.garman_klass_vol(spy_bars.frame, window=window)
    expected = swe_daily * annualization_factor("1m")
    got = intraday_rv(spy_bars.frame, "1m", window=window, estimator="garman_klass")
    assert got == pytest.approx(expected, rel=1e-12)


def test_intraday_rv_estimators_disagree_but_same_order(spy_bars):
    # Different estimators give distinct (but same-magnitude) annualized vols.
    gk = intraday_rv(spy_bars.frame, "1m", window=30, estimator="garman_klass")
    c2c = intraday_rv(spy_bars.frame, "1m", window=30, estimator="close_to_close")
    assert gk is not None and c2c is not None
    assert gk != pytest.approx(c2c)
    # Both should be the same order of magnitude (within ~10x).
    assert 0.1 < gk / c2c < 10.0
