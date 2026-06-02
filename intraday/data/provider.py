"""The :class:`DataProvider` interface and shared data-layer types.

The engine depends on this abstract interface, never on a concrete feed. Two
implementations exist:

- :class:`intraday.data.synthetic.SyntheticDataProvider` — deterministic, seeded,
  clearly labelled ``DataSource.SYNTHETIC``. The Phase-0 workhorse.
- :class:`intraday.data.theta_adapter.ThetaDataProvider` — wraps the read-only
  SWE ``engine.theta_connector``. **Never connected during a build session**
  (the operator uses the Theta subscription concurrently; the tier is FREE so
  real intraday data is gated anyway). It exists so the real path is a drop-in.

A decision at time ``T`` consumes only PIT-sliced data (see the containers in
:mod:`intraday.contracts`); providers stamp ``available_ts`` using configured
arrival latency so look-ahead cannot leak from the source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..contracts import AssetKind, BarSeries, DataSource, OptionChainSeries, OptionTape

# Canonical asset-kind registry for the Phase-1 universe.
_ASSET_KINDS: dict[str, AssetKind] = {
    "SPX": AssetKind.INDEX,
    "VIX": AssetKind.INDEX,
    "SPY": AssetKind.STOCK,
    "QQQ": AssetKind.STOCK,
}


def asset_kind_for(symbol: str) -> AssetKind:
    """Map a symbol to its :class:`AssetKind` (defaults to STOCK)."""
    return _ASSET_KINDS.get(symbol.upper(), AssetKind.STOCK)


class DataUnavailable(RuntimeError):
    """Requested data cannot be produced/fetched (generic)."""


class TierUnavailable(DataUnavailable):
    """The data exists upstream but the subscription tier does not unlock it."""


class FeedGapError(RuntimeError):
    """A live feed gapped beyond the latency budget — halt, never guess
    (DESIGN.md §2.4 freshness rule)."""


class DataProvider(ABC):
    """Abstract intraday data source.

    All ``day`` arguments are exchange-local calendar dates; all returned frames
    are tz-aware UTC and PIT-stamped. Intervals are strings like ``"1s"``/``"1m"``.
    """

    source: DataSource

    def asset_kind(self, symbol: str) -> AssetKind:
        return asset_kind_for(symbol)

    @abstractmethod
    def trading_days(self, start: date, end: date) -> list[date]:
        """Inclusive list of RTH trading days in ``[start, end]``."""

    @abstractmethod
    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        """OHLCV bars for one RTH session."""

    @abstractmethod
    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        """Intraday option-chain snapshots for one session."""

    @abstractmethod
    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        """Trade-by-trade option tape (with NBBO + ``side_inferred``)."""

    def describe(self) -> str:
        return f"{type(self).__name__}(source={self.source.value})"
