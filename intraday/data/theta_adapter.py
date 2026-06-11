"""Real-Theta data adapter — OPTIONS ONLY, and NOT connected this session.

CORRECTED DATA-TIER REALITY (supersedes the earlier "Theta serves intraday
stock/index" assumption — see PROGRESS.md "Real-data tier correction"):

- **Theta OPTIONS = STANDARD** → intraday option TICK tape + IV + 1st-order
  greeks, 2016→today. This is the ONLY real data Theta provides to this engine.
- **Theta STOCK = FREE** → end-of-day only; NO intraday stock.
- **Theta INDEX (SPX/VIX) = FREE** → no access at all.

Therefore Theta is an **options-only** provider here. The UNDERLYING (SPY/QQQ
stock; SPX/VIX index) comes from IBKR (:class:`intraday.data.ibkr.IBKRDataProvider`)
or, for deep history, put-call parity (:mod:`intraday.data.parity`) — never from
Theta. :meth:`get_bars` accordingly raises a *structural* error (wrong source),
not a tier error.

THIS SESSION IT NEVER CONNECTS. The operator uses the Theta subscription
concurrently, so we must not open a socket to ``127.0.0.1:25503`` or throttle
their pulls. Every option method raises :class:`ThetaNotConnectedThisSession`
unless ``allow_connect=True`` is explicitly passed (a deliberate future opt-in on
a STANDARD-tier laptop with the Terminal up). The class is fully importable and
unit-testable without ever touching the network. The scoped historical options
pull is delivered as an operator runbook (``docs/OPERATOR_RUNBOOK.md`` +
``scripts/pull_theta_tape_scoped.py``), not executed here.
"""

from __future__ import annotations

from datetime import date

from ..contracts import BarSeries, DataSource, OptionChainSeries, OptionTape
from ..logging_config import get_logger
from .provider import DataProvider, DataUnavailable

logger = get_logger(__name__)

_THETA_BASE_URL = "http://127.0.0.1:25503"


class ThetaNotConnectedThisSession(RuntimeError):
    """Raised because Theta access is disabled for this build session by policy."""


class ThetaDataProvider(DataProvider):
    """Adapter over SWE ``engine.theta_connector`` — OPTIONS ONLY, disconnected.

    Wraps the read-only SWE connector so that, on a STANDARD-tier laptop with the
    Terminal up, the engine could pull live/historical option tape, chains, IV and
    greeks through the same :class:`DataProvider` interface the synthetic provider
    implements. Underlying bars are NOT a Theta responsibility (see module docs).
    """

    source = DataSource.THETA

    def __init__(self, allow_connect: bool = False) -> None:
        # ``allow_connect`` is a hard, explicit opt-in. Left False this session.
        self.allow_connect = allow_connect
        self._connector = None  # lazily constructed only if allow_connect

    def _guard(self) -> None:
        if not self.allow_connect:
            raise ThetaNotConnectedThisSession(
                "Theta access is disabled this session (the operator uses the "
                "STANDARD options subscription concurrently; our pulls would "
                "throttle theirs). Use captured data via StoreBackedProvider, or "
                "construct ThetaDataProvider(allow_connect=True) deliberately on a "
                "STANDARD-tier laptop with the Terminal up. See "
                "docs/OPERATOR_RUNBOOK.md for the scoped historical pull."
            )

    # ------------------------------------------------------------------ #
    # DataProvider interface
    # ------------------------------------------------------------------ #
    def trading_days(self, start: date, end: date) -> list[date]:
        # Calendar logic is feed-independent; reuse the shared trading calendar.
        from ..timeutils import trading_days as _td

        return _td(start, end)

    def get_bars(self, symbol: str, day: date, interval: str = "1m") -> BarSeries:
        # STRUCTURAL correction: Theta is options-only here. The underlying does
        # NOT come from Theta at any tier (stock=FREE/EOD, index=unavailable), so
        # this is a wrong-source error, not a tier upsell.
        raise DataUnavailable(
            f"Theta does not serve underlying bars for {symbol}: it is an "
            "OPTIONS-ONLY source here (stock is FREE/EOD-only; index is "
            "unavailable). Get the underlying from IBKRDataProvider (intraday) or "
            "put-call parity (deep history). See PROGRESS.md 'Real-data tier "
            "correction'."
        )

    def get_option_chain(self, symbol: str, day: date) -> OptionChainSeries:
        # The real Theta options path (STANDARD). Disconnected this session.
        self._guard()
        return self._fetch_not_wired("option_chain", symbol)  # pragma: no cover

    def get_option_tape(self, symbol: str, day: date) -> OptionTape:
        # The real Theta options path (STANDARD). Disconnected this session.
        self._guard()
        return self._fetch_not_wired("option_tape", symbol)  # pragma: no cover

    def _fetch_not_wired(self, what: str, symbol: str):  # pragma: no cover - never run
        # allow_connect=True is a deliberate operator opt-in. The live Theta pull is
        # intentionally NOT wired in-engine: it is performed out-of-band by the
        # scoped operator pull (docs/OPERATOR_RUNBOOK.md +
        # scripts/pull_theta_tape_scoped.py, the bot-side gated puller) and then
        # replayed via StoreBackedProvider. We refuse to fabricate connector I/O.
        raise NotImplementedError(
            f"live Theta {what} fetch for {symbol} is not wired in-engine by "
            "design; run the scoped operator pull (docs/OPERATOR_RUNBOOK.md) and "
            "replay the captured data via StoreBackedProvider(source=DataSource.THETA)."
        )
