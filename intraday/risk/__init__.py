"""Risk layer: fractional-Kelly sizing, stops, and the daily kill-switch knob."""

from __future__ import annotations

from .sizing import SizingResult, kelly_size
from .stops import sigma_stop_target

__all__ = ["SizingResult", "kelly_size", "sigma_stop_target"]
