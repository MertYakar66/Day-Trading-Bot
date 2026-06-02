"""Real-Theta data adapter — wired to SWE's connector, but NOT connected here.

This is the drop-in real-data path. It wraps the read-only SWE
``engine.theta_connector.ThetaConnector`` so that, on a properly-subscribed
laptop with the Terminal up, the engine could pull live intraday data through the
same :class:`DataProvider` interface the synthetic provider implements.

THIS SESSION IT NEVER CONNECTS. Two independent reasons (see PROGRESS.md and the
``no-theta-this-session`` memory):

1. The operator uses the Theta subscription concurrently; our pulls would
   throttle theirs. So we must not open a socket to ``127.0.0.1:25503``.
2. The subscription tier is FREE — capability probing showed only
   ``/v3/stock/history/eod`` is unlocked; SPX/VIX index, real-time/intraday
   stock, the stock trade tape, and all option data are gated behind
   STANDARD/VALUE. So real intraday data is unavailable regardless.

Accordingly every method raises a clear exception unless ``allow_connect=True``
is explicitly passed (a deliberate future opt-in), and even then intraday/option
methods raise :class:`TierUnavailable` documenting the required tier. The class
is fully importable and unit-testable without ever touching the network.
"""

from __future__ import annotations

from datetime import date

from ..contracts import BarSeries, DataSource, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from .provider import DataProvider, TierUnavailable

logger = get_logger(__name__)

_THETA_BASE_URL = "http://127.0.0.1:25503"


class ThetaNotConnectedThisSession(RuntimeError):
    """Raised because Theta access is disabled for this build session by policy."""


class ThetaDataProvider(DataProvider):
    """Adapter over SWE ``engine.theta_connector`` (kept disconnected by default)."""

    source = DataSource.THETA

    def __init__(self, allow_connect: bool = False) -> None:
        # ``allow_connect`` is a hard, explicit opt-in. Left False this session.
        self.allow_connect = allow_connect
        self._connector = None  # lazily constructed only if allow_connect

    def _guard(self) -> None:
        if not self.allow_connect:
            raise ThetaNotConnectedThisSession(
                "Theta access is disabled this session (operator uses the "
                "subscription concurrently; tier is FREE so intraday data is "
                "gated). Use SyntheticDataProvider. To enable the real path "
                "deliberately, construct ThetaDataProvider(allow_connect=True) "
                "on a STANDARD-tier laptop with the Terminal up."
            )

    def _connector_or_raise(self):  # pragma: no cover - never run this session
        self._guard()
        if self._connector is None:
            # Imported lazily so the module is usable without the SWE dep present
            # and so importing this file never triggers SWE's heavy package init.
            from engine.theta_connector import ThetaConnector  # type: ignore

            self._connector = ThetaConnector()
        return self._connector

    # ------------------------------------------------------------------ #
    # DataProvider interface
    # ------------------------------------------------------------------ #
    def trading_days(self, start: date, end: date) -> list[date]:
        # Calendar logic is feed-independent; reuse the shared trading calendar.
        from ..timeutils import trading_days as _td

        return _td(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        self._guard()
        # FREE tier exposes only /v3/stock/history/eod (daily) — never intraday.
        raise TierUnavailable(
            f"intraday {interval} bars for {symbol} require Theta STANDARD "
            f"(FREE unlocks only EOD stock history). Source: {_THETA_BASE_URL}."
        )

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        self._guard()
        raise TierUnavailable(
            f"option-chain snapshots for {symbol} require Theta STANDARD "
            "(option data is not in the FREE tier)."
        )

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        self._guard()
        raise TierUnavailable(
            f"option tape for {symbol} requires Theta STANDARD "
            "(scripts/pull_theta_option_tape.py; not in the FREE tier)."
        )
