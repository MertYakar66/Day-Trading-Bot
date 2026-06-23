"""Hermetic tests for intraday.data.index_chain_history (deep-history chain
reconstruction + BS IV inversion). Network-free; builds in-memory partitions from
Black-Scholes-consistent prices so the inversion is an exact round-trip and no real
SWE data is required.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from engine.option_pricer import black_scholes_price

from intraday.contracts import CHAIN_COLUMNS, DataSource
from intraday.data import index_chain_history as ich
from intraday.data.swe_offline import SweDataUnavailable

# A self-consistent world: spot 600, 30 DTE, flat IV, SPY rate/div.
_SPOT = 600.0
_DAY = date(2026, 3, 2)
_EXPIRY = date(2026, 4, 1)  # 30 calendar days
_R = 0.04
_Q = 0.013
_IV = 0.20
_T = (pd.Timestamp(_EXPIRY) - pd.Timestamp(_DAY)).days / 365.0


def _make_partition(iv: float = _IV, strikes=None, *, only_calls=False) -> pd.DataFrame:
    """A fake EOD partition whose mids are exact Black-Scholes prices at ``iv``."""
    if strikes is None:
        strikes = np.arange(540.0, 661.0, 5.0)
    rows = []
    created = datetime(2026, 3, 2, 16, 0)
    rights = ["CALL"] if only_calls else ["CALL", "PUT"]
    for k in strikes:
        for right in rights:
            price = black_scholes_price(_SPOT, float(k), _T, _R, iv, right.lower(), q=_Q)
            if price <= 0.01:
                price = 0.01
            rows.append({
                "strike": float(k), "right": right,
                "bid": price * 0.99, "ask": price * 1.01,
                "open_interest": 1000.0, "created": created,
            })
    df = pd.DataFrame(rows)
    df["created_date"] = pd.to_datetime(df["created"]).dt.date
    return df


def test_roundtrip_recovers_iv_and_spot():
    part = _make_partition()
    out = ich.reconstruct_chain("SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part)
    assert out is not None
    chain, stats = out
    # Parity spot recovered to the penny.
    assert stats.spot == pytest.approx(_SPOT, abs=0.5)
    # Every inverted IV recovers the true flat 0.20.
    ivs = chain.frame["implied_vol"].to_numpy()
    assert np.allclose(ivs, _IV, atol=2e-3)
    # Shape + provenance.
    assert list(chain.frame.columns) == list(CHAIN_COLUMNS)
    assert chain.source is DataSource.THETA
    assert chain.symbol == "SPY"
    assert stats.rows_inverted >= 8


def test_smile_iv_recovered_per_strike():
    # A strike-dependent smile must come back per strike (not blended).
    strikes = np.arange(560.0, 641.0, 10.0)
    rows = []
    created = datetime(2026, 3, 2, 16, 0)
    for k in strikes:
        iv_k = 0.18 + 0.0008 * abs(k - _SPOT)  # mild smile
        for right in ("CALL", "PUT"):
            price = max(black_scholes_price(_SPOT, float(k), _T, _R, iv_k, right.lower(), q=_Q), 0.01)
            rows.append({"strike": float(k), "right": right, "bid": price * 0.99,
                         "ask": price * 1.01, "open_interest": 500.0, "created": created})
    part = pd.DataFrame(rows)
    part["created_date"] = pd.to_datetime(part["created"]).dt.date
    out = ich.reconstruct_chain("SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part, min_strikes=4)
    assert out is not None
    chain, _ = out
    f = chain.frame.copy()
    # ATM IV ~0.18, wing IV higher — confirm monotone-ish recovery.
    atm = f.loc[(f["strike"] - _SPOT).abs() < 1e-6, "implied_vol"]
    wing = f.loc[(f["strike"] - _SPOT).abs() >= 39.0, "implied_vol"]
    assert atm.mean() == pytest.approx(0.18, abs=5e-3)
    assert wing.mean() > atm.mean()


def test_spx_refused():
    with pytest.raises(SweDataUnavailable):
        ich.reconstruct_chain("SPX", _DAY, _EXPIRY, r=_R, _partition=_make_partition())
    with pytest.raises(SweDataUnavailable):
        ich.reconstruct_chain("SPXW", _DAY, _EXPIRY, r=_R, _partition=_make_partition())


def test_too_few_strikes_returns_none():
    part = _make_partition(strikes=np.array([600.0]))  # one strike → < min_strikes
    assert ich.reconstruct_chain("SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part) is None


def test_no_parity_pair_returns_none():
    # Only calls → no call/put pair → no parity spot → refuse.
    part = _make_partition(only_calls=True)
    assert ich.reconstruct_chain("SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part) is None


def test_missing_day_returns_none():
    part = _make_partition()
    assert ich.reconstruct_chain("SPY", date(2099, 1, 1), _EXPIRY, r=_R, q=_Q, _partition=part) is None


def test_strike_window_limits_inversion():
    part = _make_partition()
    wide = ich.reconstruct_chain("SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part)
    narrow = ich.reconstruct_chain(
        "SPY", _DAY, _EXPIRY, r=_R, q=_Q, _partition=part, strike_window_pct=0.03
    )
    assert wide is not None and narrow is not None
    # ±3% of 600 = [582, 618] → far fewer strikes than the full 540..660 grid.
    assert narrow[1].rows_inverted < wide[1].rows_inverted
    assert (narrow[0].frame["strike"].between(582, 618)).all()


def test_list_history_expiries_missing_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SWE_ROOT", str(tmp_path))
    with pytest.raises(SweDataUnavailable):
        ich.list_history_expiries("SPY")
