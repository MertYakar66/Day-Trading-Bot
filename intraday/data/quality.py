"""Liquidity / data-quality checks (the DESIGN.md §6 liquidity-gate analogue).

Pure, side-effect-free predicates used by the downgrade-only LiquidityGate
reviewer and the freshness guard. Kept independent of any provider so they can be
unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..timeutils import parse_interval
from .provider import FeedGapError


@dataclass(frozen=True)
class LiquidityCheck:
    """Result of a liquidity assessment for a contemplated trade."""

    ok: bool
    spread_bps: float
    reason: str = ""


def spread_bps(spread: float, price: float) -> float:
    """Bid-ask spread in basis points of price (inf if price <= 0)."""
    if price <= 0 or spread < 0:
        return float("inf")
    return (spread / price) * 10_000.0


def assess_liquidity(
    spread: float,
    price: float,
    *,
    max_spread_bps: float = 25.0,
) -> LiquidityCheck:
    """Flag a trade illiquid if its quoted spread is wider than ``max_spread_bps``.

    25 bps default is generous for liquid index ETFs (SPY/QQQ trade ~1-2 bps);
    anything wider on these names signals a thin/aberrant quote we should skip.
    """
    sb = spread_bps(spread, price)
    if sb > max_spread_bps:
        return LiquidityCheck(False, sb, f"spread {sb:.1f}bps > {max_spread_bps:.1f}bps")
    return LiquidityCheck(True, sb)


def is_stale(
    last_ts: pd.Timestamp,
    as_of: pd.Timestamp,
    *,
    max_age: pd.Timedelta,
) -> bool:
    """True if the most recent datum is older than ``max_age`` at ``as_of``
    (the DESIGN.md §2.4 freshness rule — halt rather than trade on stale data)."""
    return (pd.Timestamp(as_of) - pd.Timestamp(last_ts)) > max_age


def detect_feed_gap(
    index: pd.DatetimeIndex,
    interval: str,
    *,
    max_gap_factor: float = 1.5,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timedelta] | None:
    """Return the first ``(before, after, gap)`` where consecutive bars are spaced
    more than ``max_gap_factor × interval`` apart, else ``None``. A gap means the
    feed dropped bars — the runtime must not silently interpolate over it."""
    if index is None or len(index) < 2:
        return None
    step = parse_interval(interval)
    max_allowed = step * max_gap_factor
    diffs = index[1:] - index[:-1]
    for i in range(len(diffs)):
        if diffs[i] > max_allowed:
            return (index[i], index[i + 1], diffs[i])
    return None


def assert_no_feed_gap(
    index: pd.DatetimeIndex,
    interval: str,
    *,
    max_gap_factor: float = 1.5,
) -> None:
    """Raise :class:`FeedGapError` if the bar index has a gap (DESIGN §2.4: halt,
    never guess). Called by the backtest/runtime before acting on a session."""
    gap = detect_feed_gap(index, interval, max_gap_factor=max_gap_factor)
    if gap is not None:
        before, after, delta = gap
        raise FeedGapError(
            f"feed gap between {before} and {after} ({delta} > "
            f"{interval}×{max_gap_factor}); halting per DESIGN §2.4 (no look-ahead "
            "interpolation over missing bars)."
        )
