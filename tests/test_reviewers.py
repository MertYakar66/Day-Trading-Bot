"""Downgrade-only reviewer tests (DESIGN §6; TASKS.md T0.7).

The crown invariant: a reviewer may demote a verdict but NEVER promote it. We
prove this exhaustively over all (current, proposed) verdict pairs, then test each
concrete reviewer's trigger behavior.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.authority.reviewers import (
    DailyKillSwitchReviewer,
    EventLockoutReviewer,
    LiquidityGateReviewer,
    RegimeFilterReviewer,
    ReviewContext,
    build_default_event_gate,
)
from intraday.contracts import (
    AssetKind,
    CostBreakdown,
    GammaRegime,
    GateResult,
    Side,
    SignalProposal,
    Verdict,
)
from intraday.features.base import FeatureRow

pytestmark = pytest.mark.gate


def _result(verdict=Verdict.PROCEED, spread=0.4, ref=750.0) -> GateResult:
    prop = SignalProposal(
        strategy_id="T", symbol="SPY", ts=pd.Timestamp("2026-05-18T15:00:00Z"),
        side=Side.LONG, instrument=AssetKind.STOCK, ref_price=ref,
        target_price=752.0, stop_price=749.0, win_prob=0.6, spread=spread,
    )
    cost = CostBreakdown(0, 0, 0, 0, 0.0)
    return GateResult(prop, 100, verdict, 10.0, 5.0, cost, 0.0, "x")


def test_with_downgrade_never_upgrades():
    """Exhaustive: with_downgrade(current, proposed) is never less restrictive."""
    for cur in Verdict:
        for proposed in Verdict:
            out = _result(cur).with_downgrade(proposed, "r")
            assert out.verdict <= cur  # never upgraded
            assert out.verdict == Verdict(min(int(cur), int(proposed)))


def test_reviewers_cannot_rescue_blocked():
    """A BLOCKED result stays BLOCKED through every reviewer (no rescue)."""
    reviewers = [
        EventLockoutReviewer(build_default_event_gate([2026])),
        RegimeFilterReviewer({GammaRegime.SHORT_GAMMA: Verdict.SKIP}),
        LiquidityGateReviewer(max_spread_bps=1.0),  # would skip if it could
        DailyKillSwitchReviewer(0.03),
    ]
    res = _result(Verdict.BLOCKED, spread=10.0)
    ctx = ReviewContext(
        as_of=pd.Timestamp("2026-05-18T15:00:00Z"), symbol="SPY",
        feature_row=FeatureRow("SPY", pd.Timestamp("2026-05-18T15:00:00Z"), gamma_regime=GammaRegime.SHORT_GAMMA),
        daily_realized_pnl=-1e9, nav=100_000, session_killed=True,
    )
    for r in reviewers:
        res = r.review(res, ctx)
    assert res.verdict is Verdict.BLOCKED


def test_liquidity_gate_skips_wide_spread():
    rev = LiquidityGateReviewer(max_spread_bps=25.0)
    ctx = ReviewContext(as_of=pd.Timestamp("2026-05-18T15:00:00Z"), symbol="SPY")
    # Tight spread (≈5bps) → unchanged.
    ok = rev.review(_result(spread=0.4, ref=750.0), ctx)
    assert ok.verdict is Verdict.PROCEED
    # Wide spread (>25bps) → SKIP.
    wide = rev.review(_result(spread=5.0, ref=750.0), ctx)
    assert wide.verdict is Verdict.SKIP


def test_daily_kill_switch():
    rev = DailyKillSwitchReviewer(daily_loss_limit_pct=0.03)
    asof = pd.Timestamp("2026-05-18T15:00:00Z")
    within = ReviewContext(as_of=asof, symbol="SPY", daily_realized_pnl=-1000, nav=100_000)
    assert rev.review(_result(), within).verdict is Verdict.PROCEED
    breached = ReviewContext(as_of=asof, symbol="SPY", daily_realized_pnl=-3500, nav=100_000)
    assert rev.review(_result(), breached).verdict is Verdict.BLOCKED
    latched = ReviewContext(as_of=asof, symbol="SPY", daily_realized_pnl=0, nav=100_000, session_killed=True)
    assert rev.review(_result(), latched).verdict is Verdict.BLOCKED


def test_regime_filter_downgrades_only_hostile():
    rev = RegimeFilterReviewer({GammaRegime.SHORT_GAMMA: Verdict.SKIP})
    asof = pd.Timestamp("2026-05-18T15:00:00Z")
    hostile = ReviewContext(as_of=asof, symbol="SPY",
                            feature_row=FeatureRow("SPY", asof, gamma_regime=GammaRegime.SHORT_GAMMA))
    assert rev.review(_result(), hostile).verdict is Verdict.SKIP
    benign = ReviewContext(as_of=asof, symbol="SPY",
                           feature_row=FeatureRow("SPY", asof, gamma_regime=GammaRegime.LONG_GAMMA))
    assert rev.review(_result(), benign).verdict is Verdict.PROCEED
    # No regime read → no downgrade (no soft-warn on absent evidence).
    no_read = ReviewContext(as_of=asof, symbol="SPY", feature_row=FeatureRow("SPY", asof))
    assert rev.review(_result(), no_read).verdict is Verdict.PROCEED


@pytest.mark.swe
def test_event_lockout_blocks_on_fomc():
    """On a known FOMC date, the event-lockout reviewer downgrades to SKIP."""
    from engine.event_calendar import EventCalendarBuilder

    fomc_dates = [e.event_date for e in EventCalendarBuilder.generate_fomc_dates(2026)]
    assert fomc_dates, "expected hardcoded 2026 FOMC dates in SWE"
    gate = build_default_event_gate([2026])
    rev = EventLockoutReviewer(gate)
    fomc = fomc_dates[0]
    asof = pd.Timestamp(fomc).tz_localize("UTC") + pd.Timedelta(hours=15)
    ctx = ReviewContext(as_of=asof, symbol="SPY")
    assert rev.review(_result(), ctx).verdict is Verdict.SKIP
    # A clearly-non-event day (a Wednesday far from any macro event) is unchanged.
    quiet = pd.Timestamp("2026-05-20T15:00:00Z")
    ctx2 = ReviewContext(as_of=quiet, symbol="SPY")
    assert rev.review(_result(), ctx2).verdict is Verdict.PROCEED
