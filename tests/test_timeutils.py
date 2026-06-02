"""Unit tests for intraday.timeutils — the RTH calendar, session bounds, bar
close index, flatten time and the deterministic seed helper.

These are pure functions with no SWE / network dependency, so no @swe marker.
Times are checked in UTC; the reference day (May 2026) is in EDT, so
09:30 ET == 13:30 UTC and 16:00 ET == 20:00 UTC.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.config import SessionConfig
from intraday.timeutils import (
    bar_close_index,
    flatten_time_utc,
    is_trading_day,
    parse_interval,
    session_bounds_utc,
    stable_seed,
    trading_days,
)


# --------------------------------------------------------------------------
# parse_interval
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("interval", "seconds"),
    [
        ("1s", 1),
        ("5s", 5),
        ("10s", 10),
        ("30s", 30),
        ("1m", 60),
        ("5m", 300),
        ("15m", 900),
        ("30m", 1800),
        ("1h", 3600),
    ],
)
def test_parse_interval_valid(interval, seconds):
    td = parse_interval(interval)
    assert isinstance(td, pd.Timedelta)
    assert td == pd.Timedelta(seconds=seconds)
    assert td.total_seconds() == pytest.approx(float(seconds))


@pytest.mark.parametrize("bad", ["", "2m", "1minute", "60", "1M", "0s", "1d", "abc"])
def test_parse_interval_raises_on_unsupported(bad):
    with pytest.raises(ValueError):
        parse_interval(bad)


# --------------------------------------------------------------------------
# is_trading_day
# --------------------------------------------------------------------------
def test_is_trading_day_weekday():
    # Monday 2026-05-18 is a normal trading day.
    assert is_trading_day(date(2026, 5, 18)) is True
    # Friday 2026-05-22 too.
    assert is_trading_day(date(2026, 5, 22)) is True


def test_is_trading_day_weekend():
    # Saturday / Sunday 2026-05-16 / 2026-05-17.
    assert is_trading_day(date(2026, 5, 16)) is False
    assert is_trading_day(date(2026, 5, 17)) is False


def test_is_trading_day_holiday_memorial():
    # Memorial Day 2026-05-25 (a Monday) is a market holiday.
    assert date(2026, 5, 25).weekday() == 0
    assert is_trading_day(date(2026, 5, 25)) is False


def test_is_trading_day_holiday_new_year():
    # New Year's Day 2026-01-01 (a Thursday, weekday) is a market holiday.
    assert date(2026, 1, 1).weekday() < 5
    assert is_trading_day(date(2026, 1, 1)) is False


# --------------------------------------------------------------------------
# trading_days
# --------------------------------------------------------------------------
def test_trading_days_plain_week_count_and_order():
    # Mon 2026-05-18 .. Fri 2026-05-22: five business days, no holidays.
    days = trading_days(date(2026, 5, 18), date(2026, 5, 22))
    assert days == [
        date(2026, 5, 18),
        date(2026, 5, 19),
        date(2026, 5, 20),
        date(2026, 5, 21),
        date(2026, 5, 22),
    ]


def test_trading_days_excludes_weekends_and_memorial_day():
    # Span Mon 2026-05-18 .. Fri 2026-05-29 brackets a weekend and Memorial Day.
    days = trading_days(date(2026, 5, 18), date(2026, 5, 29))
    # Weekend 23/24 excluded, holiday 25 excluded.
    assert date(2026, 5, 23) not in days  # Saturday
    assert date(2026, 5, 24) not in days  # Sunday
    assert date(2026, 5, 25) not in days  # Memorial Day
    # 10 weekdays (18-22, 25-29) minus Memorial Day (25) = 9 trading days.
    assert len(days) == 9
    assert all(is_trading_day(d) for d in days)
    # Strictly increasing.
    assert days == sorted(days)


def test_trading_days_inclusive_bounds():
    # Both endpoints are trading days and must be included.
    days = trading_days(date(2026, 5, 18), date(2026, 5, 18))
    assert days == [date(2026, 5, 18)]


def test_trading_days_empty_when_start_after_end():
    assert trading_days(date(2026, 5, 22), date(2026, 5, 18)) == []


def test_trading_days_single_weekend_day_is_empty():
    # Saturday-only range yields nothing.
    assert trading_days(date(2026, 5, 16), date(2026, 5, 16)) == []


# --------------------------------------------------------------------------
# session_bounds_utc
# --------------------------------------------------------------------------
def test_session_bounds_utc_edt_day():
    day = date(2026, 5, 18)
    open_utc, close_utc = session_bounds_utc(day)
    assert isinstance(open_utc, pd.Timestamp)
    assert isinstance(close_utc, pd.Timestamp)
    # tz-aware UTC.
    assert str(open_utc.tz) == "UTC"
    assert str(close_utc.tz) == "UTC"
    # 09:30 ET (EDT) == 13:30 UTC; 16:00 ET == 20:00 UTC.
    assert open_utc == pd.Timestamp("2026-05-18 13:30:00", tz="UTC")
    assert close_utc == pd.Timestamp("2026-05-18 20:00:00", tz="UTC")
    # RTH spans 6.5 hours.
    assert (close_utc - open_utc) == pd.Timedelta(hours=6, minutes=30)


def test_session_bounds_utc_respects_custom_session():
    day = date(2026, 5, 18)
    from datetime import time

    custom = SessionConfig(rth_open=time(10, 0), rth_close=time(15, 0))
    open_utc, close_utc = session_bounds_utc(day, custom)
    # 10:00 ET == 14:00 UTC; 15:00 ET == 19:00 UTC (EDT).
    assert open_utc == pd.Timestamp("2026-05-18 14:00:00", tz="UTC")
    assert close_utc == pd.Timestamp("2026-05-18 19:00:00", tz="UTC")


# --------------------------------------------------------------------------
# bar_close_index
# --------------------------------------------------------------------------
def test_bar_close_index_1m_shape_and_endpoints():
    day = date(2026, 5, 18)
    idx = bar_close_index(day, "1m")
    assert isinstance(idx, pd.DatetimeIndex)
    assert len(idx) == 390
    assert idx.name == "ts"
    assert str(idx.tz) == "UTC"
    # First close is one interval after open (13:30 + 1m = 13:31); last == close.
    assert idx[0] == pd.Timestamp("2026-05-18 13:31:00", tz="UTC")
    assert idx[-1] == pd.Timestamp("2026-05-18 20:00:00", tz="UTC")
    # Uniform 1-minute spacing throughout.
    diffs = idx.to_series().diff().dropna()
    assert (diffs == pd.Timedelta(minutes=1)).all()


def test_bar_close_index_5m_count():
    # 6.5h / 5m == 78 bars.
    idx = bar_close_index(date(2026, 5, 18), "5m")
    assert len(idx) == 78
    assert idx[0] == pd.Timestamp("2026-05-18 13:35:00", tz="UTC")
    assert idx[-1] == pd.Timestamp("2026-05-18 20:00:00", tz="UTC")


def test_bar_close_index_last_equals_session_close():
    day = date(2026, 5, 18)
    _, close_utc = session_bounds_utc(day)
    assert bar_close_index(day, "1m")[-1] == close_utc


# --------------------------------------------------------------------------
# flatten_time_utc
# --------------------------------------------------------------------------
def test_flatten_time_utc_is_close_minus_five():
    day = date(2026, 5, 18)
    _, close_utc = session_bounds_utc(day)
    flat = flatten_time_utc(day)
    assert str(flat.tz) == "UTC"
    # Default flatten_before_close_min == 5.
    assert SessionConfig().flatten_before_close_min == 5
    assert flat == close_utc - pd.Timedelta(minutes=5)
    assert flat == pd.Timestamp("2026-05-18 19:55:00", tz="UTC")


def test_flatten_time_utc_respects_custom_minutes():
    day = date(2026, 5, 18)
    _, close_utc = session_bounds_utc(day)
    custom = SessionConfig(flatten_before_close_min=15)
    flat = flatten_time_utc(day, custom)
    assert flat == close_utc - pd.Timedelta(minutes=15)


# --------------------------------------------------------------------------
# stable_seed
# --------------------------------------------------------------------------
def test_stable_seed_deterministic():
    a = stable_seed(42, "SPY", date(2026, 5, 18))
    b = stable_seed(42, "SPY", date(2026, 5, 18))
    assert a == b
    # 32-bit unsigned range.
    assert 0 <= a < 2**32
    assert isinstance(a, int)


def test_stable_seed_case_insensitive_symbol():
    # Documented to upper-case the symbol before hashing.
    assert stable_seed(7, "spy", date(2026, 5, 18)) == stable_seed(
        7, "SPY", date(2026, 5, 18)
    )


def test_stable_seed_varies_by_symbol():
    d = date(2026, 5, 18)
    assert stable_seed(1, "SPY", d) != stable_seed(1, "QQQ", d)


def test_stable_seed_varies_by_day():
    assert stable_seed(1, "SPY", date(2026, 5, 18)) != stable_seed(
        1, "SPY", date(2026, 5, 19)
    )


def test_stable_seed_varies_by_base_seed():
    d = date(2026, 5, 18)
    assert stable_seed(1, "SPY", d) != stable_seed(2, "SPY", d)
