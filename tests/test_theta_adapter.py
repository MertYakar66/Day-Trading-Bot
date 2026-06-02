"""Tests for the read-only Theta data adapter.

These tests verify the disconnected-by-policy behavior of ``ThetaDataProvider``:
it is fully importable and exercisable WITHOUT ever opening a socket. We never
assert a successful data fetch (no Theta this session).
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.contracts import DataSource
from intraday.data.provider import TierUnavailable
from intraday.data.theta_adapter import (
    ThetaDataProvider,
    ThetaNotConnectedThisSession,
)

SYMBOL = "SPY"
DAY = date(2026, 5, 18)


def test_source_is_theta():
    """The adapter declares the THETA data source (class- and instance-level)."""
    assert ThetaDataProvider.source is DataSource.THETA
    provider = ThetaDataProvider()
    assert provider.source is DataSource.THETA
    # THETA is the non-synthetic feed.
    assert provider.source.is_synthetic is False


def test_default_allow_connect_is_false():
    """By policy the adapter is constructed disconnected by default."""
    provider = ThetaDataProvider()
    assert provider.allow_connect is False
    # No connector is constructed eagerly.
    assert provider._connector is None


@pytest.mark.parametrize(
    "method, args",
    [
        ("get_bars", (SYMBOL, DAY, "1m")),
        ("get_option_chain", (SYMBOL, DAY)),
        ("get_option_tape", (SYMBOL, DAY)),
    ],
)
def test_methods_blocked_when_not_connected(method, args):
    """With allow_connect=False, data methods raise ThetaNotConnectedThisSession."""
    provider = ThetaDataProvider(allow_connect=False)
    with pytest.raises(ThetaNotConnectedThisSession):
        getattr(provider, method)(*args)


def test_not_connected_error_is_runtimeerror():
    """ThetaNotConnectedThisSession is a RuntimeError subclass (catchable as such)."""
    assert issubclass(ThetaNotConnectedThisSession, RuntimeError)
    provider = ThetaDataProvider()
    with pytest.raises(RuntimeError):
        provider.get_bars(SYMBOL, DAY, "1m")


@pytest.mark.parametrize(
    "method, args",
    [
        ("get_bars", (SYMBOL, DAY, "1m")),
        ("get_option_chain", (SYMBOL, DAY)),
        ("get_option_tape", (SYMBOL, DAY)),
    ],
)
def test_methods_raise_tier_unavailable_when_allowed(method, args):
    """With allow_connect=True the guard passes but FREE tier gates intraday data.

    This still never opens a socket: TierUnavailable is raised before any
    connector I/O for these intraday/option methods.
    """
    provider = ThetaDataProvider(allow_connect=True)
    with pytest.raises(TierUnavailable):
        getattr(provider, method)(*args)


def test_tier_unavailable_message_is_informative():
    """The TierUnavailable error documents which tier/method is required."""
    provider = ThetaDataProvider(allow_connect=True)
    with pytest.raises(TierUnavailable) as exc:
        provider.get_bars(SYMBOL, DAY, "1m")
    msg = str(exc.value)
    assert "STANDARD" in msg
    assert SYMBOL in msg


def test_trading_days_calendar_only_nonempty():
    """trading_days is feed-independent and works with no network/connection."""
    provider = ThetaDataProvider(allow_connect=False)
    days = provider.trading_days(date(2026, 5, 1), date(2026, 5, 8))
    assert isinstance(days, list)
    assert len(days) > 0
    # Every entry is a date and within the requested inclusive range.
    for d in days:
        assert isinstance(d, date)
        assert date(2026, 5, 1) <= d <= date(2026, 5, 8)
    # Weekdays only in that span: Fri 5/1, Mon 5/4 .. Fri 5/8 (no weekends).
    assert days == [
        date(2026, 5, 1),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 5, 6),
        date(2026, 5, 7),
        date(2026, 5, 8),
    ]


def test_trading_days_skips_weekend():
    """A pure-weekend range yields no trading days (calendar correctness)."""
    provider = ThetaDataProvider(allow_connect=True)
    # 2026-05-16 is a Saturday, 2026-05-17 a Sunday.
    assert provider.trading_days(date(2026, 5, 16), date(2026, 5, 17)) == []


def test_trading_days_works_regardless_of_allow_connect():
    """Calendar access is identical whether or not connect is permitted."""
    blocked = ThetaDataProvider(allow_connect=False)
    allowed = ThetaDataProvider(allow_connect=True)
    rng = (date(2026, 5, 1), date(2026, 5, 8))
    assert blocked.trading_days(*rng) == allowed.trading_days(*rng)
