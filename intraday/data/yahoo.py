"""Yahoo Finance underlying data provider — free real intraday bars (reads only).

A second real **underlying** source alongside IBKR (Theta is options-only). Yahoo's
chart API serves ~60 days of 5-minute bars and ~2 years of hourly, free — deeper
than IBKR's ~1000-bar cap, and across a wide universe — which makes a statistically
powered, cross-sectional real-data test feasible without touching Theta. Having two
independent vendors also enables a data-quality cross-check (see
``tests/test_data_quality_xvendor.py``).

Yahoo timestamps are bar-**START** epoch seconds (UTC), identical in spirit to
IBKR, so this module reuses the one PIT-correct canonical-grid remap in
:mod:`intraday.data._remap` (START→CLOSE shift, forward-fill only, leading-gap
skip). Layering mirrors :mod:`intraday.data.ibkr`:

- :func:`yahoo_payload_to_frame` / :func:`bars_by_day` — pure: raw Yahoo chart JSON
  → canonical-grid :class:`BarSeries` (``DataSource.YAHOO``). No network.
- :class:`YahooDataProvider` — pulls via a read-only :class:`YahooClient` protocol
  (default :class:`UrllibYahooClient`; tests use a fake). Underlying only.
- :func:`ingest_payload` — persist captured payloads into the store for offline replay.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

from ..config import EngineConfig, SessionConfig
from ..contracts import AssetKind, BarSeries, DataSource
from ..logging_config import get_logger
from ..timeutils import trading_days as _trading_days
from ._remap import split_start_labelled_to_sessions
from .provider import DataProvider, DataUnavailable

logger = get_logger(__name__)

# Engine interval → Yahoo interval string.
_YAHOO_INTERVAL: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d",
}
# Yahoo intraday history depth (max range Yahoo serves) per interval.
_YAHOO_MAX_RANGE: dict[str, str] = {
    "1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "max",
}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def yahoo_interval_for(interval: str) -> str:
    try:
        return _YAHOO_INTERVAL[interval]
    except KeyError as exc:
        raise DataUnavailable(f"no Yahoo interval mapping for {interval!r}") from exc


# --------------------------------------------------------------------------- #
# Read-only client protocol (+ a default urllib impl)
# --------------------------------------------------------------------------- #
@runtime_checkable
class YahooClient(Protocol):
    """A minimal read-only Yahoo chart client (reads only — no auth, no orders)."""

    def chart(self, symbol: str, *, rng: str, interval: str) -> Mapping: ...


class UrllibYahooClient:
    """Default :class:`YahooClient` over urllib with a browser UA + backoff."""

    def __init__(self, *, retries: int = 4, timeout: float = 15.0) -> None:
        self.retries = retries
        self.timeout = timeout

    def chart(self, symbol: str, *, rng: str, interval: str) -> Mapping:  # pragma: no cover - network
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range={rng}&interval={interval}&includePrePost=false"
        )
        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503) and attempt < self.retries - 1:
                    time.sleep(2.0 * (2 ** attempt))
                    continue
                raise
            except Exception as e:
                last = e
                if attempt < self.retries - 1:
                    time.sleep(2.0 * (2 ** attempt))
                    continue
                raise
        raise DataUnavailable(f"Yahoo fetch failed for {symbol}: {last}")


# --------------------------------------------------------------------------- #
# Pure payload → frame / per-day BarSeries
# --------------------------------------------------------------------------- #
def yahoo_payload_to_frame(payload: Mapping) -> pd.DataFrame:
    """Raw Yahoo chart payload → DataFrame indexed by bar-**START** ts (UTC).

    Columns ``open/high/low/close/volume``. Yahoo returns ``null`` for missing
    bars/halts → coerced to NaN (the shared remap forward-fills interior gaps).
    """
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as exc:
        err = (payload.get("chart") or {}).get("error") if isinstance(payload, Mapping) else None
        raise DataUnavailable(f"malformed Yahoo payload (error={err})") from exc
    ts = result.get("timestamp") or []
    if not ts:
        raise DataUnavailable("Yahoo payload has no 'timestamp' array")
    quote = result["indicators"]["quote"][0]
    idx = pd.to_datetime(pd.Index(ts), unit="s", utc=True)
    data = {}
    for col in ("open", "high", "low", "close", "volume"):
        arr = quote.get(col)
        if arr is None or len(arr) != len(ts):
            raise DataUnavailable(f"Yahoo payload column {col!r} missing/misaligned")
        data[col] = pd.to_numeric(pd.Series(arr), errors="coerce").to_numpy(dtype="float64")
    frame = pd.DataFrame(data, index=idx).sort_index()
    frame.index.name = "ts"
    frame = frame[~frame.index.duplicated(keep="last")]
    # Volume NaN ⇒ 0 (no trade); OHLC NaN left for the remap to forward-fill.
    frame["volume"] = frame["volume"].fillna(0.0)
    return frame


def bars_by_day(
    symbol: str,
    payload: Mapping,
    interval: str,
    *,
    session: SessionConfig | None = None,
    latency: pd.Timedelta | None = None,
    asset_kind: AssetKind | None = None,
    min_coverage: float = 0.8,
    max_leading_missing: int = 0,
) -> dict[date, BarSeries]:
    """Split a Yahoo chart payload into per-session canonical-grid BarSeries
    (tagged ``DataSource.YAHOO``), via the shared PIT remap."""
    return split_start_labelled_to_sessions(
        symbol, yahoo_payload_to_frame(payload), interval, DataSource.YAHOO,
        session=session, latency=latency, asset_kind=asset_kind,
        min_coverage=min_coverage, max_leading_missing=max_leading_missing,
    )


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class YahooDataProvider(DataProvider):
    """Underlying intraday bars from Yahoo via a read-only :class:`YahooClient`.

    For historical replay, ingest once (:func:`ingest_payload`) and read from
    :class:`intraday.data.store_provider.StoreBackedProvider`. Option methods raise
    — Yahoo serves the underlying only.
    """

    source = DataSource.YAHOO

    def __init__(
        self,
        client: YahooClient | None = None,
        *,
        config: EngineConfig | None = None,
        rng: str | None = None,
        min_coverage: float = 0.8,
    ) -> None:
        self.client = client or UrllibYahooClient()
        self.config = config or EngineConfig.default()
        self.rng = rng
        self.min_coverage = min_coverage
        self._cache: dict[tuple[str, str, str], dict[date, BarSeries]] = {}

    def trading_days(self, start: date, end: date) -> list[date]:
        return _trading_days(start, end)

    def _by_day(self, symbol: str, interval: str, rng: str) -> dict[date, BarSeries]:
        key = (symbol.upper(), interval, rng)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        payload = self.client.chart(symbol, rng=rng, interval=yahoo_interval_for(interval))
        by_day = bars_by_day(
            symbol, payload, interval,
            session=self.config.session,
            latency=pd.Timedelta(milliseconds=self.config.data.bar_latency_ms),
            min_coverage=self.min_coverage,
        )
        self._cache[key] = by_day
        return by_day

    def get_bars(
        self, symbol: str, day: date, interval: str = "1m", *, rng: str | None = None
    ) -> BarSeries:
        rng = rng or self.rng or _YAHOO_MAX_RANGE.get(interval, "60d")
        by_day = self._by_day(symbol, interval, rng)
        bars = by_day.get(day)
        if bars is None:
            raise DataUnavailable(
                f"no Yahoo {interval} bars for {symbol} on {day} within range {rng!r} "
                "(Yahoo intraday history is shallow; widen range or ingest+replay)."
            )
        return bars

    def get_option_chain(self, symbol: str, day: date):
        raise DataUnavailable("YahooDataProvider serves the UNDERLYING only (no options).")

    def get_option_tape(self, symbol: str, day: date):
        raise DataUnavailable("YahooDataProvider serves the UNDERLYING only (no options).")


def ingest_payload(
    store,
    symbol: str,
    interval: str,
    payload: Mapping,
    *,
    session: SessionConfig | None = None,
    latency: pd.Timedelta | None = None,
    asset_kind: AssetKind | None = None,
    min_coverage: float = 0.8,
    max_leading_missing: int = 0,
) -> list[date]:
    """Persist a captured Yahoo payload to ``store`` (``DataSource.YAHOO``)."""
    by_day = bars_by_day(
        symbol, payload, interval,
        session=session, latency=latency, asset_kind=asset_kind,
        min_coverage=min_coverage, max_leading_missing=max_leading_missing,
    )
    for day, bars in by_day.items():
        store.write_bars(bars, day)
    return sorted(by_day)
