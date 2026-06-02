"""Read-only live polling (PAPER ONLY — never places an order).

A single-shot, point-in-time evaluation that mirrors one bar of the backtester:
pull the latest available bars from a read-only provider (IBKR/Yahoo snapshot or
the store), build a PIT :class:`FeatureRow`, run each strategy through the SAME
expectancy gate + downgrade-only reviewers, and return the gated decisions. It
touches no broker and writes no orders — at most it appends a paper-ledger row.
This is "what you can do with IBKR read-only": surface tradeable paper signals.
"""

from __future__ import annotations

from .poller import LiveDecision, LivePoller

__all__ = ["LiveDecision", "LivePoller"]
