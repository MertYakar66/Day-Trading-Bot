"""Replay captured data from the ParquetStore through the DataProvider interface.

This is how real-data backtests actually run: ingest once (e.g.
:func:`intraday.data.ibkr.ingest_payload`), then replay offline — **network-free,
deterministic, CI-safe**, and identical in shape to the synthetic path. It is the
clean separation real desks use: pull once → store → backtest many times.

``trading_days`` introspects the store's ``bars/ticker=<SYM>/date=<D>``
partitions and (for a multi-symbol run) returns only sessions present for ALL
configured symbols — the engine requires every symbol to share the RTH grid.
Provenance is enforced, never assumed: each frame's source is recovered from its
sidecar and checked against the declared :class:`DataSource`, so captured data can
never be silently relabelled.
"""

from __future__ import annotations

from datetime import date

from ..contracts import BarSeries, DataSource, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from ..timeutils import is_trading_day
from .provider import DataProvider, DataUnavailable
from .store import ParquetStore

logger = get_logger(__name__)


class StoreBackedProvider(DataProvider):
    """Serve previously-ingested bars/chains/tape from a :class:`ParquetStore`."""

    def __init__(
        self,
        store: ParquetStore,
        source: DataSource,
        *,
        symbols: list[str] | None = None,
        interval: str = "1m",
    ) -> None:
        self.store = store
        self.source = source  # declared provenance of the ingested data (e.g. IBKR)
        self.symbols = [s.upper() for s in symbols] if symbols else None
        self.interval = interval

    # -- calendar (from what is actually stored) ------------------------ #
    def _all_symbols(self) -> list[str]:
        base = self.store.root / "bars"
        if not base.exists():
            return []
        return sorted(p.name.split("=", 1)[1] for p in base.glob("ticker=*"))

    def _stored_dates_for(self, symbol: str, interval: str) -> set[date]:
        base = self.store.root / "bars" / f"ticker={symbol.upper()}"
        if not base.exists():
            return set()
        out: set[date] = set()
        for part in base.glob("date=*"):
            if not (part / f"bars_{interval}.parquet").exists():
                continue
            try:
                out.add(date.fromisoformat(part.name.split("=", 1)[1]))
            except ValueError:  # pragma: no cover - malformed partition name
                continue
        return out

    def trading_days(self, start: date, end: date) -> list[date]:
        syms = self.symbols or self._all_symbols()
        if not syms:
            return []
        date_sets = [self._stored_dates_for(s, self.interval) for s in syms]
        common = set.intersection(*date_sets) if len(date_sets) > 1 else date_sets[0]
        return sorted(d for d in common if start <= d <= end and is_trading_day(d))

    # -- frames --------------------------------------------------------- #
    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        bars = self.store.read_bars(symbol, day, interval)
        self._check_source(bars.source, "bars", symbol, day)
        return bars

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        chain = self.store.read_chain(symbol, day)
        self._check_source(chain.source, "option_chain", symbol, day)
        return chain

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        tape = self.store.read_tape(symbol, day)
        self._check_source(tape.source, "option_tape", symbol, day)
        return tape

    def _check_source(self, got: DataSource, what: str, symbol: str, day: date) -> None:
        """Never serve a frame under a provenance it was not written with."""
        if got is not self.source:
            raise DataUnavailable(
                f"provenance mismatch for {what} {symbol} {day}: stored "
                f"{got.value!r} != declared {self.source.value!r}. Refusing to "
                "relabel data sources."
            )
