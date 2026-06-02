"""End-to-end real-data PATH integration (network-free, IBKR-shaped fixtures).

Proves the corrected real-data path runs through the SAME engine as synthetic:
ingest IBKR-shaped payloads → ParquetStore (IBKR provenance) → StoreBackedProvider
→ IntradayBacktester (S3) → metrics. Asserts the report is labelled REAL (never
SYNTHETIC), PnL identities hold, the multi-symbol grid aligns, the run is
deterministic, and the remapped bars honor the no-look-ahead PIT contract.

This locks the plumbing for the operator's full backfill; it makes NO edge claim.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.backtest.engine import IntradayBacktester
from intraday.config import EngineConfig
from intraday.contracts import DataSource
from intraday.data.ibkr import ingest_payload
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider
from intraday.metrics import build_report
from intraday.signals.s3_vwap_orb import S3VwapOrb

DAYS = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]


@pytest.fixture
def real_store(tmp_path, make_ibkr_payload) -> ParquetStore:
    store = ParquetStore(tmp_path / "store")
    for d in DAYS:
        ingest_payload(store, "SPY", "5m", make_ibkr_payload(d, "5m", base=755.0, amp=6.0))
        ingest_payload(store, "QQQ", "5m", make_ibkr_payload(d, "5m", base=540.0, amp=5.0))
    return store


@pytest.mark.integration
@pytest.mark.swe
def test_real_data_backtest_runs_and_is_labelled_real(real_store):
    cfg = EngineConfig.default()
    provider = StoreBackedProvider(real_store, DataSource.IBKR, symbols=["SPY", "QQQ"], interval="5m")
    bt = IntradayBacktester(cfg, provider, [S3VwapOrb(edge=0.10)])
    result = bt.run(["SPY", "QQQ"], DAYS[0], DAYS[-1], "5m")

    assert result.data_source is DataSource.IBKR
    assert result.n_days == 3
    # PnL identity holds per trade (net = gross − costs); costs never negative.
    for t in result.trades:
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs)
        assert t.costs >= 0.0
    assert result.final_equity == pytest.approx(
        cfg.risk.paper_nav + sum(t.net_pnl for t in result.trades)
    )

    report = build_report(result)
    out = report.render()
    assert report.data_source == "ibkr"
    assert "REAL DATA: ibkr" in out
    assert "SYNTHETIC" not in out


@pytest.mark.integration
def test_real_data_backtest_is_deterministic(real_store):
    cfg = EngineConfig.default()

    def _net():
        prov = StoreBackedProvider(real_store, DataSource.IBKR, symbols=["SPY", "QQQ"], interval="5m")
        res = IntradayBacktester(cfg, prov, [S3VwapOrb(edge=0.10)]).run(
            ["SPY", "QQQ"], DAYS[0], DAYS[-1], "5m"
        )
        return res.final_equity, len(res.trades)

    assert _net() == _net()  # replay from parquet is byte-for-byte reproducible


@pytest.mark.no_lookahead
def test_stored_bars_respect_pit_availability(real_store):
    """A remapped real BarSeries exposes only bars whose close+latency ≤ as_of."""
    prov = StoreBackedProvider(real_store, DataSource.IBKR, symbols=["SPY"], interval="5m")
    bars = prov.get_bars("SPY", DAYS[0], "5m")
    mid = bars.frame.index[40]
    as_of = mid + bars.latency
    visible = bars.available_at(as_of)
    # Everything visible closed at or before the cutoff; nothing from the future.
    assert visible.index.max() <= as_of - bars.latency
    assert len(visible) == 41  # bars 0..40 inclusive
    # Appending a future bar must NOT change the PIT slice taken at `as_of`.
    future = bars.frame.iloc[[-1]].copy()
    future.index = pd.DatetimeIndex([bars.frame.index[-1] + pd.Timedelta(minutes=5)], name="ts")
    augmented = pd.concat([bars.frame, future])
    augmented.index.name = "ts"
    from intraday.contracts import BarSeries
    bars2 = BarSeries(bars.symbol, bars.interval, augmented, bars.source, bars.latency)
    pd.testing.assert_frame_equal(bars2.available_at(as_of), visible)
