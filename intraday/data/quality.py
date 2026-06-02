"""Liquidity / data-quality checks (the DESIGN.md §6 liquidity-gate analogue).

Pure, side-effect-free predicates used by the downgrade-only LiquidityGate
reviewer and the freshness guard. Kept independent of any provider so they can be
unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


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
