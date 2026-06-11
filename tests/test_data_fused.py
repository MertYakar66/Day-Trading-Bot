"""Tests for FusedDataProvider — underlying fallbacks + Theta options.

Verifies the composite resolution order, that fallbacks are tried until one
serves, that each frame keeps its OWN source (provenance never lost), and that
options delegate to the configured options provider.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.contracts import BAR_COLUMNS, BarSeries, DataSource
from intraday.data.fused import FusedDataProvider
from intraday.data.provider import DataProvider, DataUnavailable, TierUnavailable
from intraday.timeutils import bar_close_index, trading_days

DAY = date(2026, 6, 1)


def _bars(symbol: str, source: DataSource) -> BarSeries:
    idx = bar_close_index(DAY, "5m")
    frame = pd.DataFrame(
        {c: ([1.0] * len(idx) if c != "volume" else [100.0] * len(idx)) for c in BAR_COLUMNS},
        index=idx,
    )
    return BarSeries(symbol, "5m", frame, source, pd.Timedelta(milliseconds=250))


class _Failing(DataProvider):
    source = DataSource.IBKR

    def __init__(self, exc):
        self.exc = exc

    def trading_days(self, start, end):
        return trading_days(start, end)

    def get_bars(self, symbol, day, interval="1m"):
        raise self.exc

    def get_option_chain(self, symbol, day):
        raise self.exc

    def get_option_tape(self, symbol, day):
        raise self.exc


class _Stub(DataProvider):
    def __init__(self, source):
        self.source = source

    def trading_days(self, start, end):
        return trading_days(start, end)

    def get_bars(self, symbol, day, interval="1m"):
        return _bars(symbol, self.source)

    def get_option_chain(self, symbol, day):
        raise DataUnavailable("stub has no chain")

    def get_option_tape(self, symbol, day):
        raise DataUnavailable("stub has no tape")


def test_falls_back_to_next_underlying_and_keeps_provenance():
    fused = FusedDataProvider(
        [_Failing(DataUnavailable("ibkr shallow here")), _Stub(DataSource.PARITY)]
    )
    bars = fused.get_bars("SPX", DAY, "5m")
    # Second provider served; the frame keeps PARITY provenance, not FUSED.
    assert bars.source is DataSource.PARITY
    assert fused.source is DataSource.FUSED


@pytest.mark.parametrize("exc", [DataUnavailable("x"), TierUnavailable("x"), FileNotFoundError("x")])
def test_all_fallback_error_types_are_caught(exc):
    fused = FusedDataProvider([_Failing(exc), _Stub(DataSource.IBKR)])
    assert fused.get_bars("SPY", DAY, "5m").source is DataSource.IBKR


def test_raises_when_all_underlying_fail():
    fused = FusedDataProvider([_Failing(DataUnavailable("a")), _Failing(TierUnavailable("b"))])
    with pytest.raises(DataUnavailable):
        fused.get_bars("SPY", DAY, "5m")


def test_first_provider_wins():
    fused = FusedDataProvider([_Stub(DataSource.IBKR), _Stub(DataSource.PARITY)])
    assert fused.get_bars("SPY", DAY, "5m").source is DataSource.IBKR


def test_options_delegate_to_options_provider():
    class _Options(_Stub):
        def get_option_chain(self, symbol, day):
            from intraday.contracts import CHAIN_COLUMNS, OptionChainSeries
            return OptionChainSeries(symbol, pd.DataFrame(columns=list(CHAIN_COLUMNS)), self.source)

    fused = FusedDataProvider([_Stub(DataSource.IBKR)], options=_Options(DataSource.THETA))
    assert fused.get_option_chain("SPX", DAY).source is DataSource.THETA


def test_options_without_provider_raises():
    fused = FusedDataProvider([_Stub(DataSource.IBKR)])
    with pytest.raises(DataUnavailable):
        fused.get_option_chain("SPX", DAY)


def test_trading_days_is_union_of_calendars():
    """get_bars falls back through ALL providers, so the calendar must be the
    union — delegating to the first would hide deep-history sessions a fallback
    (e.g. parity) carries behind a shallow primary (e.g. IBKR)."""

    class _Calendar(_Stub):
        def __init__(self, source, days):
            super().__init__(source)
            self.days = days

        def trading_days(self, start, end):
            return [d for d in self.days if start <= d <= end]

    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
    fused = FusedDataProvider([
        _Calendar(DataSource.IBKR, [d3]),            # shallow recent store
        _Calendar(DataSource.PARITY, [d1, d2]),      # deep history fallback
    ])
    assert fused.trading_days(d1, d3) == [d1, d2, d3]
    assert fused.trading_days(d2, d2) == [d2]


def test_trading_days_full_calendar_stubs_match_baseline():
    fused = FusedDataProvider([_Stub(DataSource.IBKR), _Stub(DataSource.PARITY)])
    assert fused.trading_days(date(2026, 6, 1), date(2026, 6, 5)) == trading_days(
        date(2026, 6, 1), date(2026, 6, 5)
    )


def test_empty_underlying_list_rejected():
    with pytest.raises(ValueError):
        FusedDataProvider([])


def test_synthetic_underlying_rejected_no_laundering():
    """FUSED advertises a REAL composite source; a synthetic provider in the chain
    could be served and reported as '[REAL DATA: fused]'. Reject it at the door."""
    from intraday.data.synthetic import SyntheticDataProvider

    with pytest.raises(ValueError):
        FusedDataProvider([SyntheticDataProvider()])
    # Synthetic options provider is likewise rejected.
    with pytest.raises(ValueError):
        FusedDataProvider([_Stub(DataSource.IBKR)], options=SyntheticDataProvider())
