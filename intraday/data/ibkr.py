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

from ..config import DataConfig, EngineConfig, SessionConfig
from ..contracts import BAR_COLUMNS, AssetKind, BarSeries, DataSource
from ..logging_config import get_logger
from ..timeutils import ET, bar_close_index, parse_interval, trading_days as _trading_days
from .provider import DataProvider, DataUnavailable, asset_kind_for

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
# Pure payload → frame / per-day BarSeries
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GridCoverage:
    """How well a session's real bars covered the canonical grid (honesty metric)."""

    symbol: str
    day: date
    expected: int          # canonical grid slots for the session
    present: int           # slots backed by a real IBKR bar
    filled: int            # interior/trailing slots forward-filled from a PAST bar
    leading_missing: int   # leading slots with NO prior bar (would need a FUTURE value)

    @property
    def coverage(self) -> float:
        return (self.present / self.expected) if self.expected else 0.0


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


def _align_day_to_grid(
    day_close_frame: pd.DataFrame,
    symbol: str,
    day: date,
    interval: str,
    *,
    session: SessionConfig,
    is_index: bool,
) -> tuple[pd.DataFrame, GridCoverage]:
    """Reindex one session's CLOSE-labelled frame onto the canonical grid.

    Interior/trailing missing OHLC slots are **forward-filled only** (carry the
    last KNOWN price forward — PIT-correct, uses only the past). We deliberately do
    NOT back-fill: a *leading* missing slot (before the first real bar) would
    otherwise pull a FUTURE bar's value backward, a look-ahead. Leading gaps are
    counted in :class:`GridCoverage.leading_missing` and the caller skips such
    sessions (no observed open ⇒ don't trade it). Missing volume slots are 0 (no
    trade); index instruments carry zero volume by construction.
    """
    target = bar_close_index(day, interval, session)
    aligned = day_close_frame.reindex(target)
    present_mask = aligned["close"].notna()
    present = int(present_mask.sum())
    leading_missing = int(present_mask.to_numpy().argmax()) if present else len(target)
    volume = aligned["volume"].fillna(0.0)
    ohlc = aligned[["open", "high", "low", "close"]].ffill()  # forward (past) only
    out = ohlc.copy()
    out["volume"] = 0.0 if is_index else volume
    out = out[list(BAR_COLUMNS)]
    out.index.name = "ts"
    filled = int(len(target) - present - leading_missing)
    return out, GridCoverage(symbol.upper(), day, len(target), present, filled, leading_missing)


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
    """Split a (multi-day) IBKR payload into per-session canonical-grid BarSeries.

    Sessions are skipped and logged (never padded with fabricated data) when:
    - coverage is below ``min_coverage`` of the canonical grid (half-days /
      holidays / sparse names), or
    - the leading gap exceeds ``max_leading_missing`` (default 0): no observed open
      ⇒ forward-fill cannot define the early bars without a future value, so we do
      not trade that session.
    """
    session = session or SessionConfig()
    latency = latency if latency is not None else pd.Timedelta(milliseconds=DataConfig().bar_latency_ms)
    kind = asset_kind or asset_kind_for(symbol)
    is_index = kind is AssetKind.INDEX

    frame = payload_to_frame(payload)
    if frame.empty:
        return {}
    # START label → CLOSE label (engine indexes by close).
    frame = frame.copy()
    frame.index = frame.index + parse_interval(interval)
    # Session date = ET calendar date of the close timestamp (RTH closes are mid-day).
    et_dates = pd.DatetimeIndex(frame.index).tz_convert(ET).date

    out: dict[date, BarSeries] = {}
    for day in sorted(set(et_dates)):
        day_frame = frame.loc[et_dates == day]
        aligned, cov = _align_day_to_grid(
            day_frame, symbol, day, interval, session=session, is_index=is_index
        )
        if cov.present == 0 or cov.coverage < min_coverage:
            logger.warning(
                "IBKR %s %s: coverage %.0f%% (%d/%d) below %.0f%% — skipping session",
                symbol, day, cov.coverage * 100, cov.present, cov.expected, min_coverage * 100,
            )
            continue
        if cov.leading_missing > max_leading_missing:
            logger.warning(
                "IBKR %s %s: leading gap of %d bars (no observed open; would need a "
                "future value) — skipping session to avoid look-ahead",
                symbol, day, cov.leading_missing,
            )
            continue
        if cov.filled:
            logger.info(
                "IBKR %s %s: forward-filled %d/%d missing %s bars (coverage %.1f%%)",
                symbol, day, cov.filled, cov.expected, interval, cov.coverage * 100,
            )
        out[day] = BarSeries(symbol.upper(), interval, aligned, DataSource.IBKR, latency)
    return out


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
