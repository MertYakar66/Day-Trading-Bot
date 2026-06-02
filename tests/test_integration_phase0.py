"""Phase-0 end-to-end integration tests (TASKS.md Phase-0 exit criterion).

Replays SPX/SPY/QQQ through data → features → S3 → sizing → gate → reviewers →
fills → metrics, and asserts the harness is correct and honest:
- it produces a cost-correct, gated equity curve + full metrics report;
- accounting closes (final equity == NAV + Σ net PnL);
- positions are intraday-only (no overnight);
- every tradeable (PROCEED) signal had net EV above threshold;
- it CAPTURES edge when the (synthetic) world is mean-reverting, and
  MANUFACTURES NONE when the world is a random walk (gross ≈ 0) — the strongest
  no-look-ahead / no-fabrication check.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import Verdict
from intraday.data.synthetic import SyntheticDataProvider
from intraday.metrics import build_report
from intraday.signals.s3_vwap_orb import S3VwapOrb

pytestmark = pytest.mark.integration

START = date(2026, 5, 4)
END = date(2026, 5, 15)  # two synthetic weeks (fast, deterministic)


def _run(config: EngineConfig):
    provider = SyntheticDataProvider(config.data, config.session)
    bt = IntradayBacktester(config, provider, [S3VwapOrb()])
    return bt.run(["SPX", "SPY", "QQQ"], START, END, "1m")


def test_phase0_runs_end_to_end_and_reports():
    cfg = EngineConfig.default()
    result = _run(cfg)
    assert result.n_days == len(SyntheticDataProvider(cfg.data, cfg.session).trading_days(START, END))
    assert len(result.equity_curve) == result.n_days
    assert all({"date", "portfolio_value"} <= set(row) for row in result.equity_curve)
    assert len(result.trades) > 0

    report = build_report(result)
    assert report.data_source == "synthetic"
    assert report.total_trades == len(result.trades)
    # Report is NET of costs; rendering must not raise.
    assert isinstance(report.render(), str)


def test_accounting_closes():
    cfg = EngineConfig.default()
    result = _run(cfg)
    net = sum(t.net_pnl for t in result.trades)
    assert result.final_equity == pytest.approx(result.initial_capital + net, abs=1e-6)
    # Per trade: net == gross − costs.
    for t in result.trades:
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs)


def test_positions_are_intraday_only():
    cfg = EngineConfig.default()
    result = _run(cfg)
    for t in result.trades:
        assert t.entry_ts.date() == t.exit_ts.date(), "no overnight positions allowed"


def test_proceed_signals_have_positive_net_ev():
    cfg = EngineConfig.default()
    result = _run(cfg)
    proceeded = [s for s in result.signals if s["verdict"] == "PROCEED"]
    assert proceeded, "expected at least one tradeable signal"
    for s in proceeded:
        assert s["ev_net"] > cfg.gate.ev_threshold


def test_deterministic():
    cfg = EngineConfig.default()
    r1, r2 = _run(cfg), _run(cfg)
    assert len(r1.trades) == len(r2.trades)
    assert sum(t.net_pnl for t in r1.trades) == pytest.approx(sum(t.net_pnl for t in r2.trades))


def test_captures_edge_when_present_not_when_absent():
    """Random-walk world → gross ≈ 0 (no fabricated edge). Strong-reversion world
    → gross > 0 (engine captures real signal). This is the key honesty check."""
    rw_cfg = EngineConfig.default()  # random-walk dominant defaults
    rw = _run(rw_cfg)
    gross_rw = sum(t.gross_pnl for t in rw.trades)
    notional_rw = sum(t.size * t.entry_price * t.instrument.multiplier for t in rw.trades)
    # Gross PnL is a tiny fraction of traded notional on a random walk.
    assert abs(gross_rw) / max(notional_rw, 1.0) < 1e-3

    strong_cfg = replace(rw_cfg, data=replace(rw_cfg.data, dev_weight=4.0, ou_phi=0.99))
    strong = _run(strong_cfg)
    gross_strong = sum(t.gross_pnl for t in strong.trades)
    assert gross_strong > 0
    assert gross_strong > gross_rw
