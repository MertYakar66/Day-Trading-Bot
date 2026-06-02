"""Intraday realized volatility (DESIGN.md §4), reusing SWE estimators.

SWE's ``engine.realized_vol`` estimators hard-code a 252-trading-day
annualization (they assume *daily* bars). For *intraday* bars we must rescale:
SWE returns ``sigma_per_bar * sqrt(252)``, and the correctly annualized intraday
vol is ``sigma_per_bar * sqrt(periods_per_year)`` where
``periods_per_year = bars_per_day * 252``. The rescale factor therefore collapses
to ``sqrt(bars_per_day)`` (see ``docs/SWE_API_REFERENCE.md`` → realized_vol).

We default to Garman-Klass (uses full OHLC, drift-robust, no overnight term —
Yang-Zhang's overnight component is meaningless intrabar).
"""

from __future__ import annotations

import pandas as pd

from ..config import SessionConfig
from ..timeutils import parse_interval, session_bounds_utc

_ESTIMATORS = ("close_to_close", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang")


def bars_per_day(interval: str, session: SessionConfig | None = None) -> float:
    """Number of ``interval`` bars in one RTH session."""
    session = session or SessionConfig()
    # A representative weekday; RTH length is date-independent.
    open_utc, close_utc = session_bounds_utc(pd.Timestamp("2026-01-05").date(), session)
    rth_seconds = (close_utc - open_utc).total_seconds()
    return rth_seconds / parse_interval(interval).total_seconds()


def annualization_factor(interval: str, session: SessionConfig | None = None) -> float:
    """Multiply a (252-day) SWE RV by this to annualize an intraday RV."""
    import math

    return math.sqrt(bars_per_day(interval, session))


def intraday_rv(
    bars: pd.DataFrame,
    interval: str,
    *,
    window: int = 30,
    estimator: str = "garman_klass",
    session: SessionConfig | None = None,
) -> float | None:
    """Annualized intraday realized vol (decimal) over the last ``window`` bars.

    Returns ``None`` if there are too few bars or the estimate is non-finite.
    ``bars`` must hold lowercase ``open/high/low/close`` columns.
    """
    if estimator not in _ESTIMATORS:
        raise ValueError(f"unknown estimator {estimator!r}; choose from {_ESTIMATORS}")
    if bars is None or len(bars) < window + 1:
        return None

    # Lazy import so the rest of the feature layer is usable without the SWE dep.
    from engine import realized_vol as swe_rv

    fn = getattr(swe_rv, f"{estimator}_vol")
    daily_rv = float(fn(bars, window=window))
    if not pd.notna(daily_rv):
        return None
    return daily_rv * annualization_factor(interval, session)
