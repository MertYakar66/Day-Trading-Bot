"""Volatility risk premium (VRP) = ATM IV − CALENDAR-clock realized vol (DESIGN §4).

Both terms are annualized decimal vols on the SAME clock, so VRP is directly
interpretable: a positive VRP means options are pricing more vol than the
underlying realizes including overnight (a premium-selling lean), conditional
on regime (S2, Phase 1). ATM IV is read from the nearest-strike rows of the
latest PIT-available chain snapshot.

**The clock split (load-bearing — measured on real data 2026-06-10):** ATM IV
diffuses on the calendar clock, while the intraday Garman-Klass RV excludes
overnight variance by construction; comparing them carried a ~ +18.7 vol-pt
bias that made the pilot's "+0.19 VRP" ~98% artifact. The gate quantity is
therefore ``vrp = atm_iv - rv_calendar`` (close-to-close daily). The intraday
``rv`` is still carried on the result — it is the CORRECT clock for projecting
the remaining move-to-close of a 0DTE position (S2's ``sigma_real``), where no
overnight variance can realize before expiry. Neither leg may substitute for
the other.
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
    rv: float | None                      # intraday clock (sigma_real leg)
    vrp: float | None                     # atm_iv - rv_calendar (gate leg)
    rv_calendar: float | None = None      # calendar clock (IV's comparator)


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
        # Normalize so a pd.Timestamp cannot silently filter out every row.
        expiry = pd.Timestamp(expiry).date()
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
    rv_calendar: float | None = None,
) -> VRP:
    """VRP at ``as_of`` from PIT-available chain IV, the intraday RV over
    ``bars`` (which the caller must already have PIT-sliced to
    ``available_at(as_of)``), and the caller-supplied calendar-clock RV.

    ``rv_calendar`` is a per-day constant (close-to-close over prior sessions —
    see :func:`..features.realized_vol.calendar_rv`); the caller computes it
    once per (symbol, day). Without it the gate VRP is unknowable (``None``,
    stand aside) — the intraday RV is NEVER substituted as the IV comparator
    (that substitution was the measured ~ +18.7 vol-pt clock artifact).
    ``expiry`` restricts the IV read to one expiry (see :func:`atm_iv_at`).
    """
    iv = atm_iv_at(chain, as_of, expiry=expiry)
    rv = intraday_rv(bars, interval, window=window, estimator=estimator, session=session)
    vrp = (iv - rv_calendar) if (iv is not None and rv_calendar is not None) else None
    return VRP(atm_iv=iv, rv=rv, vrp=vrp, rv_calendar=rv_calendar)
