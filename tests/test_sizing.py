"""Tests for intraday.risk.sizing.kelly_size (fractional-Kelly + hard caps).

These exercise the SWE risk_manager path (``engine.risk_manager.calculate_kelly_fraction``)
through ``kelly_size``, so the module is marked ``swe``. All inputs are constructed
directly from ``intraday.contracts.SignalProposal`` — fully deterministic, network-free.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from intraday.config import RiskConfig
from intraday.contracts import AssetKind, Side, SignalProposal
from intraday.risk.sizing import SizingResult, kelly_size

pytestmark = pytest.mark.swe

TS = pd.Timestamp("2026-05-18 14:00", tz="UTC")


def _proposal(
    *,
    side: Side = Side.LONG,
    instrument: AssetKind = AssetKind.STOCK,
    ref_price: float,
    target_price: float,
    stop_price: float,
    win_prob: float,
) -> SignalProposal:
    return SignalProposal(
        strategy_id="s3",
        symbol="SPY",
        ts=TS,
        side=side,
        instrument=instrument,
        ref_price=ref_price,
        target_price=target_price,
        stop_price=stop_price,
        win_prob=win_prob,
        spread=0.01,
    )


def _notional_cap_units(ref_price: float, mult: int, risk: RiskConfig) -> int:
    """Max units allowed purely by the notional cap (floored, since size is int)."""
    return int(math.floor((risk.max_position_notional_pct * risk.paper_nav) / (ref_price * mult)))


def test_positive_edge_sized_and_capped_by_notional():
    # win_prob 0.7, target far (+10), stop near (-1) -> strong positive edge.
    # Kelly: b=10, f_full=(0.7*10-0.3)/10=0.67, half-Kelly -> 0.335, capped at 0.25.
    risk = RiskConfig()
    prop = _proposal(ref_price=100.0, target_price=110.0, stop_price=99.0, win_prob=0.7)
    res = kelly_size(prop, risk)

    assert isinstance(res, SizingResult)
    assert res.reason == "ok"
    assert res.size > 0
    # Kelly fraction is the 25% hard cap from the SWE risk_manager.
    assert res.kelly_fraction == pytest.approx(0.25)
    # Risk budget = min(f, max_risk_per_trade_pct) * NAV = 0.01 * 100000 = 1000.
    assert res.risk_dollars == pytest.approx(0.01 * 100_000)

    # Here the notional cap binds (200) below the risk-implied size (1000).
    cap = _notional_cap_units(prop.ref_price, prop.instrument.multiplier, risk)
    assert cap == 200
    assert res.size == cap
    # Invariant: never exceed the notional cap.
    assert res.size <= cap
    # And never exceed the absolute max_size guard.
    assert res.size <= risk.max_size


def test_size_never_exceeds_notional_cap_invariant():
    # Risk-implied size below the notional cap: risk_per_unit=2 -> 1000/2 = 500 units;
    # notional cap = 0.20*100000/20 = 1000. Risk binds, well under both caps.
    risk = RiskConfig()
    prop = _proposal(ref_price=20.0, target_price=30.0, stop_price=18.0, win_prob=0.7)
    res = kelly_size(prop, risk)

    assert res.reason == "ok"
    cap = _notional_cap_units(prop.ref_price, prop.instrument.multiplier, risk)
    assert cap == 1000
    # size_by_risk = risk_budget / (loss_per_unit*mult) = 1000 / 2 = 500
    assert res.size == 500
    assert res.size <= cap
    assert res.size < cap  # risk constraint genuinely binds here
    assert res.size <= risk.max_size


def test_no_stop_distance_returns_zero():
    # stop_price == ref_price -> risk_per_unit == 0 -> "no_stop_distance".
    # Edge stays positive (target far), so Kelly>0 and we reach the stop check.
    prop = _proposal(ref_price=100.0, target_price=110.0, stop_price=100.0, win_prob=0.7)
    res = kelly_size(prop)

    assert res.size == 0
    assert res.reason == "no_stop_distance"
    assert prop.risk_per_unit == pytest.approx(0.0)
    # Kelly was still computed positive before the stop-distance guard tripped.
    assert res.kelly_fraction > 0.0


def test_poor_odds_returns_zero_size():
    # win_prob 0.2, reward (0.5) < risk (1.0): expected value clearly negative.
    # Kelly f* = (p*b - q)/b with b=0.5, p=0.2 -> (0.1-0.8)/0.5 < 0 -> floored to 0.
    prop = _proposal(ref_price=100.0, target_price=100.5, stop_price=99.0, win_prob=0.2)
    res = kelly_size(prop)

    assert res.size == 0
    assert res.kelly_fraction == pytest.approx(0.0)
    assert res.risk_dollars == pytest.approx(0.0)
    assert res.reason == "kelly<=0"


def test_option_multiplier_in_notional_cap():
    # OPTION instrument: contract multiplier is 100, so notional cap uses ref*100.
    # ref=5 -> notional cap = 0.20*100000/(5*100) = 40 contracts.
    # risk_per_unit=1 -> size_by_risk = 1000/(1*100) = 10 -> risk binds at 10.
    risk = RiskConfig()
    prop = _proposal(
        instrument=AssetKind.OPTION,
        ref_price=5.0,
        target_price=10.0,
        stop_price=4.0,
        win_prob=0.7,
    )
    assert prop.instrument.multiplier == 100
    res = kelly_size(prop, risk)

    assert res.reason == "ok"
    cap = _notional_cap_units(prop.ref_price, prop.instrument.multiplier, risk)
    assert cap == 40
    assert res.size == 10
    assert res.size <= cap


def test_short_side_positive_edge_sizes_like_long():
    # SHORT proposal with the same |reward|/|risk| geometry sizes identically:
    # sizing is direction-agnostic (uses absolute per-unit reward/risk).
    risk = RiskConfig()
    short = _proposal(
        side=Side.SHORT, ref_price=20.0, target_price=10.0, stop_price=22.0, win_prob=0.7
    )
    res = kelly_size(short, risk)

    assert res.reason == "ok"
    assert res.size > 0
    # reward=10, risk=2 -> same b as the long invariant test -> Kelly capped at 0.25.
    assert res.kelly_fraction == pytest.approx(0.25)
    # size_by_risk = 1000/2 = 500, notional cap = 1000 -> risk binds.
    assert res.size == 500
    assert res.size <= _notional_cap_units(short.ref_price, short.instrument.multiplier, risk)


def test_default_risk_config_used_when_none():
    # Passing risk=None must fall back to RiskConfig() defaults (NAV 100k etc.).
    prop = _proposal(ref_price=100.0, target_price=110.0, stop_price=99.0, win_prob=0.7)
    res_default = kelly_size(prop, None)
    res_explicit = kelly_size(prop, RiskConfig())
    assert res_default == res_explicit
    assert res_default.risk_dollars == pytest.approx(0.01 * 100_000)
