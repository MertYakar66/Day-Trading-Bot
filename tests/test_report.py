"""Tests for the HTML dashboard generator (intraday.report).

Pins the properties that make the dashboard trustworthy:
- it is a single OFFLINE file (no external scripts/styles/CDNs — only the inert
  SVG xmlns is allowed),
- the SYNTHETIC-vs-REAL banner and the EDGE/NO-EDGE verdict are driven by the
  data and the eval (never hard-coded),
- output is deterministic given the same data + ``generated_at``,
- a backtest cannot inject markup into its own report (HTML escaping),
- the SVG primitives are well-formed and scale with the input,
- empty/degenerate inputs render gracefully instead of crashing.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import AssetKind, DataSource, Side, Trade
from intraday.data.synthetic import SyntheticDataProvider
from intraday.eval import evaluate_result
from intraday.eval.stats import StrategyEval
from intraday.metrics import build_report
from intraday.report import build_dashboard, render_dashboard
from intraday.report import dashboard as dash
from intraday.report import svg
from intraday.signals.s3_vwap_orb import S3VwapOrb

GEN = "2026-06-02T12:00:00"


# --------------------------------------------------------------------------- #
# Shared fixture: one short synthetic backtest, reused across tests
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def bt_result():
    cfg = EngineConfig.default()
    provider = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, provider, [S3VwapOrb(entry_z=0.1, edge=0.5)])
    return bt.run(["SPY", "QQQ"], date(2026, 5, 4), date(2026, 5, 8), "5m")


def _assert_offline(html: str) -> None:
    """No external resources: the file must render with the network unplugged."""
    assert "<script" not in html  # no JS at all
    cleaned = html.replace("http://www.w3.org/2000/svg", "")  # inert SVG namespace
    for bad in ("http://", "https://", "<link ", "src=", "cdn", "unpkg", "jsdelivr", "googleapis"):
        assert bad not in cleaned, f"unexpected external reference: {bad!r}"


# --------------------------------------------------------------------------- #
# End-to-end document
# --------------------------------------------------------------------------- #
def test_dashboard_is_wellformed_offline_html(bt_result):
    html = build_dashboard(bt_result, n_trials=1, generated_at=GEN)
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    for section in ("Equity curve", "Drawdown", "Daily PnL", "Honesty scorecard",
                    "Cost attribution", "Exit reasons", "Trade blotter"):
        assert section in html
    _assert_offline(html)


def test_dashboard_has_one_svg_per_chart(bt_result):
    html = build_dashboard(bt_result, generated_at=GEN)
    # equity + drawdown + daily-pnl + cost-waterfall + per-symbol bar == 5 charts
    # (the fixture is multi-symbol so the per-symbol breakdown renders; the rolling
    # Sharpe is gated off for a 5-day run; no universe section).
    assert html.count("<svg") == 5
    assert html.count("</svg>") == 5
    assert html.count("<svg") == html.count("</svg>")  # balanced (well-formed)


def test_dashboard_is_deterministic(bt_result):
    a = build_dashboard(bt_result, n_trials=3, generated_at=GEN)
    b = build_dashboard(bt_result, n_trials=3, generated_at=GEN)
    assert a == b


def test_synthetic_banner_and_no_edge_verdict(bt_result):
    html = build_dashboard(bt_result, generated_at=GEN)
    assert "SYNTHETIC DATA" in html
    assert "banner--synthetic" in html
    # Synthetic random-walk minus costs is never a real edge.
    assert "NO DEMONSTRATED EDGE" in html
    assert "verdict--noedge" in html


def test_real_source_flips_the_banner(bt_result):
    bt_result.data_source = DataSource.IBKR  # BacktestResult is a mutable dataclass
    html = build_dashboard(bt_result, generated_at=GEN)
    assert "REAL DATA" in html
    assert "banner--real" in html
    assert "ibkr" in html
    bt_result.data_source = DataSource.SYNTHETIC  # restore for other tests


def test_edge_verdict_branch_renders(bt_result):
    """A (fabricated) significant eval must light up the EDGE band."""
    metrics = build_report(bt_result)
    ev = StrategyEval(
        n_days=250, total_pnl=50_000.0, mean_daily_pnl=200.0, win_days=160,
        t_stat=8.2, p_value=0.0, sharpe_ann=8.2, sharpe_ann_ci_lo=6.1,
        sharpe_ann_ci_hi=10.3, skew=0.1, kurtosis=3.0, psr_vs_zero=0.999,
        n_trials=1, deflated_sharpe=0.98, significant=True,
    )
    html = render_dashboard(bt_result, metrics, ev, generated_at=GEN)
    assert "EDGE (rare)" in html
    assert "verdict--edge" in html


def test_scorecard_numbers_match_eval(bt_result):
    ev = evaluate_result(bt_result, n_trials=7)
    html = build_dashboard(bt_result, n_trials=7, generated_at=GEN)
    assert f"{ev.deflated_sharpe:.3f}" in html
    assert f"{ev.sharpe_ann:.2f}" in html
    assert "7-trial" in html  # the scorecard note humanises the trial count


def test_run_meta_window_and_strategy_shown(bt_result):
    html = build_dashboard(
        bt_result, generated_at=GEN,
        run_meta={"start": "2026-05-04", "end": "2026-05-08", "strategies": "s3"},
    )
    assert "2026-05-04" in html and "2026-05-08" in html
    assert "s3" in html


# --------------------------------------------------------------------------- #
# Escaping — a backtest cannot inject markup into its own report
# --------------------------------------------------------------------------- #
def test_trade_blotter_escapes_symbol():
    evil = Trade(
        symbol="<script>x</script>", strategy_id="s3", side=Side.LONG, size=1,
        instrument=AssetKind.STOCK, entry_ts=pd.Timestamp("2026-05-04 14:00", tz="UTC"),
        exit_ts=pd.Timestamp("2026-05-04 15:00", tz="UTC"), entry_price=100.0,
        exit_price=101.0, gross_pnl=1.0, costs=0.5, net_pnl=0.5, exit_reason="target",
    )
    html = dash._trade_blotter(SimpleNamespace(trades=[evil]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_blotter_truncates_and_notes(bt_result):
    many = list(bt_result.trades)
    if len(many) < 3:
        pytest.skip("need a few trades to exercise truncation note")
    html = dash._trade_blotter(SimpleNamespace(trades=many), limit=1)
    assert f"of {len(many)} trades" in html


# --------------------------------------------------------------------------- #
# Drawdown helper
# --------------------------------------------------------------------------- #
def test_drawdown_curve_is_nonpositive_and_zero_at_peak():
    dd = dash._drawdown_curve([100.0, 110.0, 99.0, 120.0])
    assert all(x <= 1e-12 for x in dd)
    assert dd[0] == 0.0 and dd[1] == 0.0   # new highs => no drawdown
    assert dd[2] == pytest.approx(99.0 / 110.0 - 1.0)
    assert dd[3] == 0.0                     # fresh peak resets the underwater curve


# --------------------------------------------------------------------------- #
# Universe section
# --------------------------------------------------------------------------- #
def test_universe_section_renders_tables_and_heatmap(bt_result):
    universe = {
        "n_symbols": 3, "n_days": 59, "n_trials": 6, "var_sr": 0.012,
        "strategies": {
            "s3": {"book_net": -12000.0, "sharpe_ann": -2.83, "t_stat": -1.37,
                   "psr_vs_zero": 0.08, "dsr_vs_all_trials": 0.0, "test_sharpe_ann": -7.1},
            "s4": {"book_net": -9000.0, "sharpe_ann": -2.76, "t_stat": -1.34,
                   "psr_vs_zero": 0.09, "dsr_vs_all_trials": 0.0, "test_sharpe_ann": -3.2},
        },
        "per_symbol": {"s3": {"SPY": -2.1, "QQQ": -1.4}, "s4": {"SPY": -3.0, "QQQ": -2.2}},
        "best_trial": {"strategy": "s3", "symbol": "SMH", "sharpe_ann": 2.42,
                       "t_stat": 1.17, "total_net": 1223.0, "deflated_sharpe": 0.127},
    }
    html = build_dashboard(bt_result, generated_at=GEN, universe=universe)
    assert "Cross-sectional universe" in html
    assert "Per-symbol Sharpe" in html
    assert "best single trial" in html.lower()
    assert html.count("<svg") == 6  # 4 core charts + per-symbol bar + universe heatmap
    _assert_offline(html)


def test_universe_section_is_defensive_about_missing_keys(bt_result):
    # Only a subset of keys present — must not raise.
    html = build_dashboard(bt_result, generated_at=GEN, universe={"n_symbols": 2})
    assert "Cross-sectional universe" in html


# --------------------------------------------------------------------------- #
# SVG primitives
# --------------------------------------------------------------------------- #
def test_line_chart_points_and_baseline():
    out = svg.line_chart([100, 101, 99, 103], baseline=100)
    assert out.startswith("<svg") and out.endswith("</svg>")
    assert 'stroke-dasharray' in out  # baseline drawn
    assert out.count("<polyline") == 1


def test_line_chart_degenerate_is_empty():
    assert "no data" in svg.line_chart([])
    assert "no data" in svg.line_chart([1.0])


def test_bar_chart_colours_by_sign():
    out = svg.bar_chart([5.0, -3.0, 2.0])
    assert svg.UP in out and svg.DOWN in out
    assert out.count("<rect") >= 3


def test_area_chart_clamps_to_zero_top():
    out = svg.area_chart([0.0, -0.05, -0.02])
    assert out.startswith("<svg") and "<polyline" in out


def test_waterfall_three_steps():
    out = svg.waterfall([("gross", -300.0, "total"), ("costs", -12000.0, "delta"),
                         ("net", -12300.0, "total")])
    assert out.count("<rect") == 3
    assert "gross" in out and "costs" in out and "net" in out


def test_heatmap_shape_and_blanks():
    out = svg.heatmap([[1.0, None], [-2.0, 0.5]], ["s3", "s4"], ["SPY", "QQQ"])
    assert "s3" in out and "SPY" in out
    # 4 cells rendered as rects (the None still gets a blank rect)
    assert out.count("<rect") == 4


def test_diverging_colour_endpoints():
    assert svg._diverging(0.0).startswith("#")
    assert svg._diverging(1.0) == "#3fb950"   # full positive -> UP green
    assert svg._diverging(-1.0) == "#f85149"  # full negative -> DOWN red


def test_sparkline_small():
    out = svg.sparkline([1, 2, 3, 2, 4])
    assert out.startswith("<svg") and "<polyline" in out


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #
def test_cli_report_writes_file(tmp_path):
    from intraday.cli import main

    out = tmp_path / "dash.html"
    rc = main([
        "report", "--start", "2026-05-04", "--end", "2026-05-06",
        "--interval", "5m", "--symbols", "SPY", "--out", str(out),
    ])
    assert rc == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    _assert_offline(html)


# --------------------------------------------------------------------------- #
# Verdict authority — the most important honesty contract
# --------------------------------------------------------------------------- #
def _mk_eval(**over) -> StrategyEval:
    base = dict(
        n_days=200, total_pnl=0.0, mean_daily_pnl=0.0, win_days=100, t_stat=0.0,
        p_value=0.5, sharpe_ann=0.0, sharpe_ann_ci_lo=-1.0, sharpe_ann_ci_hi=1.0,
        skew=0.0, kurtosis=3.0, psr_vs_zero=0.5, n_trials=1, deflated_sharpe=0.5,
        significant=False,
    )
    base.update(over)
    return StrategyEval(**base)


def test_verdict_is_sole_function_of_significant(bt_result):
    """The verdict text must depend ONLY on ev.significant, not on Sharpe/t/etc."""
    metrics = build_report(bt_result)
    a = render_dashboard(bt_result, metrics, _mk_eval(significant=True, sharpe_ann=8.0,
                                                      deflated_sharpe=0.97), generated_at=GEN)
    b = render_dashboard(bt_result, metrics, _mk_eval(significant=True, sharpe_ann=0.3,
                                                      t_stat=0.1, deflated_sharpe=0.96), generated_at=GEN)
    # Both significant -> both must show EDGE in band AND scorecard, regardless of Sharpe.
    for h in (a, b):
        assert "EDGE (rare)" in h and "verdict--edge" in h
        assert "NO DEMONSTRATED EDGE" not in h
    c = render_dashboard(bt_result, metrics, _mk_eval(significant=False, sharpe_ann=8.0,
                                                      deflated_sharpe=0.94), generated_at=GEN)
    assert "NO DEMONSTRATED EDGE" in c and "EDGE (rare)" not in c


def test_verdict_band_and_scorecard_agree(bt_result):
    metrics = build_report(bt_result)
    edge = render_dashboard(bt_result, metrics, _mk_eval(significant=True), generated_at=GEN)
    # band says "EDGE (rare)"; scorecard verdict row says "EDGE" with pos class
    assert "EDGE (rare)" in edge and '<td class="sc__val pos">EDGE</td>' in edge
    noedge = render_dashboard(bt_result, metrics, _mk_eval(significant=False), generated_at=GEN)
    assert "NO DEMONSTRATED EDGE" in noedge
    assert '<td class="sc__val neg">NO demonstrated edge</td>' in noedge


def test_nan_deflated_sharpe_renders_no_edge_without_crashing(bt_result):
    metrics = build_report(bt_result)
    for bad in (float("nan"), float("inf"), float("-inf")):
        html = render_dashboard(bt_result, metrics,
                                _mk_eval(significant=False, deflated_sharpe=bad), generated_at=GEN)
        assert "NO DEMONSTRATED EDGE" in html
        assert "nan" not in html and "inf" not in html  # rendered as em-dash, not raw


def test_banner_flips_both_ways(bt_result):
    metrics = build_report(bt_result)
    ev = _mk_eval()
    bt_result.data_source = DataSource.IBKR
    real = render_dashboard(bt_result, metrics, ev, generated_at=GEN)
    bt_result.data_source = DataSource.SYNTHETIC
    synth = render_dashboard(bt_result, metrics, ev, generated_at=GEN)
    assert "REAL DATA" in real and "SYNTHETIC DATA" not in real
    assert "SYNTHETIC DATA" in synth and "REAL DATA" not in synth


def test_metrics_and_dashboard_banner_use_same_source(bt_result):
    """The dashboard's banner must agree with metrics.render() on the source string."""
    from intraday.metrics import build_report as _br
    bt_result.data_source = DataSource.IBKR
    assert "[REAL DATA: ibkr]" in _br(bt_result).render()
    assert "REAL DATA" in build_dashboard(bt_result, generated_at=GEN)
    bt_result.data_source = DataSource.SYNTHETIC
    assert "SYNTHETIC DATA" in _br(bt_result).render()
    assert "SYNTHETIC DATA" in build_dashboard(bt_result, generated_at=GEN)


