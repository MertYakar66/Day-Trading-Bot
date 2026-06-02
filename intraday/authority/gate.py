"""The expectancy gate — the single decision authority (DESIGN.md §6).

A sized proposal is tradeable only if its **net-of-cost** expected dollar PnL
strictly exceeds the configured threshold::

    ev_gross = p·win − (1−p)·loss          (dollars, at the contemplated size)
    ev_net   = ev_gross − round_trip_cost   (cost from intraday.costs / SWE)

Verdicts mirror SWE's ``EnginePhaseReviewer``:
- non-finite EV → BLOCKED ("ev_non_finite")  — guarded BEFORE the sign check;
- EV < 0        → BLOCKED ("negative_ev");
- 0 ≤ EV ≤ thr  → REVIEW  ("below_threshold");
- EV > thr      → PROCEED.

This is the ONLY place a candidate becomes tradeable. Downgrade-only reviewers
(:mod:`intraday.authority.reviewers`) may demote a PROCEED but never rescue a
BLOCKED — the hard invariant from SWE ``CLAUDE.md`` §2, re-implemented here.

EV basis (documented modeling choice): win/loss are derived from the decision-time
``ref_price`` (the bar close), while the backtest enters at the next bar's open
(conservative fill). On a driftless tape ``E[next_open] = ref_price``, so the
certified ``ev_net`` is an unbiased PRE-FILL estimate; the realized entry differs
by next-bar noise + slippage (already charged as cost). Treat ``ev_net`` as a
pre-fill expectation, to be reconciled with realized PnL during paper validation.
"""

from __future__ import annotations

import math

from ..config import EngineConfig
from ..contracts import CostBreakdown, GateResult, SignalProposal, Verdict
from ..costs import round_trip_cost


class ExpectancyGate:
    """Net-of-cost expectancy authority."""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.default()

    def evaluate(self, proposal: SignalProposal, size: int) -> GateResult:
        threshold = self.config.gate.ev_threshold
        p = proposal.win_prob

        # Per-unit gross dollars work for both directional trades (reward/risk ×
        # multiplier) and defined-risk structures (explicit credit / max loss).
        win = proposal.reward_dollars * size
        loss = proposal.risk_dollars * size

        # Structures supply their own per-unit (multi-leg) round-trip cost so it is
        # never understated by the single-instrument model. It is given for one
        # unit and scaled linearly here (exact for spread+commission; conservative
        # for sqrt-impact, which is sublinear — we never understate).
        if proposal.cost_override is not None:
            co = proposal.cost_override
            cost = CostBreakdown(
                entry_slippage=co.entry_slippage * size,
                exit_slippage=co.exit_slippage * size,
                commission=co.commission * size,
                impact=co.impact * size,
                total=co.total * size,
                detail=co.detail,
            )
        else:
            cost = round_trip_cost(
                side=proposal.side,
                instrument=proposal.instrument,
                ref_price=proposal.ref_price,
                spread=proposal.spread,
                size=size,
                adv=proposal.adv,
                config=self.config.cost,
            )

        # Invalid probability is treated as un-evaluable (mirror R1a non-finite).
        if not (math.isfinite(p) and 0.0 <= p <= 1.0):
            return GateResult(
                proposal, size, Verdict.BLOCKED, float("nan"), float("nan"),
                cost, threshold, reason="invalid_win_prob",
            )

        ev_gross = p * win - (1.0 - p) * loss
        ev_net = ev_gross - cost.total

        if not math.isfinite(ev_net):
            verdict, reason = Verdict.BLOCKED, "ev_non_finite"
        elif ev_net < 0.0:
            verdict, reason = Verdict.BLOCKED, "negative_ev"
        elif ev_net <= threshold:
            verdict, reason = Verdict.REVIEW, "below_threshold"
        else:
            verdict, reason = Verdict.PROCEED, "positive_net_ev"

        return GateResult(
            proposal=proposal,
            size=size,
            verdict=verdict,
            ev_gross=ev_gross,
            ev_net=ev_net,
            cost=cost,
            threshold=threshold,
            reason=reason,
        )
