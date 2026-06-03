"""Intraday day-trading decision engine (Phase 0).

A standalone, **paper-only** intraday engine for the SPX/SPY/QQQ "gamma spine".
It reuses the Smart Wheel Engine (SWE) quant math as a read-only dependency
(``vendor/swe``) but shares none of its decision path. See ``DESIGN.md`` for the
full spec and ``docs/SWE_API_REFERENCE.md`` for the dependency's API surface.

Hard guardrails (enforced throughout; see ``README.md``):
- PAPER ONLY — no broker, no live orders.
- No look-ahead — every feature/decision uses only data available at or before
  the decision timestamp (point-in-time discipline).
- Net-of-cost expectancy gate is the sole authority; reviewers only downgrade.
- The SWE dependency is never modified.

Importing this package puts ``vendor/swe`` on ``sys.path`` (see
:mod:`intraday._vendor`); it does not import any SWE module or touch the network.
"""

from __future__ import annotations

from . import _vendor  # noqa: F401  (side effect: ensure vendor/swe importable)

__version__ = "0.1.0"
__all__ = ["__version__"]