# --------------------------------------------------------------------------- #
# Escaping — broader coverage
# --------------------------------------------------------------------------- #
def test_blotter_escapes_strategy_and_exit_reason():
    evil = Trade(
        symbol="SPY", strategy_id="<b>s3</b>", side=Side.LONG, size=1,
        instrument=AssetKind.STOCK, entry_ts=pd.Timestamp("2026-05-04 14:00", tz="UTC"),
        exit_ts=pd.Timestamp("2026-05-04 15:00", tz="UTC"), entry_price=100.0,
        exit_price=101.0, gross_pnl=1.0, costs=0.5, net_pnl=0.5,
        exit_reason="<img src=x onerror=alert(1)>",
    )
    html = dash._trade_blotter(SimpleNamespace(trades=[evil]))
    assert "<b>s3</b>" not in html and "&lt;b&gt;s3&lt;/b&gt;" in html
    assert "<img" not in html and "&lt;img" in html


def test_universe_best_trial_escapes_markup(bt_result):
    universe = {
        "n_trials": 3,
        "best_trial": {"strategy": "<script>x</script>", "symbol": "<svg/>",
                       "sharpe_ann": 1.0, "t_stat": 1.0, "total_net": 1.0, "deflated_sharpe": 0.1},
    }
    html = build_dashboard(bt_result, generated_at=GEN, universe=universe)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html and "&lt;svg/&gt;" in html


