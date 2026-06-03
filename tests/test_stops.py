"""Tests for the per-trade stop/target primitive (intraday.risk.stops).

``sigma_stop_target`` is exported public risk math (it is in ``risk.__all__``) with
branchy LONG/SHORT + explicit-target + ``target_k or stop_k`` fallback logic. A sign
error or a swapped LONG/SHORT branch in a sizing primitive would otherwise pass CI
silently — these pin the direction (stop always adverse, target always favorable).
"""

from __future__ import annotations

import pytest

from intraday.contracts import Side
from intraday.risk.stops import sigma_stop_target

REF, SIGMA = 100.0, 2.0


def test_long_stop_below_target_above():
    stop, tgt = sigma_stop_target(REF, SIGMA, Side.LONG, stop_k=1.5)
    assert stop == pytest.approx(REF - 1.5 * SIGMA)   # adverse side = below entry
    assert tgt == pytest.approx(REF + 1.5 * SIGMA)    # favorable = above (target_k -> stop_k)
    assert stop < REF < tgt


def test_short_stop_above_target_below():
    stop, tgt = sigma_stop_target(REF, SIGMA, Side.SHORT, stop_k=1.5)
    assert stop == pytest.approx(REF + 1.5 * SIGMA)   # adverse side = above entry
    assert tgt == pytest.approx(REF - 1.5 * SIGMA)    # favorable = below
    assert tgt < REF < stop


def test_explicit_target_overrides_sigma_target_both_sides():
    _, tgt_long = sigma_stop_target(REF, SIGMA, Side.LONG, stop_k=1.0, target_price=123.0)
    _, tgt_short = sigma_stop_target(REF, SIGMA, Side.SHORT, stop_k=1.0, target_price=88.0)
    assert tgt_long == 123.0 and tgt_short == 88.0   # explicit price wins on both sides


def test_target_k_used_when_given_else_falls_back_to_stop_k():
    _, tgt = sigma_stop_target(REF, SIGMA, Side.LONG, stop_k=1.0, target_k=3.0)
    assert tgt == pytest.approx(REF + 3.0 * SIGMA)    # target_k drives the target
    _, tgt2 = sigma_stop_target(REF, SIGMA, Side.LONG, stop_k=2.0)
    assert tgt2 == pytest.approx(REF + 2.0 * SIGMA)   # no target_k -> stop_k


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("ref,sigma,k", [(50.0, 1.0, 0.5), (200.0, 5.0, 2.0), (10.0, 0.25, 1.0)])
def test_stop_adverse_target_favorable_property(side, ref, sigma, k):
    """For any ref/sigma/k and either side, the stop sits on the adverse side of
    entry and the target on the favorable side."""
    stop, tgt = sigma_stop_target(ref, sigma, side, stop_k=k)
    if side is Side.LONG:
        assert stop < ref < tgt
    else:
        assert tgt < ref < stop
