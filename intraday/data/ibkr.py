"""IBKR underlying data provider — real intraday bars + snapshots (reads only).

Why IBKR: Theta is OPTIONS-ONLY at the operator's STANDARD tier (stock=FREE/EOD,
index=unavailable — see :mod:`intraday.data.theta_adapter`), so the UNDERLYING
(SPY/QQQ stock; SPX/VIX index) comes from Interactive Brokers. This module maps an
injected, **read-only** IBKR history client into the engine's PIT
:class:`BarSeries` on the canonical RTH **close** grid. It NEVER places orders or
mutates account state — historical/real-time *data reads only*.

Layering (separates the bulky fetch from the deterministic transform):

- :func:`payload_to_frame` / :func:`bars_by_day` — PURE functions: a raw IBKR
  history payload → canonical-grid :class:`BarSeries`. Fully unit-tested against a
  captured real payload; no network, no SWE import.
- :class:`IBKRDataProvider` — a :class:`DataProvider` that pulls via an
  :class:`IBKRClient` protocol (the operator wires ``ib_insync`` / the IBKR
  gateway; tests use a fake). Underlying only — option methods raise (options come
  from Theta).
- :func:`ingest_payload` — persist captured payloads into the
  :class:`ParquetStore` with ``DataSource.IBKR`` provenance, for offline replay
  via :class:`intraday.data.store_provider.StoreBackedProvider`.

Grid remap (the critical correctness step): IBKR labels intraday bars at the
interval **START**; the engine indexes by the bar **CLOSE**. We shift
start→close (``+interval``) and reindex onto :func:`timeutils.bar_close_index` so
(a) the multi-symbol grid-alignment check and (b) the feed-gap guard both hold.
OHLC for interior/trailing missing intervals is **forward-filled only** — it
carries the last KNOWN price forward (PAST only, PIT-safe) and is **counted**
(``GridCoverage.filled``); volume for a missing interval is ``0`` (no trade). A
missing OPENING bar (a *leading* gap) is **never back-filled from a future bar** —
sessions with a leading gap, or below ``min_coverage`` (holidays / half-days /
sparse names), are skipped and logged. We never fabricate a flat afternoon or a
forward-looking open.

Freshness note: because the remapped index is a contiguous canonical grid,
:func:`quality.assert_no_feed_gap` is trivially satisfied for IBKR/parity-remapped
data — ``GridCoverage`` (coverage + leading-gap skip) IS the real freshness gate
for these sources, not the gap detector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ..config import EngineConfig, SessionConfig
from ..contracts import AssetKind, BarSeries, DataSource
from ..logging_config import get_logger
from ..timeutils import trading_days as _trading_days
from ._remap import split_start_labelled_to_sessions
from .provider import DataProvider, DataUnavailable

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Contract registry (resolved via IBKR search_contracts; reads only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IBKRContract:
    """An IBKR instrument reference (data reads only — never order routing)."""

    symbol: str
    contract_id: int
    exchange: str
    security_type: str  # "STK" (SPY/QQQ) | "IND" (SPX/VIX)


# Verified via the IBKR MCP search_contracts tool (US listings):
IBKR_CONTRACTS: dict[str, IBKRContract] = {
    "SPY": IBKRContract("SPY", 756733, "ARCA", "STK"),
    "QQQ": IBKRContract("QQQ", 320227571, "NASDAQ", "STK"),
    "SPX": IBKRContract("SPX", 416904, "CBOE", "IND"),
    "VIX": IBKRContract("VIX", 13455763, "CBOE", "IND"),
}

# Engine interval string → IBKR ``step`` enum (get_price_history).
_IBKR_STEP: dict[str, str] = {
    "30s": "THIRTY_SECS",
    "1m": "ONE_MIN",
    "5m": "FIVE_MINS",
    "15m": "FIFTEEN_MINS",
    "30m": "THIRTY_MINS",
    "1h": "ONE_HOUR",
}


def ibkr_step_for(interval: str) -> str:
    """IBKR ``step`` enum for an engine interval (raises on unsupported)."""
    try:
        return _IBKR_STEP[interval]
    except KeyError as exc:
        raise DataUnavailable(f"no IBKR step mapping for interval {interval!r}") from exc


# --------------------------------------------------------------------------- #
# Read-only client protocol (operator wires ib_insync / IBKR gateway; tests fake)
# --------------------------------------------------------------------------- #
@runtime_checkable
class IBKRClient(Protocol):
    """A minimal read-only IBKR history client.

    Implementations MUST be data-read-only — never call order/account-mutating
    endpoints. Returns the raw IBKR ``get_price_history`` JSON object.
    """

    def price_history(
        self,
        *,
        contract_id: int,
        exchange: str,
        security_type: str,
        step: str,
        period: str | None = None,
        step_count: int | None = None,
        outside_rth: bool = False,
    ) -> Mapping: ...


# --------------------------------------------------------------------------- #
# Pure payload → frame (the canonical-grid remap is shared via data._remap)
# --------------------------------------------------------------------------- #
def payload_to_frame(payload: Mapping) -> pd.DataFrame:
    """Raw IBKR history payload → DataFrame indexed by bar-**START** ts (UTC).

    Columns: ``open/high/low/close/volume``. Validates array alignment. Index
    instruments return no ``volume`` → filled with zeros. Coerces values to float;
    any non-finite OHLC is left as NaN for the per-day aligner to fill.
    """
    times = payload.get("time")
    if not times:
        raise DataUnavailable("IBKR payload has no 'time' array")
    idx = pd.to_datetime(pd.Index(times), utc=True)
    n = len(idx)
    data: dict[str, np.ndarray] = {}
    for col in ("open", "high", "low", "close"):
        arr = payload.get(col)
        if arr is None or len(arr) != n:
            raise DataUnavailable(
                f"IBKR payload column {col!r} missing or misaligned "
                f"(got {0 if arr is None else len(arr)}, expected {n})"
            )
        data[col] = pd.to_numeric(pd.Series(arr), errors="coerce").to_numpy(dtype="float64")
    vol = payload.get("volume")
    if vol is None:
        data["volume"] = np.zeros(n, dtype="float64")
    else:
        if len(vol) != n:
            raise DataUnavailable(
                f"IBKR payload 'volume' misaligned (got {len(vol)}, expected {n})"
            )
        data["volume"] = pd.to_numeric(pd.Series(vol), errors="coerce").fillna(0.0).to_numpy(
            dtype="float64"
        )
    frame = pd.DataFrame(data, index=idx).sort_index()
    frame.index.name = "ts"
    # Drop accidental duplicate timestamps (keep the last print for that instant).
    frame = frame[~frame.index.duplicated(keep="last")]
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
    """Split a (multi-day) IBKR payload into per-session canonical-grid BarSeries
    (tagged ``DataSource.IBKR``). The PIT remap + coverage/leading-gap skip live in
    :func:`intraday.data._remap.split_start_labelled_to_sessions`."""
    return split_start_labelled_to_sessions(
        symbol, payload_to_frame(payload), interval, DataSource.IBKR,
        session=session, latency=latency, asset_kind=asset_kind,
        min_coverage=min_coverage, max_leading_missing=max_leading_missing,
    )


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class IBKRDataProvider(DataProvider):
    """Underlying intraday bars from IBKR via a read-only :class:`IBKRClient`.

    Suited to recent/live sessions (IBKR history is period-back-from-now and
    intraday-shallow). For deep historical replay, ingest once
    (:func:`ingest_payload`) and read from
    :class:`intraday.data.store_provider.StoreBackedProvider`. Option methods raise
    — options come from Theta (:class:`intraday.data.theta_adapter`).
    """

    source = DataSource.IBKR

    def __init__(
        self,
        client: IBKRClient,
        *,
        config: EngineConfig | None = None,
        default_period: str = "ONE_MONTH",
        min_coverage: float = 0.8,
    ) -> None:
        self.client = client
        self.config = config or EngineConfig.default()
        self.default_period = default_period
        self.min_coverage = min_coverage
        self._cache: dict[tuple[str, str, str], dict[date, BarSeries]] = {}

    def trading_days(self, start: date, end: date) -> list[date]:
        return _trading_days(start, end)

    def _by_day(self, symbol: str, interval: str, period: str) -> dict[date, BarSeries]:
        key = (symbol.upper(), interval, period)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        contract = IBKR_CONTRACTS.get(symbol.upper())
        if contract is None:
            raise DataUnavailable(f"no IBKR contract registered for {symbol!r}")
        payload = self.client.price_history(
            contract_id=contract.contract_id,
            exchange=contract.exchange,
            security_type=contract.security_type,
            step=ibkr_step_for(interval),
            period=period,
            outside_rth=False,
        )
        by_day = bars_by_day(
            symbol, payload, interval,
            session=self.config.session,
            latency=pd.Timedelta(milliseconds=self.config.data.bar_latency_ms),
            asset_kind=AssetKind.INDEX if contract.security_type == "IND" else AssetKind.STOCK,
            min_coverage=self.min_coverage,
        )
        self._cache[key] = by_day
        return by_day

    def get_bars(
        self, symbol: str, day: date, interval: str = "1m", *, period: str | None = None
    ) -> BarSeries:
        period = period or self.default_period
        by_day = self._by_day(symbol, interval, period)
        bars = by_day.get(day)
        if bars is None:
            raise DataUnavailable(
                f"no IBKR {interval} bars for {symbol} on {day} within period "
                f"{period!r} (IBKR history is period-back-from-now; widen the period "
                "or ingest+replay via StoreBackedProvider)."
            )
        return bars

    def get_option_chain(self, symbol: str, day: date):
        raise DataUnavailable(
            "IBKRDataProvider serves the UNDERLYING only; option chains come from "
            "Theta (STANDARD). See intraday.data.theta_adapter."
        )

    def get_option_tape(self, symbol: str, day: date):
        raise DataUnavailable(
            "IBKRDataProvider serves the UNDERLYING only; the option tape comes "
            "from Theta (STANDARD). See intraday.data.theta_adapter."
        )


# --------------------------------------------------------------------------- #
# Ingestion (captured payload → ParquetStore, IBKR provenance)
# --------------------------------------------------------------------------- #
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
    """Persist a captured IBKR payload to ``store`` (one partition per session).

    Returns the sorted list of ingested session dates. Provenance is
    ``DataSource.IBKR`` on every frame (round-trips via the store sidecar).
    """
    by_day = bars_by_day(
        symbol, payload, interval,
        session=session, latency=latency, asset_kind=asset_kind,
        min_coverage=min_coverage, max_leading_missing=max_leading_missing,
    )
    for day, bars in by_day.items():
        store.write_bars(bars, day)
    return sorted(by_day)