def test_kpi_value_is_escaped():
    out = dash._kpi("label", "<b>bad</b>", "<i>sub</i>")
    assert "<b>bad</b>" not in out and "<i>sub</i>" not in out
    assert "&lt;b&gt;bad&lt;/b&gt;" in out


# --------------------------------------------------------------------------- #
# Determinism with universe + run_meta (key order must not matter after sorting)
# --------------------------------------------------------------------------- #
def test_dashboard_deterministic_with_universe(bt_result):
    u1 = {"n_symbols": 2, "n_days": 5,
          "strategies": {"s3": {"book_net": -1.0, "sharpe_ann": -2.0},
                         "s4": {"book_net": -2.0, "sharpe_ann": -1.0}},
          "per_symbol": {"s4": {"QQQ": -1.0, "SPY": -2.0}, "s3": {"SPY": -2.0, "QQQ": -1.0}}}
    # same content, different dict insertion order
    u2 = {"n_days": 5, "n_symbols": 2,
          "strategies": {"s4": {"sharpe_ann": -1.0, "book_net": -2.0},
                         "s3": {"sharpe_ann": -2.0, "book_net": -1.0}},
          "per_symbol": {"s3": {"QQQ": -1.0, "SPY": -2.0}, "s4": {"SPY": -2.0, "QQQ": -1.0}}}
    a = build_dashboard(bt_result, generated_at=GEN, universe=u1)
    b = build_dashboard(bt_result, generated_at=GEN, universe=u2)
    assert a == b


