"""Tests for intraday.features.vrp — ATM IV extraction and volatility risk premium.

VRP = ATM IV (decimal, annualized) − intraday realized vol (decimal, annualized).
Both terms come from PIT-available data; these tests pin the band of ATM IV, the
PIT gating (None before any snapshot is available), and the VRP arithmetic
identity vrp == atm_iv - rv when both are finite.

Marked ``swe`` because vrp_at -> intraday_rv pulls in engine.realized_vol.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from intraday.features.realized_vol import intraday_rv
from intraday.features.vrp import VRP, atm_iv_at, vrp_at
from intraday.timeutils import session_bounds_utc

pytestmark = pytest.mark.swe


def _mid_session(day) -> pd.Timestamp:
    """An ``as_of`` ~1h into the session: a chain snapshot has arrived and >30
    one-minute bars are PIT-available (so RV is computable)."""
    open_utc, _ = session_bounds_utc(day)
    return open_utc + pd.Timedelta(minutes=60)


# --------------------------------------------------------------------------- #
# atm_iv_at
# --------------------------------------------------------------------------- #
def test_atm_iv_in_sane_band_after_snapshot(spy_chain, day):
    as_of = _mid_session(day)
    iv = atm_iv_at(spy_chain, as_of)
    assert iv is not None
    assert isinstance(iv, float)
    # ATM IV is anchored to the realized-vol scale with a vol-risk-premium that
    # swings with the gamma skew (see synthetic.py), so it sits in a sane band
    # around realized vol rather than a fixed anchor.
    assert 0.05 <= iv <= 0.6


def test_atm_iv_none_before_first_snapshot_available(spy_chain, day):
    # The first snapshot is stamped at the session open and only becomes usable
    # one chain-latency later, so exactly at the open nothing is available yet.
    open_utc, _ = session_bounds_utc(day)
    iv = atm_iv_at(spy_chain, open_utc)
    assert iv is None


def test_atm_iv_none_well_before_session(spy_chain, day):
    open_utc, _ = session_bounds_utc(day)
    iv = atm_iv_at(spy_chain, open_utc - pd.Timedelta(hours=1))
    assert iv is None


def test_atm_iv_uses_nearest_strike_only(spy_chain, day):
    """ATM IV must equal the mean IV of the rows at the strike nearest spot in
    the latest available snapshot — not the smile-inflated wing strikes."""
    as_of = _mid_session(day)
    snap = spy_chain.latest_available(as_of)
    assert not snap.empty
    spot = float(snap["spot"].iloc[0])
    dist = (snap["strike"] - spot).abs()
    nearest = dist.min()
    atm_rows = snap.loc[dist <= nearest + 1e-9]
    expected = float(atm_rows["implied_vol"][atm_rows["implied_vol"] > 0].mean())
    assert atm_iv_at(spy_chain, as_of) == pytest.approx(expected)
    # The wing strikes are richer (smile), so the ATM value is not the snapshot-
    # wide mean — confirms we are not averaging the whole chain.
    chain_wide_mean = float(snap["implied_vol"][snap["implied_vol"] > 0].mean())
    assert expected < chain_wide_mean


def test_atm_iv_monotone_available_as_session_progresses(spy_chain, day):
    """Once available, IV stays defined and in-band at later as_ofs too."""
    open_utc, _ = session_bounds_utc(day)
    for minutes in (30, 90, 180):
        iv = atm_iv_at(spy_chain, open_utc + pd.Timedelta(minutes=minutes))
        assert iv is not None
        assert 0.05 <= iv <= 0.6


# --------------------------------------------------------------------------- #
# vrp_at
# --------------------------------------------------------------------------- #
def test_vrp_identity_when_both_finite(spy_chain, spy_bars, day):
    as_of = _mid_session(day)
    bars_pit = spy_bars.available_at(as_of)
    result = vrp_at(spy_chain, bars_pit, as_of, "1m")

    assert isinstance(result, VRP)
    assert result.atm_iv is not None
    assert result.rv is not None
    assert result.vrp is not None
    # Core identity: vrp = atm_iv - rv, both annualized decimals.
    assert result.vrp == pytest.approx(result.atm_iv - result.rv)

    # Cross-check the components against their primitives directly.
    assert result.atm_iv == pytest.approx(atm_iv_at(spy_chain, as_of))
    assert result.rv == pytest.approx(intraday_rv(bars_pit, "1m"))

    # Both legs are finite positive annualized vols.
    assert result.rv > 0.0
    assert math.isfinite(result.rv)
    assert math.isfinite(result.vrp)


def test_vrp_none_iv_before_snapshot(spy_chain, spy_bars, day):
    """No chain snapshot available -> atm_iv None -> vrp None even if rv exists."""
    open_utc, _ = session_bounds_utc(day)
    # Pick an as_of with enough bars for RV but before the chain is available is
    # impossible (chain available 1s after open), so build a frame manually: use
    # the full day's bars but evaluate IV at the open.
    result = vrp_at(spy_chain, spy_bars.frame, open_utc, "1m")
    assert result.atm_iv is None
    assert result.vrp is None
    # RV may still compute over the supplied bars.
    assert result.rv == pytest.approx(intraday_rv(spy_bars.frame, "1m"))


def test_vrp_none_rv_with_insufficient_bars(spy_chain, spy_bars, day):
    """Too few bars (< window+1) -> rv None -> vrp None even though IV exists."""
    as_of = _mid_session(day)
    iv = atm_iv_at(spy_chain, as_of)
    assert iv is not None
    short_bars = spy_bars.frame.head(5)  # < default window (30) + 1
    result = vrp_at(spy_chain, short_bars, as_of, "1m", window=30)
    assert result.atm_iv == pytest.approx(iv)
    assert result.rv is None
    assert result.vrp is None


def test_vrp_window_kwarg_threads_to_rv(spy_chain, spy_bars, day):
    as_of = _mid_session(day)
    bars_pit = spy_bars.available_at(as_of)
    result = vrp_at(spy_chain, bars_pit, as_of, "1m", window=20)
    assert result.rv == pytest.approx(intraday_rv(bars_pit, "1m", window=20))
    assert result.vrp == pytest.approx(result.atm_iv - result.rv)
