"""Tests for the read-only Theta data adapter — OPTIONS ONLY, disconnected.

Verifies the corrected data-tier behavior of ``ThetaDataProvider`` (see
PROGRESS.md "Real-data tier correction"):

- Theta is an OPTIONS-ONLY source here; it NEVER serves the underlying. So
  ``get_bars`` raises a *structural* ``DataUnavailable`` (wrong source) at any
  ``allow_connect`` setting — the underlying comes from IBKR / put-call parity.
- The option methods are the real STANDARD path but are disconnected by policy
  this session: ``get_option_chain`` / ``get_option_tape`` raise
  ``ThetaNotConnectedThisSession`` unless ``allow_connect=True`` (then they raise
  ``NotImplementedError`` — the live pull is an out-of-band operator step).

No test asserts a successful data fetch, and none opens a socket.
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.contracts import DataSource
from intraday.data.provider import DataUnavailable
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
    # THETA is a real (non-synthetic) feed.
    assert provider.source.is_synthetic is False
    assert provider.source.is_real is True


def test_default_allow_connect_is_false():
    """By policy the adapter is constructed disconnected by default."""
    provider = ThetaDataProvider()
    assert provider.allow_connect is False
    # No connector is constructed eagerly.
    assert provider._connector is None


@pytest.mark.parametrize("allow_connect", [False, True])
def test_get_bars_is_structural_data_unavailable(allow_connect):
    """Theta is options-only: get_bars ALWAYS raises DataUnavailable (wrong source),
    independent of allow_connect — it is not a tier upsell. Never opens a socket."""
    provider = ThetaDataProvider(allow_connect=allow_connect)
    with pytest.raises(DataUnavailable) as exc:
        provider.get_bars(SYMBOL, DAY, "1m")
    msg = str(exc.value)
    assert "OPTIONS-ONLY" in msg
    assert "IBKR" in msg  # points at the correct underlying source
    # Not the not-connected error — this is structural, not a policy block.
    assert not isinstance(exc.value, ThetaNotConnectedThisSession)


@pytest.mark.parametrize("method", ["get_option_chain", "get_option_tape"])
def test_option_methods_blocked_when_not_connected(method):
    """With allow_connect=False, option methods raise ThetaNotConnectedThisSession."""
    provider = ThetaDataProvider(allow_connect=False)
    with pytest.raises(ThetaNotConnectedThisSession):
        getattr(provider, method)(SYMBOL, DAY)


def test_not_connected_error_is_runtimeerror():
    """ThetaNotConnectedThisSession is a RuntimeError subclass (catchable as such)."""
    assert issubclass(ThetaNotConnectedThisSession, RuntimeError)
    provider = ThetaDataProvider()
    with pytest.raises(RuntimeError):
        provider.get_option_tape(SYMBOL, DAY)


@pytest.mark.parametrize("method", ["get_option_chain", "get_option_tape"])
def test_option_methods_not_wired_when_allowed(method):
    """With allow_connect=True the guard passes but the live pull is intentionally
    NOT wired in-engine (out-of-band operator step) → NotImplementedError. Still no
    socket: the error is raised before any connector I/O."""
    provider = ThetaDataProvider(allow_connect=True)
    with pytest.raises(NotImplementedError) as exc:
        getattr(provider, method)(SYMBOL, DAY)
    assert "OPERATOR_RUNBOOK" in str(exc.value)


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
