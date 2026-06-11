"""Phase-1 integration: S1 gamma-regime runs end-to-end through the gate.

Uses a short (single-day) window because the dealer-GEX solve is the expensive
part of the option-feature path. Asserts the harness extension (chain → GEX/OFI
features per tick) is wired correctly and honestly gated.
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.data.synthetic import SyntheticDataProvider
from intraday.metrics import build_report
from intraday.signals.s1_gamma_regime import S1GammaRegime
from intraday.signals.s2_zerodte_vrp import S2ZeroDteVrp

pytestmark = [pytest.mark.integration, pytest.mark.swe]

DAY = date(2026, 5, 18)


def _run(cfg: EngineConfig):
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S1GammaRegime()])
    return bt.run(["SPY"], DAY, DAY, "1m")


def test_s1_runs_end_to_end_and_is_gated():
    cfg = EngineConfig.default()
    result = _run(cfg)
    # S1 consumes option features; on a single day it should surface some signals.
    assert len(result.signals) > 0
    # Every tradeable signal cleared the gate with positive net EV.
    for s in result.signals:
        if s["verdict"] == "PROCEED":
            assert s["ev_net"] > cfg.gate.ev_threshold
    # Accounting closes; positions are intraday-only.
    net = sum(t.net_pnl for t in result.trades)
    assert result.final_equity == pytest.approx(result.initial_capital + net, abs=1e-6)
    for t in result.trades:
        assert t.entry_ts.date() == t.exit_ts.date()
    # Report builds and is labelled synthetic.
    assert build_report(result).data_source == "synthetic"


def test_s1_signals_carry_regime_context():
    cfg = EngineConfig.default()
    result = _run(cfg)
    assert result.signals, "expected S1 to emit signals on this day"
    # The S1 strategy id is present on emitted signals.
    assert all(s["strategy_id"] == "S1_gamma_regime" for s in result.signals)


def test_s2_stands_aside_on_negative_calendar_vrp():
    """The synthetic world's IV is anchored to its INTRADAY clock, so on the
    calendar clock its true VRP is negative — S2 with default knobs must stand
    aside entirely (the engine no longer trades the +18.7-pt clock artifact)."""
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S2ZeroDteVrp()])
    result = bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 8), "1m")
    assert len(result.trades) == 0


def test_s2_defined_risk_condor_end_to_end():
    """S2 trades 0DTE defined-risk condors on SPY (fits a 100k account), settles
    binary at the close, and NEVER loses more than the defined max risk.

    The synthetic world's calendar-clock VRP is negative (see the stand-aside
    test above), so the condor MECHANICS are exercised with a permissive test
    threshold — the gate/threshold semantics live in test_s2.py."""
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S2ZeroDteVrp(vrp_threshold=-0.20)])
    result = bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 8), "1m")

    assert len(result.trades) > 0, "expected S2 to place at least one condor"
    for t in result.trades:
        assert t.exit_reason in ("settle_inside", "settle_outside")
        assert t.entry_ts.date() == t.exit_ts.date()  # intraday 0DTE
        # Defined risk: a loss is bounded well within a sane per-trade cap.
        assert t.net_pnl > -0.05 * result.initial_capital
        if t.exit_reason == "settle_inside":
            assert t.gross_pnl > 0  # kept the credit
    # Accounting closes.
    net = sum(t.net_pnl for t in result.trades)
    assert result.final_equity == pytest.approx(result.initial_capital + net, abs=1e-6)


def test_s2_sizes_to_zero_on_spx_for_small_account():
    """Honest retail constraint: a wide SPX 0DTE condor's max loss exceeds the 1%
    per-trade risk budget on a 100k account, so S2 correctly places NO SPX trade.
    (Permissive threshold so the zero comes from SIZING, not the VRP gate.)"""
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S2ZeroDteVrp(vrp_threshold=-0.20)])
    result = bt.run(["SPX"], date(2026, 5, 4), date(2026, 5, 6), "1m")
    assert len(result.trades) == 0
