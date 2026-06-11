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
    rv_calendar=None,
    gamma_regime=GammaRegime.LONG_GAMMA,
    gex_total=1.0e8,  # positive net gamma (vol-suppressing) by default
) -> FeatureRow:
    # The gate VRP compares like clocks: atm_iv - rv_calendar. The intraday rv
    # is the separate sigma_real leg. Defaulting the calendar leg to the same
    # number keeps each test's vrp value explicit and unchanged.
    rv_cal = rv_calendar if rv_calendar is not None else rv
    return FeatureRow(
        symbol=symbol, as_of=AS_OF, last_price=last_price, atm_iv=atm_iv, rv=rv,
        rv_calendar=rv_cal, vrp=(atm_iv - rv_cal),
        gamma_regime=gamma_regime, gex_total=gex_total, meta={},
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
                      last_price=6000.0, atm_iv=0.16, rv=0.10, rv_calendar=0.10,
                      vrp=0.06, gamma_regime=GammaRegime.LONG_GAMMA, meta={})
    assert s2.propose(late, config=EngineConfig.default()) is None


def test_win_prob_is_realized_inside_probability():
    """win_prob is the realized-vol P(inside short strikes) — the honest binary
    win probability, NOT the inflated geometry break-even baseline."""
    s2 = S2ZeroDteVrp()
    p = s2.propose(_row(atm_iv=0.18, rv=0.10), config=EngineConfig.default())
    assert p is not None
    assert p.win_prob == pytest.approx(p.meta["p_real_inside"])
    # The geometry break-even (kept for transparency) makes gross EV exactly 0; we
    # deliberately do NOT use it as win_prob (that would overstate EV).
    p_fair = p.meta["p_fair"]
    fair_ev = p_fair * p.win_amount - (1 - p_fair) * p.loss_amount
    assert fair_ev == pytest.approx(0.0, abs=1e-6)
    # VRP rich (iv >> rv) → realized inside-prob exceeds implied → positive edge.
    assert p.meta["vrp_edge"] > 0
    assert p.win_prob > p.meta["p_impl_inside"]


def test_no_edge_binary_is_negative_ev():
    """Sanity: with realized == implied vol the binary condor has NEGATIVE gross EV
    (it overstates loss vs a real condor), so win_prob=p_inside must yield EV<0 —
    the gate would refuse it. (Constructed directly since propose requires VRP>0.)"""
    from intraday.signals.s2_zerodte_vrp import _p_inside

    # k_short = 1 sigma: p_inside ≈ 0.683; condor at fair credit loses on the binary.
    sw = 60.0
    p_inside = _p_inside(sw, sw)  # realized == implied (sigma_move == short_width)
    # A representative fair-ish condor: credit small, max loss large (1σ short).
    credit, max_loss = 9.0 * 100, 51.0 * 100
    ev = p_inside * credit - (1 - p_inside) * max_loss
    assert ev < 0


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


def test_no_trade_when_realized_vol_degenerate():
    """A non-positive realized-vol estimate (flat/degenerate RV window) must NOT
    produce an over-confident trade: P(inside) would collapse to a certainty and
    inflate win_prob to the 0.99 cap on no real evidence. S2 stands aside instead."""
    s2 = S2ZeroDteVrp()
    # rv = 0 => VRP = atm_iv (rich) and gamma positive, so it clears the entry gate,
    # but sigma_real == 0 must trip the honesty guard.
    assert s2.propose(_row(atm_iv=0.16, rv=0.0), config=EngineConfig.default()) is None


# --------------------------------------------------------------------------- #
# Calendar-clock gate regressions (the 2026-06-11 VRP redefinition)
# --------------------------------------------------------------------------- #
def test_zero_true_vrp_world_yields_zero_proposals():
    """THE decisive regression: in a world with zero true VRP (IV equals the
    correctly-clocked calendar RV) S2 must propose nothing. Under the old
    intraday-clock gate this world showed a fat positive "VRP" (the measured
    ~ +18.7 vol-pt artifact) and S2 would have traded it."""
    s2 = S2ZeroDteVrp()
    fr = _row(atm_iv=0.20, rv=0.05, rv_calendar=0.20)   # vrp == 0.0 exactly
    assert fr.vrp == pytest.approx(0.0)
    assert s2.propose(fr, config=EngineConfig.default()) is None
    # The OLD definition would have seen +0.15 here - far past any threshold.
    assert fr.atm_iv - fr.rv == pytest.approx(0.15)


