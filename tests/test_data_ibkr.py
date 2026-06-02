"""Tests for the IBKR underlying provider (real intraday bars; reads only).

Covers the load-bearing correctness of the real-data underlying path:
- the parser against IBKR's ACTUAL captured JSON shape (format-locking);
- the start→close grid remap onto the engine's canonical RTH close grid;
- honest coverage accounting (forward-fill counted; sparse sessions skipped);
- the no-feed-gap invariant on remapped real bars;
- the provider surface (underlying only; options raise) + store round-trip.

No network: a captured fixture + a deterministic payload factory stand in for IBKR.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from intraday.config import EngineConfig
from intraday.contracts import BAR_COLUMNS, AssetKind, DataSource
from intraday.data.ibkr import (
    IBKR_CONTRACTS,
    IBKRDataProvider,
    bars_by_day,
    ibkr_step_for,
    ingest_payload,
    payload_to_frame,
)
from intraday.data.provider import DataUnavailable
from intraday.data.quality import assert_no_feed_gap
from intraday.data.store import ParquetStore
from intraday.data.store_provider import StoreBackedProvider
from intraday.timeutils import bar_close_index

_FIXTURE = Path(__file__).parent / "fixtures" / "ibkr" / "SPY_5m_realshape.json"
DAY = date(2026, 6, 1)  # a Monday trading day


class FakeIBKRClient:
    """A read-only IBKR client returning a canned payload (never any order call)."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def price_history(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.payload


# --------------------------------------------------------------------------- #
# Parser — against IBKR's real captured shape
# --------------------------------------------------------------------------- #
def test_payload_to_frame_parses_real_ibkr_shape():
    payload = json.loads(_FIXTURE.read_text())
    frame = payload_to_frame(payload)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == len(payload["time"])
    # tz-aware UTC, sorted, START-labelled (first label is the session open 13:30Z).
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert frame.index[0] == pd.Timestamp("2026-06-02T13:30:00Z")
    # Values round-trip from the raw arrays.
    assert frame["open"].iloc[0] == pytest.approx(757.02)
    assert frame["close"].iloc[-1] == pytest.approx(759.5)
    assert frame["volume"].iloc[0] == pytest.approx(293844)


def test_payload_to_frame_rejects_misaligned_arrays():
    payload = {"time": ["2026-06-01T13:30:00Z"], "open": [1.0], "high": [1.0],
               "low": [1.0], "close": [1.0, 2.0]}  # close misaligned
    with pytest.raises(DataUnavailable):
        payload_to_frame(payload)


def test_payload_to_frame_empty_time_raises():
    with pytest.raises(DataUnavailable):
        payload_to_frame({"time": []})


# --------------------------------------------------------------------------- #
# Grid remap: START labels → canonical CLOSE grid
# --------------------------------------------------------------------------- #
def test_bars_by_day_maps_to_canonical_close_grid(make_ibkr_payload):
    payload = make_ibkr_payload(DAY, "5m")
    by_day = bars_by_day("SPY", payload, "5m")
    assert list(by_day) == [DAY]
    bars = by_day[DAY]
    expected_index = bar_close_index(DAY, "5m")
    # Index is EXACTLY the canonical close grid (start labels shifted +interval).
    pd.testing.assert_index_equal(bars.frame.index, expected_index)
    assert len(bars.frame) == 78  # 5-min RTH bars
    # First close is one interval after the open (09:35 ET = 13:35Z), not 13:30Z.
    assert bars.frame.index[0] == pd.Timestamp("2026-06-01T13:35:00Z")
    assert bars.frame.index[-1] == pd.Timestamp("2026-06-01T20:00:00Z")
    assert list(bars.frame.columns) == list(BAR_COLUMNS)
    assert bars.source is DataSource.IBKR
    assert bars.latency == pd.Timedelta(milliseconds=EngineConfig.default().data.bar_latency_ms)


def test_remapped_bars_have_no_feed_gap(make_ibkr_payload):
    """The canonical-grid remap must satisfy the DESIGN §2.4 feed-gap guard."""
    bars = bars_by_day("SPY", make_ibkr_payload(DAY, "5m"), "5m")[DAY]
    assert_no_feed_gap(bars.frame.index, "5m")  # must not raise


def test_one_minute_grid(make_ibkr_payload):
    bars = bars_by_day("SPY", make_ibkr_payload(DAY, "1m"), "1m")[DAY]
    assert len(bars.frame) == 390
    assert bars.frame.index[0] == pd.Timestamp("2026-06-01T13:31:00Z")
    assert bars.frame.index[-1] == pd.Timestamp("2026-06-01T20:00:00Z")


# --------------------------------------------------------------------------- #
# Coverage honesty: forward-fill counted; sparse sessions skipped
# --------------------------------------------------------------------------- #
def test_missing_bars_are_forward_filled_not_gapped(make_ibkr_payload):
    # Drop a few interior bars; the aligned grid must still be complete + gap-free.
    payload = make_ibkr_payload(DAY, "5m", drop=(20, 21, 40))
    bars = bars_by_day("SPY", payload, "5m")[DAY]
    assert len(bars.frame) == 78
    assert bars.frame[["open", "high", "low", "close"]].notna().all().all()
    assert_no_feed_gap(bars.frame.index, "5m")


