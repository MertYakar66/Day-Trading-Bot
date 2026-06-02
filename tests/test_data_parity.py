"""Tests for put-call-parity underlying reconstruction (deep-history path).

Theta options are not pulled this session, so the parity path is proven BY TESTS:
we build parity-consistent call/put quotes from a KNOWN spot path and assert the
reconstruction recovers it to floating-point tolerance. Parity identity used to
construct inputs: ``C − P = S·e^{−qT} − K·e^{−rT}``.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.contracts import BAR_COLUMNS, DataSource
from intraday.data.parity import (
    ParityUnderlyingProvider,
    forward_from_parity,
    implied_spot,
    reconstruct_spot,
    spot_from_forward,
    spot_to_bars,
    year_fraction,
)
from intraday.data.provider import DataUnavailable
from intraday.timeutils import bar_close_index, session_bounds_utc

DAY = date(2026, 6, 1)
R = 0.04
Q = 0.015


def _cp_from_spot(spot, strike, *, r, q, T):
    """Parity-consistent (call_mid, put_mid) for a known spot (put leg = 5.0)."""
    cp_diff = spot * np.exp(-q * T) - strike * np.exp(-r * T)
    put = 5.0
    call = put + cp_diff
    return call, put


# --------------------------------------------------------------------------- #
# Parity math identities
# --------------------------------------------------------------------------- #
def test_scalar_recovery():
    spot, strike, T = 6000.0, 6000.0, 4.0 / (365.25 * 24)  # ~4h to expiry
    call, put = _cp_from_spot(spot, strike, r=R, q=Q, T=T)
    assert forward_from_parity(call, put, strike, r=R, T=T) == pytest.approx(
        spot * np.exp((R - Q) * T)
    )
    assert implied_spot(call, put, strike, r=R, q=Q, T=T) == pytest.approx(spot, rel=1e-9)


def test_scalar_recovery_at_long_maturity_discriminates_r_sign():
    """At a multi-month maturity e^{rT} departs from 1, so this round-trip would
    FAIL if the r sign in forward_from_parity were flipped (the ~4h case can't
    detect it). Locks the discount-sign correctness."""
    spot, strike, T = 6000.0, 5900.0, 0.25
    call, put = _cp_from_spot(spot, strike, r=R, q=Q, T=T)
    assert implied_spot(call, put, strike, r=R, q=Q, T=T) == pytest.approx(spot, rel=1e-9)
    # A flipped r sign mis-recovers by ~$100 — far above any tolerance.
    bad = implied_spot(call, put, strike, r=-R, q=Q, T=T)
    assert abs(bad - spot) > 1.0


def test_spot_from_forward_inverts():
    F = 6010.0
    T = 0.25
    s = spot_from_forward(F, r=R, q=Q, T=T)
    assert s * np.exp((R - Q) * T) == pytest.approx(F)


def test_year_fraction_zero_at_expiry_close():
    _, close_utc = session_bounds_utc(DAY)
    assert year_fraction(close_utc, DAY) == pytest.approx(0.0)
    assert year_fraction(close_utc - pd.Timedelta(hours=2), DAY) > 0.0


# --------------------------------------------------------------------------- #
# Series reconstruction over a path
# --------------------------------------------------------------------------- #
def _quotes_for_path(spot_path: pd.Series, strike: float) -> pd.DataFrame:
    rows = []
    for ts, spot in spot_path.items():
        T = year_fraction(ts, DAY)
        call, put = _cp_from_spot(spot, strike, r=R, q=Q, T=T)
        rows.append({"strike": strike, "call_mid": call, "put_mid": put, "expiry": DAY})
    return pd.DataFrame(rows, index=spot_path.index)


def test_reconstruct_spot_recovers_known_path():
    open_utc, _ = session_bounds_utc(DAY)
    idx = pd.date_range(open_utc + pd.Timedelta(minutes=5), periods=50, freq="5min")
    true_spot = pd.Series(6000.0 + 5.0 * np.sin(np.linspace(0, 6, 50)), index=idx)
    quotes = _quotes_for_path(true_spot, strike=6000.0)
    recovered = reconstruct_spot(quotes, r=R, q=Q)
    np.testing.assert_allclose(recovered.to_numpy(), true_spot.to_numpy(), rtol=1e-7)


def test_reconstruct_spot_missing_columns_raises():
    with pytest.raises(DataUnavailable):
        reconstruct_spot(pd.DataFrame({"strike": [1.0]}), r=R)


def test_spot_to_bars_canonical_grid():
    open_utc, _ = session_bounds_utc(DAY)
    idx = pd.date_range(open_utc + pd.Timedelta(minutes=1), periods=300, freq="1min")
    spot = pd.Series(np.linspace(6000.0, 6010.0, 300), index=idx)
    bars = spot_to_bars(spot, "SPX", DAY, "5m")
    pd.testing.assert_index_equal(bars.frame.index, bar_close_index(DAY, "5m"))
    assert list(bars.frame.columns) == list(BAR_COLUMNS)
    assert bars.source is DataSource.PARITY
    assert (bars.frame["volume"] == 0.0).all()  # parity gives price, not volume
    assert bars.frame["close"].iloc[-1] == pytest.approx(6010.0, abs=0.5)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
def test_parity_provider_get_bars():
    open_utc, _ = session_bounds_utc(DAY)
    idx = pd.date_range(open_utc + pd.Timedelta(minutes=5), periods=60, freq="5min")
    true_spot = pd.Series(6000.0 + np.linspace(0, 8, 60), index=idx)
    quotes = _quotes_for_path(true_spot, strike=6000.0)

    prov = ParityUnderlyingProvider(lambda sym, day: quotes, config=EngineConfig.default(), q=Q)
    bars = prov.get_bars("SPX", DAY, "5m")
    assert bars.source is DataSource.PARITY
    assert len(bars.frame) == 78
    # Reconstructed close near the true terminal spot.
    assert bars.frame["close"].iloc[-1] == pytest.approx(6008.0, abs=1.0)


def test_parity_provider_empty_quotes_raises():
    prov = ParityUnderlyingProvider(lambda sym, day: pd.DataFrame())
    with pytest.raises(DataUnavailable):
        prov.get_bars("SPX", DAY, "5m")


def test_parity_provider_options_methods_raise():
    prov = ParityUnderlyingProvider(lambda sym, day: pd.DataFrame())
    with pytest.raises(DataUnavailable):
        prov.get_option_chain("SPX", DAY)
    with pytest.raises(DataUnavailable):
        prov.get_option_tape("SPX", DAY)
