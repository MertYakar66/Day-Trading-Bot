"""Intraday data layer: PIT-aware providers + the partitioned parquet store.

Public surface:
- :class:`DataProvider` — the abstract feed interface.
- :class:`SyntheticDataProvider` — deterministic, labelled-synthetic workhorse.
- :class:`ThetaDataProvider` — real-Theta adapter (disconnected this session).
- :class:`ParquetStore` — the ``ticker=/date=`` parquet store (DESIGN §2.3).
- quality predicates + exceptions.
"""

from __future__ import annotations

from .provider import (
    DataProvider,
    DataUnavailable,
    FeedGapError,
    TierUnavailable,
    asset_kind_for,
)
from .quality import (
    LiquidityCheck,
    assert_no_feed_gap,
    assess_liquidity,
    detect_feed_gap,
    is_stale,
    spread_bps,
)
from .store import ParquetStore
from .synthetic import SyntheticDataProvider
from .theta_adapter import ThetaDataProvider, ThetaNotConnectedThisSession

__all__ = [
    "DataProvider",
    "DataUnavailable",
    "TierUnavailable",
    "FeedGapError",
    "asset_kind_for",
    "SyntheticDataProvider",
    "ThetaDataProvider",
    "ThetaNotConnectedThisSession",
    "ParquetStore",
    "LiquidityCheck",
    "assess_liquidity",
    "is_stale",
    "spread_bps",
    "detect_feed_gap",
    "assert_no_feed_gap",
]