# --------------------------------------------------------------------------- #
# Drawdown + SVG robustness on degenerate inputs
# --------------------------------------------------------------------------- #
def test_drawdown_handles_nonpositive_equity():
    assert dash._drawdown_curve([-10.0, -20.0, -5.0]) == [0.0, 0.0, 0.0]
    assert dash._drawdown_curve([0.0, 0.0]) == [0.0, 0.0]
    assert dash._drawdown_curve([]) == []


def test_charts_survive_nan_and_inf():
    # Non-finite values must not crash; finite subset (or empty) is rendered.
    assert svg.line_chart([100.0, float("nan"), 102.0, float("inf"), 101.0]).startswith("<svg")
    assert "no data" in svg.line_chart([float("nan"), float("inf")])  # nothing finite
    assert svg.bar_chart([1.0, float("nan"), -2.0]).startswith("<svg")
    assert svg.area_chart([0.0, float("-inf"), -0.05]).startswith("<svg")
    assert svg.waterfall([("g", float("nan"), "total"), ("c", -1.0, "delta"),
                          ("n", -1.0, "total")]).count("<rect") == 3
    # _f never raises on non-finite input
    assert svg._f(float("inf")) == "0" and svg._f(float("nan")) == "0"


def test_area_chart_shows_positive_values_above_zero():
    # adaptive scale: a positive blip is not silently squashed into [-1, 0]
    out = svg.area_chart([0.0, 0.02, -0.05])
    assert out.startswith("<svg") and "<polyline" in out


