"""Unit tests for S2 — 0DTE VRP defined-risk iron condor (DESIGN.md §5 S2).

Pins the entry gating (VRP rich + long gamma), the defined-risk economics
(credit / capped max loss from BSM), the honest win probability (fair baseline +
VRP edge), and the explicit 4-leg round-trip cost. SWE-marked (uses option_pricer).
"""

from __future__ import annotations

import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.contracts import AssetKind, GammaRegime, Side
from intraday.features.base import FeatureRow
from intraday.signals.s2_zerodte_vrp import S2ZeroDteVrp

pytestmark = [pytest.mark.gate, pytest.mark.swe]

# Early-afternoon decision time → plenty of time to the 20:00 UTC (16:00 ET) close.
AS_OF = pd.Timestamp("2026-05-18 15:00:00", tz="UTC")


def _row(
    *,
    symbol="SPX",
    last_price=6000.0,
    atm_iv=0.15,
    rv=0.10,
    gamma_regime=GammaRegime.LONG_GAMMA,
    gex_total=1.0e8,  # positive net gamma (vol-suppressing) by default
) -> FeatureRow:
    return FeatureRow(
        symbol=symbol, as_of=AS_OF, last_price=last_price, atm_iv=atm_iv, rv=rv,
        vrp=(atm_iv - rv), gamma_regime=gamma_regime, gex_total=gex_total, meta={},
    )


def test_capability_flags():
    s2 = S2ZeroDteVrp()
    assert s2.needs_options and s2.needs_rv


def test_proposes_defined_risk_condor_when_vrp_rich_and_long_gamma():
    s2 = S2ZeroDteVrp()
    p = s2.propose(_row(atm_iv=0.16, rv=0.10), config=EngineConfig.default())
    assert p is not None
    assert p.is_structured
    assert p.instrument is AssetKind.OPTION
    assert p.side is Side.SHORT
    assert p.win_amount > 0 and p.loss_amount > 0
    assert p.cost_override is not None and p.cost_override.total > 0
    assert {"center", "short_width"} <= set(p.meta)


def test_no_trade_when_vrp_below_threshold():
    s2 = S2ZeroDteVrp(vrp_threshold=0.05)
    # vrp = 0.02 < 0.05 → stand aside.
    assert s2.propose(_row(atm_iv=0.12, rv=0.10), config=EngineConfig.default()) is None


@pytest.mark.parametrize("gex", [-1.0e8, 0.0, None])
def test_no_trade_when_gamma_not_positive(gex):
    """S2 sells premium only when net dealer gamma is POSITIVE (vol-suppressing)."""
    s2 = S2ZeroDteVrp()
    assert s2.propose(_row(gex_total=gex), config=EngineConfig.default()) is None


def test_no_trade_too_close_to_session_close():
    s2 = S2ZeroDteVrp(min_seconds_to_close=1800)
    late = FeatureRow(symbol="SPX", as_of=pd.Timestamp("2026-05-18 19:50:00", tz="UTC"),
                      last_price=6000.0, atm_iv=0.16, rv=0.10, vrp=0.06,
                      gamma_regime=GammaRegime.LONG_GAMMA, meta={})
    assert s2.propose(late, config=EngineConfig.default()) is None


def test_win_prob_is_fair_baseline_plus_vrp_edge():
    """At p_fair the structure is a fair bet (gross EV 0); the edge is positive
    because realized < implied vol."""
    s2 = S2ZeroDteVrp()
    p = s2.propose(_row(atm_iv=0.18, rv=0.10), config=EngineConfig.default())
    assert p is not None
    p_fair = p.meta["p_fair"]
    edge = p.meta["vrp_edge"]
    # Fair baseline makes gross EV exactly zero.
    fair_ev = p_fair * p.win_amount - (1 - p_fair) * p.loss_amount
    assert fair_ev == pytest.approx(0.0, abs=1e-6)
    # VRP rich (iv >> rv) → positive edge → win_prob above fair.
    assert edge > 0
    assert p.win_prob == pytest.approx(min(0.99, max(0.01, p_fair + edge)))


def test_cost_override_accounts_four_legs_round_trip():
    """Commission = 4 legs × 2 sides × $0.65 = $5.20 per spread (size 1)."""
    s2 = S2ZeroDteVrp()
    p = s2.propose(_row(atm_iv=0.16, rv=0.10), config=EngineConfig.default())
    assert p is not None
    assert p.cost_override.commission == pytest.approx(4 * 2 * 0.65)


def test_gate_ev_consistent_with_proposal():
    from intraday.authority.gate import ExpectancyGate

    cfg = EngineConfig.default()
    s2 = S2ZeroDteVrp()
    p = s2.propose(_row(atm_iv=0.18, rv=0.10), config=cfg)
    assert p is not None
    res = ExpectancyGate(cfg).evaluate(p, size=1)
    expected_gross = p.win_prob * p.win_amount - (1 - p.win_prob) * p.loss_amount
    assert res.ev_gross == pytest.approx(expected_gross)
    assert res.ev_net == pytest.approx(res.ev_gross - res.cost.total)
