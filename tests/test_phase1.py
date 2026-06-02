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
