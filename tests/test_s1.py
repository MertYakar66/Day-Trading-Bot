"""Unit tests for S1 — the gamma-regime strategy (DESIGN.md §5 S1).

Pins S1's two-mode decision geometry against hand-built feature rows: fade in
long-gamma (mean-revert toward VWAP on exhausting OFI), ride in short-gamma (trend
on a flip break confirmed by OFI), stand aside near the flip / neutral, and the
honest fair-baseline + edge win probability.
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.contracts import AssetKind, GammaRegime, Side
from intraday.features.base import FeatureRow
from intraday.signals.s1_gamma_regime import S1GammaRegime

pytestmark = pytest.mark.gate

AS_OF = pd.Timestamp("2026-05-18 15:00:00", tz="UTC")


def _row(
    *,
    symbol="SPY",
    last_price=500.0,
    vwap=498.0,
    vwap_sigma=0.5,
    vwap_dev_sigma=2.0,
    ofi=0.5,
    gamma_regime=GammaRegime.LONG_GAMMA,
    flip_level=499.0,
    nearest_call_wall=505.0,
    nearest_put_wall=495.0,
    gex_total=1.0e8,
) -> FeatureRow:
    return FeatureRow(
        symbol=symbol, as_of=AS_OF, last_price=last_price, vwap=vwap,
        vwap_sigma=vwap_sigma, vwap_dev_sigma=vwap_dev_sigma, ofi=ofi,
        gamma_regime=gamma_regime, flip_level=flip_level,
        nearest_call_wall=nearest_call_wall, nearest_put_wall=nearest_put_wall,
        gex_total=gex_total, meta={},
    )


def test_capabilities_flags():
    s1 = S1GammaRegime()
    assert s1.needs_options is True
    assert s1.needs_rv is False


# -- long-gamma: fade toward VWAP on exhaustion ----------------------------- #


def test_long_gamma_fades_short_above(config):
    s1 = S1GammaRegime(entry_z=1.5)
    fr = _row(last_price=500.0, vwap=498.0, vwap_sigma=0.5, vwap_dev_sigma=2.0,
              ofi=0.5, gamma_regime=GammaRegime.LONG_GAMMA)
    p = s1.propose(fr, config=config)
    assert p is not None and p.side is Side.SHORT
    assert p.target_price == pytest.approx(498.0)        # back to VWAP
    assert p.stop_price == pytest.approx(500.0 + 1.0 * 0.5)
    assert p.instrument is AssetKind.STOCK


def test_long_gamma_fades_long_below(config):
    s1 = S1GammaRegime(entry_z=1.5)
    fr = _row(last_price=496.0, vwap=498.0, vwap_dev_sigma=-2.0, ofi=-0.5,
              gamma_regime=GammaRegime.LONG_GAMMA)
    p = s1.propose(fr, config=config)
    assert p is not None and p.side is Side.LONG
    assert p.target_price == pytest.approx(498.0)
    assert p.stop_price == pytest.approx(496.0 - 1.0 * 0.5)


def test_long_gamma_requires_ofi_confirmation(config):
    """Stretched but flow not exhausting (OFI opposes) → no fade."""
    s1 = S1GammaRegime(entry_z=1.5)
    assert s1.propose(_row(vwap_dev_sigma=2.0, ofi=-0.1, gamma_regime=GammaRegime.LONG_GAMMA), config=config) is None


# -- short-gamma: ride the flip break --------------------------------------- #


def test_short_gamma_rides_long_break(config):
    s1 = S1GammaRegime()
    fr = _row(last_price=500.0, flip_level=499.0, ofi=0.5,
              gamma_regime=GammaRegime.SHORT_GAMMA, nearest_call_wall=505.0)
    p = s1.propose(fr, config=config)
    assert p is not None and p.side is Side.LONG
    assert p.target_price == pytest.approx(505.0)       # next wall up
    assert p.stop_price == pytest.approx(500.0 - 1.0 * 0.5)


def test_short_gamma_rides_short_break(config):
    s1 = S1GammaRegime()
    fr = _row(last_price=498.0, flip_level=499.0, ofi=-0.5,
              gamma_regime=GammaRegime.SHORT_GAMMA, nearest_put_wall=495.0)
    p = s1.propose(fr, config=config)
    assert p is not None and p.side is Side.SHORT
    assert p.target_price == pytest.approx(495.0)
    assert p.stop_price == pytest.approx(498.0 + 1.0 * 0.5)


def test_short_gamma_wall_fallback_to_sigma_target(config):
    """No usable wall → target falls back to target_k·sigma in the trend dir."""
    s1 = S1GammaRegime(target_k=2.0)
    fr = _row(last_price=500.0, flip_level=499.0, ofi=0.5, vwap_sigma=0.5,
              gamma_regime=GammaRegime.SHORT_GAMMA, nearest_call_wall=None)
    p = s1.propose(fr, config=config)
    assert p is not None and p.side is Side.LONG
    assert p.target_price == pytest.approx(500.0 + 2.0 * 0.5)


# -- stand-aside regimes ---------------------------------------------------- #


@pytest.mark.parametrize("regime", [GammaRegime.NEAR_FLIP, GammaRegime.NEUTRAL])
def test_stand_aside_when_unstable(config, regime):
    s1 = S1GammaRegime()
    assert s1.propose(_row(gamma_regime=regime, vwap_dev_sigma=3.0, ofi=0.9), config=config) is None


def test_none_when_missing_inputs(config):
    s1 = S1GammaRegime()
    assert s1.propose(_row(gamma_regime=None), config=config) is None
    assert s1.propose(_row(ofi=None), config=config) is None
    assert s1.propose(_row(vwap_sigma=None), config=config) is None


def test_none_for_index_symbol(config):
    s1 = S1GammaRegime()
    assert s1.propose(_row(symbol="SPX", gamma_regime=GammaRegime.SHORT_GAMMA, ofi=0.9), config=config) is None


# -- honest win probability ------------------------------------------------- #


@pytest.mark.swe
def test_edge_zero_is_fair_and_gate_blocks(config):
    from intraday.authority.gate import ExpectancyGate
    from intraday.contracts import Verdict

    s1 = S1GammaRegime(edge=0.0)
    p = s1.propose(_row(gamma_regime=GammaRegime.LONG_GAMMA, vwap_dev_sigma=2.0, ofi=0.5), config=config)
    assert p is not None
    res = ExpectancyGate(config).evaluate(p, size=100)
    assert res.ev_gross == pytest.approx(0.0, abs=1e-6)
    assert res.verdict is Verdict.BLOCKED
