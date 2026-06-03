"""Tests for intraday.features.vwap and intraday.features.opening_range.

Pure-function feature math: causal session VWAP / volatility bands and the
opening-range (ORB) reference. All deterministic, network-free (synthetic
provider only). No SWE modules are imported here.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from intraday.features.opening_range import OpeningRange, opening_range
from intraday.features.vwap import vwap_bands, vwap_frame
from intraday.timeutils import session_bounds_utc

# --------------------------------------------------------------------------- #
# vwap_frame
# --------------------------------------------------------------------------- #


def test_vwap_frame_columns_and_index(spy_bars):
    out = vwap_frame(spy_bars.frame)
    assert list(out.columns) == ["vwap", "vwap_sigma", "vwap_dev_sigma"]
    # Same row count / index alignment as the input bars.
    assert len(out) == len(spy_bars.frame) == 390
    assert out.index.equals(spy_bars.frame.index)


def test_vwap_sigma_non_negative_and_finite(spy_bars):
    out = vwap_frame(spy_bars.frame)
    sigma = out["vwap_sigma"]
    assert (sigma >= 0.0).all()
    assert np.isfinite(sigma.to_numpy()).all()


def test_vwap_within_low_high_envelope(spy_bars):
    """The causal VWAP is a weighted average of typical prices, so it must lie
    inside the overall [low.min, high.max] envelope of the session."""
    df = spy_bars.frame
    out = vwap_frame(df)
    vwap = out["vwap"]
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    assert np.isfinite(vwap.to_numpy()).all()
    assert vwap.min() >= lo - 1e-9
    assert vwap.max() <= hi + 1e-9


def test_vwap_dev_sigma_definition(spy_bars):
    """vwap_dev_sigma == (close - vwap) / sigma wherever sigma > 0."""
    df = spy_bars.frame
    out = vwap_frame(df)
    sigma = out["vwap_sigma"]
    mask = sigma > 0
    # There must be some bars with positive sigma to make this meaningful.
    assert mask.sum() > 0
    expected = (df["close"][mask] - out["vwap"][mask]) / sigma[mask]
    got = out["vwap_dev_sigma"][mask]
    assert got.to_numpy() == pytest.approx(expected.to_numpy(), rel=1e-9, abs=1e-9)


def test_vwap_dev_sigma_nan_where_sigma_zero(spy_bars):
    """Where sigma is exactly zero (degenerate, e.g. the first bar), dev_sigma is
    NaN rather than +/-inf."""
    df = spy_bars.frame
    out = vwap_frame(df)
    zero_sigma = out["vwap_sigma"] == 0.0
    if zero_sigma.any():
        assert out["vwap_dev_sigma"][zero_sigma].isna().all()
    # And nowhere is dev_sigma infinite.
    dev = out["vwap_dev_sigma"].to_numpy()
    assert not np.isinf(dev).any()


def test_vwap_first_bar_equals_typical_price():
    """With a single bar (or the first cumulative point), VWAP equals the typical
    price and sigma is zero."""
    idx = pd.date_range("2026-05-18 13:31:00", periods=1, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000.0]},
        index=idx,
    )
    out = vwap_frame(df)
    typ = (101.0 + 99.0 + 100.5) / 3.0
    assert float(out["vwap"].iloc[0]) == pytest.approx(typ)
    assert float(out["vwap_sigma"].iloc[0]) == pytest.approx(0.0, abs=1e-12)
    assert math.isnan(float(out["vwap_dev_sigma"].iloc[0]))


def test_vwap_is_volume_weighted_not_simple_mean():
    """Heavier volume on a bar pulls VWAP toward that bar's typical price."""
    idx = pd.date_range("2026-05-18 13:31:00", periods=2, freq="1min", tz="UTC")
    # Bar 0 typ=10, Bar 1 typ=20, but bar 1 has 9x the volume.
    df = pd.DataFrame(
        {
            "high": [10.0, 20.0],
            "low": [10.0, 20.0],
            "close": [10.0, 20.0],
            "volume": [100.0, 900.0],
        },
        index=idx,
    )
    out = vwap_frame(df)
    # Cumulative VWAP at bar 1 = (10*100 + 20*900) / 1000 = 19.0, not 15.0.
    assert float(out["vwap"].iloc[1]) == pytest.approx(19.0)


def test_vwap_zero_volume_index_uses_equal_weights():
    """An index instrument (SPX) reports zero volume; VWAP must fall back to a
    cumulative mean of typical price with no NaN/inf."""
    provider_cfg = None  # placeholder to keep linters quiet; not used
    del provider_cfg
    idx = pd.date_range("2026-05-18 13:31:00", periods=4, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "high": [10.0, 12.0, 14.0, 16.0],
            "low": [10.0, 12.0, 14.0, 16.0],
            "close": [10.0, 12.0, 14.0, 16.0],
            "volume": [0.0, 0.0, 0.0, 0.0],
        },
        index=idx,
    )
    out = vwap_frame(df)
    # Equal weights => cumulative arithmetic mean of typical price (== close here).
    assert float(out["vwap"].iloc[0]) == pytest.approx(10.0)
    assert float(out["vwap"].iloc[1]) == pytest.approx((10.0 + 12.0) / 2)
    assert float(out["vwap"].iloc[3]) == pytest.approx((10 + 12 + 14 + 16) / 4)
    assert np.isfinite(out["vwap"].to_numpy()).all()


