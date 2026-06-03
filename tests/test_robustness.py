"""Robustness / defensive-correctness tests (the launch-hardening pass).

These pin the guards added so the engine fails LOUDLY on malformed or degenerate
input instead of silently propagating NaN/inf, mis-labelling provenance, or
reading one symbol's bar against another's timestamp:

- provenance: ``read_bars`` refuses to guess a data source (parity with tape/chain);
- data quality: empty / non-finite bar frames are rejected before they poison equity;
- grid alignment: symbols must share one RTH timestamp grid, not just a bar count;
- config: nonsensical risk/gate/cost/session knobs raise at construction;
- signals: S3/S4/S5 demand strictly positive trade geometry (no silent p=0.5);
- report SVG: non-finite colour inputs degrade to neutral, never crash;
- eval: the EDGE threshold is a single named constant.
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import CostConfig, EngineConfig, GateConfig, RiskConfig, SessionConfig
from intraday.contracts import BarSeries, DataSource
from intraday.data.provider import DataProvider, DataUnavailable
from intraday.data.quality import assert_finite_bars, spread_bps
from intraday.data.synthetic import SyntheticDataProvider
from intraday.features.base import FeatureRow
from intraday.report import svg
from intraday.signals.s3_vwap_orb import S3VwapOrb

TEST_DAY = date(2026, 5, 18)


# --------------------------------------------------------------------------- #
# A minimal provider returning hand-controlled bar frames (S3 needs no options).
# --------------------------------------------------------------------------- #
class _FrameProvider(DataProvider):
    source = DataSource.IBKR  # pretend real so a relabel bug would be visible

    def __init__(self, frames: dict[str, pd.DataFrame], days: list[date]) -> None:
        self._frames = frames
        self._days = days

    def trading_days(self, start, end):
        return list(self._days)

    def get_bars(self, symbol, day, interval="1m"):
        return BarSeries(symbol.upper(), interval, self._frames[symbol.upper()],
                         self.source, pd.Timedelta(0))

    def get_option_chain(self, symbol, day):  # pragma: no cover - S3 never calls
        raise DataUnavailable("no chain")

    def get_option_tape(self, symbol, day):  # pragma: no cover - S3 never calls
        raise DataUnavailable("no tape")


def _run(frames):
    cfg = EngineConfig.default()
    bt = IntradayBacktester(cfg, _FrameProvider(frames, [TEST_DAY]), [S3VwapOrb()])
    return bt.run(list(frames.keys()), TEST_DAY, TEST_DAY, "1m")


# --------------------------------------------------------------------------- #
# Provenance: read_bars must never silently relabel a data source
# --------------------------------------------------------------------------- #
def test_read_bars_roundtrips_source(store, config):
    synth = SyntheticDataProvider(config.data, config.session)
    bars = synth.get_bars("SPY", TEST_DAY, "1m")
    store.write_bars(bars, TEST_DAY)
    got = store.read_bars("SPY", TEST_DAY, "1m")
    assert got.source is DataSource.SYNTHETIC  # round-trips honestly


def test_read_bars_refuses_missing_sidecar(store, config):
    """A parquet of (possibly real) bars without its provenance sidecar must NOT be
    read as SYNTHETIC — it must refuse, exactly like read_tape / read_chain."""
    synth = SyntheticDataProvider(config.data, config.session)
    store.write_bars(synth.get_bars("SPY", TEST_DAY, "1m"), TEST_DAY)
    sidecar = store._partition("bars", "SPY", TEST_DAY) / "bars_1m_meta.json"
    sidecar.unlink()
    with pytest.raises(FileNotFoundError, match="provenance"):
        store.read_bars("SPY", TEST_DAY, "1m")


# --------------------------------------------------------------------------- #
# Data quality: malformed bar frames are rejected, not propagated
# --------------------------------------------------------------------------- #
def test_assert_finite_bars_rejects_empty():
    with pytest.raises(DataUnavailable, match="empty"):
        assert_finite_bars(pd.DataFrame(columns=["open", "high", "low", "close"]),
                           symbol="SPY", day=TEST_DAY)


def test_assert_finite_bars_rejects_nan_price():
    idx = pd.date_range("2026-05-18 13:30", periods=4, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0, 4.0], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
         "close": [1.0, float("nan"), 3.0, 4.0], "volume": [1, 1, 1, 1]},
        index=idx,
    )
    with pytest.raises(DataUnavailable, match="non-finite"):
        assert_finite_bars(frame, symbol="SPY", day=TEST_DAY)


def test_assert_finite_bars_rejects_missing_column():
    idx = pd.date_range("2026-05-18 13:30", periods=3, freq="1min", tz="UTC")
    frame = pd.DataFrame(  # no 'close' column
        {"open": [1.0, 2.0, 3.0], "high": [1, 2, 3], "low": [1, 2, 3]}, index=idx,
    )
    with pytest.raises(DataUnavailable, match="missing required column"):
        assert_finite_bars(frame, symbol="SPY", day=TEST_DAY)


def test_assert_finite_bars_rejects_inf_price():
    idx = pd.date_range("2026-05-18 13:30", periods=3, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0], "high": [1.0, float("inf"), 3.0],
         "low": [1, 2, 3], "close": [1, 2, 3], "volume": [1, 1, 1]},
        index=idx,
    )
    with pytest.raises(DataUnavailable, match="non-finite"):
        assert_finite_bars(frame, symbol="SPY", day=TEST_DAY)


def test_engine_halts_on_empty_session(config):
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    with pytest.raises(DataUnavailable):
        _run({"SPY": empty})


def test_engine_halts_on_nan_priced_session(config):
    synth = SyntheticDataProvider(config.data, config.session)
    frame = synth.get_bars("SPY", TEST_DAY, "1m").frame.copy()
    frame.iloc[5, frame.columns.get_loc("close")] = float("nan")
    with pytest.raises(DataUnavailable):
        _run({"SPY": frame})


# --------------------------------------------------------------------------- #
# Grid alignment: same length but shifted timestamps must be caught
# --------------------------------------------------------------------------- #
def test_engine_detects_timestamp_grid_mismatch(config):
    synth = SyntheticDataProvider(config.data, config.session)
    spy = synth.get_bars("SPY", TEST_DAY, "1m").frame
    qqq = spy.copy()
    qqq.index = qqq.index + pd.Timedelta(minutes=1)  # same count, shifted grid
    with pytest.raises(ValueError, match="timestamp mismatch"):
        _run({"SPY": spy, "QQQ": qqq})


# --------------------------------------------------------------------------- #
# Config validation: reject nonsensical knobs at construction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs", [
    {"paper_nav": 0.0},
    {"paper_nav": -100.0},
    {"kelly_fraction": 0.0},
    {"kelly_fraction": 1.5},
    {"max_risk_per_trade_pct": 0.0},
    {"max_risk_per_trade_pct": 1.5},
    {"daily_loss_limit_pct": -0.01},
    {"daily_loss_limit_pct": 1.5},
    {"max_concurrent_positions": 0},
    {"min_size": 0},
    {"max_size": 0},  # < min_size (1)
])
def test_risk_config_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)


def test_gate_config_rejects_negative_threshold():
    with pytest.raises(ValueError):
        GateConfig(ev_threshold=-1.0)
    with pytest.raises(ValueError):
        GateConfig(risk_free_rate=2.0)


def test_cost_config_rejects_negative():
    with pytest.raises(ValueError):
        CostConfig(fallback_spread_pct=-0.001)


def test_session_config_rejects_inverted_hours():
    with pytest.raises(ValueError):
        SessionConfig(rth_open=time(16, 0), rth_close=time(9, 30))


def test_default_config_is_valid():
    EngineConfig.default()  # must not raise


# --------------------------------------------------------------------------- #
# Signals: strictly positive geometry (no silent p_fair = 0.5)
# --------------------------------------------------------------------------- #
def test_s3_returns_none_on_zero_reward(config):
    """If the VWAP target coincides with the reference price (zero reward), S3 must
    stand aside rather than emit a degenerate proposal with a fabricated p_fair."""
    row = FeatureRow(
        symbol="SPY", as_of=pd.Timestamp("2026-05-18 15:00", tz="UTC"),
        last_price=500.0, vwap=500.0,  # target == ref => reward 0
        vwap_sigma=0.5, vwap_dev_sigma=3.0, orb_high=501.0, orb_low=497.0, meta={},
    )
    assert S3VwapOrb().propose(row, config=config) is None


# --------------------------------------------------------------------------- #
# Report SVG: non-finite colour inputs degrade gracefully
# --------------------------------------------------------------------------- #
def test_diverging_handles_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        out = svg._diverging(bad)
        assert out.startswith("#") and len(out) == 7


def test_heatmap_survives_non_finite_vmax():
    out = svg.heatmap([[1.0, float("nan")], [2.0, 3.0]], ["s1", "s2"], ["x", "y"],
                      vmax=float("nan"))
    assert out.startswith("<svg")
    assert "nan" not in out.lower()  # no NaN leaked into a fill/text


def test_spread_bps_flags_malformed_quotes():
    assert spread_bps(0.05, 0.0) == float("inf")   # non-positive price
    assert spread_bps(-0.01, 100.0) == float("inf")  # crossed (ask < bid)
    assert spread_bps(0.0, 100.0) == 0.0             # zero spread is valid


# --------------------------------------------------------------------------- #
# Eval: a single named significance threshold
# --------------------------------------------------------------------------- #
def test_significance_threshold_constant():
    from intraday.eval import DEFLATED_SHARPE_SIGNIFICANCE_THRESHOLD as THR
    from intraday.eval import evaluate_daily_pnl

    assert THR == 0.95
    # A flat series cannot be significant.
    ev = evaluate_daily_pnl(pd.Series([0.0, 0.0, 0.0, 0.0, 0.0]))
    assert ev.significant is False
