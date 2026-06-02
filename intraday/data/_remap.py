"""Shared bar-grid remap: vendor START-labelled OHLCV → canonical CLOSE grid.

Both IBKR and Yahoo return intraday bars labelled at the interval **START**, in
UTC; the engine indexes by the bar **CLOSE** on the canonical RTH grid
(:func:`timeutils.bar_close_index`). This module holds the one PIT-correct remap
both providers use, so the no-look-ahead discipline lives in a single place.

Discipline (identical for every vendor):
- shift START→CLOSE (``+interval``) and reindex onto the canonical grid;
- forward-fill only — carry the last KNOWN price forward (PAST-only, PIT-safe) for
  interior/trailing gaps; NEVER back-fill (a leading gap would pull a FUTURE bar
  backward into the open — a look-ahead);
- volume for a missing slot is 0 (no trade); index instruments carry 0 volume;
- skip (never pad) sessions below ``min_coverage`` or with a leading gap beyond
  ``max_leading_missing`` (no observed open ⇒ do not trade that session).

Because the remapped index is a contiguous canonical grid,
:func:`quality.assert_no_feed_gap` is trivially satisfied for remapped data —
``GridCoverage`` (coverage + leading-gap skip) is the real freshness gate here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..config import DataConfig, SessionConfig
from ..contracts import BAR_COLUMNS, AssetKind, BarSeries, DataSource
from ..logging_config import get_logger
from ..timeutils import ET, bar_close_index, parse_interval
from .provider import asset_kind_for

logger = get_logger(__name__)


@dataclass(frozen=True)
class GridCoverage:
    """How well a session's real bars covered the canonical grid (honesty metric)."""

    symbol: str
    day: date
    expected: int          # canonical grid slots for the session
    present: int           # slots backed by a real vendor bar
    filled: int            # interior/trailing slots forward-filled from a PAST bar
    leading_missing: int   # leading slots with NO prior bar (would need a FUTURE value)

    @property
    def coverage(self) -> float:
        return (self.present / self.expected) if self.expected else 0.0


def align_day_to_grid(
    day_close_frame: pd.DataFrame,
    symbol: str,
    day: date,
    interval: str,
    *,
    session: SessionConfig,
    is_index: bool,
) -> tuple[pd.DataFrame, GridCoverage]:
    """Reindex one session's CLOSE-labelled frame onto the canonical grid.

    Interior/trailing gaps are forward-filled (PAST only); leading gaps are left
    NaN and counted (the caller skips such sessions). Volume missing ⇒ 0.
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


def split_start_labelled_to_sessions(
    symbol: str,
    start_frame: pd.DataFrame,
    interval: str,
    source: DataSource,
    *,
    session: SessionConfig | None = None,
    latency: pd.Timedelta | None = None,
    asset_kind: AssetKind | None = None,
    min_coverage: float = 0.8,
    max_leading_missing: int = 0,
) -> dict[date, BarSeries]:
    """Remap a vendor's START-labelled OHLCV frame into per-session canonical-grid
    :class:`BarSeries`, tagged with ``source``. ``start_frame`` is indexed by the
    bar-START timestamp (tz-aware UTC) with columns open/high/low/close/volume.
    """
    session = session or SessionConfig()
    latency = latency if latency is not None else pd.Timedelta(milliseconds=DataConfig().bar_latency_ms)
    kind = asset_kind or asset_kind_for(symbol)
    is_index = kind is AssetKind.INDEX

    if start_frame is None or start_frame.empty:
        return {}
    frame = start_frame.copy()
    frame.index = frame.index + parse_interval(interval)  # START → CLOSE
    # Session date = ET calendar date of the close timestamp (RTH closes are mid-day).
    et_dates = pd.DatetimeIndex(frame.index).tz_convert(ET).date

    out: dict[date, BarSeries] = {}
    for day in sorted(set(et_dates)):
        day_frame = frame.loc[et_dates == day]
        aligned, cov = align_day_to_grid(
            day_frame, symbol, day, interval, session=session, is_index=is_index
        )
        if cov.present == 0 or cov.coverage < min_coverage:
            logger.warning(
                "%s %s %s: coverage %.0f%% (%d/%d) below %.0f%% — skipping session",
                source.value, symbol, day, cov.coverage * 100, cov.present, cov.expected,
                min_coverage * 100,
            )
            continue
        if cov.leading_missing > max_leading_missing:
            logger.warning(
                "%s %s %s: leading gap of %d bars (no observed open) — skipping to "
                "avoid look-ahead", source.value, symbol, day, cov.leading_missing,
            )
            continue
        if cov.filled:
            logger.info(
                "%s %s %s: forward-filled %d/%d missing %s bars (coverage %.1f%%)",
                source.value, symbol, day, cov.filled, cov.expected, interval, cov.coverage * 100,
            )
        out[day] = BarSeries(symbol.upper(), interval, aligned, source, latency)
    return out