def test_all_svg_primitives_wellformed_on_edge_cases():
    """Every primitive returns balanced <svg>...</svg> for empty/single/normal input."""
    for vals in ([], [1.0], [1.0, 2.0, 3.0]):
        for fn in (svg.line_chart, svg.area_chart, svg.bar_chart, svg.sparkline):
            out = fn(vals)
            assert out.startswith("<svg") and out.rstrip().endswith("</svg>"), fn.__name__
    assert svg.waterfall([("a", 1.0, "total")]).startswith("<svg")
    assert svg.heatmap([[1.0]], ["r"], ["c"]).startswith("<svg")
    assert svg.multi_line_chart([[1.0, 2.0], [2.0, 1.0]]).startswith("<svg")


# --------------------------------------------------------------------------- #
# Section builders: cost attribution + exit reasons
# --------------------------------------------------------------------------- #
def test_cost_attribution_renders_chart_and_facts(bt_result):
    html = dash._cost_attribution(build_report(bt_result))
    assert "<svg" in html  # the gross->costs->net waterfall
    for fact in ("cost / NAV", "cost / trade", "profit factor", "payoff ratio"):
        assert fact in html


def test_exit_reasons_renders_bars_and_escapes():
    html = dash._exit_reasons(SimpleNamespace(exit_reason_counts={"target": 5, "<b>x</b>": 2}))
    assert "exitreasons" in html
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html  # reason escaped