def test_unknowable_calendar_leg_stands_aside():
    """No calendar RV (history shallower than the window) -> vrp None -> stand
    aside, regardless of how rich IV looks against the intraday clock."""
    s2 = S2ZeroDteVrp(vrp_threshold=-1.0)               # maximally permissive
    fr = FeatureRow(
        symbol="SPX", as_of=AS_OF, last_price=6000.0, atm_iv=0.30, rv=0.05,
        rv_calendar=None, vrp=None, gamma_regime=GammaRegime.LONG_GAMMA,
        gex_total=1.0e8, meta={},
    )
    assert s2.propose(fr, config=EngineConfig.default()) is None


def test_sigma_real_still_uses_the_intraday_clock():
    """The win-probability projects the remaining move-to-close from fr.rv
    (intraday clock - no overnight variance can realize before a 0DTE close);
    the calendar leg must change the GATE, not the projection."""
    s2 = S2ZeroDteVrp()
    cfg = EngineConfig.default()
    a = s2.propose(_row(atm_iv=0.16, rv=0.10, rv_calendar=0.10), config=cfg)
    b = s2.propose(_row(atm_iv=0.16, rv=0.10, rv_calendar=0.12), config=cfg)
    assert a is not None and b is not None
    # Same intraday rv -> identical structure economics and win probability;
    # only the gate quantity (meta vrp tag, if any) may differ.
    assert b.win_prob == pytest.approx(a.win_prob)
    assert b.win_amount == pytest.approx(a.win_amount)
    assert b.loss_amount == pytest.approx(a.loss_amount)


def test_engine_none_calendar_rv_blocks_s2_even_when_permissive(monkeypatch):
    """Engine path: when trailing history cannot support the calendar leg the
    per-day rv_cal is None and S2 must never trade - even at a permissive
    threshold (None propagates, it does not default)."""
    from datetime import date

    from intraday.backtest import engine as eng
    from intraday.backtest.engine import IntradayBacktester
    from intraday.data.synthetic import SyntheticDataProvider

    monkeypatch.setattr(eng, "trailing_session_closes", lambda *a, **k: None)
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S2ZeroDteVrp(vrp_threshold=-1.0)])
    result = bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 5), "1m")
    assert len(result.trades) == 0
    # Stronger than the trade count: propose() itself returned None at every
    # tick (vrp None never reaches the gate), so NO signal was even recorded.
    assert len(result.signals) == 0


def test_engine_mixed_calendar_availability_is_per_symbol(monkeypatch):
    """Two-symbol run where only one symbol's calendar leg is computable: the
    None symbol must stand aside while the other still reaches the gate —
    rv_cal is per-symbol state, never shared."""
    from datetime import date

    from intraday.backtest import engine as eng
    from intraday.backtest.engine import IntradayBacktester
    from intraday.data.synthetic import SyntheticDataProvider
    from intraday.features.realized_vol import trailing_session_closes as real_tsc

    def per_symbol(provider, symbol, day, **kwargs):
        if symbol == "QQQ":
            return None
        return real_tsc(provider, symbol, day, **kwargs)

    monkeypatch.setattr(eng, "trailing_session_closes", per_symbol)
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S2ZeroDteVrp(vrp_threshold=-1.0)])
    result = bt.run(["SPY", "QQQ"], date(2026, 5, 4), date(2026, 5, 5), "1m")
    assert all(s["symbol"] == "SPY" for s in result.signals)
    assert len(result.signals) > 0          # SPY's permissive gate did engage
    assert all(t.symbol == "SPY" for t in result.trades)
