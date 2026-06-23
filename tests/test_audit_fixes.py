"""Regression tests for the 2026-06-10 audit fixes.

Covers four confirmed findings (see CHANGELOG):
1. Risk metrics were computed on an equity curve missing the inception point, so
   the first day's PnL was dropped from Sharpe/vol and max drawdown was censored
   against a day-1-close peak (a 10% day-1 loss reported max_drawdown == 0.0).
2. The dashboard's underwater curve shared the same censoring.
3. The persisted fills ledger double-counted entry costs on CLOSE records (and
   the EOD safety-flatten path appended no CLOSE fill at all).
4. ``gamma_structure_at``/``atm_iv_at`` read the whole chain snapshot, blending
   expiries on real multi-expiry captures (measured at ~3,000x GEX distortion).

Marked ``swe``: build_report and the gamma spine wrap SWE quant modules.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from intraday.backtest.engine import BacktestResult, IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import AssetKind, DataSource, OptionChainSeries, Side, Trade
from intraday.features.gex import gamma_structure_at
from intraday.features.vrp import atm_iv_at
from intraday.metrics import build_report
from intraday.report import dashboard as dash
from intraday.signals.s3_vwap_orb import S3VwapOrb
from intraday.timeutils import session_bounds_utc

pytestmark = pytest.mark.swe


# --------------------------------------------------------------------------- #
# 1) Metrics include the inception point
# --------------------------------------------------------------------------- #
@pytest.fixture
def day1_loss_result() -> BacktestResult:
    """A run that loses 10% on day 1 and grinds up on day 2 — the exact shape
    that previously reported max_drawdown == 0.0 and a wildly positive Sharpe."""
    config = EngineConfig.default()
    entry = pd.Timestamp("2026-05-04 14:30", tz="UTC")
    exit_ = pd.Timestamp("2026-05-04 19:00", tz="UTC")
    trade = Trade(
        symbol="SPY", strategy_id="s3", side=Side.LONG, size=100,
        instrument=AssetKind.STOCK, entry_ts=entry, exit_ts=exit_,
        entry_price=500.0, exit_price=400.0, gross_pnl=-10_000.0, costs=0.0,
        net_pnl=-10_000.0, exit_reason="stop",
    )
    return BacktestResult(
        config=config, symbols=["SPY"], interval="1m",
        data_source=DataSource.SYNTHETIC, initial_capital=100_000.0,
        final_equity=91_000.0, trades=[trade], fills=[],
        equity_curve=[
            {"date": "2026-05-04", "portfolio_value": 90_000.0},
            {"date": "2026-05-05", "portfolio_value": 91_000.0},
        ],
        signals=[], n_days=2,
    )


def test_day1_loss_is_visible_to_max_drawdown(day1_loss_result):
    report = build_report(day1_loss_result)
    # Peak starts at initial capital, so the 10% inception loss IS the drawdown.
    assert report.max_drawdown == pytest.approx(0.10, abs=1e-9)


def test_day1_loss_yields_negative_risk_metrics(day1_loss_result):
    report = build_report(day1_loss_result)
    assert report.total_return == pytest.approx(-0.09)
    assert report.sharpe_ratio < 0.0     # day-1 return (-10%) is now in the series
    assert report.annualized_return < 0.0
    assert report.calmar_ratio < 0.0


def test_annualization_uses_true_session_count(day1_loss_result):
    report = build_report(day1_loss_result)
    # 2 sessions, not 3 rows (the prepended inception row is day 0, not a session).
    expected = ((1.0 + report.total_return) ** (252.0 / 2.0)) - 1.0
    assert report.annualized_return == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# 2) Dashboard underwater curve seeds its peak at inception capital
# --------------------------------------------------------------------------- #
def test_drawdown_curve_seeded_with_initial_capital():
    dd = dash._drawdown_curve([90_000.0, 95_000.0], initial=100_000.0)
    assert dd[0] == pytest.approx(-0.10)
    assert dd[1] == pytest.approx(-0.05)


def test_drawdown_curve_unseeded_keeps_legacy_shape():
    dd = dash._drawdown_curve([90_000.0, 95_000.0])
    assert dd[0] == pytest.approx(0.0)   # first point is its own peak
    assert dd[1] == pytest.approx(0.0)


def test_one_day_run_risk_stats_are_finite(day1_loss_result):
    """A 1-day run has exactly one return once inception is included; its
    std(ddof=1) is NaN, which previously leaked into vol/Sharpe/Sortino."""
    import dataclasses
    import math

    one_day = dataclasses.replace(
        day1_loss_result,
        final_equity=90_000.0,
        equity_curve=[{"date": "2026-05-04", "portfolio_value": 90_000.0}],
        n_days=1,
    )
    report = build_report(one_day)
    assert report.volatility == 0.0
    assert report.sharpe_ratio == 0.0
    assert report.sortino_ratio == 0.0
    for field in ("max_drawdown", "annualized_return", "calmar_ratio", "total_return"):
        assert math.isfinite(getattr(report, field))
    assert "nan" not in report.render().lower()


# --------------------------------------------------------------------------- #
# 3) Fills ledger: per-leg costs, every OPEN has a CLOSE
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def s3_run() -> BacktestResult:
    config = EngineConfig.default()
    from intraday.data.synthetic import SyntheticDataProvider

    provider = SyntheticDataProvider(config.data, config.session)
    bt = IntradayBacktester(config, provider, [S3VwapOrb(entry_z=0.1, edge=0.5)])
    return bt.run(["SPY"], date(2026, 5, 4), date(2026, 5, 6), "5m")


def test_fills_ledger_costs_sum_to_trade_costs(s3_run):
    res = s3_run
    assert res.trades, "fixture run must produce trades for the invariant to bite"

    opens = [f for f in res.fills if f.kind == "OPEN"]
    closes = [f for f in res.fills if f.kind == "CLOSE"]
    assert len(opens) == len(closes) == len(res.trades)
    # Fill.cost is per leg: summed over the ledger it equals the trades' round-trip
    # costs exactly (previously the CLOSE leg re-carried the entry cost => ~150%).
    assert sum(f.cost for f in res.fills) == pytest.approx(
        sum(t.costs for t in res.trades)
    )
    assert all(f.cost >= 0.0 for f in res.fills)


# --------------------------------------------------------------------------- #
# 4) Expiry discipline in the gamma spine and the ATM-IV read
# --------------------------------------------------------------------------- #
def _mid_session(day: date) -> pd.Timestamp:
    open_utc, _ = session_bounds_utc(day)
    return open_utc + pd.Timedelta(minutes=60)


def _polluted(chain: OptionChainSeries, day: date) -> OptionChainSeries:
    """The same snapshot plus a far-dated book with huge OI and shifted IV — the
    shape of a real multi-expiry capture that must not contaminate the read."""
    far = chain.frame.copy()
    far["expiration"] = day + timedelta(days=30)
    far["open_interest"] = far["open_interest"] * 10 + 1
    far["implied_vol"] = far["implied_vol"] + 0.10
    return OptionChainSeries(
        symbol=chain.symbol,
        frame=pd.concat([chain.frame, far], ignore_index=True),
        source=chain.source,
    )


def test_gamma_structure_ignores_other_expiries(spy_chain, day):
    as_of = _mid_session(day)
    base = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")
    assert base is not None
    blended = gamma_structure_at(_polluted(spy_chain, day), as_of, expiry=day, ticker="SPY")
    assert blended is not None
    assert blended.gex_total == pytest.approx(base.gex_total, rel=1e-9)
    assert blended.regime == base.regime
    if base.flip_level is not None:
        assert blended.flip_level == pytest.approx(base.flip_level, rel=1e-9)


def test_gamma_structure_none_for_absent_expiry(spy_chain, day):
    as_of = _mid_session(day)
    assert gamma_structure_at(
        spy_chain, as_of, expiry=day + timedelta(days=7), ticker="SPY"
    ) is None


def test_atm_iv_expiry_filter(spy_chain, day):
    as_of = _mid_session(day)
    base = atm_iv_at(spy_chain, as_of)
    assert base is not None
    # The far book's +0.10 IV must not leak into the requested expiry's ATM read.
    assert atm_iv_at(_polluted(spy_chain, day), as_of, expiry=day) == pytest.approx(base)
    # An expiry the snapshot does not carry is unknowable, not approximated.
    assert atm_iv_at(spy_chain, as_of, expiry=day + timedelta(days=7)) is None


def test_pipeline_row_respects_expiry(spy_chain, spy_bars, day, config):
    """The engine path filters by expiry; the pipeline path must agree (the two
    are documented as producing identical FeatureRows)."""
    from intraday.features.pipeline import FeaturePipeline

    pipe = FeaturePipeline(config)
    as_of = _mid_session(day)
    clean = pipe.row("SPY", day, as_of, bars=spy_bars, chain=spy_chain, interval="1m")
    polluted = pipe.row(
        "SPY", day, as_of, bars=spy_bars, chain=_polluted(spy_chain, day), interval="1m"
    )
    assert clean.atm_iv is not None
    assert polluted.atm_iv == pytest.approx(clean.atm_iv)
    # A chain that only carries another tenor yields no option features at all.
    far = spy_chain.frame.copy()
    far["expiration"] = day + timedelta(days=30)
    far_only = OptionChainSeries(symbol="SPY", frame=far, source=spy_chain.source)
    row = pipe.row("SPY", day, as_of, bars=spy_bars, chain=far_only, interval="1m")
    assert row.atm_iv is None
    assert row.gex_total is None


def test_expiry_accepts_timestamp(spy_chain, day):
    """pd.Timestamp passes isinstance(date) but never equals a date elementwise —
    it must be normalized, not silently filter out every row."""
    as_of = _mid_session(day)
    a = gamma_structure_at(spy_chain, as_of, expiry=day, ticker="SPY")
    b = gamma_structure_at(spy_chain, as_of, expiry=pd.Timestamp(day), ticker="SPY")
    assert a is not None and b is not None
    assert b.gex_total == pytest.approx(a.gex_total)
    assert atm_iv_at(spy_chain, as_of, expiry=pd.Timestamp(day)) == pytest.approx(
        atm_iv_at(spy_chain, as_of)
    )


# --------------------------------------------------------------------------- #
# Comparison page routes through the centralized three-state verdict
# --------------------------------------------------------------------------- #
def test_comparison_badge_is_three_state():
    from intraday.report.comparison import _verdict_badge

    short_significant = SimpleNamespace(n_days=5, significant=True)
    assert "insufficient data" in _verdict_badge(short_significant)
    assert "badge--nodata" in _verdict_badge(short_significant)
    assert "EDGE" in _verdict_badge(SimpleNamespace(n_days=150, significant=True))
    assert "no edge" in _verdict_badge(SimpleNamespace(n_days=150, significant=False))


def test_comparison_band_never_outranks_row_badges(s3_run):
    """A sub-floor 'significant' entry must not light the page-level EDGE band
    while its own row badge abstains."""
    import dataclasses

    from intraday.eval import evaluate_result
    from intraday.report.comparison import render_comparison

    metrics = build_report(s3_run)
    ev = evaluate_result(s3_run, n_trials=2)
    entries = [
        {"name": "short-sig", "result": s3_run, "metrics": metrics,
         "ev": dataclasses.replace(ev, significant=True)},  # 3 days < floor
        {"name": "long-nosig", "result": s3_run, "metrics": metrics,
         "ev": dataclasses.replace(ev, n_days=150, significant=False)},
    ]
    html = render_comparison(entries, generated_at="2026-06-10T12:00:00")
    assert "AT LEAST ONE EDGE" not in html
    assert "badge--nodata" in html  # the sub-floor row abstains in the table
