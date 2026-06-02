"""Position sizing: fractional Kelly (SWE risk_manager) with hard caps (DESIGN §7).

We size the *risk* of a trade, not its notional: SWE's
``calculate_kelly_fraction`` returns a capital fraction in ``[0, 0.25]`` (it is
already half-Kelly-capable and hard-capped at 25%). We interpret ``f·NAV`` as the
dollar risk budget for the trade, then convert to a unit count via the per-unit
stop distance, and finally clamp by:

- ``max_risk_per_trade_pct`` — never risk more than this fraction of NAV per trade;
- ``max_position_notional_pct`` — never let one position's notional exceed this.

``NAV`` is the ASSUMPTION ``RiskConfig.paper_nav`` (DESIGN.md §11) — sizing only,
never a live-money parameter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import RiskConfig
from ..contracts import SignalProposal


@dataclass(frozen=True)
class SizingResult:
    size: int
    kelly_fraction: float
    risk_dollars: float
    reason: str


def kelly_size(proposal: SignalProposal, risk: RiskConfig | None = None) -> SizingResult:
    """Return a sized position for ``proposal`` under fractional-Kelly + caps."""
    risk = risk or RiskConfig()
    nav = risk.paper_nav
    mult = proposal.instrument.multiplier
    loss_per_unit = proposal.risk_per_unit
    win_per_unit = proposal.reward_per_unit

    from engine.risk_manager import calculate_kelly_fraction  # lazy

    f = calculate_kelly_fraction(
        win_rate=proposal.win_prob,
        avg_win=max(win_per_unit, 1e-9),
        avg_loss=max(loss_per_unit, 1e-9),
        kelly_fraction=risk.kelly_fraction,
    )
    if not math.isfinite(f) or f <= 0.0:
        return SizingResult(0, max(f, 0.0), 0.0, "kelly<=0")

    # Risk budget: the smaller of the Kelly allocation and the per-trade cap.
    risk_budget = min(f, risk.max_risk_per_trade_pct) * nav

    if loss_per_unit <= 0.0:
        return SizingResult(0, f, risk_budget, "no_stop_distance")

    size_by_risk = risk_budget / (loss_per_unit * mult)
    # Notional cap.
    denom = max(proposal.ref_price * mult, 1e-9)
    size_by_notional = (risk.max_position_notional_pct * nav) / denom

    size = int(math.floor(min(size_by_risk, size_by_notional)))
    size = max(0, min(size, risk.max_size))
    if size < risk.min_size:
        return SizingResult(0, f, risk_budget, "below_min_size")
    return SizingResult(size, f, risk_budget, "ok")
