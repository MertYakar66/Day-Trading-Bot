"""Tests for the comparison page, report index, JSON summary, shared theme, and the
multi-series chart — the report-package additions beyond the single dashboard.

Pins: the comparison overlays one line per strategy with a matching legend and a
ranked table (one row highlighted); the JSON summary is faithful + JSON-safe
(non-finite -> null) with the verdict driven by ``significant``; the index links
siblings and excludes itself; and everything stays offline, escaped, deterministic.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import DataSource
from intraday.data.synthetic import SyntheticDataProvider
from intraday.report import (
    build_comparison,
    build_index,
    build_summary,
    render_comparison,
    render_index,
    scan_directory,
    svg,
)
from intraday.report.theme import CSS, document

GEN = "2026-06-02T12:00:00"


def _run(strategy, *, start=date(2026, 5, 4), end=date(2026, 5, 8)):
    from intraday.signals.s3_vwap_orb import S3VwapOrb
    from intraday.signals.s4_orb_breakout import S4OpeningRangeBreakout
    from intraday.signals.s5_vwap_momentum import S5VwapMomentum

    cfg = EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    sig = {"s3": S3VwapOrb(entry_z=0.4, edge=0.4),
           "s4": S4OpeningRangeBreakout(edge=0.4),
           "s5": S5VwapMomentum(entry_z=0.4, edge=0.4)}[strategy]
    return IntradayBacktester(cfg, prov, [sig]).run(["SPY", "QQQ"], start, end, "5m")


@pytest.fixture(scope="module")
def runs():
    return [("s3", _run("s3")), ("s4", _run("s4")), ("s5", _run("s5"))]


def _assert_offline(html: str) -> None:
    assert "<script" not in html
    cleaned = html.replace("http://www.w3.org/2000/svg", "")
    for bad in ("http://", "https://", "<link ", "src=", "cdn", "unpkg", "jsdelivr"):
        assert bad not in cleaned, f"external reference: {bad!r}"


# --------------------------------------------------------------------------- #
# theme.document
# --------------------------------------------------------------------------- #
def test_document_wraps_and_escapes_title():
    out = document("<x> & title", "<p>body</p>")
    assert out.startswith("<!DOCTYPE html>")
    assert out.rstrip().endswith("</html>")
    assert "<title>&lt;x&gt; &amp; title</title>" in out
    assert "<p>body</p>" in out
    assert CSS.strip()[:5] in out  # stylesheet inlined


# --------------------------------------------------------------------------- #
# multi_line_chart
# --------------------------------------------------------------------------- #
def test_multi_line_one_polyline_per_series():
    out = svg.multi_line_chart([[1, 2, 3], [3, 2, 1], [0, 0, 1]], baseline=0.0)
    assert out.startswith("<svg") and out.count("<polyline") == 3
    assert "stroke-dasharray" in out  # baseline drawn


def test_multi_line_skips_degenerate_series():
    # one good series, one too-short -> exactly one polyline; never crashes
    out = svg.multi_line_chart([[1, 2, 3], [5]])
    assert out.count("<polyline") == 1


def test_multi_line_all_degenerate_is_empty():
    assert "no data" in svg.multi_line_chart([[], [1]])


def test_multi_line_handles_nan_and_uneven_lengths():
    out = svg.multi_line_chart([[1.0, float("nan"), 3.0], [2.0, 2.5]])
    assert out.startswith("<svg") and out.count("<polyline") == 2


def test_multi_line_uses_distinct_palette_colors():
    out = svg.multi_line_chart([[1, 2], [2, 3], [3, 4]])
    for c in svg.SERIES_PALETTE[:3]:
        assert c in out


# --------------------------------------------------------------------------- #
# JSON summary
# --------------------------------------------------------------------------- #
def test_summary_is_json_serialisable_and_faithful(runs):
    s = build_summary(runs[0][1], n_trials=3,
                      run_meta={"start": "2026-05-04", "end": "2026-05-08", "strategies": "s3"})
    blob = json.dumps(s)  # must not raise
    assert s["schema"] == "intraday.report.summary/1"
    assert s["is_real"] is False and s["data_source"] == "synthetic"
    assert s["eval"]["n_trials"] == 3
    assert s["verdict"] in ("EDGE", "NO_EDGE")
    assert s["verdict"] == ("EDGE" if s["eval"]["significant"] else "NO_EDGE")
    assert s["metrics"]["total_trades"] == sum(1 for _ in runs[0][1].trades)
    assert "nan" not in blob.lower() and "infinity" not in blob.lower()


def test_summary_non_finite_becomes_null():
    from types import SimpleNamespace as NS

    from intraday.report.export import summary_dict

    result = NS(data_source=DataSource.IBKR, symbols=["SPY"], interval="5m",
                n_days=0, initial_capital=100_000.0, final_equity=100_000.0)
    metrics = NS(gross_pnl=0.0, net_pnl=0.0, total_costs=0.0, sharpe_ratio=float("nan"),
                 sortino_ratio=float("inf"), max_drawdown=0.0, calmar_ratio=float("-inf"),
                 total_return=0.0, annualized_return=0.0, win_rate=0.0, profit_factor=0.0,
                 expectancy_per_trade=0.0, payoff_ratio=0.0, turnover=0.0, total_trades=0,
                 cost_bps_of_notional=0.0, cost_pct_of_nav=0.0)
    ev = NS(n_days=0, t_stat=float("nan"), p_value=1.0, sharpe_ann=float("nan"),
            sharpe_ann_ci_lo=float("nan"), sharpe_ann_ci_hi=float("nan"), psr_vs_zero=0.5,
            deflated_sharpe=float("nan"), n_trials=1, significant=False)
    s = summary_dict(result, metrics, ev)
    assert s["metrics"]["sharpe"] is None and s["metrics"]["sortino"] is None
    assert s["eval"]["deflated_sharpe"] is None
    assert s["is_real"] is True
    json.dumps(s)  # JSON-safe (null, not NaN)


# --------------------------------------------------------------------------- #
# Comparison page
# --------------------------------------------------------------------------- #
def test_comparison_overlays_each_strategy(runs):
    html = build_comparison(runs, generated_at=GEN)
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<polyline") == 3  # one equity line per strategy
    assert 'class="cmp"' in html
    for name in ("s3", "s4", "s5"):
        assert name in html
    assert html.count("legend__swatch") >= 3
    _assert_offline(html)


def test_comparison_highlights_exactly_one_best_row(runs):
    html = build_comparison(runs, generated_at=GEN)
    assert html.count('class="cmp-best"') == 1


def test_comparison_n_trials_equals_strategy_count(runs):
    html = build_comparison(runs, generated_at=GEN)
    assert "3 strategies is 3 trials" in html  # multiple-testing count is honest


def test_comparison_is_deterministic(runs):
    assert build_comparison(runs, generated_at=GEN) == build_comparison(runs, generated_at=GEN)


def test_comparison_banner_follows_source(runs):
    name, res = runs[0]
    res.data_source = DataSource.IBKR
    html = build_comparison([(name, res)], generated_at=GEN)
    assert "REAL DATA" in html
    res.data_source = DataSource.SYNTHETIC
    assert "SYNTHETIC DATA" in build_comparison([(name, res)], generated_at=GEN)


def test_comparison_escapes_strategy_name(runs):
    html = build_comparison([("<script>x</script>", runs[0][1])], generated_at=GEN)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_comparison_empty_is_graceful():
    html = render_comparison([], generated_at=GEN)
    assert html.startswith("<!DOCTYPE html>") and "No runs" in html


# --------------------------------------------------------------------------- #
# Report index
# --------------------------------------------------------------------------- #
def test_scan_directory_extracts_titles_and_skips_index(tmp_path):
    (tmp_path / "a.html").write_text("<html><head><title>Run A &amp; B</title></head></html>",
                                     encoding="utf-8")
    (tmp_path / "b.html").write_text("<html><head><title>Run B</title></head></html>",
                                     encoding="utf-8")
    (tmp_path / "index.html").write_text("<html><head><title>idx</title></head></html>",
                                         encoding="utf-8")
    entries = scan_directory(tmp_path)
    assert [e["file"] for e in entries] == ["a.html", "b.html"]  # sorted, index excluded
    assert entries[0]["title"] == "Run A & B"  # entities un-escaped on read
    assert all("bytes" in e for e in entries)


def test_scan_directory_falls_back_to_filename(tmp_path):
    (tmp_path / "notitle.html").write_text("<html><body>no title here</body></html>",
                                           encoding="utf-8")
    assert scan_directory(tmp_path)[0]["title"] == "notitle.html"


def test_extract_title_robust_to_attrs_entities_and_nesting():
    from intraday.report.index_page import _extract_title

    assert _extract_title("<title>Plain</title>", "fb") == "Plain"
    assert _extract_title("<title>A &amp; B</title>", "fb") == "A & B"  # entity decoded
    assert _extract_title("<title >spaced</title>", "fb") == "spaced"   # whitespace tolerant
    assert _extract_title("<head>no title</head>", "fb") == "fb"        # missing -> fallback
    assert _extract_title("<title>unclosed", "fb") == "fb"             # malformed -> fallback
    # pathological nested <title> is interpreter-dependent (RCDATA on some Pythons);
    # we only require it not crash and still surface the text (it is escaped on render).
    t = _extract_title("<title>Outer <title>Inner</title></title>", "fb")
    assert "Outer" in t and "Inner" in t


def test_index_title_round_trips_through_document(tmp_path):
    # a title with special chars survives write -> scan -> render, escaped at each layer
    (tmp_path / "x.html").write_text(document("<x> y & z", "<p>b</p>"), encoding="utf-8")
    entry = scan_directory(tmp_path)[0]
    assert entry["title"] == "<x> y & z"                       # recovered as plain text
    assert "&lt;x&gt; y &amp; z" in render_index([entry], generated_at=GEN)  # re-escaped


def test_render_index_links_and_escapes():
    html = render_index([{"file": "a.html", "title": "<b>A</b>", "bytes": 2048}], generated_at=GEN)
    assert 'href="a.html"' in html
    assert "<b>A</b>" not in html and "&lt;b&gt;A&lt;/b&gt;" in html
    assert "2 KB" in html
    _assert_offline(html)


def test_render_index_empty_is_graceful():
    html = render_index([], generated_at=GEN)
    assert html.startswith("<!DOCTYPE html>") and "No dashboards" in html


def test_build_index_end_to_end(tmp_path):
    (tmp_path / "d.html").write_text("<html><head><title>D</title></head></html>", encoding="utf-8")
    html = build_index(tmp_path, generated_at=GEN)
    assert 'href="d.html"' in html and "1 report(s)" in html


# --------------------------------------------------------------------------- #
# CLI integration
# --------------------------------------------------------------------------- #
def test_cli_compare_writes_html_and_json(tmp_path):
    from intraday.cli import main

    out, js = tmp_path / "c.html", tmp_path / "c.json"
    rc = main(["compare", "--start", "2026-05-04", "--end", "2026-05-06", "--interval", "5m",
               "--symbols", "SPY", "--strategy", "s3", "s4", "--out", str(out), "--emit-json", str(js)])
    assert rc == 0 and out.exists() and js.exists()
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert len(data) == 2 and {d["strategies"] for d in data} == {"s3", "s4"}


def test_cli_report_index_writes_file(tmp_path):
    from intraday.cli import main

    main(["report", "--start", "2026-05-04", "--end", "2026-05-06", "--interval", "5m",
          "--symbols", "SPY", "--out", str(tmp_path / "one.html")])
    rc = main(["report-index", "--dir", str(tmp_path), "--out", str(tmp_path / "index.html")])
    assert rc == 0
    idx = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'href="one.html"' in idx
    assert 'href="index.html"' not in idx  # never links itself


def test_cli_report_emit_json(tmp_path):
    from intraday.cli import main

    out, js = tmp_path / "r.html", tmp_path / "r.json"
    main(["report", "--start", "2026-05-04", "--end", "2026-05-06", "--interval", "5m",
          "--symbols", "SPY", "--out", str(out), "--emit-json", str(js)])
    s = json.loads(js.read_text(encoding="utf-8"))
    assert s["schema"] == "intraday.report.summary/1" and "verdict" in s
