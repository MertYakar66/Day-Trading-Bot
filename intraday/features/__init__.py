"""Intraday feature builders (DESIGN.md §4).

Each builder is causal (no look-ahead) and PIT-sampled. Public surface:
- VWAP + bands (:func:`vwap_frame`), opening range (:func:`opening_range`),
- order-flow imbalance (:func:`ofi_at`), intraday realized vol (:func:`intraday_rv`),
- VRP (:func:`vrp_at`), dealer gamma structure (:func:`gamma_structure_at`),
- the assembling :class:`FeaturePipeline` → :class:`FeatureRow`.
"""

from __future__ import annotations

from .base import FeatureRow, latest_value
from .gex import GammaStructure, gamma_structure_at
from .ofi import ofi_at, ofi_from_prints
from .opening_range import OpeningRange, opening_range
from .pipeline import FeaturePipeline, SessionFeatures
from .realized_vol import annualization_factor, bars_per_day, intraday_rv
from .vrp import VRP, atm_iv_at, vrp_at
from .vwap import vwap_bands, vwap_frame

__all__ = [
    "FeatureRow",
    "FeaturePipeline",
    "SessionFeatures",
    "latest_value",
    "vwap_frame",
    "vwap_bands",
    "opening_range",
    "OpeningRange",
    "ofi_at",
    "ofi_from_prints",
    "intraday_rv",
    "bars_per_day",
    "annualization_factor",
    "vrp_at",
    "atm_iv_at",
    "VRP",
    "gamma_structure_at",
    "GammaStructure",
]
