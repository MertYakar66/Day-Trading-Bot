"""Session / timezone helpers (RTH calendar, bar indices, interval parsing).

All engine timestamps are tz-aware UTC internally; exchange hours are defined in
US Eastern (``America/New_York``). Bars are indexed by their **close** time: a
1-minute bar covering ``[09:30, 09:31)`` is stamped ``09:31`` ET.

The trading calendar here skips weekends and a fixed set of US market holidays
(2024-2027). It is sufficient for the synthetic Phase-0 replay; the real-data
path would use the venue's official calendar.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd

from .config import SessionConfig

ET = "America/New_York"
UTC = "UTC"

# Fixed US equity market holidays (NYSE), 2024-2027. Observed dates.
_US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2024
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
        date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
        date(2024, 11, 28), date(2024, 12, 25),
        # 2025
        date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
        date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
        date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)

_INTERVAL_SECONDS: dict[str, int] = {
    "1s": 1, "5s": 5, "10s": 10, "30s": 30,
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
}


def parse_interval(interval: str) -> pd.Timedelta:
    """Return a :class:`pandas.Timedelta` for a supported interval string."""
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval: {interval!r}")
    return pd.Timedelta(seconds=_INTERVAL_SECONDS[interval])


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _US_MARKET_HOLIDAYS


def trading_days(start: date, end: date) -> list[date]:
    """Inclusive list of RTH trading days in ``[start, end]``."""
    out: list[date] = []
    cur = start
    one = pd.Timedelta(days=1)
    cur_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    while cur_ts <= end_ts:
        d = cur_ts.date()
        if is_trading_day(d):
            out.append(d)
        cur_ts = cur_ts + one
    return out


def session_bounds_utc(
    day: date, session: SessionConfig | None = None
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return ``(open_utc, close_utc)`` tz-aware UTC timestamps for ``day``'s RTH."""
    session = session or SessionConfig()
    open_et = pd.Timestamp(datetime.combine(day, session.rth_open), tz=ET)
    close_et = pd.Timestamp(datetime.combine(day, session.rth_close), tz=ET)
    return open_et.tz_convert(UTC), close_et.tz_convert(UTC)


def bar_close_index(
    day: date, interval: str, session: SessionConfig | None = None
) -> pd.DatetimeIndex:
    """DatetimeIndex of bar **close** timestamps (UTC) across one RTH session.

    The first close is one interval after the open; the last close is the RTH
    close. E.g. 1m → 09:31..16:00 ET (390 bars).
    """
    open_utc, close_utc = session_bounds_utc(day, session)
    step = parse_interval(interval)
    idx = pd.date_range(start=open_utc + step, end=close_utc, freq=step)
    idx.name = "ts"
    return idx


def flatten_time_utc(day: date, session: SessionConfig | None = None) -> pd.Timestamp:
    """The hard intraday time-stop: ``flatten_before_close_min`` before the close."""
    session = session or SessionConfig()
    _, close_utc = session_bounds_utc(day, session)
    return close_utc - pd.Timedelta(minutes=session.flatten_before_close_min)


def stable_seed(base_seed: int, symbol: str, day: date) -> int:
    """Deterministic 32-bit seed for ``(base_seed, symbol, day)`` — independent of
    ``PYTHONHASHSEED`` so synthetic data is reproducible across processes."""
    sym = sum((i + 1) * ord(c) for i, c in enumerate(symbol.upper()))
    return (base_seed * 1_000_003 + sym * 7_919 + day.toordinal() * 31) % (2**32)
