"""Coverage hardening for the audit's remaining test-gap findings.

Each test pins a previously-unexercised path: the feed-gap boundary, the engine
daily kill-switch latch, degenerate eval inputs, the zero-trade metrics report,
SVG extreme-value rendering, JSON-summary determinism, the read-only poller's
data-unavailable path, and multi-day paper-ledger persistence. All deterministic
and network-free (synthetic provider only).
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import DataSource
from intraday.data.provider import DataUnavailable, FeedGapError
from intraday.data.quality import assert_no_feed_gap, detect_feed_gap
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider
from intraday.data.synthetic import SyntheticDataProvider
from intraday.eval import evaluate_daily_pnl
from intraday.execution.paper_ledger import PaperLedger
from intraday.live.poller import LivePoller
from intraday.metrics import build_report
from intraday.report import build_summary, svg
from intraday.signals.s3_vwap_orb import S3VwapOrb

DAY = date(2026, 5, 18)


def _run(symbols, start, end, *, edge=0.5, entry_z=0.1, interval="5m", cfg=None):
    cfg = cfg or EngineConfig.default()
    prov = SyntheticDataProvider(cfg.data, cfg.session)
    bt = IntradayBacktester(cfg, prov, [S3VwapOrb(entry_z=entry_z, edge=edge)])
    return bt.run(symbols, start, end, interval)


# --------------------------------------------------------------------------- #
# Feed-gap detection boundary
# --------------------------------------------------------------------------- #
def _index(minutes: list[int]) -> pd.DatetimeIndex:
    base = pd.Timestamp("2026-05-18 13:30:00", tz="UTC")
    return pd.DatetimeIndex([base + pd.Timedelta(minutes=m) for m in minutes])


def test_feed_gap_boundary():
    # Evenly spaced 1-min bars: no gap.
    assert detect_feed_gap(_index([0, 1, 2, 3]), "1m") is None
    assert_no_feed_gap(_index([0, 1, 2, 3]), "1m")  # must not raise
    # A 1.5x gap is exactly at the threshold (detector uses strict '>') -> allowed.
    base = pd.Timestamp("2026-05-18 13:30:00", tz="UTC")
    at_threshold = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=90), base + pd.Timedelta(seconds=150)])
    assert detect_feed_gap(at_threshold, "1m") is None
    # A 2x gap (skipped a bar) -> detected and halts.
    assert detect_feed_gap(_index([0, 1, 3, 4]), "1m") is not None
    with pytest.raises(FeedGapError):
        assert_no_feed_gap(_index([0, 1, 3, 4]), "1m")


# --------------------------------------------------------------------------- #
# Engine daily kill-switch latch
# --------------------------------------------------------------------------- #
def test_engine_daily_kill_switch_latches():
    """With a near-zero daily loss budget, the first realized cost breaches it and the
    kill-switch latches, so subsequent same-day signals carry it in their trail.

    The budget ($0.10) is set far below any single trade's transaction cost, so a
    breach is virtually certain once *any* trade closes — the test guards that a
    trade actually occurred so it fails loudly rather than silently if it doesn't."""
    from dataclasses import replace
    cfg = EngineConfig.default()
    cfg = replace(cfg, risk=replace(cfg.risk, daily_loss_limit_pct=1e-6))  # ~$0.10 budget
    result = _run(["SPY"], date(2026, 5, 4), date(2026, 5, 6), edge=0.5, entry_z=0.1, cfg=cfg)
    assert result.trades, "precondition: the strategy must take at least one trade"
    assert result.signals, "precondition: the strategy must generate signals"
    latched = [s for s in result.signals if "daily_kill_switch" in s["trail"]]
    assert latched, "kill-switch never appeared in any signal trail despite a breached budget"


# --------------------------------------------------------------------------- #
# Degenerate eval inputs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("series", [
    pd.Series([], dtype="float64"),
    pd.Series([5.0]),                              # single day
    pd.Series([float("nan"), float("nan")]),      # all NaN
    pd.Series([float("nan"), 3.0, float("inf")]),  # one finite, rest non-finite
])
def test_eval_degenerate_series_do_not_crash(series):
    ev = evaluate_daily_pnl(series, n_trials=3)
    assert ev.significant is False
    assert ev.n_trials == 3
    assert isinstance(ev.to_dict(), dict)


