"""Tests for intraday.metrics.build_report (DESIGN.md §8).

Covers a real SHORT backtest run through build_report (PnL/cost identities,
SYNTHETIC provenance in the rendered report) and the empty-trades edge case
where a BacktestResult is constructed directly and must not crash the report.

SWE-marked: build_report wraps engine.performance_metrics.
"""

from __future__ import annotations

from datetime import date

import pytest

from intraday.backtest.engine import BacktestResult, IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import DataSource
from intraday.data.synthetic import SyntheticDataProvider
from intraday.metrics import MetricsReport, build_report
from intraday.signals.s3_vwap_orb import S3VwapOrb

pytestmark = pytest.mark.swe


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def short_result() -> BacktestResult:
    """A deterministic multi-day SPY backtest with the S3 VWAP-ORB strategy."""
    config = EngineConfig.default()
    provider = SyntheticDataProvider(config.data, config.session)
    bt = IntradayBacktester(config, provider, [S3VwapOrb()])
    return bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 6), "1m")


@pytest.fixture(scope="module")
def short_report(short_result: BacktestResult) -> MetricsReport:
    return build_report(short_result)


# --------------------------------------------------------------------------- #
# Backtest-driven report
# --------------------------------------------------------------------------- #
def test_report_is_synthetic_provenance(short_report: MetricsReport) -> None:
    # The provider is synthetic, so the report must record that and never look real.
    assert short_report.data_source == "synthetic"


def test_total_trades_matches_result(short_result, short_report) -> None:
    assert short_report.total_trades == len(short_result.trades)


def test_net_pnl_is_sum_of_trade_net_pnl(short_result, short_report) -> None:
    expected = sum(t.net_pnl for t in short_result.trades)
    assert short_report.net_pnl == pytest.approx(expected)


def test_gross_pnl_is_sum_of_trade_gross_pnl(short_result, short_report) -> None:
    expected = sum(t.gross_pnl for t in short_result.trades)
    assert short_report.gross_pnl == pytest.approx(expected)


def test_total_costs_is_sum_of_trade_costs(short_result, short_report) -> None:
    expected = sum(t.costs for t in short_result.trades)
    assert short_report.total_costs == pytest.approx(expected)


def test_pnl_decomposition_identity(short_report: MetricsReport) -> None:
    # NET = GROSS - COSTS, an invariant the honesty metrics must preserve.
    assert short_report.net_pnl == pytest.approx(
        short_report.gross_pnl - short_report.total_costs
    )


def test_cost_pct_of_nav_identity(short_report: MetricsReport) -> None:
    expected = short_report.total_costs / short_report.initial_capital
    assert short_report.cost_pct_of_nav == pytest.approx(expected)


def test_initial_capital_matches_paper_nav(short_result, short_report) -> None:
    assert short_report.initial_capital == pytest.approx(
        short_result.config.risk.paper_nav
    )
    assert short_report.initial_capital == pytest.approx(100_000.0)


@pytest.mark.cost_model
def test_costs_are_nonnegative(short_report: MetricsReport) -> None:
    # Costs (spread + commissions + slippage) can never be negative.
    assert short_report.total_costs >= 0.0
    assert short_report.cost_pct_of_nav >= 0.0


def test_exit_reason_counts_sum_to_total_trades(short_result, short_report) -> None:
    assert sum(short_report.exit_reason_counts.values()) == len(short_result.trades)
    for reason, cnt in short_report.exit_reason_counts.items():
        assert isinstance(reason, str)
        assert cnt >= 1


def test_n_days_matches_trading_days(short_result, short_report) -> None:
    # 2026-05-04 .. 2026-05-06 are three consecutive weekday trading days.
    assert short_report.n_days == short_result.n_days == 3


def test_render_returns_str_with_synthetic_marker(short_report: MetricsReport) -> None:
    out = short_report.render()
    assert isinstance(out, str)
    assert "SYNTHETIC" in out
    assert "synthetic" in out  # the data-source line itself
    assert "NET PnL" in out


def test_to_dict_roundtrip_keys(short_report: MetricsReport) -> None:
    d = short_report.to_dict()
    assert d["data_source"] == "synthetic"
    assert d["total_trades"] == short_report.total_trades
    assert d["net_pnl"] == pytest.approx(short_report.net_pnl)
    # to_dict must expose every reported field.
    assert set(d.keys()) == set(MetricsReport.__dataclass_fields__.keys())


def test_final_equity_consistent_with_net_pnl(short_result, short_report) -> None:
    # The backtest equity should equal NAV0 + realized net PnL.
    assert short_report.final_equity == pytest.approx(
        short_report.initial_capital + short_report.net_pnl
    )
    assert short_report.final_equity == pytest.approx(short_result.final_equity)


# --------------------------------------------------------------------------- #
# Empty-trades edge case (constructed directly)
# --------------------------------------------------------------------------- #
@pytest.fixture
def empty_result() -> BacktestResult:
    config = EngineConfig.default()
    return BacktestResult(
        config=config,
        symbols=["SPY"],
        interval="1m",
        data_source=DataSource.SYNTHETIC,
        initial_capital=100_000.0,
        final_equity=100_000.0,
        trades=[],
        fills=[],
        equity_curve=[{"date": "2026-05-04", "portfolio_value": 100_000.0}],
        signals=[],
        n_days=1,
    )


def test_build_report_no_trades_does_not_raise(empty_result: BacktestResult) -> None:
    report = build_report(empty_result)  # must not raise
    assert isinstance(report, MetricsReport)


def test_empty_report_zeros(empty_result: BacktestResult) -> None:
    report = build_report(empty_result)
    assert report.total_trades == 0
    assert report.net_pnl == pytest.approx(0.0)
    assert report.gross_pnl == pytest.approx(0.0)
    assert report.total_costs == pytest.approx(0.0)
    assert report.cost_pct_of_nav == pytest.approx(0.0)
    assert report.cost_per_trade == pytest.approx(0.0)
    assert report.cost_bps_of_notional == pytest.approx(0.0)
    assert report.expectancy_per_trade == pytest.approx(0.0)
    assert report.turnover == pytest.approx(0.0)
    assert report.exit_reason_counts == {}


def test_empty_report_renders_synthetic(empty_result: BacktestResult) -> None:
    out = build_report(empty_result).render()
    assert isinstance(out, str)
    assert "SYNTHETIC" in out
    assert "trades             : 0" in out
