"""Tests for the Yahoo underlying provider (free real intraday bars; reads only).

Locks the parser to Yahoo's actual chart-API JSON shape (captured fixture), and
verifies the shared canonical-grid remap, the no-look-ahead leading-gap skip, the
provider surface, and the store round-trip with YAHOO provenance.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from intraday.contracts import BAR_COLUMNS, DataSource
from intraday.data.provider import DataUnavailable
from intraday.data.quality import assert_no_feed_gap
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider
from intraday.data.yahoo import (
    YahooDataProvider,
    bars_by_day,
    ingest_payload,
    yahoo_interval_for,
    yahoo_payload_to_frame,
)
from intraday.timeutils import bar_close_index

_FIXTURE = Path(__file__).parent / "fixtures" / "yahoo" / "SPY_5m_slice.json"
DAY = date(2026, 6, 1)


class FakeYahooClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def chart(self, symbol: str, *, rng: str, interval: str) -> dict:
        self.calls.append({"symbol": symbol, "rng": rng, "interval": interval})
        return self.payload


def test_parses_real_yahoo_shape():
    payload = json.loads(_FIXTURE.read_text())
    frame = yahoo_payload_to_frame(payload)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 20
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    # First captured SPY 5m close was ~666.35 (real March-2026 slice).
    assert frame["close"].iloc[0] == pytest.approx(666.35, abs=0.01)


def test_malformed_payload_raises():
    with pytest.raises(DataUnavailable):
        yahoo_payload_to_frame({"chart": {"result": [], "error": "x"}})
    with pytest.raises(DataUnavailable):
        yahoo_payload_to_frame({"chart": {"result": [{"timestamp": [], "indicators": {"quote": [{}]}}]}})


def test_bars_by_day_canonical_grid(make_yahoo_payload):
    bars = bars_by_day("SPY", make_yahoo_payload(DAY, "5m"), "5m")[DAY]
    pd.testing.assert_index_equal(bars.frame.index, bar_close_index(DAY, "5m"))
    assert len(bars.frame) == 78
    assert list(bars.frame.columns) == list(BAR_COLUMNS)
    assert bars.source is DataSource.YAHOO
    assert_no_feed_gap(bars.frame.index, "5m")


def test_leading_gap_skipped_no_lookahead(make_yahoo_payload):
    # Drop the opening bar → leading gap → session skipped (never bfilled).
    assert bars_by_day("SPY", make_yahoo_payload(DAY, "5m", drop=(0,)), "5m") == {}


def test_interior_gap_forward_filled(make_yahoo_payload):
    bars = bars_by_day("SPY", make_yahoo_payload(DAY, "5m", drop=(25, 26)), "5m")[DAY]
    assert bars.frame[["open", "high", "low", "close"]].notna().all().all()
    assert bars.frame["close"].iloc[25] == pytest.approx(bars.frame["close"].iloc[24])


def test_provider_get_bars(make_yahoo_payload):
    client = FakeYahooClient(make_yahoo_payload(DAY, "5m"))
    prov = YahooDataProvider(client)
    bars = prov.get_bars("SPY", DAY, "5m")
    assert bars.source is DataSource.YAHOO
    assert len(bars.frame) == 78
    assert client.calls[0]["interval"] == "5m"


def test_provider_options_raise(make_yahoo_payload):
    prov = YahooDataProvider(FakeYahooClient(make_yahoo_payload(DAY, "5m")))
    with pytest.raises(DataUnavailable):
        prov.get_option_chain("SPY", DAY)
    with pytest.raises(DataUnavailable):
        prov.get_option_tape("SPY", DAY)


def test_interval_mapping():
    assert yahoo_interval_for("5m") == "5m"
    assert yahoo_interval_for("1h") == "1h"
    with pytest.raises(DataUnavailable):
        yahoo_interval_for("3m")


def test_ingest_and_replay_yahoo_provenance(tmp_path, make_yahoo_payload):
    store = ParquetStore(tmp_path / "store")
    days = ingest_payload(store, "SPY", "5m", make_yahoo_payload(DAY, "5m"))
    assert days == [DAY]
    replay = StoreBackedProvider(store, DataSource.YAHOO, symbols=["SPY"], interval="5m")
    bars = replay.get_bars("SPY", DAY, "5m")
    assert bars.source is DataSource.YAHOO
    # Wrong declared source must refuse to serve.
    with pytest.raises(DataUnavailable):
        StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY"], interval="5m").get_bars("SPY", DAY, "5m")