def test_exit_reasons_empty_is_blank():
    assert dash._exit_reasons(SimpleNamespace(exit_reason_counts={})) == ""


# --------------------------------------------------------------------------- #
# Aesthetics / UX additions (PR3): KPIs, colour-blind + print, per-symbol,
# rolling Sharpe, empty-state.
# --------------------------------------------------------------------------- #
def _quick_run(symbols, start, end, *, edge=0.5, entry_z=0.1, interval="5m"):
    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S3VwapOrb(entry_z=entry_z, edge=edge)])
    return bt.run(symbols, start, end, interval)


def test_dashboard_shows_sortino_and_calmar(bt_result):
    html = build_dashboard(bt_result, generated_at=GEN)
    assert "Sortino" in html and "Calmar" in html


def test_dashboard_has_colourblind_and_print_affordances(bt_result):
    html = build_dashboard(bt_result, generated_at=GEN)
    assert "@media print" in html                       # legible PDF/paper export
    assert "svg text{fill:#333}" in html                # print darkens chart labels
    assert "--pos:#1a7f37" in html                      # print darkens semantic colours
    assert r'content:"\25B2"' in html and r'content:"\25BC"' in html  # up/down chevrons
    # tie to a real KPI value carrying the chevron class (not just the CSS token).
    assert "kpi__value pos signed" in html or "kpi__value neg signed" in html
    _assert_offline(html)


def test_per_symbol_aggregation_sums_to_book(bt_result):
    agg = dash._per_symbol_agg(bt_result)
    m = build_report(bt_result)
    assert agg and set(agg) <= set(bt_result.symbols)
    assert sum(a["net"] for a in agg.values()) == pytest.approx(m.net_pnl, abs=1e-6)
    assert sum(a["gross"] for a in agg.values()) == pytest.approx(m.gross_pnl, abs=1e-6)
    assert sum(a["n"] for a in agg.values()) == len(bt_result.trades)
    html = dash._per_symbol_section(bt_result)
    for s in agg:
        assert s in html


def test_signed_cls_suppressed_for_non_finite():
    assert dash._signed_cls(5.0) == "pos signed"
    assert dash._signed_cls(-2.0) == "neg signed"
    assert dash._signed_cls(float("inf")) == ""   # em-dash value -> no chevron/colour
    assert dash._signed_cls(float("nan")) == ""


def test_per_symbol_section_only_for_multi_symbol():
    multi = _quick_run(["SPY", "QQQ"], date(2026, 5, 4), date(2026, 5, 8))
    assert "Per-symbol breakdown" in build_dashboard(multi, generated_at=GEN)
    single = _quick_run(["SPY"], date(2026, 5, 4), date(2026, 5, 8))
    assert "Per-symbol breakdown" not in build_dashboard(single, generated_at=GEN)


def test_rolling_sharpe_gated_on_length():
    short = _quick_run(["SPY"], date(2026, 5, 4), date(2026, 5, 8))   # ~5 days
    assert "Rolling Sharpe" not in build_dashboard(short, generated_at=GEN)
    long = _quick_run(["SPY"], date(2026, 4, 1), date(2026, 5, 29))   # ~40 days
    long_html = build_dashboard(long, generated_at=GEN)
    assert "Rolling Sharpe" in long_html
    assert long_html.count("<svg") == long_html.count("</svg>")       # still balanced


def test_empty_blotter_uses_empty_state():
    none = _quick_run(["SPY"], date(2026, 5, 4), date(2026, 5, 8), edge=0.0)
    assert not none.trades                       # gate refused everything (no edge)
    html = build_dashboard(none, generated_at=GEN)
    assert "empty-state" in html and "refused every signal" in html
    _assert_offline(html)
