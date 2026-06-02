"""Downgrade-only reviewers (DESIGN.md §6; mirror of SWE CLAUDE.md §2).

A reviewer may demote a verdict (PROCEED → REVIEW → SKIP → BLOCKED) but may NEVER
upgrade one. This is enforced structurally: every reviewer returns its verdict via
:meth:`GateResult.with_downgrade`, which takes the more-restrictive of the current
and proposed verdicts. The "reviewers only downgrade" property test exhausts all
(current, proposed) pairs to prove no reviewer can ever raise a verdict.

Four reviewers (DESIGN.md §6):
- :class:`EventLockoutReviewer`  — earnings/FOMC/CPI windows (SWE ``event_gate``).
- :class:`RegimeFilterReviewer`  — disable a strategy in a hostile gamma regime.
- :class:`LiquidityGateReviewer` — skip when the quoted spread is too wide.
- :class:`DailyKillSwitchReviewer` — block all signals once the daily loss budget
  is hit (SWE ``risk_manager`` ``daily_loss_limit_pct``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pandas as pd

from ..config import EngineConfig
from ..contracts import GammaRegime, GateResult, Verdict
from ..data.quality import assess_liquidity
from ..features.base import FeatureRow
from ..logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ReviewContext:
    """Everything a reviewer may consult about the current decision."""

    as_of: pd.Timestamp
    symbol: str
    feature_row: FeatureRow | None = None
    daily_realized_pnl: float = 0.0
    nav: float = 0.0
    session_killed: bool = False
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Reviewer(Protocol):
    name: str

    def review(self, result: GateResult, ctx: ReviewContext) -> GateResult: ...


class ReviewerPipeline:
    """Applies reviewers in order; each can only downgrade."""

    def __init__(self, reviewers: list[Reviewer]) -> None:
        self.reviewers = reviewers

    def apply(self, result: GateResult, ctx: ReviewContext) -> GateResult:
        for r in self.reviewers:
            result = r.review(result, ctx)
        return result


# --------------------------------------------------------------------------- #
# Concrete reviewers
# --------------------------------------------------------------------------- #


class EventLockoutReviewer:
    """Skip a signal inside an earnings/FOMC/CPI lockout window (SWE event_gate)."""

    name = "event_lockout"

    def __init__(self, event_gate) -> None:
        self._gate = event_gate

    def review(self, result: GateResult, ctx: ReviewContext) -> GateResult:
        day = pd.Timestamp(ctx.as_of).date()
        blocked, reason = self._gate.is_blocked(ctx.symbol, day, day)
        if blocked:
            return result.with_downgrade(Verdict.SKIP, f"{self.name}:{reason}")
        return result


class RegimeFilterReviewer:
    """Downgrade when the dealer-gamma regime is hostile to the strategy.

    ``hostile`` maps a :class:`GammaRegime` to the maximum verdict permitted while
    in that regime (e.g. ``{GammaRegime.SHORT_GAMMA: Verdict.SKIP}``). Absent a
    regime read (no chain), it does nothing — soft-warns never fire on absent
    evidence (mirrors SWE R7-R10 semantics). Empty map ⇒ no-op (the default for
    the S3 control, which is intentionally not regime-gated).
    """

    name = "regime_filter"

    def __init__(self, hostile: dict[GammaRegime, Verdict] | None = None) -> None:
        self.hostile = hostile or {}

    def review(self, result: GateResult, ctx: ReviewContext) -> GateResult:
        fr = ctx.feature_row
        if fr is None or fr.gamma_regime is None:
            return result
        cap = self.hostile.get(fr.gamma_regime)
        if cap is not None:
            return result.with_downgrade(cap, f"{self.name}:{fr.gamma_regime.value}")
        return result


class LiquidityGateReviewer:
    """Skip when the contemplated trade's quoted spread is too wide (DESIGN §6)."""

    name = "liquidity_gate"

    def __init__(self, max_spread_bps: float = 25.0) -> None:
        self.max_spread_bps = max_spread_bps

    def review(self, result: GateResult, ctx: ReviewContext) -> GateResult:
        p = result.proposal
        check = assess_liquidity(p.spread, p.ref_price, max_spread_bps=self.max_spread_bps)
        if not check.ok:
            return result.with_downgrade(Verdict.SKIP, f"{self.name}:{check.reason}")
        return result


class DailyKillSwitchReviewer:
    """Block all signals once the day's realized loss hits the budget (DESIGN §7).

    The budget is ``daily_loss_limit_pct × nav``. The backtest latches the kill
    for the rest of the session (``ctx.session_killed``); this reviewer also blocks
    directly off the running daily PnL so it is correct even called standalone.
    """

    name = "daily_kill_switch"

    def __init__(self, daily_loss_limit_pct: float = 0.03) -> None:
        self.daily_loss_limit_pct = daily_loss_limit_pct

    def review(self, result: GateResult, ctx: ReviewContext) -> GateResult:
        if ctx.session_killed:
            return result.with_downgrade(Verdict.BLOCKED, f"{self.name}:latched")
        budget = self.daily_loss_limit_pct * ctx.nav
        if budget > 0 and ctx.daily_realized_pnl <= -budget:
            return result.with_downgrade(
                Verdict.BLOCKED,
                f"{self.name}:daily_loss {ctx.daily_realized_pnl:.0f} <= -{budget:.0f}",
            )
        return result


# --------------------------------------------------------------------------- #
# Default wiring
# --------------------------------------------------------------------------- #


def build_default_event_gate(years: list[int]):
    """An :class:`EventGate` populated with FOMC/CPI/NFP macro lockouts (ticker
    ``"*"``) for the given years, from SWE's hardcoded/computed calendars."""
    from engine.event_calendar import EventCalendarBuilder  # lazy
    from engine.event_gate import EventGate, ScheduledEvent

    gate = EventGate(macro_buffer_days=1)
    for year in years:
        for kind, gen in (
            ("fomc", EventCalendarBuilder.generate_fomc_dates),
            ("cpi", EventCalendarBuilder.generate_cpi_dates),
            ("nfp", EventCalendarBuilder.generate_nfp_dates),
        ):
            for ev in gen(year):
                gate.add_event(ScheduledEvent("*", kind, ev.event_date))
    return gate


def default_reviewers(
    config: EngineConfig,
    *,
    years: list[int] | None = None,
    regime_hostile: dict[GammaRegime, Verdict] | None = None,
) -> ReviewerPipeline:
    """The Phase-0 reviewer stack. ``regime_hostile`` defaults to empty (the S3
    control is not regime-gated); event lockout uses the given years' calendar."""
    years = years or [2025, 2026, 2027]
    return ReviewerPipeline(
        [
            EventLockoutReviewer(build_default_event_gate(years)),
            RegimeFilterReviewer(regime_hostile or {}),
            LiquidityGateReviewer(),
            DailyKillSwitchReviewer(config.risk.daily_loss_limit_pct),
        ]
    )
