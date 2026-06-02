"""Fused data provider: underlying across sources + options from Theta.

The corrected architecture (PROGRESS.md "Real-data tier correction"): no single
feed has everything, so we COMPOSE them behind one :class:`DataProvider`:

- **underlying** (SPY/QQQ + SPX/VIX): resolve through an ordered list of
  providers — IBKR for recent/live, put-call parity for deep history, free-daily
  for context — taking the first that yields data for the requested session.
- **options** (chain/tape): from Theta (STANDARD), replayed via a store-backed
  provider once captured.

Each returned frame keeps its OWN ``source`` (IBKR/PARITY/THETA/…), so provenance
is never lost even though the composite advertises ``DataSource.FUSED``. A frame is
only ever served by the source that produced it — fallbacks are tried in order and
the chosen source is logged.
"""

from __future__ import annotations

from datetime import date

from ..contracts import BarSeries, DataSource, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from .provider import DataProvider, DataUnavailable, TierUnavailable

logger = get_logger(__name__)

# Errors that mean "this provider can't serve it — try the next fallback".
_FALLBACK_ERRORS = (DataUnavailable, TierUnavailable, FileNotFoundError)


class FusedDataProvider(DataProvider):
    """Compose underlying-fallback providers with an options provider."""

    source = DataSource.FUSED

    def __init__(
        self,
        underlying: list[DataProvider],
        *,
        options: DataProvider | None = None,
    ) -> None:
        if not underlying:
            raise ValueError("FusedDataProvider needs at least one underlying provider")
        # Honesty guard: FUSED advertises a REAL composite source, so a synthetic
        # provider must never sit in the chain — otherwise a synthetic frame could
        # be served and reported as "[REAL DATA: fused]". (Each served frame still
        # carries its own true source; this stops synthetic laundering at the door.)
        synth = [type(p).__name__ for p in underlying if p.source.is_synthetic]
        if synth:
            raise ValueError(
                f"FusedDataProvider underlying must be real sources, not synthetic: {synth}. "
                "Use SyntheticDataProvider directly for synthetic runs."
            )
        if options is not None and options.source.is_synthetic:
            raise ValueError("FusedDataProvider options provider must not be synthetic")
        self.underlying = list(underlying)
        self.options = options

    def trading_days(self, start: date, end: date) -> list[date]:
        # Delegate to the primary (first) underlying provider's calendar; order the
        # providers so the first carries the authoritative session calendar.
        return self.underlying[0].trading_days(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        errors: list[str] = []
        for prov in self.underlying:
            try:
                bars = prov.get_bars(symbol, day, interval)
            except _FALLBACK_ERRORS as exc:
                errors.append(f"{type(prov).__name__}: {exc}")
                continue
            logger.debug(
                "fused underlying %s %s %s served by %s (source=%s)",
                symbol, day, interval, type(prov).__name__, bars.source.value,
            )
            return bars
        raise DataUnavailable(
            f"no underlying provider could serve {symbol} {day} {interval}; "
            f"tried: {'; '.join(errors)}"
        )

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        return self._options_or_raise().get_option_chain(symbol, day)

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        return self._options_or_raise().get_option_tape(symbol, day)

    def _options_or_raise(self) -> DataProvider:
        if self.options is None:
            raise DataUnavailable(
                "no options provider configured in FusedDataProvider (options come "
                "from Theta STANDARD, replayed via StoreBackedProvider once captured)."
            )
        return self.options
