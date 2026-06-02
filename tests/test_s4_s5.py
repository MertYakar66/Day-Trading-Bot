"""Tests for S4 (opening-range breakout) and S5 (VWAP momentum) — underlying-only.

Pins the breakout/continuation geometry, the no-trade guards, the honest
win-probability (fair baseline + explicit edge), and that ``edge=0`` cannot pass
the net-of-cost gate (no edge ⇒ no trade).
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.authority.gate import ExpectancyGate
from intraday.config import EngineConfig
from intraday.contracts import AssetKind, Side
from intraday.features.base import FeatureRow
from intraday.signals.s4_orb_breakout import S4OpeningRangeBreakout
from intraday.signals.s5_vwap_momentum import S5VwapMomentum

AS_OF = pd.Timestamp("2026-05-18 15:00:00", tz="UTC")
CFG = EngineConfig.default()


def _orb_row(symbol="SPY", last=100.2, oh=100.0, ol=98.0):
    return FeatureRow(symbol=symbol, as_of=AS_OF, last_price=last, orb_high=oh, orb_low=ol, meta={})


def _vwap_row(symbol="SPY", last=102.5, vwap=100.0, sigma=1.0, z=2.5):
    return FeatureRow(symbol=symbol, as_of=AS_OF, last_price=last, vwap=vwap,
                      vwap_sigma=sigma, vwap_dev_sigma=z, orb_high=101.0, meta={})


# --------------------------------------------------------------------------- #
# S4 — opening-range breakout
# --------------------------------------------------------------------------- #
def test_s4_long_on_upside_break():
    p = S4OpeningRangeBreakout().propose(_orb_row(last=100.5), config=CFG)
    assert p is not None and p.side is Side.LONG
    assert p.target_price > p.ref_price > p.stop_price
    assert p.instrument is AssetKind.STOCK


def test_s4_short_on_downside_break():
    p = S4OpeningRangeBreakout().propose(_orb_row(last=97.5), config=CFG)
    assert p is not None and p.side is Side.SHORT
    assert p.target_price < p.ref_price < p.stop_price


def test_s4_no_trade_inside_range():
    assert S4OpeningRangeBreakout().propose(_orb_row(last=99.0), config=CFG) is None


def test_s4_no_trade_on_index():
    assert S4OpeningRangeBreakout().propose(_orb_row(symbol="SPX", last=100.5), config=CFG) is None


def test_s4_winprob_is_fair_plus_edge():
    p = S4OpeningRangeBreakout(edge=0.10).propose(_orb_row(last=100.5), config=CFG)
    assert p.win_prob == pytest.approx(p.meta["p_fair"] + 0.10)
    # Fair baseline makes gross EV exactly 0.
    pf = p.meta["p_fair"]
    assert pf * (p.target_price - p.ref_price) - (1 - pf) * (p.ref_price - p.stop_price) == pytest.approx(0.0, abs=1e-6)


def test_s4_zero_edge_blocked_by_gate():
    p = S4OpeningRangeBreakout(edge=0.0).propose(_orb_row(last=100.5), config=CFG)
    res = ExpectancyGate(CFG).evaluate(p, size=10)
    assert not res.tradeable  # no edge cannot beat costs


# --------------------------------------------------------------------------- #
# S5 — VWAP momentum (mirror of S3 reversion)
# --------------------------------------------------------------------------- #
def test_s5_rides_above_vwap_long():
    p = S5VwapMomentum().propose(_vwap_row(z=2.5), config=CFG)
    assert p is not None and p.side is Side.LONG  # momentum: rides UP (S3 would short)
    assert p.target_price > p.ref_price > p.stop_price


def test_s5_rides_below_vwap_short():
    p = S5VwapMomentum().propose(_vwap_row(last=97.5, z=-2.5), config=CFG)
    assert p is not None and p.side is Side.SHORT
    assert p.target_price < p.ref_price < p.stop_price


def test_s5_no_trade_within_band():
    assert S5VwapMomentum(entry_z=2.0).propose(_vwap_row(z=1.0), config=CFG) is None


def test_s5_no_trade_on_index():
    assert S5VwapMomentum().propose(_vwap_row(symbol="SPX", z=2.5), config=CFG) is None


def test_s5_gate_ev_consistent():
    p = S5VwapMomentum(edge=0.10).propose(_vwap_row(z=2.5), config=CFG)
    res = ExpectancyGate(CFG).evaluate(p, size=10)
    expected = p.win_prob * (p.target_price - p.ref_price) * 10 - (1 - p.win_prob) * (p.ref_price - p.stop_price) * 10
    assert res.ev_gross == pytest.approx(expected)


def test_s5_is_opposite_side_of_s3():
    """S5 momentum takes the OPPOSITE side of S3 reversion on the same stretch."""
    from intraday.signals.s3_vwap_orb import S3VwapOrb
    row = _vwap_row(z=2.5)
    s3 = S3VwapOrb().propose(row, config=CFG)
    s5 = S5VwapMomentum().propose(row, config=CFG)
    assert s3.side is Side.SHORT and s5.side is Side.LONG