# --------------------------------------------------------------------------- #
# Zero-trade metrics report
# --------------------------------------------------------------------------- #
def test_metrics_report_with_zero_trades():
    none = _run(["SPY"], date(2026, 5, 4), date(2026, 5, 6), edge=0.0)  # gate refuses all
    assert not none.trades
    m = build_report(none)
    assert m.total_trades == 0
    assert m.net_pnl == pytest.approx(0.0)
    assert m.win_rate == pytest.approx(0.0)
    # render must not raise / divide by zero on an empty book.
    assert isinstance(m.render(), str)


# --------------------------------------------------------------------------- #
# SVG primitives on extreme inputs
# --------------------------------------------------------------------------- #
def _balanced(out: str) -> bool:
    return out.startswith("<svg") and out.count("<svg") == out.count("</svg>") == 1


def test_svg_extreme_values():
    assert _balanced(svg.line_chart([1e-10, 1e10, 1.0, -1e9]))   # huge dynamic range
    assert _balanced(svg.line_chart([3.0, 3.0, 3.0, 3.0]))        # perfectly flat
    assert _balanced(svg.bar_chart([5.0, -3.0, 0.0, 8.0, -8.0]))  # mixed sign incl zero
    assert _balanced(svg.area_chart([0.0, -0.99, -0.5]))
    assert "no data" in svg.line_chart([42.0])                    # single value -> empty
    # No raw NaN/inf leaks into emitted coordinates/labels.
    for out in (svg.line_chart([1e-10, 1e10]), svg.bar_chart([1e9, -1e9])):
        assert "nan" not in out.lower() and "inf" not in out.lower()


# --------------------------------------------------------------------------- #
# JSON summary determinism + round-trip
# --------------------------------------------------------------------------- #
def test_summary_json_is_deterministic_and_roundtrips():
    res = _run(["SPY", "QQQ"], date(2026, 5, 4), date(2026, 5, 8))
    s1 = build_summary(res, n_trials=2, run_meta={"start": "2026-05-04", "end": "2026-05-08"})
    s2 = build_summary(res, n_trials=2, run_meta={"start": "2026-05-04", "end": "2026-05-08"})
    a = json.dumps(s1, sort_keys=True)
    b = json.dumps(s2, sort_keys=True)
    assert a == b                          # deterministic
    assert json.loads(a) == s1             # round-trips
    assert s1["verdict"] in ("EDGE", "NO_EDGE")


# --------------------------------------------------------------------------- #
# Read-only poller: data unavailable
# --------------------------------------------------------------------------- #
def test_poller_raises_when_no_data(tmp_path):
    store = ParquetStore(tmp_path / "empty")
    provider = StoreBackedProvider(store, DataSource.YAHOO, symbols=["SPY"], interval="5m")
    poller = LivePoller(EngineConfig.default(), provider, [S3VwapOrb()], interval="5m")
    # Missing data must raise a clear, specific error (not silently return nothing).
    with pytest.raises((FileNotFoundError, DataUnavailable)):
        poller.poll("SPY", DAY, pd.Timestamp("2026-05-18 18:00:00", tz="UTC"))


# --------------------------------------------------------------------------- #
# Paper ledger: multi-day persistence
# --------------------------------------------------------------------------- #
def test_paper_ledger_persists_multiple_days(tmp_path):
    res = _run(["SPY"], date(2026, 5, 4), date(2026, 5, 8))  # a full week
    assert res.n_days >= 2 and res.trades
    led = PaperLedger.from_backtest(res)
    store = ParquetStore(tmp_path / "store")
    led.persist(store)
    # signals + ledger partitions exist for more than one day.
    sig_days = list((tmp_path / "store" / "signals").glob("date=*"))
    fill_days = list((tmp_path / "store" / "paper_ledger").glob("date=*"))
    assert len(sig_days) >= 2
    assert len(fill_days) >= 1
    # round-trip one fills partition back.
    any_fill = next(iter((tmp_path / "store" / "paper_ledger").glob("date=*/fills.parquet")))
    assert len(pd.read_parquet(any_fill)) >= 1
