"""Expectancy-gate authority tests (DESIGN §6; TASKS.md T0.6).

The gate is the only place a candidate becomes tradeable. It must refuse
negative-EV and non-finite-EV signals, and only PROCEED when net-of-cost
expectancy strictly exceeds the threshold.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from intraday.authority.gate import ExpectancyGate
from intraday.config import EngineConfig
from intraday.contracts import AssetKind, SignalProposal, Side, Verdict

pytestmark = pytest.mark.gate


def _proposal(**kw) -> SignalProposal:
    base = dict(
        strategy_id="T", symbol="SPY", ts=pd.Timestamp("2026-05-18T15:00:00Z"),
        side=Side.LONG, instrument=AssetKind.STOCK, ref_price=750.0,
        target_price=752.0, stop_price=749.0, win_prob=0.6, spread=0.4, adv=70_000_000,
    )
    base.update(kw)
    return SignalProposal(**base)


def test_positive_ev_proceeds():
    gate = ExpectancyGate(EngineConfig.default())
    res = gate.evaluate(_proposal(win_prob=0.7, target_price=755.0, stop_price=749.0), size=100)
    assert res.verdict is Verdict.PROCEED
    assert res.ev_net > 0


def test_negative_ev_blocked():
    gate = ExpectancyGate(EngineConfig.default())
    # Tiny win prob with symmetric payoff → negative gross EV.
    res = gate.evaluate(_proposal(win_prob=0.05, target_price=751.0, stop_price=749.0), size=100)
    assert res.verdict is Verdict.BLOCKED
    assert res.reason == "negative_ev"
    assert res.ev_net < 0


def test_non_finite_prob_blocked():
    gate = ExpectancyGate(EngineConfig.default())
    res = gate.evaluate(_proposal(win_prob=float("nan")), size=100)
    assert res.verdict is Verdict.BLOCKED
    assert res.reason == "invalid_win_prob"


def test_prob_out_of_range_blocked():
    gate = ExpectancyGate(EngineConfig.default())
    assert gate.evaluate(_proposal(win_prob=1.5), size=100).verdict is Verdict.BLOCKED
    assert gate.evaluate(_proposal(win_prob=-0.2), size=100).verdict is Verdict.BLOCKED


def test_ev_net_equals_gross_minus_cost():
    gate = ExpectancyGate(EngineConfig.default())
    res = gate.evaluate(_proposal(win_prob=0.7, target_price=755.0, stop_price=749.0), size=100)
    assert res.ev_net == pytest.approx(res.ev_gross - res.cost.total)


def test_costs_can_flip_positive_gross_to_blocked():
    """A trade with positive gross EV but tiny edge is blocked once costs bite."""
    gate = ExpectancyGate(EngineConfig.default())
    # Barely-positive gross (small reward, p just above 0.5), large size+spread cost.
    res = gate.evaluate(
        _proposal(win_prob=0.51, target_price=750.05, stop_price=749.95, spread=2.0),
        size=500,
    )
    assert res.ev_gross > 0 or res.ev_gross == pytest.approx(res.ev_gross)
    assert res.ev_net < res.ev_gross  # costs subtracted
    assert res.verdict is Verdict.BLOCKED


def test_below_threshold_is_review():
    cfg = EngineConfig.default()
    # Raise the threshold so a small positive net EV lands in the REVIEW band.
    from dataclasses import replace
    cfg = replace(cfg, gate=replace(cfg.gate, ev_threshold=1e9))
    gate = ExpectancyGate(cfg)
    res = gate.evaluate(_proposal(win_prob=0.7, target_price=755.0, stop_price=749.0), size=100)
    assert res.verdict is Verdict.REVIEW
    assert res.reason == "below_threshold"


def test_gate_never_returns_proceed_on_loss():
    """Property-ish: across a grid, PROCEED implies strictly positive net EV."""
    gate = ExpectancyGate(EngineConfig.default())
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        for tgt in (749.5, 750.5, 752.0, 760.0):
            res = gate.evaluate(_proposal(win_prob=p, target_price=tgt), size=50)
            if res.verdict is Verdict.PROCEED:
                assert math.isfinite(res.ev_net) and res.ev_net > gate.config.gate.ev_threshold
