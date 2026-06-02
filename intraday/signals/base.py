"""Strategy contract.

A strategy turns a PIT :class:`FeatureRow` into at most one :class:`SignalProposal`
per decision tick (or ``None``). It must read only fields of the feature row
(which are already point-in-time) — never the raw future. Sizing, the expectancy
gate, and the reviewers run downstream; a strategy never decides tradeability.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import EngineConfig
from ..contracts import SignalProposal
from ..features.base import FeatureRow


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str

    def propose(self, fr: FeatureRow, *, config: EngineConfig) -> SignalProposal | None:
        """Return a trade idea for this tick, or ``None`` to stand aside."""
        ...
