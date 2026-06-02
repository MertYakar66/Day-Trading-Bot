"""Opening-range (ORB) feature (DESIGN.md §4).

The opening range is the high/low (and volume) of the first ``minutes`` of the
session. It is only *known* once that window closes, so :func:`opening_range`
reports the window-end timestamp; a PIT consumer must not use the range before
then (plus arrival latency). This is the classic intraday breakout reference.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    volume: float
    window_end: pd.Timestamp  # the range is known only at/after this bar close

    def is_known_at(self, as_of: pd.Timestamp, latency: pd.Timedelta = pd.Timedelta(0)) -> bool:
        return pd.Timestamp(as_of) >= self.window_end + latency


def opening_range(bars: pd.DataFrame, session_open: pd.Timestamp, minutes: int = 15) -> OpeningRange | None:
    """Compute the opening range from bars whose close falls within the first
    ``minutes`` after ``session_open``. Returns ``None`` if no bars qualify.
    """
    end = pd.Timestamp(session_open) + pd.Timedelta(minutes=minutes)
    window = bars.loc[bars.index <= end]
    if window.empty:
        return None
    return OpeningRange(
        high=float(window["high"].max()),
        low=float(window["low"].min()),
        volume=float(window["volume"].sum()),
        window_end=window.index[-1],
    )
