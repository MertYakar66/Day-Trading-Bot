"""Intraday data layer: PIT-aware providers + the partitioned parquet store.

The corrected real-data architecture (PROGRESS.md "Real-data tier correction"):
no single feed has everything, so providers compose behind one interface —
underlying from IBKR (recent/live) → put-call parity (deep history) →
free-daily (context); options from Theta (STANDARD); synthetic as the test fixture.

Public surface:
- :class:`DataProvider` — the abstract feed interface.
- :class:`SyntheticDataProvider` — deterministic, labelled-synthetic workhorse.
- :class:`IBKRDataProvider` — real underlying intraday bars (reads only).
- :class:`ParityUnderlyingProvider` — underlying via put-call parity (deep history).
- :class:`StoreBackedProvider` — replay captured data from the store (CI-safe).
- :class:`FusedDataProvider` — composite (underlying fallbacks + Theta options).
- :class:`ThetaDataProvider` — real-Theta OPTIONS adapter (disconnected this session).
- :class:`ParquetStore` — the ``ticker=/date=`` parquet store (DESIGN §2.3).
- quality predicates + exceptions.
"""

from __future__ import annotations

from ._remap import GridCoverage
from .fused import FusedDataProvider
from .ibkr import (
    IBKR_CONTRACTS,
    IBKRClient,
    IBKRContract,
    IBKRDataProvider,
)
from .parity import (
    ParityUnderlyingProvider,
    forward_from_parity,
    implied_spot,
    reconstruct_spot,
    spot_from_forward,
)
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
from .store_provider import StoreBackedProvider
from .synthetic import SyntheticDataProvider
from .theta_adapter import ThetaDataProvider, ThetaNotConnectedThisSession
from .yahoo import (
    UrllibYahooClient,
    YahooClient,
    YahooDataProvider,
)

__all__ = [
    "DataProvider",
    "DataUnavailable",
    "TierUnavailable",
    "FeedGapError",
    "asset_kind_for",
    "SyntheticDataProvider",
    "IBKRDataProvider",
    "IBKRClient",
    "IBKRContract",
    "IBKR_CONTRACTS",
    "YahooDataProvider",
    "YahooClient",
    "UrllibYahooClient",
    "GridCoverage",
    "ParityUnderlyingProvider",
    "forward_from_parity",
    "spot_from_forward",
    "implied_spot",
    "reconstruct_spot",
    "StoreBackedProvider",
    "FusedDataProvider",
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
