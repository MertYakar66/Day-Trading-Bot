"""Strategy modules. Phase 0 ships S3 (the VWAP-reversion control); S1/S2 land in
Phase 1 behind the same gate."""

from __future__ import annotations

from .base import Strategy
from .s3_vwap_orb import S3VwapOrb

__all__ = ["Strategy", "S3VwapOrb"]