def test_vwap_zero_volume_spx_bars_no_nan_inf(provider, day):
    """Real synthetic SPX bars (volume all 0) produce a finite VWAP series."""
    spx = provider.get_bars("SPX", day, "1m")
    assert float(spx.frame["volume"].sum()) == 0.0  # index => no volume
    out = vwap_frame(spx.frame)
    vwap = out["vwap"].to_numpy()
    assert np.isfinite(vwap).all()
    assert not np.isnan(vwap).any()
    assert (out["vwap_sigma"] >= 0.0).all()
    assert np.isfinite(out["vwap_sigma"].to_numpy()).all()
    # dev_sigma never infinite.
    assert not np.isinf(out["vwap_dev_sigma"].to_numpy()).any()


# --------------------------------------------------------------------------- #
# vwap_bands
# --------------------------------------------------------------------------- #


def test_vwap_bands_upper_above_lower():
    upper, lower = vwap_bands(100.0, 2.0, k=1.5)
    assert upper == pytest.approx(103.0)
    assert lower == pytest.approx(97.0)
    assert upper > lower


def test_vwap_bands_symmetric_around_vwap():
    vwap, sigma, k = 425.5, 1.25, 2.0
    upper, lower = vwap_bands(vwap, sigma, k)
    assert (upper + lower) / 2 == pytest.approx(vwap)
    assert upper - vwap == pytest.approx(vwap - lower)
    assert upper - lower == pytest.approx(2 * k * sigma)


def test_vwap_bands_zero_sigma_collapses():
    upper, lower = vwap_bands(100.0, 0.0, k=2.0)
    assert upper == pytest.approx(100.0)
    assert lower == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# opening_range
# --------------------------------------------------------------------------- #


def test_opening_range_high_low_volume(spy_bars, day):
    open_utc, _ = session_bounds_utc(day)
    rng = opening_range(spy_bars.frame, open_utc, minutes=15)
    assert rng is not None
    assert isinstance(rng, OpeningRange)
    assert rng.high >= rng.low
    assert rng.volume > 0.0  # SPY has volume

    # The reported high/low must match the first-15-minutes window exactly.
    end = open_utc + pd.Timedelta(minutes=15)
    window = spy_bars.frame.loc[spy_bars.frame.index <= end]
    assert rng.high == pytest.approx(float(window["high"].max()))
    assert rng.low == pytest.approx(float(window["low"].min()))
    assert rng.volume == pytest.approx(float(window["volume"].sum()))


def test_opening_range_window_end_within_bounds(spy_bars, day):
    open_utc, _ = session_bounds_utc(day)
    minutes = 15
    rng = opening_range(spy_bars.frame, open_utc, minutes=minutes)
    assert rng is not None
    # window_end is the close of the last bar within the window.
    assert rng.window_end <= open_utc + pd.Timedelta(minutes=minutes)
    # First 15 one-minute bars close at 13:31..13:45 UTC -> window_end == 13:45.
    assert rng.window_end == open_utc + pd.Timedelta(minutes=minutes)


def test_opening_range_subset_of_session(spy_bars, day):
    """The ORB high/low must sit inside the full-session envelope."""
    open_utc, _ = session_bounds_utc(day)
    rng = opening_range(spy_bars.frame, open_utc, minutes=15)
    assert rng is not None
    assert rng.high <= float(spy_bars.frame["high"].max()) + 1e-9
    assert rng.low >= float(spy_bars.frame["low"].min()) - 1e-9


def test_opening_range_empty_returns_none(day):
    open_utc, _ = session_bounds_utc(day)
    # Bars that all close before the session opens -> nothing in window? Actually
    # bars before open are <= end so they'd be included; use a frame whose only
    # bars are AFTER the window end so the window is empty.
    idx = pd.date_range(open_utc + pd.Timedelta(minutes=30), periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "high": np.arange(5.0) + 1,
            "low": np.arange(5.0),
            "close": np.arange(5.0) + 0.5,
            "volume": np.full(5, 100.0),
        },
        index=idx,
    )
    rng = opening_range(df, open_utc, minutes=15)
    assert rng is None


def test_opening_range_is_known_at_before_and_after(spy_bars, day):
    open_utc, _ = session_bounds_utc(day)
    rng = opening_range(spy_bars.frame, open_utc, minutes=15)
    assert rng is not None
    we = rng.window_end

    # Before the window closes -> not known.
    assert rng.is_known_at(we - pd.Timedelta(seconds=1)) is False
    # Exactly at window_end (zero latency) -> known.
    assert rng.is_known_at(we) is True
    # After -> known.
    assert rng.is_known_at(we + pd.Timedelta(minutes=1)) is True


def test_opening_range_is_known_at_respects_latency(spy_bars, day):
    open_utc, _ = session_bounds_utc(day)
    rng = opening_range(spy_bars.frame, open_utc, minutes=15)
    assert rng is not None
    we = rng.window_end
    latency = pd.Timedelta(milliseconds=250)

    # At window_end but data hasn't arrived yet (latency) -> not known.
    assert rng.is_known_at(we, latency=latency) is False
    # Just before latency elapses -> still not known.
    assert rng.is_known_at(we + pd.Timedelta(milliseconds=100), latency=latency) is False
    # After latency elapses -> known.
    assert rng.is_known_at(we + latency, latency=latency) is True


def test_opening_range_minutes_parameter_widens_window(spy_bars, day):
    """A longer opening window can only widen (never shrink) the high/low range
    and must include more volume."""
    open_utc, _ = session_bounds_utc(day)
    short = opening_range(spy_bars.frame, open_utc, minutes=5)
    long = opening_range(spy_bars.frame, open_utc, minutes=30)
    assert short is not None and long is not None
    assert long.high >= short.high
    assert long.low <= short.low
    assert long.volume >= short.volume
    assert long.window_end > short.window_end
