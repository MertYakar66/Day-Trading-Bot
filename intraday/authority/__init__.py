"""The discipline layer: one expectancy-gate authority + downgrade-only reviewers.

Nothing reaches the paper ledger without :class:`ExpectancyGate`; reviewers may
only demote its verdict (DESIGN.md §6).
"""

from __future__ import annotations

from .gate import ExpectancyGate
from .reviewers import (
    DailyKillSwitchReviewer,
    EventLockoutReviewer,
    LiquidityGateReviewer,
    RegimeFilterReviewer,
    ReviewContext,
    Reviewer,
    ReviewerPipeline,
    build_default_event_gate,
    default_reviewers,
)

__all__ = [
    "ExpectancyGate",
    "ReviewContext",
    "Reviewer",
    "ReviewerPipeline",
    "EventLockoutReviewer",
    "RegimeFilterReviewer",
    "LiquidityGateReviewer",
    "DailyKillSwitchReviewer",
    "build_default_event_gate",
    "default_reviewers",
]
