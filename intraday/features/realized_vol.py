"""Realized volatility (DESIGN.md §4): intraday (RTH-clock) and calendar-clock.

**Two clocks, two jobs — never interchangeable:**

- :func:`intraday_rv` (Garman-Klass on RTH bars) excludes overnight variance by
  construction. That is the CORRECT clock for projecting the remaining
  move-to-close of a 0DTE position (no overnight variance can realize before
  the close), i.e. S2's ``sigma_real`` / win-probability math.
- :func:`calendar_rv` (close-to-close daily returns) includes overnight
  variance — the SAME clock ATM implied vol diffuses on. That is the correct
  comparator for IV: ``vrp = atm_iv - calendar_rv``. Comparing IV against the
  intraday clock instead carries a large positive bias (~ +18.7 vol pts
  measured on real data, 12/12 tickers) — the artifact that made the pilot's
  "+VRP" ~98% clock mismatch.

Intraday estimators reuse SWE's ``engine.realized_vol``, which hard-codes a
252-trading-day annualization (it assumes *daily* bars). For *intraday* bars we
rescale: SWE returns ``sigma_per_bar * sqrt(252)``; correctly annualized
intraday vol is ``sigma_per_bar * sqrt(bars_per_day * 252)``, so the factor
collapses to ``sqrt(bars_per_day)`` (``docs/SWE_API_REFERENCE.md``). For
calendar (daily close-to-close) returns, ``sqrt(252)`` is correct as-is.

We default to Garman-Klass intraday (full OHLC, drift-robust, no overnight term
— Yang-Zhang's overnight component is meaningless intrabar).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
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
    return math.sqrt(bars_per_day(interval, session))


def calendar_rv(closes: pd.Series | None, *, window: int = 21) -> float | None:
    """Annualized close-to-close (calendar-clock) realized vol over the last
    ``window`` daily returns (needs ``window + 1`` closes).

    Includes overnight variance — the clock ATM implied vol diffuses on, hence
    the only honest RV leg of ``vrp = atm_iv - rv_calendar``. Returns ``None``
    when there are too few closes or any close is non-positive/NaN: a history
    shallower than the window means the VRP is unknowable (stand aside), never
    approximated from a shorter sample.
    """
    if closes is None or len(closes) < window + 1:
        return None
    tail = pd.Series(closes).astype(float).tail(window + 1)
    if bool(tail.isna().any()) or bool((tail <= 0).any()):
        return None
    rets = np.log(tail / tail.shift(1)).dropna()
    if len(rets) < window:
        return None
    sd = float(rets.std(ddof=1))
    if not np.isfinite(sd):
        return None
    return sd * math.sqrt(252.0)


def trailing_session_closes(
    provider,
    symbol: str,
    day: date,
    *,
    n_sessions: int,
    interval: str = "1m",
) -> pd.Series | None:
    """The last ``n_sessions`` session CLOSES strictly before ``day``.

    PIT-safe by construction: a prior session's close is knowable before
    ``day``'s open, and ``day`` itself never contributes. Returns ``None`` when
    the provider cannot serve that many prior sessions (e.g. the first month of
    a store-backed window) — the consumer stands aside rather than estimate
    calendar vol from a stub history.
    """
    from ..data.provider import DataUnavailable, TierUnavailable

    # Generous calendar lookback so n_sessions trading days fit (weekends,
    # holidays); the provider's own calendar decides what actually exists.
    start = day - timedelta(days=int(n_sessions * 2.5) + 14)
    prior = [d for d in provider.trading_days(start, day - timedelta(days=1)) if d < day]
    if len(prior) < n_sessions:
        return None
    closes: list[float] = []
    idx: list[date] = []
    for d in prior[-n_sessions:]:
        try:
            frame = provider.get_bars(symbol, d, interval).frame
        except (DataUnavailable, TierUnavailable, FileNotFoundError):
            return None
        if frame.empty:
            return None
        closes.append(float(frame["close"].iloc[-1]))
        idx.append(d)
    return pd.Series(closes, index=pd.Index(idx, name="session"))


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
