"""Volatility risk premium (VRP) = ATM IV − intraday realized vol (DESIGN.md §4).

Both terms are annualized decimal vols, so VRP is directly interpretable: a
positive VRP means options are pricing more vol than the underlying is realizing
(a premium-selling lean), conditional on regime (S2, Phase 1). ATM IV is read
from the nearest-strike rows of the latest PIT-available chain snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..config import SessionConfig
from ..contracts import OptionChainSeries
from .realized_vol import intraday_rv


@dataclass(frozen=True)
class VRP:
    atm_iv: float | None
    rv: float | None
    vrp: float | None


def atm_iv_at(
    chain: OptionChainSeries, as_of: pd.Timestamp, *, expiry: date | None = None
) -> float | None:
    """Average call/put implied vol at the strike nearest spot, from the latest
    snapshot available at ``as_of``. ``None`` if no snapshot has arrived.

    ``expiry`` restricts the read to that expiry's rows — a real capture can hold
    several expiries in one snapshot, and mixing their IVs would blend tenors. If
    the snapshot has no rows for the requested expiry, the IV is unknowable
    (``None``), not approximated from another tenor.
    """
    snap = chain.latest_available(as_of)
    if snap.empty:
        return None
    if expiry is not None:
        snap = snap.loc[pd.to_datetime(snap["expiration"]).dt.date == expiry]
        if snap.empty:
            return None
    spot = float(snap["spot"].iloc[0])
    nearest = (snap["strike"] - spot).abs().min()
    atm = snap.loc[(snap["strike"] - spot).abs() <= nearest + 1e-9]
    iv = atm["implied_vol"].astype(float)
    iv = iv[iv > 0]
    if iv.empty:
        return None
    return float(iv.mean())


def vrp_at(
    chain: OptionChainSeries,
    bars: pd.DataFrame,
    as_of: pd.Timestamp,
    interval: str,
    *,
    window: int = 30,
    estimator: str = "garman_klass",
    session: SessionConfig | None = None,
    expiry: date | None = None,
) -> VRP:
    """VRP at ``as_of`` from PIT-available chain IV and intraday RV over ``bars``
    (which the caller must already have PIT-sliced to ``available_at(as_of)``).
    ``expiry`` restricts the IV read to one expiry (see :func:`atm_iv_at`)."""
    iv = atm_iv_at(chain, as_of, expiry=expiry)
    rv = intraday_rv(bars, interval, window=window, estimator=estimator, session=session)
    vrp = (iv - rv) if (iv is not None and rv is not None) else None
    return VRP(atm_iv=iv, rv=rv, vrp=vrp)