def test_sparse_session_is_skipped(make_ibkr_payload):
    # Only ~26% of the session present (like the mid-session real probe) → skip.
    payload = make_ibkr_payload(DAY, "5m", n_bars=20)
    assert bars_by_day("SPY", payload, "5m", min_coverage=0.8) == {}
    # With a permissive threshold it is kept and still grid-aligned.
    kept = bars_by_day("SPY", payload, "5m", min_coverage=0.1)
    assert list(kept) == [DAY]
    assert len(kept[DAY].frame) == 78


@pytest.mark.no_lookahead
def test_leading_gap_session_is_skipped_no_lookahead(make_ibkr_payload):
    """Dropping the OPENING bar leaves a leading gap. Coverage is 77/78=98.7% (it
    passes min_coverage), but with no observed open the session MUST be skipped —
    never back-filled from a future bar. This locks the no-look-ahead fix."""
    payload = make_ibkr_payload(DAY, "5m", drop=(0,))
    assert bars_by_day("SPY", payload, "5m") == {}
    # Multi-bar leading gap is likewise skipped.
    assert bars_by_day("SPY", make_ibkr_payload(DAY, "5m", drop=(0, 1, 2)), "5m") == {}


def test_interior_gap_is_kept_and_forward_filled_only(make_ibkr_payload):
    """An INTERIOR gap (open present) is kept and forward-filled from the PAST —
    no NaN, no future leak, gap-free."""
    bars = bars_by_day("SPY", make_ibkr_payload(DAY, "5m", drop=(30, 31)), "5m")[DAY]
    assert len(bars.frame) == 78
    assert bars.frame[["open", "high", "low", "close"]].notna().all().all()
    # The filled slot equals the LAST KNOWN (prior) bar, never a future one.
    assert bars.frame["close"].iloc[30] == pytest.approx(bars.frame["close"].iloc[29])


def test_index_payload_has_zero_volume(make_ibkr_payload):
    # IND payloads omit volume; reconstructed bars carry zero volume.
    payload = make_ibkr_payload(DAY, "5m", security="IND")
    bars = bars_by_day("SPX", payload, "5m", asset_kind=AssetKind.INDEX)[DAY]
    assert (bars.frame["volume"] == 0.0).all()


# --------------------------------------------------------------------------- #
# Provider surface
# --------------------------------------------------------------------------- #
def test_provider_get_bars_returns_session(make_ibkr_payload):
    client = FakeIBKRClient(make_ibkr_payload(DAY, "5m"))
    prov = IBKRDataProvider(client)
    bars = prov.get_bars("SPY", DAY, "5m")
    assert bars.source is DataSource.IBKR
    assert len(bars.frame) == 78
    # The client was asked for the right contract, read-only.
    call = client.calls[0]
    assert call["contract_id"] == IBKR_CONTRACTS["SPY"].contract_id
    assert call["security_type"] == "STK"
    assert call["outside_rth"] is False


def test_provider_unknown_day_raises(make_ibkr_payload):
    prov = IBKRDataProvider(FakeIBKRClient(make_ibkr_payload(DAY, "5m")))
    with pytest.raises(DataUnavailable):
        prov.get_bars("SPY", date(2020, 1, 2), "5m")


def test_provider_unknown_symbol_raises(make_ibkr_payload):
    prov = IBKRDataProvider(FakeIBKRClient(make_ibkr_payload(DAY, "5m")))
    with pytest.raises(DataUnavailable):
        prov.get_bars("NVDA", DAY, "5m")


def test_provider_options_methods_raise(make_ibkr_payload):
    prov = IBKRDataProvider(FakeIBKRClient(make_ibkr_payload(DAY, "5m")))
    with pytest.raises(DataUnavailable):
        prov.get_option_chain("SPY", DAY)
    with pytest.raises(DataUnavailable):
        prov.get_option_tape("SPY", DAY)


def test_ibkr_step_mapping():
    assert ibkr_step_for("5m") == "FIVE_MINS"
    assert ibkr_step_for("1m") == "ONE_MIN"
    with pytest.raises(DataUnavailable):
        ibkr_step_for("3m")


# --------------------------------------------------------------------------- #
# Ingestion → store → StoreBackedProvider round-trip (IBKR provenance preserved)
# --------------------------------------------------------------------------- #
def test_ingest_and_replay_preserves_ibkr_provenance(tmp_path, make_ibkr_payload):
    store = ParquetStore(tmp_path / "store")
    days = ingest_payload(store, "SPY", "5m", make_ibkr_payload(DAY, "5m"))
    assert days == [DAY]

    replay = StoreBackedProvider(store, DataSource.IBKR, symbols=["SPY"], interval="5m")
    assert replay.trading_days(date(2026, 5, 1), date(2026, 6, 30)) == [DAY]
    bars = replay.get_bars("SPY", DAY, "5m")
    assert bars.source is DataSource.IBKR  # never relabelled
    assert len(bars.frame) == 78

    # A provider declaring the WRONG source must refuse to serve (no relabeling).
    wrong = StoreBackedProvider(store, DataSource.THETA, symbols=["SPY"], interval="5m")
    with pytest.raises(DataUnavailable):
        wrong.get_bars("SPY", DAY, "5m")
