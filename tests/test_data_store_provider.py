"""Tests for StoreBackedProvider — offline replay of captured data.

Focus: the calendar derived from stored partitions (multi-symbol intersection,
range + trading-day filtering) and provenance enforcement for options frames.
Bar ingest/replay/provenance is also covered in test_data_ibkr.py.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from intraday.contracts import (
    CHAIN_COLUMNS,
    TAPE_COLUMNS,
    DataSource,
    OptionChainSeries,
    OptionTape,
)
from intraday.data.ibkr import ingest_payload
from intraday.data.provider import DataUnavailable
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider

D1 = date(2026, 6, 1)
D2 = date(2026, 6, 2)
D3 = date(2026, 6, 3)


def test_trading_days_intersect_across_symbols(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    # SPY present D1,D2,D3 ; QQQ present only D1,D2 → intersection is D1,D2.
    for d in (D1, D2, D3):
        ingest_payload(store, "SPY", "5m", make_ibkr_payload(d, "5m"))
    for d in (D1, D2):
        ingest_payload(store, "QQQ", "5m", make_ibkr_payload(d, "5m"))

    both = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY", "QQQ"], interval="5m")
    assert both.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == [D1, D2]

    spy_only = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY"], interval="5m")
    assert spy_only.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == [D1, D2, D3]


def test_trading_days_filters_to_requested_range(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    for d in (D1, D2, D3):
        ingest_payload(store, "SPY", "5m", make_ibkr_payload(d, "5m"))
    prov = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY"], interval="5m")
    assert prov.trading_days(D2, D2) == [D2]


def test_trading_days_uses_all_symbols_when_unspecified(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    ingest_payload(store, "SPY", "5m", make_ibkr_payload(D1, "5m"))
    ingest_payload(store, "QQQ", "5m", make_ibkr_payload(D2, "5m"))
    # No symbols given → scans all tickers; intersection of {D1} and {D2} is empty.
    prov = StoreBackedProvider(store, DataSource.IBKR, interval="5m")
    assert prov.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == []


def test_empty_store_has_no_trading_days(tmp_path):
    prov = StoreBackedProvider(ParquetStore(tmp_path / "store"), DataSource.IBKR, interval="5m")
    assert prov.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == []


def test_interval_specific_partitions(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    ingest_payload(store, "SPY", "5m", make_ibkr_payload(D1, "5m"))
    # A provider asking for 1m sees no 1m partition for D1.
    prov1m = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY"], interval="1m")
    assert prov1m.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == []


# --------------------------------------------------------------------------- #
# Options frames + provenance enforcement
# --------------------------------------------------------------------------- #
def _chain(day: date) -> OptionChainSeries:
    snap = pd.Timestamp(f"{day}T15:00:00Z")
    row = {
        "snapshot_ts": snap, "available_ts": snap + pd.Timedelta(seconds=1),
        "expiration": day, "strike": 6000.0, "option_type": "C",
        "open_interest": 1000, "implied_vol": 0.15, "spot": 6000.0,
    }
    frame = pd.DataFrame([row], columns=list(CHAIN_COLUMNS))
    return OptionChainSeries("SPX", frame, DataSource.THETA)


def _tape(day: date) -> OptionTape:
    ts = pd.Timestamp(f"{day}T15:00:00Z")
    row = {
        "ts": ts, "available_ts": ts + pd.Timedelta(milliseconds=250), "expiration": day,
        "strike": 6000.0, "right": "C", "price": 12.0, "size": 5,
        "nbbo_bid": 11.9, "nbbo_ask": 12.1, "side_inferred": "buy",
    }
    frame = pd.DataFrame([row], columns=list(TAPE_COLUMNS))
    return OptionTape("SPX", frame, DataSource.THETA)


def test_option_chain_tape_roundtrip_with_provenance(tmp_path):
    store = ParquetStore(tmp_path / "store")
    store.write_chain(_chain(D1), D1)
    store.write_tape(_tape(D1), D1)

    theta = StoreBackedProvider(store, DataSource.THETA, symbols=["SPX"], interval="5m")
    assert theta.get_option_chain("SPX", D1).source is DataSource.THETA
    assert theta.get_option_tape("SPX", D1).source is DataSource.THETA

    # Declaring a different source must refuse to serve (no relabeling).
    ibkr = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPX"], interval="5m")
    with pytest.raises(DataUnavailable):
        ibkr.get_option_chain("SPX", D1)
    with pytest.raises(DataUnavailable):
        ibkr.get_option_tape("SPX", D1)


# --------------------------------------------------------------------------- #
# Option quotes slot (ingest/reconstruction intermediate)
# --------------------------------------------------------------------------- #
def _quotes(day: date) -> pd.DataFrame:
    ts = pd.Timestamp(f"{day}T15:00:00Z")
    return pd.DataFrame(
        [{
            "ts": ts, "expiration": day, "strike": 600.0, "right": "C",
            "bid": 1.10, "ask": 1.20, "bid_size": 10, "ask_size": 12, "mid": 1.15,
        }]
    )


def test_option_quotes_roundtrip_with_provenance(tmp_path):
    store = ParquetStore(tmp_path / "store")
    store.write_option_quotes("SPY", D1, _quotes(D1), source=DataSource.THETA, interval="1m")

    frame, source = store.read_option_quotes("SPY", D1, interval="1m")
    assert source is DataSource.THETA
    assert len(frame) == 1
    assert frame.loc[0, "mid"] == pytest.approx(1.15)
    # Interval is part of the partition key, like bars.
    with pytest.raises(FileNotFoundError):
        store.read_option_quotes("SPY", D1, interval="5m")


def test_option_quotes_missing_partition_raises(tmp_path):
    store = ParquetStore(tmp_path / "store")
    with pytest.raises(FileNotFoundError):
        store.read_option_quotes("SPY", D1)


def test_option_quotes_missing_sidecar_refused(tmp_path):
    store = ParquetStore(tmp_path / "store")
    path = store.write_option_quotes("SPY", D1, _quotes(D1), source=DataSource.THETA)
    (path.parent / "quotes_1m_meta.json").unlink()
    with pytest.raises(FileNotFoundError, match="provenance sidecar"):
        store.read_option_quotes("SPY", D1)


def test_theta_derived_source_semantics():
    """Synthesized chains are real (every input is real) but not Theta-native;
    the marker must never be confused with SYNTHETIC and must round-trip."""
    src = DataSource.THETA_DERIVED
    assert src.is_real
    assert not src.is_synthetic
    assert DataSource(src.value) is src
    assert src is not DataSource.THETA
